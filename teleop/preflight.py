"""
Pre-session checks for a humanoid teleoperation run.

Every item here comes from the operating procedure used on ZenoVistaAI's rig.
They are encoded rather than left in a document because the consequence of
skipping one is a two-metre humanoid moving unexpectedly next to a person.

The first item is the one that matters most. The G1 must be suspended in a
support frame or gantry before its joints are energised: a robot that is
standing free when control begins can fall, and a falling humanoid is dangerous
to whoever is nearest. Nothing in this package will report a session ready while
that check is unconfirmed.

These are confirmations by a person, not sensor readings. This module does not
detect whether the robot is suspended; it refuses to proceed until an operator
states that it is, and records who stated it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Check:
    key: str
    prompt: str
    why: str
    blocking: bool = True
    confirmed: Optional[bool] = None


def default_checks() -> list:
    return [
        Check("suspended",
              "Is the G1 suspended in its support frame, so joints can move without the robot falling?",
              "A humanoid that falls under power can injure whoever is nearest. This is the "
              "check that must never be waived."),
        Check("workspace_clear",
              "Is the area within the robot's reach clear of people and obstacles?",
              "Arm motion follows the operator's hands, which are somewhere else. The operator "
              "cannot see what is beside the robot except through its cameras."),
        Check("estop_reachable",
              "Is a person other than the headset operator within reach of the emergency stop?",
              "The operator is wearing a headset and cannot see the room. Someone who can see "
              "it must be able to stop the robot."),
        Check("battery_ok",
              "Is the robot's battery sufficient for the planned session?",
              "Losing power mid-session drops the robot out of controlled motion."),
        Check("network_shared",
              "Are the headset and the control PC on the same network as the robot?",
              "Tracking data and video both cross this network. A partial connection produces "
              "a robot that moves but returns no video, leaving the operator blind."),
        Check("image_server_running",
              "Is the image server running on the robot?",
              "Without it the operator has no camera view and is working blind."),
        Check("damping_mode",
              "Has the robot been placed in damping mode before standing?",
              "Damping is the safe intermediate state between zero-moment and standing, and is "
              "the state the soft emergency stop returns it to."),
        Check("standing_stable",
              "Is the robot standing stably, balanced, with no obstacle nearby?",
              "Control begins from a stable posture. Starting from an unbalanced stance "
              "propagates into every subsequent motion."),
        Check("operator_briefed",
              "Does the operator know the task, the expected end state, and how to abort?",
              "An operator who has to work out what to do while wearing the headset is slower "
              "and less accurate."),
        Check("recording_on",
              "Is session recording enabled?",
              "A teleoperated action must leave the same reviewable record as an instrument "
              "action.", blocking=False),
    ]


class Preflight:
    """Holds the checklist for one intended session."""

    def __init__(self, checks: Optional[list] = None):
        self.checks = {c.key: c for c in (checks or default_checks())}
        self.confirmed_by: str = ""

    def confirm(self, key: str, value: bool, operator: str) -> dict:
        if key not in self.checks:
            return {"error": f"no such check {key!r}",
                    "known": sorted(self.checks)}
        self.checks[key].confirmed = bool(value)
        self.confirmed_by = operator
        return {"check": key, "confirmed": bool(value), "by": operator}

    def confirm_all(self, operator: str) -> dict:
        """Confirm every item at once.

        Provided for simulation and testing. On a real rig the operator walks
        the list, because the point of the list is that each item is looked at.
        """
        for check in self.checks.values():
            check.confirmed = True
        self.confirmed_by = operator
        return {"confirmed": len(self.checks), "by": operator}

    def outstanding(self) -> list:
        return [c for c in self.checks.values() if c.blocking and c.confirmed is not True]

    def failed(self) -> list:
        return [c for c in self.checks.values() if c.confirmed is False]

    def ready(self) -> bool:
        return not self.outstanding() and not self.failed()

    def report(self) -> dict:
        return {
            "ready": self.ready(),
            "confirmed_by": self.confirmed_by,
            "total": len(self.checks),
            "confirmed": sum(1 for c in self.checks.values() if c.confirmed is True),
            "outstanding": [{"key": c.key, "prompt": c.prompt, "why": c.why}
                            for c in self.outstanding()],
            "explicitly_failed": [{"key": c.key, "why": c.why} for c in self.failed()],
        }
