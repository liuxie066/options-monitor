from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names, pure_read_toolsets
from src.application.copilot.contracts import safe_error_code
from src.application.research.redaction import redact_value
from src.application.tool_execution import execute_tool


MAX_SUMMARY_CHARS = 600
MAX_PREVIEW_ITEMS = 20
MAX_PREVIEW_DEPTH = 4
MAX_OBSERVATION_TOKENS = 4_000

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
    copilot_properties = _copilot_input_schema(definition).get("properties")
    explicit_fields = set(copilot_properties) if isinstance(copilot_properties, dict) else set()
    unsupported_fields = sorted(str(name) for name in explicit_input if name not in explicit_fields)
    if unsupported_fields:
        return None, (
            f"unsupported Copilot input fields for {tool_name}: "
            + ", ".join(unsupported_fields)
        )
    for name in explicit_fields:
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
        resolution_input = {
            key: value
            for key, value in definition.safe_default_input.items()
            if value is not None
        }
        resolution_input.update(dict((static_payloads or {}).get(name) or {}))
        copilot_schema = _copilot_input_schema(definition)
        visible_properties = copilot_schema.get("properties")
        visible_fields = set(visible_properties) if isinstance(visible_properties, dict) else set()
        default_input = {
            key: value
            for key, value in resolution_input.items()
            if key in visible_fields
        }
        output_contract = definition.resolve_output_contract(resolution_input)
        descriptions.append(
            {
                "name": definition.name,
                "description": _agent_description(definition.description, output_contract),
                "input_schema": copilot_schema,
                "default_input": redact_value(default_input),
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
        failed_observation = redact_value({
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
        })
        if conservative_json_tokens(failed_observation) <= MAX_OBSERVATION_TOKENS:
            return failed_observation
        return bounded_failed_observation(failed_observation, tool_name=tool_name)
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
    row_count = _row_count(data, output_contract)
    scope = _scope(data)
    value = _model_value(data, output_contract)
    coverage = _coverage_envelope(
        data,
        value,
        output_contract,
        payload=payload or {},
    )
    freshness = _freshness_envelope(data, output_contract)
    coverage_status = str(coverage.get("status") or "unknown")
    observation_status = (
        "partial"
        if missing_data or warnings or coverage_status != "complete"
        else ("not_found" if row_count == 0 else "complete")
    )
    observation = redact_model_observation({
        "tool_name": tool_name,
        "ok": True,
        "status": observation_status,
        "summary": _summary(tool_name, data, None, output_contract),
        "value": value,
        "source": _source(data, output_contract),
        **({"scope": scope} if scope else {}),
        "coverage": coverage,
        "freshness": freshness,
        **({"as_of": freshness["as_of"]} if freshness.get("as_of") else {}),
        **({"missing_data": missing_data} if missing_data else {}),
        **({"warnings": warnings} if warnings else {}),
        "result_contract": _compact_output_contract(output_contract),
    })
    if _contains_projection_truncation(value) or conservative_json_tokens(observation) > MAX_OBSERVATION_TOKENS:
        observation = bounded_narrowing_observation(observation)
    return observation


def conservative_json_tokens(value: Any) -> int:
    """Estimate serialized JSON without undercounting Chinese text.

    This is the shared Python-side evidence projection estimate.  Final
    provider-input admission remains owned by the Node Runtime and Pi's
    estimator.
    """

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    non_ascii = sum(ord(char) > 0x7F for char in serialized)
    ascii_count = len(serialized) - non_ascii
    return math.ceil((ascii_count / 4 + non_ascii) * 1.10)


def bounded_narrowing_observation(
    observation: dict[str, Any],
    *,
    tool_name: str | None = None,
    message: str = "结果超过单次证据预算，请缩小账户、时间、标的或结果范围后重试。",
    warning: str = "bounded_projection_requires_narrowing",
    minimal: bool = False,
) -> dict[str, Any]:
    coverage = observation.get("coverage")
    bounded_coverage = dict(coverage) if isinstance(coverage, dict) else {}
    bounded_coverage.update({"status": "partial", "needs_narrowing": True})
    warnings = [
        str(item)
        for item in observation.get("warnings") or ()
        if isinstance(item, str) and item
    ]
    if warning not in warnings:
        warnings.append(warning)
    bounded = {
        key: value
        for key, value in {
            "tool_name": tool_name or observation.get("tool_name"),
            "ok": True,
            "status": "needs_narrowing",
            "summary": observation.get("summary"),
            "value": {"message": message},
            "source": observation.get("source"),
            "scope": observation.get("scope"),
            "coverage": bounded_coverage,
            "freshness": observation.get("freshness"),
            "as_of": observation.get("as_of"),
            "missing_data": observation.get("missing_data"),
            "warnings": warnings[:8],
            "result_contract": observation.get("result_contract"),
            "ref": observation.get("ref"),
            "argument_hash": observation.get("argument_hash"),
            "output_contract_version": observation.get("output_contract_version"),
        }.items()
        if value not in (None, {}, [])
    }
    if not minimal and conservative_json_tokens(bounded) <= MAX_OBSERVATION_TOKENS:
        return bounded
    scope = (
        bounded_coverage.get("scope")
        if isinstance(bounded_coverage.get("scope"), dict)
        else observation.get("scope")
    )
    bounded_scope = (
        scope
        if isinstance(scope, dict) and conservative_json_tokens(scope) <= 256
        else None
    )
    return {
        "tool_name": tool_name or observation.get("tool_name"),
        "ok": True,
        "status": "needs_narrowing",
        "summary": _clip(observation.get("summary"), MAX_SUMMARY_CHARS),
        "value": {"message": message},
        "coverage": {
            "status": "partial",
            "complete_for": "point",
            "needs_narrowing": True,
            **({"scope": bounded_scope} if bounded_scope is not None else {}),
        },
        "freshness": {"status": "unknown"},
        "warnings": [warning],
        **({"ref": observation["ref"]} if observation.get("ref") else {}),
        **(
            {"argument_hash": observation["argument_hash"]}
            if observation.get("argument_hash")
            else {}
        ),
        **(
            {"output_contract_version": observation["output_contract_version"]}
            if observation.get("output_contract_version")
            else {}
        ),
    }


def bounded_failed_observation(
    observation: dict[str, Any],
    *,
    tool_name: str | None = None,
) -> dict[str, Any]:
    code = str(observation.get("code") or observation.get("error") or "TOOL_ERROR")[:120]
    return {
        "tool_name": tool_name or observation.get("tool_name"),
        "ok": False,
        "status": "failed",
        "error": code,
        "code": code,
        "message": _clip(observation.get("message") or "tool failed", MAX_SUMMARY_CHARS),
        "retryable": bool(observation.get("retryable", False)),
        "details": {"truncated": True},
        **({"ref": observation["ref"]} if observation.get("ref") else {}),
    }


def redact_model_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Redact model data while preserving an opaque keyset continuation token."""

    original = deepcopy(observation)
    redacted = redact_value(original)
    contract = original.get("result_contract")
    pagination = contract.get("pagination") if isinstance(contract, dict) else None
    if not isinstance(pagination, dict) or pagination.get("mode") != "keyset":
        return redacted
    original_value = original.get("value")
    redacted_value = redacted.get("value")
    if not isinstance(original_value, dict) or not isinstance(redacted_value, dict):
        return redacted
    next_cursor = original_value.get("next_cursor")
    if isinstance(next_cursor, str) and next_cursor:
        redacted_value["next_cursor"] = next_cursor
    return redacted


def audit_tool_event_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Build a redacted audit projection with opaque cursors replaced by hashes."""

    redacted = redact_value(deepcopy(value))
    return _replace_cursor_values_with_hashes(value, redacted)


def _replace_cursor_values_with_hashes(original: Any, redacted: Any) -> Any:
    if isinstance(original, dict) and isinstance(redacted, dict):
        for key, original_value in original.items():
            if key in {"cursor", "next_cursor"} and isinstance(original_value, str):
                redacted[key] = {
                    "sha256": hashlib.sha256(original_value.encode("utf-8")).hexdigest()
                }
                continue
            if key in redacted:
                redacted[key] = _replace_cursor_values_with_hashes(
                    original_value,
                    redacted[key],
                )
        return redacted
    if isinstance(original, list) and isinstance(redacted, list):
        return [
            _replace_cursor_values_with_hashes(original_item, redacted_item)
            for original_item, redacted_item in zip(original, redacted, strict=False)
        ]
    return redacted


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
    schema["additionalProperties"] = False
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


def _coverage_envelope(
    data: dict[str, Any],
    projected_value: dict[str, Any],
    output_contract: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    policy = str(output_contract.get("coverage") or "unknown")
    declared = data.get("coverage")
    scope = _scope(data) or _request_scope(payload)
    if policy == "source_declared" and isinstance(declared, dict):
        normalized = _normalize_declared_coverage(
            declared,
            require_included_count=(
                str(output_contract.get("evidence_type") or "") == "collection"
            ),
        )
        if normalized is not None:
            if scope and not normalized.get("scope"):
                normalized["scope"] = scope
            return normalized
    if policy == "point":
        return {
            "status": "complete",
            "complete_for": "point",
            **({"scope": scope} if scope else {}),
        }
    if policy == "primary_rows":
        primary = str(output_contract.get("primary_rows") or "").strip()
        source_rows = data.get(primary) if primary else None
        projected_rows = projected_value.get(primary) if primary else None
        if isinstance(source_rows, list) and isinstance(projected_rows, list):
            included_count = sum(
                not (isinstance(item, dict) and set(item) == {"_truncated_items"})
                for item in projected_rows
            )
            projection_omitted = max(0, len(source_rows) - included_count)
            coverage = {
                "status": "partial" if projection_omitted else "complete",
                "complete_for": "requested_page",
                "included_count": included_count,
                "total_count": None,
                "omitted_count": projection_omitted or None,
                **({"scope": scope} if scope else {}),
            }
            if projection_omitted:
                coverage["has_more"] = True
            return coverage
        if isinstance(source_rows, list):
            # The contract deliberately projected scalar/aggregate fields but
            # not the collection itself.  It can support point claims only.
            return {
                "status": "complete",
                "complete_for": "point",
                "included_count": 0,
                "total_count": None,
                "omitted_count": len(source_rows),
                **({"scope": scope} if scope else {}),
            }
    return {
        "status": "unknown",
        "complete_for": "point",
        **({"scope": scope} if scope else {}),
    }


def _normalize_declared_coverage(
    value: dict[str, Any],
    *,
    require_included_count: bool,
) -> dict[str, Any] | None:
    status = str(value.get("status") or "").strip().lower()
    complete_for = str(value.get("complete_for") or "").strip().lower()
    if status not in {"complete", "partial", "unknown"}:
        return None
    if complete_for not in {"point", "requested_page", "full_query"}:
        return None
    normalized: dict[str, Any] = {
        "status": status,
        "complete_for": complete_for,
    }
    for key in ("included_count", "total_count", "omitted_count"):
        raw = value.get(key)
        if raw is None:
            normalized[key] = None
        elif isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            normalized[key] = raw
        else:
            return None
    if require_included_count and normalized.get("included_count") is None:
        return None
    has_more = value.get("has_more")
    if "has_more" in value and has_more is not None and not isinstance(has_more, bool):
        return None
    if isinstance(has_more, bool):
        normalized["has_more"] = has_more
    included_count = normalized.get("included_count")
    total_count = normalized.get("total_count")
    omitted_count = normalized.get("omitted_count")
    if total_count is not None:
        if included_count is not None and included_count > total_count:
            return None
        if omitted_count is not None and omitted_count > total_count:
            return None
        if (
            included_count is not None
            and omitted_count is not None
            and included_count + omitted_count != total_count
        ):
            return None
    if (
        complete_for == "full_query"
        and status == "complete"
        and (
            included_count is None
            or total_count is None
            or omitted_count is None
            or included_count != total_count
            or omitted_count != 0
            or has_more is True
        )
    ):
        return None
    if isinstance(value.get("scope"), dict):
        normalized["scope"] = _preview(value["scope"])
    if _is_iso_timestamp(value.get("as_of")):
        normalized["as_of"] = str(value["as_of"])
    return normalized


def _freshness_envelope(
    data: dict[str, Any],
    output_contract: dict[str, Any],
) -> dict[str, Any]:
    policy = str(output_contract.get("freshness") or "unknown")
    if policy == "not_applicable":
        return {"status": "not_applicable"}
    if policy != "source_declared":
        return {"status": "unknown"}

    declared = data.get("freshness")
    if isinstance(declared, dict):
        raw_status = str(declared.get("status") or declared.get("kind") or "").strip().lower()
        status = raw_status if raw_status in {
            "current",
            "fresh",
            "historical",
            "stale",
            "not_applicable",
            "unknown",
        } else "unknown"
        as_of = _first_timestamp(declared)
        trust_status = str(declared.get("trust_status") or "").strip().lower()
        reason_codes = [
            _clip(item, 120)
            for item in (declared.get("reason_codes") or [])
            if isinstance(item, (str, int, float, bool)) and str(item).strip()
        ][:8]
        return {
            "status": status,
            **({"as_of": as_of} if as_of else {}),
            **({"trust_status": trust_status} if trust_status else {}),
            **({"reason_codes": reason_codes} if reason_codes else {}),
        }

    declared_values = _contract_values(data, output_contract.get("freshness_fields"))
    as_of = _first_timestamp(declared_values)
    if declared_values and as_of:
        return {"status": "historical", "as_of": as_of}
    return {
        "status": "unknown",
        **({"as_of": as_of} if as_of else {}),
    }


def _request_scope(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "account",
        "action",
        "config_key",
        "end_date",
        "limit",
        "market",
        "month",
        "period",
        "run_id",
        "start_date",
        "status",
        "symbol",
        "year",
    }
    return _preview({key: value for key, value in payload.items() if key in allowed})


def _first_timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        return value if _is_iso_timestamp(value) else None
    if isinstance(value, dict):
        preferred = (
            "as_of",
            "observed_at_utc",
            "observed_at",
            "checked_at",
            "latest_event_at_utc",
            "latest_mtime_utc",
            "mtime_utc",
            "requested_end_date",
            "end_date",
            "query_time_utc",
            "retrieved_at_utc",
        )
        for key in preferred:
            candidate = value.get(key)
            found = _first_timestamp(candidate)
            if found:
                return found
        for candidate in value.values():
            found = _first_timestamp(candidate)
            if found:
                return found
    if isinstance(value, list):
        for candidate in value:
            found = _first_timestamp(candidate)
            if found:
                return found
    return None


def _is_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-"


def _contains_projection_truncation(value: Any) -> bool:
    if isinstance(value, dict):
        if (
            "_truncated_keys" in value
            or "_truncated_items" in value
            or "_truncated_value" in value
        ):
            return True
        return any(_contains_projection_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_projection_truncation(item) for item in value)
    return False


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
            "evidence_type": output_contract.get("evidence_type"),
            "bounded_projection": output_contract.get("bounded_projection"),
            "coverage": output_contract.get("coverage"),
            "freshness": output_contract.get("freshness"),
            "pagination": deepcopy(output_contract.get("pagination")),
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
    if fields:
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
        serialized = " ".join(
            json.dumps(value, ensure_ascii=False, default=str).split()
        )
        if len(serialized) <= 320:
            return serialized
        return {"_truncated_value": _clip(serialized, 320)}
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
            key: _bounded_error_detail(value)
            for key, value in details.items()
            if key in {
                "allowed_views",
                "unknown_views",
                "first_keyword",
                "mode",
                "schema_errors",
                "tool_name",
                "consumer",
                "reason_code",
                "blocked_by",
            }
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


def _bounded_error_detail(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _clip(value, 240)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return _clip(json.dumps(value, ensure_ascii=False, default=str), 240)
    if isinstance(value, dict):
        keys = list(value)[:8]
        bounded = {
            str(key): _bounded_error_detail(value[key], depth=depth + 1)
            for key in keys
        }
        if len(value) > len(keys):
            bounded["_truncated_keys"] = len(value) - len(keys)
        return bounded
    if isinstance(value, list):
        items = [
            _bounded_error_detail(item, depth=depth + 1)
            for item in value[:8]
        ]
        if len(value) > len(items):
            items.append({"_truncated_items": len(value) - len(items)})
        return items
    return _clip(value, 240)


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
    "bounded_failed_observation",
    "bounded_narrowing_observation",
    "build_tool_payload",
    "call_read_tool",
    "compact_observation",
    "conservative_json_tokens",
    "redact_model_observation",
    "audit_tool_event_payload",
    "tool_descriptions",
]
