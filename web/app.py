from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("CONTAINER_AGENT_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Container Agent", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


def _incidents(limit: int = 100) -> list[dict]:
    return _read_jsonl(DATA_DIR / "incidents.jsonl", limit=limit)


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


def _activity(limit: int = 100) -> list[dict]:
    return _read_jsonl(DATA_DIR / "activity.jsonl", limit=limit)


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


def _status() -> dict[str, Any]:
    incidents_path = DATA_DIR / "incidents.jsonl"
    activity_path = DATA_DIR / "activity.jsonl"
    pending_dir = DATA_DIR / "pending"
    heartbeat = _heartbeat() or {}
    return {
        "data_dir": str(DATA_DIR),
        "data_dir_readable": os.access(DATA_DIR, os.R_OK),
        "incidents_count": len(incidents_path.read_text().splitlines()) if incidents_path.exists() else 0,
        "activity_count": len(activity_path.read_text().splitlines()) if activity_path.exists() else 0,
        "pending_count": len(list(pending_dir.glob("*.json"))) if pending_dir.exists() else 0,
        "snooze_count": len(_snoozes()),
        "heartbeat": heartbeat,
        "services": heartbeat.get("services", []),
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


@app.get("/", response_class=HTMLResponse)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
def incident_page(incident_id: str) -> str:
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
def api_pending() -> list[dict]:
    return _pending()


@app.get("/api/incidents")
def api_incidents() -> list[dict]:
    return _incidents()


@app.get("/api/incidents/{incident_id}")
def api_incident(incident_id: str) -> dict:
    row = _find_incident(incident_id)
    if not row:
        raise HTTPException(status_code=404, detail="Incident not found")
    return row


@app.get("/api/activity")
def api_activity() -> list[dict]:
    return _activity()


@app.get("/api/status")
def api_status() -> dict:
    return _status()


@app.get("/api/snoozes")
def api_snoozes() -> list[dict]:
    return _snoozes()


@app.get("/api/approvals/history")
def api_approval_history() -> list[dict]:
    return _approval_history()


@app.post("/api/snooze")
def api_snooze(req: SnoozeRequest) -> dict:
    return _write_snooze(req.project, req.service, req.hours, req.reason)


@app.delete("/api/snooze/{project}/{service}")
def api_clear_snooze(project: str, service: str) -> dict:
    if not _clear_snooze(project, service):
        raise HTTPException(status_code=404, detail="Snooze not found")
    return {"status": "cleared", "project": project, "service": service}


@app.post("/api/approve/{incident_id}")
def approve_api(incident_id: str, request: Request) -> dict:
    approved_by = request.headers.get("x-approved-by", "web-api")
    return _do_approve(incident_id, approved_by=approved_by)


@app.get("/approve/{incident_id}", response_class=HTMLResponse)
def approve_page(incident_id: str, request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-user") or request.headers.get("remote-user")
    approved_by = forwarded or "web"
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
