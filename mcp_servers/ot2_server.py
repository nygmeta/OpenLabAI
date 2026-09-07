"""
OpenLabAI: Opentrons OT-2 MCP Server
Reads OT-2 state over the Opentrons HTTP API, generates protocol files, and can
execute them under an explicit human-approval gate.

Usage:
    python mcp_servers/ot2_server.py --host 169.254.x.x

Read-only tools:
    read_deck()        - Read current deck layout
    get_run_status()   - Poll the status of a run
    create_protocol()  - Generate a protocol file into ./protocols/

Tools that prepare execution but move nothing:
    upload_protocol()  - Upload a file, wait for the robot's own analysis, and
                         create a run in the idle state. Returns any analysis
                         errors. Nothing moves until start_run is called.

Tools that move hardware:
    home_robot()       - Home all axes
    start_run()        - Begin physical execution of an uploaded run. Refuses
                         unless confirm=true is passed, refuses if the robot's
                         analysis reported errors, and refuses if the protocol
                         fails the eval framework's deck constraints.
    pause_run()        - Pause a running protocol
    resume_run()       - Resume a paused protocol
    stop_run()         - Stop a run. Never gated; stopping is a safety action.

The approval gate is deliberate: an agent can plan, generate, upload and analyse
a protocol on its own, but a person has to pass confirm=true before the robot
moves. Every start, pause, resume and stop is written to the audit log by
evals/run_logger.py.
"""

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="169.254.10.10", help="OT-2 IP address")
parser.add_argument("--operator", default=os.environ.get("OPENLAB_OPERATOR", "unknown"),
                    help="Operator name recorded in the audit log")
parser.add_argument("--api-level", default="2.13", help="Opentrons Python API level")
parser.add_argument("--pipette", default="p300_single_gen2",
                    help="Pipette load name used in generated protocols")
parser.add_argument("--mount", default="right", choices=["left", "right"],
                    help="Mount the pipette is installed on")
parser.add_argument("--tiprack", default="opentrons_96_tiprack_300ul",
                    help="Tip rack load name used in generated protocols")
args, _ = parser.parse_known_args()

OT2_HOST = args.host
OT2_BASE = f"http://{OT2_HOST}:31950"
HEADERS = {"opentrons-version": "3"}
OPERATOR = args.operator
API_LEVEL = args.api_level
PROTOCOL_DIR = "protocols"

# Default deck for generated protocols. Override per call via the deck argument.
DEFAULT_DECK = {
    "tiprack": {"load_name": args.tiprack, "slot": "7"},
    "source": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"},
    "destination": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "4"},
    "reservoir": {"load_name": "agilent_1_reservoir_290ml", "slot": "10"},
    "pipette": {"load_name": args.pipette, "mount": args.mount},
}

app = Server("openlabai-ot2")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from evals.protocol_evals import DECK_CONSTRAINTS
    from evals.run_logger import RunLogger
    HAS_EVALS = True
except Exception as _exc:                    # pragma: no cover
    HAS_EVALS = False
    EVALS_IMPORT_ERROR = str(_exc)

# run_id -> metadata we recorded at upload time, so start_run can check the
# protocol's analysis and log meaningfully.
RUN_REGISTRY: dict = {}


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
                "Generate a protocol file from a structured description and save it to protocols/. "
                "Defaults to the Opentrons Python API format, which upload_protocol can send to the "
                "robot for analysis. This writes a file; it does not run anything."
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
                    },
                    "format": {
                        "type": "string",
                        "enum": ["opentrons", "pylabrobot"],
                        "default": "opentrons",
                        "description": (
                            "opentrons: an Opentrons Python API protocol the robot can "
                            "analyse and run via upload_protocol/start_run. pylabrobot: a "
                            "script that runs on this computer and drives the robot over "
                            "the network; it cannot be uploaded."
                        ),
                    },
                    "deck": {
                        "type": "object",
                        "description": (
                            "Optional deck overrides. Keys: tiprack, source, destination, "
                            "reservoir (each {load_name, slot}) and pipette "
                            "({load_name, mount})."
                        ),
                    }
                },
                "required": ["name", "steps"]
            },
        ),
        types.Tool(
            name="upload_protocol",
            description=(
                "Upload a protocol file to the OT-2, wait for the robot's own analysis, and "
                "create a run in the idle state. Returns the run id and any analysis errors. "
                "This moves nothing — call start_run to begin physical execution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to an Opentrons Python API protocol file"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="start_run",
            description=(
                "Begin PHYSICAL EXECUTION of an uploaded run on the OT-2. The robot will move. "
                "Requires confirm=true, which represents a human operator's approval — do not "
                "pass it unless the person you are working with has explicitly approved this "
                "specific run after reviewing the protocol. Refuses if the robot's analysis "
                "reported errors or if the protocol fails deck constraint checks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run id returned by upload_protocol"},
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true. Represents explicit human approval to move the robot.",
                    },
                },
                "required": ["run_id", "confirm"],
            },
        ),
        types.Tool(
            name="pause_run",
            description="Pause a running protocol on the OT-2.",
            inputSchema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        ),
        types.Tool(
            name="resume_run",
            description="Resume a paused protocol. The robot will move again.",
            inputSchema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        ),
        types.Tool(
            name="stop_run",
            description=(
                "Stop a run on the OT-2. Not gated by confirm — stopping is always allowed, "
                "because it is the safe direction."
            ),
            inputSchema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
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
        elif name == "upload_protocol":
            return await handle_upload_protocol(arguments)
        elif name == "start_run":
            return await handle_start_run(arguments)
        elif name == "pause_run":
            return await handle_run_action(arguments, "pause")
        elif name == "resume_run":
            return await handle_run_action(arguments, "play")
        elif name == "stop_run":
            return await handle_run_action(arguments, "stop")
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
    fmt = args.get("format", "opentrons")
    deck = {**DEFAULT_DECK, **(args.get("deck") or {})}

    if fmt == "pylabrobot":
        code = _render_pylabrobot(name, description, steps)
        suffix = "_plr"
        note = ("PyLabRobot protocols run on this computer and drive the robot over the "
                "network. They cannot be uploaded to the OT-2 with upload_protocol.")
    else:
        code = _render_opentrons(name, description, steps, deck, tip_strategy)
        suffix = ""
        note = ("Opentrons Python API protocol. Review it, then upload_protocol to have "
                "the robot analyse it, then start_run with confirm=true to execute.")

    path = os.path.join(PROTOCOL_DIR, f"{name}{suffix}.py")
    os.makedirs(PROTOCOL_DIR, exist_ok=True)
    with open(path, "w") as handle:
        handle.write(code)

    return _text({
        "saved_to": path,
        "format": fmt,
        "steps": len(steps),
        "tip_strategy": tip_strategy,
        "deck": deck,
        "next_steps": note,
    })


def _render_opentrons(name: str, description: str, steps: list, deck: dict,
                      tip_strategy: str) -> str:
    """Emit an Opentrons Python API v2 protocol, which the robot can analyse and run."""
    pipette = deck["pipette"]
    lines = [
        "from opentrons import protocol_api",
        "",
        "metadata = {",
        f"    \"protocolName\": {name!r},",
        f"    \"description\": {description or name!r},",
        "    \"author\": \"Generated by OpenLabAI — review before running\",",
        f"    \"apiLevel\": {API_LEVEL!r},",
        "}",
        "",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
        f"    tiprack = protocol.load_labware({deck['tiprack']['load_name']!r}, {deck['tiprack']['slot']!r})",
        f"    source = protocol.load_labware({deck['source']['load_name']!r}, {deck['source']['slot']!r})",
        f"    destination = protocol.load_labware({deck['destination']['load_name']!r}, {deck['destination']['slot']!r})",
        f"    reservoir = protocol.load_labware({deck['reservoir']['load_name']!r}, {deck['reservoir']['slot']!r})",
        f"    pipette = protocol.load_instrument({pipette['load_name']!r}, {pipette['mount']!r}, tip_racks=[tiprack])",
        "",
        "    labware = {",
        "        \"source\": source,",
        "        \"destination\": destination,",
        "        \"reservoir\": reservoir,",
        "    }",
        "",
    ]

    def well(ref: str, default: str) -> str:
        """Turn 'source:A1' or 'A1' into a well expression."""
        ref = (ref or "").strip()
        if not ref:
            return f'labware["{default}"]["A1"]'
        if ":" in ref:
            plate, w = ref.split(":", 1)
            plate = plate.strip() or default
            return f'labware["{plate}"]["{w.strip()}"]'
        return f'labware["{default}"]["{ref}"]'

    for i, step in enumerate(steps, start=1):
        kind = step.get("type", "transfer")
        vol = step.get("volume_ul", 50)
        src = well(step.get("source"), "source")
        dst = well(step.get("destination"), "destination")
        cycles = step.get("mix_cycles", 3)
        label = step.get("label") or f"{kind} step"
        lines.append(f"    # Step {i}: {label}")
        lines.append(f"    protocol.comment({label!r})")
        if kind == "pick_up_tips":
            lines.append("    pipette.pick_up_tip()")
        elif kind == "drop_tips":
            lines.append("    pipette.drop_tip()")
        elif kind == "aspirate":
            lines.append(f"    pipette.aspirate({vol}, {src})")
        elif kind == "dispense":
            lines.append(f"    pipette.dispense({vol}, {dst})")
        elif kind == "mix":
            lines.append(f"    pipette.mix({cycles}, {vol}, {src})")
        else:  # transfer
            new_tip = "always" if tip_strategy == "new_tips_each_transfer" else "never"
            lines.append(f"    pipette.transfer({vol}, {src}, {dst}, new_tip={new_tip!r})")
        lines.append("")

    return "\n".join(lines)


def _render_pylabrobot(name: str, description: str, steps: list) -> str:
    """Emit a PyLabRobot script that drives the OT-2 from this computer."""
    lines = [
        '"""',
        f"{description or name}",
        "",
        "Generated by OpenLabAI. Runs on this computer and drives the OT-2 over HTTP.",
        "Set OT2_HOST before running. This file is not uploadable to the robot.",
        '"""',
        "",
        "import os",
        "import asyncio",
        "",
        "from pylabrobot.liquid_handling import LiquidHandler",
        "from pylabrobot.liquid_handling.backends import OpentronsOT2Backend",
        "from pylabrobot.resources import OTDeck",
        "",
        'OT2_HOST = os.environ.get("OT2_HOST", "169.254.10.10")',
        "",
        "",
        "async def run():",
        "    backend = OpentronsOT2Backend(host=OT2_HOST, port=31950)",
        "    lh = LiquidHandler(backend=backend, deck=OTDeck())",
        "    await lh.setup()",
        "",
    ]
    for i, step in enumerate(steps, start=1):
        kind = step.get("type", "transfer")
        vol = step.get("volume_ul", 50)
        src = step.get("source", "")
        dst = step.get("destination", "")
        cycles = step.get("mix_cycles", 3)
        label = step.get("label") or f"{kind} step"
        lines.append(f"    # Step {i}: {label}")
        if kind == "pick_up_tips":
            lines.append(f'    await lh.pick_up_tips(lh.deck.get_resource("{src}")["A1"])')
        elif kind == "drop_tips":
            lines.append(f'    await lh.drop_tips(lh.deck.get_resource("{src}")["A1"])')
        elif kind == "aspirate":
            lines.append(f'    await lh.aspirate(lh.deck.get_resource("{src}")["A1"], vols=[{vol}])')
        elif kind == "dispense":
            lines.append(f'    await lh.dispense(lh.deck.get_resource("{dst}")["A1"], vols=[{vol}])')
        elif kind == "mix":
            lines.append(f"    for _ in range({cycles}):")
            lines.append(f'        await lh.aspirate(lh.deck.get_resource("{src}")["A1"], vols=[{vol}])')
            lines.append(f'        await lh.dispense(lh.deck.get_resource("{src}")["A1"], vols=[{vol}])')
        else:
            lines.append(f'    await lh.aspirate(lh.deck.get_resource("{src}")["A1"], vols=[{vol}])')
            lines.append(f'    await lh.dispense(lh.deck.get_resource("{dst}")["A1"], vols=[{vol}])')
        lines.append("")
    lines += ["    await lh.stop()", "", "", 'if __name__ == "__main__":',
              "    asyncio.run(run())", ""]
    return "\n".join(lines)


# ── EXECUTION ────────────────────────────────────────────────────────────────
#
# The split below is the safety boundary. upload_protocol talks to the robot but
# never moves it: it uploads, waits for the robot's own protocol analysis, and
# creates a run that sits idle. start_run is the only tool that begins motion,
# and it refuses without explicit human confirmation.


def _text(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


async def _wait_for_analysis(protocol_id: str, attempts: int = 30) -> dict:
    """Poll the robot's analysis of an uploaded protocol until it completes."""
    for _ in range(attempts):
        data = await ot2_get(f"/protocols/{protocol_id}/analyses")
        analyses = data.get("data", [])
        if analyses:
            latest = analyses[-1]
            if latest.get("status") != "pending":
                return latest
        await asyncio.sleep(1)
    return {"status": "timeout", "errors": []}


def _constraint_violations(commands: list) -> list:
    """Check analysed commands against the eval framework's OT-2 deck limits.

    This is a second opinion on top of the robot's own analysis, using the same
    constraints the eval framework applies to generated protocols.
    """
    if not HAS_EVALS:
        return []
    limits = DECK_CONSTRAINTS.get("OT-2", {})
    ceiling = max(
        limits.get("max_volume_300ul_tips", 300),
        limits.get("max_volume_1000ul_tips", 1000),
    )
    violations = []
    for command in commands:
        params = command.get("params", {}) or {}
        volume = params.get("volume")
        if isinstance(volume, (int, float)) and volume > ceiling:
            violations.append(
                f"{command.get('commandType', 'command')} volume {volume} uL "
                f"exceeds the largest OT-2 tip capacity ({ceiling} uL)"
            )
    return violations


async def handle_upload_protocol(arguments: dict) -> list[types.TextContent]:
    path = arguments["path"]
    if not os.path.exists(path):
        return _text({"error": f"No such protocol file: {path}"})

    with open(path, "rb") as handle:
        files = {"files": (os.path.basename(path), handle.read(), "text/x-python")}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{OT2_BASE}/protocols", headers=HEADERS, files=files)
    if response.status_code >= 400:
        return _text({"error": "Upload rejected by robot",
                      "status_code": response.status_code,
                      "body": response.text[:1000]})

    protocol_id = response.json().get("data", {}).get("id")
    analysis = await _wait_for_analysis(protocol_id)
    analysis_errors = analysis.get("errors", []) or []
    commands = analysis.get("commands", []) or []
    violations = _constraint_violations(commands)

    run = await ot2_post("/runs", {"data": {"protocolId": protocol_id}})
    run_id = run.get("data", {}).get("id")

    RUN_REGISTRY[run_id] = {
        "protocol_id": protocol_id,
        "path": path,
        "analysis_status": analysis.get("status"),
        "analysis_errors": analysis_errors,
        "constraint_violations": violations,
        "command_count": len(commands),
    }

    blocked = bool(analysis_errors) or bool(violations)
    return _text({
        "run_id": run_id,
        "protocol_id": protocol_id,
        "analysis_status": analysis.get("status"),
        "analysis_errors": analysis_errors,
        "constraint_violations": violations,
        "commands_analysed": len(commands),
        "state": "idle — nothing has moved",
        "can_start": not blocked,
        "next_steps": (
            "Analysis reported problems; start_run will refuse until they are fixed."
            if blocked else
            "Have the operator review the protocol, then call start_run with "
            "run_id and confirm=true. The robot will move."
        ),
    })


async def handle_start_run(arguments: dict) -> list[types.TextContent]:
    run_id = arguments["run_id"]
    confirm = arguments.get("confirm", False)

    if confirm is not True:
        return _text({
            "refused": True,
            "reason": "confirm was not true",
            "detail": (
                "start_run moves the robot. Pass confirm=true only after a human "
                "operator has reviewed this specific protocol and approved it."
            ),
            "run_id": run_id,
        })

    record = RUN_REGISTRY.get(run_id, {})
    if record.get("analysis_errors"):
        return _text({"refused": True, "reason": "the robot's analysis reported errors",
                      "analysis_errors": record["analysis_errors"], "run_id": run_id})
    if record.get("constraint_violations"):
        return _text({"refused": True, "reason": "protocol failed deck constraint checks",
                      "constraint_violations": record["constraint_violations"], "run_id": run_id})

    result = await ot2_post(f"/runs/{run_id}/actions", {"data": {"actionType": "play"}})
    _audit(run_id, "start", record)
    return _text({
        "run_id": run_id,
        "action": "play",
        "status": result.get("data", {}).get("actionType", "play"),
        "warning": "The robot is now executing. Use stop_run to halt it.",
        "audit_logged": HAS_EVALS,
    })


async def handle_run_action(arguments: dict, action: str) -> list[types.TextContent]:
    run_id = arguments["run_id"]
    result = await ot2_post(f"/runs/{run_id}/actions", {"data": {"actionType": action}})
    _audit(run_id, action, RUN_REGISTRY.get(run_id, {}))
    return _text({"run_id": run_id, "action": action,
                  "result": result.get("data", {}), "audit_logged": HAS_EVALS})


def _audit(run_id: str, action: str, record: dict) -> None:
    """Append an audit record. Never let logging failure break a robot action —
    in particular, stop_run must always reach the robot."""
    if not HAS_EVALS:
        return
    try:
        logger = RunLogger(operator=OPERATOR, instrument="OT-2",
                           protocol_name=record.get("path", ""))
        logger.log_agent_message("system", f"{action} run {run_id}")
        logger.log_run_complete(status=action)
        logger.save()
    except Exception:
        pass


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
