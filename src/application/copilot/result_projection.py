from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.copilot.contracts import (
    AnswerReport,
    AppEvent,
    AppResult,
    safe_error_code,
)


MAX_OBSERVATION_SUMMARY_CHARS = 600
MAX_MISSING_DATA_CHARS = 160
MAX_REPORT_TEXT_CHARS = 1200
MAX_FACT_CHARS = 260
MAX_FACTS = 28
INSUFFICIENT_EVIDENCE_CONCLUSION = "结论：当前证据不足，Copilot 未能形成有效结论。"


@dataclass(frozen=True)
class ResultContext:
    request_id: str
    contract_id: str
    run_id: str
    events: list[AppEvent]
    decision_trace: dict[str, Any]
    execution_environment: str = ""
    requires_answer_synthesis: bool = False
    requires_recommendations: bool = False
    answer_dimensions: list[str] | None = None


def result_from_observations(
    context: ResultContext,
    observations: list[dict[str, Any]],
    *,
    eval_only: bool,
) -> AppResult:
    attempted = _dedupe([str(item.get("tool_name") or "observation") for item in observations])
    ok_observations = [item for item in observations if bool(item.get("ok"))]
    failed = [item for item in observations if not bool(item.get("ok"))]
    weak_evidence = [item for item in observations if not bool(item.get("evidence_ok", item.get("ok")))]
    cancelled = [item for item in observations if _error_code(item) == "CANCELLED"]
    refs = [str(item["ref"]) for item in observations if item.get("ref")]
    synthesis_missing = _requires_answer_synthesis(context, eval_only=eval_only)
    eval_synthesis_missing = _eval_model_synthesis_missing(context, observations, eval_only=eval_only)
    suppress_observation_findings = synthesis_missing or eval_synthesis_missing

    if not observations:
        report = AnswerReport(
            conclusion="结论：当前证据不足，Copilot 没有拿到可用的只读观察结果。",
            attempted_checks=attempted,
            missing_data=["no_observations"],
        )
        return AppResult(
            status="insufficient_evidence",
            answer_report=report,
            request_id=context.request_id,
            contract_id=context.contract_id,
            run_id=context.run_id,
            events=context.events,
            decision_trace=context.decision_trace,
        )

    if eval_only and (failed or weak_evidence or eval_synthesis_missing):
        conclusion = "结论（eval-only）：评估夹具未完成答案质量验证，缺少可用证据或模型合成结果。"
    elif eval_only:
        conclusion = "结论（eval-only）：这是评估夹具的形状验证，不代表生产数据结论。"
    elif cancelled:
        conclusion = "结论：运行已取消，未继续调用工具。"
    elif failed and not ok_observations:
        conclusion = "结论：当前证据不足，所有只读工具调用都失败了。"
    elif failed or weak_evidence or synthesis_missing:
        conclusion = "结论：当前证据不完整，不能给出完整判断。"
    else:
        conclusion = "结论：已整理只读工具观察结果；下面是可用观察和缺口。"

    missing = _missing_data(eval_only, observations)
    if synthesis_missing or eval_synthesis_missing:
        missing = _dedupe(missing + [_synthesis_missing_reason(context)])

    status = "answered"
    if cancelled:
        status = "cancelled"
    elif failed or weak_evidence or synthesis_missing or eval_synthesis_missing:
        status = "insufficient_evidence"

    report = AnswerReport(
        conclusion=conclusion,
        attempted_checks=attempted,
        findings=[] if suppress_observation_findings else _observation_findings(observations),
        recommendations=[],
        missing_data=missing,
        evidence_refs=refs,
    )
    return AppResult(
        status=status,
        answer_report=report,
        request_id=context.request_id,
        contract_id=context.contract_id,
        run_id=context.run_id,
        events=context.events,
        decision_trace=context.decision_trace,
    )


def result_from_agent_report(
    context: ResultContext,
    observations: list[dict[str, Any]],
    raw_report: dict[str, Any],
    *,
    required_tools: list[str] | None = None,
) -> AppResult:
    del required_tools
    report = _report_from_candidate(raw_report, observations, answer_dimensions=context.answer_dimensions)
    claimable_refs = _claimable_refs(observations)
    missing = _dedupe(_safe_report_missing_data(report) + _report_observation_missing_data(observations))
    validation_missing: list[str] = []

    if not observations:
        validation_missing.append("no_observations")
    elif not _has_valid_conclusion(report):
        validation_missing.append("valid conclusion")
    elif _has_non_claimable_report_refs(raw_report, claimable_refs):
        validation_missing.append("non-claimable evidence refs")
    elif not _report_has_visible_refs(report):
        validation_missing.append("visible evidence refs")
    elif not report.findings:
        validation_missing.append("cited findings")

    if context.execution_environment == "eval" and not _has_eval_fixture_disclosure(report):
        validation_missing.append("eval fixture disclosure")

    has_observation_gap = _has_observation_gap(observations)
    if context.requires_recommendations and not has_observation_gap and not report.recommendations:
        validation_missing.append("cited recommendations")

    status = "answered"
    if validation_missing or has_observation_gap:
        status = "insufficient_evidence"
        missing = _dedupe(missing + validation_missing)

    final_report = AnswerReport(
        conclusion=report.conclusion if status == "answered" else INSUFFICIENT_EVIDENCE_CONCLUSION,
        attempted_checks=report.attempted_checks,
        findings=report.findings,
        recommendations=report.recommendations if status == "answered" else [],
        missing_data=missing,
        evidence_refs=report.evidence_refs,
    )
    return AppResult(
        status=status,
        answer_report=final_report,
        request_id=context.request_id,
        contract_id=context.contract_id,
        run_id=context.run_id,
        events=context.events,
        decision_trace=context.decision_trace,
    )


def observation_event_payload(item: dict[str, Any], index: int) -> dict[str, Any]:
    ref = f"obs_{index}"
    payload = {
        "ref": ref,
        "tool_name": str(item.get("tool_name") or "observation"),
        "ok": bool(item.get("ok")),
        "summary": _summary_preview(item.get("summary")),
        "facts": _facts_preview(item.get("facts")),
        "value_preview": _observation_value_preview(item),
        "error": _error_preview(item.get("error")),
        "evidence_ok": bool(item.get("evidence_ok", item.get("ok"))),
        "claimable": _claimable(item),
        "evidence_context": _string_map_preview(item.get("evidence_context")),
        "missing_data": _missing_data_preview(item.get("missing_data")),
    }
    facts_omitted = _bounded_count(item.get("facts_omitted"))
    if facts_omitted:
        payload["facts_omitted"] = facts_omitted
    return payload


def _requires_answer_synthesis(context: ResultContext, *, eval_only: bool) -> bool:
    return context.requires_answer_synthesis is True and not eval_only


def _eval_model_synthesis_missing(
    context: ResultContext,
    observations: list[dict[str, Any]],
    *,
    eval_only: bool,
) -> bool:
    if not eval_only or context.requires_answer_synthesis is not True:
        return False
    if any(event.type == "model_error" for event in context.events):
        return True
    for item in observations:
        if _error_code(item) in {"MODEL_REQUIRED", "MODEL_ERROR", "MODEL_ACTION_INVALID"}:
            return True
        missing = _missing_data_preview(item.get("missing_data"))
        if "eval model answer report" in missing or "eval fixture answer report" in missing:
            return True
    return False


def _synthesis_missing_reason(context: ResultContext) -> str:
    model_error_codes = [
        str(event.payload.get("code") or "").strip().upper()
        for event in context.events
        if event.type == "model_error"
    ]
    if any(code == "MODEL_ACTION_INVALID" for code in model_error_codes):
        return "model_synthesis_invalid_action"
    if model_error_codes:
        return "model_synthesis_unavailable"
    return "model_synthesis_not_enabled"


def _has_eval_fixture_disclosure(report: AnswerReport) -> bool:
    return "eval-only" in report.conclusion and "fixture observations are not production evidence" in report.missing_data


def _has_valid_conclusion(report: AnswerReport) -> bool:
    conclusion = str(report.conclusion or "").strip()
    return conclusion.startswith("结论") and len(conclusion) > len("结论")


def _safe_report_missing_data(report: AnswerReport) -> list[str]:
    return [item for item in report.missing_data if _bounded_string_report_text(item)]


def _observation_findings(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"summary": _summary_preview(item.get("summary")), "evidence_refs": [str(item.get("ref"))]}
        for item in observations
        if _summary_preview(item.get("summary")) and _renderable_observation_finding(item)
    ]


def _missing_data(eval_only: bool, observations: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if eval_only:
        missing.append("fixture observations are not production evidence")
    for item in observations:
        item_missing = item.get("missing_data")
        if isinstance(item_missing, list):
            missing.extend(_missing_data_preview(item_missing))
            continue
        if not bool(item.get("evidence_ok", item.get("ok"))):
            missing.append(f"{item.get('tool_name') or 'observation'} evidence")
    return _dedupe(missing)


def _report_observation_missing_data(observations: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for item in observations:
        item_missing = item.get("missing_data")
        if isinstance(item_missing, list):
            values = _missing_data_preview(item_missing)
            if values:
                missing.extend(values)
                continue
        if not bool(item.get("evidence_ok", item.get("ok"))):
            missing.append(f"{item.get('tool_name') or 'observation'} evidence unavailable")
    return _dedupe(missing)


def _has_observation_gap(observations: list[dict[str, Any]]) -> bool:
    return any(not bool(item.get("ok")) or not bool(item.get("evidence_ok", item.get("ok"))) for item in observations)


def _error_code(item: dict[str, Any]) -> str:
    error = item.get("error")
    return safe_error_code(error.get("code"), default="") if isinstance(error, dict) else ""


def _error_preview(error: Any) -> dict[str, Any] | None:
    if not isinstance(error, dict):
        return None
    code = safe_error_code(error.get("code"), default="")
    if "has_message" in error:
        preview = {"has_message": bool(error.get("has_message"))}
        if code:
            preview["code"] = code
        return preview
    message = str(error.get("message") or "").strip()
    preview: dict[str, Any] = {"has_message": bool(message)}
    if code:
        preview["code"] = code
    return preview


def _missing_data_preview(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    preview: list[str] = []
    for item in value:
        text = _bounded_missing_text(item)
        if text:
            preview.append(text)
    return preview


def _string_map_preview(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    preview: dict[str, str] = {}
    for key, item in value.items():
        key_text = _bounded_missing_text(key)
        item_text = _bounded_missing_text(item)
        if key_text and item_text:
            preview[key_text] = item_text
    return preview


def _facts_preview(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    facts: list[str] = []
    for item in value:
        if isinstance(item, (dict, list, tuple, set)):
            text = f"{type(item).__name__} fact"
        else:
            text = " ".join(str(item or "").split())
        if not text:
            continue
        if len(text) > MAX_FACT_CHARS:
            text = f"{text[: MAX_FACT_CHARS - 3]}..."
        facts.append(text)
        if len(facts) >= MAX_FACTS:
            break
    return facts


def _bounded_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return min(max(0, int(value)), 1_000_000)
    except Exception:
        return 0


def _bounded_missing_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__} missing data"
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_MISSING_DATA_CHARS:
        return text
    return f"{text[: MAX_MISSING_DATA_CHARS - 3]}..."


def _summary_preview(summary: Any) -> str:
    if isinstance(summary, (dict, list, tuple, set)):
        return f"{type(summary).__name__} summary"
    text = " ".join(str(summary or "").split())
    if len(text) <= MAX_OBSERVATION_SUMMARY_CHARS:
        return text
    return f"{text[: MAX_OBSERVATION_SUMMARY_CHARS - 3]}..."


def _value_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    preview: dict[str, Any] = {}
    keys = sorted(str(key) for key in value)[:12]
    for key in keys:
        preview[key] = _preview_value(value.get(key))
    if len(value) > len(keys):
        preview["_truncated_keys"] = len(value) - len(keys)
    return preview


def _observation_value_preview(item: dict[str, Any]) -> dict[str, Any]:
    existing = item.get("value_preview")
    if isinstance(existing, dict):
        return _bounded_existing_preview(existing)
    return _value_preview(item.get("data") or {})


def _bounded_existing_preview(value: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    keys = sorted(str(key) for key in value)[:12]
    for key in keys:
        item = value.get(key)
        preview[key] = item if _is_preview_value(item) else _preview_value(item)
    if len(value) > len(keys):
        preview["_truncated_keys"] = len(value) - len(keys)
    return preview


def _is_preview_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, dict):
        return "type" in value and set(value) <= {"type", "count", "keys"}
    return False


def _preview_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 120 else f"{value[:117]}..."
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(key) for key in value)[:8]}
    return {"type": type(value).__name__}


def _report_from_candidate(
    raw_report: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    answer_dimensions: list[str] | None = None,
) -> AnswerReport:
    visible_refs = _claimable_refs(observations)
    attempted = _dedupe([str(item.get("tool_name") or "observation") for item in observations])
    findings = _candidate_findings(raw_report.get("findings"), visible_refs)
    recommendations = _candidate_recommendations(
        raw_report.get("recommendations"),
        visible_refs,
        answer_dimensions=answer_dimensions,
    )
    report_refs = _visible_refs(raw_report.get("evidence_refs"), visible_refs)
    finding_refs = [ref for item in findings for ref in item.get("evidence_refs", [])]
    recommendation_refs = [ref for item in recommendations for ref in item.get("basis_refs", [])]
    return AnswerReport(
        conclusion=_bounded_report_text(raw_report.get("conclusion")),
        attempted_checks=attempted,
        findings=findings,
        recommendations=recommendations,
        missing_data=_string_list_preview(raw_report.get("missing_data")),
        evidence_refs=_dedupe(report_refs + finding_refs + recommendation_refs),
    )


def _candidate_findings(value: Any, visible_refs: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        summary = _bounded_string_report_text(item.get("summary"))
        refs = _visible_refs(item.get("evidence_refs"), visible_refs)
        if summary and refs:
            findings.append({"summary": summary, "evidence_refs": refs})
    return findings


def _candidate_recommendations(
    value: Any,
    visible_refs: set[str],
    *,
    answer_dimensions: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed_dimensions = _allowed_answer_dimensions(answer_dimensions)
    recommendations: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        summary = _bounded_string_report_text(item.get("summary"))
        action = _bounded_string_report_text(item.get("action"))
        target_scope = _bounded_string_report_text(item.get("target_scope"))
        answer_dimension = _bounded_string_report_text(item.get("answer_dimension"))
        refs = _visible_refs(item.get("basis_refs"), visible_refs)
        has_answer_dimensions = bool(allowed_dimensions)
        if (
            summary
            and action
            and target_scope
            and (not has_answer_dimensions or answer_dimension in allowed_dimensions)
            and refs
        ):
            recommendation = {
                "summary": summary,
                "action": action,
                "target_scope": target_scope,
                "basis_refs": refs,
            }
            if has_answer_dimensions:
                recommendation["answer_dimension"] = answer_dimension
            recommendations.append(recommendation)
    return recommendations


def _allowed_answer_dimensions(value: list[str] | None) -> set[str]:
    return {str(item).strip() for item in value or [] if str(item).strip()}


def _visible_refs(value: Any, visible_refs: set[str]) -> list[str]:
    return [item for item in _string_list_preview(value) if item in visible_refs]


def _claimable_refs(observations: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("ref"))
        for item in observations
        if isinstance(item.get("ref"), str)
        and bool(item.get("ok"))
        and bool(item.get("evidence_ok", item.get("ok")))
        and _claimable(item)
    }


def _has_non_claimable_report_refs(raw_report: dict[str, Any], claimable_refs: set[str]) -> bool:
    for value in _iter_raw_report_ref_fields(raw_report):
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in claimable_refs:
                return True
    return False


def _iter_raw_report_ref_fields(raw_report: dict[str, Any]):
    yield raw_report.get("evidence_refs")
    findings = raw_report.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                yield item.get("evidence_refs")
    recommendations = raw_report.get("recommendations")
    if isinstance(recommendations, list):
        for item in recommendations:
            if isinstance(item, dict):
                yield item.get("basis_refs")


def _claimable(item: dict[str, Any]) -> bool:
    return bool(item.get("claimable", True)) and bool(item.get("ok")) and bool(item.get("evidence_ok", item.get("ok")))


def _renderable_observation_finding(item: dict[str, Any]) -> bool:
    if str(item.get("tool_name") or "") == "eval_model":
        return False
    return _claimable(item) or not (bool(item.get("ok")) and bool(item.get("evidence_ok", item.get("ok"))))


def _report_has_visible_refs(report: AnswerReport) -> bool:
    if report.evidence_refs:
        return True
    return any(item.get("evidence_refs") for item in report.findings) or any(
        item.get("basis_refs") for item in report.recommendations
    )


def _string_list_preview(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _bounded_string_report_text(item)
        if text:
            items.append(text)
    return _dedupe(items)


def _bounded_report_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__} value"
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_REPORT_TEXT_CHARS:
        return text
    return f"{text[: MAX_REPORT_TEXT_CHARS - 3]}..."


def _bounded_string_report_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _bounded_report_text(value)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
