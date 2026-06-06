from __future__ import annotations

import smtplib
import subprocess
from email.message import EmailMessage

from agent.incidents import Incident


def _via_smtplib(incident: Incident, secrets: dict[str, str]) -> bool:
    host = secrets.get("SMTP_HOST", "smtp.gmail.com")
    port = int(secrets.get("SMTP_PORT", "587"))
    user = secrets.get("SMTP_USER", "")
    password = secrets.get("SMTP_PASSWORD", "")
    recipient = secrets.get("ALERT_EMAIL", user)
    if not user or not password or not recipient:
        return False

    actions = "\n".join(f"  {i + 1}. {a.command} → {a.result}" for i, a in enumerate(incident.actions))
    commands = "\n".join(f"  - {c}" for c in incident.recommended_commands)
    body = f"""Container-agent unresolved incident

Container: {incident.project}/{incident.service}
Issue: {incident.issue}
App version: {incident.app_version or "n/a"}
Incident ID: {incident.id}

Actions taken:
{actions or "  (none)"}

Root cause:
{incident.root_cause or "unknown"}

Recommended commands:
{commands or "  (none)"}

Approve pending fix (if any):
  container-agent approve {incident.id}

Web UI: see config web_ui_url
"""

    msg = EmailMessage()
    msg["Subject"] = f"[container-agent] unresolved: {incident.project}/{incident.service}"
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


def _via_msmtp(incident: Incident, secrets: dict[str, str]) -> bool:
    recipient = secrets.get("ALERT_EMAIL")
    if not recipient:
        return False
    actions = "\n".join(f"{i + 1}. {a.command} -> {a.result}" for i, a in enumerate(incident.actions))
    body = (
        f"Container: {incident.project}/{incident.service}\n"
        f"Issue: {incident.issue}\n"
        f"App version: {incident.app_version or 'n/a'}\n"
        f"Incident ID: {incident.id}\n\n"
        f"Actions:\n{actions}\n\n"
        f"Root cause:\n{incident.root_cause or 'unknown'}\n\n"
        f"Commands:\n" + "\n".join(incident.recommended_commands)
    )
    result = subprocess.run(
        ["msmtp", recipient],
        input=body,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def send_unresolved_alert(incident: Incident, secrets: dict[str, str]) -> bool:
    if _via_msmtp(incident, secrets):
        return True
    return _via_smtplib(incident, secrets)
