from __future__ import annotations

import html
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from auth import (
    approve_path_with_token,
    authenticate,
    clear_session_cookie,
    create_session_cookie,
    get_session_user,
    is_public_path,
    require_user,
    verify_and_consume_approval_token,
)

DATA_DIR = Path(os.environ.get("CONTAINER_AGENT_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_VERSION = "0.2.1"
STATIC_ASSETS = {
    "app.js": "application/javascript",
    "style.css": "text/css",
}

app = FastAPI(title="Container Agent", version=APP_VERSION)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_public_path(path) or approve_path_with_token(path, request):
        return await call_next(request)
    if get_session_user(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


class LoginRequest(BaseModel):
    username: str
    password: str


def _static_status() -> dict[str, Any]:
    files = {name: (STATIC_DIR / name).is_file() for name in (*STATIC_ASSETS, "index.html")}
    return {
        "static_dir": str(STATIC_DIR),
        "files": files,
        "ok": all(files.values()),
    }


def _missing_static_page() -> str:
    status = _static_status()
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Container Agent — setup error</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#e8ecf4;background:#0c0e14}}</style>
</head><body>
<h1>UI not deployed correctly</h1>
<p>Static files are missing inside the container. Rebuild from a full git checkout:</p>
<pre>cd ~/container-agent
git pull
export CONTAINER_AGENT_DATA_DIR="$HOME/.local/share/container-agent"
podman compose -f compose/docker-compose.yml build --no-cache
podman compose -f compose/docker-compose.yml up -d --force-recreate
podman exec container-agent-ui ls -la /app/static/</pre>
<p>Expected files: <code>index.html</code>, <code>app.js</code>, <code>style.css</code></p>
<p>Status: <code>{html.escape(json.dumps(status))}</code></p>
<p><a href="/health">/health</a></p>
</body></html>"""


@app.on_event("startup")
def _startup_check() -> None:
    status = _static_status()
    if status["ok"]:
        print(f"container-agent-ui {APP_VERSION}: static files OK at {STATIC_DIR}")
    else:
        print(f"container-agent-ui {APP_VERSION}: WARNING missing static files: {status['files']}")


@app.get("/health")
def health() -> dict[str, Any]:
    static = _static_status()
    return {
        "status": "ok" if static["ok"] else "degraded",
        "version": APP_VERSION,
        **static,
        "data_dir": str(DATA_DIR),
        "data_dir_readable": os.access(DATA_DIR, os.R_OK),
    }


@app.get("/static/{asset}")
def static_asset(asset: str) -> FileResponse:
    media_type = STATIC_ASSETS.get(asset)
    if not media_type:
        raise HTTPException(status_code=404, detail="Not Found")
    path = STATIC_DIR / asset
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Missing static asset: {asset}")
    return FileResponse(path, media_type=media_type)


class SnoozeRequest(BaseModel):
    project: str
    service: str
    hours: float = Field(default=4, gt=0, le=168)
    reason: str = ""


def _read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


def _pending() -> list[dict]:
    pending_dir = DATA_DIR / "pending"
    if not pending_dir.exists():
        return []
    rows: list[dict] = []
    for path in sorted(pending_dir.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return rows


def _dismissed_ids() -> set[str]:
    dismissed_dir = DATA_DIR / "dismissed"
    if not dismissed_dir.exists():
        return set()
    return {p.stem for p in dismissed_dir.glob("*.dismissed")}


def _incidents(limit: int = 100) -> list[dict]:
    rows = _read_jsonl(DATA_DIR / "incidents.jsonl", limit=limit)
    dismissed = _dismissed_ids()
    if not dismissed:
        return rows
    return [r for r in rows if r.get("id") not in dismissed]


def _find_incident(incident_id: str) -> dict | None:
    path = DATA_DIR / "incidents.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text().splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id") == incident_id:
            return row
    return None


def _activity_page(limit: int = 50, offset: int = 0) -> tuple[list[dict], bool]:
    path = DATA_DIR / "activity.jsonl"
    if not path.exists():
        return [], False
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    page = rows[offset : offset + limit]
    has_more = offset + limit < len(rows)
    return page, has_more


def _heartbeat() -> dict | None:
    path = DATA_DIR / "heartbeat.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _snoozes() -> list[dict]:
    path = DATA_DIR / "state" / "snoozes.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    now = datetime.now(timezone.utc).timestamp()
    rows = []
    for entry in raw.values():
        until = float(entry.get("until", 0))
        if until <= now:
            continue
        row = dict(entry)
        row["remaining_seconds"] = int(until - now)
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("until", 0))


def _approval_history(limit: int = 50) -> list[dict]:
    return _read_jsonl(DATA_DIR / "approvals" / "history.jsonl", limit=limit)


def _file_info(path: Path) -> dict[str, Any]:
    info = {"path": str(path), "exists": path.exists(), "readable": False, "lines": 0}
    if not path.exists():
        return info
    info["readable"] = os.access(path, os.R_OK)
    try:
        info["lines"] = len(path.read_text().splitlines())
    except OSError:
        info["readable"] = False
    return info


def _display_name_lookup(services: list[dict]) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for svc in services:
        project = svc.get("project")
        service = svc.get("service")
        if project and service:
            lookup[(str(project), str(service))] = str(svc.get("display_name") or f"{project}/{service}")
    return lookup


def _top_issues(limit: int = 5, services: list[dict] | None = None) -> list[dict]:
    incidents = _incidents(limit=300)
    names = _display_name_lookup(services or [])
    tallies: dict[tuple[str, str], dict[str, Any]] = {}
    for row in incidents:
        project = str(row.get("project", ""))
        service = str(row.get("service", ""))
        if not project or not service:
            continue
        key = (project, service)
        entry = tallies.setdefault(
            key,
            {
                "project": project,
                "service": service,
                "display_name": names.get(key, f"{project}/{service}"),
                "count": 0,
                "latest_issue": row.get("issue", ""),
                "latest_outcome": row.get("outcome", ""),
                "latest_ts": row.get("ts", ""),
            },
        )
        entry["count"] += 1
    ranked = sorted(
        tallies.values(),
        key=lambda item: (-int(item["count"]), str(item.get("latest_ts", "")), str(item["project"])),
    )
    return ranked[:limit]


def _status() -> dict[str, Any]:
    incidents_path = DATA_DIR / "incidents.jsonl"
    activity_path = DATA_DIR / "activity.jsonl"
    heartbeat_path = DATA_DIR / "heartbeat.json"
    pending_dir = DATA_DIR / "pending"
    heartbeat = _heartbeat() or {}
    services = heartbeat.get("services", [])
    activity_info = _file_info(activity_path)
    agent_data_dir = heartbeat.get("data_dir")
    has_scan_data = bool(heartbeat.get("ts")) and activity_info["lines"] > 0
    return {
        "data_dir": str(DATA_DIR),
        "agent_data_dir": agent_data_dir,
        "data_dir_readable": os.access(DATA_DIR, os.R_OK),
        "has_scan_data": has_scan_data,
        "incidents_count": _file_info(incidents_path)["lines"],
        "activity_count": activity_info["lines"],
        "activity_readable": activity_info["readable"],
        "heartbeat_readable": _file_info(heartbeat_path)["readable"],
        "pending_count": len(list(pending_dir.glob("*.json"))) if pending_dir.exists() else 0,
        "snooze_count": len(_snoozes()),
        "heartbeat": heartbeat,
        "services": services,
        "top_issues": _top_issues(services=services),
    }


def _esc(value: object) -> str:
    return html.escape(str(value)) if value is not None else ""


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return _esc(ts)


def _page_shell(title: str, body: str, *, page: str = "other", extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/static/style.css">
{extra_head}
</head><body data-page="{_esc(page)}"><div class="page">{body}</div>
<script src="/static/app.js"></script>
</body></html>"""


def _do_approve(incident_id: str, approved_by: str = "web") -> dict:
    pending = DATA_DIR / "pending" / f"{incident_id}.json"
    if not pending.exists():
        raise HTTPException(status_code=404, detail="Pending action not found")
    payload = json.loads(pending.read_text())
    stamp = datetime.now(timezone.utc).isoformat()
    approvals = DATA_DIR / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / f"{incident_id}.approved").write_text(stamp)
    entry = {
        "incident_id": incident_id,
        "project": payload.get("project"),
        "service": payload.get("service"),
        "action": payload.get("action"),
        "approved_at": stamp,
        "approved_by": approved_by,
    }
    with (approvals / "history.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def _write_snooze(project: str, service: str, hours: float, reason: str = "") -> dict:
    import time

    key = f"{project}/{service}"
    until = time.time() + (hours * 3600)
    entry = {
        "project": project,
        "service": service,
        "until": until,
        "until_iso": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        "hours": hours,
        "reason": reason,
        "created_by": "web",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state_dir = DATA_DIR / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "snoozes.json"
    snoozes: dict = {}
    if path.exists():
        try:
            snoozes = json.loads(path.read_text())
            if not isinstance(snoozes, dict):
                snoozes = {}
        except json.JSONDecodeError:
            snoozes = {}
    now = time.time()
    snoozes = {k: v for k, v in snoozes.items() if float(v.get("until", 0)) > now}
    snoozes[key] = entry
    path.write_text(json.dumps(snoozes, indent=2))
    entry["remaining_seconds"] = int(until - now)
    return entry


def _clear_snooze(project: str, service: str) -> bool:
    key = f"{project}/{service}"
    path = DATA_DIR / "state" / "snoozes.json"
    if not path.exists():
        return False
    try:
        snoozes = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    if key not in snoozes:
        return False
    del snoozes[key]
    path.write_text(json.dumps(snoozes, indent=2))
    return True


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    login = STATIC_DIR / "login.html"
    if not login.is_file():
        raise HTTPException(status_code=503, detail="Login page missing")
    return HTMLResponse(login.read_text())


@app.post("/api/login")
def api_login(req: LoginRequest, request: Request) -> JSONResponse:
    if not authenticate(req.username, req.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response = JSONResponse({"status": "ok", "user": req.username.strip()})
    create_session_cookie(req.username.strip(), request, response)
    return response


@app.post("/api/logout")
def api_logout() -> JSONResponse:
    response = JSONResponse({"status": "logged_out"})
    clear_session_cookie(response)
    return response


@app.get("/api/me")
def api_me(user: str = Depends(require_user)) -> dict[str, str]:
    return {"user": user}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse(_missing_static_page(), status_code=503)
    return HTMLResponse(index.read_text())


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
def incident_page(incident_id: str, _user: str = Depends(require_user)) -> str:
    incident = _find_incident(incident_id)
    if not incident:
        body = f"""
        <div class="center-page">
          <div class="icon-lg">✕</div>
          <h1>Incident not found</h1>
          <p><a href="/">← Dashboard</a></p>
        </div>"""
        return _page_shell("Not found", body)

    actions_html = "".join(
        f"<li><code>{_esc(a.get('command'))}</code><br><span class='muted'>{_esc(a.get('result'))}</span></li>"
        for a in incident.get("actions") or []
    ) or "<li class='muted'>No actions recorded</li>"
    cmds_html = "".join(f"<li><code>{_esc(c)}</code></li>" for c in incident.get("recommended_commands") or [])
    body = f"""
    <header class="hero">
      <h1>{_esc(incident.get('project'))}/{_esc(incident.get('service'))}</h1>
      <p><span class="badge badge-{_esc(incident.get('outcome'))}">{_esc(incident.get('outcome'))}</span></p>
    </header>
    <section class="card detail-card">
      <div class="meta" data-ts="{_esc(incident.get('ts'))}">{_fmt_ts(incident.get('ts'))}</div>
      <p class="issue">{_esc(incident.get('issue'))}</p>
      <h3>Root cause</h3>
      <p class="detail">{_esc(incident.get('root_cause') or '—')}</p>
      <h3>Actions taken</h3>
      <ul class="detail-list">{actions_html}</ul>
      <h3>Recommended commands</h3>
      <ul class="detail-list">{cmds_html or "<li class='muted'>—</li>"}</ul>
      <div class="btn-row">
        <button class="btn-secondary snooze-btn" data-project="{_esc(incident.get('project'))}" data-service="{_esc(incident.get('service'))}">Snooze 4h</button>
        <a class="btn" href="/">← Dashboard</a>
      </div>
    </section>
    """
    return _page_shell(f"Incident {_esc(incident_id[:8])}", body, page="incident")


@app.get("/api/pending")
def api_pending(_user: str = Depends(require_user)) -> list[dict]:
    return _pending()


@app.get("/api/incidents")
def api_incidents(_user: str = Depends(require_user)) -> list[dict]:
    return _incidents()


@app.get("/api/incidents/{incident_id}")
def api_incident(incident_id: str, _user: str = Depends(require_user)) -> dict:
    row = _find_incident(incident_id)
    if not row:
        raise HTTPException(status_code=404, detail="Incident not found")
    return row


@app.get("/api/activity")
def api_activity(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user: str = Depends(require_user),
) -> dict[str, Any]:
    items, has_more = _activity_page(limit=limit, offset=offset)
    return {"items": items, "has_more": has_more, "offset": offset, "limit": limit}


@app.get("/api/status")
def api_status(_user: str = Depends(require_user)) -> dict:
    return _status()


@app.get("/api/snoozes")
def api_snoozes(_user: str = Depends(require_user)) -> list[dict]:
    return _snoozes()


@app.get("/api/approvals/history")
def api_approval_history(_user: str = Depends(require_user)) -> list[dict]:
    return _approval_history()


@app.post("/api/snooze")
def api_snooze(req: SnoozeRequest, _user: str = Depends(require_user)) -> dict:
    return _write_snooze(req.project, req.service, req.hours, req.reason)


@app.delete("/api/snooze/{project}/{service}")
def api_clear_snooze(project: str, service: str, _user: str = Depends(require_user)) -> dict:
    if not _clear_snooze(project, service):
        raise HTTPException(status_code=404, detail="Snooze not found")
    return {"status": "cleared", "project": project, "service": service}


@app.post("/api/approve/{incident_id}")
def approve_api(incident_id: str, user: str = Depends(require_user)) -> dict:
    return _do_approve(incident_id, approved_by=user)


# ---------------------------------------------------------------------------
# Container action queue — commands are written to disk and executed by the
# host agent on its next scan (or immediately if the systemd path unit is
# configured to watch DATA_DIR/scan_requested).
# ---------------------------------------------------------------------------

def _queue_action(action_type: str, project: str, service: str, requested_by: str) -> dict[str, Any]:
    action_id = str(uuid.uuid4())
    actions_dir = DATA_DIR / "actions"
    actions_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": action_id,
        "type": action_type,
        "project": project,
        "service": service,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": requested_by,
        "status": "pending",
    }
    (actions_dir / f"{action_id}.json").write_text(json.dumps(payload, indent=2))
    # Also touch the scan_requested trigger so the host watcher fires immediately.
    (DATA_DIR / "scan_requested").write_text(payload["requested_at"])
    return {
        "status": "queued",
        "action_id": action_id,
        "type": action_type,
        "project": project,
        "service": service,
        "message": "Action queued; the host agent will execute it on its next scan run.",
    }


@app.post("/api/scan")
def api_scan_request(user: str = Depends(require_user)) -> dict[str, Any]:
    """Signal the host agent to run an immediate scan cycle."""
    ts = datetime.now(timezone.utc).isoformat()
    (DATA_DIR / "scan_requested").write_text(ts)
    heartbeat = _heartbeat() or {}
    last_scan = heartbeat.get("ts")
    return {
        "status": "queued",
        "requested_at": ts,
        "last_scan": last_scan,
        "message": (
            "Scan trigger written. The agent will pick it up on its next scheduled run "
            "(≤5 min), or immediately if the systemd path unit is active."
        ),
    }


@app.get("/api/logs/{project}/{service}")
def api_logs(
    project: str,
    service: str,
    lines: int = Query(100, ge=10, le=500),
    _user: str = Depends(require_user),
) -> dict[str, Any]:
    """Return the most recent cached log tail for a service (written during each scan)."""
    log_path = DATA_DIR / "logs" / project / f"{service}.log"
    if not log_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No cached logs yet for this service. Logs are written during each scan cycle.",
        )
    all_lines = log_path.read_text().splitlines()
    return {
        "project": project,
        "service": service,
        "lines": all_lines[-lines:],
        "total_cached_lines": len(all_lines),
        "cached_at": datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


@app.get("/api/actions")
def api_list_actions(_user: str = Depends(require_user)) -> dict[str, Any]:
    """List pending and recently completed web-triggered actions."""
    actions_dir = DATA_DIR / "actions"
    done_dir = actions_dir / "done"
    pending: list[dict] = []
    done: list[dict] = []
    if actions_dir.exists():
        for f in sorted(actions_dir.glob("*.json")):
            try:
                pending.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
    if done_dir.exists():
        rows = []
        for f in sorted(done_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rows.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
        done = rows[:50]
    return {"pending": pending, "done": done}


@app.post("/api/start/{project}/{service}")
def api_start(project: str, service: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Queue a `podman compose up -d` for a stopped or missing service."""
    return _queue_action("start", project, service, user)


@app.post("/api/restart/{project}/{service}")
def api_restart(project: str, service: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Queue a graceful `podman compose restart` for a running service."""
    return _queue_action("restart", project, service, user)


@app.post("/api/stop/{project}/{service}")
def api_stop(project: str, service: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Queue a `podman compose stop` for a service."""
    return _queue_action("stop", project, service, user)


@app.post("/api/recover-stack/{project}")
def api_recover_stack(project: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Queue ordered `compose up -d` for every service in a project (dependencies first)."""
    return _queue_action("recover-stack", project, "", user)


@app.delete("/api/restart-counts/{project}/{service}")
def api_clear_restart_counts(
    project: str, service: str, _user: str = Depends(require_user)
) -> dict[str, Any]:
    """Clear the hourly restart rate-limit counter so the agent can restart again immediately."""
    path = DATA_DIR / "state" / "restart_counts.json"
    key = f"{project}/{service}"
    if not path.exists():
        return {"status": "ok", "message": f"No restart counts on record for {key}"}
    try:
        counts: dict = json.loads(path.read_text())
    except json.JSONDecodeError:
        counts = {}
    if key not in counts:
        return {"status": "ok", "message": f"No restart-count entry for {key}"}
    del counts[key]
    path.write_text(json.dumps(counts))
    return {"status": "cleared", "project": project, "service": service}


@app.delete("/api/pending/{incident_id}")
def api_cancel_pending(incident_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Cancel a pending approval action without applying it."""
    pending = DATA_DIR / "pending" / f"{incident_id}.json"
    if not pending.exists():
        raise HTTPException(status_code=404, detail="Pending action not found")
    try:
        payload = json.loads(pending.read_text())
    except json.JSONDecodeError:
        payload = {}
    pending.unlink()
    stamp = datetime.now(timezone.utc).isoformat()
    entry = {
        "incident_id": incident_id,
        "project": payload.get("project"),
        "service": payload.get("service"),
        "action": payload.get("action"),
        "cancelled_at": stamp,
        "cancelled_by": user,
        "outcome": "cancelled",
    }
    approvals_dir = DATA_DIR / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    with (approvals_dir / "history.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return {"status": "cancelled", "incident_id": incident_id}


@app.post("/api/dismiss/{incident_id}")
def api_dismiss(incident_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
    """Mark an unresolved incident as acknowledged so it stops surfacing on the dashboard."""
    dismissed_dir = DATA_DIR / "dismissed"
    dismissed_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    record = {"incident_id": incident_id, "dismissed_at": stamp, "dismissed_by": user}
    (dismissed_dir / f"{incident_id}.dismissed").write_text(json.dumps(record))
    return {"status": "dismissed", "incident_id": incident_id, "dismissed_at": stamp}


@app.get("/approve/{incident_id}", response_class=HTMLResponse)
def approve_page(incident_id: str, request: Request) -> str:
    token = request.query_params.get("token")
    session_user = get_session_user(request)
    if session_user:
        approved_by = session_user
    elif token and verify_and_consume_approval_token(incident_id, token):
        approved_by = "email-link"
    elif token:
        body = """
        <div class="center-page">
          <div class="icon-lg">✕</div>
          <h1>Invalid or expired link</h1>
          <p>This approval link is no longer valid. Log in to approve from the dashboard.</p>
          <p><a class="btn" href="/login">Sign in</a></p>
        </div>"""
        return _page_shell("Invalid link", body, page="approve")
    else:
        return RedirectResponse(url="/login", status_code=302)
    try:
        result = _do_approve(incident_id, approved_by=approved_by)
    except HTTPException:
        body = """
        <div class="center-page">
          <div class="icon-lg">✕</div>
          <h1>Not found</h1>
          <p>No pending action for this incident.</p>
          <p><a href="/">← Dashboard</a></p>
        </div>"""
        return _page_shell("Not found", body)

    body = f"""
    <div class="center-page">
      <div class="icon-lg">✓</div>
      <h1>Approved</h1>
      <p><strong>{_esc(result.get('project'))}/{_esc(result.get('service'))}</strong></p>
      <p>Approved at <span data-ts="{_esc(result['approved_at'])}">{_fmt_ts(result['approved_at'])}</span></p>
      <p class="muted">By {_esc(result.get('approved_by'))} · next scan applies the fix</p>
      <p style="margin-top:1.5rem"><a class="btn" href="/">← Dashboard</a></p>
    </div>
    """
    return _page_shell("Approved", body, page="approve")
