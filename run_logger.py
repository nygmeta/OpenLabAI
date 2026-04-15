"""
OpenLabAI Run Logger
=====================
Provides auditability and traceability for all agent-generated protocols
and instrument interactions.

Designed for regulated life sciences environments (GxP-adjacent).
Every action is logged with timestamp, operator, instrument, and outcome.

Usage:
    logger = RunLogger(operator="ann.nygmet", instrument="OT-2")
    logger.log_protocol_generated(protocol)
    logger.log_step_executed(step_id=1, status="success")
    logger.save()
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict


LOG_DIR = Path("run_logs")
LOG_DIR.mkdir(exist_ok=True)


@dataclass
class StepLog:
    step_id: int
    label: str
    type: str
    status: str                  # "success", "failed", "skipped", "paused"
    started_at: str
    completed_at: str = ""
    volume_ul: float = 0
    source: str = ""
    destination: str = ""
    error: str = ""
    notes: str = ""


@dataclass
class RunLog:
    run_id: str
    operator: str
    instrument: str
    protocol_name: str
    protocol_hash: str
    started_at: str
    completed_at: str = ""
    status: str = "in_progress"  # "in_progress", "completed", "failed", "aborted"
    generation_method: str = "single_shot"
    eval_score: float = 0.0
    steps: list = field(default_factory=list)
    agent_messages: list = field(default_factory=list)
    total_steps: int = 0
    completed_steps: int = 0
    notes: str = ""

    @property
    def duration_minutes(self) -> float:
        if not self.completed_at:
            return 0
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.completed_at)
        return round((end - start).total_seconds() / 60, 2)


class RunLogger:
    """
    Full audit trail logger for OpenLabAI protocol runs.
    Writes structured JSON logs for every run, suitable for
    GxP-adjacent environments requiring traceability.
    """

    def __init__(self, operator: str, instrument: str, protocol_name: str = ""):
        self.run_id = str(uuid.uuid4())[:8].upper()
        self.log = RunLog(
            run_id=self.run_id,
            operator=operator,
            instrument=instrument,
            protocol_name=protocol_name,
            protocol_hash="",
            started_at=datetime.utcnow().isoformat(),
        )
        self.log_path = LOG_DIR / f"run_{self.run_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        print(f"[RunLogger] Run {self.run_id} started — {operator} on {instrument}")

    def log_protocol_generated(self, protocol: dict, protocol_hash: str = "", eval_score: float = 0.0, generation_method: str = "single_shot"):
        self.log.protocol_name = protocol.get("protocol_name", "unnamed")
        self.log.protocol_hash = protocol_hash
        self.log.eval_score = eval_score
        self.log.generation_method = generation_method
        self.log.total_steps = len(protocol.get("steps", []))
        self.log.agent_messages.append({
            "type": "protocol_generated",
            "timestamp": datetime.utcnow().isoformat(),
            "protocol_name": self.log.protocol_name,
            "steps": self.log.total_steps,
            "eval_score": eval_score,
        })
        self._autosave()

    def log_agent_message(self, role: str, content: str):
        self.log.agent_messages.append({
            "type": "chat",
            "role": role,
            "content": content[:500],
            "timestamp": datetime.utcnow().isoformat(),
        })

    def log_step_started(self, step: dict) -> StepLog:
        step_log = StepLog(
            step_id=step.get("id", 0),
            label=step.get("label", ""),
            type=step.get("type", ""),
            status="in_progress",
            started_at=datetime.utcnow().isoformat(),
            volume_ul=step.get("volume_ul", 0),
            source=step.get("source", ""),
            destination=step.get("dest", ""),
        )
        self.log.steps.append(asdict(step_log))
        self._autosave()
        return step_log

    def log_step_completed(self, step_id: int, status: str = "success", error: str = "", notes: str = ""):
        for step in self.log.steps:
            if step["step_id"] == step_id:
                step["status"] = status
                step["completed_at"] = datetime.utcnow().isoformat()
                step["error"] = error
                step["notes"] = notes
                break
        if status == "success":
            self.log.completed_steps += 1
        elif status == "failed":
            self.log.status = "failed"
        self._autosave()

    def log_run_complete(self, status: str = "completed", notes: str = ""):
        self.log.completed_at = datetime.utcnow().isoformat()
        self.log.status = status
        self.log.notes = notes
        self._autosave()
        print(f"[RunLogger] Run {self.run_id} {status} — {self.log.duration_minutes} min — saved to {self.log_path}")

    def get_summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "operator": self.log.operator,
            "instrument": self.log.instrument,
            "protocol": self.log.protocol_name,
            "status": self.log.status,
            "started_at": self.log.started_at,
            "completed_at": self.log.completed_at,
            "duration_minutes": self.log.duration_minutes,
            "steps_total": self.log.total_steps,
            "steps_completed": self.log.completed_steps,
            "eval_score": self.log.eval_score,
            "generation_method": self.log.generation_method,
        }

    def _autosave(self):
        with open(self.log_path, "w") as f:
            json.dump(asdict(self.log), f, indent=2)

    def save(self) -> str:
        self._autosave()
        return str(self.log_path)


def load_run_log(run_id: str) -> dict:
    matches = list(LOG_DIR.glob(f"run_{run_id}_*.json"))
    if not matches:
        raise FileNotFoundError(f"No log found for run {run_id}")
    with open(matches[0]) as f:
        return json.load(f)


def list_recent_runs(n: int = 10) -> list[dict]:
    logs = sorted(LOG_DIR.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for log_path in logs[:n]:
        with open(log_path) as f:
            data = json.load(f)
        results.append({
            "run_id": data["run_id"],
            "operator": data["operator"],
            "instrument": data["instrument"],
            "protocol": data["protocol_name"],
            "status": data["status"],
            "started_at": data["started_at"],
        })
    return results


if __name__ == "__main__":
    logger = RunLogger(operator="ann.nygmet@sentientmechanics.com", instrument="OT-2", protocol_name="NGS_Cleanup_Test")

    mock_protocol = {
        "protocol_name": "NGS_Cleanup_Test",
        "steps": [{"id": 1, "label": "Aspirate beads", "type": "aspirate", "volume_ul": 90, "source": "10", "dest": "1"}]
    }
    logger.log_protocol_generated(mock_protocol, protocol_hash="abc123", eval_score=0.87, generation_method="sfs")
    logger.log_agent_message("user", "Run an NGS cleanup on my library plate")
    logger.log_agent_message("assistant", "Protocol generated — 10 steps, 28 min estimated")

    step_log = logger.log_step_started({"id": 1, "label": "Aspirate beads", "type": "aspirate", "volume_ul": 90})
    logger.log_step_completed(1, status="success")
    logger.log_run_complete(status="completed", notes="Successful demo run")

    print(json.dumps(logger.get_summary(), indent=2))
