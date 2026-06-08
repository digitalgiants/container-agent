from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DOMAIN_BLOCK_RE = re.compile(
    r"^([a-zA-Z0-9*._-]+(?:\.[a-zA-Z0-9*._-]+)+(?:\s*,\s*[a-zA-Z0-9*._-]+(?:\.[a-zA-Z0-9*._-]+)+)*)\s*\{",
    re.MULTILINE,
)
REVERSE_PROXY_RE = re.compile(
    r"reverse_proxy\s+(?:https?://)?(?:127\.0\.0\.1|localhost)(?::(\d+))?(?:\s|{|$)",
    re.IGNORECASE,
)
REVERSE_PROXY_PORT_ONLY_RE = re.compile(
    r"reverse_proxy\s+(?:https?://)?127\.0\.0\.1:(\d+)",
    re.IGNORECASE,
)


def _normalize_domains(raw: str) -> list[str]:
    domains: list[str] = []
    for part in raw.split(","):
        domain = part.strip()
        if not domain or domain == "*":
            continue
        if "." in domain or domain.endswith(".localhost"):
            domains.append(domain)
    return domains


def parse_caddyfile(text: str) -> dict[int, list[str]]:
    port_domains: dict[int, list[str]] = {}
    blocks = DOMAIN_BLOCK_RE.findall(text)
    for block_domains in blocks:
        domains = _normalize_domains(block_domains)
        if not domains:
            continue
        start = text.find(block_domains)
        if start < 0:
            continue
        brace = text.find("{", start)
        if brace < 0:
            continue
        depth = 0
        end = brace
        for idx in range(brace, len(text)):
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        body = text[brace : end + 1]
        ports: set[int] = set()
        for match in REVERSE_PROXY_PORT_ONLY_RE.finditer(body):
            ports.add(int(match.group(1)))
        for match in REVERSE_PROXY_RE.finditer(body):
            port = match.group(1)
            ports.add(int(port) if port else 80)
        for port in ports:
            port_domains.setdefault(port, [])
            for domain in domains:
                if domain not in port_domains[port]:
                    port_domains[port].append(domain)
    return port_domains


def _caddy_container_caddyfiles() -> list[Path]:
    result = subprocess.run(
        ["podman", "ps", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        image = str(row.get("Image", "")).lower()
        names = " ".join(row.get("Names") or []).lower()
        if "caddy" not in image and "caddy" not in names:
            continue
        cid = row.get("Id") or row.get("ID")
        if not cid:
            continue
        inspect = subprocess.run(
            ["podman", "inspect", cid, "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            continue
        try:
            mounts = json.loads(inspect.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for mount in mounts:
            source = mount.get("Source")
            if source and "caddy" in source.lower():
                path = Path(source)
                if path.is_file():
                    paths.append(path)
                elif path.is_dir():
                    paths.extend(sorted(path.glob("**/Caddyfile*")))
    return paths


def discover_caddyfile_paths(
    search_paths: list[Path] | None = None,
    extra_paths: list[str] | None = None,
) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = str(path.resolve())
        except OSError:
            return
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(path)

    for raw in extra_paths or []:
        add(Path(raw).expanduser())

    for default in (Path("/etc/caddy/Caddyfile"), Path.home() / "Caddyfile"):
        add(default)

    etc_d = Path("/etc/caddy")
    if etc_d.is_dir():
        for path in sorted(etc_d.glob("**/*")):
            if path.is_file() and ("caddy" in path.name.lower() or path.name == "Caddyfile"):
                add(path)

    for path in _caddy_container_caddyfiles():
        add(path)

    if search_paths:
        for root in search_paths:
            if not root.exists():
                continue
            for path in root.rglob("Caddyfile"):
                if path.is_file():
                    add(path)
            for path in root.rglob("Caddyfile.*"):
                if path.is_file():
                    add(path)

    return found


def load_port_domain_map(
    search_paths: list[Path] | None = None,
    extra_paths: list[str] | None = None,
) -> dict[int, list[str]]:
    merged: dict[int, list[str]] = {}
    for path in discover_caddyfile_paths(search_paths, extra_paths):
        try:
            parsed = parse_caddyfile(path.read_text())
        except OSError:
            continue
        for port, domains in parsed.items():
            merged.setdefault(port, [])
            for domain in domains:
                if domain not in merged[port]:
                    merged[port].append(domain)
    return merged


def domain_for_port(port_map: dict[int, list[str]], port: int) -> str | None:
    domains = port_map.get(port)
    if not domains:
        return None
    if len(domains) == 1:
        return domains[0]
    return domains[0]
