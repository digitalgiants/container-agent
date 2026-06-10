from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from agent.discovery import ComposeProject, container_id_for_service


@dataclass
class ServiceHealth:
    project: str
    service: str
    compose_file: Path
    directory: Path
    container_id: str | None
    status: str
    restart_count: int
    health_status: str | None
    http_ok: bool | None
    app_version: str | None
    issues: list[str] = field(default_factory=list)


def _inspect(container_id: str) -> dict:
    result = subprocess.run(
        ["podman", "inspect", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
        return data[0] if data else {}
    except json.JSONDecodeError:
        return {}


def _published_http_port(inspect_data: dict) -> tuple[str, int] | None:
    ports = inspect_data.get("NetworkSettings", {}).get("Ports") or {}
    for container_port, bindings in ports.items():
        if not bindings:
            continue
        if "80/tcp" in container_port or "8080/tcp" in container_port or "8000/tcp" in container_port:
            host_port = int(bindings[0]["HostPort"])
            return "127.0.0.1", host_port
        if container_port.endswith("/tcp") and bindings:
            host_port = int(bindings[0]["HostPort"])
            return "127.0.0.1", host_port
    return None


def probe_http(inspect_data: dict, timeout: float = 5.0) -> tuple[bool | None, str | None]:
    endpoint = _published_http_port(inspect_data)
    if not endpoint:
        return None, None
    host, port = endpoint
    for path in ("/api/v1/health", "/health", "/"):
        url = f"http://{host}:{port}{path}"
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException:
            continue
        if resp.status_code >= 500:
            continue
        if resp.status_code >= 400:
            continue
        app_version = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                body = resp.json()
                app_version = body.get("app_version") or body.get("version")
            except ValueError:
                pass
        return True, app_version
    return False, None


def host_disk_low(warn_percent_free: int) -> str | None:
    usage = shutil.disk_usage("/")
    pct_free = (usage.free / usage.total) * 100
    if pct_free < warn_percent_free:
        return f"Host disk low: {pct_free:.1f}% free (threshold {warn_percent_free}%)"
    return None


def host_oom_recent() -> str | None:
    result = subprocess.run(
        ["journalctl", "-k", "--since", "1 hour ago", "--no-pager", "-g", "Out of memory"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return "Kernel OOM kill detected in last hour"
    return None


def evaluate_service(project: ComposeProject, service: str) -> ServiceHealth:
    cid = container_id_for_service(project, service)
    health = ServiceHealth(
        project=project.name,
        service=service,
        compose_file=project.compose_file,
        directory=project.directory,
        container_id=cid,
        status="missing",
        restart_count=0,
        health_status=None,
        http_ok=None,
        app_version=None,
    )
    if not cid:
        health.issues.append("Container not found for compose service")
        return health

    data = _inspect(cid)
    state = data.get("State", {})
    health.status = state.get("Status", "unknown")
    health.restart_count = int(state.get("RestartCount") or 0)
    hc = state.get("Health") or {}
    health.health_status = hc.get("Status")

    if health.status != "running":
        health.issues.append(f"Container status is {health.status}")
    if health.health_status == "unhealthy":
        health.issues.append("Compose healthcheck reports unhealthy")
    if health.restart_count >= 3:
        health.issues.append(f"High restart count: {health.restart_count}")

    http_ok, app_version = probe_http(data)
    health.http_ok = http_ok
    health.app_version = app_version
    if http_ok is False:
        health.issues.append("HTTP health probe failed")

    return health
