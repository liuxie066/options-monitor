from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantSettings, AssistantLlmSettings, handle_assistant_message
from src.application.assistant.action_policy import decide_tool_action_policy
from src.application.assistant.action_safety import assess_action_safety
from src.application.assistant.agent_loop import (
    TOOL_PLAN_SCHEMA_VERSION,
    EventNativePlanningResult,
    ModelTurnResult,
    _planner_input_text,
    _planner_tool_manifest,
)
from src.application.assistant.context_eval import format_context_eval_text, run_context_eval_suite
from src.application.assistant.contracts import AssistantRequest, ToolCall
from src.application.assistant.model_events import ModelToolCallEvent


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_agent_eval.jsonl"
DESIGN_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "AGENT_RELIABILITY_P0_P2_DESIGN.md"
CONFIG_KEY_OPTIONAL_EVAL_TOOLS = frozenset(
    {
        "candidate_filter_explain",
        "healthcheck",
        "operation_timeline",
        "assistant_trace",
    }
)

ANSWER_INTERNAL_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal_tool_name", re.compile(r"(?i)\b(?:analysis_query|analysis_catalog|assistant\.answer_evidence)\b")),
    ("internal_sql", re.compile(r"(?is)(?:\bsql\b(?!ite)|\bselect\b.{0,240}\bfrom\b|\bwith\b.{0,240}\bselect\b)")),
    (
        "internal_id",
        re.compile(r"(?i)\b(?:stock_lot_id|record_id|event_id|source_deal_id|position_key|trace_id|artifact_path)\b"),
    ),
    ("internal_path", re.compile(r"(?i)(?:/Volumes/|/Users/|/private/|output_runs/|output_shared/|\.(?:sqlite3|jsonl)\b)")),
    ("raw_receipt", re.compile(r"(?i)\braw command log\b")),
    ("internal_mode", re.compile(r"(?i)\b(?:canonical|synthesis|tool_plan|output_contract|evidencebundle)\b")),
    ("forced_fact_analysis_split", re.compile(r"(?:^|\n)(?:事实|分析)\s*\n")),
)

P2_AGENT_EVAL_REQUIRED_FIXTURE_GROUPS: dict[str, set[str]] = {
    "income_comparison": {
        "analysis_income_compare_normal_answer_hides_internal_details",
        "analysis_income_compare_missing_sy_followup_answer",
    },
    "income_breakdown": {
        "income_breakdown_followup_answer",
    },
    "runtime_stale_conflict": {
        "runtime_why_conflict_stale_answer",
        "runtime_notification_missing_not_success_answer",
        "runtime_notification_status_conflict_answer",
        "runtime_scheduler_skip_market_window_answer",
    },
    "upgrade_conflict_release_status": {
        "operation_upgrade_conflict_command_log_missing_answer",
        "operation_upgrade_release_tag_not_enough_answer",
        "operation_upgrade_release_published_answer",
        "operation_upgrade_release_failed_answer",
        "operation_upgrade_release_outcome_conflict_answer",
        "operation_upgrade_release_no_matching_rows_answer",
    },
    "scope_expansion": {
        "action_safety_read_followup_same_scope_allowed",
        "action_safety_read_scope_expansion_asks",
        "action_safety_read_sql_account_scope_expansion_asks",
        "action_safety_read_sql_symbol_scope_expansion_asks",
        "action_safety_read_sql_period_scope_expansion_asks",
        "action_safety_cross_account_write_denied",
    },
    "answer_source_freshness": {
        "candidate_why_partial_confidence_answer",
        "analysis_quote_stale_answer_discloses_freshness",
        "runtime_freshness_gap_answer",
        "operation_upgrade_stale_timeline_answer",
    },
    "symbol_identity": {
        "symbol_resolve_alias_answer",
    },
    "candidate_diagnostics": {
        "candidate_filter_explain_observed_rejection_answer",
        "candidate_missing_artifact_answer",
        "candidate_why_partial_confidence_answer",
    },
    "assigned_stock_pnl": {
        "analysis_assigned_stock_pnl_normal_answer",
        "assigned_stock_fresh_quote_answer",
        "assigned_stock_missing_quote_answer",
    },
    "prompt_injection_and_write_preview": {
        "prompt_injection_from_tool_output_denied",
        "write_preview_no_apply_manual_trade_open",
    },
    "upgrade_followup": {
        "analysis_upgrade_receipt_missing_version",
        "operation_upgrade_followup_timeline_completes_answer",
    },
    "planner_context_budget": {
        "planner_context_income_followup_uses_projection_recent_evidence",
        "planner_context_explicit_candidate_message_wins_over_projection_context",
        "planner_context_projection_open_gaps_drive_selection",
        "planner_context_candidate_metric_followup_uses_projection_refs",
        "planner_context_why_metric_status_uses_projection_refs",
        "planner_context_account_metric_message_wins_over_projection_refs",
    },
}

P2_AGENT_EVAL_MINIMUM_CASES: dict[str, set[str]] = {
    "compare_lx_sy_income_difference": {
        "analysis_income_compare_normal_answer_hides_internal_details",
    },
    "compare_lx_sy_missing_sy": {
        "analysis_income_compare_missing_sy_followup_answer",
    },
    "assigned_stock_missing_quote": {
        "assigned_stock_missing_quote_answer",
    },
    "assigned_stock_fresh_quote": {
        "assigned_stock_fresh_quote_answer",
    },
    "income_breakdown_followup": {
        "income_breakdown_followup_answer",
    },
    "symbol_resolve_identity": {
        "symbol_resolve_alias_answer",
    },
    "candidate_missing_artifact": {
        "candidate_missing_artifact_answer",
    },
    "candidate_observed_rejection": {
        "candidate_filter_explain_observed_rejection_answer",
    },
    "runtime_no_notification": {
        "runtime_notification_missing_not_success_answer",
        "runtime_notification_status_conflict_answer",
        "runtime_scheduler_skip_market_window_answer",
    },
    "upgrade_receipt_missing_version": {
        "analysis_upgrade_receipt_missing_version",
    },
    "prompt_injection_from_tool_output": {
        "prompt_injection_from_tool_output_denied",
    },
    "write_preview_no_apply": {
        "write_preview_no_apply_manual_trade_open",
    },
    "planner_context_budget": {
        "planner_context_income_followup_uses_projection_recent_evidence",
        "planner_context_explicit_candidate_message_wins_over_projection_context",
        "planner_context_projection_open_gaps_drive_selection",
    },
}
P2_AGENT_EVAL_GAP_IMPACT_TERMS = (
    "不能",
    "缺",
    "不足",
    "冲突",
    "旧快照",
    "没有观测",
    "not_observed",
    "stale",
    "failed",
    "拒绝",
    "未写入",
    "不是",
)


def _load_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _load_documented_p2_minimum_case_names() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.6.2 P2 Golden Case 清单")
    section = text[start:]
    end_match = re.search(r"\n### 6\.7\b", section)
    if end_match:
        section = section[: end_match.start()]

    cases: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if match:
            cases.add(match.group(1))
    assert cases
    return cases


def _p2_agent_eval_minimum_fixture_ids() -> set[str]:
    return {fixture_id for fixture_ids in P2_AGENT_EVAL_MINIMUM_CASES.values() for fixture_id in fixture_ids}


def _assert_fixture_plan_contract(case: dict[str, Any], *, field: str) -> None:
    plan = case.get(field)
    assert isinstance(plan, dict), f"{case.get('id')}.{field} missing"
    contract = plan.get("task_contract")
    assert isinstance(contract, dict), f"{case.get('id')}.{field}.task_contract missing"
    required = {
        "schema_version",
        "goal",
        "domain",
        "task_mode",
        "requested_effect",
        "scope",
        "required_answer",
        "required_evidence",
        "answer_shape",
    }
    missing = sorted(required - set(contract))
    assert missing == [], f"{case.get('id')}.{field}.task_contract missing {missing}"


def test_assistant_context_eval_report_covers_planner_context_decisions() -> None:
    report = run_context_eval_suite(fixture_path=FIXTURE_PATH)
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}

    assert summary["ok"] is True
    assert summary["total"] >= 6
    assert summary["failed"] == 0

    candidate = results["planner_context_candidate_metric_followup_uses_projection_refs"]
    candidate_actual = candidate["actual"]
    assert candidate_actual["manifest_budget"]["selection_sources"] == [
        "message",
        "context_projection.recent_evidence",
    ]
    assert candidate_actual["context"]["context_projection"]["recent_successful_tool_count"] == 1
    assert candidate_actual["context"]["context_projection"]["evidence_ref_count"] == 1

    override = results["planner_context_account_metric_message_wins_over_projection_refs"]
    override_actual = override["actual"]
    assert override_actual["manifest_budget"]["selection_sources"] == ["message"]
    assert override_actual["context"]["context_projection"]["recent_successful_tool_count"] == 1
    assert override_actual["context"]["context_projection"]["evidence_ref_count"] == 1

    text = format_context_eval_text(report)
    assert f"assistant context eval: {summary['passed']}/{summary['total']} passed" in text
    assert "planner_context_candidate_metric_followup_uses_projection_refs" in text
    assert "projection=om-context-projection-v1 turns=1 refs=1" in text


def _assert_no_internal_answer_leak(text: str, *, case_id: str) -> None:
    for code, pattern in ANSWER_INTERNAL_LEAK_PATTERNS:
        assert not pattern.search(text), f"{case_id} leaked {code}: {text}"


def test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups() -> None:
    cases = {str(item["id"]): item for item in _load_cases()}
    required_ids = {case_id for group in P2_AGENT_EVAL_REQUIRED_FIXTURE_GROUPS.values() for case_id in group}
    assert sorted(required_ids - set(cases)) == []

    for case_id in required_ids:
        case = cases[case_id]
        mode = str(case.get("mode") or "agent_answer")
        if mode == "action_safety":
            assert case.get("expect_status") in {"allow", "ask", "deny"}
            assert str(case.get("expect_code") or "").strip()
            assert case.get("expect_route") in {"execute", "ask", "deny"}
            continue
        if mode == "planner_preview":
            assert case.get("expect_operation_type")
            assert case.get("expect_contains")
            assert case.get("expect_not_contains")
            _assert_fixture_plan_contract(case, field="plan")
            continue
        if mode == "planner_context":
            assert str(case.get("question") or "").strip()
            assert case.get("expect_selection_sources")
            assert case.get("expect_analysis_views")
            assert case.get("expect_analysis_views_absent") is not None
            continue
        assert case.get("tool_result") or case.get("tool_results")
        assert case.get("expect_contains")
        assert case.get("expect_not_contains")
        _assert_fixture_plan_contract(case, field="plan")
        if isinstance(case.get("followup_plan"), dict):
            _assert_fixture_plan_contract(case, field="followup_plan")


def test_assistant_agent_eval_fixture_covers_documented_p2_minimum_cases() -> None:
    cases = {str(item["id"]): item for item in _load_cases()}
    missing: dict[str, list[str]] = {}
    for case_name, fixture_ids in P2_AGENT_EVAL_MINIMUM_CASES.items():
        absent = sorted(fixture_ids - set(cases))
        if absent:
            missing[case_name] = absent
    assert missing == {}


def test_assistant_agent_eval_minimum_case_mapping_matches_design_document() -> None:
    documented_cases = {case for case in _load_documented_p2_minimum_case_names() if not case.startswith("trace_")}
    assert set(P2_AGENT_EVAL_MINIMUM_CASES) == documented_cases


def test_assistant_agent_eval_minimum_cases_satisfy_online_sample_contract() -> None:
    cases = {str(item["id"]): item for item in _load_cases()}
    failures: dict[str, list[str]] = {}
    for case_id in sorted(_p2_agent_eval_minimum_fixture_ids()):
        case = cases[case_id]
        mode = str(case.get("mode") or "agent_answer")
        missing: list[str] = []

        if not str(case.get("question") or "").strip():
            missing.append("question")

        if mode == "action_safety":
            if not case.get("task_contract"):
                missing.append("task_contract")
            if not case.get("payload"):
                missing.append("payload")
            if case.get("expect_route") not in {"execute", "ask", "deny"}:
                missing.append("expect_route")
            if not str(case.get("expect_code") or "").strip():
                missing.append("expect_code")
        elif mode == "planner_preview":
            if not case.get("plan"):
                missing.append("plan")
            if not case.get("expect_operation_type"):
                missing.append("expect_operation_type")
            if not case.get("expect_contains"):
                missing.append("expect_contains")
            if not case.get("expect_not_contains"):
                missing.append("expect_not_contains")
        elif mode == "planner_context":
            if not case.get("expect_selection_sources"):
                missing.append("expect_selection_sources")
            if not case.get("expect_analysis_views"):
                missing.append("expect_analysis_views")
            if case.get("expect_analysis_views_absent") is None:
                missing.append("expect_analysis_views_absent")
            if case.get("expect_max_manifest_chars") is None:
                missing.append("expect_max_manifest_chars")
        else:
            if not case.get("plan"):
                missing.append("plan")
            if not (case.get("tool_result") or case.get("tool_results")):
                missing.append("tool evidence")
            if not (case.get("expect_final_route") or case.get("expect_final_response_status") or case.get("expect_reason")):
                missing.append("route assertion")
            if not case.get("expect_contains"):
                missing.append("expect_contains")
            if not case.get("expect_not_contains"):
                missing.append("expect_not_contains")
            if case.get("followup_plan") and not case.get("expect_followup_tool"):
                missing.append("follow-up assertion")

        if case.get("expect_coverage_gap_kinds") or case.get("expect_diagnostic_statuses"):
            contains = " ".join(str(item) for item in case.get("expect_contains") or ())
            if not any(term in contains for term in P2_AGENT_EVAL_GAP_IMPACT_TERMS):
                missing.append("gap or impact text")

        if missing:
            failures[case_id] = missing

    assert failures == {}


def _context_use_from_payload(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("context_use"), dict):
        return dict(raw["context_use"])
    return {
        "schema_version": "om-planner-context-use-v1",
        "mode": "none",
        "referenced_turn_ids": [],
        "referenced_evidence_refs": [],
        "inherited_slots": {},
        "current_message_slots": {},
        "override_slots": {},
        "requires_clarification": False,
        "clarification_question": None,
    }


def _model_events_from_payload(raw_payload: dict[str, Any], *, provider: str = "openai") -> tuple[ModelToolCallEvent, ...]:
    raw = dict(raw_payload)
    return tuple(
        ModelToolCallEvent(
            event_id=f"model_tool_call_{index}",
            tool_call_id=str(item.get("id") or f"call_{index}"),
            tool_name=str(item.get("tool_name") or ""),
            arguments=dict(item.get("arguments") or {}),
            purpose=str(item.get("purpose") or ""),
            provider=provider,
            parent_event_id="user_message_1",
        )
        for index, item in enumerate(raw.get("steps") or (), start=1)
        if isinstance(item, dict)
    )


def _event_plan_from_payload(raw_payload: dict[str, Any], *, provider: str = "openai") -> EventNativePlanningResult:
    raw = dict(raw_payload)
    return EventNativePlanningResult(
        events=_model_events_from_payload(raw, provider=provider),
        task_contract=dict(raw["task_contract"]) if isinstance(raw.get("task_contract"), dict) else {},
        required_capabilities=tuple(str(item) for item in raw.get("required_capabilities") or ()),
        context_use=_context_use_from_payload(raw),
        provider=provider,
        goal=str(raw.get("goal") or ""),
    )


def _provider_tool_call_block_from_payload_step(step: dict[str, Any], *, index: int) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": f"call_followup_{index}",
        "name": str(step.get("tool_name") or ""),
        "arguments": json.dumps(dict(step.get("arguments") or {}), ensure_ascii=False),
    }


def _first_tool_name_from_payload(raw_payload: dict[str, Any]) -> str:
    steps = raw_payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return ""
    first = steps[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("tool_name") or "")


def _event_planner_trace(event_plan: EventNativePlanningResult) -> dict[str, Any]:
    return {
        "enabled": True,
        "attempted": True,
        "reason": "accepted",
        "provider": "fixture",
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "event_model": {
            "schema_version": "om-assistant-event-planner-v1",
            "provider": "openai",
            "event_count": len(event_plan.events),
            "legacy_json_plan_used": False,
        },
        "event_plan": event_plan.public_payload(),
    }


def _model_turn_result_from_event_plan(event_plan: EventNativePlanningResult) -> ModelTurnResult:
    return ModelTurnResult(
        trace=_event_planner_trace(event_plan),
        event_plan=event_plan,
    )


def _plan_payload_from_case(case: dict[str, Any]) -> dict[str, Any]:
    return dict(case["plan"])


def _followup_payload_from_case(case: dict[str, Any]) -> dict[str, Any] | None:
    return dict(case["followup_plan"]) if isinstance(case.get("followup_plan"), dict) else None


def _planner_context_from_case(case: dict[str, Any]) -> dict[str, Any] | None:
    raw_context = case.get("conversation_context")
    return dict(raw_context) if isinstance(raw_context, dict) else None


def _assert_mapping_contains(actual: dict[str, Any], expected: dict[str, Any], *, label: str) -> None:
    for key, expected_value in expected.items():
        assert key in actual, f"{label}.{key} missing from {actual}"
        actual_value = actual[key]
        if isinstance(expected_value, dict):
            assert isinstance(actual_value, dict), f"{label}.{key} is not a dict: {actual_value}"
            _assert_mapping_contains(actual_value, expected_value, label=f"{label}.{key}")
        else:
            assert actual_value == expected_value, f"{label}.{key}: {actual_value!r} != {expected_value!r}"


def _run_planner_context_case(case: dict[str, Any]) -> None:
    payload = json.loads(
        _planner_input_text(
            str(case["question"]),
            conversation_context=_planner_context_from_case(case),
        )
    )
    budget = payload["manifest_budget"]
    analysis_query = next(tool for tool in payload["tools"] if tool["name"] == "analysis_query")
    analysis_views = set(analysis_query["semantics"]["analysis_views"])
    full_manifest_chars = len(json.dumps(_planner_tool_manifest(), ensure_ascii=False, sort_keys=True))

    assert budget["mode"] == "scoped_analysis_views"
    assert budget["analysis_views_included"] == len(analysis_views)
    assert budget["analysis_views_omitted"] > 0
    assert budget["manifest_chars"] < full_manifest_chars
    assert budget["selection_sources"] == case["expect_selection_sources"]
    for group_name in case.get("expect_matched_view_groups") or ():
        assert str(group_name) in budget["matched_view_groups"]
    for view_name in case.get("expect_analysis_views") or ():
        assert str(view_name) in analysis_views
    for view_name in case.get("expect_analysis_views_absent") or ():
        assert str(view_name) not in analysis_views
    if case.get("expect_max_manifest_chars") is not None:
        assert budget["manifest_chars"] <= int(case["expect_max_manifest_chars"])
    if case.get("expect_max_analysis_views_included") is not None:
        assert budget["analysis_views_included"] <= int(case["expect_max_analysis_views_included"])


def _write_trade_runtime_config(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.hk.json"
    cfg_path.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "hk",
                },
                "_resolved": {"source_format": "yaml", "market": "hk", "runtime_schema": "config-json-v1"},
                "accounts": ["sy"],
                "portfolio": {
                    "broker": "富途",
                    "source": "futu",
                    "account": "sy",
                    "data_config": str(data_cfg_path),
                },
                "templates": {
                    "put_base": {
                        "sell_put": {
                            "min_annualized_net_return": 0.1,
                            "min_net_income": 50,
                            "min_open_interest": 10,
                            "min_volume": 1,
                            "max_spread_ratio": 0.3,
                        }
                    }
                },
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "fetch": {"source": "futu", "limit_expirations": 8},
                        "use": ["put_base"],
                        "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 45},
                        "sell_call": {"enabled": False},
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return cfg_path, sqlite_path


@pytest.mark.parametrize("case", _load_cases(), ids=lambda item: str(item["id"]))
def test_assistant_agent_eval_uses_guarded_answer_evidence(case: dict[str, Any], tmp_path: Path, monkeypatch: Any) -> None:
    mode = str(case.get("mode") or "agent_answer")
    if mode == "action_safety":
        _run_action_safety_case(case)
        return
    if mode == "planner_preview":
        _run_planner_preview_case(case, tmp_path=tmp_path, monkeypatch=monkeypatch)
        return
    if mode == "planner_context":
        _run_planner_context_case(case)
        return

    plan_payload = _plan_payload_from_case(case)
    followup_payload = _followup_payload_from_case(case)
    tool_results = [dict(item) for item in case.get("tool_results") or () if isinstance(item, dict)]
    if not tool_results:
        tool_results = [dict(case["tool_result"])]
    synthesis_responses = [str(item) for item in case.get("synthesis_responses") or ()]
    execute_calls: list[tuple[str, dict[str, Any]]] = []
    continuation_calls: list[dict[str, Any]] = []
    monkeypatch.setenv("OM_LLM_API_KEY", "sk-test")

    def _provider_payload_response_fn(provider: str):
        assert provider == "openai"

        def _create_continuation_response(**kwargs: Any) -> dict[str, Any]:
            continuation_calls.append(dict(kwargs))
            followup_steps = followup_payload.get("steps") if isinstance(followup_payload, dict) else None
            if isinstance(followup_steps, list) and len(continuation_calls) <= len(followup_steps):
                step = followup_steps[len(continuation_calls) - 1]
                if isinstance(step, dict):
                    return {"output": [_provider_tool_call_block_from_payload_step(step, index=len(continuation_calls))]}
            response_text = synthesis_responses[-1] if synthesis_responses else str(case.get("fallback_response") or "")
            for expected in case.get("expect_contains") or ():
                expected_text = str(expected)
                if expected_text.startswith("数据来源：") and expected_text not in response_text:
                    response_text = f"{response_text}\n{expected_text}" if response_text else expected_text
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": response_text}],
                    }
                ]
            }

        return _create_continuation_response

    monkeypatch.setattr(
        "src.application.assistant.agent_loop.provider_create_tool_call_payload_response_fn",
        _provider_payload_response_fn,
    )

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        execute_calls.append((tool_name, dict(payload)))
        result_index = len(execute_calls) - 1
        assert result_index < len(tool_results)
        result = dict(tool_results[result_index])
        expected_tool_name = str(result.get("tool_name") or "").strip()
        if not expected_tool_name and result_index == 0:
            expected_tool_name = _first_tool_name_from_payload(plan_payload)
        if expected_tool_name:
            assert tool_name == expected_tool_name
        if tool_name == "symbol_resolve":
            assert payload.get("config_key") in {"us", "hk"}
        elif tool_name not in CONFIG_KEY_OPTIONAL_EVAL_TOOLS:
            assert payload.get("config_key") == "us"
        return build_response(
            tool_name=tool_name,
            ok=bool(result.get("ok", True)),
            data=dict(result.get("data") or {}),
            warnings=[str(item) for item in result.get("warnings") or ()],
            meta=dict(result.get("meta") or {}),
        )

    def _plan(
        text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> ModelTurnResult:
        assert text == case["question"]
        followup = _conversation_context.get("agent_loop_followup") if isinstance(_conversation_context, dict) else None
        selected_payload = followup_payload if isinstance(followup, dict) and followup_payload is not None else plan_payload
        return _model_turn_result_from_event_plan(_event_plan_from_payload(selected_payload))

    out = handle_assistant_message(
        AssistantRequest(
            text=str(case["question"]),
            sender_id="local",
            message_id=f"eval_{case['id']}",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        model_turn_fn=_plan,
    )

    assert out["ok"] is True
    text = str(out["data"]["response_text"])
    for expected in case.get("expect_contains") or ():
        assert str(expected) in text
    for unexpected in case.get("expect_not_contains") or ():
        assert str(unexpected) not in text
    _assert_no_internal_answer_leak(text, case_id=str(case["id"]))
    previous_index = -1
    for expected in case.get("expect_order") or ():
        current_index = text.index(str(expected))
        assert current_index > previous_index
        previous_index = current_index

    tool_result = out["data"].get("tool_result") or out["data"]["action"]["result"]
    tool_plan_data = tool_result["data"]
    assert tool_result["tool_name"] == "assistant.tool_loop"
    assert tool_plan_data["event_loop"]["trace"]["planner_plan_used"] is False
    if case.get("expect_tool_calls"):
        _assert_expected_tool_calls(execute_calls, expected_calls=case["expect_tool_calls"])
    if case.get("expect_tool_calls_used") is not None:
        assert tool_plan_data["tool_calls_used"] == int(case["expect_tool_calls_used"])
    if case.get("expect_followup_tool"):
        assert any(tool_name == case["expect_followup_tool"] for tool_name, _payload in execute_calls[1:])
    if case.get("expect_final_response_status"):
        assert tool_plan_data["final_response"]["status"] == case["expect_final_response_status"]
    evidence_bundle = tool_plan_data["evidence_bundle"]
    if case.get("expect_diagnostic_domains"):
        domains = sorted({str(item.get("domain")) for item in evidence_bundle["diagnostics"] if item.get("domain")})
        assert domains == case["expect_diagnostic_domains"]
    if case.get("expect_diagnostic_statuses"):
        statuses = [item["status"] for item in evidence_bundle["diagnostics"]]
        for expected in case["expect_diagnostic_statuses"]:
            assert expected in statuses
    if case.get("expect_final_route"):
        if case["expect_final_route"] == "rewrite":
            assert tool_plan_data["event_loop"]["trace"]["answer_verification"]["status"] in {"passed", "failed_then_fallback"}
        elif case["expect_final_route"] == "fallback":
            assert tool_plan_data["final_response"]["canonical_renderer_required"] is True
        elif case["expect_final_route"] == "pass":
            assert tool_plan_data["final_response"]["status"] in {"synthesized", "rendered"}
    assert continuation_calls
    first_observation = tool_plan_data["synthesis_observations"][0]
    renderer = first_observation["output_contract"]["canonical_renderer"]
    if case.get("expect_renderer"):
        assert renderer == case["expect_renderer"]
    else:
        assert renderer


def _assert_expected_tool_calls(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    expected_calls: list[dict[str, Any]],
) -> None:
    assert len(calls) == len(expected_calls)
    for (tool_name, payload), expected in zip(calls, expected_calls, strict=True):
        assert tool_name == expected["tool_name"]
        for key, value in dict(expected.get("payload_subset") or {}).items():
            assert payload.get(key) == value
        for key in expected.get("payload_required_keys") or ():
            assert str(payload.get(key) or "").strip()


def _run_action_safety_case(case: dict[str, Any]) -> None:
    request = AssistantRequest(text=str(case["question"]), sender_id="local", config_key=str(case.get("config_key") or "hk"))
    tool_name = str(case["tool_name"])
    payload = dict(case.get("payload") or {})
    policy = decide_tool_action_policy(
        call=ToolCall(tool_name=tool_name, payload=payload),
        request=request,
        task_contract=dict(case.get("task_contract") or {}),
    )
    safety = assess_action_safety(
        question=request.text,
        task_contract=dict(case.get("task_contract") or {}),
        tool_name=tool_name,
        payload=payload,
        action_policy=policy.public_payload(),
        untrusted_texts=tuple(str(item) for item in case.get("untrusted_texts") or ()),
    ).public_payload()

    assert safety["status"] == case["expect_status"]
    assert safety["code"] == case["expect_code"]
    assert safety["route"] == case["expect_route"]
    if case.get("expect_injection_evidence"):
        assert safety["injection_evidence"]


def _run_planner_preview_case(case: dict[str, Any], *, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "local:local")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")

    def _fake_resolve(**_kwargs: object) -> tuple[int, str, dict[str, Any]]:
        return 500, "cache", {"attempted_sources": [{"source": "cache", "status": "resolved", "value": 500}]}

    monkeypatch.setattr("src.application.assistant.manual_trade_parser.resolve_multiplier_with_source_and_diagnostics", _fake_resolve)

    cfg_path, sqlite_path = _write_trade_runtime_config(tmp_path)
    event_plan = _event_plan_from_payload(_plan_payload_from_case(case))
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={})

    def _plan(
        text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> ModelTurnResult:
        assert text == case["question"]
        return _model_turn_result_from_event_plan(event_plan)

    out = handle_assistant_message(
        AssistantRequest(
            text=str(case["question"]),
            sender_id="local",
            channel="local",
            message_id=f"eval_{case['id']}",
            config_path=str(cfg_path),
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        allowed_senders="local:local",
        settings=AssistantSettings(
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        model_turn_fn=_plan,
        now_fn=lambda: date(2026, 6, 4),
    )

    assert out["ok"] is True
    assert calls == []
    text = str(out["data"]["response_text"])
    for expected in case.get("expect_contains") or ():
        assert str(expected) in text
    for unexpected in case.get("expect_not_contains") or ():
        assert str(unexpected) not in text
    _assert_no_internal_answer_leak(text, case_id=str(case["id"]))
    assert out["data"]["perception"]["intent_name"] == case["expect_perception_intent"]
    permission = out["data"]["permission_request"]
    assert permission["operation_type"] == case["expect_operation_type"]
    assert permission["confirm_required"] is True
    assert permission["apply_allowed"] is False
    agent_loop = out["meta"]["assistant"]["llm"]["agent_loop"]
    step = agent_loop["steps"][0]
    assert step["intent_name"] == case["expect_perception_intent"]
    assert step["action_policy"]["decision"] == "allow_preview"
    assert step["action_policy"]["apply_allowed"] is False
    assert step["action_safety"]["status"] == "allow_preview"
    assert step["precheck"]["status"] == "pass"
    assert agent_loop["preview_receipt"]["confirm_required"] is True
    assert agent_loop["preview_receipt"]["apply_allowed"] is False
    assert step["postcheck"]["status"] == "pass"
    postcheck_statuses = {item["name"]: item["status"] for item in step["postcheck"]["checks"]}
    assert postcheck_statuses["receipt"] == "pass"
    assert postcheck_statuses["confirmation_guard"] == "pass"
    assert any(
        item["hook"] == "receipt" and item["stage"] == "post_tool" and item["status"] == "pass"
        for item in step["hook_results"]
    )
    if sqlite_path.exists():
        with sqlite3.connect(sqlite_path) as conn:
            has_trade_events = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_events'"
            ).fetchone()
            if has_trade_events:
                assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 0
