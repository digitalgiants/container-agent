from __future__ import annotations

import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage

from agent.config import _clean_secret
from agent.incidents import Incident


def _via_smtplib(incident: Incident, secrets: dict[str, str]) -> bool:
    host = _clean_secret(secrets.get("SMTP_HOST", "smtp.gmail.com"))
    port = int(_clean_secret(secrets.get("SMTP_PORT", "587")))
    user = _clean_secret(secrets.get("SMTP_USER", ""))
    password = _clean_secret(secrets.get("SMTP_PASSWORD", ""))
    recipient = _clean_secret(secrets.get("ALERT_EMAIL", user))
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

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except (UnicodeEncodeError, smtplib.SMTPException, OSError) as exc:
        print(f"Email alert failed: {exc}", file=sys.stderr)
        return False
    return True


def _via_msmtp(incident: Incident, secrets: dict[str, str]) -> bool:
    recipient = secrets.get("ALERT_EMAIL")
    if not recipient or not shutil.which("msmtp"):
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
    try:
        if _via_msmtp(incident, secrets):
            return True
        return _via_smtplib(incident, secrets)
    except Exception as exc:  # noqa: BLE001
        print(f"Email alert failed: {exc}", file=sys.stderr)
        return False
