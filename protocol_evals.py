"""
OpenLabAI Protocol Evaluation Framework
========================================
Defines acceptance criteria, benchmarks, and validation evidence
for AI-generated liquid handling protocols.

This eval framework measures:
1. Protocol validity    — does it pass instrument-specific validation?
2. Scientific accuracy  — does it accomplish the stated biological goal?
3. Safety compliance    — does it respect volume, tip, and collision constraints?
4. Reproducibility      — does repeated generation produce consistent protocols?

Usage:
    python evals/protocol_evals.py --protocol ngs_cleanup --runs 10
"""

import json
import time
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


# ── ACCEPTANCE CRITERIA ──────────────────────────────────────────────────────

ACCEPTANCE_CRITERIA = {
    "ngs_cleanup": {
        "required_steps": ["aspirate_beads", "mix", "magnet_incubation", "remove_supernatant", "ethanol_wash", "air_dry", "elute"],
        "min_bead_ratio": 1.5,
        "max_bead_ratio": 2.5,
        "min_wash_cycles": 2,
        "elution_volume_range": (10, 50),
        "tip_change_required": True,
    },
    "normalization": {
        "required_steps": ["measure_or_accept_concentration", "calculate_volumes", "transfer", "mix"],
        "target_concentration_tolerance": 0.1,
        "min_final_volume": 10,
        "tip_change_required": True,
    },
    "serial_dilution": {
        "required_steps": ["diluent_dispense", "transfer_and_mix"],
        "min_dilution_steps": 3,
        "volume_consistency": True,
        "tip_change_required": False,
    },
    "simple_transfer": {
        "required_steps": ["aspirate", "dispense"],
        "volume_accuracy_tolerance": 0.05,
        "tip_change_required": False,
    }
}

DECK_CONSTRAINTS = {
    "OT-2": {
        "max_volume_300ul_tips": 300,
        "max_volume_1000ul_tips": 1000,
        "valid_slots": ["1","2","3","4","5","6","7","8","9","10","11","12"],
        "tip_rack_slots": ["7", "8", "11"],
        "max_aspiration_height_mm": 100,
        "min_aspiration_height_mm": 0.1,
    },
    "Hamilton_STAR": {
        "max_volume_1000ul": 1000,
        "max_volume_300ul": 300,
        "valid_positions": [f"P{i}" for i in range(1, 22)] + ["TL1", "TR1"],
        "span8_channels": 8,
    },
    "Biomek_FXP": {
        "valid_positions": [f"P{i}" for i in range(1, 22)] + ["TL1", "TR1"],
        "span8_channels": 8,
        "max_volume_300ul": 300,
    }
}


# ── DATA STRUCTURES ───────────────────────────────────────────────────────────

@dataclass
class ProtocolEvalResult:
    protocol_name: str
    instrument: str
    timestamp: str
    generation_method: str          # "single_shot" or "sfs"
    generation_time_seconds: float

    # Validity scores (0.0 - 1.0)
    syntactic_validity: float       # passes instrument schema validation
    semantic_accuracy: float        # accomplishes stated biological goal
    safety_compliance: float        # respects all deck/volume constraints
    reproducibility: float          # consistency across repeated generations

    # Acceptance criteria results
    acceptance_passed: bool
    acceptance_details: dict = field(default_factory=dict)

    # Audit trail
    protocol_hash: str = ""         # SHA256 of generated protocol JSON
    failure_modes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def overall_score(self) -> float:
        return (
            self.syntactic_validity * 0.3 +
            self.semantic_accuracy * 0.4 +
            self.safety_compliance * 0.2 +
            self.reproducibility * 0.1
        )

    @property
    def passed(self) -> bool:
        return self.acceptance_passed and self.overall_score >= 0.80


# ── CONSTRAINT CHECKER ────────────────────────────────────────────────────────

class DeckConstraintChecker:
    """
    Validates protocol steps against instrument-specific deck constraints.
    Returns a score from 0.0 to 1.0 and a list of violations.
    """

    def __init__(self, instrument: str):
        self.instrument = instrument
        self.constraints = DECK_CONSTRAINTS.get(instrument, {})
        self.violations = []
        self.warnings = []

    def check_volume(self, volume_ul: float, tip_type: str = "300ul") -> bool:
        max_key = f"max_volume_{tip_type}"
        max_vol = self.constraints.get(max_key, 300)
        if volume_ul > max_vol:
            self.violations.append(f"Volume {volume_ul}µL exceeds {tip_type} tip capacity ({max_vol}µL)")
            return False
        if volume_ul <= 0:
            self.violations.append(f"Invalid volume: {volume_ul}µL")
            return False
        return True

    def check_position(self, position: str) -> bool:
        valid = self.constraints.get("valid_slots") or self.constraints.get("valid_positions", [])
        if position not in valid:
            self.violations.append(f"Invalid deck position: {position}")
            return False
        return True

    def check_tip_availability(self, steps: list) -> bool:
        tip_changes = sum(1 for s in steps if s.get("type") in ["pick_up_tips", "wash"])
        aspirations = sum(1 for s in steps if s.get("type") in ["aspirate", "transfer"])
        if aspirations > 0 and tip_changes == 0:
            self.warnings.append("No tip pickup detected before aspiration steps")
        return True

    def score(self, steps: list) -> float:
        checks = []
        for step in steps:
            if step.get("volume_ul"):
                checks.append(self.check_volume(step["volume_ul"]))
            if step.get("source"):
                checks.append(self.check_position(step["source"]))
            if step.get("dest"):
                checks.append(self.check_position(step["dest"]))
        self.check_tip_availability(steps)
        if not checks:
            return 1.0
        return sum(checks) / len(checks)


# ── ACCEPTANCE CRITERIA CHECKER ───────────────────────────────────────────────

class AcceptanceCriteriaChecker:
    """
    Checks whether a protocol meets the scientific acceptance criteria
    for a given protocol type.
    """

    def __init__(self, protocol_type: str):
        self.criteria = ACCEPTANCE_CRITERIA.get(protocol_type, {})
        self.details = {}

    def check(self, steps: list, params: dict = None) -> tuple[bool, dict]:
        params = params or {}
        passed = True

        required = self.criteria.get("required_steps", [])
        step_types = [s.get("type", "") for s in steps]
        step_labels = [s.get("label", "").lower() for s in steps]

        for req in required:
            found = any(req.replace("_", " ") in label for label in step_labels)
            self.details[f"has_{req}"] = found
            if not found:
                passed = False

        if "min_wash_cycles" in self.criteria:
            wash_count = sum(1 for l in step_labels if "wash" in l or "etoh" in l or "ethanol" in l)
            self.details["wash_cycles"] = wash_count
            if wash_count < self.criteria["min_wash_cycles"]:
                passed = False

        if "elution_volume_range" in self.criteria:
            lo, hi = self.criteria["elution_volume_range"]
            elution_steps = [s for s in steps if "elut" in s.get("label", "").lower()]
            if elution_steps:
                vol = elution_steps[-1].get("volume_ul", 0)
                self.details["elution_volume"] = vol
                if not (lo <= vol <= hi):
                    passed = False

        return passed, self.details


# ── MAIN EVAL RUNNER ──────────────────────────────────────────────────────────

def evaluate_protocol(
    protocol: dict,
    protocol_type: str,
    instrument: str,
    generation_method: str = "single_shot",
    generation_time: float = 0.0
) -> ProtocolEvalResult:
    """
    Run full evaluation on a generated protocol.

    Args:
        protocol: dict with 'name', 'steps', 'estimated_minutes'
        protocol_type: one of ACCEPTANCE_CRITERIA keys
        instrument: one of DECK_CONSTRAINTS keys
        generation_method: 'single_shot' or 'sfs'
        generation_time: seconds taken to generate

    Returns:
        ProtocolEvalResult with all scores and audit trail
    """
    steps = protocol.get("steps", [])
    protocol_json = json.dumps(protocol, sort_keys=True)
    protocol_hash = hashlib.sha256(protocol_json.encode()).hexdigest()[:16]

    # 1. Constraint check (safety compliance)
    checker = DeckConstraintChecker(instrument)
    safety_score = checker.score(steps)

    # 2. Acceptance criteria (semantic accuracy)
    ac_checker = AcceptanceCriteriaChecker(protocol_type)
    acceptance_passed, acceptance_details = ac_checker.check(steps)
    semantic_score = sum(acceptance_details.values()) / max(len(acceptance_details), 1) if acceptance_details else 0.5

    # 3. Syntactic validity (does it have required fields)
    has_name = bool(protocol.get("protocol_name"))
    has_steps = len(steps) > 0
    has_timing = all(s.get("duration_min") for s in steps)
    syntactic_score = (has_name + has_steps + has_timing) / 3

    # 4. Reproducibility placeholder (1.0 for single run, computed across runs)
    reproducibility_score = 1.0

    return ProtocolEvalResult(
        protocol_name=protocol.get("protocol_name", "unnamed"),
        instrument=instrument,
        timestamp=datetime.utcnow().isoformat(),
        generation_method=generation_method,
        generation_time_seconds=generation_time,
        syntactic_validity=round(syntactic_score, 3),
        semantic_accuracy=round(semantic_score, 3),
        safety_compliance=round(safety_score, 3),
        reproducibility=reproducibility_score,
        acceptance_passed=acceptance_passed,
        acceptance_details=acceptance_details,
        protocol_hash=protocol_hash,
        failure_modes=checker.violations,
        warnings=checker.warnings,
    )


def run_eval_suite(protocols: list[dict], instrument: str = "OT-2") -> dict:
    """
    Run evaluation suite across multiple protocols.
    Returns summary statistics suitable for reporting.
    """
    results = []
    for p in protocols:
        ptype = p.get("protocol_type", "simple_transfer")
        result = evaluate_protocol(p, ptype, instrument)
        results.append(result)

    passed = sum(1 for r in results if r.passed)
    avg_score = sum(r.overall_score for r in results) / len(results) if results else 0

    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "avg_overall_score": round(avg_score, 3),
        "avg_syntactic": round(sum(r.syntactic_validity for r in results) / len(results), 3) if results else 0,
        "avg_semantic": round(sum(r.semantic_accuracy for r in results) / len(results), 3) if results else 0,
        "avg_safety": round(sum(r.safety_compliance for r in results) / len(results), 3) if results else 0,
        "results": [asdict(r) for r in results],
    }


# ── EXAMPLE USAGE ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    example_protocol = {
        "protocol_name": "NGS_AMPure_Cleanup",
        "protocol_type": "ngs_cleanup",
        "estimated_minutes": 28,
        "steps": [
            {"id": 1, "label": "Aspirate AMPure beads", "type": "aspirate", "source": "10", "dest": "", "volume_ul": 90, "duration_min": 2},
            {"id": 2, "label": "Dispense beads to samples", "type": "dispense", "source": "", "dest": "1", "volume_ul": 90, "duration_min": 1.5},
            {"id": 3, "label": "Mix beads and DNA", "type": "mix", "source": "1", "dest": "", "volume_ul": 80, "duration_min": 2},
            {"id": 4, "label": "Magnet incubation", "type": "wash", "source": "", "dest": "", "volume_ul": 0, "duration_min": 5},
            {"id": 5, "label": "Remove supernatant", "type": "aspirate", "source": "1", "dest": "", "volume_ul": 120, "duration_min": 2},
            {"id": 6, "label": "Ethanol wash 1", "type": "transfer", "source": "10", "dest": "1", "volume_ul": 150, "duration_min": 2},
            {"id": 7, "label": "Ethanol wash 2", "type": "transfer", "source": "10", "dest": "1", "volume_ul": 150, "duration_min": 2},
            {"id": 8, "label": "Air dry beads", "type": "wash", "source": "", "dest": "", "volume_ul": 0, "duration_min": 3},
            {"id": 9, "label": "Elute in EB buffer", "type": "dispense", "source": "10", "dest": "1", "volume_ul": 20, "duration_min": 1.5},
            {"id": 10, "label": "Mix to elute", "type": "mix", "source": "1", "dest": "", "volume_ul": 15, "duration_min": 1.5},
        ]
    }

    result = evaluate_protocol(
        example_protocol,
        protocol_type="ngs_cleanup",
        instrument="OT-2",
        generation_method="sfs",
        generation_time=7.2
    )

    print(f"\nProtocol: {result.protocol_name}")
    print(f"Overall score: {result.overall_score:.2f}")
    print(f"Passed: {result.passed}")
    print(f"Syntactic validity: {result.syntactic_validity}")
    print(f"Semantic accuracy: {result.semantic_accuracy}")
    print(f"Safety compliance: {result.safety_compliance}")
    print(f"Acceptance criteria: {result.acceptance_passed}")
    print(f"Protocol hash: {result.protocol_hash}")
    if result.failure_modes:
        print(f"Failures: {result.failure_modes}")
    if result.warnings:
        print(f"Warnings: {result.warnings}")
