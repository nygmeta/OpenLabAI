"""
RemoteUSB: a PyLabRobot transport ("driver") that reaches a USB instrument through a relay.

Why this exists. PyLabRobot talks to a Hamilton STAR over USB with libusb. On
Windows that means replacing Hamilton's own driver (with Zadig), which breaks Venus
on that PC. On macOS and Linux libusb needs no driver swap. RemoteUSB lets the
program that drives the robot run anywhere, while a small relay (usb_relay.py) runs
on a Mac or Linux machine that the robot is cabled to. The Windows PC and its
Hamilton driver are never touched.

What it does NOT do. A USB device has one owner at a time. While the relay holds the
robot, Venus cannot use it, and the other way round. Moving from one to the other
means moving the cable (or a USB switch), not swapping a driver.

It plugs into PyLabRobot at the transport layer. PyLabRobot's Hamilton backends hold
an IO object with setup(), stop(), write() and read(); RemoteUSB implements the same
four calls over TCP. See RemoteSTARBackend below.

Wire format. Each message is a 4-byte big-endian length followed by that many bytes
of UTF-8 JSON, at most MAX_FRAME. A connection has a role:
    control   setup, stop, write, ping
    read      read, ping
Two connections are used because PyLabRobot reads and writes from different
threads, and a read that is waiting for the robot must not block a write.

Authentication. The relay sends a random nonce and the client answers with
HMAC-SHA256(token, nonce + role). The token itself never crosses the network. This
stops strangers on the network from sending firmware commands; it does not encrypt
the traffic, so use it on a trusted lab network or through an SSH tunnel.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import struct
import threading
from typing import Optional

from pylabrobot.io.io import IOBase

PROTOCOL_VERSION = 1
MAX_FRAME = 1 << 20                      # 1 MiB; Hamilton firmware strings are a few hundred bytes
ROLES = ("control", "read")
ROLE_OPS = {"control": {"setup", "stop", "write", "ping"}, "read": {"read", "ping"}}
MIN_TOKEN_LENGTH = 16


class FrameError(Exception):
    """A message that does not follow the wire format."""


def encode_frame(obj: dict) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME:
        raise FrameError("message too large")
    return struct.pack(">I", len(body)) + body


def decode_body(body: bytes) -> dict:
    try:
        obj = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("a message must be a JSON object")
    return obj


def read_frame_sync(sock: socket.socket) -> dict:
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME:
        raise FrameError(f"frame of {length} bytes exceeds the {MAX_FRAME} byte limit")
    return decode_body(_recv_exactly(sock, length))


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks = []
    while n:
        chunk = sock.recv(n)
        if not chunk:
            raise ConnectionError("connection closed")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


async def read_frame_async(reader: asyncio.StreamReader) -> dict:
    try:
        header = await reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        if length > MAX_FRAME:
            raise FrameError(f"frame of {length} bytes exceeds the {MAX_FRAME} byte limit")
        return decode_body(await reader.readexactly(length))
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed") from exc


def make_proof(token: str, nonce: str, role: str) -> str:
    return hmac.new(token.encode(), f"{nonce}:{role}".encode(), hashlib.sha256).hexdigest()


class _Channel:
    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()
        self.next_id = 0


class RemoteUSB(IOBase):
    """A PyLabRobot IO object that forwards setup/stop/write/read to a USB relay."""

    def __init__(self, host: str, port: int, token: str,
                 human_readable_device_name: str = "Hamilton Liquid Handler (via relay)",
                 id_vendor: int = 0x08AF, id_product: int = 0x8000,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0, write_timeout: float = 30.0):
        super().__init__()
        self._host, self._port, self._token = host, port, token
        self._name = human_readable_device_name
        self._id_vendor, self._id_product = id_vendor, id_product
        self.connect_timeout, self.read_timeout, self.write_timeout = connect_timeout, read_timeout, write_timeout
        self._channels = {role: _Channel() for role in ROLES}
        self.device_info: dict = {}

    # ------------------------------------------------------------------ connection
    def _connect(self, role: str) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self.connect_timeout)
        try:
            sock.settimeout(self.connect_timeout)
            hello = read_frame_sync(sock)
            if hello.get("version") != PROTOCOL_VERSION or "nonce" not in hello:
                raise RuntimeError("that is not an OpenLabAI USB relay, or it speaks a different protocol version")
            sock.sendall(encode_frame({"role": role, "proof": make_proof(self._token, hello["nonce"], role)}))
            verdict = read_frame_sync(sock)
            if not verdict.get("ok"):
                raise RuntimeError(f"relay refused the {role} connection: {verdict.get('error')}")
            self.device_info = hello.get("device", {})
        except Exception:
            sock.close()
            raise
        self._channels[role].sock = sock

    def _drop(self, role: str) -> None:
        ch = self._channels[role]
        if ch.sock is not None:
            try:
                ch.sock.close()
            except OSError:
                pass
            ch.sock = None

    def _call(self, role: str, request: dict, timeout: float) -> dict:
        ch = self._channels[role]
        with ch.lock:
            if ch.sock is None:
                raise RuntimeError(f"not connected to the USB relay at {self._host}:{self._port}; call setup() first")
            ch.next_id += 1
            try:
                ch.sock.settimeout(timeout + 5.0)               # the relay's own timeout fires first
                ch.sock.sendall(encode_frame({**request, "id": ch.next_id}))
                response = read_frame_sync(ch.sock)
            except (socket.timeout, OSError, FrameError) as exc:
                self._drop(role)
                raise RuntimeError(f"lost the connection to the USB relay ({type(exc).__name__}: {exc})") from exc
            if response.get("id") != ch.next_id:
                self._drop(role)
                raise RuntimeError("the USB relay answered out of order; connection dropped")
            return response

    @staticmethod
    def _raise_for(response: dict) -> None:
        if response.get("ok"):
            return
        if response.get("error") == "TimeoutError":
            raise TimeoutError(response.get("message", "timeout"))
        raise RuntimeError(f"USB relay: {response.get('error')}: {response.get('message')}")

    # ------------------------------------------------------------------ the IO contract
    async def setup(self):
        loop = asyncio.get_running_loop()
        for role in ROLES:
            await loop.run_in_executor(None, self._connect, role)
        response = await loop.run_in_executor(None, self._call, "control", {"op": "setup"}, self.connect_timeout + 30)
        try:
            self._raise_for(response)
        except Exception:
            await self.stop()
            raise

    async def stop(self):
        loop = asyncio.get_running_loop()
        try:
            if self._channels["control"].sock is not None:
                await loop.run_in_executor(None, self._call, "control", {"op": "stop"}, 10.0)
        except Exception:
            pass
        finally:
            for role in ROLES:
                self._drop(role)

    async def write(self, data: bytes, timeout: Optional[float] = None):
        timeout = self.write_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, self._call, "control",
            {"op": "write", "data_b64": base64.b64encode(data).decode(), "timeout": timeout}, timeout)
        self._raise_for(response)

    async def read(self, timeout: Optional[float] = None, size: Optional[int] = None) -> bytes:
        timeout = self.read_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, self._call, "read", {"op": "read", "timeout": timeout, "size": size}, timeout)
        self._raise_for(response)
        return base64.b64decode(response["data_b64"])

    def serialize(self) -> dict:
        # PyLabRobot's Hamilton backend removes the first three keys. The token is never serialized.
        return {"id_vendor": self._id_vendor, "id_product": self._id_product,
                "human_readable_device_name": self._name, "relay_host": self._host, "relay_port": self._port}


def RemoteSTARBackend(host: str, port: int, token: str, **star_kwargs):
    """A PyLabRobot STARBackend whose USB transport is a RemoteUSB.

    Use it exactly like STARBackend():
        lh = LiquidHandler(backend=RemoteSTARBackend("lab-pi.local", 8765, token), deck=STARLetDeck())
    """
    from pylabrobot.liquid_handling.backends import STARBackend
    backend = STARBackend(**star_kwargs)
    backend.io = RemoteUSB(host, port, token, id_vendor=0x08AF, id_product=0x8000)
    return backend
