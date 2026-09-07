"""
Tests for acceptance-criteria matching.

Run:  python evals/test_criteria.py
Exits non-zero on failure.

These exist because criteria matching used to be literal substring matching on
step labels, which failed on ordinary wordings ("Aspirate AMPure beads" does not
contain "aspirate beads"). The negative cases matter as much as the positive
ones: a matcher that accepts everything is worse than one that is too strict,
because it would pass an unsafe protocol.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.protocol_evals import criterion_matches, evaluate_protocol

SHOULD_MATCH = [
    ("aspirate_beads", "Aspirate AMPure beads"),
    ("aspirate_beads", "Add 90 uL SPRI beads to sample"),
    ("aspirate_beads", "Transfer carboxyl beads to plate"),
    ("mix", "Mix beads and DNA"),
    ("mix", "Resuspend pellet"),
    ("magnet_incubation", "Magnet incubation"),
    ("magnet_incubation", "Incubate 5 min on magnetic stand"),
    ("magnet_incubation", "Bind on mag stand"),
    ("remove_supernatant", "Remove supernatant"),
    ("remove_supernatant", "Discard the supernatant"),
    ("ethanol_wash", "Ethanol wash 1"),
    ("ethanol_wash", "80% EtOH rinse"),
    ("air_dry", "Air dry beads"),
    ("elute", "Elute in EB buffer"),
    ("elute", "Resuspend in 20 uL water"),
    ("diluent_dispense", "Dispense diluent to plate"),
    ("transfer_and_mix", "Serial transfer and mix"),
]

SHOULD_NOT_MATCH = [
    ("aspirate_beads", "Pick up tips"),
    ("aspirate_beads", "Aspirate 50 uL of sample"),   # aspirate, but not beads
    ("ethanol_wash", "Wash with PBS"),                # a wash, but not ethanol
    ("magnet_incubation", "Incubate at 37 C"),        # incubation, but no magnet
    ("remove_supernatant", "Remove tip rack"),
    ("elute", "Add ethanol"),
    ("air_dry", "Transfer to destination plate"),
    ("calculate_volumes", "Pick up tips"),
]

INCOMPLETE_CLEANUP = {
    "protocol_name": "Incomplete_Cleanup",
    "steps": [
        {"id": 1, "label": "Pick up tips", "type": "pick_up_tips", "volume_ul": 0, "duration_min": 1},
        {"id": 2, "label": "Aspirate AMPure beads", "type": "aspirate", "source": "10", "volume_ul": 90, "duration_min": 2},
        {"id": 3, "label": "Mix beads and DNA", "type": "mix", "source": "1", "volume_ul": 80, "duration_min": 2},
    ],
}

OVER_VOLUME_TRANSFER = {
    "protocol_name": "Over_Volume",
    "steps": [
        {"id": 1, "label": "Aspirate sample", "type": "aspirate", "source": "1", "volume_ul": 5000, "duration_min": 1},
        {"id": 2, "label": "Dispense sample", "type": "dispense", "dest": "4", "volume_ul": 5000, "duration_min": 1},
    ],
}


def main() -> int:
    failures = []

    for criterion, label in SHOULD_MATCH:
        if not criterion_matches(criterion, label):
            failures.append(f"should have matched: {criterion} <- {label!r}")

    for criterion, label in SHOULD_NOT_MATCH:
        if criterion_matches(criterion, label):
            failures.append(f"should NOT have matched: {criterion} <- {label!r}")

    # An incomplete cleanup must fail acceptance.
    result = evaluate_protocol(INCOMPLETE_CLEANUP, protocol_type="ngs_cleanup", instrument="OT-2")
    if result.acceptance_passed:
        failures.append("incomplete cleanup passed acceptance criteria")
    if result.passed:
        failures.append("incomplete cleanup reported passed=True")

    # A volume beyond any OT-2 tip must be caught by the deck constraint checker.
    over = evaluate_protocol(OVER_VOLUME_TRANSFER, protocol_type="simple_transfer", instrument="OT-2")
    if over.safety_compliance >= 1.0:
        failures.append("5000 uL transfer did not reduce safety_compliance")

    # Scores must stay inside 0.0-1.0 for every protocol we evaluate.
    for name, res in [("incomplete", result), ("over_volume", over)]:
        for field in ("syntactic_validity", "semantic_accuracy", "safety_compliance", "reproducibility"):
            value = getattr(res, field)
            if not 0.0 <= value <= 1.0:
                failures.append(f"{name}.{field} out of range: {value}")
        if not 0.0 <= res.overall_score <= 1.0:
            failures.append(f"{name}.overall_score out of range: {res.overall_score}")

    checks = len(SHOULD_MATCH) + len(SHOULD_NOT_MATCH) + 7
    if failures:
        print(f"FAILED ({len(failures)} of ~{checks} checks)")
        for f in failures:
            print("  -", f)
        return 1

    print(f"OK — {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
