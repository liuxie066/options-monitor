from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_bot_config
from src.application.config_yaml import default_yaml_bot_config_path


def default_bot_config_path(*, repo_root: str | Path | None = None) -> Path:
    root = _repo_root(repo_root)
    repo_local = (root / "config.bot.json").resolve()
    if repo_local.exists():
        return repo_local
    return default_yaml_bot_config_path(repo_root=root)


def load_bot_config(
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    missing_ok: bool = False,
) -> tuple[Path, dict[str, Any]]:
    path = _resolve_path(config_path, default=default_bot_config_path(repo_root=repo_root))
    if not path.exists():
        if missing_ok:
            return path, {}
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"Bot config not found: {path}",
            hint="Run ./om config build-bot --source yaml, or pass --bot-config explicitly.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse Bot config: {path}:{exc.lineno}:{exc.colno}",
            details={"error": str(exc), "line": int(exc.lineno), "column": int(exc.colno)},
            hint="Fix the JSON syntax first.",
        ) from exc
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to read Bot config: {path}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"Bot config must be a JSON object: {path}")
    _validate_loaded_bot_config(payload, path=path)
    return path, payload


def _repo_root(raw: str | Path | None) -> Path:
    if raw is not None and str(raw).strip():
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[4]


def _resolve_path(raw: str | Path | None, *, default: Path) -> Path:
    if raw is None or not str(raw).strip():
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def _validate_loaded_bot_config(payload: dict[str, Any], *, path: Path) -> None:
    try:
        validate_bot_config(payload)
    except SystemExit as exc:
        message = str(exc)
        if message.startswith("[CONFIG_ERROR] "):
            message = message[len("[CONFIG_ERROR] "):]
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"Bot config validation failed: {path}",
            details={"error": message},
            hint="Fix config.bot.json or rebuild it with ./om config build-bot --source yaml.",
        ) from exc


__all__ = ["default_bot_config_path", "load_bot_config"]
