from __future__ import annotations

from typing import Any

from src.application.agent_tool_registry import get_tool_definition


def resolve_output_contract(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    definition = get_tool_definition(str(tool_name or ""))
    if definition is None:
        return {}
    contract = definition.resolve_output_contract(dict(payload or {}))
    return contract if isinstance(contract, dict) else {}


def project_model_preview(data: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    fields = _model_preview_fields(output_contract)
    if not fields:
        return {}
    out: dict[str, Any] = {}
    for field_path in fields:
        _set_path(out, field_path, _read_path(data, field_path))
    return _compact(out)


def _model_preview_fields(output_contract: dict[str, Any]) -> list[str]:
    declared = [
        str(item).strip()
        for item in output_contract.get("model_preview_fields") or []
        if str(item).strip()
    ]
    if declared:
        return declared
    if str(output_contract.get("result_shape") or "").strip().lower() != "scalar":
        return []
    return [
        str(item).strip()
        for item in output_contract.get("fact_fields") or []
        if str(item).strip() and "[]" not in str(item)
    ]


def _read_path(data: dict[str, Any], field_path: str) -> Any:
    current: Any = data
    for part in field_path.split("."):
        if not part or not isinstance(current, dict):
            return None
        current = current.get(part)
    return _preview_value(current)


def _set_path(out: dict[str, Any], field_path: str, value: Any) -> None:
    if value in (None, "", [], {}):
        return
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        return
    current = out
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _preview_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _preview_value(item) for key, item in value.items() if not _hidden_key(str(key))}
    if isinstance(value, list):
        return [_preview_value(item) for item in value[:8]]
    if isinstance(value, tuple):
        return [_preview_value(item) for item in value[:8]]
    if isinstance(value, str):
        return _preview_text(value, limit=1000)
    return value


def _preview_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)].rstrip() + "...(truncated)"


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _hidden_key(key: str) -> bool:
    lowered = key.lower()
    return key.startswith("_") or any(token in lowered for token in ("sql", "path", "secret", "token", "password", "raw", "config"))


__all__ = ["project_model_preview", "resolve_output_contract"]
