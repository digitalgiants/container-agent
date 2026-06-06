from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

DATA_DIR = Path(os.environ.get("CONTAINER_AGENT_DATA_DIR", "/data"))

app = FastAPI(title="Container Agent", version="0.1.0")


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
    path = DATA_DIR / "incidents.jsonl"
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


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    pending = _pending()
    incidents = _incidents()
    pending_html = "".join(
        f"<li><b>{p.get('project')}/{p.get('service')}</b> — {p.get('issue')} "
        f"<a href='/approve/{p.get('incident_id')}'>Approve</a></li>"
        for p in pending
    ) or "<li>No pending approvals</li>"
    incident_html = "".join(
        f"<li><code>{i.get('id','')[:8]}</code> {i.get('project')}/{i.get('service')} "
        f"— {i.get('outcome')} — {i.get('issue','')[:80]}</li>"
        for i in incidents
    ) or "<li>No incidents yet</li>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Container Agent</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 960px; }}
h1 {{ margin-bottom: 0.2rem; }}
section {{ margin-top: 1.5rem; }}
a {{ color: #0b57d0; }}
</style></head><body>
<h1>Container Agent</h1>
<p>Approve destructive fixes queued by the monitoring agent.</p>
<section><h2>Pending approvals</h2><ul>{pending_html}</ul></section>
<section><h2>Recent incidents</h2><ul>{incident_html}</ul></section>
</body></html>"""


@app.get("/api/pending")
def api_pending() -> list[dict]:
    return _pending()


@app.get("/api/incidents")
def api_incidents() -> list[dict]:
    return _incidents()


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
