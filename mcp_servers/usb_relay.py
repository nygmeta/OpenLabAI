#!/usr/bin/env python3
"""
USB relay: the other half of RemoteUSB (see remote_usb.py for why).

Run it on the Mac or Linux machine the instrument is cabled to. It owns the USB
device through PyLabRobot's own USB layer and forwards setup, stop, write and read
for an authenticated client. Nothing on the Windows PC with Venus needs to change.

    python mcp_servers/usb_relay.py --generate-token ~/.openlabai_relay_token
    python mcp_servers/usb_relay.py --token-file ~/.openlabai_relay_token                # Hamilton STAR, localhost only
    python mcp_servers/usb_relay.py --token-file ... --bind 0.0.0.0 --allow-network     # reachable from the lab network

It serves exactly one device, named on the command line (default: Hamilton STAR,
vendor 0x08AF, product 0x8000), never an arbitrary one. At most one controller and
one reader are connected at a time. Every connection and every write is appended to
an audit file.

SAFETY. Whoever can authenticate can send raw firmware commands, including ones that
move the arm. The token is the only protection and the traffic is not encrypted:
keep the relay on localhost, or on a trusted lab network, or behind an SSH tunnel.
If the controlling program disconnects mid-run, the relay cannot stop the robot; the
instrument finishes its current command and then idles, exactly as if the program
had crashed.
"""

import argparse
import asyncio
import base64
import datetime as dt
import hmac
import ipaddress
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_usb as proto                                     # noqa: E402


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, allow_network: bool) -> None:
    if not is_loopback(host) and not allow_network:
        raise SystemExit(f"Refusing to listen on {host}: that is not a local address. "
                         f"Add --allow-network if you mean to expose the relay on the network.")


def load_token(path: str) -> str:
    token = Path(path).expanduser().read_text().strip()
    if len(token) < proto.MIN_TOKEN_LENGTH:
        raise SystemExit(f"The token in {path} is shorter than {proto.MIN_TOKEN_LENGTH} characters.")
    return token


def generate_token(path: str) -> None:
    target = Path(path).expanduser()
    if target.exists():
        raise SystemExit(f"{target} already exists; not overwriting it.")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    print(f"Wrote a new token to {target} (readable only by you). Copy it to the client machine securely.")


class Relay:
    def __init__(self, device, token: str, audit_path: str, device_info: dict):
        self.device, self.token, self.device_info = device, token, device_info
        self.audit_path = audit_path
        self.active: set = set()
        self.server = None
        self.auth_delay = 0.5                                  # slows guessing; tests turn it down

    def audit(self, event: str, **fields) -> None:
        record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "event": event, **fields}
        try:
            with open(self.audit_path, "a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass                                               # never let logging stop a robot command

    async def start(self, host: str, port: int):
        self.server = await asyncio.start_server(self.handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        peer = "%s:%s" % (writer.get_extra_info("peername") or ("?", "?"))[:2]
        role = None
        try:
            nonce = secrets.token_hex(16)
            writer.write(proto.encode_frame({"version": proto.PROTOCOL_VERSION, "nonce": nonce,
                                             "device": self.device_info}))
            await writer.drain()
            hello = await asyncio.wait_for(proto.read_frame_async(reader), timeout=10)
            requested = hello.get("role")
            if requested not in proto.ROLES or not isinstance(hello.get("proof"), str) or not hmac.compare_digest(
                    hello["proof"], proto.make_proof(self.token, nonce, requested)):    # constant-time compare
                self.audit("auth_failed", peer=peer)
                await asyncio.sleep(self.auth_delay)
                await self._reply(writer, {"ok": False, "error": "authentication failed"})
                return
            if requested in self.active:
                self.audit("refused_busy", peer=peer, role=requested)
                await self._reply(writer, {"ok": False, "error": f"busy: a {requested} connection is already open"})
                return
            role = requested
            self.active.add(role)
            self.audit("connected", peer=peer, role=role)
            await self._reply(writer, {"ok": True, "role": role})

            while True:
                request = await proto.read_frame_async(reader)
                await self._reply(writer, await self.dispatch(role, request, peer))
        except (ConnectionError, asyncio.TimeoutError, proto.FrameError) as exc:
            if role:
                self.audit("disconnected", peer=peer, role=role, reason=type(exc).__name__)
        finally:
            if role:
                self.active.discard(role)
            writer.close()

    @staticmethod
    async def _reply(writer, payload: dict):
        writer.write(proto.encode_frame(payload))
        await writer.drain()

    async def dispatch(self, role: str, request: dict, peer: str) -> dict:
        rid, op = request.get("id"), request.get("op")
        if op not in proto.ROLE_OPS[role]:
            self.audit("refused_op", peer=peer, role=role, op=str(op)[:20])
            return {"id": rid, "ok": False, "error": "NotAllowed", "message": f"'{op}' is not allowed on a {role} connection"}
        try:
            if op == "ping":
                return {"id": rid, "ok": True}
            if op == "setup":
                await self.device.setup()
                self.audit("device_setup", peer=peer)
                return {"id": rid, "ok": True}
            if op == "stop":
                await self.device.stop()
                self.audit("device_stop", peer=peer)
                return {"id": rid, "ok": True}
            if op == "write":
                data = base64.b64decode(request["data_b64"], validate=True)
                self.audit("write", peer=peer, bytes=len(data), command=data.decode("ascii", "replace")[:120])
                await self.device.write(data, timeout=float(request.get("timeout", 30)))
                return {"id": rid, "ok": True}
            if op == "read":
                size = request.get("size")
                data = await self.device.read(timeout=float(request.get("timeout", 30)), size=size)
                return {"id": rid, "ok": True, "data_b64": base64.b64encode(bytes(data)).decode()}
        except TimeoutError as exc:
            return {"id": rid, "ok": False, "error": "TimeoutError", "message": str(exc)}
        except Exception as exc:                               # report it, keep serving
            self.audit("device_error", peer=peer, op=op, error=type(exc).__name__, message=str(exc)[:200])
            return {"id": rid, "ok": False, "error": type(exc).__name__, "message": str(exc)[:300]}
        return {"id": rid, "ok": False, "error": "BadRequest", "message": "unknown operation"}


def real_device(vendor_id: int, product_id: int):
    """PyLabRobot's own USB layer. Needs pyusb and libusb-package; no driver swap on macOS or Linux."""
    from pylabrobot.io.usb import USB
    return USB(id_vendor=vendor_id, id_product=product_id, human_readable_device_name="Instrument (relay)")


async def serve(args) -> None:
    check_bind(args.bind, args.allow_network)
    token = load_token(args.token_file)
    info = {"id_vendor": args.vendor_id, "id_product": args.product_id, "name": "OpenLabAI USB relay"}
    relay = Relay(real_device(args.vendor_id, args.product_id), token, args.audit_log, info)
    port = await relay.start(args.bind, args.port)
    print(f"USB relay for {args.vendor_id:#06x}:{args.product_id:#06x} listening on {args.bind}:{port}. "
          f"Audit log: {args.audit_log}. Ctrl-C to stop.")
    async with relay.server:
        await relay.server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--generate-token", metavar="FILE", help="write a new random token to FILE and exit")
    ap.add_argument("--token-file", help="file holding the shared token (16+ characters)")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-network", action="store_true", help="permit --bind to a non-local address")
    ap.add_argument("--vendor-id", type=lambda s: int(s, 0), default=0x08AF, help="USB vendor id (default: Hamilton)")
    ap.add_argument("--product-id", type=lambda s: int(s, 0), default=0x8000, help="USB product id (default: STAR)")
    ap.add_argument("--audit-log", default="relay_audit.jsonl")
    args = ap.parse_args()
    if args.generate_token:
        generate_token(args.generate_token)
        return
    if not args.token_file:
        ap.error("--token-file is required (create one with --generate-token FILE)")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
