"""
OpenLabAI: teleoperation bridge
Exposes the humanoid as a workflow step, without ever letting an agent drive it.

Usage:
    python integrations/teleop_bridge.py --demo

The design constraint. Everywhere else in OpenLabAI an agent proposes an action
and a human approves it. A humanoid under extended-reality teleoperation is
different in kind: the robot reproduces an operator's hand and arm motion in
real time, and there is no meaningful sense in which an agent could "approve" a
continuous motion stream. So the agent is not given control of the robot at all.

What the agent can do is request a teleoperation session: state what needs
doing, why fixed automation cannot do it, and what the operator should see when
it is finished. A human accepts the request, performs the task wearing the
headset, and closes the session. The bridge records the request, the operator,
the timing, and the outcome, so a teleoperated step leaves the same kind of
audit record as a robot step.

This is the seam between the two halves of the platform. A workflow does not
have to end because one action resists fixed automation; it can hand that action
to a person driving the humanoid and continue afterwards.

Relationship to the control loop. The underlying extended-reality control loop —
headset tracking, retargeting to the robot's hands, inverse kinematics,
filtering, and the WebRTC video return path — is provided by Unitree Robotics'
open-source xr_teleoperate framework, with ZenoVistaAI's G1 Teleop Console layered
on top for session control, hand calibration, LiDAR visualisation and diagnostics.
This bridge does not reimplement any of that and does not send joint commands. It
brokers requests and records outcomes. See docs/HUMANOID_TELEOP.md.
"""

from __future__ import annotations

import sys
import json
import time
import argparse
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.run_logger import RunLogger

# Tasks a humanoid is appropriate for. The bridge refuses requests outside this
# list rather than accepting anything phrased confidently: teleoperation is for
# work that defeats fixed automation, not a way around a missing connector.
SUITABLE = {
    "retrieve": "Fetch an item from storage, a cold room, or a shared bench",
    "open_container": "Open a container, box or bag that no deck position accepts",
    "load_instrument": "Load or unload an instrument not designed for robotic access",
    "transport": "Carry labware between stations that are not physically integrated",
    "inspect": "Look at something and report what is there",
    "recover": "Recover from a fault, such as a mispositioned plate",
}

UNSUITABLE_HINTS = {
    "pipette": "Pipetting belongs on a liquid handler; use the OT-2 or Hamilton connector.",
    "aspirate": "Aspiration belongs on a liquid handler, not the humanoid.",
    "dispense": "Dispensing belongs on a liquid handler, not the humanoid.",
    "centrifuge": "Use the centrifuge device driver; it is on the bench.",
    "seal": "Use the sealer device driver; it is on the bench.",
    "incubate": "Use the incubator device driver; it is on the bench.",
}


@dataclass
class TeleopRequest:
    request_id: str
    task_type: str
    instruction: str
    reason: str
    expected_outcome: str
    requested_by: str
    created: float = field(default_factory=time.time)
    status: str = "pending"
    operator: str = ""
    started: Optional[float] = None
    ended: Optional[float] = None
    outcome: str = ""
    session_recorded: bool = False


class TeleopBridge:
    """Brokers teleoperation requests. Sends no motion commands, ever."""

    def __init__(self, console_url: str = "http://localhost:8080"):
        self.console_url = console_url
        self.requests: dict[str, TeleopRequest] = {}

    # -- request ----------------------------------------------------------

    def request_session(self, task_type: str, instruction: str, reason: str,
                        expected_outcome: str, requested_by: str) -> dict:
        if task_type not in SUITABLE:
            return {"refused": True,
                    "reason": f"{task_type!r} is not a task type the humanoid is used for",
                    "suitable_task_types": SUITABLE}

        lowered = instruction.lower()
        for word, hint in UNSUITABLE_HINTS.items():
            if word in lowered:
                return {"refused": True,
                        "reason": f"this request describes {word}, which is not humanoid work",
                        "detail": hint}

        if not reason.strip():
            return {"refused": True,
                    "reason": "a reason is required",
                    "detail": ("State why fixed automation cannot do this. Teleoperation is "
                               "for work that defeats automation, not a substitute for a "
                               "connector that has not been written.")}

        request_id = hashlib.sha256(
            f"{requested_by}{instruction}{time.time()}".encode()).hexdigest()[:12]
        self.requests[request_id] = TeleopRequest(
            request_id=request_id, task_type=task_type, instruction=instruction,
            reason=reason, expected_outcome=expected_outcome, requested_by=requested_by)
        return {"request_id": request_id, "status": "pending",
                "console_url": self.console_url,
                "next_step": ("A human operator accepts this request, performs it wearing the "
                              "headset, and closes it. No agent drives the robot.")}

    # -- operator actions -------------------------------------------------

    def accept(self, request_id: str, operator: str) -> dict:
        req = self.requests.get(request_id)
        if not req:
            return {"error": "no such request"}
        if req.status != "pending":
            return {"error": f"request is {req.status}, not pending"}
        req.status, req.operator, req.started = "in_session", operator, time.time()
        return {"request_id": request_id, "status": req.status, "operator": operator,
                "instruction": req.instruction,
                "note": "Session recording starts with the console, not with this bridge."}

    def complete(self, request_id: str, outcome: str, session_recorded: bool = True) -> dict:
        req = self.requests.get(request_id)
        if not req:
            return {"error": "no such request"}
        if req.status != "in_session":
            return {"error": f"request is {req.status}, not in_session"}
        req.status, req.ended = "complete", time.time()
        req.outcome, req.session_recorded = outcome, session_recorded

        logger = RunLogger(operator=req.operator, instrument="humanoid:unitree_g1",
                           protocol_name=f"teleop:{req.task_type}")
        logger.log_agent_message("system",
                                 f"requested by {req.requested_by}: {req.instruction}")
        logger.log_agent_message("system", f"outcome: {outcome}")
        logger.log_run_complete(status="completed")
        path = logger.save()
        return {"request_id": request_id, "status": "complete", "operator": req.operator,
                "duration_s": round(req.ended - req.started, 1),
                "outcome": outcome, "session_recorded": session_recorded,
                "audit_log": str(path)}

    def abort(self, request_id: str, operator: str, why: str) -> dict:
        req = self.requests.get(request_id)
        if not req:
            return {"error": "no such request"}
        req.status, req.outcome, req.ended = "aborted", why, time.time()
        return {"request_id": request_id, "status": "aborted", "operator": operator,
                "reason": why,
                "note": "Aborting is never gated, as with stopping any instrument."}

    def pending(self) -> list:
        return [asdict(r) for r in self.requests.values() if r.status == "pending"]


def _demo() -> None:
    bridge = TeleopBridge()
    print("Teleoperation bridge — no robot is contacted.\n")

    print("1. An agent asks the humanoid to pipette:")
    print("  ", bridge.request_session(
        "retrieve", "Pipette 50 uL from A1 to B1", "faster than the liquid handler",
        "liquid moved", "agent")["detail"])

    print("\n2. An agent asks without giving a reason:")
    print("  ", bridge.request_session(
        "retrieve", "Get the reagent box", "", "box on bench", "agent")["detail"])

    print("\n3. A legitimate request:")
    made = bridge.request_session(
        "open_container",
        "Fetch the AMPure reagent box from the cold room, open it, and place the "
        "reservoir in deck position 5.",
        "The box is not a labware format any deck accepts and the lid needs a "
        "two-handed opening motion.",
        "Reservoir seated in position 5, lid set aside, box returned to the cold room.",
        "agent")
    print("   request_id:", made["request_id"], "| status:", made["status"])
    print("  ", made["next_step"])

    print("\n4. An operator accepts and performs it:")
    accepted = bridge.accept(made["request_id"], "a.nygmet")
    print("   operator:", accepted["operator"], "| status:", accepted["status"])
    done = bridge.complete(made["request_id"],
                           "Reservoir seated in position 5; lid on the adjacent bench.")
    print("   status:", done["status"], "| session recorded:", done["session_recorded"])
    print("   audit log:", done["audit_log"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    if ap.parse_args().demo:
        _demo()
    else:
        ap.print_help()
