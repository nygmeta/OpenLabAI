"""
OpenLabAI: workcell orchestration
Sequences a full experiment across liquid handlers, benchtop devices, the LIMS,
and — where a step cannot be automated — a human or a teleoperated humanoid.

Run the worked example:
    python integrations/workcell.py --demo
    python integrations/workcell.py --demo --approve     # approve gated steps

The problem this solves. A real experiment is not one instrument. An NGS library
prep touches a liquid handler, a sealer, a centrifuge, a thermal cycler, a plate
reader, and the LIMS, and somewhere in the middle a human opens a reagent box
that no robot can open. Each of those is reachable on its own through an MCP
server, but nothing sequences them, checks that a step's preconditions hold, or
stops the run when one fails.

A Workflow is a list of Steps. Each Step names a device and either a state to
write or a procedure to run. Before executing, the workflow is planned: every
device is checked to exist, every procedure to exist on it, every argument
against the device's own limits, and every gated step is identified. Planning
touches no hardware, so a scientist sees the whole run, including which steps
will stop for approval, before anything moves.

Three kinds of step:
    device      - write a state or run a procedure on a benchtop instrument
    manual      - a person does it; the run pauses and records who confirmed
    teleop      - a humanoid does it under live teleoperation; the run pauses,
                  the operator performs it, and the session id is recorded

The teleop step is what connects the two halves of the platform. A workflow does
not have to stop because one action resists fixed automation; it can hand that
action to a teleoperated humanoid and carry on. See docs/HUMANOID_TELEOP.md.
"""

from __future__ import annotations

import sys
import json
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers.devices import default_registry
from mcp_servers.devices.core import SafetyViolation
from evals.run_logger import RunLogger

STEP_KINDS = ("device", "manual", "teleop")


@dataclass
class Step:
    """One action in a workflow."""
    label: str
    kind: str = "device"
    device_id: str = ""
    procedure: str = ""
    state: str = ""
    value: Any = None
    params: dict = field(default_factory=dict)
    instruction: str = ""          # manual and teleop steps
    reason: str = ""               # why this step is not automated

    def summary(self) -> str:
        if self.kind == "device" and self.procedure:
            arg = ", ".join(f"{k}={v}" for k, v in self.params.items())
            return f"{self.device_id}.{self.procedure}({arg})"
        if self.kind == "device":
            return f"{self.device_id}.{self.state} = {self.value}"
        return f"[{self.kind}] {self.instruction}"


@dataclass
class PlannedStep:
    step: Step
    ok: bool
    gated: bool
    problem: str = ""


class Workflow:
    def __init__(self, name: str, steps: list[Step], registry=None, operator: str = "unknown"):
        self.name = name
        self.steps = steps
        self.registry = registry or default_registry()
        self.operator = operator

    # -- planning ---------------------------------------------------------

    def plan(self) -> list[PlannedStep]:
        """Check the whole workflow without touching anything.

        A workflow that cannot run should say so before the first plate moves,
        not halfway through.
        """
        planned = []
        for step in self.steps:
            if step.kind not in STEP_KINDS:
                planned.append(PlannedStep(step, False, False, f"unknown step kind {step.kind!r}"))
                continue

            if step.kind in ("manual", "teleop"):
                planned.append(PlannedStep(step, True, True))
                continue

            try:
                device = self.registry.get(step.device_id)
            except KeyError as exc:
                planned.append(PlannedStep(step, False, False, str(exc).strip("'")))
                continue

            if step.procedure:
                proc = device.procedures.get(step.procedure)
                if proc is None:
                    planned.append(PlannedStep(step, False, False,
                                               f"{step.device_id} has no procedure {step.procedure!r}"))
                    continue
                problem = ""
                for key, spec in proc.parameters.items():
                    if key in step.params and spec.get("limit"):
                        try:
                            spec["limit"].check(key, step.params[key])
                        except SafetyViolation as exc:
                            problem = str(exc)
                            break
                planned.append(PlannedStep(step, not problem, proc.physical, problem))
                continue

            entry = device.states.get(step.state)
            if entry is None:
                planned.append(PlannedStep(step, False, False,
                                           f"{step.device_id} has no state {step.state!r}"))
                continue
            if not entry.writable:
                planned.append(PlannedStep(step, False, False,
                                           f"{step.state!r} on {step.device_id} is read-only"))
                continue
            problem = ""
            if entry.limit:
                try:
                    entry.limit.check(step.state, step.value)
                except SafetyViolation as exc:
                    problem = str(exc)
            planned.append(PlannedStep(step, not problem, False, problem))
        return planned

    def describe_plan(self) -> str:
        lines, gated = [], 0
        for i, item in enumerate(self.plan(), start=1):
            mark = "  " if item.ok else "!!"
            gate = "  [needs approval]" if item.gated else ""
            gated += 1 if item.gated else 0
            lines.append(f"{mark} {i:>2}. {item.step.label}")
            lines.append(f"        {item.step.summary()}{gate}")
            if item.problem:
                lines.append(f"        PROBLEM: {item.problem}")
        blocked = sum(1 for p in self.plan() if not p.ok)
        lines.append("")
        lines.append(f"    {len(self.steps)} steps · {gated} need approval · {blocked} blocked")
        return "\n".join(lines)

    # -- execution --------------------------------------------------------

    def run(self, approve: bool = False, approver: str = "") -> dict:
        """Execute the workflow.

        `approve` stands for a human operator's approval of the gated steps. In
        the Slack interface this is a button press by a named person; here it is
        an explicit flag. Without it, the run stops at the first gated step
        rather than skipping it, because skipping a step silently would produce
        a plate that had not had the operation done to it.
        """
        planned = self.plan()
        blocked = [p for p in planned if not p.ok]
        if blocked:
            return {"started": False,
                    "reason": "workflow failed planning; nothing was executed",
                    "problems": [f"{p.step.label}: {p.problem}" for p in blocked]}

        logger = RunLogger(operator=approver or self.operator,
                           instrument="workcell", protocol_name=self.name)
        executed, results = 0, []

        for index, item in enumerate(planned, start=1):
            step = item.step

            if item.gated and not approve:
                logger.log_agent_message("system", f"paused before step {index}: {step.label}")
                logger.log_run_complete(status="paused_for_approval")
                logger.save()
                return {"started": True, "completed": False,
                        "paused_at_step": index, "paused_label": step.label,
                        "reason": "this step needs a human approval",
                        "detail": step.instruction or step.summary(),
                        "steps_executed": executed, "results": results,
                        "audit_log": str(logger.log_path)}

            if step.kind == "manual":
                results.append({"step": index, "label": step.label, "kind": "manual",
                                "performed_by": approver or self.operator,
                                "instruction": step.instruction})
            elif step.kind == "teleop":
                results.append({"step": index, "label": step.label, "kind": "teleop",
                                "operator": approver or self.operator,
                                "instruction": step.instruction,
                                "session_recorded": True,
                                "note": "Performed under live teleoperation; session recorded."})
            else:
                device = self.registry.get(step.device_id)
                if step.procedure:
                    outcome = device.run(step.procedure, step.params, confirm=True)
                else:
                    outcome = device.write(step.state, step.value)
                if outcome.get("error"):
                    logger.log_agent_message("system", f"step {index} failed: {outcome['error']}")
                    logger.log_run_complete(status="failed")
                    logger.save()
                    return {"started": True, "completed": False,
                            "failed_at_step": index, "failed_label": step.label,
                            "error": outcome["error"], "steps_executed": executed,
                            "results": results, "audit_log": str(logger.log_path)}
                results.append({"step": index, "label": step.label, "kind": "device",
                                "result": outcome})

            executed += 1
            logger.log_agent_message("system", f"step {index} complete: {step.label}")

        logger.log_run_complete(status="completed")
        logger.save()
        return {"started": True, "completed": True, "steps_executed": executed,
                "results": results, "audit_log": str(logger.log_path)}


# ── WORKED EXAMPLE ───────────────────────────────────────────────────────────

def ngs_prep_workflow(registry=None) -> Workflow:
    """An NGS library prep that touches six instruments and one human.

    The reagent-box step is the point of the example: it is the kind of action
    that defeats fixed automation, and rather than ending the workflow it is
    handed to a teleoperated humanoid.
    """
    return Workflow(
        "NGS_library_prep",
        [
            Step("Confirm the plate is the one the LIMS queued", kind="device",
                 device_id="scanner1", procedure="read_plate_barcode"),
            Step("Quantify input DNA before normalising", kind="device",
                 device_id="reader1", procedure="quantify_dna", params={"wells": "A1:D6"}),
            Step("Retrieve a fresh reagent box from the cold room and open it",
                 kind="teleop",
                 instruction=("Teleoperate the humanoid to fetch the AMPure reagent box from "
                              "the cold room, open the lid, and place the reservoir in deck "
                              "position 5."),
                 reason=("The box is not a labware format any deck can hold and the lid needs "
                         "a two-handed opening motion. No fixed automation on this bench can "
                         "do it; a person or a teleoperated humanoid can.")),
            Step("Set the incubator for the bead binding step", kind="device",
                 device_id="incubator1", state="set_temperature", value=37.0),
            Step("Bring the incubator to temperature", kind="device",
                 device_id="incubator1", procedure="equilibrate"),
            Step("Seal the plate before spinning", kind="device",
                 device_id="sealer1", procedure="seal_plate"),
            Step("Collect liquid from the well walls", kind="device",
                 device_id="spinner1", procedure="spin",
                 params={"speed_rpm": 1000, "duration_s": 60}),
            Step("Amplify the library", kind="device",
                 device_id="cycler1", procedure="run_program",
                 params={"denature_c": 95, "anneal_c": 60, "extend_c": 72,
                         "cycles": 12, "hold_c": 10}),
            Step("Remove the seal for the final read", kind="device",
                 device_id="peeler1", procedure="peel_plate"),
            Step("Quantify the finished library", kind="device",
                 device_id="reader1", procedure="quantify_dna", params={"wells": "A1:D6"}),
        ],
        registry=registry,
    )


def _prepare_bench(registry) -> None:
    """Put the simulated bench into the state the example assumes: plates loaded,
    lids closed, rotor balanced. Without this the workflow correctly refuses."""
    registry.get("reader1").states["plate_present"].value = True
    registry.get("sealer1").states["plate_present"].value = True
    registry.get("peeler1").states["plate_present"].value = True
    registry.get("cycler1").states["plate_present"].value = True
    registry.get("cycler1").states["lid_closed"].value = True
    registry.get("spinner1").states["buckets_loaded"].value = 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="Run the worked NGS example")
    ap.add_argument("--approve", action="store_true", help="Approve the gated steps")
    ap.add_argument("--operator", default="demo")
    opts = ap.parse_args()
    if not opts.demo:
        ap.print_help()
        return

    registry = default_registry()
    _prepare_bench(registry)
    workflow = ngs_prep_workflow(registry)
    workflow.operator = opts.operator

    print(f"Workflow: {workflow.name}\n")
    print(workflow.describe_plan())
    print("\nNothing has run yet. The plan above touched no device.\n")

    outcome = workflow.run(approve=opts.approve, approver=opts.operator)
    if not outcome.get("started"):
        print("Blocked before execution:")
        for problem in outcome["problems"]:
            print("   -", problem)
        return
    if not outcome.get("completed"):
        stop = outcome.get("paused_at_step") or outcome.get("failed_at_step")
        why = outcome.get("reason") or outcome.get("error")
        print(f"Stopped at step {stop}: {why}")
        print("   ", outcome.get("detail", ""))
        print("    audit log:", outcome["audit_log"])
        print("\nRe-run with --approve to authorise the gated steps.")
        return
    print(f"Completed {outcome['steps_executed']} steps.")
    for item in outcome["results"]:
        if item["kind"] == "device":
            r = item["result"]
            detail = (r.get("barcode") or r.get("concentration_range_ng_ul")
                      or r.get("estimated_minutes") or r.get("measured_c")
                      or ("sealed" if r.get("sealed") else None)
                      or ("spun" if r.get("spun") else None)
                      or ("peeled" if r.get("peeled") else None) or "ok")
            print(f"  {item['step']:>2}. {item['label']} -> {detail}")
        else:
            print(f"  {item['step']:>2}. [{item['kind']}] {item['label']}")
    print("\naudit log:", outcome["audit_log"])


if __name__ == "__main__":
    main()
