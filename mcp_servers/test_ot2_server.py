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

    # ---- create_protocol: a request cannot put code into the file -----------
    def rejected(**request):
        before = set(Path("protocols").glob("*"))
        out = call("create_protocol", **request)
        return (isinstance(out, dict) and out.get("refused") is True
                and set(Path("protocols").glob("*")) == before)

    ok_step = {"type": "transfer", "source": "source:A1", "destination": "destination:B2", "volume_ul": 20}
    check(rejected(name="x", steps=[{**ok_step, "source": 'A1"]); import os; os.system("id") #'}]),
          "a well reference carrying code is rejected and no file is written")
    check(rejected(name="x", steps=[{**ok_step, "label": "ok\nimport os"}]),
          "a step label with a newline is rejected (it would become a line of code)")
    check(rejected(name="../../escape", steps=[ok_step]),
          "a protocol name with path separators is rejected")
    check(rejected(name="x", steps=[{**ok_step, "type": "self_destruct"}]),
          "an unknown step type is rejected instead of being treated as a transfer")
    check(rejected(name="x", steps=[{**ok_step, "volume_ul": -5}]), "a negative volume is rejected")
    check(rejected(name="x", steps=[{**ok_step, "volume_ul": "20"}]), "a volume given as text is rejected")
    check(rejected(name="x", steps=[{"type": "aspirate", "source": "source:A1", "volume_ul": 400}]),
          "a 400 uL aspirate is rejected for the default 300 uL pipette")
    check(rejected(name="x", steps=[{"type": "mix", "source": "source:A1", "volume_ul": 20, "mix_cycles": 9999}]),
          "an unreasonable mix cycle count is rejected")
    check(rejected(name="x", steps=[ok_step], deck={"source": {"load_name": 'p"); evil(', "slot": "1"}}),
          "a labware load name carrying code is rejected")
    check(rejected(name="x", steps=[ok_step], deck={"source": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "99"}}),
          "a deck slot that does not exist on an OT-2 is rejected")
    check(rejected(name="x", steps=[]), "an empty step list is rejected")
    check(rejected(name="x", steps=[ok_step] * 501), "more than 500 steps is rejected")
    plr = call("create_protocol", name="plr_doc", format="pylabrobot", description='x""" \nimport os\n"""',
               steps=[{"type": "transfer", "source": "plate_a", "destination": "plate_b", "volume_ul": 20}])
    tree = ast.parse(Path(plr["saved_to"]).read_text())
    check(not any(isinstance(n, ast.Import) and any(a.name == "os" for a in n.names)
                  and n.lineno < 8 for n in tree.body),
          "a description cannot close the generated docstring and add code")
    check(fake.requests == [], "none of the rejected requests contacted the robot")

    # ---- upload_protocol: moves nothing -------------------------------------
    good = write_protocol(work, "good")
    up = call("upload_protocol", path=good)
    check(up.get("can_start") is True and up.get("run_id", "").startswith("run-"),
          "a clean upload returns a run id and can_start=true")
    import hashlib
    check(up.get("protocol_sha256") == hashlib.sha256(Path(good).read_bytes()).hexdigest(),
          "upload_protocol reports the SHA-256 of the exact file it sent")
    check("idle" in up.get("state", ""), "an uploaded run sits idle")
    check(plays(fake) == [], "upload_protocol sends no play action")
    check(fake.count("POST", "/protocols") == 1 and fake.count("POST", "/runs") == 1,
          "upload_protocol makes one protocol upload and one run")
    requests_before = len(fake.requests)
    missing = call("upload_protocol", path=str(Path(work) / "nope.py"))
    check("error" in missing, "uploading a missing file is an error")
    not_py = Path(work) / "notes.txt"; not_py.write_text("not a protocol")
    check("error" in call("upload_protocol", path=str(not_py)), "a file that is not .py is not uploaded")
    check("error" in call("upload_protocol", path=work), "a directory is not uploaded")
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
    logged = [json.loads(p.read_text()) for p in Path(work).glob("run_logs/*.json")]
    check(any(entry.get("protocol_hash") == up["protocol_sha256"] for entry in logged),
          "the audit record carries the SHA-256 of the protocol that was started")

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

    # ---- the checks fail closed ----------------------------------------------
    over = call("upload_protocol", path=write_protocol(work, "over", "FAKE_OVER_PIPETTE"))
    check(over.get("can_start") is False and "pipette's capacity" in " ".join(over["constraint_violations"]),
          "50 uL through a 20 uL pipette is flagged, although it is under the largest tip size")
    refused = call("start_run", run_id=over["run_id"], confirm=True)
    check(refused.get("refused") is True, "start_run refuses a volume over the pipette's capacity")

    empty = call("upload_protocol", path=write_protocol(work, "empty", "FAKE_NO_COMMANDS"))
    check(empty.get("can_start") is False and empty.get("analysis_problem"),
          "an analysis that produced no commands does not count as a pass")
    refused = call("start_run", run_id=empty["run_id"], confirm=True)
    check(refused.get("refused") is True and "no commands" in refused.get("reason", ""),
          "start_run refuses a run whose analysis checked nothing, even with confirm=true")
    check("did not complete" in ot2_server._analysis_problem({"status": "timeout", "errors": []}, 0),
          "an analysis that timed out is a reason not to start")
    check("did not complete" in ot2_server._analysis_problem({"status": "pending"}, 3),
          "an analysis still pending is a reason not to start")
    check(ot2_server._analysis_problem({"status": "completed", "result": "not-ok"}, 3) != "",
          "an analysis whose result is not ok is a reason not to start")

    ot2_server.HAS_EVALS = False
    blind = call("upload_protocol", path=good)
    refused = call("start_run", run_id=blind["run_id"], confirm=True)
    ot2_server.HAS_EVALS = True
    check(blind.get("can_start") is False and blind.get("constraint_checks_available") is False,
          "when the constraint checks cannot be imported, an upload says it cannot start")
    check(refused.get("refused") is True and "unavailable" in refused.get("reason", ""),
          "start_run refuses when the constraint checks could not be run, even with confirm=true")

    second = call("upload_protocol", path=good)
    refused = call("start_run", run_id=second["run_id"], confirm=True)
    check(refused.get("refused") is True and refused.get("active_runs") == [run_id],
          "start_run refuses while another run is active on the robot")
    check(len(plays(fake)) == n, "none of the refusals above reached the robot")

    # ---- monitoring and recovery ----------------------------------------------
    status = call("get_run_status", run_id=run_id)
    check(status.get("status") == "running" and any("pause_run" in a for a in status["next_actions"]),
          "get_run_status on a running run offers pause and stop")
    check(status.get("protocol_sha256") == up["protocol_sha256"] and status.get("uploaded_by_this_server") is True,
          "get_run_status ties the run back to the protocol hash it was uploaded with")
    fake.runs[run_id]["status"] = "failed"
    fake.runs[run_id]["errors"] = [{"detail": "tip not attached"}]
    status = call("get_run_status", run_id=run_id)
    check(status.get("errors") == ["tip not attached"], "get_run_status reports the robot's errors for a failed run")
    advice = " ".join(status["next_actions"])
    check("inspect the deck" in advice and "cannot be resumed" in advice,
          "a failed run's next actions send a person to the deck and never suggest resuming")
    fake.runs[run_id]["status"] = "stopped"
    fake.runs[run_id].pop("errors")

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
    refused = call("resume_run", run_id=run_id)
    check(refused.get("refused") is True and [a for _, a in fake.actions][-1] == "pause",
          "resume_run refuses without confirm=true and sends nothing")
    call("resume_run", run_id=run_id, confirm=True)
    stopped = call("stop_run", run_id="run-uploaded-elsewhere")
    acts = [a for _, a in fake.actions]
    check(acts[-3:] == ["pause", "play", "stop"], "pause, resume and stop each send the matching action")
    check(stopped.get("action") == "stop", "stop_run needs no confirm and works on any run id")

    # ---- the rest -------------------------------------------------------------
    check("Unknown tool" in call("no_such_tool"), "an unknown tool name is reported, not executed")
    status = call("get_run_status")
    check(status.get("status") in ("stopped", "running", "paused", "idle"), "get_run_status reports the latest run")
    check(call("home_robot").get("refused") is True and call("home_robot", confirm="true").get("refused") is True,
          "home_robot refuses unless confirm is exactly true")
    refused = call("home_robot", confirm=True)
    check(refused.get("refused") is True and "active" in refused.get("reason", ""),
          "home_robot refuses while a run is active")
    check(fake.count("POST", "/robot/home") == 0, "no refused home reached the robot")
    for active_id in refused["active_runs"]:
        call("stop_run", run_id=active_id)
    homed = call("home_robot", confirm=True)
    check(homed.get("status") == "homed" and fake.count("POST", "/robot/home") == 1,
          "home_robot with confirm=true and no active run calls /robot/home once")
    check(homed.get("audit_logged") is True, "homing is written to the audit log")
    fake.stop()

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
