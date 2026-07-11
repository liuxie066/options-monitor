from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping

from src.application.agent_tool_contracts import AgentToolError


TOOL_ARGUMENT_JSON_SCHEMA_KEYS = frozenset(
    {
        "type",
        "enum",
        "items",
        "properties",
        "additionalProperties",
        "default",
        "description",
        "examples",
        "format",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
        "pattern",
        "propertyNames",
    }
)

_BOOL_ARGUMENT_NAMES = frozenset(
    {
        "allow_downgrade",
        "apply",
        "compare_baseline",
        "confirm",
        "dry_run",
        "force",
        "include_rows",
        "include_service_status",
        "include_snapshot",
        "no_context",
        "no_exchange_rates",
        "refresh_quotes",
        "scanned_only",
    }
)

_INTEGER_ARGUMENT_NAMES = frozenset(
    {
        "as_of_ms",
        "audit_scan_limit",
        "candidate_evidence_min_sample",
        "exp_within_days",
        "limit",
        "limit_expirations",
        "lines",
        "max_notification_chars",
        "max_run_age_minutes",
        "opend_port",
        "opend_telnet_port",
        "timeout_sec",
        "timeoutSeconds",
        "top",
        "top_n",
        "ttl_sec",
    }
)


def build_tool_input_json_schema(
    input_schema: Mapping[str, Any],
    *,
    additional_properties: bool | dict[str, Any] = False,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw_key, raw_value in input_schema.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        description = tool_argument_description(raw_value)
        properties[key] = tool_argument_schema_from_value(key=key, value=raw_value, description=description)
        if tool_argument_declares_required(raw_value):
            required.append(key)
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": deepcopy(additional_properties),
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def tool_argument_description(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("description") or "").strip()
    return str(value or "").strip()


def tool_argument_declares_required(value: Any) -> bool:
    return isinstance(value, dict) and value.get("required") is True


def tool_argument_schema_from_value(*, key: str, value: Any, description: str) -> dict[str, Any]:
    if isinstance(value, dict):
        schema = _explicit_json_schema(value)
        if schema:
            return schema
    return _tool_argument_json_schema(key=key, description=description)


def provider_compatible_argument_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(schema)
    raw_type = out.get("type")
    if isinstance(raw_type, list):
        non_null_types = [item for item in raw_type if item != "null"]
        if len(non_null_types) == 1:
            out["type"] = non_null_types[0]
    raw_enum = out.get("enum")
    if isinstance(raw_enum, list):
        enum_values = [item for item in raw_enum if item is not None]
        if enum_values:
            out["enum"] = enum_values
        else:
            out.pop("enum", None)
    properties = out.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {
            str(prop_name): provider_compatible_argument_schema(prop_schema)
            if isinstance(prop_schema, dict)
            else deepcopy(prop_schema)
            for prop_name, prop_schema in properties.items()
        }
    additional = out.get("additionalProperties")
    if isinstance(additional, dict):
        out["additionalProperties"] = provider_compatible_argument_schema(additional)
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = provider_compatible_argument_schema(items)
    return out


def validate_tool_input_payload(
    *,
    tool_name: str,
    payload: Any,
    schema: dict[str, Any],
    enforce_required: bool = False,
) -> None:
    errors = list(_validate_value(payload, schema, path="", enforce_required=enforce_required))
    if not errors:
        return
    first = errors[0]
    path = str(first.get("path") or "<root>")
    expected = str(first.get("expected") or "valid input")
    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"{tool_name} input does not match tool schema at {path}: expected {expected}",
        hint=_schema_repair_hint(first),
        details={"tool_name": tool_name, "schema_errors": errors[:10]},
    )


def _schema_repair_hint(error: Mapping[str, Any]) -> str:
    path = str(error.get("path") or "<root>")
    expected = str(error.get("expected") or "valid input")
    actual = str(error.get("actual") or "invalid value")
    if expected == "required property" or actual == "missing":
        return f"Add `{path}` using the type and meaning described in the tool schema, then retry."
    if expected == "declared property" or actual == "unexpected property":
        return f"Remove unsupported field `{path}` and retry using only fields declared by the tool schema."
    if expected.startswith("one of "):
        return f"Set `{path}` to {expected} and retry."
    return f"Set `{path}` to {expected} instead of {actual}, then retry."


def _explicit_json_schema(value: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if key == "required":
            if isinstance(item, list):
                schema[key] = [str(raw_item) for raw_item in item if str(raw_item).strip()]
            continue
        if key not in TOOL_ARGUMENT_JSON_SCHEMA_KEYS or item in (None, "", [], {}):
            continue
        if key == "properties" and isinstance(item, dict):
            schema[key] = {
                str(prop_name): _explicit_json_schema(prop_schema)
                if isinstance(prop_schema, dict)
                else deepcopy(prop_schema)
                for prop_name, prop_schema in item.items()
            }
        elif key in {"items", "additionalProperties", "propertyNames"} and isinstance(item, dict):
            schema[key] = _explicit_json_schema(item)
        else:
            schema[key] = deepcopy(item)
    return schema


def _tool_argument_json_schema(*, key: str, description: str) -> dict[str, Any]:
    lowered = f"{key} {description}".lower()
    tokens = _tool_argument_tokens(lowered)
    enum = _tool_argument_enum(description)
    if enum:
        schema: dict[str, Any] = {"type": "string", "enum": enum}
    elif key in _BOOL_ARGUMENT_NAMES or "bool" in tokens or "boolean" in tokens:
        schema = {"type": "boolean"}
    elif key in _INTEGER_ARGUMENT_NAMES or "int" in tokens or "integer" in tokens:
        schema = {"type": "integer"}
    elif "list/dict" in lowered or "list or dict" in lowered:
        schema = {"type": ["array", "object"]}
    elif "numeric" in tokens or "number" in tokens:
        schema = {"type": "number"}
    elif "object" in tokens or "structured" in tokens:
        schema = {"type": "object"}
    elif _tool_argument_is_array(key=key, description=description):
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": "string"}
    if description:
        schema["description"] = description
    return schema


def _tool_argument_tokens(text: str) -> set[str]:
    return {item for item in re.split(r"[^a-z0-9_]+", text.lower()) if item}


def _tool_argument_enum(description: str) -> list[str]:
    text = description.strip()
    lowered = text.lower()
    if lowered.startswith("optional "):
        text = text[len("optional ") :].strip()
    if " " in text or "|" not in text:
        return []
    values = [item.strip() for item in text.split("|") if item.strip()]
    if len(values) < 2:
        return []
    return values if all(_enum_value_is_simple(value) for value in values) else []


def _enum_value_is_simple(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "-", "."} for ch in value)


def _tool_argument_is_array(*, key: str, description: str) -> bool:
    text = f"{key} {description}".lower()
    if re.search(r"\b(?:list|array)\s*\[", text):
        return True
    if re.search(r"\b(?:list|array)\s+of\b", text):
        return True
    return "array" in _tool_argument_tokens(text)


def _validate_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    enforce_required: bool,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _value_matches_type(value, expected_type):
        return [_schema_error(path=path, expected=_expected_type_label(expected_type), actual=_json_type_name(value))]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        errors.append(_schema_error(path=path, expected=f"one of {enum}", actual=repr(value)))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(_schema_error(path=path, expected=f">= {minimum}", actual=repr(value)))
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(_schema_error(path=path, expected=f"<= {maximum}", actual=repr(value)))
    if isinstance(value, dict):
        errors.extend(_validate_object(value, schema, path=path, enforce_required=enforce_required))
    if isinstance(value, list):
        errors.extend(_validate_array(value, schema, path=path, enforce_required=enforce_required))
    return errors


def _validate_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    enforce_required: bool,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    min_properties = schema.get("minProperties")
    max_properties = schema.get("maxProperties")
    if isinstance(min_properties, int) and len(value) < min_properties:
        errors.append(_schema_error(path=path, expected=f"at least {min_properties} properties", actual=str(len(value))))
    if isinstance(max_properties, int) and len(value) > max_properties:
        errors.append(_schema_error(path=path, expected=f"at most {max_properties} properties", actual=str(len(value))))

    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if enforce_required:
        for required_key in schema.get("required") or []:
            key = str(required_key)
            if key not in value:
                errors.append(_schema_error(path=_join_path(path, key), expected="required property", actual="missing"))
    property_names = schema.get("propertyNames") if isinstance(schema.get("propertyNames"), dict) else {}
    for raw_key, raw_item in value.items():
        key = str(raw_key)
        item_path = _join_path(path, key)
        errors.extend(_validate_property_name(key, property_names, path=item_path))
        property_schema = properties.get(key)
        if isinstance(property_schema, dict):
            errors.extend(_validate_value(raw_item, property_schema, path=item_path, enforce_required=enforce_required))
            continue
        additional = schema.get("additionalProperties", True)
        if additional is False:
            errors.append(_schema_error(path=item_path, expected="declared property", actual="unexpected property"))
        elif isinstance(additional, dict):
            errors.extend(_validate_value(raw_item, additional, path=item_path, enforce_required=enforce_required))
    return errors


def _validate_property_name(key: str, schema: dict[str, Any], *, path: str) -> list[dict[str, Any]]:
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern:
        try:
            if re.search(pattern, key) is None:
                return [_schema_error(path=path, expected=f"property name matching {pattern}", actual=repr(key))]
        except re.error:
            return [_schema_error(path=path, expected="valid propertyNames.pattern", actual=repr(pattern))]
    return []


def _validate_array(
    value: list[Any],
    schema: dict[str, Any],
    *,
    path: str,
    enforce_required: bool,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    if isinstance(min_items, int) and len(value) < min_items:
        errors.append(_schema_error(path=path, expected=f"at least {min_items} items", actual=str(len(value))))
    if isinstance(max_items, int) and len(value) > max_items:
        errors.append(_schema_error(path=path, expected=f"at most {max_items} items", actual=str(len(value))))
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            errors.extend(_validate_value(item, items, path=f"{path}[{index}]" if path else f"[{index}]", enforce_required=enforce_required))
    return errors


def _value_matches_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_value_matches_type(value, item) for item in expected_type)
    expected = str(expected_type or "")
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _expected_type_label(expected_type: Any) -> str:
    if isinstance(expected_type, list):
        return " or ".join(str(item) for item in expected_type)
    return str(expected_type)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _schema_error(*, path: str, expected: str, actual: str) -> dict[str, str]:
    return {"path": path or "<root>", "expected": expected, "actual": actual}


def _join_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key
