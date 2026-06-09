from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.podman_json import parse_podman_json_rows

COMPOSE_PROVIDER_MARKERS = (
    "looking up compose provider failed",
    "executable file not found in $PATH",
    "podman-compose",
    "docker-compose",
)


COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "vendor",
        ".local",
        ".cache",
        ".bun",
        "overlay",
    }
)


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


def _walk_compose_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name in SKIP_DIR_NAMES:
                        continue
                    stack.append(entry)
                elif entry.name in COMPOSE_FILENAMES:
                    yield entry
            except OSError:
                continue


def discover_compose_projects(search_paths: list[Path]) -> list[ComposeProject]:
    found: dict[Path, ComposeProject] = {}
    for root in search_paths:
        if not root.exists():
            continue
        for path in _walk_compose_files(root.resolve()):
            directory = path.parent.resolve()
            name = directory.name
            found[directory] = ComposeProject(name=name, compose_file=path, directory=directory)
    return sorted(found.values(), key=lambda p: str(p.directory))


def is_compose_provider_error(output: str) -> bool:
    lower = output.lower()
    return any(marker in lower for marker in COMPOSE_PROVIDER_MARKERS)


def compose_provider_available() -> bool:
    """Return False when `podman compose` cannot find a compose provider."""
    result = _run(["podman", "compose", "version"])
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and is_compose_provider_error(combined):
        return False
    return result.returncode == 0


def _load_compose_yaml(compose_file: Path) -> dict:
    try:
        data = yaml.safe_load(compose_file.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_compose_services(compose_file: Path) -> list[str]:
    services = _load_compose_yaml(compose_file).get("services")
    if not isinstance(services, dict):
        return []
    return sorted(str(name) for name in services.keys())


def _project_label_candidates(project: ComposeProject) -> list[str]:
    names = [project.name]
    compose_name = _load_compose_yaml(project.compose_file).get("name")
    if compose_name:
        names.append(str(compose_name))
    normalized = re.sub(r"[^a-z0-9]+", "", project.name.lower())
    if normalized:
        names.append(normalized)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _service_label_keys() -> tuple[tuple[str, str], ...]:
    return (
        ("com.docker.compose.project", "com.docker.compose.service"),
        ("io.podman.compose.project", "io.podman.compose.service"),
    )


def _container_rows_by_labels(project: ComposeProject, service: str | None = None) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    service_labels = ("com.docker.compose.service", "io.podman.compose.service")

    for project_key, _service_key in _service_label_keys():
        for project_label in _project_label_candidates(project):
            cmd = ["podman", "ps", "-a", "--filter", f"label={project_key}={project_label}", "--format", "json"]
            result = _run(cmd)
            if result.returncode != 0:
                continue
            for row in parse_podman_json_rows(result.stdout):
                cid = str(row.get("Id") or row.get("ID") or "").strip()
                if not cid or cid in seen_ids:
                    continue
                labels = row.get("Labels") or {}
                svc_name = ""
                for svc_label in service_labels:
                    if labels.get(svc_label):
                        svc_name = str(labels[svc_label])
                        break
                if service and svc_name and svc_name != service:
                    continue
                seen_ids.add(cid)
                row["_compose_service"] = svc_name
                rows.append(row)
    return rows


def _container_id_by_labels(project: ComposeProject, service: str) -> str | None:
    rows = _container_rows_by_labels(project, service=service)
    if not rows:
        return None
    for row in rows:
        labels = row.get("Labels") or {}
        for _, service_key in _service_label_keys():
            if labels.get(service_key) == service:
                return str(row.get("Id") or row.get("ID") or "").strip() or None
        if row.get("_compose_service") == service:
            return str(row.get("Id") or row.get("ID") or "").strip() or None
    return str(rows[0].get("Id") or rows[0].get("ID") or "").strip() or None


def list_compose_services(project: ComposeProject) -> list[str]:
    config_result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "config", "--services"],
        cwd=project.directory,
    )
    if config_result.returncode == 0 and config_result.stdout.strip():
        return sorted({s.strip() for s in config_result.stdout.splitlines() if s.strip()})

    parsed = _parse_compose_services(project.compose_file)
    if parsed:
        return parsed

    result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "ps", "--format", "json"],
        cwd=project.directory,
    )
    if result.returncode == 0:
        services: list[str] = []
        for row in parse_podman_json_rows(result.stdout):
            name = row.get("Service") or row.get("Names") or row.get("Name")
            if name:
                services.append(str(name).split(",")[0])
        if services:
            return sorted(set(services))

    label_services = sorted(
        {
            str(row.get("_compose_service"))
            for row in _container_rows_by_labels(project)
            if row.get("_compose_service")
        }
    )
    return label_services


def container_id_for_service(project: ComposeProject, service: str) -> str | None:
    result = _run(
        ["podman", "compose", "-f", str(project.compose_file), "ps", "-q", service],
        cwd=project.directory,
    )
    if result.returncode == 0:
        cid = result.stdout.strip().splitlines()
        if cid:
            return cid[0]

    return _container_id_by_labels(project, service)
