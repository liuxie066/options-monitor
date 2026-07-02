from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.answer_guard import answer_guard_trace_payload, verify_answer_guard
from src.application.assistant.evidence import EvidenceBundle, build_evidence_bundle
from src.application.assistant.model_events import (
    AssistantEvent,
    ModelFinalAnswerEvent,
    ToolResultAdapterOutput,
    ToolResultEvent,
)
from src.application.assistant.renderer import render_canonical_tool_result
from src.application.assistant.tool_contracts import resolve_output_contract


MODEL_EVIDENCE_SCHEMA_VERSION = "om-assistant-model-evidence-v1"
MODEL_ANSWER_VERIFICATION_SCHEMA_VERSION = "om-assistant-model-answer-verification-v1"


@dataclass(frozen=True)
class ModelEvidenceBundle:
    evidence_bundle: EvidenceBundle
    observations: tuple[dict[str, Any], ...]
    evidence_event: AssistantEvent
    schema_version: str = MODEL_EVIDENCE_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_bundle": self.evidence_bundle.trace_payload(),
            "observation_count": len(self.observations),
            "evidence_event": self.evidence_event.public_payload(),
        }


@dataclass(frozen=True)
class ModelAnswerVerification:
    answer_event: ModelFinalAnswerEvent
    status: str
    guard: dict[str, Any]
    trace: dict[str, Any]
    fallback_text: str = ""
    schema_version: str = MODEL_ANSWER_VERIFICATION_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "status": self.status,
            "passed": self.passed,
            "answer_event_id": self.answer_event.event_id,
            "answer_route": self.answer_event.answer_route,
            "trace": dict(self.trace),
        }
        if self.fallback_text:
            payload["fallback_available"] = True
        return payload


def build_model_evidence_bundle(
    *,
    question: str,
    task_contract: dict[str, Any] | None,
    tool_results: list[ToolResultAdapterOutput] | tuple[ToolResultAdapterOutput, ...],
    event_id: str = "evidence_updated_1",
    parent_event_id: str | None = None,
) -> ModelEvidenceBundle:
    observations = tuple(
        observation
        for index, adapter in enumerate(tool_results, start=1)
        if (observation := event_observation_from_tool_result(adapter, index=index))
    )
    evidence_bundle = _with_event_records(
        build_evidence_bundle(
            question=question,
            plan=_evidence_plan(question=question, task_contract=task_contract),
            observations=list(observations),
        ),
        tool_results=tool_results,
    )
    evidence_event = AssistantEvent(
        event_id=event_id,
        event_type="evidence_updated",
        parent_event_id=parent_event_id,
        payload={
            "schema_version": MODEL_EVIDENCE_SCHEMA_VERSION,
            "evidence_bundle": evidence_bundle.trace_payload(),
            "observation_count": len(observations),
            "source_event_ids": [adapter.event.event_id for adapter in tool_results],
        },
    )
    return ModelEvidenceBundle(
        evidence_bundle=evidence_bundle,
        observations=observations,
        evidence_event=evidence_event,
    )


def event_observation_from_tool_result(adapter: ToolResultAdapterOutput, *, index: int) -> dict[str, Any]:
    raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
    event = adapter.event
    data = _result_data(raw_result, event=event)
    payload = _normalized_payload(event)
    output_contract = resolve_output_contract(event.tool_name, payload)
    observation: dict[str, Any] = {
        "index": int(index),
        "tool_name": event.tool_name,
        "payload": payload,
        "ok": bool(event.ok),
        "error": raw_result.get("error") if isinstance(raw_result.get("error"), dict) else None,
        "data": data,
    }
    if output_contract:
        observation["output_contract"] = output_contract
    return observation


def verify_model_final_answer(
    *,
    answer_event: ModelFinalAnswerEvent,
    model_evidence: ModelEvidenceBundle,
    task_contract: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    tool_results: list[ToolResultAdapterOutput] | tuple[ToolResultAdapterOutput, ...] = (),
) -> ModelAnswerVerification:
    guard = verify_answer_guard(
        answer_event.answer_text,
        observations=[dict(item) for item in model_evidence.observations],
        evidence_bundle=model_evidence.evidence_bundle,
        task_contract=task_contract,
        coverage=coverage,
    )
    if _missing_required_tool_evidence(task_contract=task_contract, observations=model_evidence.observations):
        guard = {
            **guard,
            "violations": [
                *[dict(item) for item in guard.get("violations") or [] if isinstance(item, dict)],
                {
                    "type": "missing_required_tool_evidence",
                    "claim": "model_final_answer_without_tool_result",
                    "evidence": "TaskContract requires OM evidence, but no tool_result was observed",
                },
            ],
        }
    integrity_violations = _completion_integrity_violations(answer_event)
    if integrity_violations:
        guard = {
            **guard,
            "violations": [
                *[dict(item) for item in guard.get("violations") or [] if isinstance(item, dict)],
                *integrity_violations,
            ],
        }
    status = "failed" if guard.get("violations") else "passed"
    fallback_text = user_fallback_from_tool_results(tool_results)
    trace = answer_guard_trace_payload(status, guard)
    if fallback_text:
        trace["fallback"] = (
            "canonical_renderer"
            if fallback_text == canonical_fallback_from_tool_results(tool_results)
            else "user_fallback"
        )
    return ModelAnswerVerification(
        answer_event=answer_event,
        status=status,
        guard=guard,
        trace=trace,
        fallback_text=fallback_text,
    )


def _missing_required_tool_evidence(
    *,
    task_contract: dict[str, Any] | None,
    observations: tuple[dict[str, Any], ...],
) -> bool:
    if observations:
        return False
    contract = task_contract if isinstance(task_contract, dict) else {}
    if not contract:
        return False
    requested_effect = str(contract.get("requested_effect") or "read").strip()
    if requested_effect != "read":
        return True
    domain = str(contract.get("domain") or "general").strip()
    if domain != "general":
        return True
    required = {str(item).strip() for item in contract.get("required_evidence") or [] if str(item).strip()}
    return bool(required - {"summary", "source_policy"})


def _completion_integrity_violations(answer_event: ModelFinalAnswerEvent) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    metadata = answer_event.provider_metadata if isinstance(answer_event.provider_metadata, dict) else {}
    finish_reason = str(metadata.get("finish_reason") or "").strip().lower()
    status = str(metadata.get("status") or "").strip().lower()
    incomplete_reason = str(metadata.get("incomplete_reason") or "").strip().lower()
    if finish_reason in {"length", "max_tokens"} or status == "incomplete" or incomplete_reason in {
        "max_output_tokens",
        "max_tokens",
    }:
        violations.append(
            {
                "type": "provider_output_truncated",
                "claim": finish_reason or incomplete_reason or status,
                "evidence": "provider indicated the final answer may be incomplete",
            }
        )
    if _looks_like_incomplete_final_answer(answer_event.answer_text):
        violations.append(
            {
                "type": "incomplete_final_answer",
                "claim": _tail(answer_event.answer_text),
                "evidence": "final answer ends with a dangling phrase or list marker",
            }
        )
    return violations


def _looks_like_incomplete_final_answer(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    compact = "".join(value.split()).lower()
    if not compact:
        return False
    if value[-1] in {"，", "、", "：", ":", "-", "；", ";"}:
        return True
    dangling_suffixes = (
        "只有",
        "包括",
        "如下",
        "如下:",
        "如下：",
        "分别为",
        "为:",
        "为：",
        "和",
        "或",
        "以及",
        "但",
        "但是",
        "因为",
        "所以",
        "如果",
        "当前可用的previewcapability只有",
    )
    return any(compact.endswith(suffix) for suffix in dangling_suffixes)


def _tail(text: str, *, limit: int = 80) -> str:
    value = str(text or "").strip()
    return value[-limit:]


def canonical_fallback_from_tool_results(
    tool_results: list[ToolResultAdapterOutput] | tuple[ToolResultAdapterOutput, ...],
) -> str:
    for adapter in reversed(tuple(tool_results)):
        raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
        if not bool(raw_result.get("ok")):
            continue
        event = adapter.event
        data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
        payload = _normalized_payload(event)
        output_contract = resolve_output_contract(event.tool_name, payload)
        if not _output_contract_allows_final_answer(output_contract):
            continue
        renderer_key = str(output_contract.get("canonical_renderer") or "").strip()
        if not renderer_key:
            continue
        rendered = render_canonical_tool_result(
            renderer_key=renderer_key,
            data=data,
            tool_result=build_response(tool_name=event.tool_name, ok=True, data=data),
        )
        if rendered:
            return rendered
    return ""


def user_fallback_from_tool_results(
    tool_results: list[ToolResultAdapterOutput] | tuple[ToolResultAdapterOutput, ...],
) -> str:
    for adapter in reversed(tuple(tool_results)):
        raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
        if not bool(raw_result.get("ok")):
            continue
        event = adapter.event
        data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
        if event.tool_name == "analysis_query":
            rendered = _analysis_query_user_fallback(data)
            if rendered:
                return rendered
        rendered = canonical_fallback_from_tool_results((adapter,))
        if rendered:
            return rendered
    return ""


def _analysis_query_user_fallback(data: dict[str, Any]) -> str:
    rows = [item for item in data.get("rows") or [] if isinstance(item, dict)]
    columns = [str(item) for item in data.get("columns") or [] if str(item).strip()]
    try:
        row_count = int(data.get("row_count") if data.get("row_count") is not None else len(rows))
    except Exception:
        row_count = len(rows)
    source = str(data.get("source_label") or "OM read-only analysis workspace").strip()
    warning_lines = _analysis_query_warning_lines(data)
    if row_count <= 0 or not rows:
        lines = ["没有查到匹配记录。", *warning_lines, f"数据来源：{source}"]
        return "\n".join(line for line in lines if line).strip()

    assigned_stock = _assigned_stock_lifecycle_fallback(data=data, rows=rows, columns=columns)
    if assigned_stock:
        return assigned_stock

    lines = [_analysis_query_summary_line(rows=rows, columns=columns, row_count=row_count)]
    comparison = _analysis_query_comparison_line(rows=rows)
    if comparison:
        lines.append(comparison)
    else:
        lines.extend(_analysis_query_row_lines(rows=rows, columns=columns))
    if bool(data.get("truncated")):
        lines.append("结果已截断，仅展示可用预览中的关键行。")
    lines.extend(warning_lines)
    lines.append(f"数据来源：{source}")
    return "\n".join(line for line in lines if line).strip()


def _assigned_stock_lifecycle_fallback(*, data: dict[str, Any], rows: list[dict[str, Any]], columns: list[str]) -> str:
    field_set = set(columns)
    assigned_stock_fields = {
        "shares_remaining",
        "shares_sold",
        "stock_cost_per_share",
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
    }
    if len(field_set & assigned_stock_fields) < 3:
        return ""
    rendered = render_canonical_tool_result(
        renderer_key="assigned_stock_lifecycle",
        data={
            "rows": rows,
            "filters": _assigned_stock_filters_from_rows(rows),
            "warnings": data.get("warnings") if isinstance(data.get("warnings"), list) else [],
            "source_label": str(data.get("source_label") or "").strip(),
        },
        tool_result=build_response(tool_name="analysis_query", ok=True, data=data),
    )
    return rendered.strip()


def _assigned_stock_filters_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in ("account", "status", "symbol"):
        values = _unique_values(rows, field)
        if len(values) == 1:
            out[field] = values[0]
        elif field in {"account", "status"}:
            out[field] = "all"
    return out


def _analysis_query_warning_lines(data: dict[str, Any]) -> list[str]:
    rendered = render_canonical_tool_result(
        renderer_key="analysis_result",
        data=data,
        tool_result=build_response(tool_name="analysis_query", ok=True, data=data),
    )
    return [
        line
        for line in rendered.splitlines()
        if line.startswith("提示：") or line.startswith("覆盖范围：")
    ]


def _analysis_query_summary_line(*, rows: list[dict[str, Any]], columns: list[str], row_count: int) -> str:
    status_values = _unique_values(rows, "status")
    if status_values:
        return f"分析完成：共 {row_count} 行，状态包括 {', '.join(status_values[:4])}。"
    if "higher_account" in columns:
        higher = _first_value(rows, "higher_account")
        if higher:
            return f"分析完成：共 {row_count} 行，当前对比里 {higher} 更高。"
    return f"分析完成：共 {row_count} 行。"


def _analysis_query_comparison_line(*, rows: list[dict[str, Any]]) -> str:
    row = rows[0] if rows else {}
    higher = _first_value(rows, "higher_account")
    diff_key = _first_existing_key(row, ("income_diff_cny", "diff_cny", "difference_cny", "pnl_diff", "amount_diff"))
    if not higher or not diff_key:
        return ""
    month = _format_value(row.get("month"))
    left_key = _first_existing_key(row, ("lx_income_cny", "lx_net_income_cny", "lx_amount"))
    right_key = _first_existing_key(row, ("sy_income_cny", "sy_net_income_cny", "sy_amount"))
    parts = []
    if month != "-":
        parts.append(month)
    if left_key and right_key:
        parts.append(f"lx={_format_value(row.get(left_key))}")
        parts.append(f"sy={_format_value(row.get(right_key))}")
    parts.append(f"差额={_format_value(row.get(diff_key))}")
    return f"关键差异：{higher} 更高（{'，'.join(parts)}）。"


def _analysis_query_row_lines(*, rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    display_columns = _analysis_query_display_columns(rows=rows, columns=columns)
    lines: list[str] = []
    for index, row in enumerate(rows[:5], start=1):
        facts = [f"{column}={_format_value(row.get(column))}" for column in display_columns if row.get(column) is not None]
        if facts:
            lines.append(f"{index}. " + "，".join(facts))
    if len(rows) > 5:
        lines.append(f"其余 {len(rows) - 5} 行未展开。")
    return lines


def _analysis_query_display_columns(*, rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    if not columns and rows:
        columns = [str(key) for key in rows[0]]
    priority = [
        "month",
        "account",
        "symbol",
        "currency",
        "status",
        "shares_remaining",
        "shares_sold",
        "stock_cost_per_share",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
        "assigned_stock_unrealized_pnl",
        "spot",
        "net_income_cny",
        "net_return_rate",
    ]
    ordered = [column for column in priority if column in columns]
    ordered.extend(column for column in columns if column not in ordered)
    return ordered[:10]


def _first_existing_key(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    return next((key for key in keys if key in row), "")


def _first_value(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = _format_value(row.get(key))
        if value != "-":
            return value
    return ""


def _unique_values(rows: list[dict[str, Any]], key: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = _format_value(row.get(key))
        if value == "-" or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value).strip() or "-"


def _output_contract_allows_final_answer(output_contract: dict[str, Any]) -> bool:
    answer_surface = str(output_contract.get("answer_surface") or "").strip().lower()
    if not answer_surface:
        return True
    return answer_surface in {"answer", "final", "user"}


def _evidence_plan(*, question: str, task_contract: dict[str, Any] | None) -> dict[str, Any]:
    contract = dict(task_contract or {})
    return {
        "goal": str(contract.get("goal") or question or "").strip(),
        "task_contract": contract,
        "steps": [],
    }


def _result_data(raw_result: dict[str, Any], *, event: ToolResultEvent) -> dict[str, Any]:
    data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
    out = _copy_mapping(data)
    if event.missing_data and "missing_data" not in out:
        out["missing_data"] = [_copy_mapping(item) for item in event.missing_data]
    if event.conflicts and "conflicts" not in out:
        out["conflicts"] = [_copy_mapping(item) for item in event.conflicts]
    return out


def _with_event_records(
    evidence_bundle: EvidenceBundle,
    *,
    tool_results: list[ToolResultAdapterOutput] | tuple[ToolResultAdapterOutput, ...],
) -> EvidenceBundle:
    missing_data = [dict(item) for item in evidence_bundle.missing_data]
    conflicts = [dict(item) for item in evidence_bundle.conflicts]
    for adapter in tool_results:
        event = adapter.event
        for item in event.missing_data:
            missing_data.append({**_copy_mapping(item), "source_tool": event.tool_name})
        for item in event.conflicts:
            conflicts.append({**_copy_mapping(item), "source_tool": event.tool_name})
    return EvidenceBundle(
        scope=evidence_bundle.scope,
        facts=evidence_bundle.facts,
        datasets=evidence_bundle.datasets,
        diagnostics=evidence_bundle.diagnostics,
        calculations=evidence_bundle.calculations,
        missing_data=tuple(_dedupe_records(missing_data)),
        conflicts=tuple(_dedupe_records(conflicts)),
        provenance_lines=evidence_bundle.provenance_lines,
        fallback_renderers=evidence_bundle.fallback_renderers,
        guard_contracts=evidence_bundle.guard_contracts,
    )


def _normalized_payload(event: ToolResultEvent) -> dict[str, Any]:
    trace_payload = event.trace_payload if isinstance(event.trace_payload, dict) else {}
    payload = trace_payload.get("normalized_payload")
    return _copy_mapping(payload) if isinstance(payload, dict) else {}


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


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


__all__ = [
    "MODEL_ANSWER_VERIFICATION_SCHEMA_VERSION",
    "MODEL_EVIDENCE_SCHEMA_VERSION",
    "ModelAnswerVerification",
    "ModelEvidenceBundle",
    "build_model_evidence_bundle",
    "canonical_fallback_from_tool_results",
    "event_observation_from_tool_result",
    "user_fallback_from_tool_results",
    "verify_model_final_answer",
]
