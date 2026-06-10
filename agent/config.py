from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "container-agent"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "container-agent"


def agent_subprocess_env() -> dict[str, str]:
    """PATH for subprocesses so podman can find podman-compose (not in RHEL 10 repos)."""
    env = os.environ.copy()
    path_parts: list[str] = []
    local_bin = Path.home() / ".local" / "bin"
    if local_bin.is_dir():
        path_parts.append(str(local_bin))
    venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin"
    if venv_bin.is_dir():
        path_parts.append(str(venv_bin))
    existing = env.get("PATH", "")
    if existing:
        path_parts.append(existing)
    else:
        path_parts.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    env["PATH"] = ":".join(path_parts)
    return env


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _clean_secret(value: str) -> str:
    for char in ("\xa0", "\u200b", "\u200c", "\u200d", "\ufeff"):
        value = value.replace(char, "")
    return value.strip()


def load_secrets(secrets_path: Path | None = None) -> dict[str, str]:
    path = secrets_path or (DEFAULT_CONFIG_DIR / "secrets.env")
    if not path.exists():
        return {}
    secrets: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = _clean_secret(value)
    return secrets


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or (DEFAULT_CONFIG_DIR / "config.yaml")
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["compose_search_paths"] = [_expand(p) for p in cfg.get("compose_search_paths", [])]
    cfg.setdefault("data_dir", str(DEFAULT_DATA_DIR))
    cfg["data_dir"] = _expand(cfg["data_dir"])
    cfg.setdefault("ollama_url", "http://127.0.0.1:11434")
    cfg.setdefault("ollama_model", "qwen2.5:7b-instruct")
    cfg.setdefault("ollama_timeout_seconds", 180)
    return cfg


def ensure_data_dirs(data_dir: Path) -> None:
    for sub in ("pending", "approvals", "backups", "state"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
