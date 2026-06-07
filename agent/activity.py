from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActivityRecord:
    message: str
    level: str = "info"
    category: str = "agent"
    project: str | None = None
    service: str | None = None
    incident_id: str | None = None
    ts: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivityLog:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "activity.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        message: str,
        *,
        level: str = "info",
        category: str = "agent",
        project: str | None = None,
        service: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        row = ActivityRecord(
            message=message,
            level=level,
            category=category,
            project=project,
            service=service,
            incident_id=incident_id,
        )
        with self.path.open("a") as fh:
            fh.write(json.dumps(row.to_dict()) + "\n")

    def write_heartbeat(self, summary: dict[str, Any]) -> None:
        payload = {"ts": _utcnow(), **summary}
        (self.data_dir / "heartbeat.json").write_text(json.dumps(payload, indent=2))
