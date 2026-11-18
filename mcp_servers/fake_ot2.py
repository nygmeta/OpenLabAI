"""
A fake Opentrons OT-2 HTTP server, for tests only.

It answers the handful of endpoints that mcp_servers/ot2_server.py uses and
records every request it receives, so a test can assert what the server did and,
just as important, what it did not do (for example, that nothing was ever sent a
"play" action).

THIS IS NOT A ROBOT AND IT IS NOT EVIDENCE THAT ANYTHING WORKS ON HARDWARE.
It identifies itself as a test double on /health, and scripts/verify_ot2_hardware.py
refuses to file a log from it as a hardware verification. Its endpoints follow
what ot2_server.py expects; they were not generated from the robot's own API
specification, so a passing test here says the server is consistent with this
fake and nothing more.

Protocol-file markers that steer the fake's analysis:
    FAKE_ANALYSIS_ERROR   the analysis reports an error
    FAKE_BIG_VOLUME       the analysis contains a 5000 uL aspirate

Usage in a test:
    fake = FakeOT2()
    port = fake.start()
    ... point the server at 127.0.0.1:<port> ...
    fake.stop()
"""

import json
import re
import threading
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEST_DOUBLE_NAME = "FAKE-OT2 (OpenLabAI test double, not a robot)"


class FakeOT2:
    def __init__(self, labware_endpoint: bool = True):
        self.labware_endpoint = labware_endpoint     # False: behave like a robot with no /labware
        self.requests = []                           # (method, path) in arrival order
        self.actions = []                            # (run_id, actionType)
        self.protocols = {}                          # protocol_id -> file text
        self.runs = {}                               # run_id -> dict
        self._counter = 0
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # ------------------------------------------------------------------ control
    def start(self) -> int:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):               # keep test output quiet
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                outer._record("GET", self.path)
                code, payload = outer.handle("GET", self.path, b"", self.headers)
                self._send(code, payload)

            def do_POST(self):
                body = self._body()
                outer._record("POST", self.path)
                code, payload = outer.handle("POST", self.path, body, self.headers)
                self._send(code, payload)

            def do_DELETE(self):
                outer._record("DELETE", self.path)
                code, payload = outer.handle("DELETE", self.path, b"", self.headers)
                self._send(code, payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_address[1]

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def _record(self, method, path):
        with self._lock:
            self.requests.append((method, path))

    def _next(self, prefix):
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter}"

    def count(self, method, path_prefix) -> int:
        return sum(1 for m, p in self.requests if m == method and p.startswith(path_prefix))

    # ------------------------------------------------------------------ the API
    def handle(self, method, path, body, headers):
        if method == "GET" and path == "/health":
            return 200, {
                "name": TEST_DOUBLE_NAME, "robot_model": "OT-2 Standard (simulated)",
                "api_version": "fake", "fw_version": "fake", "system_version": "fake",
                "robot_serial": "FAKE-0001",
            }
        if method == "GET" and path == "/openapi.json":
            paths = ["/health", "/robot/home", "/runs", "/runs/{run_id}", "/runs/{run_id}/actions",
                     "/protocols", "/protocols/{protocol_id}", "/protocols/{protocol_id}/analyses"]
            if self.labware_endpoint:
                paths.append("/labware")
            return 200, {"openapi": "3.0.0", "info": {"title": "fake ot-2"}, "paths": {p: {} for p in paths}}
        if method == "GET" and path == "/labware":
            if not self.labware_endpoint:
                return 404, {"message": "not found"}
            return 200, {"data": [
                {"loadName": "opentrons_96_tiprack_300ul", "displayName": "300 uL tips", "isTiprack": True,
                 "location": {"slotName": "7"}, "wells": {}},
                {"loadName": "corning_96_wellplate_360ul_flat", "displayName": "Sample plate", "isTiprack": False,
                 "location": {"slotName": "1"}, "wells": {}},
            ]}
        if method == "POST" and path == "/robot/home":
            return 200, {"message": "Homing"}
        if method == "GET" and path == "/runs":
            return 200, {"data": list(self.runs.values())}
        if method == "POST" and path == "/protocols":
            text = self._multipart_text(body, headers.get("Content-Type", ""))
            protocol_id = self._next("proto")
            self.protocols[protocol_id] = text
            return 201, {"data": {"id": protocol_id}}
        m = re.fullmatch(r"/protocols/([^/]+)/analyses", path)
        if method == "GET" and m:
            text = self.protocols.get(m.group(1))
            if text is None:
                return 404, {"message": "no such protocol"}
            errors = [{"detail": "fake analysis error"}] if "FAKE_ANALYSIS_ERROR" in text else []
            commands = [{"commandType": "home", "params": {}}]
            if "FAKE_BIG_VOLUME" in text:
                commands.append({"commandType": "aspirate", "params": {"volume": 5000}})
            return 200, {"data": [{"status": "completed", "errors": errors, "commands": commands}]}
        m = re.fullmatch(r"/protocols/([^/]+)", path)
        if method == "DELETE" and m:
            self.protocols.pop(m.group(1), None)
            return 200, {}
        if method == "POST" and path == "/runs":
            payload = json.loads(body or b"{}")
            run_id = self._next("run")
            self.runs[run_id] = {"id": run_id, "status": "idle", "createdAt": "fake",
                                 "protocolId": payload.get("data", {}).get("protocolId")}
            return 201, {"data": self.runs[run_id]}
        m = re.fullmatch(r"/runs/([^/]+)/actions", path)
        if method == "POST" and m:
            action = json.loads(body or b"{}").get("data", {}).get("actionType")
            self.actions.append((m.group(1), action))
            status = {"play": "running", "pause": "paused", "stop": "stopped"}.get(action)
            if m.group(1) in self.runs and status:
                self.runs[m.group(1)]["status"] = status
            return 201, {"data": {"actionType": action}}
        m = re.fullmatch(r"/runs/([^/]+)", path)
        if method == "DELETE" and m:
            self.runs.pop(m.group(1), None)
            return 200, {}
        return 404, {"message": f"fake robot has no {method} {path}"}

    @staticmethod
    def _multipart_text(body: bytes, content_type: str) -> str:
        """Pull the uploaded file's text out of a multipart body (cgi is gone in 3.13)."""
        msg = message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
        for part in msg.walk():
            if part.get_filename():
                return part.get_payload(decode=True).decode("utf-8", "replace")
        return ""
