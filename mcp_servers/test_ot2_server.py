"""
Tests for the OT-2 MCP server, run against a fake robot (mcp_servers/fake_ot2.py).

Run:  python mcp_servers/test_ot2_server.py
Exits non-zero on failure.

What these tests are, and are not. They check that the server sends the right
requests and, above all, that the approval gate holds: nothing is ever sent a
"play" action unless confirm is exactly True after a clean upload. They run
against a test double, so they say nothing about how the server behaves on a
physical OT-2. Hardware is checked separately, by scripts/verify_ot2_hardware.py.
"""

import ast
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_servers"))
sys.argv = [sys.argv[0]]            # ot2_server parses sys.argv when it is imported

# protocols/ and run_logs/ are relative to the working directory, and the logger
# creates run_logs/ when it is imported, so move to a scratch directory first.
WORK = tempfile.mkdtemp(prefix="openlabai_ot2_test_")
os.chdir(WORK)

import ot2_server                   # noqa: E402
from fake_ot2 import FakeOT2        # noqa: E402

CHECKS = 0
FAILURES = []


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def call(tool, /, **arguments):
    """Call a tool the way the MCP layer does and return the parsed JSON, or the raw text."""
    result = asyncio.run(ot2_server.call_tool(tool, arguments))
    text = result[0].text
    try:
        return json.loads(text)
    except ValueError:
        return text


def point_at(port):
    ot2_server.OT2_BASE = f"http://127.0.0.1:{port}"


def write_protocol(directory, name, marker=""):
    path = Path(directory) / f"{name}.py"
    path.write_text(
        "from opentrons import protocol_api\n"
        "metadata = {'protocolName': 'test', 'apiLevel': '2.13'}\n"
        f"# {marker}\n"
        "def run(protocol: protocol_api.ProtocolContext):\n"
        "    protocol.home()\n")
    return str(path)


def plays(fake):
    return [a for a in fake.actions if a[1] == "play"]


def main():
    work = WORK

    # ---- the tool list -------------------------------------------------------
    tools = asyncio.run(ot2_server.list_tools())
    names = {t.name for t in tools}
    check(len(tools) == 9, f"the OT-2 server publishes 9 tools, found {len(tools)}")
    check({"read_deck", "get_run_status", "home_robot", "create_protocol", "upload_protocol",
           "start_run", "pause_run", "resume_run", "stop_run"} == names, "tool names match the README")

    # ---- read_deck: connected, unreachable, and endpoint missing -------------
    fake = FakeOT2()
    point_at(fake.start())
    deck = call("read_deck")
    check(deck.get("connected") is True and len(deck.get("labware", [])) == 2,
          "read_deck reads two labware items from a reachable robot and says connected")
    check(deck["labware"][0]["slot"] == "7" and deck["labware"][0]["is_tiprack"] is True,
          "read_deck maps slot and tip-rack fields")
    fake.stop()

    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); closed_port = sock.getsockname()[1]; sock.close()
    point_at(closed_port)
    deck = call("read_deck")
    check(deck.get("connected") is False and deck.get("mode") == "mock",
          "read_deck on an unreachable robot returns the mock deck, labelled connected=false")

    fake = FakeOT2(labware_endpoint=False)
    point_at(fake.start())
    deck = call("read_deck")
    check(deck.get("connected") is False and deck.get("mode") == "mock",
          "read_deck falls back to the mock deck when the robot has no /labware endpoint "
          "(callers must check connected before treating a deck as real)")
    fake.stop()

    # ---- create_protocol ----------------------------------------------------
    fake = FakeOT2()
    point_at(fake.start())
    made = call("create_protocol", name="unit test", steps=[
        {"type": "transfer", "source": "source:A1", "destination": "destination:B2", "volume_ul": 20}])
    saved = Path(made["saved_to"])
    check(saved.exists() and made["format"] == "opentrons", "create_protocol writes an Opentrons protocol file")
    tree = ast.parse(saved.read_text())
    check(any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body),
          "the generated protocol parses and defines run()")
    check(fake.requests == [], "create_protocol never contacts the robot")

    # ---- upload_protocol: moves nothing -------------------------------------
    good = write_protocol(work, "good")
    up = call("upload_protocol", path=good)
    check(up.get("can_start") is True and up.get("run_id", "").startswith("run-"),
          "a clean upload returns a run id and can_start=true")
    check("idle" in up.get("state", ""), "an uploaded run sits idle")
    check(plays(fake) == [], "upload_protocol sends no play action")
    check(fake.count("POST", "/protocols") == 1 and fake.count("POST", "/runs") == 1,
          "upload_protocol makes one protocol upload and one run")
    requests_before = len(fake.requests)
    missing = call("upload_protocol", path=str(Path(work) / "nope.py"))
    check("error" in missing, "uploading a missing file is an error")
    check(len(fake.requests) == requests_before, "a missing file is rejected without contacting the robot")

    # ---- start_run: the approval gate ---------------------------------------
    run_id = up["run_id"]
    refused = call("start_run", run_id=run_id, confirm=False)
    check(refused.get("refused") is True, "start_run refuses with confirm=false")
    refused = call("start_run", run_id=run_id)
    check(refused.get("refused") is True, "start_run refuses with confirm missing")
    refused = call("start_run", run_id=run_id, confirm="true")
    check(refused.get("refused") is True, "start_run refuses confirm given as the string 'true'")
    refused = call("start_run", run_id=run_id, confirm=1)
    check(refused.get("refused") is True, "start_run refuses confirm=1 (only boolean true counts)")
    check(plays(fake) == [], "no refusal path ever reached the robot")

    started = call("start_run", run_id=run_id, confirm=True)
    check(started.get("action") == "play" and len(plays(fake)) == 1,
          "start_run with confirm=true after a clean upload sends exactly one play action")
    check(started.get("audit_logged") is True and any(Path(work).glob("run_logs/*.json")),
          "starting a run writes an audit record")

    # ---- start_run: refusals that confirm=true must not override --------------
    bad = call("upload_protocol", path=write_protocol(work, "bad", "FAKE_ANALYSIS_ERROR"))
    check(bad.get("can_start") is False and bad["analysis_errors"], "analysis errors block can_start")
    n = len(plays(fake))
    refused = call("start_run", run_id=bad["run_id"], confirm=True)
    check(refused.get("refused") is True and "analysis" in refused.get("reason", ""),
          "start_run refuses a run whose analysis reported errors, even with confirm=true")

    big = call("upload_protocol", path=write_protocol(work, "big", "FAKE_BIG_VOLUME"))
    check(big.get("can_start") is False and big["constraint_violations"],
          "a 5000 uL aspirate is flagged by the deck constraint check")
    refused = call("start_run", run_id=big["run_id"], confirm=True)
    check(refused.get("refused") is True and "constraint" in refused.get("reason", ""),
          "start_run refuses a run that fails deck constraints, even with confirm=true")

    refused = call("start_run", run_id="run-never-uploaded", confirm=True)
    check(refused.get("refused") is True and "not uploaded" in refused.get("reason", ""),
          "start_run refuses a run id this server never analysed, even with confirm=true")
    check(len(plays(fake)) == n, "none of the three refusals above reached the robot")

    # ---- a logging failure must not stop a robot action, and must be reported --
    real_logger = ot2_server.RunLogger

    class BrokenLogger:
        def __init__(self, *a, **k):
            raise RuntimeError("disk full")

    ot2_server.RunLogger = BrokenLogger
    again = call("upload_protocol", path=good)
    started = call("start_run", run_id=again["run_id"], confirm=True)
    ot2_server.RunLogger = real_logger
    check(started.get("action") == "play", "the run still starts when the audit logger fails")
    check(started.get("audit_logged") is False, "a failed audit write is reported as audit_logged=false")

    # ---- pause, resume, stop -------------------------------------------------
    call("pause_run", run_id=run_id)
    call("resume_run", run_id=run_id)
    stopped = call("stop_run", run_id="run-uploaded-elsewhere")
    acts = [a for _, a in fake.actions]
    check(acts[-3:] == ["pause", "play", "stop"], "pause, resume and stop each send the matching action")
    check(stopped.get("action") == "stop", "stop_run needs no confirm and works on any run id")

    # ---- the rest -------------------------------------------------------------
    check("Unknown tool" in call("no_such_tool"), "an unknown tool name is reported, not executed")
    status = call("get_run_status")
    check(status.get("status") in ("stopped", "running", "paused", "idle"), "get_run_status reports the latest run")
    homed = call("home_robot")
    check(homed.get("status") == "homed" and fake.count("POST", "/robot/home") == 1, "home_robot calls /robot/home once")
    fake.stop()

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
