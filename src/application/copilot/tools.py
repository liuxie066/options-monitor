from __future__ import annotations

import json
from typing import Any

from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names, pure_read_toolsets
from src.application.copilot.contracts import safe_error_code
from src.application.tool_execution import execute_tool


MAX_SUMMARY_CHARS = 600
MAX_PREVIEW_ITEMS = 20
MAX_PREVIEW_DEPTH = 4


def available_read_tools(toolsets: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not toolsets:
        return tuple(sorted(pure_read_tool_names()))
    registry = pure_read_toolsets()
    names = {
        name
        for toolset in toolsets
        for name in registry.get(str(toolset), ())
    }
    return tuple(sorted(names))


def build_tool_payload(
    tool_name: str,
    scene_input: dict[str, Any],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    definition = get_tool_definition(tool_name)
    if definition is None or not definition.is_pure_read():
        return None, f"unsupported read-only tool: {tool_name}"
    payload = dict(definition.safe_default_input)
    static = (static_payloads or {}).get(tool_name)
    if isinstance(static, dict):
        payload.update(static)
    properties = definition.input_json_schema().get("properties")
    fields = set(properties) if isinstance(properties, dict) else set()
    for name in fields:
        if name not in scene_input:
            continue
        value = scene_input.get(name)
        if value is None:
            continue
        payload[name] = value.strip() if isinstance(value, str) else value
    return payload, None


def call_read_tool(tool_name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
    if tool_name not in allowed_tools:
        return _tool_error(tool_name, "POLICY_ERROR", "tool is outside the Host allowlist")
    definition = get_tool_definition(tool_name)
    if definition is None:
        return _tool_error(tool_name, "INPUT_ERROR", f"unknown tool: {tool_name}")
    if not definition.is_pure_read():
        return _tool_error(tool_name, "POLICY_ERROR", f"tool is not pure read-only: {tool_name}")
    return execute_tool(tool_name, payload)


def tool_descriptions(
    tool_names: list[str] | tuple[str, ...],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for name in tool_names:
        definition = get_tool_definition(name)
        if definition is None or not definition.is_pure_read():
            continue
        default_input = {
            **dict(definition.safe_default_input),
            **dict((static_payloads or {}).get(name) or {}),
        }
        output_contract = definition.resolve_output_contract(default_input)
        descriptions.append(
            {
                "name": definition.name,
                "description": _agent_description(definition.description, output_contract),
                "input_schema": definition.input_json_schema(),
                "default_input": default_input,
                "examples": [dict(item) for item in definition.examples[:3]],
                "capabilities": list(definition.capabilities),
                "output_contract": output_contract,
            }
        )
    return descriptions


def compact_observation(
    tool_name: str,
    response: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    definition = get_tool_definition(tool_name)
    output_contract = definition.resolve_output_contract(payload or {}) if definition is not None else {}
    ok = bool(response.get("ok")) if isinstance(response, dict) else False
    data = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else {}
    error = response.get("error") if isinstance(response, dict) and isinstance(response.get("error"), dict) else None
    warnings = [
        _clip(item, 320)
        for item in (response.get("warnings") or [])
        if isinstance(item, (str, int, float, bool)) and str(item).strip()
    ][:8] if isinstance(response, dict) else []
    if not ok or error:
        safe_error = _safe_error(error)
        return {
            "tool_name": tool_name,
            "ok": False,
            "error": str((safe_error or {}).get("code") or "TOOL_ERROR"),
            "message": str((safe_error or {}).get("message") or "tool failed"),
            **(
                {"hint": str((safe_error or {}).get("hint"))}
                if (safe_error or {}).get("hint")
                else {}
            ),
        }
    return {
        "tool_name": tool_name,
        "ok": True,
        "summary": _summary(tool_name, data, None, output_contract),
        "value": _preview(data),
        **({"warnings": warnings} if warnings else {}),
        "result_contract": _compact_output_contract(output_contract),
    }


def _summary(
    tool_name: str,
    data: dict[str, Any],
    error: dict[str, Any] | None,
    output_contract: dict[str, Any],
) -> str:
    if error:
        message = " ".join(str(error.get("message") or error.get("code") or "tool failed").split())
        return _clip(f"{tool_name} failed: {message}", MAX_SUMMARY_CHARS)
    primary_rows = str(output_contract.get("primary_rows") or "").strip()
    row_count_field = str(output_contract.get("row_count_field") or "").strip()
    row_count = data.get(row_count_field) if row_count_field else None
    if row_count is None and primary_rows and isinstance(data.get(primary_rows), list):
        row_count = len(data[primary_rows])
    source = str(output_contract.get("source_label") or "").strip()
    details = []
    if primary_rows:
        details.append(f"primary={primary_rows}")
    if row_count is not None:
        details.append(f"rows={row_count}")
    if source:
        details.append(f"source={source}")
    if not details:
        keys = ", ".join(sorted(str(key) for key in data)[:12])
        details.append(f"fields={keys}" if keys else "no data fields")
    return _clip(f"{tool_name} returned read-only data; " + "; ".join(details), MAX_SUMMARY_CHARS)


def _agent_description(description: str, output_contract: dict[str, Any]) -> str:
    compact = _compact_output_contract(output_contract)
    parts = [str(description).strip()]
    if compact.get("primary_rows"):
        parts.append(f"Returns primary collection `{compact['primary_rows']}`.")
    if compact.get("fact_fields"):
        parts.append("Key result fields: " + ", ".join(compact["fact_fields"]) + ".")
    if compact.get("source_label"):
        parts.append(f"Source: {compact['source_label']}.")
    return " ".join(part for part in parts if part)


def _compact_output_contract(output_contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output_contract, dict):
        return {}
    return {
        key: value
        for key, value in {
            "schema_version": output_contract.get("schema_version"),
            "primary_rows": output_contract.get("primary_rows"),
            "row_count_field": output_contract.get("row_count_field"),
            "fact_fields": list(output_contract.get("fact_fields") or ())[:16],
            "missing_data_fields": list(output_contract.get("missing_data_fields") or ())[:8],
            "source_label": output_contract.get("source_label"),
        }.items()
        if value not in (None, "", [])
    }


def _preview(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if depth >= MAX_PREVIEW_DEPTH:
        return _clip(json.dumps(value, ensure_ascii=False, default=str), 320)
    if isinstance(value, dict):
        keys = list(value)[:MAX_PREVIEW_ITEMS]
        result = {str(key): _preview(value[key], depth=depth + 1) for key in keys}
        if len(value) > len(keys):
            result["_truncated_keys"] = len(value) - len(keys)
        return result
    if isinstance(value, list):
        items = [_preview(item, depth=depth + 1) for item in value[:MAX_PREVIEW_ITEMS]]
        if len(value) > len(items):
            items.append({"_truncated_items": len(value) - len(items)})
        return items
    return str(value)


def _safe_error(error: dict[str, Any] | None) -> dict[str, str] | None:
    if not error:
        return None
    safe = {
        "code": safe_error_code(error.get("code"), default="TOOL_ERROR"),
        "message": _clip(error.get("message") or "tool failed", MAX_SUMMARY_CHARS),
    }
    for key in ("field", "hint", "reason"):
        value = error.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            safe[key] = _clip(value, 240)
    return safe


def _tool_error(tool_name: str, code: str, message: str) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "hint": "Choose an allowed pure-read tool and retry with arguments matching its schema.",
        },
    }


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


__all__ = [
    "available_read_tools",
    "build_tool_payload",
    "call_read_tool",
    "compact_observation",
    "tool_descriptions",
]
