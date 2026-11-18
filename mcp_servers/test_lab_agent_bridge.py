"""
Tests for the Lab Agent bridge (lab_agent_bridge.py) and the plan_workflow tool of plr_server.py.

Run:  python mcp_servers/test_lab_agent_bridge.py
Exits non-zero on failure.

Two real Lab Agent workflows (mcp_servers/fixtures/lab_agent/) check that the bridge reads Lab
Agent's actual format. The rest are hand-built workflows for the edge cases. Everything runs in
simulation; Lab Agent's voice pipeline and a robot are not involved. The central property: a
workflow the connector cannot do completely is refused whole, and no plan exists for it.
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_servers"))
sys.argv = [sys.argv[0]]
WORK = tempfile.mkdtemp(prefix="openlabai_bridge_test_")
os.chdir(WORK)
warnings.filterwarnings("ignore")

import lab_agent_bridge as bridge    # noqa: E402
import plr_connector as plr          # noqa: E402
import plr_server                    # noqa: E402

FIX = ROOT / "mcp_servers" / "fixtures" / "lab_agent"
CHECKS, FAILURES = 0, []


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def call(tool, /, **arguments):
    return json.loads(asyncio.run(plr_server.call_tool(tool, arguments))[0].text)


def load(name):
    return json.loads((FIX / name).read_text())


def wf(ops, labware=None):
    """A small hand-built workflow in Lab Agent's shape."""
    return {"workflow_id": "WF-TEST", "labware": labware or [
        {"id": "src", "type": "corning_96_wellplate_360ul", "role": "samples", "slot": None},
        {"id": "dst", "type": "corning_96_wellplate_360ul", "role": "assay", "slot": None},
        {"id": "tips", "type": "opentrons_96_tiprack_300ul", "role": "tips", "slot": None}], "reagents": [], "operations": ops}


def t(src="src:A1", dst="dst:B2", vol=50, unit="uL", **extra):
    s, d = src.split(":"), dst.split(":")
    return {"op": "transfer", "source": {"labware": s[0], "well": s[1]}, "dest": {"labware": d[0], "well": d[1]},
            "volume": {"value": vol, "unit": unit}, **extra}


def refusal_reasons(workflow, **kw):
    r = bridge.convert(workflow, **kw)
    return (r.get("ok"), " | ".join(x["reason"] for x in r.get("refusals", [])))


def main():
    before = len(plr_server.PLANS)

    # ---- real Lab Agent workflows ------------------------------------------------------------------
    dil = call("plan_workflow", workflow=load("serial_dilution.json"))
    check(dil.get("refused") is True and "outside what this pipette can do" in json.dumps(dil["refusals"]),
          "Lab Agent's nanolitre serial dilution is refused because its volumes are below the pipette's range")
    check(dil["readback"].startswith("I cannot run this on the OpenLabAI connector") and "Nothing has been planned" in dil["readback"],
          "the refusal comes with a sentence a voice layer can speak")
    elisa = call("plan_workflow", workflow=load("elisa_first_62_ops.json"))
    reasons = " ".join(r["reason"] for r in elisa["refusals"])
    check(elisa.get("refused") is True and "timed temperature hold" in reasons, "the ELISA is refused because of its incubation")
    check(elisa["transfers_before_first_unsupported"] == 58, "the refusal says that 58 transfers come before the first unsupported step")
    check(len(plr_server.PLANS) == before, "a refused workflow creates no plan")

    part = load("elisa_first_62_ops.json")
    part["operations"] = part["operations"][:58]                         # only the real transfers
    card = call("plan_workflow", workflow=part)
    check(card.get("valid") is True and card["source"] == "lab_agent_workflow" and card["summary"]["steps"] == 58,
          f"the 58 real ELISA transfers make a valid plan ({card.get('errors') or card.get('refusals')})")
    check("The plan checks out: 58 transfers" in card["readback"] and card["readback"].endswith("Say confirm to run it, or cancel."),
          "an accepted plan gets a readback that ends by asking for confirmation")
    check(card["assumptions"] and "Starting volumes were assumed" in card["assumptions"][0], "the assumed starting volumes are stated")
    check(len(plr_server.PLANS) == before + 1, "an accepted workflow creates exactly one plan")

    # ---- the approval gate still applies -----------------------------------------------------------------
    check(call("execute_plan", plan_id=card["plan_id"], approval_token=card["approval_token"]).get("refused") is True,
          "an accepted plan still cannot run without confirm=true")
    ran = call("execute_plan", plan_id=card["plan_id"], approval_token=card["approval_token"], confirm=True)
    check(ran.get("executed") is True and ran["hardware_touched"] is False and ran["matches_simulation"] is True,
          "with confirm=true and the token it runs, in simulation")

    # ---- each unsupported operation is refused, by name ---------------------------------------------------------
    for kind, expect in (("incubate", "timed temperature hold"), ("wait", "timed pause"), ("wash", "plate-wash"),
                         ("move_labware", "moving labware"), ("manual_step", "for a person"), ("mix", "mixing in place"), ("dance", "unknown operation")):
        ok, why = refusal_reasons(wf([t(), {"op": kind}]))
        check(not ok and expect in why, f"operation '{kind}' is refused with its reason")
    ok, why = refusal_reasons(wf([t(mix_after={"reps": 3, "volume": 40})]))
    check(not ok and "mixing after a transfer" in why, "mixing after a transfer is refused")
    check(bridge.convert(wf([t(), {"op": "incubate"}, t()]))["transfers_before_first_unsupported"] == 1, "the hint counts transfers before the first unsupported step")

    # ---- volumes -----------------------------------------------------------------------------------------------
    for vol, unit, good in ((0.5, "uL", False), (300, "uL", True), (301, "uL", False), (0.1, "mL", True), (400, "nL", False), (50, "µL", True), (0, "uL", False)):
        ok, why = refusal_reasons(wf([t(vol=vol, unit=unit)]))
        check(ok is good, f"{vol} {unit} is {'accepted' if good else 'refused'}")
    check(not refusal_reasons(wf([t(vol=True)]))[0], "a boolean volume is refused")
    check(not refusal_reasons(wf([t(vol=5, unit="gallons")]))[0], "an unknown unit is refused")
    check(not refusal_reasons(wf([{**t(), "volume": None}]))[0], "a missing volume is refused")

    # ---- labware and wells ---------------------------------------------------------------------------------------
    for label, bad in (("an unknown labware type", wf([t()], [{"id": "src", "type": "os.system", "slot": None}, {"id": "dst", "type": "corning_96_wellplate_360ul", "slot": None}])),
                       ("two labware items with one id", wf([t()], [{"id": "src", "type": "corning_96_wellplate_360ul"}, {"id": "src", "type": "corning_96_wellplate_360ul"}])),
                       ("a labware id with odd characters", wf([t()], [{"id": "../x", "type": "corning_96_wellplate_360ul"}])),
                       ("a source that is a tip rack", wf([t(src="tips:A1")])),
                       ("a well that does not exist", wf([t(src="src:Z9")])),
                       ("labware that is not in the workflow", wf([t(src="nowhere:A1")])),
                       ("a transfer without a source", wf([{"op": "transfer", "dest": {"labware": "dst", "well": "A1"}, "volume": {"value": 5, "unit": "uL"}}]))):
        check(not bridge.convert(bad)["ok"], f"{label} is refused")
    for junk in (None, [], "text", {}, {"labware": [], "operations": []}, {"labware": [], "operations": [1, None, "x"]}):
        r = bridge.convert(junk)
        check(r["ok"] is False and r["refusals"], f"malformed input {str(junk)[:30]!r} is refused without raising")

    # ---- slots and tips ----------------------------------------------------------------------------------------
    r = bridge.convert(wf([t()]))
    slots = {x["name"]: x["slot"] for x in r["deck"]["labware"]}
    check(slots == {"src": 1, "dst": 2, "tips": 7}, f"unplaced labware gets deterministic OT-2 slots, got {slots}")
    given = wf([t()], [{"id": "src", "type": "corning_96_wellplate_360ul", "slot": "5"}, {"id": "dst", "type": "corning_96_wellplate_360ul", "slot": "5"},
                       {"id": "tips", "type": "opentrons_96_tiprack_300ul", "slot": "9"}])
    slots = {x["name"]: x["slot"] for x in bridge.convert(given)["deck"]["labware"]}
    check(slots["src"] == 5 and slots["dst"] != 5 and slots["tips"] == 9, "a slot given by the workflow is honoured and a clash is moved")
    r = bridge.convert(wf([t()], [{"id": "src", "type": "corning_96_wellplate_360ul"}, {"id": "dst", "type": "corning_96_wellplate_360ul"}]))
    check(r["ok"] and any(x["name"] == "openlabai_tips" for x in r["deck"]["labware"]) and "300 uL rack was added" in " ".join(r["assumptions"]),
          "a workflow with no tip rack gets one added, and says so")
    check(bridge.convert(wf([t()]))["deck"] == bridge.convert(wf([t()]))["deck"], "conversion is deterministic")

    # ---- starting volumes --------------------------------------------------------------------------------------
    r = bridge.convert(wf([t(vol=100), t(vol=50, dst="dst:B3")]))
    check(r["deck"]["labware"][0]["volumes_ul"] == {"A1": 155.0}, "a source's assumed volume is what the plan takes plus 5 uL dead volume")
    r = bridge.convert(wf([t(src="src:A1", dst="dst:A1", vol=80), t(src="dst:A1", dst="dst:A2", vol=60)]))
    check(r["ok"] and "A1" not in (r["deck"]["labware"][1].get("volumes_ul") or {}), "a well filled by an earlier step needs no assumed starting volume")
    ok, why = refusal_reasons(wf([t(vol=80)]), loaded_volumes_ul={"src:A1": 50})
    check(not ok and "loaded with 50 uL" in why, "a loaded volume that is too small is refused")
    r = bridge.convert(wf([t(vol=80)]), loaded_volumes_ul={"src:A1": 120})
    check(r["ok"] and r["deck"]["labware"][0]["volumes_ul"] == {"A1": 120.0} and not r["assumptions"], "a loaded volume replaces the assumption")
    big = call("plan_workflow", workflow=wf([t(src="res:A1", dst="dst:A1", vol=300)] * 55, [
        {"id": "res", "type": "nest_12_reservoir_15ml", "role": "reagent"}, {"id": "dst", "type": "corning_96_wellplate_360ul"},
        {"id": "tips", "type": "opentrons_96_tiprack_300ul", "role": "tips"}]))
    check(big.get("refused") is True and "holds at most" in json.dumps(big["refusals"]),
          "a source that would need more than its well can hold is refused (PyLabRobot does not check this itself)")

    # ---- what the connector's own checks add -----------------------------------------------------------------------
    carry = call("plan_workflow", workflow=wf([t(), t(src="src:A2", dst="dst:B3", new_tip=False)]))
    check(carry["valid"] and carry["warnings"] and "warning" in carry["readback"], "a carry-over warning reaches the readback")
    exhausted = call("plan_workflow", workflow=wf([t(dst=f"dst:{r}{c}", vol=1) for c in range(1, 13) for r in "ABCDEFGH"] + [t(vol=1)]))
    check(exhausted["valid"] is False and "plan check failed at step 97" in exhausted["readback"], "running out of tips is reported by step in the readback")
    overflow = call("plan_workflow", workflow=wf([t(src="src:A1", dst="dst:B1", vol=250), t(src="src:A2", dst="dst:B1", vol=250)]))
    check(overflow["valid"] is False and overflow["errors"][0]["type"] == "TooLittleVolumeError", "overfilling a destination well is caught by the simulation")

    # ---- tool surface and the connector fix ----------------------------------------------------------------
    names = {x.name for x in asyncio.run(plr_server.list_tools())}
    check(names == {"describe_connector", "plan_protocol", "plan_workflow", "get_plan", "execute_plan"}, "the server publishes five tools")
    try:
        asyncio.run(plr.simulate({"labware": [{"name": "tips", "type": "opentrons_96_tiprack_300ul", "slot": 7},
                                              {"name": "res", "type": "nest_12_troughplate_15000uL_Vb", "slot": 2, "volumes_ul": {"A1": 31905}},
                                              {"name": "dst", "type": "cor_96_wellplate_360uL_Fb", "slot": 4}]},
                                  [{"source": "res:A1", "destination": "dst:A1", "volume_ul": 100}]))
        check(False, "plan_protocol should refuse a starting volume larger than the well")
    except plr.PlanError as exc:
        check("holds at most" in str(exc), "plan_protocol also refuses a starting volume larger than the well")

    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed")
        sys.exit(1)
    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
