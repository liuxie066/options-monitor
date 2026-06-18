from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.agent_loop import _planner_input_text, _planner_tool_manifest


CONTEXT_EVAL_SCHEMA_VERSION = "om-assistant-context-eval-v1"


def run_context_eval_suite(
    *,
    fixture_path: str | Path,
    case_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    selected_ids = {str(item) for item in case_ids or () if str(item).strip()}
    cases = [
        case
        for case in _load_fixture_cases(Path(fixture_path))
        if str(case.get("mode") or "agent_answer") == "planner_context"
        and (not selected_ids or str(case.get("id") or "") in selected_ids)
    ]
    observed_ids = {str(case.get("id") or "") for case in cases}
    missing_ids = sorted(selected_ids - observed_ids)
    full_manifest_chars = len(json.dumps(_planner_tool_manifest(), ensure_ascii=False, sort_keys=True))
    results = [
        evaluate_context_case(case, full_manifest_chars=full_manifest_chars)
        for case in cases
    ]
    failed = [result for result in results if not result.get("ok")]
    summary = {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "fixture_path": str(Path(fixture_path)),
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "missing_case_ids": missing_ids,
        "ok": not failed and not missing_ids and bool(results),
    }
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "summary": summary,
        "results": results,
    }


def evaluate_context_case(case: dict[str, Any], *, full_manifest_chars: int | None = None) -> dict[str, Any]:
    payload = json.loads(
        _planner_input_text(
            str(case.get("question") or ""),
            conversation_context=_case_conversation_context(case),
        )
    )
    budget = payload.get("manifest_budget") if isinstance(payload.get("manifest_budget"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    analysis_views = _analysis_views_from_payload(payload)
    full_chars = int(full_manifest_chars or len(json.dumps(_planner_tool_manifest(), ensure_ascii=False, sort_keys=True)))
    checks = _evaluate_context_checks(
        case=case,
        budget=budget,
        context=context,
        analysis_views=analysis_views,
        full_manifest_chars=full_chars,
    )
    failures = [check for check in checks if not check.get("passed")]
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "id": str(case.get("id") or ""),
        "mode": "planner_context",
        "question": str(case.get("question") or ""),
        "ok": not failures,
        "status": "pass" if not failures else "fail",
        "actual": _context_eval_actual_payload(
            budget=budget,
            context=context,
            analysis_views=analysis_views,
        ),
        "checks": checks,
        "failures": failures,
    }


def format_context_eval_text(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        (
            f"assistant context eval: {summary.get('passed', 0)}/{summary.get('total', 0)} passed"
            f", failed={summary.get('failed', 0)}"
        )
    ]
    missing = summary.get("missing_case_ids") if isinstance(summary.get("missing_case_ids"), list) else []
    if missing:
        lines.append(f"missing cases: {', '.join(str(item) for item in missing)}")
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        actual = result.get("actual") if isinstance(result.get("actual"), dict) else {}
        context = actual.get("context") if isinstance(actual.get("context"), dict) else {}
        budget = actual.get("manifest_budget") if isinstance(actual.get("manifest_budget"), dict) else {}
        prefix = "PASS" if result.get("ok") else "FAIL"
        lines.append(
            " ".join(
                [
                    prefix,
                    str(result.get("id") or ""),
                    f"sources={_compact_list(budget.get('selection_sources'))}",
                    f"frame={_format_frame(context.get('active_frame'))}",
                    f"glossary={context.get('metric_glossary_namespace') or '-'}",
                    f"followup={_format_followup(context.get('followup_resolution'))}",
                ]
            )
        )
        for failure in result.get("failures") or []:
            if isinstance(failure, dict):
                lines.append(
                    f"  - {failure.get('name')}: expected={failure.get('expected')!r} actual={failure.get('actual')!r}"
                )
    return "\n".join(lines)


def _load_fixture_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        item = json.loads(text)
        if isinstance(item, dict):
            item["_fixture_line"] = line_no
            rows.append(item)
    return rows


def _case_conversation_context(case: dict[str, Any]) -> dict[str, Any] | None:
    raw_context = case.get("conversation_context")
    return dict(raw_context) if isinstance(raw_context, dict) else None


def _analysis_views_from_payload(payload: dict[str, Any]) -> list[str]:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    analysis_query = next((tool for tool in tools if isinstance(tool, dict) and tool.get("name") == "analysis_query"), {})
    semantics = analysis_query.get("semantics") if isinstance(analysis_query.get("semantics"), dict) else {}
    return [str(item) for item in semantics.get("analysis_views") or () if str(item).strip()]


def _evaluate_context_checks(
    *,
    case: dict[str, Any],
    budget: dict[str, Any],
    context: dict[str, Any],
    analysis_views: list[str],
    full_manifest_chars: int,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    _add_check(checks, "budget.mode", "scoped_analysis_views", budget.get("mode"))
    _add_check(checks, "budget.analysis_views_included", len(analysis_views), budget.get("analysis_views_included"))
    _add_check(checks, "budget.manifest_is_scoped", True, int(budget.get("manifest_chars") or 0) < full_manifest_chars)
    if case.get("expect_selection_sources") is not None:
        _add_check(checks, "budget.selection_sources", case["expect_selection_sources"], budget.get("selection_sources"))
    for group_name in case.get("expect_matched_view_groups") or ():
        _add_check(
            checks,
            f"budget.matched_view_groups.{group_name}",
            True,
            str(group_name) in set(str(item) for item in budget.get("matched_view_groups") or ()),
        )
    for view_name in case.get("expect_analysis_views") or ():
        _add_check(checks, f"analysis_views.include.{view_name}", True, str(view_name) in set(analysis_views))
    for view_name in case.get("expect_analysis_views_absent") or ():
        _add_check(checks, f"analysis_views.absent.{view_name}", True, str(view_name) not in set(analysis_views))
    if case.get("expect_max_manifest_chars") is not None:
        _add_check(
            checks,
            "budget.max_manifest_chars",
            True,
            int(budget.get("manifest_chars") or 0) <= int(case["expect_max_manifest_chars"]),
        )
    if case.get("expect_max_analysis_views_included") is not None:
        _add_check(
            checks,
            "budget.max_analysis_views_included",
            True,
            int(budget.get("analysis_views_included") or 0) <= int(case["expect_max_analysis_views_included"]),
        )
    if case.get("expect_recent_read_hint_count") is not None:
        recent_hints = context.get("recent_read_hints") if isinstance(context.get("recent_read_hints"), list) else []
        _add_check(checks, "context.recent_read_hint_count", int(case["expect_recent_read_hint_count"]), len(recent_hints))
    _add_context_expectation_checks(checks, case=case, context=context)
    return checks


def _add_context_expectation_checks(
    checks: list[dict[str, Any]],
    *,
    case: dict[str, Any],
    context: dict[str, Any],
) -> None:
    if case.get("expect_context_active_frame") is not None:
        active_frame = context.get("active_frame") if isinstance(context.get("active_frame"), dict) else {}
        for key_path, expected, actual in _mapping_subset_diffs(
            dict(case["expect_context_active_frame"]),
            active_frame,
            prefix="context.active_frame",
        ):
            _add_check(checks, key_path, expected, actual)
    if case.get("expect_context_active_frame_absent"):
        _add_check(checks, "context.active_frame_absent", True, "active_frame" not in context)
    if case.get("expect_context_frame_stack_count") is not None:
        frame_stack = context.get("frame_stack") if isinstance(context.get("frame_stack"), list) else []
        _add_check(checks, "context.frame_stack_count", int(case["expect_context_frame_stack_count"]), len(frame_stack))
    if case.get("expect_context_frame_stack_absent"):
        _add_check(checks, "context.frame_stack_absent", True, "frame_stack" not in context)
    glossary = context.get("metric_glossary") if isinstance(context.get("metric_glossary"), dict) else {}
    if case.get("expect_context_metric_glossary_namespace") is not None:
        _add_check(
            checks,
            "context.metric_glossary.namespace",
            case["expect_context_metric_glossary_namespace"],
            glossary.get("namespace"),
        )
    terms = glossary.get("terms") if isinstance(glossary.get("terms"), dict) else {}
    for term in case.get("expect_context_metric_glossary_terms") or ():
        _add_check(checks, f"context.metric_glossary.terms.{term}", True, str(term) in terms)
    for term, formula in dict(case.get("expect_context_metric_glossary_term_formulas") or {}).items():
        term_payload = terms.get(str(term)) if isinstance(terms.get(str(term)), dict) else {}
        _add_check(checks, f"context.metric_glossary.terms.{term}.formula", formula, term_payload.get("formula"))
    if case.get("expect_followup_resolution") is not None:
        followup = context.get("followup_resolution") if isinstance(context.get("followup_resolution"), dict) else {}
        for key_path, expected, actual in _mapping_subset_diffs(
            dict(case["expect_followup_resolution"]),
            followup,
            prefix="context.followup_resolution",
        ):
            _add_check(checks, key_path, expected, actual)


def _mapping_subset_diffs(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    prefix: str,
) -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        key_path = f"{prefix}.{key}"
        if isinstance(expected_value, dict):
            actual_child = actual_value if isinstance(actual_value, dict) else {}
            out.extend(_mapping_subset_diffs(expected_value, actual_child, prefix=key_path))
        else:
            out.append((key_path, expected_value, actual_value))
    return out


def _add_check(checks: list[dict[str, Any]], name: str, expected: Any, actual: Any) -> None:
    checks.append(
        {
            "name": name,
            "passed": actual == expected,
            "expected": expected,
            "actual": actual,
        }
    )


def _context_eval_actual_payload(
    *,
    budget: dict[str, Any],
    context: dict[str, Any],
    analysis_views: list[str],
) -> dict[str, Any]:
    return {
        "manifest_budget": {
            "mode": budget.get("mode"),
            "manifest_chars": budget.get("manifest_chars"),
            "analysis_views_included": budget.get("analysis_views_included"),
            "analysis_views_omitted": budget.get("analysis_views_omitted"),
            "matched_view_groups": list(budget.get("matched_view_groups") or []),
            "selection_sources": list(budget.get("selection_sources") or []),
        },
        "analysis_views": analysis_views,
        "context": _context_decision_summary(context),
    }


def _context_decision_summary(context: dict[str, Any]) -> dict[str, Any]:
    recent_hints = context.get("recent_read_hints") if isinstance(context.get("recent_read_hints"), list) else []
    frame_stack = context.get("frame_stack") if isinstance(context.get("frame_stack"), list) else []
    glossary = context.get("metric_glossary") if isinstance(context.get("metric_glossary"), dict) else {}
    terms = glossary.get("terms") if isinstance(glossary.get("terms"), dict) else {}
    return {
        "active_frame": _frame_summary(context.get("active_frame")),
        "active_frame_present": isinstance(context.get("active_frame"), dict),
        "frame_stack_count": len(frame_stack),
        "frame_stack_present": bool(frame_stack),
        "metric_glossary_namespace": glossary.get("namespace"),
        "metric_glossary_terms": sorted(str(item) for item in terms),
        "followup_resolution": _followup_summary(context.get("followup_resolution")),
        "recent_read_hint_count": len(recent_hints),
    }


def _frame_summary(value: Any) -> dict[str, Any]:
    frame = value if isinstance(value, dict) else {}
    if not frame:
        return {}
    return {
        "source": frame.get("source"),
        "domain": frame.get("domain"),
        "tool_name": frame.get("tool_name"),
        "metric_namespace": frame.get("metric_namespace"),
        "tool_payload": frame.get("tool_payload") if isinstance(frame.get("tool_payload"), dict) else {},
    }


def _followup_summary(value: Any) -> dict[str, Any]:
    followup = value if isinstance(value, dict) else {}
    if not followup:
        return {}
    return {
        key: followup.get(key)
        for key in (
            "status",
            "reason",
            "domain",
            "tool_name",
            "metric_namespace",
            "previous_metric_namespace",
        )
        if followup.get(key) not in (None, "", [], {})
    }


def _format_frame(value: Any) -> str:
    frame = value if isinstance(value, dict) else {}
    if not frame:
        return "-"
    return "/".join(
        str(item)
        for item in (frame.get("domain"), frame.get("tool_name"), frame.get("metric_namespace"))
        if item
    ) or "-"


def _format_followup(value: Any) -> str:
    followup = value if isinstance(value, dict) else {}
    if not followup:
        return "-"
    status = str(followup.get("status") or "-")
    namespace = str(followup.get("metric_namespace") or "").strip()
    previous = str(followup.get("previous_metric_namespace") or "").strip()
    if previous and namespace:
        return f"{status}:{previous}->{namespace}"
    if namespace:
        return f"{status}:{namespace}"
    return status


def _compact_list(value: Any) -> str:
    items = [str(item) for item in value or () if str(item).strip()]
    return ",".join(items) if items else "-"


__all__ = [
    "CONTEXT_EVAL_SCHEMA_VERSION",
    "evaluate_context_case",
    "format_context_eval_text",
    "run_context_eval_suite",
]
