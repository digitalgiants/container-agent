from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DEFAULT_DATA_DIR, load_config, load_secrets


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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="container-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="Approve a pending remediation")
    approve.add_argument("incident_id")

    sub.add_parser("status", help="Show agent status")
    sub.add_parser("run", help="Run one monitoring cycle now")

    args = parser.parse_args(argv)
    if args.command == "approve":
        return cmd_approve(args.incident_id)
    if args.command == "status":
        return cmd_status()
    if args.command == "run":
        from agent.__main__ import run_once

        return run_once()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
