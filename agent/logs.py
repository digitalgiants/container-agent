from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent.discovery import ComposeProject, container_id_for_service


def tail_service_logs(project: ComposeProject, service: str, lines: int) -> str:
    result = subprocess.run(
        [
            "podman",
            "compose",
            "-f",
            str(project.compose_file),
            "logs",
            "--tail",
            str(lines),
            service,
        ],
        cwd=str(project.directory),
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return output

    cid = container_id_for_service(project, service)
    if cid:
        log_result = subprocess.run(
            ["podman", "logs", "--tail", str(lines), cid],
            capture_output=True,
            text=True,
            check=False,
        )
        if log_result.returncode == 0:
            return (log_result.stdout or "") + (log_result.stderr or "")

    return output


def scan_log_issues(log_text: str, patterns: list[str]) -> list[str]:
    issues: list[str] = []
    lower = log_text.lower()
    for pattern in patterns:
        if pattern.lower() in lower:
            issues.append(f"Log pattern matched: {pattern}")
    return issues


def extract_lock_hints(log_text: str) -> list[str]:
    hints: list[str] = []
    regexes = [
        r"([/\w.-]+\.(?:lock|pid|db|sqlite|wal|shm))",
        r"open\(['\"]([^'\"]+)['\"]",
    ]
    for rx in regexes:
        for match in re.findall(rx, log_text, flags=re.IGNORECASE):
            if match and match not in hints:
                hints.append(match)
    return hints[:10]
