from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from agent.approvals import log_approval
from agent.config import DEFAULT_CONFIG_DIR, load_config
from agent.discovery import discover_compose_projects
from agent.ollama_client import ollama_available
from agent.ui_auth import ensure_session_secret, load_ui_auth, write_ui_auth


def cmd_approve(incident_id: str) -> int:
    cfg = load_config()
    data_dir = Path(cfg["data_dir"])
    pending = data_dir / "pending" / f"{incident_id}.json"
    if not pending.exists():
        print(f"No pending action for incident {incident_id}", file=sys.stderr)
        return 1
    payload = json.loads(pending.read_text())
    log_approval(
        data_dir,
        incident_id=incident_id,
        project=payload.get("project"),
        service=payload.get("service"),
        action=payload.get("action"),
        approved_by="cli",
    )
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
    ollama_url = str(cfg.get("ollama_url", "http://127.0.0.1:11434"))
    ollama_model = str(cfg.get("ollama_model", "qwen2.5:7b-instruct"))
    ollama_up = ollama_available(ollama_url)
    print(f"ollama: {ollama_url} ({'up' if ollama_up else 'down'})")
    print(f"ollama_model: {ollama_model}")
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
        print(f"services in last scan: {len(payload.get('services') or [])}")
    compose_env = Path(__file__).resolve().parent.parent / "compose" / ".env"
    if compose_env.exists():
        print(f"ui compose env: {compose_env.read_text().strip()}")
    else:
        print("ui compose env: missing — run install.sh or: container-agent sync-ui", file=sys.stderr)
    return 0


def cmd_pull_llm() -> int:
    import subprocess

    cfg = load_config()
    model = str(cfg.get("ollama_model", "qwen2.5:7b-instruct"))
    url = str(cfg.get("ollama_url", "http://127.0.0.1:11434"))
    if not ollama_available(url):
        print("Ollama is not reachable. Start it with:", file=sys.stderr)
        print("  podman compose -f ~/container-agent/compose/docker-compose.yml up -d ollama", file=sys.stderr)
        return 1
    print(f"Pulling model {model}...")
    result = subprocess.run(
        ["podman", "exec", "container-agent-ollama", "ollama", "pull", model],
        check=False,
    )
    return result.returncode


def cmd_sync_ui() -> int:
    cfg = load_config()
    data_dir = cfg["data_dir"]
    config_dir = DEFAULT_CONFIG_DIR
    repo_dir = Path(__file__).resolve().parent.parent
    compose_env = repo_dir / "compose" / ".env"
    compose_env.write_text(
        f"CONTAINER_AGENT_DATA_DIR={data_dir}\nCONTAINER_AGENT_CONFIG_DIR={config_dir}\n"
    )
    print(f"Wrote {compose_env}")
    print("Recreate the UI container:")
    print(f"  podman compose -f {repo_dir}/compose/docker-compose.yml up -d --force-recreate")
    return 0


def cmd_set_ui_password() -> int:
    config_dir = DEFAULT_CONFIG_DIR
    existing = load_ui_auth(config_dir)
    default_user = (existing or {}).get("username", "")
    username = input(f"UI username [{default_user}]: ").strip() or default_user
    if not username:
        print("Username required", file=sys.stderr)
        return 1
    password = getpass.getpass("New UI password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match", file=sys.stderr)
        return 1
    if not password:
        print("Password required", file=sys.stderr)
        return 1
    write_ui_auth(config_dir, username, password)
    ensure_session_secret(config_dir / "secrets.env")
    print(f"Updated UI login for {username}")
    print("Recreate UI container: container-agent sync-ui && podman compose -f ~/container-agent/compose/docker-compose.yml up -d --force-recreate")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="container-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="Approve a pending remediation")
    approve.add_argument("incident_id")

    sub.add_parser("status", help="Show agent status")
    sub.add_parser("run", help="Run one monitoring cycle now")
    sub.add_parser("discover", help="List compose projects found under configured search paths")
    sub.add_parser("sync-ui", help="Write compose/.env so the web UI uses the same data_dir")
    sub.add_parser("set-ui-password", help="Set or change the web UI login password")
    sub.add_parser("pull-llm", help="Pull the configured Ollama model into the container")

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
    if args.command == "sync-ui":
        return cmd_sync_ui()
    if args.command == "set-ui-password":
        return cmd_set_ui_password()
    if args.command == "pull-llm":
        return cmd_pull_llm()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
