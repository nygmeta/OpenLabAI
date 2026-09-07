"""
OpenLabAI: Hamilton STAR / STARlet MCP Server
Wraps PyLabRobot's Hamilton STAR backend.

Usage:
    python mcp_servers/hamilton_server.py --deck starlet

Tools exposed to Claude:
    read_deck()          - Report the configured deck layout and connection state
    create_protocol()    - Generate a PyLabRobot protocol file for the STAR/STARlet
    simulate_protocol()  - Dry-run a generated protocol through PyLabRobot's
                           chatterbox backend, which logs every command instead of
                           sending it to hardware
    get_status()         - Backend connection state and PyLabRobot version

Not implemented: this server does not execute protocols on a physical Hamilton.
simulate_protocol() never touches hardware. Generated files are reviewed by a
human and run deliberately.

Hardware requirements for a live connection (not exercised by this server):
Windows, a STAR or STARlet on USB, and the libusbK driver in place of Hamilton's
default driver. See https://docs.pylabrobot.org. Without those, every tool here
still works and reports "mode": "simulation".
"""

import os
import json
import asyncio
import argparse

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

try:
    from pylabrobot.liquid_handling import LiquidHandler
    from pylabrobot.liquid_handling.backends import STARBackend
    from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
    from pylabrobot.resources import STARDeck, STARLetDeck
    HAS_PLR = True
    PLR_IMPORT_ERROR = ""
except Exception as exc:                     # pragma: no cover - depends on install
    HAS_PLR = False
    PLR_IMPORT_ERROR = str(exc)

parser = argparse.ArgumentParser()
parser.add_argument("--deck", default="starlet", choices=["star", "starlet"],
                    help="Deck geometry to assume (default: starlet)")
args, _ = parser.parse_known_args()

DECK_CHOICE = args.deck
PROTOCOL_DIR = "protocols"

app = Server("openlabai-hamilton")


# Deck model. PyLabRobot addresses a Hamilton STAR/STARlet deck by rail number,
# with carriers mounted on rails and labware in carrier sites. These are the
# named positions this server builds and that generated protocols address. They
# match DECK_CONSTRAINTS["Hamilton_STAR"] in evals/protocol_evals.py, so a
# protocol the eval framework accepts uses names the deck actually has.
TIP_CARRIER_RAIL = 1
PLATE_CARRIER_RAIL = 10
TIP_SITES = 5
PLATE_SITES = 5

TIP_RACK_CLASS = "hamilton_96_tiprack_1000uL_filter"
PLATE_CLASS = "Cor_96_wellplate_360ul_Fb"
CHANNELS = 8


def deck_positions() -> list:
    """Named labware positions on the deck this server builds."""
    return ([f"tips_{i}" for i in range(1, TIP_SITES + 1)]
            + [f"plate_{i}" for i in range(1, PLATE_SITES + 1)])


def deck_layout() -> dict:
    return {
        "tip_carrier": {
            "type": "TIP_CAR_480_A00",
            "rail": TIP_CARRIER_RAIL,
            "sites": {f"tips_{i}": TIP_RACK_CLASS for i in range(1, TIP_SITES + 1)},
        },
        "plate_carrier": {
            "type": "PLT_CAR_L5AC_A00",
            "rail": PLATE_CARRIER_RAIL,
            "sites": {f"plate_{i}": PLATE_CLASS for i in range(1, PLATE_SITES + 1)},
        },
    }


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_deck",
            description=(
                "Report the Hamilton STAR/STARlet deck layout this server is configured for, "
                "and whether a physical instrument is connected. Use this before creating a protocol."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_status",
            description="Report backend connection state and whether PyLabRobot is installed.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="create_protocol",
            description=(
                "Generate a PyLabRobot protocol file for a Hamilton STAR or STARlet from a "
                "structured description. Writes a .py file to protocols/ and returns the path. "
                "This does not run anything."
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
                                "type": {"type": "string",
                                         "enum": ["aspirate", "dispense", "transfer", "mix",
                                                  "pick_up_tips", "drop_tips"]},
                                "label": {"type": "string", "description": "Human-readable step label"},
                                "source": {"type": "string", "description": "Deck position, e.g. P1"},
                                "destination": {"type": "string", "description": "Deck position, e.g. P4"},
                                "volume_ul": {"type": "number"},
                                "mix_cycles": {"type": "integer"},
                            },
                        },
                    },
                },
                "required": ["name", "steps"],
            },
        ),
        types.Tool(
            name="simulate_protocol",
            description=(
                "Dry-run a generated protocol through PyLabRobot's chatterbox backend. Every "
                "command is logged instead of being sent to hardware, so this is safe to run "
                "with no instrument attached. Returns the command log or the error that stopped it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Path to a protocol file previously written by create_protocol"},
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "read_deck":
            return _json(handle_read_deck())
        if name == "get_status":
            return _json(handle_status())
        if name == "create_protocol":
            return _json(handle_create_protocol(arguments))
        if name == "simulate_protocol":
            return _json(await handle_simulate(arguments))
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        return _json({"error": str(exc), "tool": name})


def _json(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def handle_status() -> dict:
    return {
        "pylabrobot_installed": HAS_PLR,
        "import_error": PLR_IMPORT_ERROR,
        "deck": DECK_CHOICE,
        "connected": False,
        "mode": "simulation",
        "note": (
            "This server has not been run against a physical Hamilton STAR or STARlet. "
            "A live connection needs Windows, USB, and the libusbK driver."
        ),
    }


def handle_read_deck() -> dict:
    return {
        "instrument": f"Hamilton {DECK_CHOICE.upper()}",
        "mode": "simulation",
        "connected": False,
        "channels": CHANNELS,
        "layout": deck_layout(),
        "positions": deck_positions(),
        "note": (
            "This is the deck geometry the server builds for generated protocols, not a "
            "reading from an attached instrument. No physical Hamilton has been connected. "
            "Address labware by the names in 'positions' and a column number 1-12."
        ),
    }


def handle_create_protocol(args_: dict) -> dict:
    name = args_["name"].replace(" ", "_")
    steps = args_.get("steps", [])
    description = args_.get("description", "")
    path = os.path.join(PROTOCOL_DIR, f"{name}.py")

    valid = set(deck_positions())
    warnings = []
    bad = sorted({
        pos for step in steps
        for pos in (step.get("source"), step.get("destination"))
        if pos and pos not in valid
    })
    if bad:
        warnings.append(
            f"Position(s) not on this deck: {', '.join(bad)}. "
            f"Valid positions are {', '.join(deck_positions())}."
        )
    for step in steps:
        col = step.get("column", 1)
        if not isinstance(col, int) or not 1 <= col <= 12:
            warnings.append(f"Column {col!r} out of range for a 96-well plate; use 1-12.")

    deck_cls = "STARDeck" if DECK_CHOICE == "star" else "STARLetDeck"
    head = [
        '"""',
        f"{description or name}",
        "",
        "Generated by OpenLabAI for a Hamilton STAR/STARlet via PyLabRobot.",
        "",
        "Review before running. Dry-run first:",
        f"    python {path} --simulate",
        "Running without --simulate requires a physical Hamilton and moves hardware.",
        '"""',
        "",
        "import sys",
        "import asyncio",
        "",
        "from pylabrobot.liquid_handling import LiquidHandler",
        "from pylabrobot.liquid_handling.backends import STARBackend",
        "from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend",
        "from pylabrobot.resources import (",
        f"    {deck_cls},",
        "    TIP_CAR_480_A00,",
        "    PLT_CAR_L5AC_A00,",
        f"    {TIP_RACK_CLASS},",
        f"    {PLATE_CLASS},",
        ")",
        "",
        f"CHANNELS = {CHANNELS}",
        "",
        "",
        "def build_deck(lh):",
        '    """Mount carriers on rails and labware in carrier sites."""',
        '    tip_car = TIP_CAR_480_A00(name="tip_carrier")',
    ]
    for i in range(1, TIP_SITES + 1):
        head.append(f'    tip_car[{i - 1}] = {TIP_RACK_CLASS}(name="tips_{i}")')
    head.append(f"    lh.deck.assign_child_resource(tip_car, rails={TIP_CARRIER_RAIL})")
    head.append('    plt_car = PLT_CAR_L5AC_A00(name="plate_carrier")')
    for i in range(1, PLATE_SITES + 1):
        head.append(f'    plt_car[{i - 1}] = {PLATE_CLASS}(name="plate_{i}")')
    head.append(f"    lh.deck.assign_child_resource(plt_car, rails={PLATE_CARRIER_RAIL})")
    head += [
        "",
        "",
        "async def run(simulate: bool = False):",
        "    backend = (LiquidHandlerChatterboxBackend(num_channels=CHANNELS)",
        "               if simulate else STARBackend())",
        f"    lh = LiquidHandler(backend=backend, deck={deck_cls}())",
        "    build_deck(lh)",
        "    await lh.setup()",
        "",
    ]

    body = []
    for i, step in enumerate(steps, start=1):
        kind = step.get("type", "transfer")
        vol = float(step.get("volume_ul", 50))
        src = step.get("source", "")
        dst = step.get("destination", "")
        col = step.get("column", 1)
        cycles = int(step.get("mix_cycles", 3))
        label = step.get("label") or f"{kind} step"
        span = f'"A{col}:H{col}"' if CHANNELS == 8 else f'"A{col}"'
        vols = f"[{vol}] * CHANNELS"
        body.append(f"    # Step {i}: {label}")
        if kind == "pick_up_tips":
            body.append(f'    await lh.pick_up_tips(lh.deck.get_resource("{src}")[{span}])')
        elif kind == "drop_tips":
            body.append(f'    await lh.drop_tips(lh.deck.get_resource("{src}")[{span}])')
        elif kind == "aspirate":
            body.append(f'    await lh.aspirate(lh.deck.get_resource("{src}")[{span}], vols={vols})')
        elif kind == "dispense":
            body.append(f'    await lh.dispense(lh.deck.get_resource("{dst}")[{span}], vols={vols})')
        elif kind == "mix":
            body.append(f"    for _ in range({cycles}):")
            body.append(f'        await lh.aspirate(lh.deck.get_resource("{src}")[{span}], vols={vols})')
            body.append(f'        await lh.dispense(lh.deck.get_resource("{src}")[{span}], vols={vols})')
        else:  # transfer
            body.append(f'    await lh.aspirate(lh.deck.get_resource("{src}")[{span}], vols={vols})')
            body.append(f'    await lh.dispense(lh.deck.get_resource("{dst}")[{span}], vols={vols})')
        body.append("")

    tail = [
        "    await lh.stop()",
        "",
        "",
        'if __name__ == "__main__":',
        '    asyncio.run(run(simulate="--simulate" in sys.argv))',
        "",
    ]

    os.makedirs(PROTOCOL_DIR, exist_ok=True)
    with open(path, "w") as handle:
        handle.write("\n".join(head + body + tail))

    result = {
        "saved_to": path,
        "steps": len(steps),
        "deck": DECK_CHOICE,
        "channels": CHANNELS,
        "next_steps": (
            f"Review {path}, then dry-run it with simulate_protocol before any hardware run."
        ),
    }
    if warnings:
        result["warnings"] = warnings
    return result


async def handle_simulate(args_: dict) -> dict:
    path = args_["path"]
    if not os.path.exists(path):
        return {"error": f"No such protocol file: {path}"}
    if not HAS_PLR:
        return {"error": "PyLabRobot is not installed", "import_error": PLR_IMPORT_ERROR}

    import io
    import runpy
    import contextlib

    buffer = io.StringIO()
    try:
        module = runpy.run_path(path, run_name="openlabai_simulate")
        entry = module.get("run")
        if entry is None:
            return {"error": f"{path} defines no run() coroutine"}
        with contextlib.redirect_stdout(buffer):
            await entry(simulate=True)
    except Exception as exc:
        return {
            "path": path,
            "mode": "simulation",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "log": buffer.getvalue().splitlines()[-40:],
        }

    log = buffer.getvalue().splitlines()
    return {
        "path": path,
        "mode": "simulation",
        "ok": True,
        "commands_logged": len(log),
        "log": log[:200],
        "note": "Chatterbox backend — no hardware was contacted.",
    }


async def main():
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
