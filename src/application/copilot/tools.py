from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names, pure_read_toolsets
from src.application.copilot.contracts import safe_error_code
from src.application.tool_execution import execute_tool


MAX_SUMMARY_CHARS = 600
MAX_PREVIEW_ITEMS = 20
MAX_PREVIEW_DEPTH = 4

_COPILOT_HIDDEN_INPUT_NAMES = frozenset({"data_config"})
_COPILOT_HIDDEN_INPUT_SUFFIXES = ("_path", "_paths", "_dir", "_root")


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
    explicit_input: dict[str, Any],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
    fixed_input: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    definition = get_tool_definition(tool_name)
    if definition is None or not definition.is_pure_read():
        return None, f"unsupported read-only tool: {tool_name}"
    explicit_payload: dict[str, Any] = {}
    static = (static_payloads or {}).get(tool_name)
    if isinstance(static, dict):
        explicit_payload.update(static)
    properties = definition.input_json_schema().get("properties")
    fields = set(properties) if isinstance(properties, dict) else set()
    for name in fields:
        if name not in explicit_input:
            continue
        value = explicit_input.get(name)
        explicit_payload[name] = value.strip() if isinstance(value, str) else value
    if definition.copilot_input_normalizer is not None:
        try:
            explicit_payload = definition.copilot_input_normalizer(explicit_payload)
        except (TypeError, ValueError) as exc:
            return None, str(exc)
    payload = {
        name: value
        for name, value in definition.safe_default_input.items()
        if value is not None
    }
    payload.update(explicit_payload)
    for name in fields:
        if name not in (fixed_input or {}):
            continue
        value = (fixed_input or {}).get(name)
        if value in (None, ""):
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
            key: value
            for key, value in definition.safe_default_input.items()
            if value is not None
        }
        default_input.update(dict((static_payloads or {}).get(name) or {}))
        output_contract = definition.resolve_output_contract(default_input)
        descriptions.append(
            {
                "name": definition.name,
                "description": _agent_description(definition.description, output_contract),
                "input_schema": _copilot_input_schema(definition),
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
        code = str((safe_error or {}).get("code") or "TOOL_ERROR")
        return {
            "tool_name": tool_name,
            "ok": False,
            "status": "failed",
            "error": code,
            "code": code,
            "message": str((safe_error or {}).get("message") or "tool failed"),
            "retryable": bool((safe_error or {}).get("retryable", False)),
            **(
                {"hint": str((safe_error or {}).get("hint"))}
                if (safe_error or {}).get("hint")
                else {}
            ),
            **({"field": str((safe_error or {}).get("field"))} if (safe_error or {}).get("field") else {}),
            **({"details": (safe_error or {}).get("details")} if (safe_error or {}).get("details") else {}),
        }
    model_missing_fields = output_contract.get("model_missing_data_fields")
    has_model_missing_surface = bool(model_missing_fields) and any(
        _values_at_path(data, str(path).split("."))
        for path in model_missing_fields
    )
    missing_data = _contract_values(
        data,
        model_missing_fields if has_model_missing_surface else output_contract.get("missing_data_fields"),
        missing_only=True,
    )
    freshness = _freshness(data, output_contract)
    row_count = _row_count(data, output_contract)
    scope = _scope(data)
    coverage = _coverage(data)
    return {
        "tool_name": tool_name,
        "ok": True,
        "status": "partial" if missing_data or warnings else ("not_found" if row_count == 0 else "complete"),
        "summary": _summary(tool_name, data, None, output_contract),
        "value": _model_value(data, output_contract),
        "source": _source(data, output_contract),
        **({"scope": scope} if scope else {}),
        **({"coverage": coverage} if coverage else {}),
        **({"freshness": freshness} if freshness else {}),
        **({"missing_data": missing_data} if missing_data else {}),
        **({"warnings": warnings} if warnings else {}),
        "result_contract": _compact_output_contract(output_contract),
    }


def _copilot_input_schema(definition) -> dict[str, Any]:
    schema = deepcopy(definition.copilot_input_schema) if definition.copilot_input_schema else definition.input_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in definition.safe_default_input.items():
            if value is None:
                continue
            if name in properties and isinstance(properties[name], dict):
                properties[name].setdefault("default", deepcopy(value))
    if not isinstance(properties, dict):
        return schema
    allowed = set(definition.copilot_input_fields) if definition.copilot_input_fields else None
    visible = {
        name: value
        for name, value in properties.items()
        if (allowed is None or name in allowed) and not _is_hidden_copilot_input(name)
    }
    schema["properties"] = visible
    required = [name for name in schema.get("required") or [] if name in visible]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def _is_hidden_copilot_input(name: str) -> bool:
    return name in _COPILOT_HIDDEN_INPUT_NAMES or name.endswith(_COPILOT_HIDDEN_INPUT_SUFFIXES)


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


def _row_count(data: dict[str, Any], output_contract: dict[str, Any]) -> int | None:
    field = str(output_contract.get("row_count_field") or "").strip()
    value = data.get(field) if field else None
    if isinstance(value, int):
        return value
    primary = str(output_contract.get("primary_rows") or "").strip()
    rows = data.get(primary) if primary else None
    return len(rows) if isinstance(rows, list) else None


def _source(data: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    source = data.get("source")
    if isinstance(source, dict):
        return _preview(source)
    label = str(output_contract.get("source_label") or "").strip()
    return {"label": label} if label else {}


def _scope(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("scope") if isinstance(data.get("scope"), dict) else data.get("filters")
    return _preview(value) if isinstance(value, dict) else {}


def _coverage(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("coverage") if isinstance(data.get("coverage"), dict) else data.get("evidence_scope")
    return _preview(value) if isinstance(value, dict) else {}


def _freshness(data: dict[str, Any], output_contract: dict[str, Any]) -> Any:
    values = _contract_values(data, output_contract.get("freshness_fields"))
    if values:
        return values
    value = data.get("freshness")
    return _preview(value) if isinstance(value, (dict, list)) and value else {}


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


def _field_priorities(output_contract: dict[str, Any]) -> dict[str, list[str]]:
    priorities: dict[str, list[str]] = {}
    fields = [
        *(output_contract.get("model_preview_fields") or ()),
        *(output_contract.get("fact_fields") or ()),
        *(output_contract.get("freshness_fields") or ()),
        *(output_contract.get("missing_data_fields") or ()),
    ]
    for raw_path in fields:
        parts = [part for part in str(raw_path).split(".") if part]
        for index, part in enumerate(parts):
            parent = ".".join(parts[:index])
            key = part[:-2] if part.endswith("[]") else part
            priorities.setdefault(parent, [])
            if key not in priorities[parent]:
                priorities[parent].append(key)
    return priorities


def _model_value(data: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    fields = output_contract.get("model_value_fields")
    presentation_required = "presentation" in (fields or ())
    if fields and (not presentation_required or isinstance(data.get("presentation"), dict)):
        return _contract_values(data, fields, preview_max_depth=6)
    return _preview(data, priorities=_field_priorities(output_contract))


def _preview(
    value: Any,
    *,
    depth: int = 0,
    path: str = "",
    priorities: dict[str, list[str]] | None = None,
    max_depth: int = MAX_PREVIEW_DEPTH,
) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if depth >= max_depth:
        return _clip(json.dumps(value, ensure_ascii=False, default=str), 320)
    if isinstance(value, dict):
        preferred = list((priorities or {}).get(path, ()))
        keys = [key for key in preferred if key in value]
        keys.extend(key for key in value if key not in keys)
        keys = keys[:MAX_PREVIEW_ITEMS]
        result = {
            str(key): _preview(
                value[key],
                depth=depth + 1,
                path=f"{path}.{key}" if path else str(key),
                priorities=priorities,
                max_depth=max_depth,
            )
            for key in keys
        }
        if len(value) > len(keys):
            result["_truncated_keys"] = len(value) - len(keys)
        return result
    if isinstance(value, list):
        item_path = f"{path}[]"
        items = [
            _preview(
                item,
                depth=depth + 1,
                path=item_path,
                priorities=priorities,
                max_depth=max_depth,
            )
            for item in value[:MAX_PREVIEW_ITEMS]
        ]
        if len(value) > len(items):
            items.append({"_truncated_items": len(value) - len(items)})
        return items
    return str(value)


def _contract_values(
    data: dict[str, Any],
    paths: Any,
    *,
    missing_only: bool = False,
    preview_max_depth: int = MAX_PREVIEW_DEPTH,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_path in paths or ():
        path = str(raw_path or "").strip()
        if not path:
            continue
        values = _values_at_path(data, path.split("."))
        values = [value for value in values if value not in (None, "", [], {})]
        if missing_only:
            values = [value for value in values if _indicates_missing_data(value)]
        if values:
            out[path] = _preview(
                values[0] if len(values) == 1 else values,
                max_depth=preview_max_depth,
            )
    return out


def _indicates_missing_data(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (list, dict)):
        return bool(value)
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in {
        "missing",
        "not_found",
        "not_observed",
        "not_reported",
        "not_evaluable",
        "unavailable",
        "unknown",
        "stale",
        "partial",
        "incomplete",
    } or normalized.startswith(("missing_", "unavailable_", "not_observed_", "not_reported_"))


def _values_at_path(value: Any, parts: list[str]) -> list[Any]:
    if not parts:
        return [value]
    part = parts[0]
    is_list = part.endswith("[]")
    key = part[:-2] if is_list else part
    if not isinstance(value, dict) or key not in value:
        return []
    child = value[key]
    if is_list:
        if not isinstance(child, list):
            return []
        return [item for child_item in child for item in _values_at_path(child_item, parts[1:])]
    return _values_at_path(child, parts[1:])


def _safe_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
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
    details = error.get("details")
    if isinstance(details, dict):
        safe_details = {
            key: _preview(value)
            for key, value in details.items()
            if key in {"allowed_views", "unknown_views", "first_keyword", "mode", "schema_errors", "tool_name"}
        }
        if safe_details:
            safe["details"] = safe_details
    explicit_retryable = error.get("retryable")
    if not isinstance(explicit_retryable, bool) and isinstance(details, dict):
        explicit_retryable = details.get("retryable")
    safe["retryable"] = (
        explicit_retryable
        if isinstance(explicit_retryable, bool)
        else safe["code"] in {"INPUT_ERROR", "READ_ERROR", "INTERNAL_ERROR", "TOOL_ERROR"}
    )
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
