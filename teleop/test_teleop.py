"""
Tests for teleoperation configuration, preflight and the session state machine.

Run:  python teleop/test_teleop.py
Exits non-zero on failure.

The properties these protect are physical. A humanoid that stands before its
support frame is confirmed can fall on somebody; a robot handed an unverified
tracking stream moves unpredictably; a stop that can be refused is not a stop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teleop import TeleopConfig, ConfigError, Preflight, TeleopSession, State


def main() -> int:
    failures, checks = [], 0

    # Configuration is validated, not trusted.
    for bad in [{"arm": "G1_99"}, {"end_effector": "gripper9"},
                {"xr_mode": "telepathy"}, {"headset": "hololens"}]:
        checks += 1
        try:
            TeleopConfig(**bad)
            failures.append(f"accepted invalid configuration {bad}")
        except ConfigError:
            pass

    checks += 1
    cfg = TeleopConfig()
    if "--arm=G1_29" not in cfg.launch_command() or "--ee=inspire1" not in cfg.launch_command():
        failures.append(f"launch command missing expected flags: {cfg.launch_command()}")

    # No credentials or hard-coded robot addresses anywhere in the package.
    # The needles are assembled at runtime so this file does not itself contain
    # the literals it is scanning for.
    checks += 1
    needles = [".".join(["192", "168", "123"]), "pass" + "word=", "ssh " + "unitree@"]
    pkg = Path(__file__).resolve().parent
    for path in sorted(pkg.glob("*.py")):
        text = path.read_text()
        for needle in needles:
            if needle in text:
                failures.append(f"{path.name} contains {needle!r}, "
                                "which looks like a hard-coded address or credential")

    # Preflight blocks standing.
    checks += 1
    s = TeleopSession()
    s.power_on(); s.engage_damping()
    if not s.stand().get("refused"):
        failures.append("robot stood before preflight was confirmed")
    checks += 1
    if s.state != State.DAMPING:
        failures.append(f"refused stand changed state to {s.state}")

    # The suspension check is blocking and cannot be skipped.
    checks += 1
    pf = Preflight()
    pf.confirm_all("op")
    pf.confirm("suspended", False, "op")
    if pf.ready():
        failures.append("preflight reported ready with suspension explicitly failed")
    checks += 1
    if not any(c["key"] == "suspended" for c in pf.report()["explicitly_failed"]):
        failures.append("failed suspension check not reported")

    # Full happy path.
    checks += 1
    s2 = TeleopSession()
    s2.power_on(); s2.engage_damping(); s2.preflight.confirm_all("a.nygmet")
    if s2.stand().get("refused"):
        failures.append("stand refused after preflight passed")

    # Latency above budget warns rather than silently proceeding.
    checks += 1
    if "warning" not in s2.connect_services(latency_ms=180):
        failures.append("latency above budget produced no warning")
    checks += 1
    s3 = TeleopSession(); s3.power_on(); s3.engage_damping()
    s3.preflight.confirm_all("op"); s3.stand()
    if "warning" in s3.connect_services(latency_ms=60):
        failures.append("latency within budget produced a spurious warning")

    # Control requires a named operator and confirmed tracking.
    checks += 1
    if not s2.begin_control("", "task", True).get("refused"):
        failures.append("control began with no named operator")
    checks += 1
    if not s2.begin_control("op", "task", False).get("refused"):
        failures.append("control began with tracking unconfirmed")
    checks += 1
    if s2.begin_control("a.nygmet", "fetch reagent box", True).get("refused"):
        failures.append("control refused when operator and tracking were both present")
    checks += 1
    if not s2.status()["under_control"]:
        failures.append("session did not report being under control")

    # Illegal transitions are refused.
    checks += 1
    s4 = TeleopSession()
    if not s4.stand().get("refused"):
        failures.append("robot stood directly from off")

    # Stop always works and is never gated.
    checks += 1
    out = s2.soft_stop("test stop")
    if out.get("state") != State.DAMPING.value:
        failures.append(f"soft stop did not return to damping: {out}")
    checks += 1
    if s2.status()["under_control"]:
        failures.append("still under control after a soft stop")

    # Ending requires damping first, and records the session.
    checks += 1
    s5 = TeleopSession(); s5.power_on(); s5.engage_damping()
    s5.preflight.confirm_all("op"); s5.stand(); s5.connect_services(50)
    s5.begin_control("op", "task", True)
    if not s5.end_session("done").get("refused"):
        failures.append("session ended directly from active without stopping")
    s5.soft_stop("finished")
    checks += 1
    if s5.end_session("box retrieved").get("refused"):
        failures.append("session would not end from damping")
    checks += 1
    summary = s5.summary()
    if summary["operator"] != "op" or summary["outcome"] != "box retrieved":
        failures.append(f"session summary incomplete: {summary}")

    if failures:
        print(f"FAILED ({len(failures)} of {checks} checks)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"OK — {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
