"""
OpenLabAI: PyLabRobot MCP Server
OpenLabAI's own connector, built on PyLabRobot (https://github.com/PyLabRobot/pylabrobot).
Plans are simulated before they can run, and a person approves the exact commands
that will be sent. See mcp_servers/plr_connector.py for how that works.

Usage:
    python mcp_servers/plr_server.py                                  # simulation, no hardware
    python mcp_servers/plr_server.py --backend ot2 --host 169.254.x.x # a physical OT-2

Tools:
    describe_connector()  - Backend, PyLabRobot version, allowed labware, and what
                            has actually been shown to work (computed from committed
                            hardware logs).
    plan_protocol()       - Check a deck and a list of transfers by simulating them on
                            PyLabRobot with tip and volume tracking. Returns a plan
                            card: errors, carry-over warnings, final volumes, and an
                            approval token that covers the exact commands. Moves nothing.
    get_plan()            - Read a plan card again, optionally with the command trace.
    execute_plan()        - Run an approved plan. Requires confirm=true (a person's
                            approval) AND the approval token from the plan card. The plan
                            is simulated again first; a changed plan or changed commands
                            need a new approval. In simulation mode nothing physical happens.

A live backend (--backend ot2) is refused at start-up unless a committed hardware
log shows this connector working on that backend, or the operator passes
--accept-unverified to say they are running it unverified, supervised.
"""

import os
import sys
import json
import uuid
import asyncio
import argparse
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plr_connector as plr                       # noqa: E402

try:
    from evals.run_logger import RunLogger        # noqa: E402
    HAS_EVALS = True
except Exception:                                 # pragma: no cover
    HAS_EVALS = False

parser = argparse.ArgumentParser()
parser.add_argument("--backend", choices=["simulation", "ot2"], default="simulation")
parser.add_argument("--host", default="169.254.10.10", help="OT-2 IP address (ot2 backend)")
parser.add_argument("--port", type=int, default=31950)
parser.add_argument("--operator", default=os.environ.get("OPENLAB_OPERATOR", "unknown"),
                    help="Operator name recorded in the audit log")
parser.add_argument("--accept-unverified", action="store_true",
                    help="allow a live backend that has no committed hardware log")
args, _ = parser.parse_known_args()

CONFIG = {"backend": args.backend, "host": args.host, "port": args.port,
          "operator": args.operator, "accept_unverified": args.accept_unverified}
PLANS: dict = {}                                  # plan_id -> {"spec", "steps", "card"}

app = Server("openlabai-plr")


def _text(payload) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def backend_allowed() -> tuple[bool, dict]:
    """May this server drive its backend? Simulation always; a live one only with evidence or an explicit flag."""
    evidence = plr.evidence_level(CONFIG["backend"])
    if CONFIG["backend"] == "simulation" or evidence["level"] == "hardware-verified":
        return True, evidence
    return bool(CONFIG["accept_unverified"]), evidence


async def make_live(spec: dict):
    """The only place a live backend is created."""
    if CONFIG["backend"] == "ot2":
        from pylabrobot.liquid_handling.backends import OpentronsOT2Backend
        backend = OpentronsOT2Backend(host=CONFIG["host"], port=CONFIG["port"])
    else:
        backend = plr.TraceBackend(num_channels=1)    # simulation: nothing leaves this process
    lh, labware = plr.build_deck(spec, backend)
    await lh.setup()
    return lh, labware


def _audit(plan_id: str, action: str, plan: dict) -> bool:
    """Append an audit record and say whether it was written. A logging failure never
    changes what happened, but it is reported."""
    if not HAS_EVALS:
        return False
    try:
        mode = "OT-2 via PyLabRobot" if CONFIG["backend"] == "ot2" else "PyLabRobot simulation"
        logger = RunLogger(operator=CONFIG["operator"], instrument=mode, protocol_name=plan_id)
        logger.log_agent_message("system", f"{action} plan {plan_id}, token {plan['card']['approval_token']}")
        logger.log_run_complete(status="failed" if action == "failed" else "completed")
        logger.save()
        return True
    except Exception:
        return False


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="describe_connector",
            description=("Describe this PyLabRobot connector: backend, versions, the labware a plan may use, "
                         "and what has been shown to work on hardware (computed from committed logs)."),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="plan_protocol",
            description=(
                "Check a plan by simulating it on PyLabRobot with tip and volume tracking. A plan is a deck "
                "(OT-2 slots 1-11, labware with optional starting volumes) and a list of transfers. Returns a "
                "plan card with errors, carry-over warnings, final volumes and an approval token. Moves nothing."),
            inputSchema={
                "type": "object",
                "properties": {
                    "deck": {"type": "object", "description":
                             "{labware: [{name, type, slot, volumes_ul?: {A1: 100}}]}. Types: see describe_connector."},
                    "steps": {"type": "array", "items": {"type": "object", "properties": {
                        "op": {"type": "string", "enum": ["transfer"]},
                        "source": {"type": "string", "description": "labware:well, e.g. src:A1"},
                        "destination": {"type": "string"},
                        "volume_ul": {"type": "number"},
                        "new_tip": {"type": "boolean", "default": True}},
                        "required": ["source", "destination", "volume_ul"]}},
                },
                "required": ["deck", "steps"],
            },
        ),
        types.Tool(
            name="get_plan",
            description="Read a plan card again by plan_id, optionally with the full command trace.",
            inputSchema={"type": "object", "properties": {"plan_id": {"type": "string"},
                                                          "include_trace": {"type": "boolean", "default": False}},
                         "required": ["plan_id"]},
        ),
        types.Tool(
            name="execute_plan",
            description=(
                "Run an approved plan. With the ot2 backend THE ROBOT WILL MOVE. Requires confirm=true, which "
                "represents a person's approval of this specific plan card, and the approval_token printed on that "
                "card. Do not pass confirm unless the person you are working with has reviewed the card and approved. "
                "The plan is simulated again first; if the plan or the generated commands changed, it is refused."),
            inputSchema={"type": "object", "properties": {
                "plan_id": {"type": "string"},
                "approval_token": {"type": "string", "description": "From the plan card the person approved."},
                "confirm": {"type": "boolean", "description": "Must be true. Represents explicit human approval."}},
                "required": ["plan_id", "approval_token", "confirm"]},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "describe_connector":
            return await handle_describe()
        if name == "plan_protocol":
            return await handle_plan(arguments)
        if name == "get_plan":
            return await handle_get_plan(arguments)
        if name == "execute_plan":
            return await handle_execute(arguments)
        return _text({"error": f"Unknown tool: {name}"})
    except plr.PlanError as exc:
        return _text({"valid": False, "plan_error": str(exc)})
    except Exception as exc:
        return _text({"error": f"{type(exc).__name__}: {exc}"})


async def handle_describe():
    allowed, evidence = backend_allowed()
    return _text({
        "connector": "plr_server", "connector_version": plr.CONNECTOR_VERSION,
        "pylabrobot_version": plr.plr_version(), "backend": CONFIG["backend"],
        "backend_allowed": allowed, "evidence": evidence,
        "scope": "OT-2 deck, one channel, transfer steps only",
        "labware_types": sorted(plr.REGISTRY),
        "note": ("Evidence comes from committed logs in hardware_logs/, not from this text. "
                 "Simulation results say nothing about a physical robot."),
    })


async def handle_plan(arguments: dict):
    spec, steps = arguments["deck"], arguments["steps"]
    report = await plr.simulate(spec, steps)
    plan_id = "plan-" + uuid.uuid4().hex[:8]
    _, evidence = backend_allowed()
    card = {k: v for k, v in report.items() if k != "trace"}
    card.update({"plan_id": plan_id, "backend": CONFIG["backend"], "evidence": evidence,
                 "state": "planned only. nothing has moved",
                 "next_steps": ("Fix the errors and plan again." if not report["valid"] else
                                "Have a person review this card, then call execute_plan with the plan_id, "
                                "this approval_token and confirm=true.")})
    PLANS[plan_id] = {"spec": spec, "steps": steps, "card": card, "trace": report["trace"]}
    return _text(card)


async def handle_get_plan(arguments: dict):
    plan = PLANS.get(arguments["plan_id"])
    if not plan:
        return _text({"error": f"no such plan: {arguments['plan_id']}"})
    out = dict(plan["card"])
    if arguments.get("include_trace"):
        out["trace"] = plan["trace"]
    return _text(out)


async def handle_execute(arguments: dict):
    plan_id, token = arguments["plan_id"], arguments["approval_token"]
    if arguments.get("confirm") is not True:
        return _text({"refused": True, "reason": "confirm was not true",
                      "detail": ("execute_plan can move a robot. Pass confirm=true only after a person has "
                                 "reviewed this specific plan card and approved it."), "plan_id": plan_id})
    plan = PLANS.get(plan_id)
    if not plan:
        return _text({"refused": True, "reason": "unknown plan_id: plan it with plan_protocol first", "plan_id": plan_id})
    if not plan["card"]["valid"]:
        return _text({"refused": True, "reason": "the plan did not simulate cleanly", "errors": plan["card"]["errors"]})
    allowed, evidence = backend_allowed()
    if not allowed:
        return _text({"refused": True, "reason": "this live backend has no committed hardware log and the server "
                                                  "was not started with --accept-unverified", "evidence": evidence})
    if token != plan["card"]["approval_token"]:
        return _text({"refused": True, "reason": "approval token does not match the plan card",
                      "detail": "Approve the card you were shown, and pass its approval_token."})

    result = await plr.execute_plan(plan["spec"], plan["steps"], token, make_live)
    executed = result.get("executed") is True
    audit_logged = _audit(plan_id, "executed" if executed else "failed", plan) if not result.get("refused") else False
    result.update({"plan_id": plan_id, "backend": CONFIG["backend"],
                   "hardware_touched": CONFIG["backend"] != "simulation" and "refused" not in result,
                   "audit_logged": audit_logged})
    return _text(result)


async def main():
    allowed, evidence = backend_allowed()
    if not allowed:
        print(f"Refusing to start the {CONFIG['backend']} backend: {evidence['level']}. "
              f"Run it supervised with --accept-unverified, then commit a hardware log.", file=sys.stderr)
        sys.exit(2)
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
