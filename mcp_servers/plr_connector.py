"""
OpenLabAI's own PyLabRobot connector (library, no MCP).

PyLabRobot (https://github.com/PyLabRobot/pylabrobot) is a hardware-agnostic
Python library for liquid handlers. A PyLabRobot "backend" is the object that
turns a liquid handler's operations (pick up tips, aspirate, dispense, drop tips)
into commands for one machine. This module adds three things on top of it:

1. TraceBackend: a backend of our own that records every command a protocol
   would send, instead of sending it anywhere. It is built on PyLabRobot's
   SerializingBackend.

2. A plan that is simulated before it can run. A plan is a deck (labware in
   slots, with starting volumes) and a list of transfers. simulate() runs the plan
   on TraceBackend with PyLabRobot's tip and volume tracking switched on, so a
   plan that aspirates liquid that is not there, reuses a tip spot, or exceeds a
   tip's capacity is rejected before any robot is involved. It also reports
   carry-over risks and the final volume in every well it touched.

3. Approval bound to the exact commands. The approval token is a hash of the
   plan AND of the command stream it generates. execute_plan() runs the plan
   again, and refuses unless the token still matches, so what a person approved
   is exactly what runs. A plan or PyLabRobot version that would now generate
   different commands needs a fresh approval.

The token ties an approval to one exact plan. It is not a secret and it does not
replace a person's decision: execution still needs confirm=true from a human, as in
the other OpenLabAI servers.

Nothing in this module talks to hardware unless a live backend factory is passed
to execute_plan(). mcp_servers/plr_server.py builds that factory for an OT-2.
"""

import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

import pylabrobot.resources as _R
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends.serializing_backend import SerializingBackend
from pylabrobot.resources import OTDeck, set_tip_tracking, set_volume_tracking

CONNECTOR_VERSION = 1
HARDWARE_LOG_DIR = Path(__file__).resolve().parent.parent / "hardware_logs"

# Labware a plan may name. An explicit list, not getattr on user input, so a plan
# can never reach any other PyLabRobot function.
_ALLOWED = {
    "tiprack": ["opentrons_96_tiprack_20ul", "opentrons_96_tiprack_300ul", "opentrons_96_tiprack_1000ul"],
    "plate": ["cor_96_wellplate_360uL_Fb", "cor_96_wellplate_2mL_Vb", "NEST_96_wellplate_2200uL_Ub"],
    "trough": ["nest_12_troughplate_15000uL_Vb", "nest_8_troughplate_22000uL_Vb", "nest_1_troughplate_195000uL_Vb"],
}
REGISTRY = {name: (kind, getattr(_R, name))
            for kind, names in _ALLOWED.items() for name in names if hasattr(_R, name)}

WELL_REF = re.compile(r"^([A-Za-z0-9_\-]{1,40}):([A-H](?:[1-9]|1[0-2]))$")
OT2_SLOTS = range(1, 12)
MAX_STEPS = 500


class PlanError(Exception):
    """The plan is malformed or names something the connector does not allow."""


class TraceBackend(SerializingBackend):
    """A PyLabRobot backend that records every command instead of sending it."""

    def __init__(self, num_channels: int = 1):
        super().__init__(num_channels=num_channels)
        self.trace = []

    async def send_command(self, command, data=None):
        self.trace.append({"command": command, "data": data})
        return None


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def plr_version() -> str:
    try:
        return importlib.metadata.version("pylabrobot")
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- plan

def validate_plan(spec: dict, steps: list) -> None:
    """Raise PlanError on anything malformed. Runs before any PyLabRobot object exists."""
    if not isinstance(spec, dict) or not isinstance(spec.get("labware"), list) or not spec["labware"]:
        raise PlanError("deck.labware must be a non-empty list")
    names, slots = set(), set()
    for item in spec["labware"]:
        if not isinstance(item, dict):
            raise PlanError("each labware entry must be an object")
        name, kind = item.get("name"), item.get("type")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name):
            raise PlanError(f"labware name {name!r} must be 1-40 letters, digits, - or _")
        if name in names:
            raise PlanError(f"two labware items are named {name!r}")
        names.add(name)
        if kind not in REGISTRY:
            raise PlanError(f"labware type {kind!r} is not allowed; choose one of {sorted(REGISTRY)}")
        slot = item.get("slot")
        if slot not in OT2_SLOTS:
            raise PlanError(f"slot {slot!r} is not an OT-2 slot (1 to 11)")
        if slot in slots:
            raise PlanError(f"slot {slot} is used twice")
        slots.add(slot)
        for well, volume in (item.get("volumes_ul") or {}).items():
            if not re.fullmatch(r"[A-H](?:[1-9]|1[0-2])", str(well)) or not isinstance(volume, (int, float)) or volume < 0:
                raise PlanError(f"starting volume {well!r}: {volume!r} is not a well and a non-negative number")
    if not any(REGISTRY[i["type"]][0] == "tiprack" for i in spec["labware"]):
        raise PlanError("the deck needs at least one tip rack")
    if not isinstance(steps, list) or not steps:
        raise PlanError("steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise PlanError(f"at most {MAX_STEPS} steps per plan")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op", "transfer") != "transfer":
            raise PlanError(f"step {i}: only op 'transfer' is supported")
        for key in ("source", "destination"):
            match = WELL_REF.match(str(step.get(key, "")))
            if not match or match.group(1) not in names:
                raise PlanError(f"step {i}: {key} {step.get(key)!r} must look like 'plate:A1' and name a labware item")
            if REGISTRY[next(l["type"] for l in spec["labware"] if l["name"] == match.group(1))][0] == "tiprack":
                raise PlanError(f"step {i}: {key} cannot be a tip rack")
        volume = step.get("volume_ul")
        if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not 0 < volume <= 5000:
            raise PlanError(f"step {i}: volume_ul must be a number above 0")
        if "new_tip" in step and not isinstance(step["new_tip"], bool):
            raise PlanError(f"step {i}: new_tip must be true or false")


class Labware(dict):
    """name -> PyLabRobot resource, plus what kind each one is (tiprack, plate, trough)."""

    def __init__(self):
        super().__init__()
        self.kinds = {}


def build_deck(spec: dict, backend):
    """Make a fresh LiquidHandler on an OT-2 deck. Tracking is switched on first,
    because PyLabRobot reads those flags when a resource is created."""
    set_tip_tracking(True)
    set_volume_tracking(True)
    deck = OTDeck()
    labware = Labware()
    for item in spec["labware"]:
        kind, factory = REGISTRY[item["type"]]
        resource = factory(name=item["name"])
        deck.assign_child_at_slot(resource, item["slot"])
        for well, volume in (item.get("volumes_ul") or {}).items():
            resource[well][0].tracker.set_volume(volume)
        labware[item["name"]] = resource
        labware.kinds[item["name"]] = kind
    return LiquidHandler(backend=backend, deck=deck), labware


def _next_tip(labware):
    """The first tip that is still on a rack, in the order racks were listed."""
    for name, resource in labware.items():
        if labware.kinds[name] != "tiprack":
            continue
        for col in range(1, 13):
            for row in "ABCDEFGH":
                spot = resource[f"{row}{col}"][0]
                if spot.tracker.has_tip:
                    return f"{name}:{row}{col}", spot
    return None, None


def _ref(labware, ref):
    plate, well = ref.split(":")
    return labware[plate][well][0]


# ------------------------------------------------------------------------ running

async def run_steps(lh, labware, steps):
    """Execute a plan on a set-up LiquidHandler. Returns what the plan did, for the
    report. Raises whatever PyLabRobot raises, tagged with the step index."""
    sessions, held = [], None
    for i, step in enumerate(steps):
        try:
            new_tip = step.get("new_tip", True)
            if held is not None and new_tip:
                await lh.discard_tips()
                held = None
            if held is None:
                label, spot = _next_tip(labware)
                if spot is None:
                    raise RuntimeError("NoTipAvailable: every tip in every rack is used")
                await lh.pick_up_tips([spot])
                held = {"spot": label, "sources": [], "destinations": [], "steps": []}
                sessions.append(held)
            tip = lh.head[0].get_tip()
            if step["volume_ul"] > tip.maximal_volume:
                raise RuntimeError(f"VolumeExceedsTip: {step['volume_ul']} uL is more than the "
                                   f"{tip.maximal_volume} uL tip")
            await lh.aspirate([_ref(labware, step["source"])], vols=[step["volume_ul"]])
            await lh.dispense([_ref(labware, step["destination"])], vols=[step["volume_ul"]])
            if step["source"] not in held["sources"]:
                held["sources"].append(step["source"])
            if step["destination"] not in held["destinations"]:
                held["destinations"].append(step["destination"])
            held["steps"].append(i)
        except Exception as exc:
            exc.step_index = i
            raise
    if held is not None:
        await lh.discard_tips()
    return sessions


def _touched(steps):
    return sorted({s[k] for s in steps for k in ("source", "destination")})


def _final_volumes(labware, steps):
    return {ref: round(_ref(labware, ref).tracker.get_used_volume(), 3) for ref in _touched(steps)}


async def simulate(spec: dict, steps: list) -> dict:
    """Run the plan on TraceBackend and return a report. Never touches hardware."""
    validate_plan(spec, steps)
    backend = TraceBackend(num_channels=1)
    lh, labware = build_deck(spec, backend)
    await lh.setup()
    errors, sessions = [], []
    try:
        sessions = await run_steps(lh, labware, steps)
    except Exception as exc:
        errors.append({"step": getattr(exc, "step_index", None), "type": type(exc).__name__,
                       "message": str(exc)[:300]})
    await lh.stop()

    warnings = []
    for s in sessions:
        if len(s["sources"]) > 1:
            warnings.append(f"tip {s['spot']} aspirated from {len(s['sources'])} different wells "
                            f"({', '.join(s['sources'])}) without being replaced: carry-over risk")
    final = {} if errors else _final_volumes(labware, steps)
    plan_hash = sha(canonical({"deck": spec, "steps": steps, "pylabrobot": plr_version(),
                               "connector": CONNECTOR_VERSION}))
    trace_hash = sha(canonical(backend.trace))
    valid = not errors
    return {
        "valid": valid, "errors": errors, "warnings": warnings,
        "summary": {"steps": len(steps), "tips_used": len(sessions), "commands": len(backend.trace),
                    "total_volume_moved_ul": round(sum(s["volume_ul"] for s in steps), 3)},
        "tip_sessions": sessions, "final_volumes_ul": final,
        "plan_hash": plan_hash, "trace_hash": trace_hash,
        "approval_token": sha(plan_hash + ":" + trace_hash)[:16] if valid else None,
        "pylabrobot_version": plr_version(), "trace": backend.trace,
    }


async def execute_plan(spec: dict, steps: list, approval_token: str, make_live) -> dict:
    """Re-simulate, check the approval token, then run the plan on a live backend.

    make_live is an async function (spec) -> (LiquidHandler, labware) that returns
    a handler that has already been set up. It is the only place hardware can come in.
    """
    report = await simulate(spec, steps)
    if not report["valid"]:
        return {"refused": True, "reason": "the plan no longer simulates cleanly", "errors": report["errors"]}
    if approval_token != report["approval_token"]:
        return {"refused": True, "reason": "approval token does not match the plan as it stands now",
                "detail": "The plan, or the commands PyLabRobot generates for it, changed after it was approved. "
                          "Plan it again and approve the new card."}
    lh, labware = await make_live(spec)
    error = None
    try:
        await run_steps(lh, labware, steps)
    except Exception as exc:
        error = {"step": getattr(exc, "step_index", None), "type": type(exc).__name__, "message": str(exc)[:300]}
    finally:
        try:
            await lh.stop()
        except Exception:
            pass
    if error:
        return {"executed": False, "error": error,
                "warning": "The run stopped part-way. The robot's state is unknown: check it before doing anything else."}
    final = _final_volumes(labware, steps)
    return {"executed": True, "final_volumes_ul": final,
            "matches_simulation": final == report["final_volumes_ul"]}


# ----------------------------------------------------------------------- evidence

def evidence_level(backend_kind: str, log_dir: Path = None) -> dict:
    """What has this backend actually been shown to do? Computed from committed
    hardware logs, never from prose. A log counts only if it came from this
    connector, names this backend, was not simulated, and has verdict 'verified'."""
    if backend_kind == "simulation":
        return {"backend": backend_kind, "level": "simulation only", "logs": []}
    log_dir = Path(log_dir or HARDWARE_LOG_DIR)
    good = []
    for path in sorted(log_dir.glob("*.json")) if log_dir.exists() else []:
        try:
            log = json.loads(path.read_text())
        except Exception:
            continue
        if (log.get("connector") == "plr_server" and log.get("backend") == backend_kind
                and log.get("simulated") is False and log.get("verdict") == "verified"):
            good.append(path.name)
    return {"backend": backend_kind,
            "level": "hardware-verified" if good else "unverified: no committed hardware log for this connector",
            "logs": good}
