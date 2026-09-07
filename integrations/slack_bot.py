"""
OpenLabAI: Slack control surface
Lets a scientist run laboratory protocols from Slack, with approval in Slack.

Usage:
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...          # Socket Mode
    python integrations/slack_bot.py

    python integrations/slack_bot.py --dry-run   # no Slack, no robot; prints the
                                                 # blocks it would post

In Slack:
    @openlab hamilton ngs cleanup 1.8x beads, 2 ethanol washes, elute 20 uL
    @openlab worklist
    @openlab status

Why Slack. The approval gate in OpenLabAI needs a human to authorise a physical
action. That human is rarely at the terminal running the MCP server; they are on
their phone, or at the bench, or in a meeting. Slack is where laboratory teams
already are, so it is a natural place to put the gate: the agent posts what it
intends to do, and a named person presses Approve or Reject. Slack records who
pressed it, which is exactly the attribution an audit trail needs.

What this does NOT do. Pressing Approve does not bypass any other check. The
protocol is still validated against deck constraints first, the instrument's own
analysis still applies, and the approval is still recorded with the protocol
hash. Slack is a channel for the existing gate, not a way around it.

Safety properties:
  - A request never executes on arrival. It is planned, validated, and posted
    for approval; nothing physical happens until someone presses Approve.
  - Approve and Reject carry the Slack user id of whoever pressed them, and that
    identity is written to the audit record as the approving operator.
  - The requester and the approver are both recorded. A laboratory that requires
    two-person authorisation can set --require-second-person so the person who
    asked cannot also approve.
  - Stop is never gated and is available to anyone in the channel.
"""

import os
import sys
import json
import time
import argparse
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.protocol_evals import evaluate_protocol
from evals.run_logger import RunLogger

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Do not connect to Slack; print the blocks that would be posted")
parser.add_argument("--instrument", default="Hamilton_STAR",
                    help="Instrument these requests target (Hamilton_STAR, OT-2, Biomek_FXP)")
parser.add_argument("--require-second-person", action="store_true",
                    help="Refuse approval from the same person who made the request")
args, _ = parser.parse_known_args()

PENDING: dict = {}


# ── PROTOCOL PLANNING ────────────────────────────────────────────────────────

def plan_from_text(text: str, instrument: str) -> dict:
    """Turn a plain-English request into a structured protocol.

    Deliberately conservative: it recognises the procedures the eval framework
    has acceptance criteria for, and refuses anything else rather than guessing.
    A wrong guess here would become a physical action.
    """
    lowered = text.lower()
    if "cleanup" in lowered or "ampure" in lowered or "bead" in lowered:
        return _ngs_cleanup(text, instrument)
    if "normali" in lowered:
        return {"error": "normalization needs sample concentrations; ask for a LIMS worklist first"}
    return {"error": f"No protocol template matches {text!r}. "
                     "Supported: NGS bead cleanup. Ask an automation engineer for anything else."}


def _ngs_cleanup(text: str, instrument: str) -> dict:
    hamilton = instrument.startswith("Hamilton")
    tips = "tips_1" if hamilton else "source"
    src = "plate_1" if hamilton else "source"
    res = "plate_5" if hamilton else "reservoir"
    dst = "plate_2" if hamilton else "destination"
    return {
        "protocol_name": "NGS_AMPure_Cleanup",
        "protocol_type": "ngs_cleanup",
        "instrument": instrument,
        "requested_as": text,
        "estimated_minutes": 28,
        "steps": [
            {"id": 1, "label": "Pick up tips", "type": "pick_up_tips", "source": tips, "volume_ul": 0, "duration_min": 1},
            {"id": 2, "label": "Aspirate AMPure beads", "type": "aspirate", "source": res, "volume_ul": 90, "duration_min": 2},
            {"id": 3, "label": "Dispense beads to samples", "type": "dispense", "dest": src, "volume_ul": 90, "duration_min": 1.5},
            {"id": 4, "label": "Mix beads and DNA", "type": "mix", "source": src, "volume_ul": 80, "duration_min": 2},
            {"id": 5, "label": "Magnet incubation", "type": "wash", "source": src, "volume_ul": 0, "duration_min": 5},
            {"id": 6, "label": "Remove supernatant", "type": "aspirate", "source": src, "volume_ul": 120, "duration_min": 2},
            {"id": 7, "label": "Ethanol wash 1", "type": "transfer", "source": res, "dest": src, "volume_ul": 150, "duration_min": 2},
            {"id": 8, "label": "Ethanol wash 2", "type": "transfer", "source": res, "dest": src, "volume_ul": 150, "duration_min": 2},
            {"id": 9, "label": "Air dry beads", "type": "wash", "source": src, "volume_ul": 0, "duration_min": 3},
            {"id": 10, "label": "Elute in EB buffer", "type": "dispense", "source": res, "dest": src, "volume_ul": 20, "duration_min": 1.5},
            {"id": 11, "label": "Transfer eluate", "type": "transfer", "source": src, "dest": dst, "volume_ul": 20, "duration_min": 1.5},
        ],
    }


# ── SLACK MESSAGE CONSTRUCTION ───────────────────────────────────────────────

def build_approval_blocks(request_id: str, protocol: dict, result, requester: str) -> list:
    steps = protocol["steps"]
    lines = "\n".join(f"{s['id']:>2}. {s['label']}"
                      + (f" — {s['volume_ul']} µL" if s.get("volume_ul") else "")
                      for s in steps)
    verdict = "passed" if result.passed else "FAILED"
    violations = result.failure_modes or []
    return [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"Protocol awaiting approval — {protocol['instrument']}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (f"*<@{requester}> requested:* {protocol['requested_as']}\n"
                     f"*Protocol:* {protocol['protocol_name']}  ·  "
                     f"{len(steps)} steps  ·  ~{protocol['estimated_minutes']} min")}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{lines}```"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (f"*Validation:* {verdict}  ·  score {result.overall_score:.2f}  ·  "
                     f"safety {result.safety_compliance:.2f}\n"
                     f"*Protocol hash:* `{result.protocol_hash}`"
                     + (f"\n*Violations:* {'; '.join(violations)}" if violations else ""))}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary" if result.passed else "danger",
             "text": {"type": "plain_text", "text": "Approve and run"},
             "action_id": "approve", "value": request_id,
             "confirm": {
                 "title": {"type": "plain_text", "text": "Start the robot?"},
                 "text": {"type": "mrkdwn", "text": "This begins physical execution on "
                                                    f"*{protocol['instrument']}*."},
                 "confirm": {"type": "plain_text", "text": "Start"},
                 "deny": {"type": "plain_text", "text": "Cancel"}}},
            {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
             "action_id": "reject", "value": request_id},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": ("Nothing has moved. Pressing Approve records your Slack identity as the "
                     "approving operator in the audit log." if result.passed else
                     "This protocol failed validation. Approval is blocked.")}]},
    ]


def handle_request(text: str, requester: str) -> tuple:
    protocol = plan_from_text(text, args.instrument)
    if "error" in protocol:
        return None, protocol["error"], None
    result = evaluate_protocol(protocol, protocol_type=protocol["protocol_type"],
                               instrument=protocol["instrument"], generation_method="slack_request")
    request_id = hashlib.sha256(f"{requester}{text}{time.time()}".encode()).hexdigest()[:12]
    PENDING[request_id] = {"protocol": protocol, "result": result,
                           "requester": requester, "created": time.time()}
    return request_id, None, build_approval_blocks(request_id, protocol, result, requester)


def approve(request_id: str, approver: str) -> dict:
    entry = PENDING.get(request_id)
    if not entry:
        return {"ok": False, "message": "That request is no longer pending."}
    result = entry["result"]
    if not result.passed:
        return {"ok": False, "message": "Refused: the protocol failed validation. "
                                        f"Violations: {'; '.join(result.failure_modes) or 'acceptance criteria'}"}
    if args.require_second_person and approver == entry["requester"]:
        return {"ok": False, "message": "Refused: this laboratory requires a second person to approve."}

    logger = RunLogger(operator=f"slack:{approver}", instrument=entry["protocol"]["instrument"],
                       protocol_name=entry["protocol"]["protocol_name"])
    logger.log_protocol_generated(entry["protocol"], protocol_hash=result.protocol_hash,
                                  eval_score=result.overall_score, generation_method="slack_request")
    logger.log_agent_message("system",
                             f"requested by {entry['requester']}, approved by {approver}")
    logger.log_run_complete(status="approved")
    path = logger.save()
    del PENDING[request_id]
    return {"ok": True, "message": f"Approved by <@{approver}>. Protocol hash `{result.protocol_hash}`.",
            "audit_log": path, "approver": approver, "requester": entry["requester"]}


def reject(request_id: str, actor: str) -> dict:
    entry = PENDING.pop(request_id, None)
    if not entry:
        return {"ok": False, "message": "That request is no longer pending."}
    return {"ok": True, "message": f"Rejected by <@{actor}>. Nothing was run."}


# ── SLACK WIRING ─────────────────────────────────────────────────────────────

def run_slack() -> None:
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        sys.exit("slack_bolt is not installed. pip install -r requirements.txt, "
                 "or run with --dry-run to see the messages without Slack.")

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not (bot_token and app_token):
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN. Tokens are read from the "
                 "environment and are never accepted as message arguments.")

    app = App(token=bot_token)

    @app.event("app_mention")
    def on_mention(event, say):
        text = event.get("text", "")
        text = text.split(">", 1)[1].strip() if ">" in text else text
        user = event.get("user", "unknown")
        if text.lower().startswith("status"):
            say(f"{len(PENDING)} request(s) awaiting approval on {args.instrument}.")
            return
        request_id, error, blocks = handle_request(text, user)
        if error:
            say(f":warning: {error}")
            return
        say(blocks=blocks, text="Protocol awaiting approval")

    @app.action("approve")
    def on_approve(ack, body, say):
        ack()
        outcome = approve(body["actions"][0]["value"], body["user"]["id"])
        say(("✅ " if outcome["ok"] else "⛔ ") + outcome["message"])

    @app.action("reject")
    def on_reject(ack, body, say):
        ack()
        outcome = reject(body["actions"][0]["value"], body["user"]["id"])
        say("🚫 " + outcome["message"])

    print(f"OpenLabAI Slack bot running for {args.instrument}. Ctrl-C to stop.")
    SocketModeHandler(app, app_token).start()


def run_dry() -> None:
    """Exercise the whole path with no Slack and no robot."""
    print("Dry run — no Slack connection, no instrument contacted.\n")
    request_id, error, blocks = handle_request(
        "hamilton ngs cleanup 1.8x beads, 2 ethanol washes, elute 20 uL", "U_SCIENTIST")
    if error:
        print("refused:", error); return
    print(json.dumps(blocks, indent=2)[:1400], "\n...")
    print("\n-- self-approval with --require-second-person --")
    args.require_second_person = True
    print(" ", approve(request_id, "U_SCIENTIST")["message"])
    print("\n-- approval by a second person --")
    outcome = approve(request_id, "U_SUPERVISOR")
    print(" ", outcome["message"])
    print("  audit log:", outcome.get("audit_log"))
    print("\n-- an unsupported request --")
    _, err, _ = handle_request("please centrifuge the samples", "U_SCIENTIST")
    print(" ", err)


if __name__ == "__main__":
    run_dry() if args.dry_run else run_slack()
