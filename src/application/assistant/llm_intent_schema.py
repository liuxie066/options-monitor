from __future__ import annotations

import re
from typing import Any

from src.application.assistant.commands import (
    ACCOUNT_VALUES,
    LLM_INTENT_SCHEMA_VERSION,
    LOG_KIND_VALUES,
    POSITION_STATUS_VALUES,
    llm_capability_manifest,
    llm_argument_schema_properties,
    llm_argument_schema_required_keys,
    llm_executable_arguments,
    llm_executable_intent_names,
)
from src.application.assistant.settings import LlmTranslatorSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.contracts import AssistantIntent


_ACCOUNT_VALUES = frozenset(ACCOUNT_VALUES)
_POSITION_STATUS_VALUES = frozenset(POSITION_STATUS_VALUES)
_LOG_KIND_VALUES = frozenset(LOG_KIND_VALUES)
_MONTH_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")

_ALLOWED_ARGUMENTS = llm_executable_arguments()


def llm_intent_schema() -> dict[str, Any]:
    return {
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "shape": {
            "intent": llm_executable_intent_names(),
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
            "intent": {"type": "string", "enum": llm_executable_intent_names()},
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
) -> AssistantIntent:
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
            hint="LLM translator is currently restricted to read-only intents.",
            details={"allowed_intents": llm_executable_intent_names()},
        )

    confidence = _parse_confidence(payload.get("confidence"))
    if confidence < float(settings.confidence_min):
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="LLM intent confidence is too low.",
            hint="Please use a supported command or provide a clearer request.",
            details={"confidence": confidence, "confidence_min": float(settings.confidence_min)},
        )

    arguments = _dict(payload.get("arguments"))
    _reject_extra_arguments(intent_name, arguments)
    normalized = _normalize_arguments(intent_name, arguments)
    return AssistantIntent(name=intent_name, arguments=normalized, parser="llm", confidence=confidence)


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
    if intent_name == "option_positions_open":
        out: dict[str, Any] = {}
        account = _optional_account(arguments.get("account"))
        status = str(arguments.get("status") or "open").strip().lower()
        if status not in _POSITION_STATUS_VALUES:
            raise AgentToolError(code="INPUT_ERROR", message="LLM option position status must be open or all")
        if account:
            out["account"] = account
        out["status"] = status
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
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported LLM intent: {intent_name}")


def _optional_account(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value not in _ACCOUNT_VALUES:
        raise AgentToolError(code="INPUT_ERROR", message="LLM account must be lx or sy")
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
