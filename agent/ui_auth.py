from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def ui_auth_path(config_dir: Path) -> Path:
    return config_dir / "ui-auth.json"


def load_ui_auth(config_dir: Path) -> dict | None:
    path = ui_auth_path(config_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def write_ui_auth(config_dir: Path, username: str, password: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": username.strip(),
        "password_hash": hash_password(password),
        "updated_at": _utcnow().isoformat(),
    }
    path = ui_auth_path(config_dir)
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)


def ensure_session_secret(secrets_path: Path) -> str:
    lines: list[str] = []
    secret = None
    if secrets_path.exists():
        for line in secrets_path.read_text().splitlines():
            if line.startswith("UI_SESSION_SECRET="):
                secret = line.partition("=")[2].strip()
                if secret:
                    lines.append(line)
                    continue
            lines.append(line)
    if not secret:
        secret = secrets.token_urlsafe(48)
        lines.append(f"UI_SESSION_SECRET={secret}")
        secrets_path.write_text("\n".join(lines) + "\n")
        secrets_path.chmod(0o600)
    return secret


def _token_path(data_dir: Path, incident_id: str) -> Path:
    return data_dir / "approvals" / "tokens" / f"{incident_id}.json"


def create_approval_token(data_dir: Path, incident_id: str, *, hours: float = 72) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires = _utcnow() + timedelta(hours=hours)
    path = _token_path(data_dir, incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "incident_id": incident_id,
                "token_hash": token_hash,
                "expires_at": expires.isoformat(),
            }
        )
    )
    return token


def verify_and_consume_approval_token(data_dir: Path, incident_id: str, token: str) -> bool:
    path = _token_path(data_dir, incident_id)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    if _utcnow() > expires:
        path.unlink(missing_ok=True)
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if token_hash != payload.get("token_hash"):
        return False
    path.unlink(missing_ok=True)
    return True
