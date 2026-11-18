"""
Tests for scripts/verify_ot2_hardware.py, run against a fake robot.

Run:  python scripts/test_verify_ot2_hardware.py
Exits non-zero on failure.

The point of these tests is the script's honesty rules: no log when no robot
answers, no hardware log from a test double, a failure when read_deck would have
returned a mock deck, and no motion without explicit confirmation. They do not
verify anything about a physical robot.
"""

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_servers"))
from fake_ot2 import FakeOT2   # noqa: E402

SCRIPT = ROOT / "scripts" / "verify_ot2_hardware.py"
HARDWARE_LOGS = ROOT / "hardware_logs"
CHECKS = 0
FAILURES = []


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def run_script(port, scratch, *extra, stdin=""):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--port", str(port), "--operator", "test", *extra],
        input=stdin, capture_output=True, text=True, timeout=180, cwd=scratch)


def logs_in(directory):
    return sorted(Path(directory).glob("ot2_verification_*.json")) if Path(directory).exists() else []


def newest(directory):
    files = logs_in(directory)
    return json.loads(files[-1].read_text()) if files else None


def step(log, name):
    return next((s for s in log["steps"] if s["step"] == name), {})


def main():
    scratch = tempfile.mkdtemp(prefix="openlabai_verify_test_")

    # ---- no robot: no log -----------------------------------------------------
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); closed = sock.getsockname()[1]; sock.close()
    out = Path(scratch) / "logs_none"
    r = run_script(closed, scratch, "--log-dir", str(out))
    check(r.returncode == 2, "exit code 2 when no robot answers")
    check(logs_in(out) == [], "no log is written when no robot answers")
    check("no log was written" in r.stdout, "the message says that nothing was run")

    # ---- a test double is never filed as hardware evidence ------------------------
    before = logs_in(HARDWARE_LOGS)
    fake = FakeOT2(); port = fake.start()
    r = run_script(port, scratch)
    check(r.returncode == 3, "exit code 3 when asked to log a test double into hardware_logs/")
    check(logs_in(HARDWARE_LOGS) == before, "hardware_logs/ is unchanged after that refusal")
    check(fake.requests and all(p in ("/health",) for _, p in fake.requests),
          "the refusal happens before anything else is sent to the robot")
    fake.stop()

    # ---- read-only tier, simulated robot, explicit log dir -----------------------
    fake = FakeOT2(); port = fake.start()
    out = Path(scratch) / "logs_readonly"
    r = run_script(port, scratch, "--log-dir", str(out))
    log = newest(out)
    check(r.returncode == 0 and log is not None, f"read-only run exits 0 and writes a log ({r.stderr[-200:]!r})")
    if log:
        check(log["simulated"] is True and log["verdict"].startswith("simulated"), "the log says simulated, not verified")
        check(logs_in(out)[-1].name.endswith("_SIMULATED.json"), "the file name carries _SIMULATED")
        check({"read_deck", "get_run_status", "create_protocol"} <= set(log["tools_passed"]),
              "read_deck, get_run_status and create_protocol pass against the fake")
        check(step(log, "upload_protocol")["status"] == "skipped" and step(log, "home_robot")["status"] == "skipped",
              "upload and motion are skipped unless requested")
        check(set(log["never_exercised_by_this_script"]) == {"start_run", "pause_run", "resume_run", "stop_run"},
              "the log lists the four tools this script never runs")
        check("robot_serial" not in log["robot"] and log["robot"]["robot_serial_sha256_12"],
              "the serial number is hashed, not stored")
        check(log["robot_openapi"]["has_labware_path"] is True, "the robot's own path list is recorded")
    check(fake.count("POST", "/protocols") == 0 and fake.count("POST", "/robot/home") == 0 and fake.actions == [],
          "the read-only tier uploads nothing, homes nothing and starts nothing")
    fake.stop()

    # ---- upload tier ----------------------------------------------------------------
    fake = FakeOT2(); port = fake.start()
    out = Path(scratch) / "logs_upload"
    r = run_script(port, scratch, "--log-dir", str(out), "--allow-upload")
    log = newest(out)
    check(r.returncode == 0 and log and step(log, "upload_protocol")["status"] == "pass", "the upload tier passes")
    check(fake.count("POST", "/protocols") == 1 and fake.count("POST", "/runs") == 1, "one protocol and one run were created")
    check(fake.count("DELETE", "/runs/") == 1 and fake.count("DELETE", "/protocols/") == 1 and not fake.runs and not fake.protocols,
          "the idle run and the protocol are deleted again")
    check(fake.actions == [], "the upload tier never starts a run")
    fake.stop()

    # ---- motion tier: only after typing HOME ---------------------------------------
    fake = FakeOT2(); port = fake.start()
    out = Path(scratch) / "logs_motion_declined"
    r = run_script(port, scratch, "--log-dir", str(out), "--allow-motion", stdin="no\n")
    log = newest(out)
    check(fake.count("POST", "/robot/home") == 0, "the robot is not homed unless the operator types HOME")
    check(log and step(log, "home_robot")["status"] == "declined", "a declined confirmation is logged as declined")
    fake.stop()

    fake = FakeOT2(); port = fake.start()
    out = Path(scratch) / "logs_motion"
    r = run_script(port, scratch, "--log-dir", str(out), "--allow-motion", stdin="HOME\n")
    log = newest(out)
    check(fake.count("POST", "/robot/home") == 1 and log and step(log, "home_robot")["status"] == "pass",
          "typing HOME homes the robot once and the step passes")
    fake.stop()

    # ---- a robot with no /labware: read_deck must fail, not pass on mock data ---------
    fake = FakeOT2(labware_endpoint=False); port = fake.start()
    out = Path(scratch) / "logs_nolabware"
    r = run_script(port, scratch, "--log-dir", str(out))
    log = newest(out)
    check(r.returncode == 1 and log and log["verdict"] == "failed", "a mock deck makes the run fail")
    check(log and step(log, "read_deck")["status"] == "fail" and "mock deck" in step(log, "read_deck")["detail"],
          "read_deck is marked failed and the log says why")
    check(log and "no /labware path" in step(log, "read_deck")["detail"],
          "the log notes that this robot's own API lists no /labware path")
    fake.stop()

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
