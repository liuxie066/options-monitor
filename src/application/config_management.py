from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.agent_plugin.config import load_runtime_config
from scripts.validate_config import validate_config


def load_config(*, config_key: str | None = None, config_path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    return load_runtime_config(config_key=config_key, config_path=config_path)


def validate_runtime_config(*, config_key: str | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    path, cfg = load_config(config_key=config_key, config_path=config_path)
    validate_config(dict(cfg))
    return {
        "ok": True,
        "config_path": str(path),
        "config_key": str(config_key or "").strip().lower() or None,
    }


def load_raw_json(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"[CONFIG_ERROR] config must be a JSON object: {target}")
    return payload

