from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

from agent.caddy_domains import domain_for_port, load_port_domain_map
from agent.config import agent_subprocess_env
from agent.discovery import ComposeProject, container_id_for_service


def image_display_name(image: str) -> str:
    name = image.split("@", 1)[0]
    name = name.rsplit(":", 1)[0]
    return name.rsplit("/", 1)[-1] or name


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=agent_subprocess_env(),
    )


def compose_service_image(project: ComposeProject, service: str) -> str | None:
    result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "config", "--format", "json"],
        cwd=project.directory,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    svc = (data.get("services") or {}).get(service) or {}
    image = svc.get("image")
    if image:
        return str(image)
    build = svc.get("build")
    if isinstance(build, dict):
        return build.get("image") or service
    if isinstance(build, str):
        return service
    return service


def host_ports_for_container(container_id: str) -> list[int]:
    result = _run(["podman", "inspect", container_id, "--format", "{{json .NetworkSettings.Ports}}"])
    if result.returncode != 0:
        return []
    try:
        ports = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    host_ports: list[int] = []
    for bindings in ports.values():
        if not bindings:
            continue
        for binding in bindings:
            host_port = binding.get("HostPort")
            if host_port:
                host_ports.append(int(host_port))
    return sorted(set(host_ports))


@lru_cache(maxsize=1)
def _cached_port_map(
    search_tuple: tuple[str, ...],
    extra_tuple: tuple[str, ...],
) -> dict[int, list[str]]:
    search_paths = [Path(p) for p in search_tuple]
    return load_port_domain_map(search_paths, list(extra_tuple))


def resolve_display_name(
    project: ComposeProject,
    service: str,
    *,
    search_paths: list[Path] | None = None,
    caddyfile_paths: list[str] | None = None,
) -> str:
    search_tuple = tuple(str(p) for p in (search_paths or []))
    extra_tuple = tuple(caddyfile_paths or [])
    port_map = _cached_port_map(search_tuple, extra_tuple)

    cid = container_id_for_service(project, service)
    if cid:
        for port in host_ports_for_container(cid):
            domain = domain_for_port(port_map, port)
            if domain:
                return domain

    image = compose_service_image(project, service)
    if image:
        return image_display_name(image)
    return service


def clear_display_name_cache() -> None:
    _cached_port_map.cache_clear()
