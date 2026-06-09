from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.activity import ActivityLog
from agent.config import ensure_data_dirs, load_config, load_secrets
from agent.discovery import (
    ComposeProject,
    compose_provider_available,
    discover_compose_projects,
    list_compose_services,
)
from agent.email_sender import send_approval_required_alert
from agent.ollama_client import analyze_incident
from agent.health import evaluate_service, host_disk_low, host_oom_recent
from agent.incidents import IncidentStore
from agent.logs import extract_lock_hints, scan_log_issues, tail_service_logs
from agent.remediate import (
    compose_start,
    compose_stop,
    graceful_restart,
    has_open_pending_for_service,
    remediate,
)
from agent.service_names import clear_display_name_cache, resolve_display_name
from agent.snooze import is_snoozed
from agent.ui_auth import create_approval_token


def _process_web_actions(
    data_dir: Path,
    projects: list[ComposeProject],
    activity: ActivityLog,
) -> None:
    """Execute queued actions written by the web UI and record results."""
    actions_dir = data_dir / "actions"
    if not actions_dir.exists():
        return
    done_dir = actions_dir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)

    project_map: dict[str, ComposeProject] = {p.name: p for p in projects}

    for action_file in sorted(actions_dir.glob("*.json")):
        try:
            payload = json.loads(action_file.read_text())
        except (json.JSONDecodeError, OSError):
            action_file.unlink(missing_ok=True)
            continue

        action_type = payload.get("type", "")
        project_name = str(payload.get("project", ""))
        service = str(payload.get("service", ""))
        project = project_map.get(project_name)

        if not project:
            result_msg = f"Project '{project_name}' not found in discovered stacks"
        elif action_type == "start":
            result_msg = compose_start(project, service).result
        elif action_type == "restart":
            result_msg = graceful_restart(project, service).result
        elif action_type == "stop":
            result_msg = compose_stop(project, service).result
        else:
            result_msg = f"Unknown action type: {action_type!r}"

        payload["status"] = "done"
        payload["result"] = result_msg
        payload["processed_at"] = datetime.now(timezone.utc).isoformat()

        (done_dir / action_file.name).write_text(json.dumps(payload, indent=2))
        action_file.unlink(missing_ok=True)

        activity.record(
            f"Web action '{action_type}' {project_name}/{service}: {result_msg[:120]}",
            category="web_action",
            project=project_name,
            service=service,
        )
        print(f"Web action: {action_type} {project_name}/{service}")


def _cache_service_logs(data_dir: Path, project: str, service: str, log_text: str) -> None:
    """Write the latest log tail to disk so the web UI can serve it."""
    log_dir = data_dir / "logs" / project
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{service}.log").write_text(log_text)


def _service_status(health, service_issues: list[str]) -> str:
    if not health.container_id or health.status != "running":
        return "critical"
    if health.health_status == "unhealthy":
        return "critical"
    if service_issues:
        return "warning"
    return "healthy"


def _re_evaluate(project: ComposeProject, service: str) -> bool:
    return len(evaluate_service(project, service).issues) == 0


def run_once() -> int:
    cfg = load_config()
    secrets = load_secrets()
    data_dir = Path(cfg["data_dir"])
    ensure_data_dirs(data_dir)
    store = IncidentStore(data_dir)
    activity = ActivityLog(data_dir)

    host_issues: list[str] = []
    disk_issue = host_disk_low(int(cfg.get("disk_warn_percent_free", 15)))
    if disk_issue:
        host_issues.append(disk_issue)
    oom_issue = host_oom_recent()
    if oom_issue:
        host_issues.append(oom_issue)
    if not compose_provider_available():
        host_issues.append(
            "podman compose provider missing — install podman-compose "
            "(sudo dnf install podman-compose). Monitoring uses podman label fallbacks; "
            "start/stop/restart actions still require compose."
        )

    projects = discover_compose_projects(cfg["compose_search_paths"])

    # Check for a web-UI-triggered scan request and acknowledge it.
    scan_trigger = data_dir / "scan_requested"
    if scan_trigger.exists():
        try:
            requested_at = scan_trigger.read_text().strip()
            scan_trigger.unlink(missing_ok=True)
            activity.record(
                f"Scan triggered via web UI (requested {requested_at})",
                category="scan",
            )
        except OSError:
            pass

    if not projects:
        msg = "No compose projects found"
        activity.record(msg, level="warn", category="scan")
        print(msg, file=sys.stderr)
        activity.write_heartbeat(
            {
                "data_dir": str(data_dir),
                "projects": 0,
                "pending": 0,
                "resolved": 0,
                "unresolved": 0,
                "services": [],
            }
        )
        return 0

    log_patterns = cfg.get("log_error_patterns", [])
    tail_lines = int(cfg.get("log_tail_lines", 80))
    unresolved = 0
    resolved = 0
    pending = 0
    service_health_rows: list[dict] = []

    clear_display_name_cache()
    caddyfile_paths = cfg.get("caddyfile_paths", [])
    compose_paths = cfg["compose_search_paths"]

    # Process any container actions queued through the web UI before the main scan.
    _process_web_actions(data_dir, projects, activity)

    activity.record(
        f"Scan started: {len(projects)} compose project(s)",
        category="scan",
    )
    for issue in host_issues:
        activity.record(issue, level="warn", category="host")

    for project in projects:
        services = list_compose_services(project)
        if not services:
            activity.record(
                f"No services found for compose project {project.name}",
                level="warn",
                category="scan",
                project=project.name,
            )
            continue

        for service in services:
            health = evaluate_service(project, service)
            logs = tail_service_logs(project, service, tail_lines)
            _cache_service_logs(data_dir, health.project, service, logs)
            log_issues = scan_log_issues(logs, log_patterns)
            lock_hints = extract_lock_hints(logs)
            service_issues = list(health.issues) + log_issues
            snoozed = is_snoozed(data_dir, health.project, health.service)
            display_name = resolve_display_name(
                project,
                health.service,
                search_paths=compose_paths,
                caddyfile_paths=caddyfile_paths,
            )
            service_health_rows.append(
                {
                    "project": health.project,
                    "service": health.service,
                    "display_name": display_name,
                    "status": _service_status(health, service_issues),
                    "snoozed": snoozed,
                    "issues": service_issues[:5],
                    "container_status": health.status,
                    "app_version": health.app_version,
                }
            )

            if not service_issues:
                continue
            if snoozed:
                activity.record(
                    "Skipped snoozed service",
                    category="snooze",
                    project=health.project,
                    service=health.service,
                )
                continue

            issues = service_issues + host_issues

            issue_summary = "; ".join(issues[:5])
            past = store.find_similar(health.project, health.service, issue_summary, limit=3)
            if past:
                issue_summary = f"{issue_summary} (seen {len(past)}x before)"

            result = remediate(
                health=health,
                project=project,
                issue_summary=issue_summary,
                cfg=cfg,
                data_dir=data_dir,
                log_hints=lock_hints,
            )

            if result.pending_approval:
                store.append(result.incident)
                pending += 1
                msg = f"Pending approval: {result.incident.id}"
                activity.record(
                    msg,
                    category="pending",
                    project=health.project,
                    service=health.service,
                    incident_id=result.incident.id,
                )
                print(f"{msg} ({health.project}/{health.service})")

                pending_payload: dict = {}
                pending_file = data_dir / "pending" / f"{result.incident.id}.json"
                if pending_file.exists():
                    try:
                        pending_payload = json.loads(pending_file.read_text())
                    except json.JSONDecodeError:
                        pending_payload = {}

                should_email = not has_open_pending_for_service(
                    data_dir,
                    health.project,
                    health.service,
                    except_incident_id=result.incident.id,
                )
                approve_token = create_approval_token(data_dir, result.incident.id) if should_email else None
                if should_email and send_approval_required_alert(
                    result.incident,
                    pending_payload,
                    secrets,
                    web_ui_url=str(cfg.get("web_ui_url", "http://127.0.0.1:8787")),
                    approve_token=approve_token,
                ):
                    activity.record(
                        "Emailed approval request",
                        category="email",
                        project=health.project,
                        service=health.service,
                        incident_id=result.incident.id,
                    )
                    print(f"Emailed approval required: {health.project}/{health.service}")
                continue

            if _re_evaluate(project, service):
                result.incident.outcome = "resolved"
                store.append(result.incident)
                resolved += 1
                msg = f"Resolved after remediation"
                activity.record(
                    msg,
                    category="resolved",
                    project=health.project,
                    service=health.service,
                    incident_id=result.incident.id,
                )
                print(f"Resolved: {health.project}/{health.service}")
                continue

            root_cause, commands = analyze_incident(
                result.incident,
                logs,
                ollama_url=str(cfg.get("ollama_url", "http://127.0.0.1:11434")),
                ollama_model=str(cfg.get("ollama_model", "qwen2.5:7b-instruct")),
                timeout_seconds=int(cfg.get("ollama_timeout_seconds", 180)),
            )
            result.incident.root_cause = root_cause
            result.incident.recommended_commands = commands
            result.incident.outcome = "unresolved"
            store.append(result.incident)
            activity.record(
                "Unresolved after auto-fix (logged, no email)",
                category="unresolved",
                project=health.project,
                service=health.service,
                incident_id=result.incident.id,
            )
            print(f"Unresolved (logged): {health.project}/{health.service}", file=sys.stderr)
            unresolved += 1

    activity.record(
        f"Scan complete: {resolved} resolved, {pending} pending, {unresolved} unresolved",
        category="scan",
    )
    activity.write_heartbeat(
        {
            "data_dir": str(data_dir),
            "projects": len(projects),
            "resolved": resolved,
            "pending": pending,
            "unresolved": unresolved,
            "services": service_health_rows,
        }
    )
    return 0 if unresolved == 0 else 1


def main() -> None:
    raise SystemExit(run_once())


if __name__ == "__main__":
    main()
