from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from src.application.runtime_config_paths import read_json_file, write_json_atomic


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
    py = (base_dir / ".venv" / "bin" / "python").resolve()
    if not py.exists():
        raise HTTPException(status_code=500, detail="python venv not found; run ./run_webui.sh once")
    cmd = [str(py), "scripts/validate_config.py", "--config", str(path)]
    try:
        result = subprocess.run(cmd, cwd=str(base_dir), capture_output=True, text=True, timeout=30)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"validate failed to run: {exc}") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=(result.stderr.strip() or result.stdout.strip() or "validate failed"))


def require_token_for_write(req: Request) -> None:
    token = (os.environ.get("OM_WEBUI_TOKEN") or "").strip()
    if not token:
        return
    got = (req.headers.get("x-om-token") or "").strip()
    if got != token:
        raise HTTPException(status_code=401, detail="missing/invalid X-OM-Token")


def resolve_portfolio_data_config_path(cfg: dict[str, Any], *, config_path: Path) -> Path | None:
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    raw = portfolio.get("data_config")
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return path


def load_data_config_for_runtime(cfg: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    data_path = resolve_portfolio_data_config_path(cfg, config_path=config_path)
    if data_path is None or not data_path.exists():
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
