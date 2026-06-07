from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

DATA_DIR = Path(os.environ.get("CONTAINER_AGENT_DATA_DIR", "/data"))

app = FastAPI(title="Container Agent", version="0.1.0")


def _read_jsonl(path: Path, limit: int = 50) -> list[dict]:
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


def _incidents(limit: int = 30) -> list[dict]:
    return _read_jsonl(DATA_DIR / "incidents.jsonl", limit=limit)


def _activity(limit: int = 50) -> list[dict]:
    return _read_jsonl(DATA_DIR / "activity.jsonl", limit=limit)


def _heartbeat() -> dict | None:
    path = DATA_DIR / "heartbeat.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _status() -> dict:
    incidents_path = DATA_DIR / "incidents.jsonl"
    activity_path = DATA_DIR / "activity.jsonl"
    pending_dir = DATA_DIR / "pending"
    return {
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "data_dir_readable": os.access(DATA_DIR, os.R_OK),
        "incidents_exists": incidents_path.exists(),
        "incidents_count": len(incidents_path.read_text().splitlines()) if incidents_path.exists() else 0,
        "activity_exists": activity_path.exists(),
        "activity_count": len(activity_path.read_text().splitlines()) if activity_path.exists() else 0,
        "pending_count": len(list(pending_dir.glob("*.json"))) if pending_dir.exists() else 0,
        "heartbeat": _heartbeat(),
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


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    pending = _pending()
    incidents = _incidents()
    activity = _activity()
    status = _status()
    heartbeat = status.get("heartbeat") or {}

    pending_html = "".join(
        f"""<li class="card pending">
          <div class="meta">{_fmt_ts(p.get('ts'))}</div>
          <div><b>{_esc(p.get('project'))}/{_esc(p.get('service'))}</b></div>
          <div class="issue">{_esc(p.get('issue'))}</div>
          <div class="detail">Action: {_esc(p.get('action'))}</div>
          <div class="detail">Paths: {_esc(', '.join(p.get('paths') or []))}</div>
          <a href='/approve/{_esc(p.get('incident_id'))}'>Approve {_esc((p.get('incident_id') or '')[:8])}</a>
        </li>"""
        for p in pending
    ) or "<li class='empty'>No pending approvals</li>"

    incident_html = "".join(
        f"""<li class="card incident outcome-{_esc(i.get('outcome'))}">
          <div class="meta">{_fmt_ts(i.get('ts'))} · <code>{_esc((i.get('id') or '')[:8])}</code></div>
          <div><b>{_esc(i.get('project'))}/{_esc(i.get('service'))}</b>
            <span class="badge">{_esc(i.get('outcome'))}</span></div>
          <div class="issue">{_esc(i.get('issue'))}</div>
          <div class="detail">{_esc((i.get('root_cause') or '')[:240])}</div>
        </li>"""
        for i in incidents
    ) or "<li class='empty'>No incidents yet</li>"

    activity_html = "".join(
        f"""<li class="activity {_esc(a.get('level', 'info'))}">
          <span class="meta">{_fmt_ts(a.get('ts'))}</span>
          <span class="msg">{_esc(a.get('message'))}</span>
          {f"<span class='svc'>{_esc(a.get('project'))}/{_esc(a.get('service'))}</span>" if a.get('project') else ""}
        </li>"""
        for a in activity
    ) or "<li class='empty'>No agent activity yet — run <code>container-agent run</code> once.</li>"

    last_scan = _fmt_ts(heartbeat.get("ts")) if heartbeat else "never"
    agent_data_dir = _esc(heartbeat.get("data_dir") or "unknown")

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Container Agent</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 980px; color: #1a1a1a; }}
h1 {{ margin-bottom: 0.2rem; }}
p.sub {{ color: #555; margin-top: 0; }}
section {{ margin-top: 1.75rem; }}
ul {{ list-style: none; padding: 0; margin: 0; }}
.card, .activity, .empty {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.85rem 1rem; margin-bottom: 0.65rem; }}
.meta {{ color: #666; font-size: 0.85rem; margin-bottom: 0.35rem; }}
.issue {{ margin: 0.35rem 0; }}
.detail {{ color: #444; font-size: 0.92rem; }}
.badge {{ font-size: 0.75rem; background: #eee; padding: 0.1rem 0.45rem; border-radius: 999px; margin-left: 0.35rem; }}
.outcome-unresolved .badge {{ background: #fde8e8; }}
.outcome-resolved .badge {{ background: #e5f6ea; }}
.outcome-pending_approval .badge {{ background: #fff4db; }}
.activity.warn {{ border-color: #f0c36d; background: #fffaf0; }}
.activity.error {{ border-color: #e57373; background: #fff5f5; }}
.activity .svc {{ color: #666; font-size: 0.85rem; margin-left: 0.5rem; }}
.diagnostics {{ font-size: 0.88rem; color: #444; background: #f7f7f7; padding: 0.75rem 1rem; border-radius: 8px; }}
a {{ color: #0b57d0; }}
code {{ font-size: 0.9em; }}
</style></head><body>
<h1>Container Agent</h1>
<p class="sub">Approve destructive fixes and review recent agent activity.</p>

<section>
  <h2>Agent activity</h2>
  <p class="sub">Non-actionable scan results: resolved services, emails sent, warnings, and scan summaries.</p>
  <ul>{activity_html}</ul>
</section>

<section><h2>Pending approvals</h2><ul>{pending_html}</ul></section>
<section><h2>Recent incidents</h2><ul>{incident_html}</ul></section>

<section>
  <h2>Diagnostics</h2>
  <div class="diagnostics">
    <div>UI data dir: <code>{_esc(status['data_dir'])}</code>
      ({'readable' if status['data_dir_readable'] else 'not readable'})</div>
    <div>Agent data dir (last scan): <code>{agent_data_dir}</code></div>
    <div>Last scan: {last_scan or 'never'}</div>
    <div>Files: incidents={status['incidents_count']}, activity={status['activity_count']}, pending={status['pending_count']}</div>
  </div>
</section>
</body></html>"""


@app.get("/api/pending")
def api_pending() -> list[dict]:
    return _pending()


@app.get("/api/incidents")
def api_incidents() -> list[dict]:
    return _incidents()


@app.get("/api/activity")
def api_activity() -> list[dict]:
    return _activity()


@app.get("/api/status")
def api_status() -> dict:
    return _status()


@app.post("/api/approve/{incident_id}")
@app.get("/approve/{incident_id}")
def approve(incident_id: str) -> dict:
    pending = DATA_DIR / "pending" / f"{incident_id}.json"
    if not pending.exists():
        raise HTTPException(status_code=404, detail="Pending action not found")
    approvals = DATA_DIR / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    (approvals / f"{incident_id}.approved").write_text(stamp)
    return {"incident_id": incident_id, "approved_at": stamp, "status": "approved"}
