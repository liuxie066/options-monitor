from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.agent_loop import _planner_input_text, _planner_tool_manifest
from src.application.assistant.context_projection import build_context_projection, context_projection_trace
from src.application.assistant.context_validation import validate_context_use


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
    requires_cases = bool(selected_ids) or eval_mode in {"planner_context", "projection", "validation", "scenarios"}
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


def evaluate_validation_case(case: dict[str, Any]) -> dict[str, Any]:
    projection = _case_context_projection(case)
    if not projection:
        projection = build_context_projection(
            current_user_message=str(case.get("current_user_message") or case.get("question") or ""),
            conversation_context=_case_conversation_context(case),
            recent_sessions=_case_recent_sessions(case),
        )
    validation = validate_context_use(
        current_user_message=str(case.get("current_user_message") or case.get("question") or ""),
        context_projection=projection,
        plan_payload=_case_plan_payload(case),
        planner_manifest=_case_planner_manifest(case),
    )
    actual = _validation_eval_actual_payload(validation)
    checks = _evaluate_validation_checks(case=case, validation=validation, actual=actual)
    failures = [check for check in checks if not check.get("passed")]
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "id": str(case.get("id") or ""),
        "mode": "validation",
        "question": str(case.get("current_user_message") or case.get("question") or ""),
        "ok": not failures,
        "status": "pass" if not failures else "fail",
        "actual": actual,
        "checks": checks,
        "failures": failures,
    }


def evaluate_scenario_case(case: dict[str, Any], *, full_manifest_chars: int | None = None) -> dict[str, Any]:
    question = str(case.get("current_user_message") or case.get("question") or "")
    conversation_context = _case_conversation_context(case) or {}
    projection = _case_context_projection(case)
    if not projection:
        projection = build_context_projection(
            current_user_message=question,
            conversation_context=conversation_context,
            recent_sessions=_case_recent_sessions(case),
        )
    planner_context = dict(conversation_context)
    planner_context["context_projection"] = projection
    planner_payload = json.loads(_planner_input_text(question, conversation_context=planner_context))
    budget = planner_payload.get("manifest_budget") if isinstance(planner_payload.get("manifest_budget"), dict) else {}
    context = planner_payload.get("context") if isinstance(planner_payload.get("context"), dict) else {}
    analysis_views = _analysis_views_from_payload(planner_payload)
    validation = validate_context_use(
        current_user_message=question,
        context_projection=projection,
        plan_payload=_case_plan_payload(case),
        planner_manifest=_case_planner_manifest(case),
    )
    full_chars = int(full_manifest_chars or len(json.dumps(_planner_tool_manifest(), ensure_ascii=False, sort_keys=True)))
    actual = _scenario_eval_actual_payload(
        case=case,
        projection=projection,
        budget=budget,
        context=context,
        analysis_views=analysis_views,
        validation=validation,
    )
    checks = _evaluate_scenario_checks(
        case=case,
        projection=projection,
        budget=budget,
        context=context,
        analysis_views=analysis_views,
        validation=validation,
        full_manifest_chars=full_chars,
    )
    failures = [check for check in checks if not check.get("passed")]
    return {
        "schema_version": CONTEXT_EVAL_SCHEMA_VERSION,
        "id": str(case.get("id") or ""),
        "mode": "scenarios",
        "family": str(case.get("family") or ""),
        "question": question,
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
        elif result.get("mode") == "validation":
            validation = actual.get("context_validation") if isinstance(actual.get("context_validation"), dict) else {}
            lines.append(
                " ".join(
                    [
                        prefix,
                        str(result.get("id") or ""),
                        f"validation={validation.get('status') or '-'}",
                        f"code={validation.get('code') or '-'}",
                        f"context={validation.get('context_use_mode') or '-'}",
                    ]
                )
            )
        elif result.get("mode") == "planner_context":
            projection = context.get("context_projection") if isinstance(context.get("context_projection"), dict) else {}
            lines.append(
                " ".join(
                    [
                        prefix,
                        str(result.get("id") or ""),
                        f"sources={_compact_list(budget.get('selection_sources'))}",
                        f"projection={projection.get('schema_version') or '-'}",
                        f"turns={projection.get('recent_turn_count', 0)}",
                        f"refs={projection.get('evidence_ref_count', 0)}",
                    ]
                )
            )
        elif result.get("mode") == "scenarios":
            planner = actual.get("planner_context") if isinstance(actual.get("planner_context"), dict) else {}
            projection = actual.get("projection") if isinstance(actual.get("projection"), dict) else {}
            validation = actual.get("validation") if isinstance(actual.get("validation"), dict) else {}
            decision = actual.get("decision") if isinstance(actual.get("decision"), dict) else {}
            planner_budget = planner.get("manifest_budget") if isinstance(planner.get("manifest_budget"), dict) else {}
            projection_trace = (
                projection.get("context_projection") if isinstance(projection.get("context_projection"), dict) else {}
            )
            validation_trace = (
                validation.get("context_validation") if isinstance(validation.get("context_validation"), dict) else {}
            )
            lines.append(
                " ".join(
                    [
                        prefix,
                        str(result.get("id") or ""),
                        f"family={result.get('family') or '-'}",
                        f"validation={validation_trace.get('status') or '-'}",
                        f"code={validation_trace.get('code') or '-'}",
                        f"sources={_compact_list(planner_budget.get('selection_sources'))}",
                        f"read_scope={_compact_list(planner_budget.get('read_tool_selection_sources'))}",
                        f"refs={projection_trace.get('evidence_ref_count', 0)}",
                        f"gaps={projection_trace.get('open_gap_count', 0)}",
                        f"terminal={decision.get('terminal') or '-'}",
                        f"tool={decision.get('first_tool') or '-'}",
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
    if mode == "validation":
        return evaluate_validation_case(case)
    if mode == "scenarios":
        return evaluate_scenario_case(case, full_manifest_chars=full_manifest_chars)
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


def _case_context_projection(case: dict[str, Any]) -> dict[str, Any] | None:
    raw_projection = case.get("context_projection")
    return dict(raw_projection) if isinstance(raw_projection, dict) else None


def _case_plan_payload(case: dict[str, Any]) -> dict[str, Any]:
    raw_plan = case.get("plan_payload")
    if not isinstance(raw_plan, dict):
        raw_plan = case.get("plan")
    return dict(raw_plan) if isinstance(raw_plan, dict) else {}


def _case_planner_manifest(case: dict[str, Any]) -> list[dict[str, Any]]:
    raw_manifest = case.get("planner_manifest")
    if isinstance(raw_manifest, list):
        return [dict(item) for item in raw_manifest if isinstance(item, dict)]
    return _planner_tool_manifest()


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
    for source in case.get("expect_read_tool_selection_sources") or ():
        _add_check(
            checks,
            f"budget.read_tool_selection_sources.{source}",
            True,
            str(source) in set(str(item) for item in budget.get("read_tool_selection_sources") or ()),
        )
    for tool_name in case.get("expect_read_tools_included") or ():
        _add_check(
            checks,
            f"budget.read_tools_included.{tool_name}",
            True,
            str(tool_name) in set(str(item) for item in budget.get("read_tools_included") or ()),
        )
    for tool_name in case.get("expect_read_tools_omitted") or ():
        _add_check(
            checks,
            f"budget.read_tools_omitted.{tool_name}",
            True,
            str(tool_name) in set(str(item) for item in budget.get("read_tools_omitted") or ()),
        )
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
    if case.get("expect_preview_authority_allowed") is not None:
        _add_check(
            checks,
            "budget.preview_authority_allowed",
            bool(case["expect_preview_authority_allowed"]),
            bool(budget.get("preview_authority_allowed")),
        )
    _add_context_expectation_checks(checks, case=case, context=context)
    return checks


def _evaluate_validation_checks(
    *,
    case: dict[str, Any],
    validation: dict[str, Any],
    actual: dict[str, Any],
) -> list[dict[str, Any]]:
    expect = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    validation_summary = actual.get("context_validation") if isinstance(actual.get("context_validation"), dict) else {}
    checks: list[dict[str, Any]] = []
    _add_check(checks, "validation.schema_version", "om-context-validation-v1", validation_summary.get("schema_version"))
    for key in ("status", "code", "context_use_mode"):
        if expect.get(key) is not None:
            _add_check(checks, f"validation.{key}", expect[key], validation_summary.get(key))
    if expect.get("referenced_turn_ids") is not None:
        _add_check(checks, "validation.referenced_turn_ids", expect["referenced_turn_ids"], validation.get("referenced_turn_ids"))
    if expect.get("referenced_evidence_refs") is not None:
        _add_check(
            checks,
            "validation.referenced_evidence_refs",
            expect["referenced_evidence_refs"],
            validation.get("referenced_evidence_refs"),
        )
    if expect.get("warning_count") is not None:
        _add_check(checks, "validation.warning_count", int(expect["warning_count"]), len(validation.get("warnings") or []))
    if expect.get("violation_reason") is not None:
        violation = validation.get("violation") if isinstance(validation.get("violation"), dict) else {}
        _add_check(checks, "validation.violation.reason", expect["violation_reason"], violation.get("reason"))
    if isinstance(expect.get("validated_slots"), dict):
        slots = validation.get("validated_slots") if isinstance(validation.get("validated_slots"), dict) else {}
        for key_path, expected, actual_value in _mapping_subset_diffs(
            dict(expect["validated_slots"]),
            slots,
            prefix="validation.validated_slots",
        ):
            _add_check(checks, key_path, expected, actual_value)
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


def _evaluate_scenario_checks(
    *,
    case: dict[str, Any],
    projection: dict[str, Any],
    budget: dict[str, Any],
    context: dict[str, Any],
    analysis_views: list[str],
    validation: dict[str, Any],
    full_manifest_chars: int,
) -> list[dict[str, Any]]:
    expect = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    checks: list[dict[str, Any]] = []
    _add_check(checks, "scenario.family", True, bool(str(case.get("family") or "").strip()))

    projection_expect = expect.get("projection") if isinstance(expect.get("projection"), dict) else {}
    if projection_expect:
        checks.extend(
            _evaluate_projection_checks(
                case={"expect": projection_expect},
                projection=projection,
                actual=_projection_eval_actual_payload(projection),
            )
        )

    planner_expect = expect.get("planner") if isinstance(expect.get("planner"), dict) else {}
    if planner_expect:
        planner_case = {
            "expect_selection_sources": planner_expect.get("selection_sources"),
            "expect_matched_view_groups": planner_expect.get("matched_view_groups"),
            "expect_analysis_views": planner_expect.get("analysis_views"),
            "expect_analysis_views_absent": planner_expect.get("analysis_views_absent"),
            "expect_max_manifest_chars": planner_expect.get("max_manifest_chars"),
            "expect_max_analysis_views_included": planner_expect.get("max_analysis_views_included"),
            "expect_preview_authority_allowed": planner_expect.get("preview_authority_allowed"),
            "expect_read_tool_selection_sources": planner_expect.get("read_tool_selection_sources"),
            "expect_read_tools_included": planner_expect.get("read_tools_included"),
            "expect_read_tools_omitted": planner_expect.get("read_tools_omitted"),
        }
        checks.extend(
            _evaluate_context_checks(
                case=planner_case,
                budget=budget,
                context=context,
                analysis_views=analysis_views,
                full_manifest_chars=full_manifest_chars,
            )
        )

    validation_expect = expect.get("validation") if isinstance(expect.get("validation"), dict) else {}
    if validation_expect:
        checks.extend(
            _evaluate_validation_checks(
                case={"expect": validation_expect},
                validation=validation,
                actual=_validation_eval_actual_payload(validation),
            )
        )

    decision_expect = expect.get("decision") if isinstance(expect.get("decision"), dict) else {}
    if decision_expect:
        checks.extend(
            _evaluate_decision_checks(
                expect=decision_expect,
                actual=_plan_decision_actual_payload(_case_plan_payload(case)),
            )
        )

    if expect.get("no_legacy_authority"):
        for key in ("active_frame", "frame_stack", "followup_resolution", "metric_glossary"):
            _add_check(checks, f"scenario.no_legacy_authority.{key}", True, not _contains_mapping_key(context, key))
    return checks


def _evaluate_decision_checks(*, expect: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    scalar_fields = (
        "terminal",
        "first_tool",
        "first_tool_family",
        "requested_effect",
        "context_use_mode",
        "requires_clarification",
        "tool_call_count",
    )
    for field in scalar_fields:
        if field in expect:
            _add_check(checks, f"decision.{field}", expect[field], actual.get(field))
    if expect.get("allowed_terminals") is not None:
        _add_check(
            checks,
            "decision.allowed_terminals",
            True,
            str(actual.get("terminal") or "") in set(str(item) for item in expect.get("allowed_terminals") or ()),
        )
    for terminal in expect.get("forbidden_terminals") or ():
        _add_check(
            checks,
            f"decision.forbidden_terminal.{terminal}",
            True,
            str(actual.get("terminal") or "") != str(terminal),
        )
    if expect.get("allowed_first_tools") is not None:
        _add_check(
            checks,
            "decision.allowed_first_tools",
            True,
            str(actual.get("first_tool") or "") in set(str(item) for item in expect.get("allowed_first_tools") or ()),
        )
    tool_sequence = [str(item) for item in actual.get("tool_sequence") or ()]
    for tool_name in expect.get("forbidden_tools") or ():
        _add_check(
            checks,
            f"decision.forbidden_tool.{tool_name}",
            True,
            str(tool_name) not in set(tool_sequence),
        )
    if expect.get("tool_sequence") is not None:
        _add_check(checks, "decision.tool_sequence", list(expect["tool_sequence"]), tool_sequence)
    first_arguments = actual.get("first_arguments") if isinstance(actual.get("first_arguments"), dict) else {}
    for key in expect.get("first_arguments_absent") or ():
        _add_check(checks, f"decision.first_arguments_absent.{key}", True, str(key) not in first_arguments)
    if isinstance(expect.get("first_arguments_contains"), dict):
        for key_path, expected, actual_value in _mapping_subset_diffs(
            dict(expect["first_arguments_contains"]),
            first_arguments,
            prefix="decision.first_arguments",
        ):
            _add_check(checks, key_path, expected, actual_value)
    if expect.get("max_tool_calls") is not None:
        _add_check(
            checks,
            "decision.max_tool_calls",
            True,
            int(actual.get("tool_call_count") or 0) <= int(expect["max_tool_calls"]),
        )
    return checks


def _add_context_expectation_checks(
    checks: list[dict[str, Any]],
    *,
    case: dict[str, Any],
    context: dict[str, Any],
) -> None:
    projection = context.get("context_projection") if isinstance(context.get("context_projection"), dict) else {}
    if case.get("expect_context_projection_provided") is not None:
        _add_check(
            checks,
            "context.context_projection_provided",
            bool(case["expect_context_projection_provided"]),
            bool(projection),
        )
    if case.get("expect_context_projection_current_user_message") is not None:
        current = projection.get("current_user_message") if isinstance(projection.get("current_user_message"), dict) else {}
        _add_check(
            checks,
            "context.context_projection.current_user_message.text",
            case["expect_context_projection_current_user_message"],
            current.get("text"),
        )
    for key, context_key in (
        ("expect_context_projection_recent_turn_count", "recent_turns"),
        ("expect_context_projection_recent_successful_tool_count", "recent_successful_tools"),
        ("expect_context_projection_evidence_ref_count", "available_evidence_refs"),
        ("expect_context_projection_open_gap_count", "open_evidence_gaps"),
    ):
        if case.get(key) is not None:
            values = projection.get(context_key) if isinstance(projection.get(context_key), list) else []
            _add_check(checks, f"context.context_projection.{context_key}_count", int(case[key]), len(values))
    for tool_name in case.get("expect_context_projection_recent_tools") or ():
        tools = [
            str(item.get("tool_name") or "")
            for item in projection.get("recent_successful_tools") or []
            if isinstance(item, dict)
        ]
        _add_check(
            checks,
            f"context.context_projection.recent_tool.{tool_name}",
            True,
            str(tool_name) in set(tools),
        )
    for source_tool in case.get("expect_context_projection_evidence_source_tools") or ():
        refs = [
            str(item.get("source_tool") or "")
            for item in projection.get("available_evidence_refs") or []
            if isinstance(item, dict)
        ]
        _add_check(
            checks,
            f"context.context_projection.evidence_source_tool.{source_tool}",
            True,
            str(source_tool) in set(refs),
        )
    if case.get("expect_context_projection_no_legacy_authority"):
        _add_check(checks, "context.no_active_frame_authority", True, not _contains_mapping_key(context, "active_frame"))
        _add_check(checks, "context.no_frame_stack_authority", True, not _contains_mapping_key(context, "frame_stack"))
        _add_check(checks, "context.no_followup_authority", True, not _contains_mapping_key(context, "followup_resolution"))


def _contains_mapping_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_contains_mapping_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


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
            "read_tool_selection_sources": list(budget.get("read_tool_selection_sources") or []),
            "read_tools_included": list(budget.get("read_tools_included") or []),
            "read_tools_omitted": list(budget.get("read_tools_omitted") or []),
        },
        "analysis_views": analysis_views,
        "context": _context_decision_summary(context),
    }


def _scenario_eval_actual_payload(
    *,
    case: dict[str, Any],
    projection: dict[str, Any],
    budget: dict[str, Any],
    context: dict[str, Any],
    analysis_views: list[str],
    validation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "family": str(case.get("family") or ""),
        "projection": _projection_eval_actual_payload(projection),
        "planner_context": _context_eval_actual_payload(
            budget=budget,
            context=context,
            analysis_views=analysis_views,
        ),
        "validation": _validation_eval_actual_payload(validation),
        "decision": _plan_decision_actual_payload(_case_plan_payload(case)),
    }


def _plan_decision_actual_payload(plan_payload: dict[str, Any]) -> dict[str, Any]:
    plan = plan_payload if isinstance(plan_payload, dict) else {}
    steps = [dict(item) for item in plan.get("steps") or [] if isinstance(item, dict)]
    first = steps[0] if steps else {}
    first_tool = str(first.get("tool_name") or "")
    first_arguments = dict(first.get("arguments") or {}) if isinstance(first.get("arguments"), dict) else {}
    contract = plan.get("task_contract") if isinstance(plan.get("task_contract"), dict) else {}
    context_use = plan.get("context_use") if isinstance(plan.get("context_use"), dict) else {}
    return {
        "terminal": _plan_terminal_kind(plan=plan, steps=steps, first_tool=first_tool, context_use=context_use),
        "tool_call_count": len(steps),
        "tool_sequence": [str(step.get("tool_name") or "") for step in steps],
        "first_tool": first_tool,
        "first_tool_family": _tool_family(first_tool),
        "first_arguments": first_arguments,
        "first_argument_keys": sorted(str(key) for key in first_arguments),
        "requested_effect": str(contract.get("requested_effect") or ""),
        "context_use_mode": str(context_use.get("mode") or "none"),
        "requires_clarification": bool(context_use.get("requires_clarification")),
    }


def _plan_terminal_kind(
    *,
    plan: dict[str, Any],
    steps: list[dict[str, Any]],
    first_tool: str,
    context_use: dict[str, Any],
) -> str:
    if bool(context_use.get("requires_clarification")) or str(context_use.get("mode") or "") == "ambiguous":
        return "clarification_request"
    if first_tool in _PREVIEW_TOOL_NAMES:
        return "preview_tool_call"
    if steps:
        return "read_tool_call"
    required_answer = [str(item) for item in plan.get("required_answer") or ()]
    if "clarification_request" in required_answer:
        return "clarification_request"
    return "no_tool"


_PREVIEW_TOOL_NAMES = frozenset(
    {
        "manual_trade_open",
        "manual_trade_close",
        "manual_trade_update",
        "manual_assignment",
        "manual_expiry",
        "symbol_edit",
        "model_use",
        "monitor_run_now",
        "upgrade_now",
    }
)


def _tool_family(tool_name: str) -> str:
    name = str(tool_name or "")
    if name in {"monthly_income_report"}:
        return "income"
    if name in {"analysis_query", "analysis_catalog"}:
        return "analysis"
    if name in {"option_positions_read"}:
        return "position"
    if name in {"candidate_filter_explain"}:
        return "candidate"
    if name in {"symbol_config_read", "symbol_resolve"}:
        return "config"
    if name in _PREVIEW_TOOL_NAMES:
        return "preview"
    if name in {"runtime_status", "healthcheck", "scheduler_status"}:
        return "runtime"
    return name or "none"


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


def _validation_eval_actual_payload(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "context_validation": {
            "schema_version": validation.get("schema_version"),
            "status": validation.get("status"),
            "code": validation.get("code"),
            "context_use_mode": validation.get("context_use_mode"),
            "referenced_turn_ids": list(validation.get("referenced_turn_ids") or []),
            "referenced_evidence_refs": list(validation.get("referenced_evidence_refs") or []),
            "warning_count": len(validation.get("warnings") or []),
            "violation": dict(validation.get("violation") or {}) if isinstance(validation.get("violation"), dict) else {},
        },
        "validated_slots": dict(validation.get("validated_slots") or {})
        if isinstance(validation.get("validated_slots"), dict)
        else {},
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
    projection = context.get("context_projection") if isinstance(context.get("context_projection"), dict) else {}
    return {
        "context_projection": context_projection_trace(projection),
        "context_policy": dict(context.get("context_policy") or {}) if isinstance(context.get("context_policy"), dict) else {},
    }


def _compact_list(value: Any) -> str:
    items = [str(item) for item in value or () if str(item).strip()]
    return ",".join(items) if items else "-"


__all__ = [
    "CONTEXT_EVAL_MODES",
    "CONTEXT_EVAL_SCHEMA_VERSION",
    "evaluate_context_case",
    "evaluate_projection_case",
    "evaluate_scenario_case",
    "format_context_eval_text",
    "normalize_context_eval_mode",
    "run_context_eval_suite",
]
