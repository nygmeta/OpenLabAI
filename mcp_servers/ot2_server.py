"""
OpenLabAI: Opentrons OT-2 MCP Server
Reads OT-2 state over the Opentrons HTTP API, generates protocol files, and can
execute them under an explicit human-approval gate.

Usage:
    python mcp_servers/ot2_server.py --host 169.254.x.x [--port 31950]

Read-only tools:
    read_deck()        - Read current deck layout
    get_run_status()   - Report a run's status, its errors, and the safe next
                         actions for that state (monitoring and recovery)
    create_protocol()  - Generate a protocol file into ./protocols/. The request
                         is validated first: names, well references, volumes and
                         deck entries that are malformed are rejected, so nothing
                         an agent passes in can end up as code in the file.

Tools that prepare execution but move nothing:
    upload_protocol()  - Upload a file, wait for the robot's own analysis, and
                         create a run in the idle state. Returns any analysis
                         errors. Nothing moves until start_run is called.

Tools that move hardware (each refuses unless confirm=true is passed):
    home_robot()       - Home all axes. Also refuses while a run is active.
    start_run()        - Begin physical execution of an uploaded run. Refuses a
                         run that was not uploaded through upload_protocol in
                         this session, refuses unless the robot's analysis
                         completed with no errors, refuses if the deck constraint
                         checks failed or could not be run, and refuses while
                         another run is active on the robot.
    resume_run()       - Resume a paused protocol

Tools that only make the robot safer:
    pause_run()        - Pause a running protocol
    stop_run()         - Stop a run. Never gated; stopping is a safety action.

The approval gate is deliberate: an agent can plan, generate, upload and analyse
a protocol on its own, but a person has to pass confirm=true before the robot
moves. Every home, start, pause, resume and stop is written to the audit log by
evals/run_logger.py, with the SHA-256 of the protocol file that was uploaded.

The checks fail closed. If the robot's analysis did not finish, if it produced no
commands, or if the constraint checks are unavailable, the answer is "do not
start", never "nothing was found, so go ahead".
"""

import os
import re
import sys
import json
import asyncio
import hashlib
import argparse
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="169.254.10.10", help="OT-2 IP address")
parser.add_argument("--port", type=int, default=31950,
                    help="Opentrons HTTP API port (31950 unless you tunnel it)")
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
OT2_BASE = f"http://{OT2_HOST}:{args.port}"
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

# ── LIMITS ON WHAT A REQUEST MAY CONTAIN ────────────────────────────────────
#
# create_protocol turns a request into Python source. Every value that ends up
# in that source is checked against one of these first.
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
WELL_RE = re.compile(r"^(?:(source|destination|reservoir)\s*:\s*)?([A-P](?:[1-9]|1[0-9]|2[0-4]))$")
RESOURCE_RE = re.compile(r"^[A-Za-z0-9_\-]{0,64}$")       # PyLabRobot resource names
LOAD_NAME_RE = re.compile(r"^[a-z0-9_.]{1,80}$")
STEP_TYPES = ("aspirate", "dispense", "transfer", "mix", "pick_up_tips", "drop_tips")
DECK_KEYS = ("tiprack", "source", "destination", "reservoir", "pipette")
OT2_SLOTS = {str(n) for n in range(1, 12)}
MAX_STEPS = 500
MAX_MIX_CYCLES = 50
MAX_LABEL_CHARS = 120
# The robot splits a transfer into several aspirations, so a transfer may exceed
# the pipette's capacity. Beyond this it is almost certainly a unit mistake.
MAX_TRANSFER_UL = 5000
MAX_PROTOCOL_BYTES = 1 << 20
# Run states in which the robot is, or may at any moment be, moving.
ACTIVE_RUN_STATES = ("running", "paused", "finishing", "stop-requested",
                     "blocked-by-open-door", "awaiting-recovery")


def pipette_capacity_ul(load_name: str):
    """'p300_single_gen2' -> 300. None when the name does not say."""
    match = re.match(r"^p(\d+)_", str(load_name or ""))
    return int(match.group(1)) if match else None


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
            description=(
                "Get the status of a run on the OT-2 (the most recent run when run_id is "
                "omitted). Reports the run's errors and the safe next actions for its state, "
                "so use it to monitor a run and to decide how to recover from a failure."
            ),
            inputSchema={"type": "object",
                         "properties": {"run_id": {"type": "string"}}, "required": []},
        ),
        types.Tool(
            name="home_robot",
            description=(
                "Home all robot axes. The robot will move. Requires confirm=true, which "
                "represents a human operator confirming that the deck and the robot's path "
                "are clear. Refuses while a run is active."
            ),
            inputSchema={
                "type": "object",
                "properties": {"confirm": {
                    "type": "boolean",
                    "description": "Must be true. Represents explicit human approval to move the robot."}},
                "required": ["confirm"],
            },
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
            description=(
                "Resume a paused protocol. The robot will move again. Requires confirm=true, "
                "which represents a human operator confirming it is safe to continue: a run "
                "is often paused because someone reached into the deck."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true. Represents explicit human approval to move the robot."},
                },
                "required": ["run_id", "confirm"],
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
            return await handle_run_status(arguments)
        elif name == "home_robot":
            return await handle_home(arguments)
        elif name == "create_protocol":
            return await handle_create_protocol(arguments)
        elif name == "upload_protocol":
            return await handle_upload_protocol(arguments)
        elif name == "start_run":
            return await handle_start_run(arguments)
        elif name == "pause_run":
            return await handle_run_action(arguments, "pause")
        elif name == "resume_run":
            return await handle_resume(arguments)
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


# What a person or an agent may safely do next, by run state. Recovery never
# means "try again automatically": a failed run leaves tips, liquid and labware
# in a state only a person at the robot can see.
NEXT_ACTIONS = {
    "idle": ["Review the protocol, then start_run with confirm=true.",
             "stop_run to discard the run."],
    "running": ["pause_run to hold the robot.", "stop_run to end the run."],
    "paused": ["Have the operator check the deck, then resume_run with confirm=true.",
               "stop_run to end the run."],
    "blocked-by-open-door": ["Close the door. Have the operator check the deck, then "
                             "resume_run with confirm=true.", "stop_run to end the run."],
    "awaiting-recovery": ["The robot stopped on an error and is waiting. Have the operator "
                          "inspect the deck before anything else.", "stop_run to end the run."],
    "stop-requested": ["Wait. The robot is stopping."],
    "finishing": ["Wait. The robot is finishing."],
    "stopped": ["Nothing will move. Have the operator check for attached tips and liquid "
                "left in a tip before home_robot with confirm=true.",
                "To run again, upload_protocol again: a stopped run cannot be restarted."],
    "failed": ["Nothing will move. Read the errors above.",
               "Have the operator inspect the deck: a tip may still be attached and may "
               "hold liquid. home_robot with confirm=true only once the deck is clear.",
               "Fix the cause, then upload_protocol again: a failed run cannot be resumed.",
               "Do not rerun on the same plate without the scientist deciding what the "
               "partial run did to the samples."],
    "succeeded": ["The run completed. No action needed."],
}


async def _active_runs(exclude: str = "") -> list:
    """Ids of runs on the robot that are moving or may move, other than `exclude`."""
    data = await ot2_get("/runs")
    return [r.get("id") for r in data.get("data", [])
            if r.get("status") in ACTIVE_RUN_STATES and r.get("id") != exclude]


def _refuse_unconfirmed(tool: str, extra: dict = None) -> list[types.TextContent]:
    return _text({
        "refused": True,
        "reason": "confirm was not true",
        "detail": (f"{tool} moves the robot. Pass confirm=true only after a human "
                   "operator has approved this specific action."),
        **(extra or {}),
    })


async def handle_run_status(arguments: dict = None) -> list[types.TextContent]:
    run_id = (arguments or {}).get("run_id")
    if run_id:
        run = (await ot2_get(f"/runs/{run_id}")).get("data", {})
    else:
        runs = (await ot2_get("/runs")).get("data", [])
        if not runs:
            return _text({"status": "no_runs", "message": "No runs found on this robot."})
        run = runs[-1]
    status = run.get("status")
    errors = [e.get("detail") or e.get("errorType") or str(e) for e in (run.get("errors") or [])]
    record = RUN_REGISTRY.get(run.get("id"), {})
    return _text({
        "run_id": run.get("id"),
        "status": status,
        "created_at": run.get("createdAt"),
        "current_step": run.get("currentOffsetId"),
        "errors": errors,
        "protocol_sha256": record.get("protocol_sha256"),
        "uploaded_by_this_server": bool(record),
        "next_actions": NEXT_ACTIONS.get(
            status, ["Unrecognised run state. Have the operator check the robot before acting."]),
    })


async def handle_home(arguments: dict = None) -> list[types.TextContent]:
    if (arguments or {}).get("confirm") is not True:
        return _refuse_unconfirmed("home_robot")
    active = await _active_runs()
    if active:
        return _text({"refused": True, "reason": "a run is active on the robot",
                      "active_runs": active,
                      "detail": "Homing during a run would collide with it. stop_run first."})
    await ot2_post("/robot/home", {"target": "robot"})
    audit_logged = _audit("", "home", {})
    return _text({"status": "homed", "message": "Robot homed successfully.",
                  "audit_logged": audit_logged})


def _validate_request(name: str, steps, deck: dict, fmt: str) -> list:
    """Everything wrong with a create_protocol request. Empty means it may be rendered."""
    problems = []
    if not NAME_RE.match(name):
        problems.append("name may contain only letters, digits, '_' and '-' (at most 64 characters)")
    if fmt not in ("opentrons", "pylabrobot"):
        problems.append(f"unknown format {fmt!r}")
    if not isinstance(steps, list) or not steps:
        return problems + ["steps must be a non-empty list"]
    if len(steps) > MAX_STEPS:
        problems.append(f"{len(steps)} steps exceeds the limit of {MAX_STEPS}")

    unknown = [k for k in deck if k not in DECK_KEYS]
    if unknown:
        problems.append(f"unknown deck entries: {sorted(unknown)}")
    for key in DECK_KEYS:
        entry = deck.get(key)
        if not isinstance(entry, dict) or not LOAD_NAME_RE.match(str(entry.get("load_name", ""))):
            problems.append(f"deck.{key} needs a load_name of lowercase letters, digits, '_' and '.'")
            continue
        if key == "pipette":
            if entry.get("mount") not in ("left", "right"):
                problems.append("deck.pipette.mount must be 'left' or 'right'")
        elif str(entry.get("slot")) not in OT2_SLOTS:
            problems.append(f"deck.{key}.slot must be an OT-2 slot, 1 to 11")

    capacity = pipette_capacity_ul((deck.get("pipette") or {}).get("load_name")) \
        if isinstance(deck.get("pipette"), dict) else None
    ref_ok = WELL_RE.match if fmt == "opentrons" else RESOURCE_RE.match
    for i, step in enumerate(steps[:MAX_STEPS], start=1):
        if not isinstance(step, dict):
            problems.append(f"step {i}: must be an object")
            continue
        kind = step.get("type", "transfer")
        if kind not in STEP_TYPES:
            problems.append(f"step {i}: unknown type {kind!r}")
            continue
        for field in ("source", "destination"):
            ref = str(step.get(field) or "").strip()
            if ref and not ref_ok(ref):
                problems.append(f"step {i}: {field} {ref[:40]!r} is not a valid reference "
                                "(expected e.g. 'source:A1')")
        label = step.get("label", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS or not label.isprintable():
            problems.append(f"step {i}: label must be one printable line of at most "
                            f"{MAX_LABEL_CHARS} characters")
        if kind in ("aspirate", "dispense", "transfer", "mix"):
            vol = step.get("volume_ul", 50)
            if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not vol > 0:
                problems.append(f"step {i}: volume_ul must be a number greater than 0")
            elif kind == "transfer" and vol > MAX_TRANSFER_UL:
                problems.append(f"step {i}: transfer of {vol} uL exceeds {MAX_TRANSFER_UL} uL")
            elif kind != "transfer" and capacity and vol > capacity:
                problems.append(f"step {i}: {kind} of {vol} uL exceeds the pipette's "
                                f"{capacity} uL capacity")
        if kind == "mix":
            cycles = step.get("mix_cycles", 3)
            if isinstance(cycles, bool) or not isinstance(cycles, int) \
                    or not 1 <= cycles <= MAX_MIX_CYCLES:
                problems.append(f"step {i}: mix_cycles must be a whole number from 1 to {MAX_MIX_CYCLES}")
    return problems


async def handle_create_protocol(args: dict) -> list[types.TextContent]:
    name = str(args.get("name", "")).strip().replace(" ", "_")
    steps = args.get("steps", [])
    description = str(args.get("description", ""))
    tip_strategy = args.get("tip_strategy", "new_tips_each_transfer")
    fmt = args.get("format", "opentrons")
    overrides = args.get("deck") or {}
    if not isinstance(overrides, dict):
        return _text({"refused": True, "reason": "the request is not valid",
                      "problems": ["deck must be an object"]})
    deck = {**DEFAULT_DECK, **overrides}

    problems = _validate_request(name, steps, deck, fmt)
    if tip_strategy not in ("new_tips_each_transfer", "reuse_tips"):
        problems.append(f"unknown tip_strategy {tip_strategy!r}")
    if problems:
        return _text({"refused": True, "reason": "the request is not valid",
                      "problems": problems, "detail": "No file was written."})

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
    # The description sits inside a docstring, so it must not be able to close it.
    description = description.replace('"', "'").replace("\\", "/")
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


def _constraint_violations(commands: list, pipettes: list = None) -> list:
    """Check analysed commands against the eval framework's OT-2 deck limits.

    This is a second opinion on top of the robot's own analysis, using the same
    constraints the eval framework applies to generated protocols. A volume is
    checked against the capacity of the pipette the command names; when the
    analysis does not say which pipette that is, against the largest OT-2 tip.
    """
    if not HAS_EVALS:
        return []
    limits = DECK_CONSTRAINTS.get("OT-2", {})
    ceiling = max(
        limits.get("max_volume_300ul_tips", 300),
        limits.get("max_volume_1000ul_tips", 1000),
    )
    capacity = {p.get("id"): pipette_capacity_ul(p.get("pipetteName"))
                for p in (pipettes or []) if isinstance(p, dict)}
    violations = []
    for command in commands:
        params = command.get("params", {}) or {}
        volume = params.get("volume")
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            continue
        kind = command.get("commandType", "command")
        own = capacity.get(params.get("pipetteId"))
        if volume < 0:
            violations.append(f"{kind} volume {volume} uL is negative")
        elif own and volume > own:
            violations.append(f"{kind} volume {volume} uL exceeds the pipette's capacity ({own} uL)")
        elif volume > ceiling:
            violations.append(
                f"{kind} volume {volume} uL "
                f"exceeds the largest OT-2 tip capacity ({ceiling} uL)"
            )
    return violations


def _analysis_problem(analysis: dict, command_count: int) -> str:
    """Why this analysis cannot be relied on, or '' when it can.

    An analysis that timed out or returned no commands has found no errors only
    because it checked nothing, so it must not count as a pass.
    """
    status = analysis.get("status")
    if status != "completed":
        return f"the robot's analysis did not complete (status: {status})"
    if analysis.get("result") not in (None, "ok"):
        return f"the robot's analysis result was {analysis.get('result')!r}"
    if command_count == 0:
        return "the robot's analysis produced no commands, so nothing was checked"
    return ""


async def handle_upload_protocol(arguments: dict) -> list[types.TextContent]:
    path = str(arguments.get("path", ""))
    if not os.path.isfile(path):
        return _text({"error": f"No such protocol file: {path}"})
    if not path.endswith(".py"):
        return _text({"error": "Only Opentrons Python protocol files (.py) can be uploaded"})
    if os.path.getsize(path) > MAX_PROTOCOL_BYTES:
        return _text({"error": f"Protocol file is larger than {MAX_PROTOCOL_BYTES} bytes"})

    with open(path, "rb") as handle:
        content = handle.read()
    protocol_sha256 = hashlib.sha256(content).hexdigest()
    files = {"files": (os.path.basename(path), content, "text/x-python")}
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
    violations = _constraint_violations(commands, analysis.get("pipettes"))
    analysis_problem = _analysis_problem(analysis, len(commands))

    run = await ot2_post("/runs", {"data": {"protocolId": protocol_id}})
    run_id = run.get("data", {}).get("id")

    RUN_REGISTRY[run_id] = {
        "protocol_id": protocol_id,
        "path": path,
        "protocol_sha256": protocol_sha256,
        "analysis_problem": analysis_problem,
        "analysis_status": analysis.get("status"),
        "analysis_errors": analysis_errors,
        "constraint_violations": violations,
        "command_count": len(commands),
    }

    blocked = (bool(analysis_errors) or bool(violations) or bool(analysis_problem)
               or not HAS_EVALS)
    return _text({
        "run_id": run_id,
        "protocol_id": protocol_id,
        "protocol_sha256": protocol_sha256,
        "analysis_status": analysis.get("status"),
        "analysis_problem": analysis_problem or None,
        "constraint_checks_available": HAS_EVALS,
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
        return _refuse_unconfirmed("start_run", {"run_id": run_id})

    # The checks below depend on what upload_protocol recorded. A run this server
    # never analysed (created elsewhere, or before a restart) would skip them, so
    # it is refused: upload it again through upload_protocol first.
    if run_id not in RUN_REGISTRY:
        return _text({
            "refused": True,
            "reason": "this run was not uploaded through upload_protocol in this session",
            "detail": (
                "start_run only starts runs whose analysis and deck-constraint "
                "checks this server has seen. Call upload_protocol again, review "
                "the result, then start_run."
            ),
            "run_id": run_id,
        })

    record = RUN_REGISTRY[run_id]
    if not HAS_EVALS:
        return _text({"refused": True, "reason": "deck constraint checks are unavailable",
                      "detail": "The eval framework could not be imported, so this protocol "
                                "was never checked against deck constraints.",
                      "run_id": run_id})
    if record.get("analysis_problem"):
        return _text({"refused": True, "reason": record["analysis_problem"],
                      "detail": "An analysis that did not finish found no errors only because "
                                "it checked nothing. Call upload_protocol again.",
                      "run_id": run_id})
    if record.get("analysis_errors"):
        return _text({"refused": True, "reason": "the robot's analysis reported errors",
                      "analysis_errors": record["analysis_errors"], "run_id": run_id})
    if record.get("constraint_violations"):
        return _text({"refused": True, "reason": "protocol failed deck constraint checks",
                      "constraint_violations": record["constraint_violations"], "run_id": run_id})

    active = await _active_runs(exclude=run_id)
    if active:
        return _text({"refused": True, "reason": "another run is active on the robot",
                      "active_runs": active, "run_id": run_id,
                      "detail": "Stop or finish the active run first."})

    result = await ot2_post(f"/runs/{run_id}/actions", {"data": {"actionType": "play"}})
    audit_logged = _audit(run_id, "start", record)
    return _text({
        "run_id": run_id,
        "protocol_sha256": record.get("protocol_sha256"),
        "action": "play",
        "status": result.get("data", {}).get("actionType", "play"),
        "warning": "The robot is now executing. Use stop_run to halt it.",
        "audit_logged": audit_logged,
    })


async def handle_resume(arguments: dict) -> list[types.TextContent]:
    if arguments.get("confirm") is not True:
        return _refuse_unconfirmed("resume_run", {"run_id": arguments.get("run_id")})
    return await handle_run_action(arguments, "play")


async def handle_run_action(arguments: dict, action: str) -> list[types.TextContent]:
    run_id = arguments["run_id"]
    result = await ot2_post(f"/runs/{run_id}/actions", {"data": {"actionType": action}})
    audit_logged = _audit(run_id, action, RUN_REGISTRY.get(run_id, {}))
    return _text({"run_id": run_id, "action": action,
                  "result": result.get("data", {}), "audit_logged": audit_logged})


def _audit(run_id: str, action: str, record: dict) -> bool:
    """Append an audit record and say whether it was written. A logging failure
    never breaks a robot action — in particular, stop_run must always reach the
    robot — but it is reported, so a missing record is not mistaken for a logged one."""
    if not HAS_EVALS:
        return False
    try:
        logger = RunLogger(operator=OPERATOR, instrument="OT-2",
                           protocol_name=record.get("path", ""))
        logger.log.protocol_hash = record.get("protocol_sha256", "")
        logger.log_agent_message("system", f"{action} run {run_id}".strip())
        logger.log_run_complete(status=action)
        logger.save()
        return True
    except Exception:
        return False


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
