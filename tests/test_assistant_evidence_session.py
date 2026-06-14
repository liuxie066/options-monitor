from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantRequest, AssistantSettings, LlmTranslatorSettings, handle_assistant_message
from src.application.assistant.agent_loop import (
    LlmPlannerResult,
    LlmSynthesisResult,
    PlannerPlan,
    PlannerPlanStep,
    TOOL_CHECK_SCHEMA_VERSION,
    TOOL_PLAN_SCHEMA_VERSION,
)
from src.application.assistant.action_policy import ACTION_POLICY_SCHEMA_VERSION
from src.application.assistant.action_safety import ACTION_SAFETY_SCHEMA_VERSION
from src.application.assistant.answer_verifier import verify_response_against_evidence, verify_response_shape
from src.application.assistant.coverage_verifier import COVERAGE_RESULT_SCHEMA_VERSION, verify_coverage
from src.application.assistant.evidence import DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION, EVIDENCE_BUNDLE_SCHEMA_VERSION, build_evidence_bundle
from src.application.assistant.session_store import AgentSessionStore, collect_assistant_trace, format_assistant_trace
from src.application.assistant.task_contract import TASK_CONTRACT_SCHEMA_VERSION, build_task_contract
from src.application.assistant.verifier_hooks import HOOK_RESULT_SCHEMA_VERSION
from src.application.tool_execution import execute_tool as run_tool


def test_evidence_bundle_extracts_contract_facts_and_missing_quote() -> None:
    bundle = build_evidence_bundle(
        question="查看 sy 指派正股持仓盈亏",
        plan={
            "goal": "查看 sy 指派正股持仓盈亏",
            "steps": [],
        },
        observations=[
            {
                "index": 1,
                "tool_name": "option_positions_read",
                "payload": {"action": "assigned-stock", "account": "sy", "status": "open", "refresh_quotes": True},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "option_positions_read.assigned_stock_output.v1",
                    "canonical_renderer": "assigned_stock_lifecycle",
                    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
                    "guard_profile": "position_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "rows[].account",
                        "rows[].symbol",
                        "rows[].currency",
                        "rows[].shares_remaining",
                        "rows[].spot",
                        "rows[].quote_status",
                        "rows[].assigned_stock_unrealized_pnl",
                    ],
                },
                "data": {
                    "action": "assigned-stock",
                    "filters": {"account": "sy", "status": "open", "refresh_quotes": True},
                    "rows": [
                        {
                            "account": "sy",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "status": "open",
                            "shares_remaining": 200,
                            "spot": None,
                            "quote_status": "missing_quote",
                            "assigned_stock_unrealized_pnl": None,
                        }
                    ],
                    "row_count": 1,
                    "quote_refresh": {
                        "status": "missing_quote",
                        "quote_source": "opend_realtime",
                        "missing_symbols": ["0700.HK"],
                    },
                },
            }
        ],
    )

    payload = bundle.public_payload()
    assert payload["schema_version"] == EVIDENCE_BUNDLE_SCHEMA_VERSION
    assert payload["scope"]["accounts"] == ["sy"]
    assert payload["scope"]["symbols"] == ["0700.HK"]
    assert payload["datasets"][0]["row_count"] == 1
    assert payload["datasets"][0]["source_label"] == "OM 本地 SQLite assigned_stock_events + trade_events"
    assert any(item["path"] == "rows[].shares_remaining" and item["value"] == 200 for item in payload["facts"])
    spot_fact = next(item for item in payload["facts"] if item["path"] == "rows[].spot")
    assert spot_fact["freshness"] == "missing"
    assert spot_fact["account"] == "sy"
    assert spot_fact["symbol"] == "0700.HK"
    assert any(
        item.get("kind") == "missing_quote"
        and (item.get("symbol") == "0700.HK" or "0700.HK" in (item.get("symbols") or []))
        for item in payload["missing_data"]
    )
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["schema_version"] == DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION
    assert diagnostic["domain"] == "quote_freshness"
    assert diagnostic["status"] == "observed_quote_gap"
    assert diagnostic["scope"]["accounts"] == ["sy"]
    assert diagnostic["scope"]["symbols"] == ["0700.HK"]
    assert diagnostic["source"]["view"] == "assigned_stock_lifecycle"
    assert diagnostic["observed_reason"] == "0700.HK has no usable as-of quote"
    assert diagnostic["answer_boundary"] == "quote_status_only_cannot_infer_upstream_root_cause"
    assert diagnostic["confidence"] == "direct"
    assert payload["guard_contracts"][0]["guard_profile"] == "position_rows"
    trace_payload = bundle.trace_payload()
    assert trace_payload["diagnostic_count"] == 1
    assert trace_payload["diagnostic_domains"] == ["quote_freshness"]


def test_evidence_bundle_extracts_upgrade_timeline_diagnostics() -> None:
    bundle = build_evidence_bundle(
        question="为什么升级没回执",
        plan={"goal": "检查升级回执", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "operation_timeline",
                "payload": {"operation_types": ["upgrade_now"], "limit": 5},
                "ok": True,
                "error": None,
                "data": {
                    "schema_version": "operation-timeline-v1",
                    "filters": {"operation_types": ["upgrade_now"]},
                    "timeline_count": 1,
                    "timelines": [
                        {
                            "identity": {"command_id": "in_123", "operation_id": "in_123"},
                            "operation": {
                                "operation_id": "in_123",
                                "command_id": "in_123",
                                "operation_type": "upgrade_now",
                                "status": "confirmed",
                            },
                            "receipt": {
                                "status": "not_observed",
                                "reason": "receipt_not_in_audit_or_operation_store",
                            },
                            "outcome": {
                                "status": "confirmed",
                                "ok": False,
                                "warnings": ["receipt_not_observed"],
                            },
                            "warnings": ["receipt_not_observed"],
                        }
                    ],
                    "warnings": [],
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["schema_version"] == DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION
    assert diagnostic["domain"] == "upgrade"
    assert diagnostic["status"] == "observed_operation_status"
    assert diagnostic["scope"]["operation_types"] == ["upgrade_now"]
    assert diagnostic["scope"]["operation_ids"] == ["in_123"]
    assert diagnostic["scope"]["statuses"] == ["confirmed"]
    assert diagnostic["source"]["view"] == "operation_timeline"
    assert diagnostic["observed_reason"] == "upgrade operation status is confirmed"
    assert diagnostic["answer_boundary"] == "operation_timeline_status_and_receipt_evidence_only"
    assert diagnostic["confidence"] == "partial"
    assert diagnostic["missing_data"][0]["kind"] == "receipt_not_observed"
    assert bundle.trace_payload()["diagnostic_domains"] == ["upgrade"]


def test_evidence_bundle_marks_upgrade_timeline_status_conflict() -> None:
    bundle = build_evidence_bundle(
        question="升级 in_123 到底成功还是失败",
        plan={"goal": "检查升级状态冲突", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "operation_timeline",
                "payload": {"operation_types": ["upgrade_now"], "operation_id": "in_123", "limit": 5},
                "ok": True,
                "error": None,
                "data": {
                    "schema_version": "operation-timeline-v1",
                    "filters": {"operation_types": ["upgrade_now"], "operation_id": "in_123"},
                    "timeline_count": 1,
                    "timelines": [
                        {
                            "identity": {"command_id": "in_123", "operation_id": "in_123"},
                            "operation": {
                                "operation_id": "in_123",
                                "command_id": "in_123",
                                "operation_type": "upgrade_now",
                                "status": "applied",
                                "current_version": "1.2.110",
                                "target_version": "1.2.111",
                            },
                            "receipt": {"status": "observed"},
                            "outcome": {"status": "failed", "ok": False},
                            "warnings": [],
                        }
                    ],
                    "warnings": [],
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "upgrade"
    assert diagnostic["status"] == "conflicting_evidence"
    assert diagnostic["confidence"] == "conflict"
    assert diagnostic["answer_boundary"] == "conflicting_operation_timeline_evidence_only"
    assert "operation_status=applied" in diagnostic["observed_reason"]
    assert "outcome_status=failed" in diagnostic["observed_reason"]


def test_evidence_bundle_extracts_upgrade_missing_version_diagnostics() -> None:
    bundle = build_evidence_bundle(
        question="升级为什么版本是空的",
        plan={"goal": "检查升级版本回执", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "inbound.upgrade",
                "payload": {"target_version": None},
                "ok": True,
                "error": None,
                "data": {
                    "operation_id": "in_456",
                    "operation_type": "upgrade_now",
                    "status": "previewed",
                    "payload": {"operation_type": "upgrade_now", "arguments": {"target_version": None}},
                    "preview": {"upgrade": {"status": "no_target_version"}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "upgrade"
    assert diagnostic["status"] == "observed_upgrade_operation"
    assert diagnostic["scope"]["operation_ids"] == ["in_456"]
    assert diagnostic["confidence"] == "partial"
    missing_kinds = {item["kind"] for item in diagnostic["missing_data"]}
    assert missing_kinds == {"current_version_missing", "target_version_missing"}


def test_coverage_marks_upgrade_missing_version_and_receipt_unrecoverable() -> None:
    plan = PlannerPlan(
        goal="检查升级版本和回执",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
                purpose="读取升级 operation 状态",
            ),
        ),
    )
    contract = build_task_contract(
        question="为什么升级完成后没有显示当前版本和目标版本，也没收到成功回执？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "rows": [
                        {
                            "command_id": "in_123",
                            "operation_id": "in_123",
                            "operation_type": "upgrade_now",
                            "operation_status": "applied",
                            "current_version": None,
                            "target_version": None,
                            "receipt_status": "not_observed",
                            "source": "operation_timeline",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["upgrade_operation_status"],
                    "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "unrecoverable_gap"
    assert coverage["next_action"] == "answer_with_missing_data"
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert set(gaps) == {"upgrade_current_version_missing", "upgrade_target_version_missing", "upgrade_receipt_missing"}
    assert all(item["recoverable"] is False for item in gaps.values())
    assert gaps["upgrade_current_version_missing"]["required_answer_key"] == "current_version"
    assert gaps["upgrade_target_version_missing"]["required_answer_key"] == "target_version"
    assert gaps["upgrade_receipt_missing"]["required_answer_key"] == "receipt_status"
    assert "current_version" in coverage["missing"]
    assert "target_version" in coverage["missing"]


def test_coverage_marks_upgrade_status_conflict_unrecoverable() -> None:
    plan = PlannerPlan(
        goal="检查升级状态冲突",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
                purpose="读取升级 operation 状态",
            ),
        ),
    )
    contract = build_task_contract(
        question="升级 in_123 到底成功还是失败？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "rows": [
                        {
                            "command_id": "in_123",
                            "operation_id": "in_123",
                            "operation_type": "upgrade_now",
                            "operation_status": "applied",
                            "outcome_status": "failed",
                            "current_version": "1.2.110",
                            "target_version": "1.2.111",
                            "receipt_status": "observed",
                            "source": "operation_timeline",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["upgrade_operation_status"],
                    "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["status"] == "conflicting_evidence"
    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "unrecoverable_gap"
    assert coverage["next_action"] == "answer_with_missing_data"
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert set(gaps) == {"upgrade_status_conflict"}
    assert gaps["upgrade_status_conflict"]["recoverable"] is False


def test_coverage_allows_operation_timeline_followup_before_timeline_is_queried() -> None:
    plan = PlannerPlan(
        goal="检查升级版本和回执",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="inbound.upgrade",
                arguments={"target_version": None},
                purpose="生成升级预览",
            ),
        ),
    )
    contract = build_task_contract(
        question="为什么升级 in_456 当前版本和目标版本是空的？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "inbound.upgrade",
                "payload": {"target_version": None},
                "ok": True,
                "error": None,
                "data": {
                    "operation_id": "in_456",
                    "operation_type": "upgrade_now",
                    "status": "previewed",
                    "payload": {"operation_type": "upgrade_now", "arguments": {"target_version": None}},
                    "preview": {"upgrade": {"status": "no_target_version"}},
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "recoverable_gap"
    assert coverage["next_action"] == "followup_tool"
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert gaps["upgrade_current_version_missing"]["recoverable"] is True
    assert gaps["upgrade_current_version_missing"]["suggested_tool"] == "operation_timeline"
    assert gaps["upgrade_target_version_missing"]["recoverable"] is True


def test_coverage_accepts_complete_upgrade_status_evidence() -> None:
    plan = PlannerPlan(
        goal="检查升级版本和回执",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
                purpose="读取升级 operation 状态",
            ),
        ),
    )
    contract = build_task_contract(
        question="检查升级 in_123 当前版本和目标版本",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "rows": [
                        {
                            "command_id": "in_123",
                            "operation_id": "in_123",
                            "operation_type": "upgrade_now",
                            "operation_status": "applied",
                            "current_version": "1.2.110",
                            "target_version": "1.2.111",
                            "receipt_status": "observed",
                            "source": "operation_timeline",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["upgrade_operation_status"],
                    "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "complete"
    assert coverage["missing"] == []
    assert coverage["gaps"] == []


def test_evidence_bundle_infers_candidate_diagnostics_from_analysis_rows() -> None:
    bundle = build_evidence_bundle(
        question="为什么 NVDA 没出现在候选里",
        plan={"goal": "解释 NVDA 候选诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select account, symbol, status, rule from candidate_filter_diagnostics"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].account", "rows[].symbol", "rows[].status", "rows[].rule"],
                },
                "data": {
                    "rows": [{"account": "lx", "symbol": "NVDA", "status": "rejected", "rule": "liquidity"}],
                    "row_count": 1,
                    "views_used": ["candidate_filter_diagnostics"],
                    "evidence": {"coverage": {"views": ["candidate_filter_diagnostics"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "candidate_filter"
    assert diagnostic["status"] == "observed_rejection"
    assert diagnostic["scope"]["accounts"] == ["lx"]
    assert diagnostic["scope"]["symbols"] == ["NVDA"]
    assert diagnostic["source"]["view"] == "candidate_filter_diagnostics"
    assert diagnostic["confidence"] == "direct"
    assert "liquidity" in diagnostic["observed_reason"]


def test_evidence_bundle_infers_runtime_skip_diagnostics_from_analysis_rows() -> None:
    bundle = build_evidence_bundle(
        question="今天为什么没推送",
        plan={"goal": "解释 runtime 推送诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select market, account, latest_status, skip_reason from runtime_tick_status"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].market", "rows[].account", "rows[].latest_status", "rows[].skip_reason"],
                },
                "data": {
                    "rows": [
                        {
                            "market": "us",
                            "account": "lx",
                            "latest_status": "scheduler_skip",
                            "skip_reason": "market_closed",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "runtime_tick"
    assert diagnostic["status"] == "observed_scheduler_skip"
    assert diagnostic["scope"]["accounts"] == ["lx"]
    assert diagnostic["confidence"] == "direct"
    assert diagnostic["observed_reason"] == "scheduler skipped because market_closed"
    assert diagnostic["answer_boundary"] == "observed_runtime_status_only"


def test_evidence_bundle_marks_analysis_diagnostic_missing_and_conflict() -> None:
    missing_bundle = build_evidence_bundle(
        question="为什么 NVDA 没出现在候选里",
        plan={"goal": "解释 NVDA 候选诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select count(*) as row_count from candidate_filter_diagnostics"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].row_count"],
                },
                "data": {
                    "rows": [{"row_count": 0}],
                    "row_count": 1,
                    "views_used": ["candidate_filter_diagnostics"],
                    "evidence": {"coverage": {"views": ["candidate_filter_diagnostics"]}},
                },
            }
        ],
    )
    missing = missing_bundle.public_payload()["diagnostics"][0]
    assert missing["status"] == "no_matching_rows"
    assert missing["confidence"] == "missing"

    conflict_bundle = build_evidence_bundle(
        question="今天为什么没推送",
        plan={"goal": "解释 runtime 推送诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select account, diagnostic_status, summary from runtime_tick_status"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].account", "rows[].diagnostic_status", "rows[].summary"],
                },
                "data": {
                    "rows": [
                        {
                            "account": "lx",
                            "diagnostic_status": "conflicting_evidence",
                            "summary": "runtime status and notification audit disagree",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"]}},
                },
            }
        ],
    )
    conflict = conflict_bundle.public_payload()["diagnostics"][0]
    assert conflict["domain"] == "runtime_tick"
    assert conflict["status"] == "conflicting_evidence"
    assert conflict["confidence"] == "conflict"
    assert conflict["answer_boundary"] == "conflicting_runtime_evidence_only"


def test_answer_verifier_rejects_unsupported_quote_upstream_root_cause() -> None:
    bundle = build_evidence_bundle(
        question="为什么 FUTU 指派正股没有浮盈亏",
        plan={"goal": "解释 FUTU 指派正股没有浮盈亏", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "option_positions_read",
                "payload": {"action": "assigned-stock", "account": "sy", "status": "open", "refresh_quotes": True},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "option_positions_read.assigned_stock_output.v1",
                    "canonical_renderer": "assigned_stock_lifecycle",
                    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
                    "guard_profile": "position_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "freshness_fields": ["rows[].quote_status", "quote_refresh.status"],
                    "missing_data_fields": ["quote_refresh.missing_symbols"],
                    "fact_fields": [
                        "rows[].account",
                        "rows[].symbol",
                        "rows[].quote_status",
                    ],
                },
                "data": {
                    "action": "assigned-stock",
                    "filters": {"account": "sy", "status": "open", "refresh_quotes": True},
                    "rows": [
                        {
                            "account": "sy",
                            "symbol": "FUTU",
                            "status": "open",
                            "quote_status": "missing_quote",
                        }
                    ],
                    "row_count": 1,
                    "quote_refresh": {
                        "status": "missing_quote",
                        "quote_source": "opend_realtime",
                        "missing_symbols": ["FUTU"],
                    },
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "FUTU 指派正股没有当前浮盈亏，因为没有可用报价；不能据此判断上游连接原因。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()

    bad = verify_response_against_evidence(
        "FUTU 指派正股没有浮盈亏，原因是 OpenD 断开导致无法获取报价。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_quote_upstream_root_cause_claim" for item in bad.violations)


def test_evidence_bundle_records_cross_tool_reconciliation_views() -> None:
    bundle = build_evidence_bundle(
        question="为什么 6 月收益和指派正股盈亏对不上",
        plan={"goal": "解释 6 月收益和指派正股盈亏差异", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "monthly_income_report",
                "payload": {"account": "lx", "month": "2026-06", "include_rows": True},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "monthly_income_report.detail_output.v1",
                    "canonical_renderer": "monthly_income",
                    "source_label": "OM 本地账本",
                    "guard_profile": "income_rows",
                    "primary_rows": "cashflow_rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "cashflow_rows[].account",
                        "cashflow_rows[].symbol",
                        "cashflow_rows[].currency",
                        "cashflow_rows[].net_cashflow_gross",
                    ],
                },
                "data": {
                    "filters": {"account": "lx", "month": "2026-06"},
                    "cashflow_rows": [
                        {
                            "month": "2026-06",
                            "account": "lx",
                            "symbol": "FUTU",
                            "currency": "USD",
                            "net_cashflow_gross": 520.0,
                        }
                    ],
                    "row_count": 1,
                },
            },
            {
                "index": 2,
                "tool_name": "option_positions_read",
                "payload": {"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "option_positions_read.assigned_stock_output.v1",
                    "canonical_renderer": "assigned_stock_lifecycle",
                    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
                    "guard_profile": "position_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "rows[].account",
                        "rows[].symbol",
                        "rows[].currency",
                        "rows[].assigned_stock_unrealized_pnl",
                        "rows[].assignment_lifecycle_pnl",
                    ],
                },
                "data": {
                    "action": "assigned-stock",
                    "filters": {"account": "lx", "status": "open", "refresh_quotes": True},
                    "rows": [
                        {
                            "account": "lx",
                            "symbol": "FUTU",
                            "currency": "USD",
                            "assigned_stock_unrealized_pnl": -2000.0,
                            "assignment_lifecycle_pnl": -1480.0,
                            "quote_status": "fresh",
                        }
                    ],
                    "row_count": 1,
                },
            },
        ],
    )

    payload = bundle.public_payload()
    assert payload["scope"]["accounts"] == ["lx"]
    assert payload["scope"]["symbols"] == ["FUTU"]
    calculation_kinds = {item["kind"] for item in payload["calculations"]}
    assert "accounting_view_summary" in calculation_kinds
    assert "cross_tool_reconciliation" in calculation_kinds
    reconciliation = next(item for item in payload["calculations"] if item["kind"] == "cross_tool_reconciliation")
    assert reconciliation["status"] == "different_accounting_views"
    assert "cashflow" in reconciliation["views"]
    assert "assigned_stock_unrealized_pnl" in reconciliation["views"]
    view_summary = next(item for item in payload["calculations"] if item["kind"] == "accounting_view_summary")
    view_names = {item["view"] for item in view_summary["views"]}
    assert {"cashflow", "assigned_stock_unrealized_pnl", "assignment_lifecycle_pnl"} <= view_names


def test_task_contract_and_coverage_detect_missing_account_comparison_scope() -> None:
    plan = PlannerPlan(
        goal="对比 lx 和 sy 的账户收益",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": "select month, account, net_income_cny from account_monthly_performance where account = 'lx'",
                    "limit": 20,
                },
                purpose="读取 lx 账户收益",
            ),
        ),
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    assert contract.public_payload()["schema_version"] == TASK_CONTRACT_SCHEMA_VERSION
    assert contract.intent_families == ("account_comparison",)
    assert contract.scope["requested_accounts"] == ["lx", "sy"]
    assert "comparison_winner" in contract.required_answer
    assert "rate_difference" in contract.optional_answer

    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM SQLite analysis view",
                    "guard_profile": "analysis_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "columns": ["month", "account", "net_income_cny"],
                    "rows": [{"month": "2026-06", "account": "lx", "net_income_cny": 2414.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        }
                    },
                },
            }
        ],
    )
    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["schema_version"] == COVERAGE_RESULT_SCHEMA_VERSION
    assert coverage["status"] == "recoverable_gap"
    assert coverage["gaps"][0]["kind"] == "analysis_missing_account_coverage"
    assert coverage["gaps"][0]["missing_accounts"] == ["sy"]


def test_coverage_rejects_account_comparison_without_same_period_metric() -> None:
    plan = PlannerPlan(
        goal="对比 lx 和 sy 的账户收益",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": (
                        "select month, account, net_income_cny "
                        "from account_monthly_performance where account in ('lx','sy')"
                    ),
                    "limit": 20,
                },
                purpose="读取 lx/sy 账户收益",
            ),
        ),
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "columns": ["month", "account", "net_income_cny"],
                    "rows": [
                        {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
                        {"month": "2026-06", "account": "sy", "net_income_cny": 11138.0},
                    ],
                    "row_count": 2,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-05", "2026-06"],
                            "accounts": ["lx", "sy"],
                            "symbols": [],
                        }
                    },
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "recoverable_gap"
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert "analysis_comparison_metric_missing" in gaps
    assert "comparison_winner" in coverage["missing"]
    assert "amount_difference" in coverage["missing"]


def test_coverage_accepts_pivot_account_comparison_same_period_metric() -> None:
    plan = PlannerPlan(
        goal="对比 lx 和 sy 的账户收益",
        response_mode="synthesis",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": (
                        "select month, "
                        "sum(case when account = 'lx' then net_income_cny else 0 end) as lx_income_cny, "
                        "sum(case when account = 'sy' then net_income_cny else 0 end) as sy_income_cny "
                        "from account_monthly_performance where account in ('lx','sy') group by month"
                    ),
                    "limit": 20,
                },
                purpose="读取 lx/sy 账户收益",
            ),
        ),
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": plan.steps[0].arguments,
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "columns": ["month", "lx_income_cny", "sy_income_cny"],
                    "rows": [{"month": "2026-06", "lx_income_cny": 2414.0, "sy_income_cny": 11138.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx", "sy"],
                            "symbols": [],
                        }
                    },
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "complete"
    assert "comparison_winner" in coverage["satisfied"]
    assert "amount_difference" in coverage["satisfied"]


def test_answer_shape_verifier_requires_account_comparison_difference() -> None:
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan={
            "goal": "对比 lx 和 sy 的账户收益",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "analysis_query",
                    "arguments": {
                        "sql": "select account, net_income_cny from account_monthly_performance where account in ('lx','sy')"
                    },
                    "purpose": "读取账户收益",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    coverage = {
        "schema_version": COVERAGE_RESULT_SCHEMA_VERSION,
        "status": "complete",
        "satisfied": list(contract.required_answer),
        "missing": [],
        "gaps": [],
        "next_action": "final_answer",
    }

    bad = verify_response_shape("2026-06 sy 高于 lx。", task_contract=contract.public_payload(), coverage=coverage)
    assert any(item["required_answer_key"] == "amount_difference" for item in bad.violations)

    good = verify_response_shape(
        "2026-06 sy 高于 lx，差额 CNY 8,724。", task_contract=contract.public_payload(), coverage=coverage
    )
    assert good.violations == ()


def test_task_contract_treats_source_focused_difference_as_breakdown() -> None:
    contract = build_task_contract(
        question="分析 lx 和 sy 收益差异主要来自哪里",
        plan={
            "goal": "分析 lx 和 sy 收益差异主要来自哪里",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "analysis_query",
                    "arguments": {
                        "sql": "select month, account, net_income_cny from account_monthly_performance where account in ('lx','sy')"
                    },
                    "purpose": "先对比账户级收益",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.intent_families == ("breakdown",)
    assert "main_drivers" in contract.required_answer
    assert "amount_difference" not in contract.required_answer


def test_task_contract_does_not_treat_strategy_config_difference_as_income_comparison() -> None:
    contract = build_task_contract(
        question="FUTU 在 lx 和 sy 的策略配置有什么差异？",
        plan={
            "goal": "比较 FUTU 策略配置",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "analysis_query",
                    "arguments": {
                        "sql": (
                            "select symbol, account, strategy_family, min_delta, min_yield "
                            "from strategy_config_by_symbol_account where symbol = 'FUTU'"
                        )
                    },
                    "purpose": "读取策略配置差异",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert "account_comparison" not in contract.intent_families
    assert contract.intent_families == ("general_analysis",)
    assert contract.required_answer == ("summary", "source_and_policy")


def test_contract_verifier_rejects_unsupported_quantity_symbol_date_and_status() -> None:
    bundle = build_evidence_bundle(
        question="查看 lx 指派正股持仓盈亏",
        plan={"goal": "查看 lx 指派正股持仓盈亏", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "option_positions_read",
                "payload": {"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "option_positions_read.assigned_stock_output.v1",
                    "canonical_renderer": "assigned_stock_lifecycle",
                    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
                    "guard_profile": "position_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "rows[].account",
                        "rows[].symbol",
                        "rows[].status",
                        "rows[].expiration_ymd",
                        "rows[].currency",
                        "rows[].shares_remaining",
                        "rows[].spot",
                        "rows[].quote_status",
                    ],
                },
                "data": {
                    "action": "assigned-stock",
                    "filters": {"account": "lx", "status": "open", "refresh_quotes": True},
                    "rows": [
                        {
                            "account": "lx",
                            "symbol": "FUTU",
                            "status": "open",
                            "expiration_ymd": "2026-06-18",
                            "currency": "USD",
                            "shares_remaining": 100,
                            "spot": 98,
                            "quote_status": "fresh",
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "lx FUTU open 状态，剩余 100 股，行情 quote=fresh。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()
    bad = verify_response_against_evidence(
        "lx NVDA closed 状态，剩余 200 股，2026-07 有 2 条记录。",
        evidence_bundle=bundle,
    )
    violation_types = {item["type"] for item in bad.violations}
    assert {
        "unsupported_contract_quantity",
        "unsupported_contract_symbol",
        "unsupported_contract_date",
        "unsupported_contract_status",
    } <= violation_types


def test_agent_loop_tool_result_contains_evidence_bundle_and_session(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "action": "assigned-stock",
                "filters": {"account": "lx", "status": "open", "refresh_quotes": True},
                "rows": [
                    {
                        "stock_lot_id": "assigned-stock-assign_1",
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "stock_cost_per_share": 100,
                        "remaining_stock_cost_basis": 10000,
                        "spot": 98,
                        "quote_status": "fresh",
                        "assigned_stock_unrealized_pnl": -200,
                        "assigned_stock_realized_pnl": 0,
                        "assignment_lifecycle_pnl": 50,
                    }
                ],
                "row_count": 1,
                "quote_refresh": {"status": "ok", "quote_source": "opend_realtime"},
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
                response_mode="synthesis",
                required_capabilities=("assigned_stock_positions", "read_only"),
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="option_positions_read",
                        arguments={"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True},
                        purpose="读取指派正股持仓盈亏",
                    ),
                ),
            ),
            trace={
                "enabled": True,
                "attempted": True,
                "reason": "accepted",
                "provider": "openai",
                "base_url": "",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 20,
                "max_output_tokens": 512,
                "schema_version": TOOL_PLAN_SCHEMA_VERSION,
            },
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        _observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        return LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股剩余 100 股，成本 USD 100/股，spot USD 98，正股浮盈亏 USD -200，生命周期PnL USD 50。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="查看 lx 指派正股持仓盈亏",
            sender_id="local",
            message_id="msg_agent_session_snapshot",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert calls == [
        (
            "option_positions_read",
            {"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True, "config_key": "us"},
        )
    ]
    tool_plan_data = out["data"]["action"]["result"]["data"]
    evidence = tool_plan_data["evidence_bundle"]
    assert evidence["schema_version"] == EVIDENCE_BUNDLE_SCHEMA_VERSION
    coverage_event = next(item for item in tool_plan_data["tool_events"] if item.get("phase") == "coverage_verify")
    assert coverage_event["hook_results"][0]["hook"] == "coverage"
    assert coverage_event["hook_results"][0]["status"] == "pass"
    assert evidence["scope"]["accounts"] == ["lx"]
    assert evidence["scope"]["symbols"] == ["NVDA"]
    assert any(
        item["path"] == "rows[].assigned_stock_unrealized_pnl" and item["value"] == -200
        for item in evidence["facts"]
    )
    session = tool_plan_data["agent_session"]
    assert session["schema_version"] == "om-agent-session-v1"
    assert session["task_state"] == "done"
    assert session["goal"] == "查看 lx 指派正股持仓盈亏"
    assert session["task_contract"]["schema_version"] == TASK_CONTRACT_SCHEMA_VERSION
    assert session["task_contract"]["intent_families"] == ["assigned_stock_pnl"]
    assert session["coverage"]["schema_version"] == COVERAGE_RESULT_SCHEMA_VERSION
    assert session["coverage"]["status"] == "complete"
    assert session["evidence_bundle"]["fact_count"] == len(evidence["facts"])
    assert session["evidence_bundle"]["diagnostic_count"] == 0
    assert session["evidence_bundle"]["diagnostic_domains"] == []
    assert session["permission_state"] == {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [],
        "apply_allowed": False,
    }
    assert session["tool_transcript"][0]["tool_name"] == "option_positions_read"
    assert session["tool_transcript"][0]["authorization_reason"] == "pure_read_whitelist"
    assert session["tool_transcript"][0]["action_policy"]["schema_version"] == ACTION_POLICY_SCHEMA_VERSION
    assert session["tool_transcript"][0]["action_policy"]["decision"] == "allow_read"
    assert session["tool_transcript"][0]["action_safety"]["schema_version"] == ACTION_SAFETY_SCHEMA_VERSION
    assert session["tool_transcript"][0]["action_safety"]["status"] == "allow"
    assert session["tool_transcript"][0]["precheck"]["schema_version"] == TOOL_CHECK_SCHEMA_VERSION
    assert session["tool_transcript"][0]["precheck"]["status"] == "pass"
    assert session["tool_transcript"][0]["postcheck"]["schema_version"] == TOOL_CHECK_SCHEMA_VERSION
    assert session["tool_transcript"][0]["postcheck"]["status"] == "pass"
    assert session["tool_transcript"][0]["hook_results"][0]["schema_version"] == HOOK_RESULT_SCHEMA_VERSION
    assert any(item["hook"] == "action_policy" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    assert any(item["hook"] == "result_status" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    assert session["tool_transcript"][0]["evidence_summary"]["source_label"] == "OM 本地 SQLite assigned_stock_events + trade_events"
    assert session["tool_transcript"][0]["evidence_summary"]["primary_rows"] == "rows"
    assert session["tool_transcript"][0]["evidence_summary"]["row_count"] == 1

    audit_db = tmp_path / "inbound.sqlite3"
    persisted = AgentSessionStore(audit_db).list_recent(command_id=out["data"]["command_id"])
    assert len(persisted) == 1
    assert persisted[0]["session_id"] == session["session_id"]
    assert persisted[0]["command_id"] == out["data"]["command_id"]
    assert persisted[0]["goal"] == "查看 lx 指派正股持仓盈亏"
    assert persisted[0]["task_state"] == "done"
    assert persisted[0]["fact_count"] == len(evidence["facts"])
    assert persisted[0]["tool_call_count"] == 1

    trace = collect_assistant_trace(audit_db=str(audit_db), command_id=out["data"]["command_id"])
    assert trace["schema_version"] == "om-assistant-trace-v1"
    assert trace["trace_count"] == 1
    trace_entry = trace["traces"][0]
    assert trace_entry["identity"]["session_id"] == session["session_id"]
    assert trace_entry["task"]["goal"] == "查看 lx 指派正股持仓盈亏"
    assert trace_entry["plan"]["revision_count"] == 1
    assert trace_entry["tools"][0]["tool_name"] == "option_positions_read"
    assert trace_entry["tools"][0]["action_policy"]["decision"] == "allow_read"
    assert trace_entry["tools"][0]["action_safety"]["code"] == "ok"
    assert trace_entry["tools"][0]["precheck"]["status"] == "pass"
    assert trace_entry["tools"][0]["postcheck"]["status"] == "pass"
    assert any(item["hook"] == "action_safety" and item["status"] == "pass" for item in trace_entry["tools"][0]["hook_results"])
    assert trace_entry["tools"][0]["evidence_summary"]["row_count"] == 1
    assert trace_entry["tools"][0]["evidence_summary"]["missing_data_count"] == 0
    assert trace_entry["evidence"]["fact_count"] == len(evidence["facts"])
    assert trace_entry["evidence"]["diagnostic_count"] == 0
    assert trace_entry["evidence"]["diagnostic_domains"] == []
    assert trace_entry["answer"]["response_status"] == "synthesized"
    assert any(item["hook"] == "final_response" and item["status"] == "pass" for item in trace_entry["answer"]["hook_results"])
    assert any(item["hook"] == "answer_guard" and item["status"] == "pass" for item in trace_entry["answer"]["hook_results"])

    tool_trace = run_tool("assistant_trace", {"audit_db": str(audit_db), "command_id": out["data"]["command_id"]})
    assert tool_trace["ok"] is True
    assert tool_trace["data"]["trace_count"] == 1
    assert tool_trace["data"]["traces"][0]["identity"]["command_id"] == out["data"]["command_id"]
    trace_text = tool_trace["data"]["response_text"]
    assert "任务：查看 lx 指派正股持仓盈亏" in trace_text
    assert "工具：读取指派正股持仓（ok，1 行）" in trace_text
    assert "证据：facts=" in trace_text
    assert "缺口：无" in trace_text
    assert "校验：pre/action_policy=pass" in trace_text
    assert "pre/action_safety=pass" in trace_text
    assert "最终：pass（LLM 回答通过证据校验）" in trace_text
    assert "option_positions_read" not in trace_text
    assert "stock_lot_id" not in trace_text
    assert "session=as_" not in trace_text


def test_format_assistant_trace_compact_redacts_internal_details() -> None:
    text = format_assistant_trace(
        [
            {
                "identity": {"command_id": "in_trace_1", "session_id": "as_internal"},
                "task": {"updated_at": "2026-06-15T10:00:00Z", "state": "done", "goal": "解释升级为什么没有回执"},
                "tools": [
                    {
                        "tool_name": "analysis_query",
                        "payload": {"sql": "select * from command_log", "stock_lot_id": "assigned-stock-secret"},
                        "authorized": True,
                        "ok": True,
                        "evidence_summary": {
                            "canonical_renderer": "analysis_result",
                            "source_label": "OM read-only analysis workspace",
                            "row_count": 1,
                        },
                        "hook_results": [
                            {"stage": "pre_tool", "hook": "action_safety", "status": "pass", "code": "ok"},
                            {"stage": "post_tool", "hook": "missing_data", "status": "warning", "code": "missing_data"},
                        ],
                    }
                ],
                "evidence": {
                    "fact_count": 3,
                    "diagnostic_count": 1,
                    "missing_data_count": 1,
                    "conflict_count": 0,
                    "sources": ["OM read-only analysis workspace"],
                    "diagnostic_domains": ["upgrade"],
                },
                "answer": {
                    "response_status": "rendered",
                    "synthesis_reason": "task_contract_fallback",
                    "fallback": "task_contract",
                    "answer_guard": {"status": "failed_then_fallback"},
                    "hook_results": [
                        {"stage": "answer", "hook": "answer_guard", "status": "fail", "code": "failed_then_fallback"}
                    ],
                },
            }
        ],
        filters={"command_id": "in_trace_1", "limit": 10},
        warnings=[],
    )

    assert "任务：解释升级为什么没有回执" in text
    assert "工具：读取分析证据（ok，1 行）" in text
    assert "缺口：missing=1，诊断域=upgrade" in text
    assert "校验：post/missing_data=warning" in text
    assert "answer/answer_guard=fail/failed_then_fallback" in text
    assert "最终：fallback（证据校验失败后使用保底回答）" in text
    assert "analysis_query" not in text
    assert "select *" not in text
    assert "stock_lot_id" not in text
    assert "assigned-stock-secret" not in text
    assert "session=as_internal" not in text


def test_format_assistant_trace_shows_key_routes() -> None:
    base_trace = {
        "identity": {"command_id": "in_route"},
        "task": {"updated_at": "2026-06-15T10:00:00Z", "state": "done", "goal": "route check"},
        "tools": [],
        "evidence": {"fact_count": 0, "diagnostic_count": 0, "missing_data_count": 0, "conflict_count": 0},
    }
    cases = [
        (
            {"response_status": "needs_clarification", "response_reason": "missing account"},
            "最终：ask（missing account）",
        ),
        (
            {"response_status": "preview", "response_reason": "pending operator confirmation"},
            "最终：preview（pending operator confirmation）",
        ),
        (
            {
                "response_status": "synthesized",
                "synthesis_reason": "agent_composed_response",
                "answer_guard": {"status": "failed_then_rewritten"},
            },
            "最终：rewrite->pass（重写后通过证据校验）",
        ),
        (
            {"response_status": "denied", "response_reason": "planned tool call failed pre-tool safety checks"},
            "最终：denied（planned tool call failed pre-tool safety checks）",
        ),
    ]

    for answer, expected in cases:
        text = format_assistant_trace(
            [{**base_trace, "answer": answer}],
            filters={"command_id": "in_route", "limit": 10},
            warnings=[],
        )
        assert expected in text


def test_agent_loop_replans_read_only_followup_for_recoverable_quote_gap(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    planner_followup_contexts: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        if payload.get("refresh_quotes") is True:
            return build_response(
                tool_name=tool_name,
                ok=True,
                data={
                    "action": "assigned-stock",
                    "filters": {"account": "lx", "status": "open", "refresh_quotes": True},
                    "rows": [
                        {
                            "stock_lot_id": "assigned-stock-assign_1",
                            "account": "lx",
                            "symbol": "NVDA",
                            "currency": "USD",
                            "status": "open",
                            "shares_remaining": 100,
                            "stock_cost_per_share": 100,
                            "remaining_stock_cost_basis": 10000,
                            "spot": 98,
                            "quote_status": "fresh",
                            "assigned_stock_unrealized_pnl": -200,
                            "assigned_stock_realized_pnl": 0,
                            "assignment_lifecycle_pnl": 50,
                        }
                    ],
                    "row_count": 1,
                    "quote_refresh": {"status": "ok", "quote_source": "opend_realtime"},
                },
            )
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "action": "assigned-stock",
                "filters": {"account": "lx", "status": "open", "refresh_quotes": False},
                "rows": [
                    {
                        "stock_lot_id": "assigned-stock-assign_1",
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "spot": None,
                        "quote_status": "missing_quote",
                        "assigned_stock_unrealized_pnl": None,
                        "assigned_stock_realized_pnl": 0,
                        "assignment_lifecycle_pnl": None,
                    }
                ],
                "row_count": 1,
                "quote_refresh": {
                    "status": "missing_quote",
                    "quote_source": "opend_realtime",
                    "missing_symbols": ["NVDA"],
                },
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        followup = conversation_context.get("agent_loop_followup") if isinstance(conversation_context, dict) else None
        if isinstance(followup, dict):
            planner_followup_contexts.append(followup)
            return LlmPlannerResult(
                plan=PlannerPlan(
                    goal="查看 lx 指派正股持仓盈亏",
                    response_mode="synthesis",
                    required_capabilities=("assigned_stock_positions", "read_only"),
                    steps=(
                        PlannerPlanStep(
                            id="step_2",
                            tool_name="option_positions_read",
                            arguments={
                                "action": "assigned-stock",
                                "account": "lx",
                                "status": "open",
                                "refresh_quotes": True,
                            },
                            purpose="补查指派正股实时行情",
                        ),
                    ),
                ),
                trace={
                    "enabled": True,
                    "attempted": True,
                    "reason": "accepted",
                    "provider": "openai",
                    "base_url": "",
                    "model": "gpt-5.2",
                    "api_key_env": "OM_LLM_API_KEY",
                    "confidence_min": 0.75,
                    "timeout_seconds": 20,
                    "max_output_tokens": 512,
                    "schema_version": TOOL_PLAN_SCHEMA_VERSION,
                },
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
                response_mode="synthesis",
                required_capabilities=("assigned_stock_positions", "read_only"),
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="option_positions_read",
                        arguments={"action": "assigned-stock", "account": "lx", "status": "open"},
                        purpose="读取指派正股持仓",
                    ),
                ),
            ),
            trace={
                "enabled": True,
                "attempted": True,
                "reason": "accepted",
                "provider": "openai",
                "base_url": "",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 20,
                "max_output_tokens": 512,
                "schema_version": TOOL_PLAN_SCHEMA_VERSION,
            },
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        answer_evidence = observations[-1]
        assert answer_evidence["tool_name"] == "assistant.answer_evidence"
        assert "spot USD 98" in answer_evidence["data"]["fallback_renderer_text"]
        return LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股剩余 100 股，成本 USD 100/股，spot USD 98，正股浮盈亏 USD -200，生命周期PnL USD 50。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="查看 lx 指派正股持仓盈亏",
            sender_id="local",
            message_id="msg_agent_replan_quote_gap",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert calls == [
        ("option_positions_read", {"action": "assigned-stock", "account": "lx", "status": "open", "config_key": "us"}),
        (
            "option_positions_read",
            {"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True, "config_key": "us"},
        ),
    ]
    assert planner_followup_contexts
    assert planner_followup_contexts[0]["evidence_gaps"][0]["kind"] == "recoverable_missing_quote"
    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert len(tool_plan_data["plan_revisions"]) == 2
    assert tool_plan_data["agent_session"]["plan_revisions"][1]["reason"] == "follow-up evidence-gap plan"
    assert tool_plan_data["tool_calls_used"] == 2
    assert tool_plan_data["evidence_gaps"] == []
    assert "spot USD 98" in out["data"]["response_text"]


def test_agent_loop_does_not_replan_unrecoverable_upgrade_gap(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    planner_followup_contexts: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "rows": [
                    {
                        "command_id": "in_123",
                        "operation_id": "in_123",
                        "operation_type": "upgrade_now",
                        "operation_status": "applied",
                        "current_version": None,
                        "target_version": None,
                        "receipt_status": "not_observed",
                        "source": "operation_timeline",
                    }
                ],
                "row_count": 1,
                "views_used": ["upgrade_operation_status"],
                "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        followup = conversation_context.get("agent_loop_followup") if isinstance(conversation_context, dict) else None
        if isinstance(followup, dict):
            planner_followup_contexts.append(followup)
            return LlmPlannerResult(
                plan=PlannerPlan(
                    goal="补查升级状态",
                    response_mode="synthesis",
                    required_capabilities=("operation_timeline", "read_only"),
                    steps=(
                        PlannerPlanStep(
                            id="step_2",
                            tool_name="operation_timeline",
                            arguments={"operation_types": ["upgrade_now"], "operation_id": "in_123", "limit": 5},
                            purpose="补查升级 operation timeline",
                        ),
                    ),
                ),
                trace={"schema_version": TOOL_PLAN_SCHEMA_VERSION, "attempted": True, "reason": "followup"},
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="检查升级版本和回执",
                response_mode="synthesis",
                required_capabilities=("analysis_query", "read_only"),
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="analysis_query",
                        arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
                        purpose="读取升级 operation 状态",
                    ),
                ),
            ),
            trace={"schema_version": TOOL_PLAN_SCHEMA_VERSION, "attempted": True, "reason": "initial"},
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        _observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        return LlmSynthesisResult(
            response_text="升级已完成。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="为什么升级完成后没有显示当前版本和目标版本，也没收到成功回执？command_id in_123",
            sender_id="local",
            message_id="msg_agent_upgrade_unrecoverable_gap",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert calls == [
        (
            "analysis_query",
            {
                "sql": "select * from upgrade_operation_status where command_id = 'in_123'",
                "config_key": "us",
            },
        )
    ]
    assert planner_followup_contexts == []
    tool_plan_data = out["data"]["action"]["result"]["data"]
    coverage = tool_plan_data["coverage"]
    assert coverage["status"] == "unrecoverable_gap"
    assert coverage["next_action"] == "answer_with_missing_data"
    assert {gap["kind"] for gap in coverage["gaps"]} == {
        "upgrade_current_version_missing",
        "upgrade_target_version_missing",
        "upgrade_receipt_missing",
    }
    assert tool_plan_data["followup_decisions"] == []
    assert tool_plan_data["tool_calls_used"] == 1
    assert "缺少当前版本" in out["data"]["response_text"]
    assert "缺少目标版本" in out["data"]["response_text"]
    assert "缺少最终回执证据" in out["data"]["response_text"]


def test_agent_loop_contract_verifier_rejects_unsupported_currency_amount(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    synth_calls = 0

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "summary": [
                    {
                        "month": "2026-06",
                        "account": "lx",
                        "currency": "HKD",
                        "net_cashflow_gross": 1200.0,
                    }
                ],
                "return_summary": [
                    {
                        "month": "2026-06",
                        "account": "lx",
                        "net_income_cny": 1104.0,
                        "net_income_by_ccy": {"HKD": 1200.0},
                        "cash_secured_cny": 10000.0,
                        "net_return_rate": 0.1104,
                    }
                ],
                "cashflow_rows": [
                    {
                        "month": "2026-06",
                        "account": "lx",
                        "symbol": "0700.HK",
                        "trade_action": "sell_open",
                        "currency": "HKD",
                        "contracts": 1,
                        "net_cashflow_gross": 1200.0,
                    }
                ],
                "row_count": 1,
                "premium_row_count": 1,
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="分析 lx 2026-06 的净现金流明细",
                response_mode="synthesis",
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="monthly_income_report",
                        arguments={"account": "lx", "month": "2026-06", "include_rows": True},
                        purpose="读取收益明细",
                    ),
                ),
            ),
            trace={
                "enabled": True,
                "attempted": True,
                "reason": "accepted",
                "provider": "openai",
                "base_url": "",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 20,
                "max_output_tokens": 512,
                "schema_version": TOOL_PLAN_SCHEMA_VERSION,
            },
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        _observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        nonlocal synth_calls
        synth_calls += 1
        return LlmSynthesisResult(
            response_text="lx 2026-06 净现金流为 HKD 9,999。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="分析 lx 2026-06 的净现金流明细",
            sender_id="local",
            message_id="msg_agent_contract_verifier_amount",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert calls == [
        (
            "monthly_income_report",
            {"account": "lx", "config_key": "us", "include_rows": True, "month": "2026-06"},
        )
    ]
    assert synth_calls == 2
    text = out["data"]["response_text"]
    assert "HKD 9,999" not in text
    assert "HKD 1,200" in text
    synthesis = out["data"]["action"]["result"]["data"]["synthesis"]
    assert synthesis["reason"] == "agent_renderer_fallback"
    assert synthesis["answer_guard"]["status"] == "failed_then_fallback"
    violations = synthesis["answer_guard"]["violations"]
    assert any(item["type"] == "unsupported_contract_currency_amount" for item in violations)


def test_agent_loop_answer_shape_fallback_preserves_account_comparison(tmp_path: Path) -> None:
    synth_calls = 0

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "columns": ["month", "account", "net_income_cny", "net_return_rate"],
                "rows": [
                    {"month": "2026-06", "account": "lx", "net_income_cny": 2414.0, "net_return_rate": 0.0082},
                    {"month": "2026-06", "account": "sy", "net_income_cny": 11138.0, "net_return_rate": 0.0235},
                ],
                "row_count": 2,
                "evidence": {
                    "coverage": {
                        "views": ["account_monthly_performance"],
                        "months": ["2026-06"],
                        "accounts": ["lx", "sy"],
                        "symbols": [],
                    }
                },
                "fallback_text": "分析查询结果：2 行",
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="对比 lx 和 sy 的账户收益",
                response_mode="synthesis",
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="analysis_query",
                        arguments={
                            "sql": (
                                "select month, account, net_income_cny, net_return_rate "
                                "from account_monthly_performance where account in ('lx','sy')"
                            ),
                            "limit": 20,
                        },
                        purpose="读取 lx/sy 账户收益",
                    ),
                ),
            ),
            trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        _observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        nonlocal synth_calls
        synth_calls += 1
        return LlmSynthesisResult(
            response_text="2026-06 sy 高于 lx。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="对比 lx 和 sy 的账户收益，有什么不同？",
            sender_id="local",
            message_id="msg_agent_shape_compare_fallback",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert synth_calls == 2
    text = out["data"]["response_text"]
    assert "sy 更高" in text
    assert "差额 CNY 8,724" in text
    tool_plan_data = out["data"]["action"]["result"]["data"]
    synthesis = tool_plan_data["synthesis"]
    assert synthesis["reason"] == "task_contract_fallback"
    assert synthesis["answer_guard"]["status"] == "failed_then_fallback"
    assert any(
        item["type"] == "answer_shape_missing_required_key"
        and item["required_answer_key"] == "amount_difference"
        for item in synthesis["answer_guard"]["violations"]
    )
    assert tool_plan_data["final_response"]["status"] == "rendered"


def test_assistant_trace_does_not_create_agent_session_schema_on_read(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    with sqlite3.connect(audit_db) as conn:
        conn.execute("CREATE TABLE inbound_command_audit (id INTEGER PRIMARY KEY)")

    trace = collect_assistant_trace(audit_db=str(audit_db), limit=5)

    assert trace["trace_count"] == 0
    assert "agent_session_store_missing" in trace["warnings"]
    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_sessions'"
        ).fetchone()
    assert row is None


def test_message_less_local_agent_sessions_do_not_overwrite_each_other(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "action": "assigned-stock",
                "filters": {"account": "lx", "status": "open", "refresh_quotes": True},
                "rows": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "spot": 98,
                        "quote_status": "fresh",
                        "assigned_stock_unrealized_pnl": -200,
                    }
                ],
                "row_count": 1,
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
                response_mode="synthesis",
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="option_positions_read",
                        arguments={"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True},
                        purpose="读取指派正股持仓盈亏",
                    ),
                ),
            ),
            trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        )

    settings = AssistantSettings(
        mode="agent_loop",
        llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
    )
    first = handle_assistant_message(
        AssistantRequest(text="查看 lx 指派正股持仓盈亏", sender_id="local", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute,
        settings=settings,
        plan_tools_fn=_plan,
        synthesize_response_fn=lambda *_args: LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98，正股浮盈亏 USD -200；缺少成本和生命周期PnL证据。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        ),
    )
    second = handle_assistant_message(
        AssistantRequest(text="查看 lx 指派正股持仓盈亏", sender_id="local", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute,
        settings=settings,
        plan_tools_fn=_plan,
        synthesize_response_fn=lambda *_args: LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98，正股浮盈亏 USD -200；缺少成本和生命周期PnL证据。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        ),
    )

    assert first["data"]["command_id"] != second["data"]["command_id"]
    rows = AgentSessionStore(audit_db).list_recent(limit=10)
    assert len(rows) == 2
    assert {row["command_id"] for row in rows} == {first["data"]["command_id"], second["data"]["command_id"]}
    assert len({row["session_id"] for row in rows}) == 2


def test_agent_loop_returns_error_when_tool_budget_is_exhausted(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        refresh = payload.get("refresh_quotes") is True
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "action": "assigned-stock",
                "filters": {"account": "lx", "status": "open", "refresh_quotes": refresh},
                "rows": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "spot": 98 if refresh else None,
                        "quote_status": "fresh" if refresh else "missing_quote",
                        "assigned_stock_unrealized_pnl": -200 if refresh else None,
                    }
                ],
                "row_count": 1,
                "quote_refresh": {"status": "ok" if refresh else "missing_quote", "missing_symbols": [] if refresh else ["NVDA"]},
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        followup = conversation_context.get("agent_loop_followup") if isinstance(conversation_context, dict) else None
        refresh = isinstance(followup, dict)
        steps = tuple(
            PlannerPlanStep(
                id=f"step_{index}",
                tool_name="option_positions_read",
                arguments={
                    "action": "assigned-stock",
                    "account": "lx",
                    "status": "open",
                    **({"refresh_quotes": True} if refresh else {}),
                },
                purpose="读取指派正股持仓盈亏",
            )
            for index in range(1, 4)
        )
        return LlmPlannerResult(
            plan=PlannerPlan(goal="查看 lx 指派正股持仓盈亏", response_mode="synthesis", steps=steps),
            trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="查看 lx 指派正股持仓盈亏",
            sender_id="local",
            message_id="msg_agent_budget_exhausted",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=lambda *_args: LlmSynthesisResult(
            response_text="不应使用这段回答",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        ),
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "TOOL_BUDGET_EXHAUSTED"
    assert len(calls) == 5
    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert any(event["phase"] == "tool_budget_exhausted" for event in tool_plan_data["tool_events"])


def test_agent_loop_rejects_unrelated_followup_plan_for_evidence_gap(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "action": "assigned-stock",
                "filters": {"account": "lx", "status": "open", "refresh_quotes": False},
                "rows": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "spot": None,
                        "quote_status": "missing_quote",
                        "assigned_stock_unrealized_pnl": None,
                    }
                ],
                "row_count": 1,
                "quote_refresh": {"status": "missing_quote", "missing_symbols": ["NVDA"]},
            },
        )

    def _plan(
        _text: str,
        _settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        followup = conversation_context.get("agent_loop_followup") if isinstance(conversation_context, dict) else None
        if isinstance(followup, dict):
            return LlmPlannerResult(
                plan=PlannerPlan(
                    goal="查看运行状态",
                    response_mode="synthesis",
                    steps=(PlannerPlanStep(id="step_2", tool_name="runtime_status", arguments={}, purpose="无关补查"),),
                ),
                trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
                response_mode="synthesis",
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="option_positions_read",
                        arguments={"action": "assigned-stock", "account": "lx", "status": "open"},
                        purpose="读取指派正股持仓盈亏",
                    ),
                ),
            ),
            trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="查看 lx 指派正股持仓盈亏",
            sender_id="local",
            message_id="msg_agent_unrelated_followup",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=lambda *_args: LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股缺少行情。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        ),
    )

    assert out["ok"] is True
    assert [name for name, _payload in calls] == ["option_positions_read"]
    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert len(tool_plan_data["plan_revisions"]) == 1
    assert any(event["phase"] == "replan" and event["status"] == "unrelated_to_gap" for event in tool_plan_data["tool_events"])
