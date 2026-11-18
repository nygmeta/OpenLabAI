"""
Tests for the RemoteUSB transport and the USB relay (remote_usb.py, usb_relay.py).

Run:  python mcp_servers/test_remote_usb.py
Exits non-zero on failure.

A real relay listens on a real TCP socket on localhost, with a fake USB device
behind it. The final tests run PyLabRobot's actual Hamilton backend over RemoteUSB
against a fake instrument that answers firmware strings.

NOT tested here: a physical Hamilton, PyUSB, libusb, or the macOS/Linux driver
stack. This checks the transport and the relay's safeguards, nothing about hardware.
"""

import asyncio
import base64
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_servers"))

import warnings                      # noqa: E402
warnings.filterwarnings("ignore")
import remote_usb as proto           # noqa: E402
import usb_relay                     # noqa: E402

CHECKS = 0
FAILURES = []
TOKEN = "t" * 40


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


class FakeDevice:
    """Stands in for pylabrobot.io.usb.USB. Answers Hamilton-style 'idNNNN' firmware strings."""

    def __init__(self):
        self.writes, self.setup_calls, self.stop_calls = [], 0, 0
        self.respond, self.fail_write_with = True, None
        self._queue = None

    def queue(self):
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def setup(self):
        self.setup_calls += 1

    async def stop(self):
        self.stop_calls += 1

    async def write(self, data, timeout=None):
        if self.fail_write_with:
            raise self.fail_write_with
        self.writes.append(bytes(data))
        if self.respond:
            m = re.match(rb"^(\w{2}\w{2})id(\d{4})", data)
            await self.queue().put(m.group(1) + b"id" + m.group(2) + b"er00/00" if m else bytes(data))

    async def read(self, timeout=None, size=None):
        try:
            return await asyncio.wait_for(self.queue().get(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("fake device: nothing to read")


class RelayThread:
    def __init__(self, device, audit_path, token=TOKEN):
        self.relay = usb_relay.Relay(device, token, audit_path, {"id_vendor": 0x08AF, "id_product": 0x8000, "name": "fake"})
        self.relay.auth_delay = 0.02
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.port = self.loop.run_until_complete(self.relay.start("127.0.0.1", 0))
        self.ready.set()
        self.loop.run_forever()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def raw(port, role="control", token=TOKEN, proof=None):
    """A bare protocol connection, for testing what the relay refuses."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    hello = proto.read_frame_sync(sock)
    sock.sendall(proto.encode_frame({"role": role, "proof": proof or proto.make_proof(token, hello["nonce"], role)}))
    return sock, proto.read_frame_sync(sock)


def rpc(sock, **request):
    sock.sendall(proto.encode_frame(request))
    return proto.read_frame_sync(sock)


def audit_events(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def main():
    work = tempfile.mkdtemp(prefix="openlabai_relay_test_")
    audit = os.path.join(work, "audit.jsonl")
    device = FakeDevice()
    relay = RelayThread(device, audit)
    port = relay.port

    # ---- authentication ---------------------------------------------------------------
    sock, verdict = raw(port, proof="0" * 64)
    check(verdict.get("ok") is False and "authentication" in verdict["error"], "a wrong token is refused")
    sock.close()
    sock, verdict = raw(port, token="x" * 40)
    check(verdict.get("ok") is False, "a different token is refused")
    sock.close()
    sock, verdict = raw(port, role="admin")
    check(verdict.get("ok") is False, "an unknown role is refused")
    sock.close()
    check(device.setup_calls == 0 and device.writes == [], "nothing reached the device before authentication")
    check(sum(1 for e in audit_events(audit) if e["event"] == "auth_failed") == 3, "every failed attempt is in the audit log")

    sock, verdict = raw(port)
    check(verdict.get("ok") is True and verdict["role"] == "control", "the right token is accepted")
    check(rpc(sock, id=1, op="ping").get("ok") is True, "ping works")

    # ---- roles and single-owner rules ------------------------------------------------------
    refused = rpc(sock, id=2, op="read", timeout=0.1)
    check(refused.get("ok") is False and refused["error"] == "NotAllowed", "a control connection cannot read")
    second, verdict = raw(port)
    check(verdict.get("ok") is False and "busy" in verdict["error"], "a second controller is refused while one is connected")
    second.close()
    rsock, verdict = raw(port, role="read")
    check(verdict.get("ok") is True, "a reader can connect alongside the controller")
    check(rpc(rsock, id=1, op="write", data_b64="AA==", timeout=1).get("error") == "NotAllowed", "a read connection cannot write")
    rsock.close()
    sock.close()
    time.sleep(0.2)
    again, verdict = raw(port)
    check(verdict.get("ok") is True, "a new controller can connect once the first has gone")
    again.close()
    time.sleep(0.1)

    # ---- the PyLabRobot IO contract -------------------------------------------------------
    async def contract():
        io = proto.RemoteUSB("127.0.0.1", port, TOKEN, read_timeout=2, write_timeout=2)
        await io.setup()
        check(device.setup_calls == 1, "setup() opens the device through the relay")
        payload = bytes(range(256))
        await io.write(payload)
        check(device.writes[-1] == payload, "binary data survives a write unchanged")
        check(await io.read(timeout=2) == payload, "binary data survives a read unchanged")

        await io.write(b"C0RFid0001")
        check(await io.read(timeout=2) == b"C0RFid0001er00/00", "a firmware string and its answer round-trip")

        try:
            await io.read(timeout=0.2)
            check(False, "an empty read should time out")
        except TimeoutError:
            check(True, "a device read timeout arrives as TimeoutError, which PyLabRobot expects")

        device.fail_write_with = ValueError("pump fault")
        try:
            await io.write(b"x")
            check(False, "a device error should surface")
        except RuntimeError as exc:
            check("pump fault" in str(exc), "a device error arrives as RuntimeError with its message")
        device.fail_write_with = None

        # a read that is waiting must not block a write (PyLabRobot reads and writes from different threads)
        device.respond = False
        got = {}
        reader = threading.Thread(target=lambda: got.update(data=asyncio.run(io.read(timeout=3))))
        reader.start()
        time.sleep(0.3)
        device.respond = True
        started = time.time()
        await io.write(b"C0QMid0002")
        reader.join(5)
        check(got.get("data") == b"C0QMid0002er00/00" and time.time() - started < 2,
              "a write goes through while a read is waiting, and the read gets the answer")

        info = io.serialize()
        check({"id_vendor", "id_product", "human_readable_device_name"} <= set(info) and TOKEN not in json.dumps(info),
              "serialize() has the keys PyLabRobot expects and never contains the token")
        await io.stop()
        check(device.stop_calls == 1, "stop() closes the device through the relay")
        try:
            await io.write(b"x")
            check(False, "writing after stop should fail")
        except RuntimeError as exc:
            check("not connected" in str(exc), "writing after stop says it is not connected")

        bad = proto.RemoteUSB("127.0.0.1", port, "wrong" * 8)
        try:
            await bad.setup()
            check(False, "setup with a wrong token should fail")
        except RuntimeError as exc:
            check("refused" in str(exc), "setup with a wrong token fails clearly")

    asyncio.run(contract())

    # ---- a malformed or hostile client cannot take the relay down ----------------------------------
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    proto.read_frame_sync(s)
    s.sendall(struct.pack(">I", proto.MAX_FRAME + 1))
    s.settimeout(3)
    try:
        closed = s.recv(10) == b""
    except (ConnectionResetError, socket.timeout):
        closed = True
    check(closed, "an oversize frame makes the relay drop the connection")
    s.close()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    proto.read_frame_sync(s)
    s.sendall(struct.pack(">I", 5) + b"not{}")
    time.sleep(0.2)
    s.close()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.close()
    sock, verdict = raw(port)
    check(verdict.get("ok") is True, "the relay still serves clients after oversize, malformed and dropped connections")
    sock.close()
    time.sleep(0.1)

    # ---- the audit log ---------------------------------------------------------------------------------
    events = audit_events(audit)
    writes = [e for e in events if e["event"] == "write"]
    check(any(w.get("command", "").startswith("C0RFid0001") for w in writes), "writes are audited with the command text")
    check(TOKEN not in Path(audit).read_text() and not any("proof" in e for e in events), "the audit log never holds the token or a proof")
    relay.stop()

    # ---- start-up safeguards -------------------------------------------------------------------------------
    for host in ("127.0.0.1", "localhost", "::1"):
        usb_relay.check_bind(host, allow_network=False)
    check(True, "loopback addresses are accepted")
    try:
        usb_relay.check_bind("0.0.0.0", allow_network=False)
        check(False, "a network bind should need --allow-network")
    except SystemExit:
        check(True, "binding to a non-local address needs --allow-network")
    usb_relay.check_bind("0.0.0.0", allow_network=True)
    short = Path(work) / "short"
    short.write_text("abc")
    try:
        usb_relay.load_token(str(short))
        check(False, "a short token should be refused")
    except SystemExit:
        check(True, "a token shorter than 16 characters is refused")
    target = Path(work) / "newtoken"
    usb_relay.generate_token(str(target))
    check(len(usb_relay.load_token(str(target))) == 64 and oct(target.stat().st_mode)[-3:] == "600",
          "a generated token is 64 hex characters and readable only by its owner")
    try:
        usb_relay.generate_token(str(target))
        check(False, "generate_token should not overwrite")
    except SystemExit:
        check(True, "generate_token refuses to overwrite an existing file")

    # ---- through PyLabRobot's real Hamilton backend ------------------------------------------------------
    device2 = FakeDevice()
    relay2 = RelayThread(device2, os.path.join(work, "audit2.jsonl"))

    async def through_plr():
        from pylabrobot.liquid_handling.backends.hamilton.base import HamiltonLiquidHandler
        from pylabrobot.resources import STARLetDeck
        backend = proto.RemoteSTARBackend("127.0.0.1", relay2.port, TOKEN)
        backend.set_deck(STARLetDeck())
        check(isinstance(backend.io, proto.RemoteUSB), "RemoteSTARBackend is a STARBackend whose transport is RemoteUSB")
        check(TOKEN not in json.dumps(backend.serialize(), default=str), "the backend serializes without the token")
        await HamiltonLiquidHandler.setup(backend)              # the base class: transport + reading thread, no instrument init
        try:
            reply = await backend.send_command(module="C0", command="RF")
            check("er00/00" in str(reply), "PyLabRobot's own send_command gets its answer through the relay")
        finally:
            await HamiltonLiquidHandler.stop(backend)
        check(device2.writes and device2.writes[0].startswith(b"C0RF"), "the firmware string the relay sent is the one PyLabRobot built")

    try:
        asyncio.run(through_plr())
    except Exception as exc:
        check(False, f"PyLabRobot's Hamilton backend over RemoteUSB failed: {type(exc).__name__}: {exc}")
    relay2.stop()

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
