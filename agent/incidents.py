from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActionRecord:
    command: str
    result: str


@dataclass
class Incident:
    project: str
    service: str
    issue: str
    app_version: str | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    outcome: str = "open"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=_utcnow)
    root_cause: str | None = None
    recommended_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actions"] = [asdict(a) for a in self.actions]
        return data


class IncidentStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "incidents.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, incident: Incident) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(incident.to_dict()) + "\n")

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(rows))

    def find_similar(self, project: str, service: str, issue: str, limit: int = 20) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for row in self.recent(limit=200):
            if row.get("project") == project and row.get("service") == service:
                if issue.lower() in str(row.get("issue", "")).lower() or row.get("outcome") == "resolved":
                    matches.append(row)
            if len(matches) >= limit:
                break
        return matches
