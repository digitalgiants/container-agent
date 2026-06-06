from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.discovery import ComposeProject, container_id_for_service
from agent.health import ServiceHealth
from agent.incidents import ActionRecord, Incident


@dataclass
class RemediationResult:
    incident: Incident
    resolved: bool
    pending_approval: bool = False


def _run(cmd: list[str], cwd: Path | None = None) -> ActionRecord:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    summary = output[-2000:] if output else f"exit {result.returncode}"
    status = "ok" if result.returncode == 0 else f"failed (exit {result.returncode})"
    return ActionRecord(command=" ".join(cmd), result=f"{status}: {summary}")


def _restart_state_path(data_dir: Path) -> Path:
    return data_dir / "state" / "restart_counts.json"


def _load_restart_counts(data_dir: Path) -> dict[str, list[float]]:
    path = _restart_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {k: [float(x) for x in v] for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_restart_counts(data_dir: Path, counts: dict[str, list[float]]) -> None:
    path = _restart_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(counts))


def _prune_hour(counts: dict[str, list[float]]) -> dict[str, list[float]]:
    cutoff = time.time() - 3600
    return {k: [t for t in v if t >= cutoff] for k, v in counts.items()}


def can_restart(data_dir: Path, key: str, max_per_hour: int) -> bool:
    counts = _prune_hour(_load_restart_counts(data_dir))
    return len(counts.get(key, [])) < max_per_hour


def record_restart(data_dir: Path, key: str) -> None:
    counts = _prune_hour(_load_restart_counts(data_dir))
    counts.setdefault(key, []).append(time.time())
    _save_restart_counts(data_dir, counts)


def graceful_restart(project: ComposeProject, service: str) -> ActionRecord:
    return _run(
        ["podman", "compose", "-f", str(project.compose_file), "restart", service],
        cwd=project.directory,
    )


def force_restart(project: ComposeProject, service: str) -> ActionRecord:
    stop = _run(
        ["podman", "compose", "-f", str(project.compose_file), "stop", service],
        cwd=project.directory,
    )
    start = _run(
        ["podman", "compose", "-f", str(project.compose_file), "start", service],
        cwd=project.directory,
    )
    return ActionRecord(
        command=f"force restart {service}",
        result=f"{stop.result}\n{start.result}",
    )


def _inspect_mounts(container_id: str) -> list[Path]:
    result = subprocess.run(
        ["podman", "inspect", container_id, "--format", "{{json .Mounts}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        mounts = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    paths: list[Path] = []
    for mount in mounts:
        source = mount.get("Source")
        if source:
            paths.append(Path(source))
    return paths


def _is_protected(path: Path, globs: list[str]) -> bool:
    s = str(path)
    for pattern in globs:
        if fnmatch.fnmatch(s, pattern) or fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def backup_file(path: Path, backups_dir: Path, project: str, service: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = backups_dir / ts / project / service
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.copy2(path, dest)
    return dest


def find_candidate_lock_files(
    mount_paths: list[Path],
    lock_patterns: list[str],
    log_hints: list[str],
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    for hint in log_hints:
        p = Path(hint)
        if p.is_absolute() and p.exists():
            key = str(p)
            if key not in seen:
                seen.add(key)
                candidates.append(p)

    for mount in mount_paths:
        if not mount.exists():
            continue
        for pattern in lock_patterns:
            for path in mount.rglob(pattern.lstrip("*") if pattern.startswith("*.") else pattern):
                if path.is_file():
                    key = str(path)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(path)
    return candidates[:20]


def lsof_check(path: Path) -> ActionRecord:
    result = subprocess.run(
        ["lsof", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ActionRecord(command=f"lsof {path}", result="no open handles or lsof unavailable")
    return ActionRecord(command=f"lsof {path}", result=(result.stdout or result.stderr).strip()[-1500:])


def remove_stale_lock(
    path: Path,
    backups_dir: Path,
    project: str,
    service: str,
    protected_globs: list[str],
    allow_protected_delete: bool = False,
) -> tuple[ActionRecord, bool]:
    if _is_protected(path, protected_globs) and not allow_protected_delete:
        backup = backup_file(path, backups_dir, project, service)
        return ActionRecord(
            command=f"protected file backup {path}",
            result=f"backed up to {backup}; delete requires approval",
        ), False
    if not path.exists():
        return ActionRecord(command=f"remove {path}", result="already absent"), True
    backup = backup_file(path, backups_dir, project, service)
    path.unlink()
    return ActionRecord(
        command=f"remove stale lock {path}",
        result=f"removed after backup {backup}",
    ), True


def write_pending_action(data_dir: Path, incident: Incident, action: str, paths: list[str]) -> Path:
    pending_dir = data_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "incident_id": incident.id,
        "project": incident.project,
        "service": incident.service,
        "issue": incident.issue,
        "action": action,
        "paths": paths,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    path = pending_dir / f"{incident.id}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


def apply_pending_if_approved(data_dir: Path, incident_id: str) -> bool:
    approval = data_dir / "approvals" / f"{incident_id}.approved"
    return approval.exists()


def remediate(
    health: ServiceHealth,
    project: ComposeProject,
    issue_summary: str,
    cfg: dict,
    data_dir: Path,
    log_hints: list[str] | None = None,
) -> RemediationResult:
    incident = Incident(
        project=health.project,
        service=health.service,
        issue=issue_summary,
        app_version=health.app_version,
    )
    log_hints = log_hints or []
    restart_key = f"{health.project}/{health.service}"
    max_restarts = int(cfg.get("max_restarts_per_hour", 5))
    automation_mode = cfg.get("automation_mode", "human_in_loop")
    protected_globs = cfg.get("protected_volume_globs", [])
    lock_patterns = cfg.get("lock_file_patterns", [])
    graceful_retries = int(cfg.get("graceful_retries_before_force", 2))

    if can_restart(data_dir, restart_key, max_restarts):
        action = graceful_restart(project, health.service)
        incident.actions.append(action)
        record_restart(data_dir, restart_key)
    else:
        incident.actions.append(
            ActionRecord(
                command="restart skipped",
                result=f"max {max_restarts} restarts/hour reached for {restart_key}",
            )
        )

    cid = container_id_for_service(project, health.service) or health.container_id
    lock_candidates: list[Path] = []
    if cid and ("lock" in issue_summary.lower() or log_hints):
        mounts = _inspect_mounts(cid)
        lock_candidates = find_candidate_lock_files(mounts, lock_patterns, log_hints)
        for candidate in lock_candidates:
            incident.actions.append(lsof_check(candidate))

    pending_paths: list[str] = []
    for candidate in lock_candidates:
        approved = apply_pending_if_approved(data_dir, incident.id)
        is_protected = _is_protected(candidate, protected_globs)
        if automation_mode == "full_auto" or approved:
            if is_protected and automation_mode != "full_auto" and not approved:
                pending_paths.append(str(candidate))
                incident.actions.append(
                    ActionRecord(
                        command=f"queue protected lock fix {candidate}",
                        result="awaiting approval via web UI or CLI",
                    )
                )
                continue
            action, _ = remove_stale_lock(
                candidate,
                data_dir / "backups",
                health.project,
                health.service,
                protected_globs,
                allow_protected_delete=automation_mode == "full_auto" or approved,
            )
            incident.actions.append(action)
        else:
            pending_paths.append(str(candidate))
            incident.actions.append(
                ActionRecord(
                    command=f"queue lock fix {candidate}",
                    result="awaiting approval via web UI or CLI",
                )
            )

    if pending_paths:
        write_pending_action(data_dir, incident, "remove_stale_locks", pending_paths)
        incident.outcome = "pending_approval"
        return RemediationResult(incident=incident, resolved=False, pending_approval=True)

    for _ in range(graceful_retries):
        if can_restart(data_dir, restart_key, max_restarts):
            action = graceful_restart(project, health.service)
            incident.actions.append(action)
            record_restart(data_dir, restart_key)

    if can_restart(data_dir, restart_key, max_restarts):
        incident.actions.append(force_restart(project, health.service))
        record_restart(data_dir, restart_key)

    incident.outcome = "auto_fix_attempted"
    return RemediationResult(incident=incident, resolved=False, pending_approval=False)
