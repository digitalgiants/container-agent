from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


@dataclass(frozen=True)
class ComposeProject:
    name: str
    compose_file: Path
    directory: Path


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def discover_compose_projects(search_paths: list[Path]) -> list[ComposeProject]:
    found: dict[Path, ComposeProject] = {}
    for root in search_paths:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.name in COMPOSE_FILENAMES and path.is_file():
                directory = path.parent.resolve()
                name = directory.name
                found[directory] = ComposeProject(name=name, compose_file=path, directory=directory)
    return sorted(found.values(), key=lambda p: str(p.directory))


def list_compose_services(project: ComposeProject) -> list[str]:
    config_result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "config", "--services"],
        cwd=project.directory,
    )
    if config_result.returncode == 0 and config_result.stdout.strip():
        return sorted({s.strip() for s in config_result.stdout.splitlines() if s.strip()})

    result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "ps", "--format", "json"],
        cwd=project.directory,
    )
    if result.returncode != 0:
        return []
    services: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Service") or row.get("Names") or row.get("Name")
        if name:
            services.append(str(name).split(",")[0])
    return sorted(set(services))


def container_id_for_service(project: ComposeProject, service: str) -> str | None:
    result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "ps", "-q", service],
        cwd=project.directory,
    )
    if result.returncode != 0:
        return None
    cid = result.stdout.strip().splitlines()
    return cid[0] if cid else None
