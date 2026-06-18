from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.agent_loop import _planner_input_text, _planner_tool_manifest
from src.application.assistant.context_projection import build_context_projection, context_projection_trace


CONTEXT_EVAL_SCHEMA_VERSION = "om-assistant-context-eval-v1"
CONTEXT_EVAL_MODES = ("planner_context", "projection", "validation", "scenarios")
_MODE_FIXTURE_VALUES = {
    "planner_context": {"planner_context"},
    "projection": {"projection", "context_projection"},
    "validation": {"validation", "context_validation"},
    "scenarios": {"scenario", "scenarios", "context_scenario"},
}
_MODE_ALIASES = {
    "legacy": "planner_context",
    "context_projection": "projection",
    "context_validation": "validation",
    "scenario": "scenarios",
    "context_scenario": "scenarios",
}


def run_context_eval_suite(
    *,
    fixture_path: str | Path,
    case_ids: list[str] | tuple[str, ...] | None = None,
    mode: str = "planner_context",
) -> dict[str, Any]:
    eval_mode = normalize_context_eval_mode(mode)
    selected_ids = {str(item) for item in case_ids or () if str(item).strip()}
    cases = [
        case
        for case in _load_fixture_cases(Path(fixture_path))
        if _case_matches_mode(case, eval_mode)
        and (not selected_ids or str(case.get("id") or "") in selected_ids)
    ]
    observed_ids = {str(case.get("id") or "") for case in cases}
    missing_ids = sorted(selected_ids - observed_ids)
    full_manifest_chars = len(json.dumps(_planner_tool_manifest(), ensure_ascii=False, sort_keys=True))
    results = [
        _evaluate_case_for_mode(case, mode=eval_mode, full_manifest_chars=full_manifest_chars)
        for case in cases
    ]
    failed = [result for result in results if not result.get("ok")]
    skipped = [result for result in results if str(result.get("status") or "") == "skip"]
    passed = [result for result in results if result.get("ok") and str(result.get("status") or "") == "pass"]
    requires_cases = bool(selected_ids) or eval_mode in {"planner_context", "projection"}
    summary = {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "mode": eval_mode,
        "fixture_path": str(Path(fixture_path)),
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "skipped": len(skipped),
        "empty": not bool(results),
        "missing_case_ids": missing_ids,
        "ok": not failed and not missing_ids and (bool(results) or not requires_cases),
    }
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "summary": summary,
        "results": results,
    }


def normalize_context_eval_mode(mode: str | None) -> str:
    text = str(mode or "planner_context").strip()
    normalized = _MODE_ALIASES.get(text, text)
    if normalized not in CONTEXT_EVAL_MODES:
        raise ValueError(f"unsupported context eval mode: {mode!r}")
    return normalized


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


def evaluate_projection_case(case: dict[str, Any]) -> dict[str, Any]:
    projection = build_context_projection(
        current_user_message=str(case.get("current_user_message") or case.get("question") or ""),
        conversation_context=_case_conversation_context(case),
        recent_sessions=_case_recent_sessions(case),
    )
    actual = _projection_eval_actual_payload(projection)
    checks = _evaluate_projection_checks(case=case, projection=projection, actual=actual)
    failures = [check for check in checks if not check.get("passed")]
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "id": str(case.get("id") or ""),
        "mode": "projection",
        "question": str(case.get("current_user_message") or case.get("question") or ""),
        "ok": not failures,
        "status": "pass" if not failures else "fail",
        "actual": actual,
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
    mode = str(summary.get("mode") or "").strip()
    if mode:
        lines[0] += f", mode={mode}"
    if int(summary.get("skipped") or 0):
        lines[0] += f", skipped={summary.get('skipped', 0)}"
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
        if str(result.get("status") or "") == "skip":
            prefix = "SKIP"
        if result.get("mode") == "projection":
            projection = actual.get("context_projection") if isinstance(actual.get("context_projection"), dict) else {}
            lines.append(
                " ".join(
                    [
                        prefix,
                        str(result.get("id") or ""),
                        f"turns={projection.get('recent_turn_count', 0)}",
                        f"tools={projection.get('recent_successful_tool_count', 0)}",
                        f"refs={projection.get('evidence_ref_count', 0)}",
                        f"gaps={projection.get('open_gap_count', 0)}",
                        f"pending={projection.get('pending_operation_count', 0)}",
                        f"truncated={str(projection.get('truncated', False)).lower()}",
                    ]
                )
            )
        elif result.get("mode") == "planner_context":
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
        else:
            lines.append(
                " ".join(
                    [
                        prefix,
                        str(result.get("id") or ""),
                        f"mode={result.get('mode') or '-'}",
                        f"reason={result.get('reason') or '-'}",
                    ]
                )
            )
        for failure in result.get("failures") or []:
            if isinstance(failure, dict):
                lines.append(
                    f"  - {failure.get('name')}: expected={failure.get('expected')!r} actual={failure.get('actual')!r}"
                )
    return "\n".join(lines)


def _evaluate_case_for_mode(
    case: dict[str, Any],
    *,
    mode: str,
    full_manifest_chars: int,
) -> dict[str, Any]:
    if mode == "planner_context":
        return evaluate_context_case(case, full_manifest_chars=full_manifest_chars)
    if mode == "projection":
        return evaluate_projection_case(case)
    return _evaluate_deferred_context_layer_case(case, mode=mode)


def _evaluate_deferred_context_layer_case(case: dict[str, Any], *, mode: str) -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "id": str(case.get("id") or ""),
        "mode": mode,
        "question": str(case.get("question") or ""),
        "ok": True,
        "status": "skip",
        "reason": f"{mode}_evaluator_not_wired",
        "actual": {},
        "checks": [],
        "failures": [],
    }


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


def _case_matches_mode(case: dict[str, Any], mode: str) -> bool:
    raw_mode = str(case.get("mode") or "agent_answer").strip()
    return raw_mode in _MODE_FIXTURE_VALUES[mode]


def _case_conversation_context(case: dict[str, Any]) -> dict[str, Any] | None:
    raw_context = case.get("conversation_context")
    return dict(raw_context) if isinstance(raw_context, dict) else None


def _case_recent_sessions(case: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_sessions = case.get("recent_sessions")
    if not isinstance(raw_sessions, list):
        return None
    return [dict(item) for item in raw_sessions if isinstance(item, dict)]


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


def _evaluate_projection_checks(
    *,
    case: dict[str, Any],
    projection: dict[str, Any],
    actual: dict[str, Any],
) -> list[dict[str, Any]]:
    expect = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    projection_summary = actual.get("context_projection") if isinstance(actual.get("context_projection"), dict) else {}
    checks: list[dict[str, Any]] = []
    _add_check(checks, "projection.schema_version", "om-context-projection-v1", projection_summary.get("schema_version"))
    if expect.get("recent_turn_count") is not None:
        _add_check(
            checks,
            "projection.recent_turn_count",
            int(expect["recent_turn_count"]),
            projection_summary.get("recent_turn_count"),
        )
    if expect.get("recent_turn_count_min") is not None:
        _add_check(
            checks,
            "projection.recent_turn_count_min",
            True,
            int(projection_summary.get("recent_turn_count") or 0) >= int(expect["recent_turn_count_min"]),
        )
    for key in (
        "recent_successful_tool_count",
        "evidence_ref_count",
        "open_gap_count",
        "pending_operation_count",
    ):
        if expect.get(key) is not None:
            _add_check(checks, f"projection.{key}", int(expect[key]), projection_summary.get(key))
    if expect.get("truncated") is not None:
        _add_check(checks, "projection.truncated", bool(expect["truncated"]), bool(projection_summary.get("truncated")))
    for reason in expect.get("truncation_reason_contains") or ():
        _add_check(
            checks,
            f"projection.truncation_reason.{reason}",
            True,
            str(reason) in str(projection_summary.get("truncation_reason") or ""),
        )
    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    for key in expect.get("forbidden_payload_keys_absent") or ():
        _add_check(checks, f"projection.forbidden_absent.{key}", True, str(key) not in serialized)
    if isinstance(expect.get("safe_slots"), dict):
        aggregate_slots = actual.get("safe_slots") if isinstance(actual.get("safe_slots"), dict) else {}
        for key_path, expected, actual_value in _mapping_subset_diffs(
            dict(expect["safe_slots"]),
            aggregate_slots,
            prefix="projection.safe_slots",
        ):
            _add_check(checks, key_path, expected, actual_value)
    if isinstance(expect.get("data_shape"), dict):
        _add_check(
            checks,
            "projection.data_shape_contains",
            True,
            _data_shape_expectation_matches(
                actual.get("data_shapes") if isinstance(actual.get("data_shapes"), list) else [],
                dict(expect["data_shape"]),
            ),
        )
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


def _projection_eval_actual_payload(projection: dict[str, Any]) -> dict[str, Any]:
    trace = context_projection_trace(projection)
    return {
        "context_projection": trace,
        "successful_tools": [
            str(item.get("tool_name") or "")
            for item in projection.get("recent_successful_tools") or []
            if isinstance(item, dict) and str(item.get("tool_name") or "").strip()
        ],
        "safe_slots": _aggregate_projection_safe_slots(projection),
        "data_shapes": [
            dict(item.get("data_shape"))
            for item in projection.get("recent_successful_tools") or []
            if isinstance(item, dict) and isinstance(item.get("data_shape"), dict)
        ],
    }


def _aggregate_projection_safe_slots(projection: dict[str, Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for container_key in (
        "recent_turns",
        "recent_successful_tools",
        "available_evidence_refs",
        "open_evidence_gaps",
        "pending_operations",
    ):
        for item in projection.get(container_key) or []:
            if isinstance(item, dict):
                _merge_slot_values(out, item.get("safe_slots"))
    return out


def _merge_slot_values(out: dict[str, list[Any]], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, raw_values in value.items():
        bucket = out.setdefault(str(key), [])
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for item in values:
            if item not in bucket:
                bucket.append(item)


def _data_shape_expectation_matches(data_shapes: list[Any], expected: dict[str, Any]) -> bool:
    for shape in data_shapes:
        if not isinstance(shape, dict):
            continue
        matched = True
        for key, expected_value in expected.items():
            actual_value = shape.get(key)
            if isinstance(expected_value, list):
                if list(actual_value or []) != expected_value:
                    matched = False
                    break
            elif actual_value != expected_value:
                matched = False
                break
        if matched:
            return True
    return False


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
    "CONTEXT_EVAL_MODES",
    "CONTEXT_EVAL_SCHEMA_VERSION",
    "evaluate_context_case",
    "evaluate_projection_case",
    "format_context_eval_text",
    "normalize_context_eval_mode",
    "run_context_eval_suite",
]
