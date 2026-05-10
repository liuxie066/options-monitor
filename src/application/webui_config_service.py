from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from src.application.config_validator import validate_config as validate_runtime_config
from src.application.runtime_config_paths import read_json_file, resolve_public_data_config_path, write_json_atomic


def runtime_config_path(config_key: str, filename: str, *, default_runtime_config_dir: Path) -> Path:
    env_key = f"OM_WEBUI_CONFIG_{config_key.upper()}"
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    env_dir = (os.environ.get("OM_WEBUI_CONFIG_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / filename
    return default_runtime_config_dir / filename


def resolve_config_path(path: Path, *, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def recommended_runtime_config_path(config_key: str, *, base_dir: Path) -> Path:
    filename = "config.hk.json" if str(config_key).strip().lower() == "hk" else "config.us.json"
    return (base_dir.parent / "options-monitor-config" / filename).resolve()


def uses_runtime_config_override(config_key: str) -> bool:
    env_key = f"OM_WEBUI_CONFIG_{str(config_key).strip().upper()}"
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit:
        return True
    env_dir = (os.environ.get("OM_WEBUI_CONFIG_DIR") or "").strip()
    return bool(env_dir)


def load_config(config_key: str, *, config_files: dict[str, Path], base_dir: Path) -> dict[str, Any]:
    if config_key not in config_files:
        raise HTTPException(status_code=400, detail=f"invalid configKey: {config_key}")
    path = resolve_config_path(config_files[config_key], base_dir=base_dir)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"config not found: {path}")
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="failed to parse config")
    return payload


def try_load_config(config_key: str, *, config_files: dict[str, Path], base_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return load_config(config_key, config_files=config_files, base_dir=base_dir), None
    except HTTPException as exc:
        return None, str(exc.detail)


def backup(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, bak)
    return bak


def validate_config(path: Path, *, base_dir: Path) -> None:
    config_path = resolve_config_path(path, base_dir=base_dir)
    try:
        payload = read_json_file(config_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to read config: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="config must be a JSON object")
    try:
        validate_runtime_config(dict(payload))
    except SystemExit as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"validate failed: {exc}") from exc


def require_token_for_write(req: Request) -> None:
    token = (os.environ.get("OM_WEBUI_TOKEN") or "").strip()
    if not token:
        return
    got = (req.headers.get("x-om-token") or "").strip()
    if got != token:
        raise HTTPException(status_code=401, detail="missing/invalid X-OM-Token")

def load_data_config_for_runtime(cfg: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_path = resolve_public_data_config_path({"config_path": str(config_path)}, portfolio, repo_base=lambda: config_path.parent)
    if not data_path.exists():
        return {}
    payload = read_json_file(data_path)
    return payload if isinstance(payload, dict) else {}


def write_validated_config(path: Path, cfg: dict[str, Any], *, base_dir: Path) -> None:
    bak = backup(path)
    try:
        write_json_atomic(path, cfg)
        validate_config(path, base_dir=base_dir)
    except HTTPException:
        shutil.copy2(bak, path)
        raise
    except Exception as exc:
        shutil.copy2(bak, path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
