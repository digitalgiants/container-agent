from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.config import ensure_data_dirs, load_config, load_secrets
from agent.discovery import discover_compose_projects, list_compose_services
from agent.email_sender import send_unresolved_alert
from agent.gemini_client import analyze_incident
from agent.health import evaluate_service, host_disk_low, host_oom_recent
from agent.incidents import IncidentStore
from agent.logs import extract_lock_hints, scan_log_issues, tail_service_logs
from agent.remediate import remediate

from agent.discovery import ComposeProject


def _re_evaluate(project: ComposeProject, service: str) -> bool:
    return len(evaluate_service(project, service).issues) == 0


def run_once() -> int:
    cfg = load_config()
    secrets = load_secrets()
    data_dir = Path(cfg["data_dir"])
    ensure_data_dirs(data_dir)
    store = IncidentStore(data_dir)

    host_issues: list[str] = []
    disk_issue = host_disk_low(int(cfg.get("disk_warn_percent_free", 15)))
    if disk_issue:
        host_issues.append(disk_issue)
    oom_issue = host_oom_recent()
    if oom_issue:
        host_issues.append(oom_issue)

    projects = discover_compose_projects(cfg["compose_search_paths"])
    if not projects:
        print("No compose projects found", file=sys.stderr)
        return 0

    log_patterns = cfg.get("log_error_patterns", [])
    tail_lines = int(cfg.get("log_tail_lines", 80))
    unresolved = 0

    for project in projects:
        services = list_compose_services(project)
        if not services:
            services = [project.name]

        for service in services:
            health = evaluate_service(project, service)
            logs = tail_service_logs(project, service, tail_lines)
            log_issues = scan_log_issues(logs, log_patterns)
            lock_hints = extract_lock_hints(logs)
            issues = list(health.issues) + log_issues + host_issues

            if not issues:
                continue

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
                print(f"Pending approval: {result.incident.id} ({health.project}/{health.service})")
                continue

            if _re_evaluate(project, service):
                result.incident.outcome = "resolved"
                store.append(result.incident)
                print(f"Resolved: {health.project}/{health.service}")
                continue

            root_cause, commands = analyze_incident(
                result.incident,
                logs,
                secrets.get("GEMINI_API_KEY", ""),
            )
            result.incident.root_cause = root_cause
            result.incident.recommended_commands = commands
            result.incident.outcome = "unresolved"
            store.append(result.incident)

            if send_unresolved_alert(result.incident, secrets):
                print(f"Emailed unresolved: {health.project}/{health.service}")
            else:
                print(f"Unresolved (email not configured): {health.project}/{health.service}", file=sys.stderr)
            unresolved += 1

    return 0 if unresolved == 0 else 1


def main() -> None:
    raise SystemExit(run_once())


if __name__ == "__main__":
    main()
