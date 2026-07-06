from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.tool_contracts import project_model_preview


MODEL_EVENT_SCHEMA_VERSION = "om-assistant-model-event-v1"
TOOL_RESULT_ADAPTER_SCHEMA_VERSION = "om-assistant-tool-result-adapter-v1"
MODEL_OBSERVATION_SCHEMA_VERSION = "om-assistant-model-observation-v1"
EVIDENCE_DELTA_SCHEMA_VERSION = "om-assistant-evidence-delta-v1"
TRACE_PAYLOAD_SCHEMA_VERSION = "om-assistant-tool-trace-payload-v1"

ModelEventType = Literal[
    "user_message",
    "context_projected",
    "model_tool_call",
    "tool_guard_decision",
    "tool_result",
    "evidence_updated",
    "model_final_answer",
    "clarification_request",
    "loop_stopped",
]


@dataclass(frozen=True)
class AssistantEvent:
    event_id: str
    event_type: ModelEventType
    payload: dict[str, Any]
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": _copy_mapping(self.payload),
        }
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload


@dataclass(frozen=True)
class ModelToolCallEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    purpose: str = ""
    provider: str | None = None
    parent_event_id: str | None = None
    protocol_error: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["model_tool_call"]:
        return "model_tool_call"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": _copy_mapping(self.arguments),
        }
        if self.purpose:
            payload["purpose"] = self.purpose
        if self.provider:
            payload["provider"] = self.provider
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        if isinstance(self.protocol_error, dict) and self.protocol_error:
            payload["protocol_error"] = _copy_mapping(self.protocol_error)
        return payload


@dataclass(frozen=True)
class ToolGuardDecisionEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    allowed: bool
    decision: str
    reason: str
    risk_class: str
    scope_source: str
    normalized_payload: dict[str, Any]
    duplicate_signature: str | None = None
    error_code: str | None = None
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["tool_guard_decision"]:
        return "tool_guard_decision"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "allowed": bool(self.allowed),
            "decision": self.decision,
            "reason": self.reason,
            "risk_class": self.risk_class,
            "scope_source": self.scope_source,
            "normalized_payload": _copy_mapping(self.normalized_payload),
        }
        if self.duplicate_signature:
            payload["duplicate_signature"] = self.duplicate_signature
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload


@dataclass(frozen=True)
class ToolResultEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    ok: bool
    observation: dict[str, Any]
    evidence_delta: dict[str, Any]
    trace_payload: dict[str, Any]
    missing_data: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["tool_result"]:
        return "tool_result"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": bool(self.ok),
            "observation": _copy_mapping(self.observation),
            "evidence_delta": _copy_mapping(self.evidence_delta),
            "trace_payload": _copy_mapping(self.trace_payload),
            "missing_data": [_copy_mapping(item) for item in self.missing_data],
            "conflicts": [_copy_mapping(item) for item in self.conflicts],
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload

    def provider_tool_result_payload(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_call_id": self.tool_call_id,
            "is_error": not self.ok,
            "content": _copy_mapping(self.observation),
        }


@dataclass(frozen=True)
class ToolResultAdapterOutput:
    raw_result: dict[str, Any]
    event: ToolResultEvent
    schema_version: str = TOOL_RESULT_ADAPTER_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event": self.event.public_payload(),
        }


@dataclass(frozen=True)
class ModelFinalAnswerEvent:
    event_id: str
    answer_text: str
    answer_route: str = "llm_from_evidence"
    parent_event_id: str | None = None
    provider_metadata: dict[str, Any] | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["model_final_answer"]:
        return "model_final_answer"

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "answer_text": self.answer_text,
            "answer_route": self.answer_route,
        }
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        if isinstance(self.provider_metadata, dict) and self.provider_metadata:
            payload["provider_metadata"] = _copy_mapping(self.provider_metadata)
        return payload


def adapt_tool_result(
    *,
    event_id: str,
    tool_call_id: str,
    tool_name: str,
    raw_result: dict[str, Any],
    normalized_payload: dict[str, Any] | None = None,
    guard_decision: ToolGuardDecisionEvent | None = None,
    output_contract: dict[str, Any] | None = None,
    parent_event_id: str | None = None,
) -> ToolResultAdapterOutput:
    if not isinstance(raw_result, dict):
        raise AgentToolError(code="INVALID_TOOL_RESULT", message="raw tool result must be an object")

    ok = bool(raw_result.get("ok"))
    error = raw_result.get("error") if isinstance(raw_result.get("error"), dict) else {}
    data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
    missing_data = tuple(_dict_items(data.get("missing_data")))
    conflicts = tuple(_dict_items(data.get("conflicts")))
    error_code = str(error.get("code") or "") or None

    contract = output_contract if isinstance(output_contract, dict) else {}
    data_preview = _provider_data_preview(
        tool_name=tool_name,
        data=data,
        output_contract=contract,
    )
    data_summary = _data_summary(data, output_contract=contract, data_preview=data_preview)
    data_quality = _data_quality_summary(
        data=data,
        data_preview=data_preview,
        missing_data=missing_data,
        conflicts=conflicts,
    )
    observation = {
        "schema_version": MODEL_OBSERVATION_SCHEMA_VERSION,
        "tool_name": tool_name,
        "ok": ok,
        "status": "ok" if ok else "error",
        "data_summary": data_summary,
        "data_quality": data_quality,
        "missing_data": [_copy_mapping(item) for item in missing_data],
        "conflicts": [_copy_mapping(item) for item in conflicts],
        "continuation_advice": _continuation_advice(
            ok=ok,
            missing_data=missing_data,
            conflicts=conflicts,
        ),
    }
    query_scope = _query_scope_summary(data=data, normalized_payload=normalized_payload or {})
    if query_scope:
        observation["query_scope"] = query_scope
    contract_summary = _output_contract_summary(contract)
    if contract_summary:
        observation["output_contract"] = contract_summary
    if data_preview:
        observation["data_preview"] = data_preview
    if error:
        observation["error"] = {
            "code": error_code,
            "message": str(error.get("message") or ""),
        }
    if guard_decision is not None and not ok:
        observation["guard_decision"] = _guard_decision_observation(guard_decision)

    evidence_delta = {
        "schema_version": EVIDENCE_DELTA_SCHEMA_VERSION,
        "tool_name": tool_name,
        "ok": ok,
        "datasets": [_dataset_summary(tool_name=tool_name, data=data, raw_result=raw_result)],
        "missing_data": [_copy_mapping(item) for item in missing_data],
        "conflicts": [_copy_mapping(item) for item in conflicts],
    }
    if error:
        evidence_delta["error"] = {"code": error_code}

    trace_payload = {
        "schema_version": TRACE_PAYLOAD_SCHEMA_VERSION,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "ok": ok,
        "normalized_payload": _copy_mapping(normalized_payload or {}),
        "guard_decision": guard_decision.public_payload() if guard_decision else {},
        "result_size": _rough_size(raw_result),
        "error_code": error_code,
        "observation_summary": data_summary,
        "data_quality": data_quality,
    }

    event = ToolResultEvent(
        event_id=event_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        ok=ok,
        observation=observation,
        evidence_delta=evidence_delta,
        trace_payload=trace_payload,
        missing_data=missing_data,
        conflicts=conflicts,
        error_code=error_code,
        parent_event_id=parent_event_id,
    )
    return ToolResultAdapterOutput(raw_result=_copy_mapping(raw_result), event=event)


def _guard_decision_observation(event: ToolGuardDecisionEvent) -> dict[str, Any]:
    payload = {
        "tool_name": event.tool_name,
        "allowed": bool(event.allowed),
        "decision": event.decision,
        "reason": event.reason,
        "risk_class": event.risk_class,
        "scope_source": event.scope_source,
        "error_code": event.error_code,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def event_transcript_payload(events: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in events:
        public_payload = getattr(event, "public_payload", None)
        if callable(public_payload):
            payloads.append(public_payload())
        elif isinstance(event, dict):
            payloads.append(_copy_mapping(event))
        else:
            raise AgentToolError(code="INVALID_MODEL_EVENT", message="event transcript item is not serializable")
    return payloads


def _copy_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _copy_value(item) for key, item in value.items()}


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_value(item) for item in value]
    return value


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_copy_mapping(item) for item in value if isinstance(item, dict)]


def _data_summary(
    data: dict[str, Any],
    *,
    output_contract: dict[str, Any] | None = None,
    data_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = data.get("rows")
    contract = output_contract if isinstance(output_contract, dict) else {}
    summary = {
        "keys": _public_data_keys(data),
        "row_count": len(rows) if isinstance(rows, list) else _optional_int(data.get("row_count")),
        "source_label": data.get("source_label") or contract.get("source_label"),
    }
    row_counts = _row_count_summary(data)
    if row_counts:
        summary["row_counts"] = row_counts
    completeness = _preview_completeness_summary(data_preview or {})
    if completeness:
        summary["preview_completeness"] = completeness
    contract_summary = _output_contract_summary(contract or data.get("output_contract"))
    if contract_summary:
        summary["output_contract"] = contract_summary
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


def _row_count_summary(data: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in data.items():
        key_text = str(key)
        if key_text != "row_count" and not key_text.endswith("_row_count"):
            continue
        count = _optional_int(value)
        if count is not None:
            out[key_text] = count
    return out


def _preview_completeness_summary(data_preview: dict[str, Any]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for key, value in data_preview.items():
        key_text = str(key)
        if key_text == "rows_complete" or key_text.endswith("_complete"):
            out[key_text] = bool(value)
    return out


def _data_quality_summary(
    *,
    data: dict[str, Any],
    data_preview: dict[str, Any],
    missing_data: tuple[dict[str, Any], ...],
    conflicts: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "missing_data_count": len(missing_data),
        "conflict_count": len(conflicts),
    }
    row_count = _optional_int(data.get("row_count"))
    if row_count is not None:
        out["row_count"] = row_count
    rows = data_preview.get("rows")
    if isinstance(rows, list):
        out["preview_row_count"] = len(rows)
    completeness = _preview_completeness_summary(data_preview)
    if completeness:
        out["preview_completeness"] = completeness
    if "truncated" in data:
        out["truncated"] = bool(data.get("truncated"))
    quote_refresh = data.get("quote_refresh")
    if isinstance(quote_refresh, dict):
        out["quote_refresh_status"] = str(quote_refresh.get("status") or "")
        if quote_refresh.get("quote_source"):
            out["quote_source"] = str(quote_refresh.get("quote_source") or "")
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _query_scope_summary(*, data: dict[str, Any], normalized_payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    coverage = data.get("evidence")
    coverage = coverage.get("coverage") if isinstance(coverage, dict) else None
    if isinstance(coverage, dict):
        scoped = {
            key: _preview_value(coverage.get(key))
            for key in ("views", "months", "accounts", "symbols")
            if coverage.get(key) not in (None, "", [], {})
        }
        if scoped:
            out["coverage"] = scoped
    filters = data.get("filters")
    if isinstance(filters, dict):
        out["filters"] = _preview_mapping(filters)
    views_used = data.get("views_used")
    if isinstance(views_used, list):
        out["views_used"] = _preview_columns(views_used, limit=20)
    payload_scope = {
        key: _preview_value(normalized_payload.get(key))
        for key in (
            "account",
            "accounts",
            "symbol",
            "symbols",
            "month",
            "months",
            "status",
            "action",
            "limit",
        )
        if normalized_payload.get(key) not in (None, "", [], {})
    }
    query = normalized_payload.get("query")
    if isinstance(query, dict):
        payload_scope["query"] = _preview_mapping(query)
    if payload_scope:
        out["payload"] = payload_scope
    return _compact_preview(out)


def _output_contract_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out = {
        key: value.get(key)
        for key in (
            "canonical_renderer",
            "source_label",
            "answer_surface",
            "result_shape",
            "primary_rows",
            "row_count_field",
            "guard_profile",
        )
        if value.get(key) not in (None, "", [], {})
    }
    if isinstance(value.get("fact_fields"), list):
        out["fact_field_count"] = len(value["fact_fields"])
    if isinstance(value.get("model_preview_fields"), list):
        out["model_preview_field_count"] = len(value["model_preview_fields"])
    return out


def _continuation_advice(
    *,
    ok: bool,
    missing_data: tuple[dict[str, Any], ...],
    conflicts: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    return {
        "may_request_more_read_tools": bool(ok),
        "answer_from_observation_if_sufficient": bool(ok),
        "must_disclose_missing_data": bool(missing_data),
        "must_disclose_conflicts": bool(conflicts),
    }


def _provider_data_preview(*, tool_name: str, data: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "analysis_query":
        rows = data.get("rows")
        row_count = _optional_int(data.get("row_count"))
        truncated = bool(data.get("truncated", False))
        row_limit = _analysis_preview_row_limit(rows=rows, row_count=row_count, truncated=truncated)
        preview_rows = _preview_rows(rows, limit=row_limit)
        return _compact_preview(
            {
                "source_label": data.get("source_label") or "OM read-only analysis workspace",
                "columns": _preview_columns(data.get("columns")),
                "rows": preview_rows,
                "row_count": row_count,
                "rows_complete": _preview_rows_complete(
                    preview_rows=preview_rows,
                    row_count=row_count,
                    truncated=truncated,
                ),
                "row_preview_limit": row_limit,
                "truncated": truncated,
                "views_used": _preview_columns(data.get("views_used")),
                "fallback_text": "" if preview_rows else _preview_text(data.get("fallback_text"), limit=12000),
            }
        )
    if tool_name == "monthly_income_report":
        summary_rows, summary_complete, summary_limit = _preview_rows_with_metadata(
            data.get("summary"),
            row_count=_optional_int(data.get("row_count")),
            default_limit=8,
        )
        return_rows, return_complete, return_limit = _preview_rows_with_metadata(
            data.get("return_summary"),
            row_count=None,
            default_limit=8,
        )
        combined_rows, combined_complete, combined_limit = _preview_rows_with_metadata(
            data.get("combined_return_summary"),
            row_count=None,
            default_limit=4,
        )
        cashflow_rows, cashflow_complete, cashflow_limit = _monthly_income_preview_rows_with_metadata(
            data.get("cashflow_rows"),
            row_count=_optional_int(data.get("cashflow_row_count")),
            default_limit=5,
        )
        realized_rows, realized_complete, realized_limit = _monthly_income_preview_rows_with_metadata(
            data.get("realized_rows"),
            row_count=_optional_int(data.get("realized_row_count")),
            default_limit=5,
        )
        premium_rows, premium_complete, premium_limit = _monthly_income_preview_rows_with_metadata(
            data.get("premium_rows"),
            row_count=_optional_int(data.get("premium_row_count")),
            default_limit=5,
        )
        return _compact_preview(
            {
                "summary": summary_rows,
                "summary_complete": summary_complete,
                "summary_preview_limit": summary_limit,
                "return_summary": return_rows,
                "return_summary_complete": return_complete,
                "return_summary_preview_limit": return_limit,
                "combined_return_summary": combined_rows,
                "combined_return_summary_complete": combined_complete,
                "combined_return_summary_preview_limit": combined_limit,
                "cashflow_rows": cashflow_rows,
                "cashflow_rows_complete": cashflow_complete,
                "cashflow_rows_preview_limit": cashflow_limit,
                "realized_rows": realized_rows,
                "realized_rows_complete": realized_complete,
                "realized_rows_preview_limit": realized_limit,
                "premium_rows": premium_rows,
                "premium_rows_complete": premium_complete,
                "premium_rows_preview_limit": premium_limit,
                "row_count": _optional_int(data.get("row_count")),
                "premium_row_count": _optional_int(data.get("premium_row_count")),
                "cashflow_row_count": _optional_int(data.get("cashflow_row_count")),
                "realized_row_count": _optional_int(data.get("realized_row_count")),
            }
        )
    contract_preview = project_model_preview(data, output_contract)
    if contract_preview:
        return contract_preview
    return {}


def _analysis_preview_row_limit(*, rows: Any, row_count: int | None, truncated: bool) -> int:
    if truncated:
        return 12
    if isinstance(rows, list):
        observed_count = len(rows)
        if observed_count <= 50 and (row_count is None or row_count <= 50):
            return max(observed_count, row_count or 0)
    return 12


def _preview_rows_with_metadata(
    value: Any,
    *,
    row_count: int | None,
    default_limit: int,
    truncated: bool = False,
) -> tuple[list[dict[str, Any]], bool | None, int | None]:
    if not isinstance(value, list) and row_count is None:
        return [], None, None
    limit = _analysis_preview_row_limit(rows=value, row_count=row_count, truncated=truncated)
    if limit == 12 and default_limit != 12:
        limit = default_limit
    rows = _preview_rows(value, limit=limit)
    return (
        rows,
        _preview_rows_complete(preview_rows=rows, row_count=row_count, truncated=truncated),
        limit,
    )


def _monthly_income_preview_rows_with_metadata(
    value: Any,
    *,
    row_count: int | None,
    default_limit: int,
) -> tuple[list[dict[str, Any]], bool | None, int | None]:
    if not isinstance(value, list) and row_count is None:
        return [], None, None
    rows = [item for item in value or [] if isinstance(item, dict)]
    rows = sorted(rows, key=_monthly_income_row_magnitude, reverse=True)
    preview_rows = [_preview_mapping(item) for item in rows[:default_limit]]
    count = row_count if row_count is not None else len(rows)
    return preview_rows, _preview_rows_complete(preview_rows=preview_rows, row_count=count, truncated=False), default_limit


def _monthly_income_row_magnitude(row: dict[str, Any]) -> float:
    for key in (
        "net_cashflow_gross",
        "realized_gross",
        "realized_pnl_gross",
        "premium_gross",
        "premium_income_gross",
        "premium_received_gross",
        "realized_long_pnl_gross",
        "amount",
    ):
        try:
            return abs(float(row[key]))
        except Exception:
            continue
    return 0.0


def _preview_rows_complete(*, preview_rows: list[dict[str, Any]], row_count: int | None, truncated: bool) -> bool:
    if truncated:
        return False
    if row_count is not None:
        return row_count <= len(preview_rows)
    return bool(preview_rows)


def _preview_columns(value: Any, *, limit: int = 24) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value[:limit] if str(item).strip()]


def _preview_rows(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_preview_mapping(item) for item in value[:limit] if isinstance(item, dict)]


def _preview_mapping(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text.startswith("_") or _sensitive_preview_key(key_text):
            continue
        out[key_text] = _preview_value(item)
    return out


def _preview_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _preview_mapping(value)
    if isinstance(value, list):
        return [_preview_value(item) for item in value[:8]]
    if isinstance(value, tuple):
        return [_preview_value(item) for item in value[:8]]
    if isinstance(value, str):
        return _preview_text(value, limit=1000)
    return _copy_value(value)


def _preview_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)].rstrip() + "...(truncated)"


def _compact_preview(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _sensitive_preview_key(key: str) -> bool:
    lowered = key.lower()
    if _sensitive_summary_key(lowered):
        return True
    return lowered in {
        "artifact_path",
        "event_id",
        "position_key",
        "record_id",
        "source_deal_id",
        "stock_lot_id",
        "trace_id",
    }


def _dataset_summary(*, tool_name: str, data: dict[str, Any], raw_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "schema_version": str(raw_result.get("schema_version") or ""),
        "data_summary": _data_summary(data),
    }


def _public_data_keys(data: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in data.keys() if not _sensitive_summary_key(str(key)))[:20]


def _sensitive_summary_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("sql", "path", "secret", "token", "password", "raw", "config"))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _rough_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return len(str(value))


__all__ = [
    "EVIDENCE_DELTA_SCHEMA_VERSION",
    "MODEL_EVENT_SCHEMA_VERSION",
    "MODEL_OBSERVATION_SCHEMA_VERSION",
    "TOOL_RESULT_ADAPTER_SCHEMA_VERSION",
    "TRACE_PAYLOAD_SCHEMA_VERSION",
    "AssistantEvent",
    "ModelFinalAnswerEvent",
    "ModelToolCallEvent",
    "ToolGuardDecisionEvent",
    "ToolResultAdapterOutput",
    "ToolResultEvent",
    "adapt_tool_result",
    "event_transcript_payload",
]
