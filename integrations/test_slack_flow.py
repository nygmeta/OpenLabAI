"""
Tests for the Slack approval flow.

Run:  python integrations/test_slack_flow.py
Exits non-zero on failure.

The safety claim these protect: a Slack message can never cause physical motion
on its own. It creates a pending request; a person must approve it; a protocol
that fails validation cannot be approved at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.argv = ["test"]

from integrations import slack_bot as sb


def main() -> int:
    failures = []

    # A request must not execute on arrival — it becomes pending.
    sb.PENDING.clear()
    rid, err, blocks = sb.handle_request("ngs cleanup 1.8x beads, 2 washes, elute 20 uL", "U_A")
    if err:
        failures.append(f"valid cleanup request was refused: {err}")
    if rid not in sb.PENDING:
        failures.append("request did not become pending")
    if blocks and not any(b.get("type") == "actions" for b in blocks):
        failures.append("approval message has no Approve/Reject buttons")

    # An unsupported request must be refused rather than guessed at.
    _, err2, _ = sb.handle_request("centrifuge at 4000g", "U_A")
    if not err2:
        failures.append("unsupported request was not refused")

    # Two-person rule: the requester cannot approve their own request.
    sb.args.require_second_person = True
    outcome = sb.approve(rid, "U_A")
    if outcome["ok"]:
        failures.append("self-approval succeeded under the two-person rule")
    if rid not in sb.PENDING:
        failures.append("refused self-approval consumed the pending request")

    # A second person can approve, and both identities are recorded.
    outcome = sb.approve(rid, "U_B")
    if not outcome["ok"]:
        failures.append(f"second-person approval failed: {outcome['message']}")
    else:
        if outcome.get("approver") != "U_B" or outcome.get("requester") != "U_A":
            failures.append("audit outcome did not record both requester and approver")
        if not outcome.get("audit_log"):
            failures.append("approval did not write an audit log")
    if rid in sb.PENDING:
        failures.append("approved request stayed pending")

    # An approved request cannot be replayed.
    if sb.approve(rid, "U_B")["ok"]:
        failures.append("a consumed request could be approved twice")

    # A protocol that fails validation cannot be approved.
    sb.args.require_second_person = False
    sb.PENDING.clear()
    bad_protocol = {"protocol_name": "Bad", "protocol_type": "ngs_cleanup",
                    "instrument": "Hamilton_STAR", "requested_as": "x", "estimated_minutes": 1,
                    "steps": [{"id": 1, "label": "Pick up tips", "type": "pick_up_tips",
                               "volume_ul": 0, "duration_min": 1}]}
    from evals.protocol_evals import evaluate_protocol
    bad_result = evaluate_protocol(bad_protocol, protocol_type="ngs_cleanup", instrument="Hamilton_STAR")
    sb.PENDING["bad"] = {"protocol": bad_protocol, "result": bad_result,
                         "requester": "U_A", "created": 0}
    if sb.approve("bad", "U_B")["ok"]:
        failures.append("a protocol failing validation was approved")

    # Reject clears the request without running anything.
    sb.PENDING["rej"] = {"protocol": bad_protocol, "result": bad_result,
                         "requester": "U_A", "created": 0}
    if not sb.reject("rej", "U_B")["ok"] or "rej" in sb.PENDING:
        failures.append("reject did not clear the pending request")

    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("OK — 12 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
