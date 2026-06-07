from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _snooze_path(data_dir: Path) -> Path:
    return data_dir / "state" / "snoozes.json"


def _load(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = _snooze_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save(data_dir: Path, snoozes: dict[str, dict[str, Any]]) -> None:
    path = _snooze_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snoozes, indent=2))


def service_key(project: str, service: str) -> str:
    return f"{project}/{service}"


def snooze(
    data_dir: Path,
    project: str,
    service: str,
    hours: float,
    *,
    reason: str = "",
    created_by: str = "ui",
) -> dict[str, Any]:
    key = service_key(project, service)
    until = time.time() + (hours * 3600)
    entry = {
        "project": project,
        "service": service,
        "until": until,
        "until_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
        "hours": hours,
        "reason": reason,
        "created_by": created_by,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    snoozes = _load(data_dir)
    snoozes[key] = entry
    _save(data_dir, snoozes)
    return entry


def clear_snooze(data_dir: Path, project: str, service: str) -> bool:
    key = service_key(project, service)
    snoozes = _load(data_dir)
    if key not in snoozes:
        return False
    del snoozes[key]
    _save(data_dir, snoozes)
    return True


def is_snoozed(data_dir: Path, project: str, service: str) -> bool:
    key = service_key(project, service)
    snoozes = _prune_expired(_load(data_dir))
    _save(data_dir, snoozes)
    entry = snoozes.get(key)
    if not entry:
        return False
    return float(entry.get("until", 0)) > time.time()


def list_snoozes(data_dir: Path) -> list[dict[str, Any]]:
    snoozes = _prune_expired(_load(data_dir))
    _save(data_dir, snoozes)
    now = time.time()
    rows = []
    for entry in snoozes.values():
        row = dict(entry)
        row["remaining_seconds"] = max(0, int(float(entry.get("until", 0)) - now))
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("until", 0))


def _prune_expired(snoozes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = time.time()
    return {k: v for k, v in snoozes.items() if float(v.get("until", 0)) > now}
