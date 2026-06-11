from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.discovery import ComposeProject
from agent.health import ServiceHealth, evaluate_service
from agent.incidents import ActionRecord
from agent.remediate import can_restart, compose_start, record_restart

DEFAULT_DEPENDENCY_LOG_PATTERNS = (
    "could not connect to server",
    "connection refused",
    "connection timed out",
    "waiting for postgres",
    "waiting for database",
    "dial tcp",
    "failed to connect",
    "name or service not known",
    "no such host",
)


def _load_compose_yaml(compose_file: Path) -> dict:
    try:
        data = yaml.safe_load(compose_file.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_depends_on(service_def: dict) -> list[str]:
    depends_on = service_def.get("depends_on")
    if depends_on is None:
        return []
    if isinstance(depends_on, list):
        return [str(name) for name in depends_on]
    if isinstance(depends_on, dict):
        return [str(name) for name in depends_on.keys()]
    return []


def compose_dependency_map(compose_file: Path) -> dict[str, list[str]]:
    services = _load_compose_yaml(compose_file).get("services")
    if not isinstance(services, dict):
        return {}
    dep_map: dict[str, list[str]] = {}
    for name, definition in services.items():
        if isinstance(definition, dict):
            dep_map[str(name)] = _parse_depends_on(definition)
    return dep_map


def compose_startup_order(compose_file: Path) -> list[str]:
    dep_map = compose_dependency_map(compose_file)
    if not dep_map:
        return []

    indegree = {
        name: len([dep for dep in deps if dep in dep_map])
        for name, deps in dep_map.items()
    }
    queue = sorted(name for name, count in indegree.items() if count == 0)
    ordered: list[str] = []

    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for name, deps in dep_map.items():
            if node in deps:
                indegree[name] -= 1
                if indegree[name] == 0:
                    queue.append(name)
                    queue.sort()

    for name in sorted(dep_map):
        if name not in ordered:
            ordered.append(name)
    return ordered


def service_has_healthcheck(compose_file: Path, service: str) -> bool:
    services = _load_compose_yaml(compose_file).get("services")
    if not isinstance(services, dict):
        return False
    definition = services.get(service)
    return isinstance(definition, dict) and bool(definition.get("healthcheck"))


def service_is_ready(health: ServiceHealth) -> bool:
    if health.status != "running":
        return False
    if health.health_status == "unhealthy":
        return False
    if health.health_status == "healthy":
        return True
    return health.health_status in (None, "starting")


def wait_for_service_ready(
    project: ComposeProject,
    service: str,
    *,
    timeout_seconds: int = 30,
    poll_seconds: float = 2.0,
) -> ActionRecord:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        health = evaluate_service(project, service)
        if service_is_ready(health):
            detail = health.health_status or health.status
            return ActionRecord(
                command=f"wait for {service}",
                result=f"ready ({detail})",
            )
        time.sleep(poll_seconds)
    health = evaluate_service(project, service)
    return ActionRecord(
        command=f"wait for {service}",
        result=f"timeout after {timeout_seconds}s (status={health.status}, health={health.health_status})",
    )


@dataclass(frozen=True)
class ServiceScan:
    service: str
    health: ServiceHealth
    issues: list[str]
    logs: str
    snoozed: bool


def _dependency_log_patterns(cfg: dict) -> tuple[str, ...]:
    patterns = list(DEFAULT_DEPENDENCY_LOG_PATTERNS)
    for pattern in cfg.get("log_error_patterns", []):
        lower = str(pattern).lower()
        if any(token in lower for token in ("connect", "connection", "refused", "postgres", "database")):
            patterns.append(str(pattern))
    return tuple(dict.fromkeys(patterns))


def _is_unhealthy_status(status: str) -> bool:
    return status in ("missing", "exited", "stopped", "created", "unknown") or status != "running"


def stack_recovery_plan(project: ComposeProject, scans: list[ServiceScan], cfg: dict) -> list[str]:
    order = compose_startup_order(project.compose_file)
    if len(order) < 2:
        return []

    dep_map = compose_dependency_map(project.compose_file)
    status_by_service = {scan.service: scan.health.status for scan in scans}
    needed: set[str] = set()
    dep_patterns = _dependency_log_patterns(cfg)

    for scan in scans:
        if scan.snoozed:
            continue
        service = scan.service
        if _is_unhealthy_status(scan.health.status) or scan.issues:
            needed.add(service)

        for dep in dep_map.get(service, []):
            dep_status = status_by_service.get(dep, "missing")
            if _is_unhealthy_status(dep_status):
                needed.add(dep)
                needed.add(service)

        log_lower = scan.logs.lower()
        if any(pattern.lower() in log_lower for pattern in dep_patterns):
            needed.add(service)
            for dep in dep_map.get(service, []):
                needed.add(dep)

    if not needed:
        return []

    expanded = set(needed)
    changed = True
    while changed:
        changed = False
        for service in list(expanded):
            for dep in dep_map.get(service, []):
                if dep not in expanded:
                    expanded.add(dep)
                    changed = True

    return [service for service in order if service in expanded]


def recover_compose_stack(
    project: ComposeProject,
    services: list[str],
    data_dir: Path,
    cfg: dict,
) -> tuple[list[ActionRecord], bool]:
    if not services:
        return [], False

    restart_key = f"{project.name}/__stack__"
    max_restarts = int(cfg.get("max_restarts_per_hour", 5))
    if not can_restart(data_dir, restart_key, max_restarts):
        return [
            ActionRecord(
                command="stack recovery skipped",
                result=f"max {max_restarts} restarts/hour reached for {restart_key}",
            )
        ], False

    wait_seconds = int(cfg.get("stack_recovery_wait_seconds", 30))
    actions: list[ActionRecord] = []

    for index, service in enumerate(services):
        actions.append(compose_start(project, service))
        if service_has_healthcheck(project.compose_file, service):
            actions.append(
                wait_for_service_ready(
                    project,
                    service,
                    timeout_seconds=wait_seconds,
                )
            )
        elif index < len(services) - 1:
            time.sleep(2)

    record_restart(data_dir, restart_key)
    return actions, True


def recover_project_stack(
    project: ComposeProject,
    data_dir: Path,
    cfg: dict,
) -> tuple[list[ActionRecord], bool]:
    services = compose_startup_order(project.compose_file)
    if not services:
        services = [
            name
            for name in (_load_compose_yaml(project.compose_file).get("services") or {}).keys()
        ]
    return recover_compose_stack(project, services, data_dir, cfg)
