from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "container-agent"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "container-agent"


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


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
        secrets[key.strip()] = value.strip()
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
    return cfg


def ensure_data_dirs(data_dir: Path) -> None:
    for sub in ("pending", "approvals", "backups", "state"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
