from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _history_path(data_dir: Path) -> Path:
    return data_dir / "approvals" / "history.jsonl"


def log_approval(
    data_dir: Path,
    *,
    incident_id: str,
    project: str | None,
    service: str | None,
    action: str | None,
    approved_by: str,
) -> dict[str, Any]:
    approvals_dir = data_dir / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    (approvals_dir / f"{incident_id}.approved").write_text(stamp)
    entry = {
        "incident_id": incident_id,
        "project": project,
        "service": service,
        "action": action,
        "approved_at": stamp,
        "approved_by": approved_by,
    }
    with _history_path(data_dir).open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def list_approval_history(data_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    path = _history_path(data_dir)
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))
