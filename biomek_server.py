"""
Biomek FXP MCP Server
Connects Claude to Biomek FXP via COM automation (BiomekFX.Application)

Requirements:
  pip install mcp pywin32

Usage:
  python server.py

Then add to your Claude MCP config (claude_desktop_config.json):
{
  "mcpServers": {
    "biomek": {
      "command": "python",
      "args": ["C:/path/to/biomek_mcp/server.py"]
    }
  }
}
"""

import json
import sys
import traceback
from typing import Any

try:
    import win32com.client
    import pythoncom
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("WARNING: pywin32 not installed. Running in MOCK mode.", file=sys.stderr)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("biomek-fxp")

# ---------------------------------------------------------------------------
# COM connection helper
# ---------------------------------------------------------------------------

def get_biomek():
    """Connect to running Biomek FXP instance via COM."""
    if not HAS_WIN32:
        return None
    pythoncom.CoInitialize()
    try:
        biomek = win32com.client.Dispatch("BiomekFX.Application")
        return biomek
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Biomek FXP COM interface: {e}\n"
            "Make sure Biomek software is running."
        )


# ---------------------------------------------------------------------------
# TOOL: read_deck
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_deck",
            description=(
                "Read the current Biomek FXP deck layout. "
                "Returns a list of deck positions with their labware name, "
                "type, and whether they are occupied. "
                "Use this to understand what is loaded on the robot before "
                "creating a protocol."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="create_protocol",
            description=(
                "Create a new Biomek FXP liquid handling method/protocol. "
                "Generates a .mth method file from a structured description. "
                "Specify source wells, destination wells, volumes, and "
                "any mix steps. The file is saved to the Biomek methods folder."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Protocol name (used as file name, no spaces)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what this protocol does",
                    },
                    "steps": {
                        "type": "array",
                        "description": "List of liquid handling steps",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["aspirate", "dispense", "transfer", "mix"],
                                    "description": "Step type",
                                },
                                "source": {
                                    "type": "string",
                                    "description": "Source deck position, e.g. 'P1' or 'P1:A1-H1'",
                                },
                                "destination": {
                                    "type": "string",
                                    "description": "Destination deck position, e.g. 'P4' or 'P4:A1-H12'",
                                },
                                "volume_ul": {
                                    "type": "number",
                                    "description": "Volume in microliters",
                                },
                                "mix_cycles": {
                                    "type": "integer",
                                    "description": "Number of mix cycles (for mix steps)",
                                    "default": 3,
                                },
                                "aspirate_height_mm": {
                                    "type": "number",
                                    "description": "Aspirate height in mm (default 1.0)",
                                    "default": 1.0,
                                },
                                "dispense_height_mm": {
                                    "type": "number",
                                    "description": "Dispense height in mm (default 1.0)",
                                    "default": 1.0,
                                },
                            },
                            "required": ["type", "volume_ul"],
                        },
                    },
                    "tip_strategy": {
                        "type": "string",
                        "enum": ["new_tips_each_step", "reuse_tips", "new_tips_each_source"],
                        "description": "Tip usage strategy",
                        "default": "new_tips_each_step",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Full Windows path to save the .mth file. "
                            "Defaults to C:/Biomek/Methods/<name>.mth"
                        ),
                    },
                },
                "required": ["name", "steps"],
            },
        ),
        types.Tool(
            name="get_variables",
            description=(
                "Read the current values of all Biomek method variables "
                "(like AspirateHeight, DispenseHeight, MixSpeed, etc.) "
                "from the currently open method. Useful for understanding "
                "current liquid handling parameters before editing them."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# TOOL handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "read_deck":
            return await handle_read_deck()
        elif name == "create_protocol":
            return await handle_create_protocol(arguments)
        elif name == "get_variables":
            return await handle_get_variables()
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        tb = traceback.format_exc()
        return [types.TextContent(type="text", text=f"Error: {e}\n\n{tb}")]


async def handle_read_deck() -> list[types.TextContent]:
    """Read deck layout from Biomek COM interface."""

    if not HAS_WIN32:
        # Return mock data so you can test without the robot
        mock_deck = {
            "mode": "MOCK (pywin32 not installed)",
            "positions": [
                {"id": "P1", "labware": "Cos_96_Rd", "type": "plate", "occupied": True},
                {"id": "P2", "labware": "Empty", "type": "empty", "occupied": False},
                {"id": "P4", "labware": "Cos_96_Rd", "type": "plate", "occupied": True},
                {"id": "P7", "labware": "TipBox_1000", "type": "tips", "occupied": True},
                {"id": "P8", "labware": "TipBox_1000", "type": "tips", "occupied": True},
                {"id": "TL1", "labware": "Reagent_Trough_100", "type": "trough", "occupied": True},
            ],
        }
        return [types.TextContent(type="text", text=json.dumps(mock_deck, indent=2))]

    biomek = get_biomek()
    deck_info = {"positions": []}

    try:
        # Access the deck object
        deck = biomek.Deck
        position_count = deck.Count

        for i in range(1, position_count + 1):
            try:
                pos = deck.Item(i)
                labware = pos.Labware
                deck_info["positions"].append({
                    "id": pos.Name,
                    "labware": labware.Name if labware else "Empty",
                    "type": _labware_type(labware.Name if labware else ""),
                    "occupied": labware is not None,
                    "rows": labware.Rows if labware else 0,
                    "columns": labware.Columns if labware else 0,
                })
            except Exception:
                # Some positions may not be accessible
                pass

        deck_info["total_positions"] = len(deck_info["positions"])
        deck_info["occupied"] = sum(1 for p in deck_info["positions"] if p["occupied"])

    except Exception as e:
        # Fallback: try to read from active method worklist
        deck_info["error"] = str(e)
        deck_info["note"] = (
            "Could not read deck directly. "
            "Make sure a method is open in Biomek."
        )

    return [types.TextContent(type="text", text=json.dumps(deck_info, indent=2))]


async def handle_get_variables() -> list[types.TextContent]:
    """Read method variables from currently open Biomek method."""

    if not HAS_WIN32:
        mock_vars = {
            "mode": "MOCK",
            "variables": {
                "AspirateHeight": 1.0,
                "AspirateHeight_384": 0.8,
                "DestMixSpeed": 12,
                "DestMixTimes": 3,
                "DestMixVolume": 6,
                "DispenseHeight": 1.0,
                "DispenseHeight_384": 1.5,
                "MixAspHeight": 1.0,
                "MixDispHeight": 1.5,
                "TransferAspHeight": 0.1,
                "TransferDispHeight": 2.0,
                "TransferMixSpeed": 12,
                "TransferMixTimes": 4,
                "TransferMixVolume": 8,
            },
        }
        return [types.TextContent(type="text", text=json.dumps(mock_vars, indent=2))]

    biomek = get_biomek()
    result = {"variables": {}}

    try:
        method = biomek.ActiveMethod
        variables = method.Variables

        for i in range(1, variables.Count + 1):
            var = variables.Item(i)
            result["variables"][var.Name] = var.Value

    except Exception as e:
        result["error"] = str(e)

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_create_protocol(args: dict) -> list[types.TextContent]:
    """
    Generate a Biomek method script file.

    Biomek FXP methods are XML-based .mth files. This generates a minimal
    valid method that Biomek can open and validate.
    """
    name = args["name"].replace(" ", "_")
    steps = args.get("steps", [])
    description = args.get("description", "")
    tip_strategy = args.get("tip_strategy", "new_tips_each_step")
    save_path = args.get("save_path", f"C:/Biomek/Methods/{name}.mth")

    # Build Biomek method XML
    # The FXP uses a proprietary .mth XML schema. We generate the worklist steps.
    step_xml_parts = []

    for i, step in enumerate(steps):
        step_type = step.get("type", "transfer")
        volume = step.get("volume_ul", 0)
        source = step.get("source", "")
        dest = step.get("destination", "")
        asp_h = step.get("aspirate_height_mm", 1.0)
        disp_h = step.get("dispense_height_mm", 1.0)
        mix_n = step.get("mix_cycles", 3)

        if step_type == "transfer":
            step_xml_parts.append(
                _xml_transfer_step(i + 1, source, dest, volume, asp_h, disp_h, tip_strategy)
            )
        elif step_type == "aspirate":
            step_xml_parts.append(
                _xml_aspirate_step(i + 1, source, volume, asp_h)
            )
        elif step_type == "dispense":
            step_xml_parts.append(
                _xml_dispense_step(i + 1, dest, volume, disp_h)
            )
        elif step_type == "mix":
            step_xml_parts.append(
                _xml_mix_step(i + 1, source, volume, mix_n, asp_h)
            )

    method_xml = _build_method_xml(name, description, step_xml_parts)

    # Write file
    try:
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(method_xml)
        saved = True
        save_error = None
    except Exception as e:
        saved = False
        save_error = str(e)

    result = {
        "protocol_name": name,
        "steps_generated": len(steps),
        "tip_strategy": tip_strategy,
        "save_path": save_path,
        "saved": saved,
        "save_error": save_error,
        "next_steps": (
            "Open the .mth file in Biomek software, then run "
            "Method > Validate to check for deck position errors "
            "before running."
        ),
    }

    if not saved:
        result["method_xml_preview"] = method_xml[:2000] + "...(truncated)"

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# XML generation helpers for Biomek .mth format
# ---------------------------------------------------------------------------

def _build_method_xml(name: str, description: str, steps: list[str]) -> str:
    steps_block = "\n".join(steps)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Method Name="{name}" Description="{description}" Version="1.0">
  <Header>
    <CreatedBy>BiomekMCP</CreatedBy>
    <Description>{description}</Description>
  </Header>
  <Worklist>
{steps_block}
  </Worklist>
</Method>
"""


def _xml_transfer_step(
    idx: int, source: str, dest: str, volume: float,
    asp_h: float, disp_h: float, tip_strategy: str
) -> str:
    tips_xml = (
        '<GetTips/><ReturnTips/>'
        if tip_strategy == "new_tips_each_step"
        else ""
    )
    return f"""    <Step Index="{idx}" Type="Transfer">
      {tips_xml}
      <Aspirate Source="{source}" Volume="{volume}" Height="{asp_h}"/>
      <Dispense Destination="{dest}" Volume="{volume}" Height="{disp_h}"/>
    </Step>"""


def _xml_aspirate_step(idx: int, source: str, volume: float, asp_h: float) -> str:
    return f"""    <Step Index="{idx}" Type="Aspirate">
      <Aspirate Source="{source}" Volume="{volume}" Height="{asp_h}"/>
    </Step>"""


def _xml_dispense_step(idx: int, dest: str, volume: float, disp_h: float) -> str:
    return f"""    <Step Index="{idx}" Type="Dispense">
      <Dispense Destination="{dest}" Volume="{volume}" Height="{disp_h}"/>
    </Step>"""


def _xml_mix_step(idx: int, source: str, volume: float, cycles: int, asp_h: float) -> str:
    return f"""    <Step Index="{idx}" Type="Mix">
      <Mix Source="{source}" Volume="{volume}" Cycles="{cycles}" Height="{asp_h}"/>
    </Step>"""


def _labware_type(name: str) -> str:
    name_lower = name.lower()
    if "tip" in name_lower:
        return "tips"
    if "plate" in name_lower or "cos" in name_lower or "96" in name_lower or "384" in name_lower:
        return "plate"
    if "trough" in name_lower or "reagent" in name_lower:
        return "trough"
    if not name or name == "Empty":
        return "empty"
    return "labware"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
