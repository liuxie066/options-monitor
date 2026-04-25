from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.config_loader import (
    load_config as _load_runtime_pipeline_config,
    resolve_data_config_path,
    resolve_templates_config,
    resolve_watchlist_config,
    set_watchlist_config,
)
from scripts.agent_plugin.config import (
    load_runtime_config as _load_runtime_config,
    repo_base as _repo_base,
    resolve_output_root as _resolve_output_root,
    write_tools_enabled as _write_tools_enabled,
)
from scripts.validate_config import validate_config


def load_config(*, config_key: str | None = None, config_path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    return _load_runtime_config(config_key=config_key, config_path=config_path)


def load_runtime_config(*, config_key: str | None = None, config_path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    return load_config(config_key=config_key, config_path=config_path)


def load_runtime_pipeline_config(
    *,
    base: Path,
    config_path: Path,
    is_scheduled: bool,
    log,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    return _load_runtime_pipeline_config(
        base=base,
        config_path=config_path,
        is_scheduled=is_scheduled,
        log=log,
        validate_config_fn=validate_config,
        state_dir=state_dir,
    )


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


def repo_base() -> Path:
    return _repo_base()


def resolve_output_root(output_dir: str | Path | None = None) -> Path:
    return _resolve_output_root(output_dir)


def write_tools_enabled() -> bool:
    return _write_tools_enabled()


__all__ = [
    "load_config",
    "load_runtime_config",
    "load_runtime_pipeline_config",
    "validate_config",
    "validate_runtime_config",
    "load_raw_json",
    "repo_base",
    "resolve_output_root",
    "write_tools_enabled",
    "resolve_data_config_path",
    "resolve_templates_config",
    "resolve_watchlist_config",
    "set_watchlist_config",
]
