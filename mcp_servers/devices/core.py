"""
Device abstraction for benchtop laboratory equipment.

Every device is described by a manifest of two things:

    states      - conditions the device can be in (plate at position 3,
                  block at 25 C, drawer open). Some are readable only;
                  some can be written.
    procedures  - operations the device can perform (seal a plate, spin,
                  read absorbance). Some move hardware or apply heat.

On top of that sit three primitives: read a state, write a state, run a
procedure. An agent that understands those three verbs can operate any device
in the registry without device-specific code, which is the point: adding a
plate sealer should not require teaching the agent about plate sealers.

Safety limits are attached to the device, not to the caller. A write outside a
limit is refused by the device before anything physical happens, so an agent
cannot talk its way past a temperature ceiling. Procedures that move hardware
or apply heat are additionally gated on explicit human confirmation, the same
gate used for robot motion elsewhere in OpenLabAI.

This structure deliberately mirrors the vocabulary Anthropic published for the
Model Hardware Standard (states, procedures, manifests, read/write primitives,
device-enforced safety limits). See docs/MHS_INTEROP.md for exactly what that
does and does not claim: the MHS specification is not public, so this is an
architecture aligned with its described model, not a conformant implementation.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional


class SafetyViolation(Exception):
    """Raised when a requested value or argument is outside a device limit.

    Raised by the device, before any simulated or physical action, so that the
    refusal cannot be bypassed by the caller.
    """


@dataclass
class SafetyLimit:
    """A bound the device enforces on itself."""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    allowed: Optional[list] = None

    def check(self, name: str, value: Any) -> None:
        if self.allowed is not None and value not in self.allowed:
            raise SafetyViolation(
                f"{name}={value!r} is not permitted; allowed values are {self.allowed}")
        if isinstance(value, (int, float)):
            if self.minimum is not None and value < self.minimum:
                raise SafetyViolation(f"{name}={value} is below the device minimum {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise SafetyViolation(f"{name}={value} exceeds the device maximum {self.maximum}")

    def describe(self) -> str:
        if self.allowed is not None:
            return "one of " + ", ".join(str(a) for a in self.allowed)
        if self.minimum is not None and self.maximum is not None:
            return f"{self.minimum} to {self.maximum}"
        if self.maximum is not None:
            return f"at most {self.maximum}"
        if self.minimum is not None:
            return f"at least {self.minimum}"
        return "unconstrained"


@dataclass
class State:
    """One condition the device can be in."""
    name: str
    value: Any
    unit: str = ""
    writable: bool = False
    description: str = ""
    limit: Optional[SafetyLimit] = None

    def to_manifest(self) -> dict:
        entry = {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "writable": self.writable,
            "description": self.description,
        }
        if self.limit:
            entry["safety_limit"] = self.limit.describe()
        return entry


@dataclass
class Procedure:
    """One operation the device can perform.

    `physical` marks a procedure that moves hardware, applies heat, or is
    otherwise not freely reversible. Those require explicit confirmation.
    """
    name: str
    description: str
    handler: Callable[..., dict]
    parameters: dict = field(default_factory=dict)
    physical: bool = False
    duration_s: float = 0.0

    def to_manifest(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                key: {"type": spec.get("type", "number"),
                      "unit": spec.get("unit", ""),
                      "description": spec.get("description", ""),
                      "safety_limit": spec["limit"].describe() if spec.get("limit") else None}
                for key, spec in self.parameters.items()
            },
            "moves_hardware": self.physical,
            "typical_duration_s": self.duration_s,
        }


class Device:
    """Base class for every benchtop instrument.

    Subclasses declare states and procedures in build(); everything else --
    manifest generation, limit enforcement, the confirmation gate -- is handled
    here so that a new driver is a description of the instrument rather than a
    reimplementation of the safety model.
    """

    device_class = "generic"
    vendor = ""
    model = ""
    description = ""

    def __init__(self, device_id: str, simulated: bool = True):
        self.device_id = device_id
        self.simulated = simulated
        self.states: dict[str, State] = {}
        self.procedures: dict[str, Procedure] = {}
        self.build()

    # -- declaration ------------------------------------------------------

    def build(self) -> None:
        raise NotImplementedError

    def add_state(self, *args, **kwargs) -> None:
        state = State(*args, **kwargs)
        self.states[state.name] = state

    def add_procedure(self, *args, **kwargs) -> None:
        procedure = Procedure(*args, **kwargs)
        self.procedures[procedure.name] = procedure

    # -- manifest ---------------------------------------------------------

    def manifest(self) -> dict:
        """The reference description an agent reads to learn what this device
        can measure, what can be adjusted, and what limits are enforced."""
        return {
            "device_id": self.device_id,
            "device_class": self.device_class,
            "vendor": self.vendor,
            "model": self.model,
            "description": self.description,
            "mode": "simulated" if self.simulated else "live",
            "states": [s.to_manifest() for s in self.states.values()],
            "procedures": [p.to_manifest() for p in self.procedures.values()],
        }

    # -- primitives -------------------------------------------------------

    def read(self, state: str) -> dict:
        if state not in self.states:
            raise KeyError(f"{self.device_id} has no state {state!r}")
        entry = self.states[state]
        return {"device_id": self.device_id, "state": state,
                "value": entry.value, "unit": entry.unit}

    def write(self, state: str, value: Any) -> dict:
        if state not in self.states:
            raise KeyError(f"{self.device_id} has no state {state!r}")
        entry = self.states[state]
        if not entry.writable:
            raise SafetyViolation(f"{state!r} on {self.device_id} is read-only")
        if entry.limit:
            entry.limit.check(state, value)          # refused before anything happens
        entry.value = value
        return {"device_id": self.device_id, "state": state,
                "value": entry.value, "unit": entry.unit, "written": True}

    def run(self, procedure: str, params: dict = None, confirm: bool = False) -> dict:
        if procedure not in self.procedures:
            raise KeyError(f"{self.device_id} has no procedure {procedure!r}")
        proc = self.procedures[procedure]
        params = params or {}

        # Validate every argument against its limit before the gate, so an
        # unsafe request is reported as unsafe rather than as unconfirmed.
        for key, spec in proc.parameters.items():
            if key in params and spec.get("limit"):
                spec["limit"].check(key, params[key])

        if proc.physical and confirm is not True:
            return {
                "device_id": self.device_id,
                "procedure": procedure,
                "refused": True,
                "reason": "confirm was not true",
                "detail": (f"{procedure} on {self.device_id} moves hardware or applies heat. "
                           "Pass confirm=true only with a human operator's approval."),
            }

        result = proc.handler(**params)
        result.update({"device_id": self.device_id, "procedure": procedure,
                       "mode": "simulated" if self.simulated else "live"})
        return result


class DeviceRegistry:
    """Discovery. Devices and agents find each other here rather than through
    per-device configuration in the agent."""

    def __init__(self):
        self._devices: dict[str, Device] = {}

    def add(self, device: Device) -> Device:
        self._devices[device.device_id] = device
        return device

    def get(self, device_id: str) -> Device:
        if device_id not in self._devices:
            raise KeyError(f"No device {device_id!r}. Known: {sorted(self._devices)}")
        return self._devices[device_id]

    def all(self) -> list[Device]:
        return list(self._devices.values())

    def manifests(self, device_class: str = "") -> list[dict]:
        return [d.manifest() for d in self._devices.values()
                if not device_class or d.device_class == device_class]

    def classes(self) -> list[str]:
        return sorted({d.device_class for d in self._devices.values()})


def simulated_noise(base: float, spread: float, rng: random.Random) -> float:
    """Plausible instrument noise, so simulated reads are not suspiciously exact."""
    return round(rng.gauss(base, spread), 4)
