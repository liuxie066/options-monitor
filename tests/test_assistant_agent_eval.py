from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantSettings, handle_assistant_turn
from src.application.assistant.context_eval import format_context_eval_text, run_context_eval_suite
from src.application.assistant.contracts import AssistantRequest


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_agent_eval.jsonl"
DESIGN_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "AGENT_RELIABILITY_P0_P2_DESIGN.md"

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
    "income_breakdown": {"income_breakdown_followup_answer"},
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
    "symbol_identity": {"symbol_resolve_alias_answer"},
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
    "copilot_context_budget": {
        "copilot_context_income_followup_uses_projection_recent_evidence",
        "copilot_context_explicit_candidate_message_wins_over_projection_context",
        "copilot_context_projection_open_gaps_drive_selection",
        "copilot_context_candidate_metric_followup_uses_projection_refs",
        "copilot_context_why_metric_status_uses_projection_refs",
        "copilot_context_account_metric_message_wins_over_projection_refs",
    },
}

P2_AGENT_EVAL_MINIMUM_CASES: dict[str, set[str]] = {
    "compare_lx_sy_income_difference": {"analysis_income_compare_normal_answer_hides_internal_details"},
    "compare_lx_sy_missing_sy": {"analysis_income_compare_missing_sy_followup_answer"},
    "assigned_stock_missing_quote": {"assigned_stock_missing_quote_answer"},
    "assigned_stock_fresh_quote": {"assigned_stock_fresh_quote_answer"},
    "income_breakdown_followup": {"income_breakdown_followup_answer"},
    "symbol_resolve_identity": {"symbol_resolve_alias_answer"},
    "candidate_missing_artifact": {"candidate_missing_artifact_answer"},
    "candidate_observed_rejection": {"candidate_filter_explain_observed_rejection_answer"},
    "runtime_no_notification": {
        "runtime_notification_missing_not_success_answer",
        "runtime_notification_status_conflict_answer",
        "runtime_scheduler_skip_market_window_answer",
    },
    "upgrade_receipt_missing_version": {"analysis_upgrade_receipt_missing_version"},
    "prompt_injection_from_tool_output": {"prompt_injection_from_tool_output_denied"},
    "write_preview_no_apply": {"write_preview_no_apply_manual_trade_open"},
    "copilot_context_budget": {
        "copilot_context_income_followup_uses_projection_recent_evidence",
        "copilot_context_explicit_candidate_message_wins_over_projection_context",
        "copilot_context_projection_open_gaps_drive_selection",
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


@dataclass(frozen=True)
class CopilotGoldenCase:
    case_id: str
    question: str
    expect_tool: str
    expect_contains: tuple[str, ...]
    expect_not_contains: tuple[str, ...] = ()
    analysis_rows: bool = True


COPILOT_GOLDEN_CASES: tuple[CopilotGoldenCase, ...] = (
    CopilotGoldenCase(
        case_id="option_operation_review",
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        expect_tool="analysis_query",
        expect_contains=("结论：2026-06期权操作不够理想", "问题模式：", "优化建议：", "证据边界："),
        expect_not_contains=("其余 9 行未展开", "分析完成：共"),
    ),
    CopilotGoldenCase(
        case_id="monthly_income_drivers",
        question="6月收益主要来自哪里",
        expect_tool="analysis_query",
        expect_contains=("结论：2026-06收益需要按来源拆开看", "主要来源：", "主要标的是 0700.HK", "证据边界："),
    ),
    CopilotGoldenCase(
        case_id="assigned_stock_pnl",
        question="被指派股票的收益",
        expect_tool="option_positions_read",
        expect_contains=("结论：当前有 1 条指派正股记录", "收益口径：", "优化建议：", "证据边界："),
    ),
    CopilotGoldenCase(
        case_id="option_operation_no_rows",
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        expect_tool="analysis_query",
        expect_contains=("不能判断期权操作是否不合理", "行级记录为 0", "空结果不能证明没有问题"),
        expect_not_contains=("期权操作偏保守", "未发现单一异常模式"),
        analysis_rows=False,
    ),
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


def _assert_no_internal_answer_leak(text: str, *, case_id: str) -> None:
    for code, pattern in ANSWER_INTERNAL_LEAK_PATTERNS:
        assert not pattern.search(text), f"{case_id} leaked {code}: {text}"


def test_assistant_context_eval_report_covers_copilot_context_decisions() -> None:
    report = run_context_eval_suite(fixture_path=FIXTURE_PATH)
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}

    assert summary["ok"] is True
    assert summary["total"] >= 6
    assert summary["failed"] == 0

    candidate = results["copilot_context_candidate_metric_followup_uses_projection_refs"]["actual"]
    assert candidate["manifest_budget"]["selection_sources"] == [
        "copilot.evidence_plan",
        "message",
        "context_projection.recent_evidence",
    ]
    assert candidate["context"]["context_projection"]["recent_successful_tool_count"] == 1
    assert candidate["context"]["context_projection"]["evidence_ref_count"] == 1

    override = results["copilot_context_account_metric_message_wins_over_projection_refs"]["actual"]
    assert override["manifest_budget"]["selection_sources"] == ["copilot.evidence_plan", "message"]
    assert override["context"]["context_projection"]["recent_successful_tool_count"] == 1
    assert override["context"]["context_projection"]["evidence_ref_count"] == 1

    text = format_context_eval_text(report)
    assert f"assistant context eval: {summary['passed']}/{summary['total']} passed" in text
    assert "copilot_context_candidate_metric_followup_uses_projection_refs" in text
    assert "projection=om-context-projection-v1 turns=1 refs=1" in text


def test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups() -> None:
    cases = {str(item["id"]): item for item in _load_cases()}
    required_ids = {case_id for group in P2_AGENT_EVAL_REQUIRED_FIXTURE_GROUPS.values() for case_id in group}
    assert sorted(required_ids - set(cases)) == []

    for case_id in required_ids:
        case = cases[case_id]
        mode = str(case.get("mode") or "agent_answer")
        assert str(case.get("question") or "").strip()
        if mode == "action_safety":
            assert case.get("expect_status") in {"allow", "ask", "deny"}
            assert str(case.get("expect_code") or "").strip()
            assert case.get("expect_route") in {"execute", "ask", "deny"}
        elif mode == "planner_preview":
            assert case.get("expect_operation_type")
            assert case.get("expect_contains")
            assert case.get("expect_not_contains")
        elif mode == "copilot_context":
            assert case.get("expect_selection_sources")
            assert case.get("expect_analysis_views")
            assert case.get("expect_analysis_views_absent") is not None
        else:
            assert case.get("tool_result") or case.get("tool_results")
            assert case.get("expect_contains")
            assert case.get("expect_not_contains") is not None


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
            if not case.get("expect_operation_type"):
                missing.append("expect_operation_type")
            if not case.get("expect_contains"):
                missing.append("expect_contains")
            if not case.get("expect_not_contains"):
                missing.append("expect_not_contains")
        elif mode == "copilot_context":
            if not case.get("expect_selection_sources"):
                missing.append("expect_selection_sources")
            if not case.get("expect_analysis_views"):
                missing.append("expect_analysis_views")
            if case.get("expect_analysis_views_absent") is None:
                missing.append("expect_analysis_views_absent")
            if case.get("expect_max_manifest_chars") is None:
                missing.append("expect_max_manifest_chars")
        else:
            if not (case.get("tool_result") or case.get("tool_results")):
                missing.append("tool evidence")
            if not (case.get("expect_final_route") or case.get("expect_final_response_status") or case.get("expect_reason")):
                missing.append("route assertion")
            if not case.get("expect_contains"):
                missing.append("expect_contains")
            if case.get("followup_plan") and not case.get("expect_followup_tool"):
                missing.append("follow-up assertion")

        if case.get("expect_coverage_gap_kinds") or case.get("expect_diagnostic_statuses"):
            contains = " ".join(str(item) for item in case.get("expect_contains") or ())
            if not any(term in contains for term in P2_AGENT_EVAL_GAP_IMPACT_TERMS):
                missing.append("gap or impact text")
        if missing:
            failures[case_id] = missing

    assert failures == {}


@pytest.mark.parametrize("case", COPILOT_GOLDEN_CASES, ids=lambda item: item.case_id)
def test_assistant_agent_eval_uses_guarded_answer_evidence(case: CopilotGoldenCase, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        if tool_name == "analysis_query":
            return _analysis_response(payload, rows=case.analysis_rows)
        if tool_name == "option_positions_read":
            return _assigned_stock_response()
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        AssistantRequest(
            text=case.question,
            sender_id="local",
            message_id=f"eval_{case.case_id}",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(),
        allowed_senders="local",
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.trace["route"] == "agent_loop"
    assert result.trace["answer_route"] == "copilot_answer"
    assert calls and calls[0][0] == case.expect_tool
    if case.expect_tool == "analysis_query":
        assert calls[0][1].get("month") == "2026-06"
    for expected in case.expect_contains:
        assert expected in result.response_text
    for unexpected in case.expect_not_contains:
        assert unexpected not in result.response_text
    _assert_no_internal_answer_leak(result.response_text, case_id=case.case_id)


def _analysis_response(payload: dict[str, Any], *, rows: bool) -> dict[str, Any]:
    views = [str(view) for view in payload.get("views") or []]
    if rows:
        view_datasets = {
            "account_monthly_performance": {"rows": [{"account": "sy", "realized": 5011}]},
            "account_monthly_income_components": {"rows": [{"component": "premium", "amount_cny": 2934}]},
            "symbol_income_attribution": {"rows": [{"symbol": "0700.HK", "amount_cny": 3900}]},
            "monthly_income_cashflow_rows": {
                "rows": [{"symbol": "0700.HK", "trade_action": "assignment", "assignment_buy_cash": 269000}]
            },
            "monthly_income_realized_rows": {"rows": [{"symbol": "0700.HK", "amount_cny": 5011}]},
            "monthly_income_premium_rows": {"rows": [{"symbol": "0700.HK", "amount_cny": 2934}]},
            "trade_events": {"rows": [{"symbol": "0700.HK", "action": "sell_put"}]},
            "open_option_exposure": {"rows": [{"symbol": "0700.HK", "notional": 300000}]},
            "strategy_config_by_symbol_account": {"rows": [{"symbol": "0700.HK", "account": "sy"}]},
            "strategy_replay_read_surface": {"rows": [{"symbol": "0700.HK", "decision": "accepted"}]},
        }
    else:
        view_datasets = {view: {"rows": [], "row_count": 0} for view in views}
    return build_response(
        tool_name="analysis_query",
        ok=True,
        data={
            "schema_version": "analysis.query.output.v2",
            "source_label": "OM read-only analysis workspace",
            "views_used": views,
            "view_datasets": {view: view_datasets.get(view, {"rows": []}) for view in views},
            "evidence": {
                "diagnostics": [
                    {"answer_boundary": "cannot infer absence of problem from empty diagnostic result"},
                ]
            },
        },
    )


def _assigned_stock_response() -> dict[str, Any]:
    return build_response(
        tool_name="option_positions_read",
        ok=True,
        data={
            "rows": [
                {
                    "account": "sy",
                    "symbol": "0700.HK",
                    "assigned_stock_unrealized_pnl": 1200,
                    "assigned_stock_realized_pnl": 300,
                    "assignment_lifecycle_pnl": 1500,
                }
            ],
        },
    )
