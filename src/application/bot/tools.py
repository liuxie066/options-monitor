from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Callable

from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names
from src.application.bot.contracts import safe_error_code
from src.application.research.redaction import redact_value
from src.application.tool_execution import execute_tool


MAX_SUMMARY_CHARS = 600
MAX_PREVIEW_ITEMS = 20
MAX_PREVIEW_DEPTH = 4
MAX_OBSERVATION_TOKENS = 4_000
MAX_NATIVE_OBSERVATION_TOKENS = 8_000

_BOT_RETIRED_READ_TOOLS = frozenset({"preview_notification", "portfolio_cash_bridge"})

_BOT_HIDDEN_INPUT_NAMES = frozenset({"data_config"})
_BOT_HIDDEN_INPUT_SUFFIXES = ("_path", "_paths", "_dir", "_root")


def is_active_bot_read_tool(name: str) -> bool:
    definition = get_tool_definition(name)
    return name not in _BOT_RETIRED_READ_TOOLS and definition is not None and definition.is_pure_read()


def available_read_tools() -> tuple[str, ...]:
    return tuple(sorted(name for name in pure_read_tool_names() if is_active_bot_read_tool(name)))


def build_tool_payload(
    tool_name: str,
    explicit_input: dict[str, Any],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
    fixed_input: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    definition = get_tool_definition(tool_name)
    if not is_active_bot_read_tool(tool_name):
        return None, f"unsupported read-only tool: {tool_name}"
    explicit_payload: dict[str, Any] = {}
    static = (static_payloads or {}).get(tool_name)
    if isinstance(static, dict):
        explicit_payload.update(static)
    properties = definition.input_json_schema().get("properties")
    fields = set(properties) if isinstance(properties, dict) else set()
    bot_properties = _bot_input_schema(definition).get("properties")
    explicit_fields = set(bot_properties) if isinstance(bot_properties, dict) else set()
    unsupported_fields = sorted(str(name) for name in explicit_input if name not in explicit_fields)
    if unsupported_fields:
        return None, (
            f"unsupported Bot input fields for {tool_name}: "
            + ", ".join(unsupported_fields)
        )
    for name in explicit_fields:
        if name not in explicit_input:
            continue
        value = explicit_input.get(name)
        explicit_payload[name] = value.strip() if isinstance(value, str) else value
    if definition.bot_input_normalizer is not None:
        try:
            explicit_payload = definition.bot_input_normalizer(explicit_payload)
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
        explicit = explicit_payload.get(name)
        if explicit not in (None, "") and explicit != value:
            return None, f"tool input conflicts with trusted scope: {name}"
        payload[name] = value.strip() if isinstance(value, str) else value
    if "account" in fields and payload.get("account") not in (None, ""):
        from src.application.account_config import accounts_from_config, normalize_account_label
        from src.application.agent_tool_config import load_runtime_config
        from src.application.agent_tool_contracts import AgentToolError

        trusted = dict(payload)
        for key in ("config_key", "config_path"):
            if (fixed_input or {}).get(key) not in (None, ""):
                trusted[key] = fixed_input[key]
        try:
            _, config = load_runtime_config(config_key=trusted.get("config_key"), config_path=trusted.get("config_path"))
            account = normalize_account_label(payload["account"])
            allowed = accounts_from_config(config, fallback=())
        except (AgentToolError, ValueError, OSError):
            return None, "无法验证账户配置；先使用 project_context 确认有效范围。"
        if account not in allowed:
            return None, "account is outside the configured scope; use project_context to discover valid scope"
        payload["account"] = account
    return payload, None


def call_read_tool(
    tool_name: str,
    payload: dict[str, Any],
    *,
    allowed_tools: tuple[str, ...],
    now_ms: int | None = None,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if tool_name not in allowed_tools:
        return _tool_error(tool_name, "POLICY_ERROR", "tool is outside the Host allowlist")
    definition = get_tool_definition(tool_name)
    if definition is None:
        return _tool_error(tool_name, "INPUT_ERROR", f"unknown tool: {tool_name}")
    if not definition.is_pure_read():
        return _tool_error(tool_name, "POLICY_ERROR", f"tool is not pure read-only: {tool_name}")
    if not is_active_bot_read_tool(tool_name):
        return _tool_error(tool_name, "POLICY_ERROR", f"tool is unavailable in Bot: {tool_name}")
    if tool_name == "option_performance_report" and now_ms is not None:
        from src.application.agent_tools.materialization_impl import (
            option_performance_report_now_ms,
        )

        with option_performance_report_now_ms(now_ms):
            return execute_tool(tool_name, payload)
    if tool_name in {"project_context", "project_files", "runtime_runs", "runtime_logs", "notification_perception_read"}:
        from src.application.agent_tools.project import project_query_context

        with project_query_context(
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
            project_token_limit=6500,
        ):
            return execute_tool(tool_name, payload)
    if tool_name == "scheduled_tasks_read":
        from src.application.agent_tools.scheduled_tasks_impl import scheduled_tasks_query_context

        with scheduled_tasks_query_context(deadline_monotonic=deadline_monotonic, cancelled=cancelled):
            return execute_tool(tool_name, payload)
    return execute_tool(tool_name, payload)


def tool_descriptions(
    tool_names: list[str] | tuple[str, ...],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for name in tool_names:
        definition = get_tool_definition(name)
        if not is_active_bot_read_tool(name):
            continue
        resolution_input = {
            key: value
            for key, value in definition.safe_default_input.items()
            if value is not None
        }
        resolution_input.update(dict((static_payloads or {}).get(name) or {}))
        if definition.bot_input_normalizer is not None:
            resolution_input = definition.bot_input_normalizer(resolution_input)
        bot_schema = _bot_input_schema(definition)
        visible_properties = bot_schema.get("properties")
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
                "input_schema": bot_schema,
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
    del payload
    return model_observation(tool_name, response)


def conservative_json_tokens(value: Any) -> int:
    """Estimate serialized JSON without undercounting Chinese text."""

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
    message: str = ("结果不完整或超过单次证据预算。若工具支持过滤或分页，请缩小范围；"
                    "否则说明工具输出限制，不要重复相同查询。"),
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
    original_value = observation.get("value") if isinstance(observation.get("value"), dict) else {}
    continuation = {key: original_value[key] for key in ("next_cursor", "detail_query", "continuation_status")
                    if original_value.get(key) is not None}
    if conservative_json_tokens(continuation) > 1500:
        continuation = {"continuation_status": "owner_query_exceeds_observation_budget"}
    elif not continuation:
        continuation = {"continuation_status": "owner_has_no_continuation_for_this_output"}
    bounded = {
        key: value
        for key, value in {
            "tool_name": tool_name or observation.get("tool_name"),
            "ok": True,
            "status": "needs_narrowing",
            "summary": observation.get("summary"),
            "value": {"message": message, **continuation},
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
        "value": {"message": message, **continuation},
        "summary": _clip(observation.get("summary"), MAX_SUMMARY_CHARS),
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
    """Redact model data, retaining typed project hashes and opaque cursors."""

    original = deepcopy(observation)
    redacted = redact_value(original)
    if original.get("ok") is True and original.get("tool_name") == "project_context":
        # This owner field is a closed provenance enum, never the runtime path.
        for container in (None, "value"):
            source_parent = original if container is None else original.get(container)
            target_parent = redacted if container is None else redacted.get(container)
            if not isinstance(source_parent, dict) or not isinstance(target_parent, dict):
                continue
            source, target = source_parent.get("source"), target_parent.get("source")
            if isinstance(source, dict) and isinstance(target, dict):
                value = source.get("runtime_root_source")
                if value in ("argument", "env:OM_RUNTIME_ROOT", "repo_default"):
                    target["runtime_root_source"] = value
    if original.get("ok") is True and original.get("tool_name") in {
        "project_context", "project_files", "runtime_runs", "runtime_logs",
        "candidate_filter_explain", "candidate_rank_explain", "option_performance_report",
        "notification_perception_read", "daily_decision_brief_read",
    }:
        def restore_hashes(source: Any, target: Any) -> None:
            if not isinstance(source, dict) or not isinstance(target, dict):
                return
            for field in ("revision", "content_hash", "config_revision", "source_hash", "ledger_input_hash"):
                value = source.get(field)
                if not isinstance(value, str):
                    continue
                raw_hash = value.removeprefix("sha256:")
                if len(raw_hash) == 64 and all(char in "0123456789abcdefABCDEF" for char in raw_hash):
                    target[field] = value

        # Only typed provenance locations from canonical read owners retain hashes.
        for path in (("source",), ("scope",), ("coverage", "scope"),
                     ("value",), ("value", "source"), ("value", "scope"),
                     ("value", "quality"), ("value", "coverage", "scope")):
            source, target = original, redacted
            for key in path:
                source = source.get(key) if isinstance(source, dict) else None
                target = target.get(key) if isinstance(target, dict) else None
            restore_hashes(source, target)
        source_value, target_value = original.get("value"), redacted.get("value")
        if isinstance(source_value, dict) and isinstance(target_value, dict):
            source_entries, target_entries = source_value.get("entries"), target_value.get("entries")
            if isinstance(source_entries, list) and isinstance(target_entries, list):
                for source, target in zip(source_entries, target_entries, strict=False):
                    restore_hashes(source, target)
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


def audit_tool_input(
    tool_name: str,
    value: dict[str, Any],
    *,
    model_proposal: bool = False,
) -> dict[str, Any]:
    """Keep useful tool-input audit evidence without retaining free-form bodies."""

    definition = get_tool_definition(tool_name)
    if definition is None:
        supported_fields: set[str] = set()
    else:
        schema = (
            _bot_input_schema(definition)
            if model_proposal
            else definition.input_json_schema()
        )
        properties = schema.get("properties")
        supported_fields = set(properties) if isinstance(properties, dict) else set()
    projected = {
        str(name): (
            _audit_value_metadata(field_value)
            if name == "query" or name not in supported_fields
            else audit_tool_event_payload({"value": field_value})["value"]
        )
        for name, field_value in value.items()
    }
    if conservative_json_tokens(projected) <= MAX_OBSERVATION_TOKENS:
        return projected
    bounded = {str(name): _audit_value_metadata(field_value) for name, field_value in value.items()}
    if conservative_json_tokens(bounded) <= MAX_OBSERVATION_TOKENS:
        return bounded
    return {
        "field_count": len(value),
        "sha256": hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest(),
        "truncated": True,
    }


def _audit_value_metadata(value: Any) -> dict[str, Any]:
    serialized = _canonical_json(value)
    return {
        "type": type(value).__name__,
        "length": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


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


def _bot_input_schema(definition) -> dict[str, Any]:
    schema = deepcopy(definition.bot_input_schema) if definition.bot_input_schema else definition.input_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in definition.safe_default_input.items():
            if value is None:
                continue
            if name in properties and isinstance(properties[name], dict):
                properties[name].setdefault("default", deepcopy(value))
    if not isinstance(properties, dict):
        return schema
    allowed = set(definition.bot_input_fields) if definition.bot_input_fields else None
    visible = {
        name: value
        for name, value in properties.items()
        if (allowed is None or name in allowed) and not _is_hidden_bot_input(name)
    }
    schema["properties"] = visible
    schema["additionalProperties"] = False
    required = [name for name in schema.get("required") or [] if name in visible]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def _is_hidden_bot_input(name: str) -> bool:
    return name in _BOT_HIDDEN_INPUT_NAMES or name.endswith(_BOT_HIDDEN_INPUT_SUFFIXES)


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
        return deepcopy(source)
    label = str(output_contract.get("source_label") or "").strip()
    return {"label": label} if label else {}


def _scope(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("scope") if isinstance(data.get("scope"), dict) else data.get("filters")
    return deepcopy(value) if isinstance(value, dict) else {}


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
        normalized["scope"] = deepcopy(value["scope"])
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
        "accounts",
        "action",
        "as_of_date",
        "broker",
        "config_key",
        "limit",
        "market",
        "month",
        "period",
        "run_id",
        "status",
        "symbol",
        "view",
        "year",
    }
    return deepcopy({key: value for key, value in payload.items() if key in allowed})


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
    parts = [str(description).strip()]
    if output_contract.get("primary_rows"):
        parts.append(f"Returns primary collection `{output_contract['primary_rows']}`.")
    if output_contract.get("fact_fields"):
        parts.append("Key result fields: " + ", ".join(output_contract["fact_fields"]) + ".")
    if output_contract.get("source_label"):
        parts.append(f"Source: {output_contract['source_label']}.")
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


def _model_value(data: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    # Preserve complete values until the serialized observation budget is checked.
    # Object width/depth alone says nothing about evidence completeness or size.
    fields = output_contract.get("model_value_fields")
    if not fields:
        return deepcopy(data)
    out: dict[str, Any] = {}
    for path in fields:
        values = _values_at_path(data, str(path).split("."))
        if values:
            out[str(path)] = deepcopy(values[0] if len(values) == 1 else values)
    return out


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
                "account",
                "run_id",
                "reason",
                "hint",
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
    "is_active_bot_read_tool",
    "audit_tool_input",
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


def model_observation(tool_name: str, response: dict[str, Any]) -> dict[str, Any]:
    """Pass source-owned data once, redacted and bounded; never expire successful reads."""
    clean = _without_model_provenance(redact_value(response))
    if not isinstance(clean, dict):
        return {"ok": False, "tool_name": tool_name, "error": {"code": "INVALID_RESULT"}}
    data = clean.get("data")
    if tool_name == "runtime_runs" and isinstance(data, dict):
        for row in data.get("runs") or ():
            if not isinstance(row, dict):
                continue
            row.setdefault("outcomes", {
                "usable_scan_result": row.get("scanned"),
                "pipeline_completed_successfully": row.get("ran_pipeline"),
            })
            row.pop("scanned", None)
            row.pop("ran_pipeline", None)
            row.pop("terminal", None)
    if tool_name == "project_files" and isinstance(data, dict) and data.get("action") == "read":
        start = (data.get("body_range") or {}).get("start_line")
        if type(start) is int and isinstance(data.get("text"), str):
            data["text"] = "\n".join(f"{start + offset}|{line}" for offset, line in enumerate(data["text"].split("\n")))
            data["text_format"] = "line_numbered"
    if conservative_json_tokens(clean) > MAX_NATIVE_OBSERVATION_TOKENS:
        data = clean.get("data") if isinstance(clean.get("data"), dict) else {}
        return {"ok": False, "tool_name": tool_name, "status": "needs_narrowing",
                "error": {"code": "NEEDS_NARROWING", "message": "Result exceeds this response budget; request a smaller page or source range. No complete result was read."},
                "source": data.get("source"), "scope": data.get("scope"),
                "coverage": {"status": "partial"}}
    return {**clean, "tool_name": tool_name}


def _without_model_provenance(value: Any) -> Any:
    """Opaque digests are useful for Host validation, not for causal analysis."""
    if isinstance(value, dict):
        return {
            key: _without_model_provenance(item)
            for key, item in value.items()
            if key not in {"revision", "content_hash", "config_revision", "source_hash", "ledger_input_hash"}
        }
    if isinstance(value, list):
        return [_without_model_provenance(item) for item in value]
    return value
