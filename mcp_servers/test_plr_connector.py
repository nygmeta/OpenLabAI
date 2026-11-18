"""
Tests for the PyLabRobot connector (plr_connector.py) and its MCP server (plr_server.py).

Run:  python mcp_servers/test_plr_connector.py
Exits non-zero on failure.

Everything here runs in simulation on PyLabRobot's own tip and volume tracking.
Nothing touches hardware, and the live OT-2 path (PyLabRobot's OpentronsOT2Backend)
is NOT exercised: a stand-in backend takes its place where a test needs a "live" one.
The tests check that bad plans are rejected, that an approval is bound to the exact
commands, and that the approval gate and the live-backend gate hold.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_servers"))
sys.argv = [sys.argv[0]]

WORK = tempfile.mkdtemp(prefix="openlabai_plr_test_")
os.chdir(WORK)                      # the audit logger writes run_logs/ relative to here

import warnings                     # noqa: E402
warnings.filterwarnings("ignore")
import plr_connector as plr         # noqa: E402
import plr_server                   # noqa: E402

CHECKS = 0
FAILURES = []


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def sim(spec, steps):
    return asyncio.run(plr.simulate(spec, steps))


def call(tool, /, **arguments):
    result = asyncio.run(plr_server.call_tool(tool, arguments))
    return json.loads(result[0].text)


def deck(**volumes):
    return {"labware": [
        {"name": "tips", "type": "opentrons_96_tiprack_300ul", "slot": 7},
        {"name": "src", "type": "cor_96_wellplate_360uL_Fb", "slot": 1,
         "volumes_ul": volumes or {"A1": 100, "A2": 100}},
        {"name": "dst", "type": "cor_96_wellplate_360uL_Fb", "slot": 4}]}


def t(source="src:A1", destination="dst:B2", volume_ul=50, **extra):
    return {"source": source, "destination": destination, "volume_ul": volume_ul, **extra}


def rejects(spec, steps, message):
    try:
        plr.validate_plan(spec, steps)
        check(False, f"{message} (was accepted)")
    except plr.PlanError:
        check(True, message)


def main():
    # ---- a good plan ----------------------------------------------------------
    good = sim(deck(), [t()])
    commands = [c["command"] for c in good["trace"]]
    check(good["valid"] and good["errors"] == [], "a clean plan simulates without errors")
    check(commands == ["setup", "pick_up_tips", "aspirate", "dispense", "drop_tips", "stop"],
          f"the recorded command stream is the expected sequence, got {commands}")
    check(good["final_volumes_ul"] == {"dst:B2": 50.0, "src:A1": 50.0}, "final volumes come from PyLabRobot's volume tracking")
    check(good["summary"]["tips_used"] == 1 and good["summary"]["total_volume_moved_ul"] == 50, "summary counts tips and volume")
    check(len(good["approval_token"]) == 16, "a valid plan gets a 16-character approval token")

    # ---- the approval token ---------------------------------------------------------
    again = sim(deck(), [t()])
    check(good["approval_token"] == again["approval_token"] and good["trace_hash"] == again["trace_hash"],
          "the same plan always gets the same token")
    check(sim(deck(), [t(volume_ul=51)])["approval_token"] != good["approval_token"], "a different volume changes the token")
    check(sim(deck(A1=101, A2=100), [t()])["approval_token"] != good["approval_token"], "a different starting volume changes the token")
    check(sim(deck(), [t(destination="dst:B3")])["approval_token"] != good["approval_token"], "a different destination changes the token")

    # ---- plans PyLabRobot's tracking rejects --------------------------------------------
    bad = sim(deck(), [t(volume_ul=250)])
    check(not bad["valid"] and bad["errors"][0]["type"] == "TooLittleLiquidError" and bad["errors"][0]["step"] == 0
          and bad["approval_token"] is None, "aspirating more than the well holds is rejected and gets no token")
    over = sim(deck(), [t(volume_ul=400)])
    check(not over["valid"] and "VolumeExceedsTip" in over["errors"][0]["message"], "a volume above the tip's capacity is rejected")
    many = sim(deck(A1=300, A2=300), [t(destination=f"dst:{r}{c}", volume_ul=1)
                                       for c in range(1, 13) for r in "ABCDEFGH"] + [t(volume_ul=1)])
    check(not many["valid"] and many["errors"][0]["step"] == 96 and "NoTipAvailable" in many["errors"][0]["message"],
          "a 97th transfer with new tips runs out of tips at step 96")

    # ---- carry-over -----------------------------------------------------------------
    carry = sim(deck(), [t(), t(source="src:A2", destination="dst:B3", new_tip=False)])
    check(carry["valid"] and len(carry["warnings"]) == 1 and "carry-over" in carry["warnings"][0],
          "reusing one tip across two source wells is flagged as a carry-over risk")
    check(sim(deck(), [t(), t(destination="dst:B3", new_tip=False)])["warnings"] == [],
          "reusing a tip on the same source well is not flagged")
    check(sim(deck(), [t(), t(source="src:A2", destination="dst:B3")])["warnings"] == [],
          "a new tip for each transfer is not flagged")
    check(carry["summary"]["tips_used"] == 1 and carry["summary"]["commands"] == 8, "two transfers on one tip use one pickup")

    # ---- malformed plans never reach PyLabRobot -----------------------------------------
    rejects({"labware": [{"name": "x", "type": "os.system", "slot": 1}]}, [t()], "a labware type outside the allow-list is rejected")
    base = deck()
    rejects({"labware": base["labware"] + [{"name": "tips", "type": "cor_96_wellplate_360uL_Fb", "slot": 2}]}, [t()], "duplicate labware names are rejected")
    rejects({"labware": base["labware"] + [{"name": "x", "type": "cor_96_wellplate_360uL_Fb", "slot": 1}]}, [t()], "two labware items in one slot are rejected")
    rejects({"labware": base["labware"] + [{"name": "x", "type": "cor_96_wellplate_360uL_Fb", "slot": 12}]}, [t()], "slot 12 is rejected")
    rejects({"labware": [l for l in base["labware"] if l["name"] != "tips"]}, [t()], "a deck without a tip rack is rejected")
    rejects({"labware": [{"name": "../x", "type": "cor_96_wellplate_360uL_Fb", "slot": 1}]}, [t()], "a labware name with odd characters is rejected")
    rejects(base, [], "an empty step list is rejected")
    rejects(base, [t()] * (plr.MAX_STEPS + 1), "more than 500 steps are rejected")
    rejects(base, [t(source="src:Z9")], "a malformed well is rejected")
    rejects(base, [t(source="nowhere:A1")], "a step naming labware that is not on the deck is rejected")
    rejects(base, [t(source="tips:A1")], "a tip rack cannot be a source")
    rejects(base, [t(volume_ul=0)], "a zero volume is rejected")
    rejects(base, [t(volume_ul=True)], "a boolean volume is rejected")
    rejects(base, [t(new_tip="yes")], "new_tip must be a boolean")
    rejects(base, [{"op": "mix", **t()}], "only the transfer op is supported")

    # ---- the MCP server ------------------------------------------------------------
    described = call("describe_connector")
    check(described["backend"] == "simulation" and described["evidence"]["level"] == "simulation only",
          "describe_connector reports simulation and its evidence level")
    check("opentrons_96_tiprack_300ul" in described["labware_types"], "describe_connector lists the allowed labware")

    card = call("plan_protocol", deck=deck(), steps=[t()])
    pid, token = card["plan_id"], card["approval_token"]
    check(card["valid"] and "nothing has moved" in card["state"] and "trace" not in card, "plan_protocol returns a card, not the trace")
    check("trace" in call("get_plan", plan_id=pid, include_trace=True) and "trace" not in call("get_plan", plan_id=pid),
          "get_plan includes the trace only on request")
    check("plan_error" in call("plan_protocol", deck=deck(), steps=[t(source="src:Z9")]), "a malformed plan comes back as a plan_error")
    check(call("plan_protocol", deck=deck(), steps=[t(volume_ul=250)])["valid"] is False, "an unsafe plan comes back with valid=false")
    check("error" in call("get_plan", plan_id="plan-nope"), "an unknown plan id is an error")

    # ---- the approval gate ------------------------------------------------------------
    for label, extra in (("missing", {}), ("false", {"confirm": False}), ("the string 'true'", {"confirm": "true"}), ("1", {"confirm": 1})):
        check(call("execute_plan", plan_id=pid, approval_token=token, **extra).get("refused") is True,
              f"execute_plan refuses with confirm {label}")
    check("unknown plan_id" in call("execute_plan", plan_id="plan-nope", approval_token=token, confirm=True)["reason"], "execute_plan refuses an unknown plan")
    check("token" in call("execute_plan", plan_id=pid, approval_token="0" * 16, confirm=True)["reason"], "execute_plan refuses the wrong token")
    badcard = call("plan_protocol", deck=deck(), steps=[t(volume_ul=250)])
    check(call("execute_plan", plan_id=badcard["plan_id"], approval_token="x", confirm=True).get("refused") is True,
          "execute_plan refuses a plan that did not simulate cleanly")

    ran = call("execute_plan", plan_id=pid, approval_token=token, confirm=True)
    check(ran.get("executed") is True and ran["matches_simulation"] is True and ran["hardware_touched"] is False,
          "an approved plan runs in simulation and its end state matches the simulation")
    check(ran["audit_logged"] is True and any(Path(WORK).glob("run_logs/*.json")), "execution writes an audit record")

    # ---- the approval is bound to the exact plan and the exact commands ----------------------
    card2 = call("plan_protocol", deck=deck(), steps=[t()])
    plr_server.PLANS[card2["plan_id"]]["steps"] = [t(volume_ul=80)]          # changed after approval
    refused = call("execute_plan", plan_id=card2["plan_id"], approval_token=card2["approval_token"], confirm=True)
    check(refused.get("refused") is True and "token" in refused["reason"], "a plan changed after approval is refused")

    card3 = call("plan_protocol", deck=deck(), steps=[t()])
    real_version = plr.plr_version
    plr.plr_version = lambda: "99.9.9"                                          # a different PyLabRobot would generate different commands
    refused = call("execute_plan", plan_id=card3["plan_id"], approval_token=card3["approval_token"], confirm=True)
    plr.plr_version = real_version
    check(refused.get("refused") is True and "token" in refused["reason"], "a changed PyLabRobot version needs a new approval")

    # ---- what runs live is what was approved ----------------------------------------------
    captured = []
    real_make_live = plr_server.make_live

    async def stand_in(spec):
        backend = plr.TraceBackend(num_channels=1)
        lh, labware = plr.build_deck(spec, backend)
        await lh.setup()
        captured.append(backend)
        return lh, labware

    plr_server.make_live = stand_in
    card4 = call("plan_protocol", deck=deck(), steps=[t(), t(source="src:A2", destination="dst:B3")])
    ran = call("execute_plan", plan_id=card4["plan_id"], approval_token=card4["approval_token"], confirm=True)
    plr_server.make_live = real_make_live
    check(ran.get("executed") is True and len(captured) == 1, "execute_plan runs on whatever live backend the factory returns")
    check(plr.sha(plr.canonical(captured[0].trace)) == card4["trace_hash"],
          "the commands sent to the live backend hash to the trace that was approved")

    # ---- a failure part-way is reported, not hidden ---------------------------------------------
    class FailingBackend(plr.TraceBackend):
        async def send_command(self, command, data=None):
            if command == "aspirate":
                raise RuntimeError("simulated pump fault")
            return await super().send_command(command, data)

    async def failing(spec):
        lh, labware = plr.build_deck(spec, FailingBackend(num_channels=1))
        await lh.setup()
        return lh, labware

    plr_server.make_live = failing
    card5 = call("plan_protocol", deck=deck(), steps=[t()])
    ran = call("execute_plan", plan_id=card5["plan_id"], approval_token=card5["approval_token"], confirm=True)
    plr_server.make_live = real_make_live
    check(ran.get("executed") is False and "pump fault" in ran["error"]["message"] and "unknown" in ran["warning"],
          "a fault during a run is reported with a warning that the robot's state is unknown")

    # ---- a logging failure is reported ---------------------------------------------------------
    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("disk full")

    real_logger = plr_server.RunLogger
    plr_server.RunLogger = Broken
    card6 = call("plan_protocol", deck=deck(), steps=[t()])
    ran = call("execute_plan", plan_id=card6["plan_id"], approval_token=card6["approval_token"], confirm=True)
    plr_server.RunLogger = real_logger
    check(ran.get("executed") is True and ran["audit_logged"] is False, "a failed audit write is reported as audit_logged=false")

    # ---- the live backend gate ------------------------------------------------------------------
    logs = Path(tempfile.mkdtemp(prefix="openlabai_plr_logs_"))
    real_dir = plr.HARDWARE_LOG_DIR
    plr.HARDWARE_LOG_DIR = logs
    plr_server.CONFIG.update(backend="ot2", accept_unverified=False)
    check(plr_server.backend_allowed()[0] is False, "a live backend with no hardware log is not allowed")
    refused = call("execute_plan", plan_id=pid, approval_token=token, confirm=True)
    check(refused.get("refused") is True and "hardware log" in refused["reason"], "execute_plan refuses an unverified live backend")

    def log(name, **fields):
        (logs / name).write_text(json.dumps({"connector": "plr_server", "backend": "ot2", "simulated": False,
                                              "verdict": "verified", **fields}))

    log("a_simulated.json", simulated=True)
    log("b_other_connector.json", connector="ot2_server")
    log("c_failed.json", verdict="failed")
    log("d_other_backend.json", backend="star")
    (logs / "e_corrupt.json").write_text("{not json")
    check(plr_server.backend_allowed()[0] is False, "simulated, failed, other-connector, other-backend and corrupt logs do not count as evidence")
    plr_server.CONFIG["accept_unverified"] = True
    check(plr_server.backend_allowed()[0] is True, "--accept-unverified lets an operator run an unverified backend")
    plr_server.CONFIG["accept_unverified"] = False
    log("f_real.json")
    allowed, evidence = plr_server.backend_allowed()
    check(allowed and evidence["level"] == "hardware-verified" and evidence["logs"] == ["f_real.json"],
          "a verified, non-simulated log from this connector and backend counts")
    plr_server.CONFIG.update(backend="simulation", accept_unverified=False)
    plr.HARDWARE_LOG_DIR = real_dir

    # ---- the server will not even start an unverified live backend -------------------------------
    result = subprocess.run([sys.executable, str(ROOT / "mcp_servers" / "plr_server.py"), "--backend", "ot2", "--host", "127.0.0.1"],
                            input="", capture_output=True, text=True, timeout=60, cwd=WORK)
    check(result.returncode == 2 and "Refusing to start" in result.stderr, "plr_server.py --backend ot2 refuses to start without evidence or --accept-unverified")

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
