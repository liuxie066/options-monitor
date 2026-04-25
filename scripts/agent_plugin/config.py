from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.agent_plugin.contracts import AgentToolError
from scripts.config_loader import normalize_portfolio_broker_config


DEFAULT_CONFIGS = {
    "us": "config.us.json",
    "hk": "config.hk.json",
}


def repo_base() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_runtime_config_path(
    *,
    config_key: str | None = None,
    config_path: str | Path | None = None,
) -> Path:
    if config_path is not None and str(config_path).strip():
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        return path

    key = str(config_key or "").strip().lower()
    if key not in DEFAULT_CONFIGS:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="config_key must be us or hk when config_path is omitted",
        )

    return (repo_base() / DEFAULT_CONFIGS[key]).resolve()


def _absolutize_portfolio_data_config(cfg: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    out = deepcopy(cfg)
    portfolio = out.get("portfolio")
    if not isinstance(portfolio, dict):
        return out

    data_ref = portfolio.get("data_config")
    if data_ref is None or not str(data_ref).strip():
        return out

    data_path = Path(str(data_ref).strip()).expanduser()
    if not data_path.is_absolute():
        data_path = (config_path.parent / data_path).resolve()
    portfolio["data_config"] = str(data_path)
    return out


def load_runtime_config(
    *,
    config_key: str | None = None,
    config_path: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    path = resolve_runtime_config_path(config_key=config_key, config_path=config_path)
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"runtime config not found: {path.name}",
            hint="Create the repo-local config file or pass config_path explicitly.",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse runtime config: {path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(raw, dict):
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="runtime config must be a JSON object",
            details={"path": path.name},
        )
    cfg = _absolutize_portfolio_data_config(raw, config_path=path)
    cfg = normalize_portfolio_broker_config(cfg)
    cfg["config_source_path"] = str(path)
    return path, cfg


def resolve_output_root(output_dir: str | Path | None = None) -> Path:
    if output_dir is not None and str(output_dir).strip():
        path = Path(output_dir).expanduser()
        if not path.is_absolute():
            path = (repo_base() / path).resolve()
        return path
    env_dir = str(os.environ.get("OM_OUTPUT_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (repo_base() / "output" / "agent_plugin").resolve()


def write_tools_enabled() -> bool:
    raw = str(os.environ.get("OM_AGENT_ENABLE_WRITE_TOOLS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
