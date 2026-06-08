from __future__ import annotations

import requests

from agent.incidents import Incident


def _build_prompt(incident: Incident, log_excerpt: str) -> str:
    actions_text = "\n".join(f"- Ran: {a.command}\n  Result: {a.result}" for a in incident.actions)
    return f"""You are a Podman Compose operations assistant on RHEL Linux.

Container: {incident.project}/{incident.service}
Issue: {incident.issue}
App version: {incident.app_version or "unknown"}

Actions already taken:
{actions_text or "(none)"}

Recent logs:
{log_excerpt[-6000:]}

Respond in two sections:
ROOT_CAUSE: one short paragraph
COMMANDS: numbered list of shell commands to try next (podman compose only, no destructive rm on DB volumes without backup)
"""


def _parse_response(text: str) -> tuple[str, list[str]]:
    root_cause = text.strip()
    commands: list[str] = []
    if "COMMANDS:" in text:
        root_part, _, cmd_part = text.partition("COMMANDS:")
        root_cause = root_part.replace("ROOT_CAUSE:", "").strip()
        for line in cmd_part.splitlines():
            line = line.strip()
            if not line:
                continue
            if line[0].isdigit():
                line = line.split(".", 1)[-1].strip()
            if line.startswith("- "):
                line = line[2:]
            if line.startswith("`") and line.endswith("`"):
                line = line[1:-1]
            commands.append(line)
    return root_cause, commands


def ollama_available(ollama_url: str, timeout: float = 5.0) -> bool:
    try:
        response = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=timeout)
        return response.status_code == 200
    except requests.RequestException:
        return False


def analyze_incident(
    incident: Incident,
    log_excerpt: str,
    *,
    ollama_url: str,
    ollama_model: str,
    timeout_seconds: int = 180,
) -> tuple[str | None, list[str]]:
    base = ollama_url.rstrip("/")
    if not ollama_available(base):
        return "LLM analysis skipped: Ollama not reachable", []

    prompt = _build_prompt(incident, log_excerpt)
    try:
        response = requests.post(
            f"{base}/api/generate",
            json={
                "model": ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        text = str(payload.get("response", "")).strip()
    except requests.RequestException as exc:
        return f"LLM analysis failed: {exc}", []

    if not text:
        return "LLM analysis failed: empty response", []

    return _parse_response(text)
