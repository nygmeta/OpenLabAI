"""
Teleoperation session configuration.

The values here mirror the launch configuration used on ZenoVistaAI's rig: a
Unitree G1 in its 29-degree-of-freedom configuration with Inspire dexterous
hands, driven from a Meta Quest headset in bare-hand tracking mode through
Unitree Robotics' open-source xr_teleoperate framework.

Configuration is validated rather than trusted. An arm or end-effector string
that xr_teleoperate does not recognise fails here, at import time, rather than
after the robot is already standing and an operator is wearing a headset.

No credentials appear in this file or anywhere in this package. The robot's
address and login are site configuration, read from the environment
(OPENLAB_G1_HOST), never committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Values accepted by xr_teleoperate. Kept explicit so an unsupported
# combination is caught before a session is offered to an operator.
ARMS = {
    "G1_29": "Unitree G1, 29 degrees of freedom",
    "G1_23": "Unitree G1, 23 degrees of freedom",
    "H1_2": "Unitree H1-2",
    "H1": "Unitree H1",
}

END_EFFECTORS = {
    "inspire1": "Inspire dexterous hands",
    "dex3": "Unitree Dex3 hands",
    "brainco": "BrainCo hands",
    "dex1": "Unitree Dex1 gripper",
    "": "no end effector",
}

XR_MODES = {
    "hand": "bare hand tracking",
    "controller": "controller tracking",
}

HEADSETS = {
    "quest3": "Meta Quest 3",
    "questpro": "Meta Quest Pro",
    "visionpro": "Apple Vision Pro",
    "pico4": "PICO 4",
}


class ConfigError(ValueError):
    """Raised for a configuration xr_teleoperate would not accept."""


@dataclass
class TeleopConfig:
    arm: str = "G1_29"
    end_effector: str = "inspire1"
    xr_mode: str = "hand"
    headset: str = "quest3"
    motion: bool = True                 # whether locomotion is enabled
    robot_host: str = field(default_factory=lambda: os.environ.get("OPENLAB_G1_HOST", ""))
    televiewer_port: int = 8012
    # Below this, fine manipulation is practical; above it an operator must slow down.
    latency_budget_ms: int = 100

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ConfigError(f"arm {self.arm!r} is not supported; choose from {sorted(ARMS)}")
        if self.end_effector not in END_EFFECTORS:
            raise ConfigError(f"end effector {self.end_effector!r} is not supported; "
                              f"choose from {sorted(k for k in END_EFFECTORS if k)}")
        if self.xr_mode not in XR_MODES:
            raise ConfigError(f"xr mode {self.xr_mode!r} is not supported; choose from {sorted(XR_MODES)}")
        if self.headset not in HEADSETS:
            raise ConfigError(f"headset {self.headset!r} is not supported; choose from {sorted(HEADSETS)}")
        if not 1 <= self.televiewer_port <= 65535:
            raise ConfigError(f"televiewer port {self.televiewer_port} is out of range")

    def describe(self) -> dict:
        return {
            "arm": self.arm, "arm_description": ARMS[self.arm],
            "end_effector": self.end_effector,
            "end_effector_description": END_EFFECTORS[self.end_effector],
            "xr_mode": self.xr_mode, "xr_mode_description": XR_MODES[self.xr_mode],
            "headset": self.headset, "headset_description": HEADSETS[self.headset],
            "locomotion_enabled": self.motion,
            "robot_host_configured": bool(self.robot_host),
            "latency_budget_ms": self.latency_budget_ms,
        }

    def launch_command(self) -> str:
        """The xr_teleoperate invocation this configuration corresponds to.

        Returned for the operator to run themselves. Nothing in this package
        executes it: starting a teleoperation session is a physical act that a
        person performs at the rig, with the robot suspended and in sight.
        """
        parts = ["python teleop_and_arm.py",
                 f"--xr-mode={self.xr_mode}",
                 f"--arm={self.arm}"]
        if self.end_effector:
            parts.append(f"--ee={self.end_effector}")
        if self.motion:
            parts.append("--motion")
        return " ".join(parts)

    def televiewer_url(self) -> str:
        host = self.robot_host or "<control-pc-ip>"
        return f"https://{host}:{self.televiewer_port}?ws=wss://{host}:{self.televiewer_port}"

