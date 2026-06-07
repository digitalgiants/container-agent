from __future__ import annotations

import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import Any

from agent.config import _clean_secret
from agent.incidents import Incident


def _build_approval_body(
    incident: Incident,
    pending: dict[str, Any],
    web_ui_url: str,
) -> str:
    paths = "\n".join(f"  - {p}" for p in pending.get("paths") or [])
    return f"""Container-agent approval required

A fix is queued and needs your approval before the agent can apply it.

Container: {incident.project}/{incident.service}
Issue: {incident.issue}
App version: {incident.app_version or "n/a"}
Incident ID: {incident.id}

Action: {pending.get("action", "unknown")}
Paths:
{paths or "  (none)"}

Approve in browser:
  {web_ui_url.rstrip("/")}/approve/{incident.id}

Or on the server:
  container-agent approve {incident.id}
"""


def _via_smtplib(subject: str, body: str, secrets: dict[str, str]) -> bool:
    host = _clean_secret(secrets.get("SMTP_HOST", "smtp.gmail.com"))
    port = int(_clean_secret(secrets.get("SMTP_PORT", "587")))
    user = _clean_secret(secrets.get("SMTP_USER", ""))
    password = _clean_secret(secrets.get("SMTP_PASSWORD", ""))
    recipient = _clean_secret(secrets.get("ALERT_EMAIL", user))
    if not user or not password or not recipient:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
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


def _via_msmtp(body: str, secrets: dict[str, str]) -> bool:
    recipient = secrets.get("ALERT_EMAIL")
    if not recipient or not shutil.which("msmtp"):
        return False
    result = subprocess.run(
        ["msmtp", recipient],
        input=body,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def send_approval_required_alert(
    incident: Incident,
    pending: dict[str, Any],
    secrets: dict[str, str],
    *,
    web_ui_url: str = "http://127.0.0.1:8787",
) -> bool:
    subject = f"[container-agent] approval required: {incident.project}/{incident.service}"
    body = _build_approval_body(incident, pending, web_ui_url)
    try:
        if _via_msmtp(body, secrets):
            return True
        return _via_smtplib(subject, body, secrets)
    except Exception as exc:  # noqa: BLE001
        print(f"Email alert failed: {exc}", file=sys.stderr)
        return False
