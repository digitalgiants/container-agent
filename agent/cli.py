from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.config import load_config
from agent.discovery import discover_compose_projects


def cmd_approve(incident_id: str) -> int:
    cfg = load_config()
    data_dir = Path(cfg["data_dir"])
    pending = data_dir / "pending" / f"{incident_id}.json"
    if not pending.exists():
        print(f"No pending action for incident {incident_id}", file=sys.stderr)
        return 1
    approvals = data_dir / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / f"{incident_id}.approved").write_text(datetime.now(timezone.utc).isoformat())
    print(f"Approved {incident_id}. Next agent run will apply queued fixes.")
    return 0


def cmd_discover() -> int:
    cfg = load_config()
    projects = discover_compose_projects(cfg["compose_search_paths"])
    if not projects:
        print("No compose projects found.")
        return 1
    for project in projects:
        print(f"{project.name}\t{project.compose_file}")
    print(f"\n{len(projects)} project(s) under {cfg['compose_search_paths']}")
    return 0


def cmd_status() -> int:
    cfg = load_config()
    data_dir = Path(cfg["data_dir"])
    pending_dir = data_dir / "pending"
    print(f"automation_mode: {cfg.get('automation_mode')}")
    print(f"data_dir: {data_dir}")
    pending = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    print(f"pending approvals: {len(pending)}")
    for path in pending:
        payload = json.loads(path.read_text())
        print(f"  - {payload.get('incident_id')}: {payload.get('project')}/{payload.get('service')}")
    incidents = data_dir / "incidents.jsonl"
    if incidents.exists():
        lines = incidents.read_text().splitlines()
        print(f"incidents logged: {len(lines)}")
    activity = data_dir / "activity.jsonl"
    if activity.exists():
        lines = activity.read_text().splitlines()
        print(f"activity logged: {len(lines)}")
    heartbeat = data_dir / "heartbeat.json"
    if heartbeat.exists():
        payload = json.loads(heartbeat.read_text())
        print(f"last scan: {payload.get('ts', 'unknown')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="container-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="Approve a pending remediation")
    approve.add_argument("incident_id")

    sub.add_parser("status", help="Show agent status")
    sub.add_parser("run", help="Run one monitoring cycle now")
    sub.add_parser("discover", help="List compose projects found under configured search paths")

    args = parser.parse_args(argv)
    if args.command == "approve":
        return cmd_approve(args.incident_id)
    if args.command == "status":
        return cmd_status()
    if args.command == "run":
        from agent.__main__ import run_once

        return run_once()
    if args.command == "discover":
        return cmd_discover()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
