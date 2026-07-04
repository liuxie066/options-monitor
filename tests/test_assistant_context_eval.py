from __future__ import annotations

from pathlib import Path

from src.application.assistant.context_eval import format_context_eval_text, run_context_eval_suite


FIXTURE_DIR = Path(__file__).parent / "fixtures"
AGENT_EVAL_FIXTURE = FIXTURE_DIR / "assistant_agent_eval.jsonl"
PROJECTION_FIXTURE = FIXTURE_DIR / "assistant_context_projection.jsonl"
VALIDATION_FIXTURE = FIXTURE_DIR / "assistant_context_validation.jsonl"
SCENARIOS_FIXTURE = FIXTURE_DIR / "assistant_context_scenarios.jsonl"


def test_context_eval_projection_mode_runs_projection_fixture() -> None:
    report = run_context_eval_suite(fixture_path=PROJECTION_FIXTURE, mode="projection")
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}

    assert summary["mode"] == "projection"
    assert summary["ok"] is True
    assert summary["total"] == 1
    assert summary["failed"] == 0

    result = results["projection_keeps_recent_tool_ref"]
    actual = result["actual"]
    projection = actual["context_projection"]
    assert projection["recent_turn_count"] >= 2
    assert projection["recent_successful_tool_count"] == 2
    assert projection["evidence_ref_count"] == 2
    assert actual["safe_slots"]["account"] == ["lx", "sy"]
    assert actual["safe_slots"]["month"] == ["2026-06"]

    text = format_context_eval_text(report)
    assert "assistant context eval: 1/1 passed" in text
    assert "mode=projection" in text
    assert "refs=2" in text


def test_context_eval_validation_mode_runs_validation_fixture() -> None:
    report = run_context_eval_suite(fixture_path=VALIDATION_FIXTURE, mode="validation")
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}

    assert summary["mode"] == "validation"
    assert summary["ok"] is True
    assert summary["total"] == 10
    assert summary["failed"] == 0
    assert results["validation_blocks_unreferenced_carry"]["actual"]["context_validation"]["status"] == "blocked"
    assert results["validation_asks_when_truncated_context_has_no_ref"]["actual"]["context_validation"]["status"] == "ask_clarification"

    text = format_context_eval_text(report)
    assert "assistant context eval: 10/10 passed" in text
    assert "mode=validation" in text
    assert "validation=blocked" in text
    assert "code=CONTEXT_SLOT_NOT_AVAILABLE" in text


def test_context_eval_scenarios_mode_runs_real_followup_regression_fixture() -> None:
    report = run_context_eval_suite(fixture_path=SCENARIOS_FIXTURE, mode="scenarios")
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}
    families = {item["family"] for item in report["results"]}

    assert summary["mode"] == "scenarios"
    assert summary["ok"] is True
    assert summary["total"] == 23
    assert summary["failed"] == 0
    assert summary["empty"] is False
    runtime_budget = results["scenario_runtime_followup_carries_diagnostic_run_ref"]["actual"]["planner_context"][
        "manifest_budget"
    ]
    runtime_checks = {
        check["name"] for check in results["scenario_runtime_followup_carries_diagnostic_run_ref"]["checks"]
    }
    assert "read_tool_scope:runtime" in runtime_budget["read_tool_selection_sources"]
    assert "runtime_status" in runtime_budget["read_tools_included"]
    assert "monthly_income_report" in runtime_budget["read_tools_omitted"]
    assert "budget.read_tool_selection_sources.read_tool_scope:runtime" in runtime_checks
    assert "budget.read_tools_included.runtime_status" in runtime_checks
    assert "budget.read_tools_omitted.monthly_income_report" in runtime_checks
    assert families == {
        "assigned_stock_read",
        "assignment_preview",
        "candidate_followup",
        "candidate_standalone",
        "config_followup",
        "evidence_gap_carry",
        "expiry_preview",
        "explicit_current_scope_over_history",
        "explicit_switch",
        "income_followup",
        "income_metric_followup",
        "income_standalone",
        "manual_trade_preview",
        "metric_followup",
        "missing_data_read",
        "multi_topic_ambiguity",
        "no_context",
        "notice_explanation",
        "position_followup",
        "runtime_followup",
        "symbol_edit_current_message",
        "upgrade_preview",
        "upgrade_status",
    }

    text = format_context_eval_text(report)
    assert "assistant context eval: 23/23 passed" in text
    assert "mode=scenarios" in text
    assert "family=income_followup" in text
    assert "read_scope=read_tool_scope:runtime" in text
    assert "read_scope=read_tool_scope:preview_manual_trade" in text
    assert "terminal=preview_tool_call tool=manual_assignment" in text
    assert "scenario_low_risk_missing_data_no_clarification" in text
    assert "terminal=read_tool_call" in text
    assert "validation=ask_clarification" in text
