from __future__ import annotations

import google.generativeai as genai

from agent.incidents import Incident


def analyze_incident(incident: Incident, log_excerpt: str, api_key: str) -> tuple[str | None, list[str]]:
    if not api_key:
        return None, []

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    actions_text = "\n".join(f"- Ran: {a.command}\n  Result: {a.result}" for a in incident.actions)
    prompt = f"""You are a Podman Compose operations assistant on RHEL Linux.

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

    try:
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"Gemini analysis failed: {exc}", []

    root_cause = text
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
