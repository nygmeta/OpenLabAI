"""
Bridge: a Lab Agent workflow -> an OpenLabAI plan.

Lab Agent (https://github.com/nygmeta/Claude_Life_sciences) turns a spoken request into a
validated, robot-agnostic Workflow: labware plus a list of operations. This module takes that
Workflow as plain JSON, so it needs no Lab Agent code, and converts it into the deck and
transfers that OpenLabAI's PyLabRobot connector (plr_connector.py) can simulate, check, and
bind a person's approval to.

What it does NOT do is guess. The connector performs plate-to-plate transfers on an OT-2 deck.
Anything else in a workflow (incubations, washes, waits, labware moves, manual steps, mixing
after a transfer, volumes outside the pipette's range, labware it does not know) is refused,
by name, with the reason. A refused workflow produces no plan at all, so nothing can run
partly. For a refused workflow the result says how many transfers come before the first
unsupported operation, so a person can see what a later "run the first part" feature could
cover.

Starting volumes. A Lab Agent workflow does not say how much liquid is in each well. The bridge
assumes each source well holds what the plan takes from it, plus a small dead volume, and lists
those assumptions so they can be read aloud and confirmed. Pass loaded_volumes_ul to replace an
assumption with a real number; a plan that takes more than a given well holds is refused.
"""

import re
from collections import defaultdict

BRIDGE_VERSION = 1

# Lab Agent's neutral labware tokens -> labware OpenLabAI's connector allows.
LABWARE_MAP = {
    "corning_96_wellplate_360ul": "cor_96_wellplate_360uL_Fb",
    "nest_12_reservoir_15ml": "nest_12_troughplate_15000uL_Vb",
    "opentrons_96_tiprack_300ul": "opentrons_96_tiprack_300ul",
}
TIP_TOKENS = {"opentrons_96_tiprack_300ul"}
TIP_SLOTS = [7, 8, 11]                        # the OT-2 positions that hold tip racks
OTHER_SLOTS = [1, 2, 3, 4, 5, 6, 9, 10]
MIN_UL, MAX_UL = 1.0, 300.0                   # the limits Lab Agent's own Opentrons adapter declares for a p300
DEAD_UL = 5.0
UNITS = {"nl": 1e-3, "ul": 1.0, "µl": 1.0, "μl": 1.0, "ml": 1e3}
ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
WELL = re.compile(r"^[A-H](?:[1-9]|1[0-2])$")
WHY = {
    "incubate": "a timed temperature hold is not something this connector performs",
    "wait": "a timed pause is not something this connector performs",
    "wash": "a plate-wash cycle is not something this connector performs",
    "move_labware": "moving labware is not something this connector performs",
    "manual_step": "this step is for a person to do",
    "mix": "mixing in place is not supported yet",
}


def to_ul(volume):
    """{'value': 100, 'unit': 'uL'} -> microlitres, or None if it is not a usable volume."""
    if not isinstance(volume, dict):
        return None
    unit = str(volume.get("unit", "uL")).strip().lower()
    value = volume.get("value")
    if unit not in UNITS or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) * UNITS[unit]


def _ref(ref):
    if not isinstance(ref, dict):
        return None
    lab, well = ref.get("labware"), ref.get("well")
    return f"{lab}:{well}" if isinstance(lab, str) and isinstance(well, str) else None


def convert(workflow, loaded_volumes_ul=None) -> dict:
    """Convert a Lab Agent Workflow (a dict) into {'deck', 'steps'} for plr_connector, or refuse it.

    Returns {'ok': True, 'deck', 'steps', 'assumptions', 'notes'} or
            {'ok': False, 'refusals': [{'where', 'reason'}], 'transfers_before_first_unsupported': n}.
    """
    refusals, notes, assumptions = [], [], []

    def refuse(where, reason):
        refusals.append({"where": where, "reason": reason})

    if not isinstance(workflow, dict) or not isinstance(workflow.get("labware"), list) \
            or not isinstance(workflow.get("operations"), list) or not workflow["operations"]:
        return {"ok": False, "refusals": [{"where": "workflow", "reason": "expected an object with a labware list and a non-empty operations list"}],
                "transfers_before_first_unsupported": 0}
    overrides = loaded_volumes_ul if isinstance(loaded_volumes_ul, dict) else {}

    # ---- labware
    labware, kinds = [], {}
    for item in workflow["labware"]:
        lid = item.get("id") if isinstance(item, dict) else None
        token = item.get("type") if isinstance(item, dict) else None
        if not isinstance(lid, str) or not ID.match(lid):
            refuse("labware", f"labware id {lid!r} must be 1-40 letters, digits, - or _")
            continue
        if token not in LABWARE_MAP:
            refuse(f"labware {lid}", f"labware type {token!r} is not one this connector knows; known types: {sorted(LABWARE_MAP)}")
            continue
        if lid in kinds:
            refuse(f"labware {lid}", "two labware items share this id")
            continue
        kinds[lid] = "tiprack" if token in TIP_TOKENS else "other"
        labware.append({"id": lid, "type": token, "slot": item.get("slot")})

    # ---- operations
    steps, first_unsupported = [], None
    run, start = defaultdict(float), defaultdict(float)
    for i, op in enumerate(workflow["operations"]):
        kind = op.get("op") if isinstance(op, dict) else None
        where = f"operation {i + 1} ({kind})"
        if kind != "transfer":
            if first_unsupported is None:
                first_unsupported = i
            refuse(where, WHY.get(kind, "unknown operation"))
            continue
        src, dst, volume = _ref(op.get("source")), _ref(op.get("dest")), to_ul(op.get("volume"))
        if src is None or dst is None:
            refuse(where, "a transfer needs a source and a destination, each with a labware id and a well")
            continue
        bad = [r for r in (src, dst) if r.split(":")[0] not in kinds or kinds[r.split(":")[0]] == "tiprack" or not WELL.match(r.split(":")[1])]
        if bad:
            refuse(where, f"{', '.join(bad)} is not a well on a plate or reservoir in this workflow")
            continue
        if volume is None:
            refuse(where, "the volume is missing or not a number with a unit (nL, uL or mL)")
            continue
        if not MIN_UL <= volume <= MAX_UL:
            refuse(where, f"{volume:g} uL is outside what this pipette can do ({MIN_UL:g} to {MAX_UL:g} uL)")
            continue
        if op.get("mix_after"):
            refuse(where, "mixing after a transfer is not supported yet")
            continue
        new_tip = op.get("new_tip", True)
        if not isinstance(new_tip, bool):
            refuse(where, "new_tip must be true or false")
            continue
        steps.append({"source": src, "destination": dst, "volume_ul": round(volume, 6), "new_tip": new_tip})
        if src not in run and src in overrides:
            run[src] = float(overrides[src])
        if run[src] < volume:
            if src in overrides:
                refuse(where, f"{src} is loaded with {overrides[src]:g} uL but the plan takes more than that by this step")
                continue
            start[src] += volume - run[src]
            run[src] = volume
        run[src] -= volume
        run[dst] += volume

    if refusals:
        return {"ok": False, "refusals": refusals,
                "transfers_before_first_unsupported": len(steps) if first_unsupported is None else first_unsupported}

    # ---- slots
    used = {int(x["slot"]) for x in labware if str(x.get("slot")).isdigit() and 1 <= int(x["slot"]) <= 11}
    pools = {"tiprack": [s for s in TIP_SLOTS], "other": [s for s in OTHER_SLOTS]}
    taken, deck_items = set(), []
    for item in labware:
        slot = int(item["slot"]) if str(item.get("slot")).isdigit() and 1 <= int(item["slot"]) <= 11 and int(item["slot"]) not in taken else None
        if slot is None:
            free = [s for s in pools[kinds[item["id"]]] if s not in taken and s not in used]
            if not free:
                return {"ok": False, "refusals": [{"where": f"labware {item['id']}", "reason": "there is no free OT-2 slot for it"}], "transfers_before_first_unsupported": len(steps)}
            slot = free[0]
            notes.append(f"{item['id']} was placed in slot {slot}")
        taken.add(slot)
        deck_items.append({"name": item["id"], "type": LABWARE_MAP[item["type"]], "slot": slot})
    if not any(kinds[x["id"]] == "tiprack" for x in labware):
        free = [s for s in TIP_SLOTS if s not in taken]
        if not free:
            return {"ok": False, "refusals": [{"where": "deck", "reason": "no tip rack in the workflow and no free tip-rack slot to add one"}], "transfers_before_first_unsupported": len(steps)}
        deck_items.append({"name": "openlabai_tips", "type": "opentrons_96_tiprack_300ul", "slot": free[0]})
        assumptions.append(f"The workflow has no tip rack, so a 300 uL rack was added in slot {free[0]}.")

    # ---- starting volumes, stated as assumptions the person can confirm
    volumes = defaultdict(dict)
    stated = []
    for ref, need in sorted(start.items()):
        lab, well = ref.split(":")
        volumes[lab][well] = round(need + DEAD_UL, 3)
        stated.append(f"{ref} {need + DEAD_UL:g} uL")
    for ref, vol in overrides.items():
        if isinstance(ref, str) and ":" in ref and ref.split(":")[0] in kinds and isinstance(vol, (int, float)) and not isinstance(vol, bool):
            volumes[ref.split(":")[0]][ref.split(":")[1]] = float(vol)
    if stated:
        shown = ", ".join(stated[:6]) + (f" and {len(stated) - 6} more" if len(stated) > 6 else "")
        assumptions.append(f"Starting volumes were assumed to be what the plan takes plus {DEAD_UL:g} uL dead volume: {shown}. Confirm the wells hold at least this.")
    for item in deck_items:
        if item["name"] in volumes:
            item["volumes_ul"] = dict(volumes[item["name"]])

    return {"ok": True, "deck": {"labware": deck_items}, "steps": steps, "assumptions": assumptions, "notes": notes,
            "workflow_id": workflow.get("workflow_id")}


def readback_refused(conversion) -> str:
    """What a voice layer can say when the workflow cannot go through the connector."""
    reasons = conversion["refusals"]
    first = "; ".join(f"{r['where']}: {r['reason']}" for r in reasons[:3])
    more = f" and {len(reasons) - 3} more problems" if len(reasons) > 3 else ""
    n = conversion.get("transfers_before_first_unsupported", 0)
    hint = f" {n} transfers come before the first step this connector cannot do." if n else ""
    return f"I cannot run this on the OpenLabAI connector. {first}{more}.{hint} Nothing has been planned and nothing will move."


def readback(card, conversion) -> str:
    """A short plan summary for text-to-speech, ending with the ask for a person's approval."""
    if not card["valid"]:
        err = card["errors"][0]
        where = f"step {err['step'] + 1}" if err.get("step") is not None else "the setup"
        return f"The plan check failed at {where}: {err['message']} Nothing will move."
    s = card["summary"]
    plural = lambda n, word: f"{n} {word}" + ("" if n == 1 else "s")
    parts = [f"The plan checks out: {plural(s['steps'], 'transfer')}, {s['total_volume_moved_ul']:g} microliters in total, using {plural(s['tips_used'], 'tip')}."]
    if card["warnings"]:
        parts.append(f"{len(card['warnings'])} warning{'s' if len(card['warnings']) > 1 else ''}: {card['warnings'][0]}")
    if conversion["assumptions"]:
        parts.append("I assumed the source wells hold at least what the plan takes; please confirm they are loaded.")
    parts.append("Say confirm to run it, or cancel.")
    return " ".join(parts)
