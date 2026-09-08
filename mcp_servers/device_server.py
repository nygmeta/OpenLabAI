"""
OpenLabAI: Benchtop device MCP server
One server for every small instrument on the bench.

Usage:
    python mcp_servers/device_server.py

Tools exposed to Claude:
    list_devices()      - Discover devices and their classes
    describe_device()   - Full manifest: states, procedures, safety limits
    read_state()        - Read one state from one device
    write_state()       - Write one state, refused if outside a device limit
    run_procedure()     - Run a procedure; gated when it moves hardware or heats

Rather than one server per instrument, devices are described by manifests and
reached through three primitives. An agent that has learned read, write and run
can operate a plate sealer it has never seen, because the sealer's manifest tells
it what the sealer can do and what limits apply. Adding an instrument is a driver
file, not a new server and not new agent instructions.

Devices currently registered: microplate readers (BMG CLARIOstar, Thermo
NanoDrop class), heat sealer, seal peeler, plate centrifuge, shaking incubator,
thermal cycler, barcode reader, and a plate hotel.

Everything here is simulated. No driver in this server contacts physical
hardware; simulated behaviour is representative of the instrument class, not a
reproduction of any vendor's firmware. Simulated reads are labelled as such in
every response.
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

from mcp_servers.devices import default_registry
from mcp_servers.devices.core import SafetyViolation

parser = argparse.ArgumentParser()
parser.add_argument("--operator", default=os.environ.get("OPENLAB_OPERATOR", "unknown"))
args, _ = parser.parse_known_args()

try:
    from evals.run_logger import RunLogger
    HAS_EVALS = True
except Exception:
    HAS_EVALS = False

REGISTRY = default_registry()
app = Server("openlabai-devices")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    classes = ", ".join(REGISTRY.classes())
    return [
        types.Tool(
            name="list_devices",
            description=(
                f"Discover the instruments available on this bench. Classes present: {classes}. "
                "Returns device ids, classes and one-line descriptions. Call this first."
            ),
            inputSchema={
                "type": "object",
                "properties": {"device_class": {"type": "string",
                                                "description": "Optional filter, e.g. plate_reader"}},
                "required": [],
            },
        ),
        types.Tool(
            name="describe_device",
            description=(
                "Full manifest for one device: every state it reports, which states can be "
                "written, every procedure it can run, which procedures move hardware, and the "
                "safety limits the device enforces. Read this before writing or running anything."
            ),
            inputSchema={
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        ),
        types.Tool(
            name="read_state",
            description="Read one state from one device. Reading never moves hardware.",
            inputSchema={
                "type": "object",
                "properties": {"device_id": {"type": "string"}, "state": {"type": "string"}},
                "required": ["device_id", "state"],
            },
        ),
        types.Tool(
            name="write_state",
            description=(
                "Set a writable state, such as a target temperature or shaking speed. The device "
                "refuses any value outside its own safety limits, before anything changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "state": {"type": "string"},
                    "value": {"description": "Number, string or boolean, per the manifest"},
                },
                "required": ["device_id", "state", "value"],
            },
        ),
        types.Tool(
            name="run_procedure",
            description=(
                "Run a procedure on a device. Procedures whose manifest says moves_hardware is "
                "true apply heat or move mechanism and REFUSE unless confirm=true, which "
                "represents a human operator's approval of this specific action. Procedures that "
                "only measure are ungated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "procedure": {"type": "string"},
                    "params": {"type": "object", "description": "Arguments named in the manifest"},
                    "confirm": {"type": "boolean",
                                "description": "Required for procedures that move hardware."},
                },
                "required": ["device_id", "procedure"],
            },
        ),
    ]


def _text(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "list_devices":
            wanted = arguments.get("device_class", "")
            devices = [{"device_id": d.device_id, "device_class": d.device_class,
                        "vendor": d.vendor, "model": d.model,
                        "description": d.description, "mode": "simulated"}
                       for d in REGISTRY.all()
                       if not wanted or d.device_class == wanted]
            return _text({"count": len(devices), "classes": REGISTRY.classes(),
                          "devices": devices,
                          "note": "All devices are simulated; none contacts physical hardware."})

        if name == "describe_device":
            return _text(REGISTRY.get(arguments["device_id"]).manifest())

        if name == "read_state":
            return _text(REGISTRY.get(arguments["device_id"]).read(arguments["state"]))

        if name == "write_state":
            device = REGISTRY.get(arguments["device_id"])
            result = device.write(arguments["state"], arguments["value"])
            _audit(device.device_id, f"write {arguments['state']}={arguments['value']}")
            return _text(result)

        if name == "run_procedure":
            device = REGISTRY.get(arguments["device_id"])
            result = device.run(arguments["procedure"], arguments.get("params") or {},
                                confirm=arguments.get("confirm", False))
            if not result.get("refused") and not result.get("error"):
                _audit(device.device_id, f"run {arguments['procedure']}")
            return _text(result)

        return _text({"error": f"Unknown tool: {name}"})

    except SafetyViolation as exc:
        return _text({"refused": True, "reason": "safety limit",
                      "detail": str(exc),
                      "hint": "Call describe_device to see the limits this device enforces."})
    except KeyError as exc:
        return _text({"error": str(exc).strip("'")})
    except Exception as exc:
        return _text({"error": f"{type(exc).__name__}: {exc}"})


def _audit(device_id: str, action: str) -> None:
    """Record device actions alongside robot runs. Never let logging failure
    break a device call."""
    if not HAS_EVALS:
        return
    try:
        logger = RunLogger(operator=args.operator, instrument=f"device:{device_id}",
                           protocol_name=action)
        logger.log_agent_message("system", action)
        logger.log_run_complete(status="completed")
        logger.save()
    except Exception:
        pass


async def main():
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
