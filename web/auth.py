from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

CONFIG_DIR = Path(__import__("os").environ.get("CONTAINER_AGENT_CONFIG_DIR", "/config"))
DATA_DIR = Path(__import__("os").environ.get("CONTAINER_AGENT_DATA_DIR", "/data"))

SESSION_COOKIE = "ca_session"
SESSION_MAX_AGE = 30 * 60  # 30 minutes


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_secrets() -> dict[str, str]:
    path = CONFIG_DIR / "secrets.env"
    if not path.exists():
        return {}
    secrets: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip()
    return secrets


def _session_secret() -> str | None:
    return _load_secrets().get("UI_SESSION_SECRET")


def _serializer() -> URLSafeTimedSerializer | None:
    secret = _session_secret()
    if not secret:
        return None
    return URLSafeTimedSerializer(secret, salt="container-agent-ui")


def load_ui_auth() -> dict[str, Any] | None:
    path = CONFIG_DIR / "ui-auth.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def is_localhost(request: Request) -> bool:
    if request.client and request.client.host in {"127.0.0.1", "::1"}:
        return True
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host in {"127.0.0.1", "localhost"}


def cookie_secure(request: Request) -> bool:
    return not is_localhost(request)


def authenticate(username: str, password: str) -> bool:
    auth = load_ui_auth()
    if not auth:
        return False
    if username.strip() != auth.get("username"):
        return False
    return verify_password(password, str(auth.get("password_hash", "")))


def create_session_cookie(username: str, request: Request, response: Response) -> None:
    serializer = _serializer()
    if not serializer:
        raise HTTPException(status_code=503, detail="UI session secret not configured")
    token = serializer.dumps({"user": username})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def get_session_user(request: Request) -> str | None:
    serializer = _serializer()
    if not serializer:
        return None
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = serializer.loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = data.get("user")
    return str(user) if user else None


def require_user(request: Request) -> str:
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _token_path(incident_id: str) -> Path:
    return DATA_DIR / "approvals" / "tokens" / f"{incident_id}.json"


def verify_and_consume_approval_token(incident_id: str, token: str) -> bool:
    path = _token_path(incident_id)
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


def is_public_path(path: str) -> bool:
    if path in {"/health", "/login", "/api/login", "/api/logout"}:
        return True
    if path.startswith("/static/"):
        return True
    return False


def approve_path_with_token(path: str, request: Request) -> bool:
    return path.startswith("/approve/") and bool(request.query_params.get("token"))
