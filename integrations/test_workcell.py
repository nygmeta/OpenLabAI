"""
Tests for workflow planning, execution and the teleoperation bridge.

Run:  python integrations/test_workcell.py
Exits non-zero on failure.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers.devices import default_registry
from integrations.workcell import Step, Workflow, ngs_prep_workflow, _prepare_bench
from integrations.teleop_bridge import TeleopBridge


def main() -> int:
    failures, checks = [], 0

    # Planning finds problems without touching anything.
    reg = default_registry()
    bad = Workflow("bad", [
        Step("nonexistent device", device_id="nope1", procedure="spin"),
        Step("nonexistent procedure", device_id="sealer1", procedure="teleport"),
        Step("value above a limit", device_id="sealer1", state="set_temperature", value=400),
        Step("read-only state", device_id="sealer1", state="temperature", value=50),
    ], registry=reg)
    plan = bad.plan()
    checks += 1
    if sum(1 for p in plan if not p.ok) != 4:
        failures.append(f"expected 4 planning problems, got {[p.problem for p in plan]}")
    checks += 1
    if reg.get("sealer1").states["set_temperature"].value != 165.0:
        failures.append("planning mutated device state")

    # A workflow that fails planning executes nothing.
    checks += 1
    outcome = bad.run(approve=True)
    if outcome.get("started"):
        failures.append("a workflow that failed planning was started")

    # The worked example plans clean and identifies its gated steps.
    reg2 = default_registry(); _prepare_bench(reg2)
    flow = ngs_prep_workflow(reg2)
    plan = flow.plan()
    checks += 1
    if any(not p.ok for p in plan):
        failures.append(f"example workflow failed planning: {[p.problem for p in plan if p.problem]}")
    checks += 1
    if sum(1 for p in plan if p.gated) < 5:
        failures.append("expected several gated steps in the example workflow")

    # Without approval it pauses at the first gated step and runs no further.
    checks += 1
    outcome = flow.run(approve=False)
    if outcome.get("completed"):
        failures.append("workflow completed without approval")
    checks += 1
    if outcome.get("paused_at_step") != 3:
        failures.append(f"expected pause at step 3, got {outcome.get('paused_at_step')}")
    checks += 1
    if not outcome.get("audit_log"):
        failures.append("a paused run wrote no audit log")

    # With approval it runs to completion.
    reg3 = default_registry(); _prepare_bench(reg3)
    flow3 = ngs_prep_workflow(reg3)
    checks += 1
    outcome = flow3.run(approve=True, approver="tester")
    if not outcome.get("completed"):
        failures.append(f"approved workflow did not complete: {outcome}")
    checks += 1
    if outcome.get("steps_executed") != 10:
        failures.append(f"expected 10 steps, executed {outcome.get('steps_executed')}")

    # A failing interlock stops the run rather than continuing.
    reg4 = default_registry(); _prepare_bench(reg4)
    reg4.get("spinner1").states["lid_closed"].value = False
    flow4 = ngs_prep_workflow(reg4)
    checks += 1
    outcome = flow4.run(approve=True, approver="tester")
    if outcome.get("completed"):
        failures.append("workflow completed despite an open centrifuge lid")
    checks += 1
    if outcome.get("failed_at_step") != 7:
        failures.append(f"expected failure at the spin step, got {outcome.get('failed_at_step')}")

    # Teleop bridge refuses work that belongs on a liquid handler.
    bridge = TeleopBridge()
    checks += 1
    if not bridge.request_session("retrieve", "Pipette 50 uL into B1", "quicker",
                                  "moved", "agent").get("refused"):
        failures.append("teleop bridge accepted a pipetting request")
    checks += 1
    if not bridge.request_session("retrieve", "Fetch a box", "", "box", "agent").get("refused"):
        failures.append("teleop bridge accepted a request with no reason")
    checks += 1
    if not bridge.request_session("dance", "Do a jig", "fun", "joy", "agent").get("refused"):
        failures.append("teleop bridge accepted an unknown task type")

    made = bridge.request_session(
        "open_container", "Open the reagent box and seat the reservoir",
        "No deck position accepts the box and the lid needs two hands",
        "Reservoir seated", "agent")
    checks += 1
    if "request_id" not in made:
        failures.append(f"legitimate teleop request refused: {made}")
    else:
        checks += 1
        if bridge.complete(made["request_id"], "done").get("error") is None:
            failures.append("a request was completed without being accepted")
        bridge.accept(made["request_id"], "operator1")
        done = bridge.complete(made["request_id"], "Reservoir seated")
        checks += 1
        if done.get("status") != "complete" or not done.get("audit_log"):
            failures.append(f"teleop completion did not record an audit log: {done}")

    if failures:
        print(f"FAILED ({len(failures)} of {checks} checks)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"OK — {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
