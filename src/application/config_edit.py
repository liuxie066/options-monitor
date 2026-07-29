from __future__ import annotations

import json
from copy import deepcopy
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import config_key_parts as _key_parts
from src.application.config_primitives import config_path_get as _path_get
from src.application.runtime_config_readiness import require_runtime_config_readiness


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"runtime config not found: {path}",
            hint="Pass --config-path explicitly, or create config.yaml with `om config init` and build runtime snapshots with `om config build --source yaml`.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse runtime config: {path}:{exc.lineno}:{exc.colno}",
            details={
                "error": str(exc),
                "line": int(exc.lineno),
                "column": int(exc.colno),
                "position": int(exc.pos),
            },
        ) from exc
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to read runtime config: {path}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"runtime config must be a JSON object: {path}")
    return payload


def get_runtime_config_value(
    *,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    key: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    path = resolve_runtime_config_path(config_key=config_key, config_path=config_path)
    cfg = _read_json_object(path)
    readiness = require_runtime_config_readiness(
        cfg,
        repo_root=Path(repo_root).expanduser() if repo_root is not None else Path.cwd(),
        runtime_config_path=path,
        config_key=config_key,
    )
    parts = _key_parts(key)
    exists, current = _path_get(cfg, parts)
    if not exists:
        raise AgentToolError(
            code="CONFIG_KEY_NOT_FOUND",
            message=f"runtime config key not found: {key}",
            details={"config_path": str(path), "key": key},
        )
    return {
        "config_path": str(path),
        "key": key,
        "exists": True,
        "value": deepcopy(current),
        "readiness": readiness,
    }


__all__ = [
    "get_runtime_config_value",
]
