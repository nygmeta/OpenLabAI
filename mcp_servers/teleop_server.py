"""
OpenLabAI: Humanoid teleoperation MCP server
Lets an agent observe a teleoperation session and request one. Never drive one.

Usage:
    python mcp_servers/teleop_server.py --arm G1_29 --ee inspire1 --headset quest3

Tools exposed to Claude:
    get_teleop_status()    - Session state, preflight readiness, latency, config
    get_preflight()        - The checklist, and what is still outstanding
    confirm_preflight()    - Record a named operator's confirmation of one check
    request_teleop_task()  - Ask for a task to be performed by a human operator
    get_launch_details()   - The xr_teleoperate command and Televiewer URL for the
                             operator to run themselves
    soft_stop()            - Drop the robot to damping. Never gated.

Why the tool list stops there. Everywhere else in OpenLabAI an agent proposes an
action and a human approves it. Teleoperation is different in kind: it is a
continuous motion stream reproducing an operator's hands thirty or more times a
second, and there is no discrete action for a human to approve. So the agent is
not given the robot at all. It can see the session, ask for work to be done, and
stop the robot. It cannot start control, and it cannot send motion.

The one tool that changes the robot's physical state is soft_stop, and it is
ungated deliberately: stopping is always the safe direction.

Credentials and robot addresses are never arguments to these tools. The robot
host comes from OPENLAB_G1_HOST in the server environment.
"""

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teleop import TeleopConfig, TeleopSession, ConfigError, ARMS, END_EFFECTORS, HEADSETS
from integrations.teleop_bridge import TeleopBridge, SUITABLE

parser = argparse.ArgumentParser()
parser.add_argument("--arm", default="G1_29", choices=sorted(ARMS))
parser.add_argument("--ee", default="inspire1", choices=sorted(k for k in END_EFFECTORS if k))
parser.add_argument("--headset", default="quest3", choices=sorted(HEADSETS))
parser.add_argument("--xr-mode", default="hand", choices=["hand", "controller"])
parser.add_argument("--operator", default=os.environ.get("OPENLAB_OPERATOR", "unknown"))
args, _ = parser.parse_known_args()

try:
    CONFIG = TeleopConfig(arm=args.arm, end_effector=args.ee,
                          xr_mode=args.xr_mode, headset=args.headset)
except ConfigError as exc:
    sys.exit(f"Configuration rejected: {exc}")

SESSION = TeleopSession(config=CONFIG)
BRIDGE = TeleopBridge()
app = Server("openlabai-teleop")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_teleop_status",
            description=("Current teleoperation session state: whether the robot is under an "
                         "operator's control, whether preflight passed, measured latency, and "
                         "the rig configuration."),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_preflight",
            description=("The pre-session safety checklist and what remains outstanding. The "
                         "robot cannot be brought to standing until the blocking items are "
                         "confirmed by a named person."),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="confirm_preflight",
            description=("Record a named operator's confirmation of one checklist item. These "
                         "are statements by a person, not sensor readings: the software cannot "
                         "detect whether the robot is suspended, only refuse to proceed until "
                         "someone states that it is."),
            inputSchema={
                "type": "object",
                "properties": {
                    "check": {"type": "string", "description": "Checklist key, e.g. suspended"},
                    "confirmed": {"type": "boolean"},
                    "operator": {"type": "string", "description": "Person making the statement"},
                },
                "required": ["check", "confirmed", "operator"],
            },
        ),
        types.Tool(
            name="request_teleop_task",
            description=("Request that a human operator perform a task using the humanoid. State "
                         "why fixed automation cannot do it. Requests describing work that "
                         "belongs on a liquid handler or benchtop instrument are refused."),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string",
                                  "description": f"One of: {', '.join(sorted(SUITABLE))}"},
                    "instruction": {"type": "string"},
                    "reason": {"type": "string",
                               "description": "Why fixed automation cannot perform this"},
                    "expected_outcome": {"type": "string"},
                },
                "required": ["task_type", "instruction", "reason", "expected_outcome"],
            },
        ),
        types.Tool(
            name="get_launch_details",
            description=("The xr_teleoperate command and Televiewer URL for this configuration, "
                         "for an operator to run at the rig. This server does not execute them: "
                         "starting a session is a physical act performed with the robot in sight."),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="soft_stop",
            description=("Drop the robot to damping mode. Never gated, available at any time, to "
                         "anyone. Stopping is always the safe direction."),
            inputSchema={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": [],
            },
        ),
    ]


def _text(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "get_teleop_status":
            status = SESSION.status()
            status["session"] = SESSION.summary()
            status["note"] = ("This server cannot start control or send motion. It observes, "
                              "requests work, and can stop the robot.")
            return _text(status)

        if name == "get_preflight":
            return _text(SESSION.preflight.report())

        if name == "confirm_preflight":
            result = SESSION.preflight.confirm(arguments["check"],
                                               arguments["confirmed"],
                                               arguments["operator"])
            result["preflight_ready"] = SESSION.preflight.ready()
            return _text(result)

        if name == "request_teleop_task":
            return _text(BRIDGE.request_session(
                arguments["task_type"], arguments["instruction"],
                arguments["reason"], arguments["expected_outcome"],
                requested_by="agent"))

        if name == "get_launch_details":
            return _text({
                "launch_command": CONFIG.launch_command(),
                "televiewer_url": CONFIG.televiewer_url(),
                "configuration": CONFIG.describe(),
                "note": ("Run at the rig, with the robot suspended and in sight. The robot host "
                         "is read from OPENLAB_G1_HOST; no credentials are held by this server."),
            })

        if name == "soft_stop":
            return _text(SESSION.soft_stop(arguments.get("reason", "requested via MCP")))

        return _text({"error": f"Unknown tool: {name}"})

    except KeyError as exc:
        return _text({"error": f"missing or unknown argument: {exc}"})
    except Exception as exc:
        return _text({"error": f"{type(exc).__name__}: {exc}"})


async def main():
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
