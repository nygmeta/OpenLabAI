"""
Teleoperation session state machine.

The states below follow the real bring-up sequence on the rig: the robot powers
up in zero-moment, is placed in damping, is brought to standing, the operator's
services and headset are connected, and only then does control begin. Each
transition has a precondition, and the machine refuses a transition whose
precondition does not hold.

    OFF ──▶ ZERO_MOMENT ──▶ DAMPING ──▶ STANDING ──▶ READY ──▶ ACTIVE
                               ▲                                 │
                               └──────── soft stop ──────────────┘

The soft stop drops the robot from ACTIVE back to DAMPING from any state, and is
never gated: halting is always permitted, exactly as with any instrument
elsewhere in this project.

What this module does not do: it sends no joint commands and streams no tracking
data. The control loop is Unitree Robotics' xr_teleoperate, and the operator
console is a separate ZenoVistaAI application. This is the session and safety
layer that lets a laboratory workflow reason about whether a teleoperated step
can proceed, and produces the record that it did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import TeleopConfig
from .preflight import Preflight


class State(str, Enum):
    OFF = "off"
    ZERO_MOMENT = "zero_moment"
    DAMPING = "damping"
    STANDING = "standing"
    READY = "ready"
    ACTIVE = "active"
    STOPPED = "stopped"


# Allowed transitions, and the precondition each one carries.
TRANSITIONS = {
    (State.OFF, State.ZERO_MOMENT): "robot powered on and finished initialising",
    (State.ZERO_MOMENT, State.DAMPING): "damping engaged before any standing attempt",
    (State.DAMPING, State.STANDING): "preflight checks passed",
    (State.STANDING, State.READY): "control services running and headset connected",
    (State.READY, State.ACTIVE): "operator present and tracking confirmed",
    (State.ACTIVE, State.DAMPING): "soft stop",
    (State.STANDING, State.DAMPING): "soft stop",
    (State.READY, State.DAMPING): "soft stop",
    (State.DAMPING, State.STOPPED): "session ended",
    (State.STOPPED, State.OFF): "powered down",
}


@dataclass
class SessionRecord:
    session_id: str
    operator: str
    task: str
    started: float
    ended: Optional[float] = None
    transitions: list = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    outcome: str = ""


class TeleopSession:
    """One teleoperation session, from power-on to shutdown."""

    def __init__(self, config: Optional[TeleopConfig] = None,
                 preflight: Optional[Preflight] = None):
        self.config = config or TeleopConfig()
        self.preflight = preflight or Preflight()
        self.state = State.OFF
        self.operator = ""
        self.task = ""
        self.record: Optional[SessionRecord] = None
        self.tracking_ok = False
        self.services_ok = False
        self.latency_ms: Optional[float] = None

    # -- transitions ------------------------------------------------------

    def _move(self, target: State, note: str = "") -> dict:
        key = (self.state, target)
        if key not in TRANSITIONS:
            return {"refused": True,
                    "reason": f"cannot go from {self.state.value} to {target.value}",
                    "allowed_from_here": [t.value for (s, t) in TRANSITIONS if s == self.state]}
        previous, self.state = self.state, target
        if self.record:
            self.record.transitions.append(
                {"from": previous.value, "to": target.value, "at": time.time(), "note": note})
        return {"state": self.state.value, "from": previous.value,
                "precondition": TRANSITIONS[key], "note": note}

    def power_on(self) -> dict:
        """Robot powered and initialised. It starts in zero-moment."""
        return self._move(State.ZERO_MOMENT, "robot initialised")

    def engage_damping(self) -> dict:
        """Damping is the safe intermediate state, and where a soft stop returns to."""
        return self._move(State.DAMPING, "damping engaged")

    def stand(self) -> dict:
        """Refused until preflight passes. This is the gate that protects the rig."""
        if not self.preflight.ready():
            return {"refused": True,
                    "reason": "preflight checks are not complete",
                    "report": self.preflight.report()}
        return self._move(State.STANDING, "standing posture reached")

    def connect_services(self, latency_ms: Optional[float] = None) -> dict:
        """Image server, control services and headset connected."""
        self.services_ok = True
        self.latency_ms = latency_ms
        result = self._move(State.READY, "services and headset connected")
        if latency_ms is not None and latency_ms > self.config.latency_budget_ms:
            result["warning"] = (
                f"measured latency {latency_ms:.0f} ms exceeds the {self.config.latency_budget_ms} ms "
                "budget; fine manipulation will be difficult and the operator should slow down")
        return result

    def begin_control(self, operator: str, task: str, tracking_confirmed: bool) -> dict:
        """Hand control to the operator. The robot now follows their hands."""
        if not operator:
            return {"refused": True, "reason": "a named operator is required"}
        if not tracking_confirmed:
            return {"refused": True,
                    "reason": "hand tracking is not confirmed",
                    "detail": ("The robot must not be given a tracking stream the operator has "
                               "not verified. Confirm hands are tracked in the headset first.")}
        self.operator, self.task, self.tracking_ok = operator, task, True
        self.record = SessionRecord(session_id=f"TS-{int(time.time())}", operator=operator,
                                    task=task, started=time.time())
        return self._move(State.ACTIVE, f"control given to {operator}")

    def soft_stop(self, reason: str = "operator stop") -> dict:
        """Drop to damping. Never gated, from any state that allows it."""
        if self.state in (State.OFF, State.STOPPED, State.ZERO_MOMENT):
            return {"state": self.state.value, "note": "already not under control"}
        if self.record:
            self.record.aborted = True
            self.record.abort_reason = reason
        return self._move(State.DAMPING, f"soft stop: {reason}")

    def end_session(self, outcome: str = "") -> dict:
        if self.state != State.DAMPING:
            return {"refused": True,
                    "reason": f"end the session from damping, not {self.state.value}",
                    "detail": "Call soft_stop() first."}
        if self.record:
            self.record.ended = time.time()
            self.record.outcome = outcome
        return self._move(State.STOPPED, "session ended")

    # -- reporting --------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "under_control": self.state == State.ACTIVE,
            "operator": self.operator or None,
            "task": self.task or None,
            "preflight_ready": self.preflight.ready(),
            "services_connected": self.services_ok,
            "tracking_confirmed": self.tracking_ok,
            "latency_ms": self.latency_ms,
            "latency_within_budget": (None if self.latency_ms is None
                                      else self.latency_ms <= self.config.latency_budget_ms),
            "configuration": self.config.describe(),
        }

    def summary(self) -> dict:
        if not self.record:
            return {"session": None}
        r = self.record
        return {
            "session_id": r.session_id, "operator": r.operator, "task": r.task,
            "duration_s": round((r.ended or time.time()) - r.started, 1),
            "transitions": len(r.transitions), "aborted": r.aborted,
            "abort_reason": r.abort_reason, "outcome": r.outcome,
        }
