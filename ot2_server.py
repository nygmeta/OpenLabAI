"""
Lab-Assistant: Opentrons OT-2 MCP Server
Provides live bidirectional control of OT-2 via HTTP API.

Usage:
    python ot2_server.py --host 169.254.x.x

Tools exposed to Claude:
    read_deck()        - Read current deck layout
    run_protocol()     - Upload and execute a protocol
    get_run_status()   - Poll current run status
    home_robot()       - Home all axes
    create_protocol()  - Generate a PyLabRobot protocol file
"""

import json
import asyncio
import argparse
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="169.254.10.10", help="OT-2 IP address")
args, _ = parser.parse_known_args()

OT2_HOST = args.host
OT2_BASE = f"http://{OT2_HOST}:31950"
HEADERS = {"opentrons-version": "3"}

app = Server("lab-assistant-ot2")


async def ot2_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{OT2_BASE}{path}", headers=HEADERS)
        r.raise_for_status()
        return r.json()


async def ot2_post(path: str, data: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OT2_BASE}{path}",
            headers={**HEADERS, "Content-Type": "application/json"},
            json=data or {}
        )
        r.raise_for_status()
        return r.json()


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_deck",
            description=(
                "Read the current Opentrons OT-2 deck layout. "
                "Returns all loaded labware with positions, types, and well counts. "
                "Use this first before creating any protocol."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_run_status",
            description="Get the status of the current or most recent protocol run on the OT-2.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="home_robot",
            description="Home all robot axes. Use before starting a new run or if the robot seems stuck.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="create_protocol",
            description=(
                "Generate a PyLabRobot-compatible Python protocol file from a structured description. "
                "The file is saved and ready to upload to the OT-2."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Protocol name (no spaces)"},
                    "description": {"type": "string", "description": "What this protocol does"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["aspirate", "dispense", "transfer", "mix", "pick_up_tips", "drop_tips"]},
                                "source": {"type": "string"},
                                "destination": {"type": "string"},
                                "volume_ul": {"type": "number"},
                                "mix_cycles": {"type": "integer"},
                            }
                        }
                    },
                    "tip_strategy": {
                        "type": "string",
                        "enum": ["new_tips_each_transfer", "reuse_tips"],
                        "default": "new_tips_each_transfer"
                    }
                },
                "required": ["name", "steps"]
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "read_deck":
            return await handle_read_deck()
        elif name == "get_run_status":
            return await handle_run_status()
        elif name == "home_robot":
            return await handle_home()
        elif name == "create_protocol":
            return await handle_create_protocol(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=(
            f"Cannot connect to OT-2 at {OT2_HOST}. "
            "Running in mock mode. Check that:\n"
            "1. The OT-2 is powered on\n"
            "2. You're on the same network\n"
            "3. The IP address is correct\n\n"
            + json.dumps(mock_deck(), indent=2)
        ))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def handle_read_deck() -> list[types.TextContent]:
    try:
        data = await ot2_get("/labware")
        result = {
            "connected": True,
            "host": OT2_HOST,
            "labware": [
                {
                    "slot": lw.get("location", {}).get("slotName", "?"),
                    "name": lw.get("loadName", "unknown"),
                    "display_name": lw.get("displayName", ""),
                    "is_tiprack": lw.get("isTiprack", False),
                    "wells": lw.get("wells", {})
                }
                for lw in data.get("data", [])
            ]
        }
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception:
        return [types.TextContent(type="text", text=json.dumps(mock_deck(), indent=2))]


async def handle_run_status() -> list[types.TextContent]:
    data = await ot2_get("/runs")
    runs = data.get("data", [])
    if not runs:
        return [types.TextContent(type="text", text='{"status": "no_runs", "message": "No runs found on this robot."}')]
    latest = runs[-1]
    result = {
        "run_id": latest.get("id"),
        "status": latest.get("status"),
        "created_at": latest.get("createdAt"),
        "current_step": latest.get("currentOffsetId"),
    }
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_home() -> list[types.TextContent]:
    await ot2_post("/robot/home", {"target": "robot"})
    return [types.TextContent(type="text", text='{"status": "homed", "message": "Robot homed successfully."}')]


async def handle_create_protocol(args: dict) -> list[types.TextContent]:
    name = args["name"].replace(" ", "_")
    steps = args.get("steps", [])
    description = args.get("description", "")
    tip_strategy = args.get("tip_strategy", "new_tips_each_transfer")
    path = f"protocols/{name}.py"

    imports = [
        "import asyncio",
        "from pylabrobot.liquid_handling import LiquidHandler",
        "from pylabrobot.resources import OTDeck",
        "from pylabrobot.liquid_handling.backends import OpentronsBackend",
        "",
        f'"""{description}"""',
        "",
        "async def run():",
        "    backend = OpentronsBackend(host='OT2_IP_HERE', port=31950)",
        "    deck = OTDeck()",
        "    lh = LiquidHandler(backend=backend, deck=deck)",
        "    await lh.setup()",
        "",
    ]

    step_lines = []
    for i, step in enumerate(steps):
        t = step.get("type", "transfer")
        vol = step.get("volume_ul", 50)
        src = step.get("source", "")
        dst = step.get("destination", "")
        cycles = step.get("mix_cycles", 3)

        if t == "pick_up_tips":
            step_lines.append(f"    # Step {i+1}: Pick up tips")
            step_lines.append(f"    await lh.pick_up_tips(tip_rack['{src}'])")
        elif t == "aspirate":
            step_lines.append(f"    # Step {i+1}: Aspirate {vol} µL from {src}")
            step_lines.append(f"    await lh.aspirate(plate['{src}'], vols={vol})")
        elif t == "dispense":
            step_lines.append(f"    # Step {i+1}: Dispense {vol} µL to {dst}")
            step_lines.append(f"    await lh.dispense(plate['{dst}'], vols={vol})")
        elif t == "transfer":
            step_lines.append(f"    # Step {i+1}: Transfer {vol} µL from {src} to {dst}")
            step_lines.append(f"    await lh.transfer(plate['{src}'], plate['{dst}'], volume={vol})")
        elif t == "mix":
            step_lines.append(f"    # Step {i+1}: Mix {cycles}x at {vol} µL in {src}")
            step_lines.append(f"    await lh.mix(plate['{src}'], volume={vol}, repetitions={cycles})")
        elif t == "drop_tips":
            step_lines.append(f"    # Step {i+1}: Drop tips")
            step_lines.append(f"    await lh.drop_tips(tip_rack['{src}'])")
        step_lines.append("")

    step_lines.append("    await lh.stop()")
    step_lines.append("")
    step_lines.append("if __name__ == '__main__':")
    step_lines.append("    asyncio.run(run())")

    code = "\n".join(imports + step_lines)

    import os
    os.makedirs("protocols", exist_ok=True)
    with open(path, "w") as f:
        f.write(code)

    return [types.TextContent(type="text", text=json.dumps({
        "saved_to": path,
        "steps": len(steps),
        "tip_strategy": tip_strategy,
        "next_steps": f"Review {path}, update OT2_IP_HERE, then upload to OT-2 via Opentrons App or HTTP API."
    }, indent=2))]


def mock_deck() -> dict:
    return {
        "connected": False,
        "mode": "mock",
        "labware": [
            {"slot": "1", "name": "corning_96_wellplate_360ul_flat", "display_name": "Sample plate", "is_tiprack": False},
            {"slot": "4", "name": "corning_96_wellplate_360ul_flat", "display_name": "Destination plate", "is_tiprack": False},
            {"slot": "7", "name": "opentrons_96_tiprack_300ul", "display_name": "300 µL tips", "is_tiprack": True},
            {"slot": "10", "name": "agilent_1_reservoir_290ml", "display_name": "Reagent reservoir", "is_tiprack": False},
        ]
    }


async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
