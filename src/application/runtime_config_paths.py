from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.application.settings import build_effective_env


def resolve_data_config_ref(payload: dict[str, Any], portfolio_cfg: dict[str, Any]) -> str | None:
    value = payload.get("data_config") or portfolio_cfg.get("data_config")
    raw = str(value or "").strip()
    if raw:
        return raw
    env_ref = str(build_effective_env().get("OM_DATA_CONFIG") or "").strip()
    return env_ref or None


def absolutize_portfolio_data_config(cfg: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    out = dict(cfg or {})
    portfolio = out.get("portfolio")
    if not isinstance(portfolio, dict):
        return out

    portfolio_out = dict(portfolio)
    data_ref = resolve_data_config_ref({}, portfolio_out)
    if not data_ref:
        out["portfolio"] = portfolio_out
        return out

    data_path = Path(data_ref).expanduser()
    if not data_path.is_absolute():
        data_path = (config_path.parent / data_path).resolve()
    portfolio_out["data_config"] = str(data_path)
    out["portfolio"] = portfolio_out
    return out


def resolve_public_data_config_path(
    payload: dict[str, Any],
    portfolio_cfg: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
) -> Path:
    raw = resolve_data_config_ref(payload, portfolio_cfg)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (repo_base() / path).resolve()
        return path
    config_path = str(payload.get("config_path") or "").strip()
    if config_path:
        runtime_path = Path(config_path).expanduser()
        if not runtime_path.is_absolute():
            runtime_path = runtime_path.resolve()
        return (runtime_path.parent / "portfolio.runtime.json").resolve()
    return (repo_base() / "portfolio.runtime.json").resolve()


def resolve_local_path(value: Any, *, default: Path, repo_base: Callable[[], Path]) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (repo_base() / path).resolve()
    return path


def read_json_object_or_empty(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
