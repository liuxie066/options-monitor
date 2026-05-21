from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_config
from src.application.runtime_config_paths import write_json_atomic
from src.application.write_contract import attach_write_contract


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"runtime config not found: {path}",
            hint="Pass --config-path explicitly, or create the canonical config with om setup / om config build.",
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


def _key_parts(key: str) -> list[str]:
    parts = [part.strip() for part in str(key or "").split(".")]
    if not parts or any(not part for part in parts):
        raise AgentToolError(code="INPUT_ERROR", message="config key must be a non-empty dot path")
    return parts


def _path_get(data: Any, parts: list[str]) -> tuple[bool, Any]:
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _path_set(data: Any, parts: list[str], value: Any) -> None:
    current = data
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                current[part] = {}
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                raise AgentToolError(code="INPUT_ERROR", message=f"config list index out of range: {part}")
            current = current[index]
            continue
        raise AgentToolError(code="INPUT_ERROR", message="config key can only traverse JSON objects or existing array indexes")

    leaf = parts[-1]
    if isinstance(current, dict):
        current[leaf] = value
        return
    if isinstance(current, list) and leaf.isdigit():
        index = int(leaf)
        if index < 0 or index >= len(current):
            raise AgentToolError(code="INPUT_ERROR", message=f"config list index out of range: {leaf}")
        current[index] = value
        return
    raise AgentToolError(code="INPUT_ERROR", message="config key parent must be a JSON object or existing array")


def _decode_set_value(*, value: str | None, json_value: str | None) -> Any:
    has_value = value is not None
    has_json = json_value is not None
    if has_value == has_json:
        raise AgentToolError(code="INPUT_ERROR", message="pass exactly one of --value or --json-value")
    if has_value:
        return value
    try:
        return json.loads(str(json_value))
    except JSONDecodeError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"--json-value is not valid JSON: {exc.msg}",
            details={"line": int(exc.lineno), "column": int(exc.colno), "position": int(exc.pos)},
        ) from exc


def _validate_runtime_config_payload(cfg: dict[str, Any]) -> None:
    try:
        validate_config(cfg)
    except SystemExit as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=str(exc),
            hint="The change was not written. Fix the config or preview a different value.",
        ) from exc


def get_runtime_config_value(
    *,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    key: str,
) -> dict[str, Any]:
    path = resolve_runtime_config_path(config_key=config_key, config_path=config_path)
    cfg = _read_json_object(path)
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
    }


def set_runtime_config_value(
    *,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    key: str,
    value: str | None = None,
    json_value: str | None = None,
    apply: bool = False,
    confirm: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    path = resolve_runtime_config_path(config_key=config_key, config_path=config_path)
    cfg = _read_json_object(path)
    parts = _key_parts(key)
    new_value = _decode_set_value(value=value, json_value=json_value)

    existed, old_value = _path_get(cfg, parts)
    mutated = deepcopy(cfg)
    _path_set(mutated, parts, new_value)
    _validate_runtime_config_payload(deepcopy(mutated))

    should_apply = bool(apply or confirm)

    backup_path: Path | None = None
    if should_apply:
        if backup:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            backup_path = path.with_name(f"{path.name}.bak.{stamp}")
            shutil.copy2(path, backup_path)
        write_json_atomic(path, mutated)

    return attach_write_contract(
        {
            "config_path": str(path),
            "key": key,
            "existed": bool(existed),
            "old_value": deepcopy(old_value) if existed else None,
            "new_value": deepcopy(new_value),
            "changed": (not existed) or old_value != new_value,
            "validated": True,
            "applied": should_apply,
        },
        dry_run=not should_apply,
        write_applied=should_apply,
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {path}" if backup_path else "rerun config set with the previous value",
    )


__all__ = [
    "get_runtime_config_value",
    "set_runtime_config_value",
]
