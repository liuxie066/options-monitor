from __future__ import annotations

import re
from typing import Any

from src.application.assistant.commands import (
    ACCOUNT_VALUES,
    LLM_INTENT_SCHEMA_VERSION,
    LOG_KIND_VALUES,
    llm_capability_manifest,
    llm_argument_schema_properties,
    llm_argument_schema_required_keys,
    llm_executable_arguments,
    llm_executable_intent_names,
    llm_recognizable_intent_names,
    spec_by_intent,
)
from src.application.assistant.settings import LlmTranslatorSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.contracts import PerceptionResult
from src.application.assistant.position_query import PositionQuery


_ACCOUNT_VALUES = frozenset(ACCOUNT_VALUES)
_LOG_KIND_VALUES = frozenset(LOG_KIND_VALUES)
_MONTH_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")

_ALLOWED_ARGUMENTS = llm_executable_arguments()
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_SYMBOL_EDIT_SET_ALIASES = {
    "sell_call.enabled": "sell_call.enabled",
    "covered_call.enabled": "sell_call.enabled",
    "sell_call.min_strike": "sell_call.min_strike",
    "covered_call.min_strike": "sell_call.min_strike",
    "sell_put.enabled": "sell_put.enabled",
}
_SAFE_ENSURE_USE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def llm_intent_schema() -> dict[str, Any]:
    return {
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "shape": {
            "intent": llm_recognizable_intent_names(),
            "arguments": "object",
            "confidence": "number 0..1",
        },
        "argument_keys": {name: sorted(keys) for name, keys in _ALLOWED_ARGUMENTS.items()},
        "capability_manifest": llm_capability_manifest(),
        "write_intents_allowed": False,
    }


def llm_intent_json_schema() -> dict[str, Any]:
    """JSON schema requested from an LLM provider.

    The schema is intentionally broad at the `arguments` level because the final
    authority is still inbound_intent_from_llm_payload(), which rejects write
    intents, unsupported arguments, low confidence, and incomplete slots.
    """

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "enum": [LLM_INTENT_SCHEMA_VERSION]},
            "intent": {"type": "string", "enum": llm_recognizable_intent_names()},
            "arguments": {
                "type": "object",
                "additionalProperties": False,
                "properties": llm_argument_schema_properties(),
                "required": llm_argument_schema_required_keys(),
            },
            "confidence": {"type": "number"},
        },
        "required": ["schema_version", "intent", "arguments", "confidence"],
    }


def inbound_intent_from_llm_payload(
    payload: dict[str, Any],
    *,
    settings: LlmTranslatorSettings,
) -> PerceptionResult:
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message="LLM intent payload must be a JSON object")

    version = str(payload.get("schema_version") or LLM_INTENT_SCHEMA_VERSION).strip()
    if version != LLM_INTENT_SCHEMA_VERSION:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="unsupported LLM intent schema version",
            details={"schema_version": version, "expected": LLM_INTENT_SCHEMA_VERSION},
        )

    intent_name = str(payload.get("intent") or "").strip()
    if not intent_name:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="LLM intent payload is missing intent")
    if intent_name not in _ALLOWED_ARGUMENTS:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"LLM intent is not allowed: {intent_name}",
            hint="LLM translator is restricted to read-only intents plus explicitly allowed preview-only symbol settings.",
            details={
                "intent_name": intent_name,
                "llm_rejected_reason": _llm_rejected_reason(intent_name),
                "allowed_intents": llm_recognizable_intent_names(),
            },
        )

    confidence = _parse_confidence(payload.get("confidence"))
    if confidence < float(settings.confidence_min):
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="我还不能确定你要做什么。",
            hint="请换一种更明确的说法，或发送“你能做什么”查看当前支持的能力。",
            details={"confidence": confidence, "confidence_min": float(settings.confidence_min)},
        )

    arguments = _dict(payload.get("arguments"))
    _reject_extra_arguments(intent_name, arguments)
    normalized = _normalize_arguments(intent_name, arguments)
    return PerceptionResult(intent_name=intent_name, arguments=normalized, source="llm", confidence=confidence)


def _parse_confidence(raw: Any) -> float:
    if raw is None:
        raise AgentToolError(code="INPUT_ERROR", message="LLM intent payload is missing confidence")
    try:
        value = float(raw)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message="LLM intent confidence must be a number") from exc
    if value < 0 or value > 1:
        raise AgentToolError(code="INPUT_ERROR", message="LLM intent confidence must be between 0 and 1")
    return value


def _dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentToolError(code="INPUT_ERROR", message="LLM intent arguments must be an object")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip():
            continue
        out[str(key)] = raw
    return out


def _reject_extra_arguments(intent_name: str, arguments: dict[str, Any]) -> None:
    allowed = _ALLOWED_ARGUMENTS[intent_name]
    extra = sorted(str(key) for key in arguments if str(key) not in allowed)
    if extra:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"LLM intent has unsupported arguments for {intent_name}: {', '.join(extra)}",
            details={"allowed_arguments": sorted(allowed), "extra_arguments": extra},
        )


def _normalize_arguments(intent_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if intent_name in {"help", "runtime_status", "healthcheck", "config_validate", "symbol_list", "pending_operations"}:
        return {}
    if intent_name == "symbol_config_query":
        return _normalize_symbol_config_query_arguments(arguments)
    if intent_name in {"position_query", "position_exit_analysis"}:
        return PositionQuery.from_payload(arguments).to_payload()
    if intent_name == "assigned_stock_position_query":
        out: dict[str, Any] = {"assigned_stock_status": _optional_assigned_stock_status(arguments.get("assigned_stock_status")) or "open"}
        account = _optional_account(arguments.get("account"))
        symbol = str(arguments.get("symbol") or "").strip().upper()
        stock_lot_id = str(arguments.get("stock_lot_id") or "").strip()
        refresh_quotes = arguments.get("refresh_quotes")
        if account:
            out["account"] = account
        if symbol:
            out["symbol"] = symbol
        if stock_lot_id:
            out["stock_lot_id"] = stock_lot_id
        out["refresh_quotes"] = True if refresh_quotes is None else bool(refresh_quotes)
        return out
    if intent_name == "monthly_income_report":
        out = {}
        account = _optional_account(arguments.get("account"))
        month = _optional_month(arguments.get("month"))
        if account:
            out["account"] = account
        if month:
            out["month"] = month
        return out
    if intent_name == "runtime_runs":
        return {"limit": _optional_int(arguments.get("limit"), default=10, minimum=1, maximum=50, field="limit")}
    if intent_name == "runtime_logs":
        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="LLM runtime_logs intent requires run_id")
        kind = str(arguments.get("kind") or "all").strip().lower()
        if kind not in _LOG_KIND_VALUES:
            raise AgentToolError(code="INPUT_ERROR", message="LLM runtime_logs kind must be all, tool, or state")
        lines = _optional_int(arguments.get("lines"), default=50, minimum=1, maximum=500, field="lines")
        return {"run_id": run_id, "kind": kind, "lines": lines}
    if intent_name == "symbol_edit":
        return _normalize_symbol_edit_arguments(arguments)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported LLM intent: {intent_name}")


def _normalize_symbol_edit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    symbol = str(arguments.get("symbol") or "").strip()
    if not symbol:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="LLM symbol_edit intent requires symbol")

    raw_sets = arguments.get("set")
    if not isinstance(raw_sets, dict) or not raw_sets:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="LLM symbol_edit intent requires set")

    normalized_sets: dict[str, Any] = {}
    for raw_key, raw_value in raw_sets.items():
        key = _SYMBOL_EDIT_SET_ALIASES.get(str(raw_key or "").strip())
        if key is None:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="LLM symbol_edit can only preview covered-call or sell-put monitored-symbol settings",
                details={
                    "unsupported_field": str(raw_key or "").strip(),
                    "supported_fields": sorted(_SYMBOL_EDIT_SET_ALIASES),
                },
            )
        value = _normalize_symbol_edit_set_value(key, raw_value)
        if key in normalized_sets and normalized_sets[key] != value:
            raise AgentToolError(
                code="NEEDS_CLARIFICATION",
                message=f"LLM symbol_edit has conflicting values for {key}",
            )
        normalized_sets[key] = value

    ensure_use = _optional_ensure_use(arguments.get("ensure_use"))
    if "sell_call.min_strike" in normalized_sets:
        normalized_sets.setdefault("sell_call.enabled", True)
        ensure_use = _append_unique(ensure_use, "call_base")
    if normalized_sets.get("sell_call.enabled") is True:
        ensure_use = _append_unique(ensure_use, "call_base")
    if normalized_sets.get("sell_put.enabled") is True:
        ensure_use = _append_unique(ensure_use, "put_base")

    out: dict[str, Any] = {"symbol": symbol, "set": normalized_sets}
    if ensure_use:
        out["ensure_use"] = ensure_use
    return out


def _normalize_symbol_config_query_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    symbol = str(arguments.get("symbol") or "").strip()
    if not symbol:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="LLM symbol_config_query intent requires symbol")
    out: dict[str, Any] = {"symbol": symbol}
    for key in ("strategy", "field"):
        value = str(arguments.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _normalize_symbol_edit_set_value(key: str, raw_value: Any) -> bool | float:
    if key.endswith(".enabled"):
        return _required_bool(raw_value, key)
    if key.endswith(".min_strike"):
        return _required_positive_float(raw_value, key)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported LLM symbol_edit field: {key}")


def _required_bool(raw: Any, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if value in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    raise AgentToolError(code="INPUT_ERROR", message=f"LLM {field} must be boolean")


def _required_positive_float(raw: Any, field: str) -> float:
    try:
        value = float(raw)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"LLM {field} must be a number") from exc
    if value <= 0:
        raise AgentToolError(code="INPUT_ERROR", message=f"LLM {field} must be positive")
    return value


def _optional_ensure_use(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentToolError(code="INPUT_ERROR", message="LLM ensure_use must be an array")
    out: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if not value:
            continue
        if not _SAFE_ENSURE_USE_RE.fullmatch(value):
            raise AgentToolError(code="INPUT_ERROR", message="LLM ensure_use contains an unsafe template name")
        out = _append_unique(out, value)
    return out


def _append_unique(values: list[str], value: str) -> list[str]:
    if value in values:
        return values
    return [*values, value]


def _llm_rejected_reason(intent_name: str) -> str:
    spec = _COMMAND_SPECS_BY_INTENT.get(intent_name)
    if spec is None:
        return "unknown_intent"
    return "known_non_executable_intent"


def _optional_account(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value not in _ACCOUNT_VALUES:
        raise AgentToolError(code="INPUT_ERROR", message="LLM account must be lx or sy")
    return value


def _optional_assigned_stock_status(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    aliases = {
        "partial": "partially_sold",
        "partially-sold": "partially_sold",
        "close": "closed",
    }
    value = aliases.get(value, value)
    if value not in {"open", "partially_sold", "closed", "all"}:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="LLM assigned_stock_status must be open, partially_sold, closed, or all",
        )
    return value


def _optional_month(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if not _MONTH_RE.fullmatch(value):
        raise AgentToolError(code="INPUT_ERROR", message="LLM month must be YYYY-MM")
    return value


def _optional_int(raw: Any, *, default: int, minimum: int, maximum: int, field: str) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"LLM {field} must be an integer") from exc
    if value < minimum or value > maximum:
        raise AgentToolError(code="INPUT_ERROR", message=f"LLM {field} must be between {minimum} and {maximum}")
    return value
