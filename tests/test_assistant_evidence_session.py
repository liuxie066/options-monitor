from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.assistant import AssistantRequest, AssistantSettings, AssistantLlmSettings, handle_assistant_message
from src.application.assistant.agent_loop import (
    EventNativePlanningResult,
    LlmPlannerResult,
    PLANNER_CONTEXT_USE_SCHEMA_VERSION,
    TOOL_CHECK_SCHEMA_VERSION,
    TOOL_PLAN_SCHEMA_VERSION,
)
from src.application.assistant.model_events import ModelToolCallEvent
from src.application.assistant.action_policy import ACTION_POLICY_SCHEMA_VERSION
from src.application.assistant.action_safety import ACTION_SAFETY_SCHEMA_VERSION
from src.application.assistant.answer_verifier import verify_response_against_evidence, verify_response_shape
from src.application.assistant.coverage_verifier import COVERAGE_RESULT_SCHEMA_VERSION, verify_coverage
from src.application.assistant.evidence import DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION, EVIDENCE_BUNDLE_SCHEMA_VERSION, build_evidence_bundle
from src.application.assistant.session_store import AgentSessionStore, collect_assistant_trace, format_assistant_trace
from src.application.assistant.task_contract import TASK_CONTRACT_SCHEMA_VERSION, build_task_contract
from src.application.assistant.verifier_hooks import HOOK_RESULT_SCHEMA_VERSION
from src.application.agent_tool_registry import get_tool_definition
from src.application.tool_execution import execute_tool as run_tool


TRACE_ROUTE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_trace_route_samples.jsonl"
DESIGN_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "AGENT_RELIABILITY_P0_P2_DESIGN.md"
TRACE_INTERNAL_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("session_id", re.compile(r"\bas_[A-Za-z0-9_:-]+\b")),
    (
        "internal_tool_name",
        re.compile(r"(?i)\b(?:analysis_query|analysis_catalog|manual_trade_open|inbound\.(?:manual_trade|upgrade|symbols|model))\b"),
    ),
    ("internal_sql", re.compile(r"(?is)(?:\bsql\b(?!ite)|\bselect\b.{0,240}\bfrom\b|\bwith\b.{0,240}\bselect\b)")),
    (
        "internal_id",
        re.compile(r"(?i)\b(?:stock_lot_id|record_id|event_id|source_deal_id|position_key|trace_id|artifact_path)\b"),
    ),
    ("internal_path", re.compile(r"(?i)(?:/Volumes/|/Users/|/private/|output_runs/|output_shared/|\.(?:sqlite3|jsonl)\b)")),
    ("raw_receipt", re.compile(r"(?i)(?:raw command log|raw_log|真实成交提醒)")),
    ("internal_mode", re.compile(r"(?i)\b(?:canonical|synthesis|tool_plan|output_contract|evidencebundle)\b")),
)
REQUIRED_TRACE_ROUTE_SAMPLE_IDS = {
    "trace_ask_missing_account",
    "trace_preview_manual_trade",
    "trace_rewrite_upgrade_conflict",
    "trace_fallback_bad_answer",
    "trace_denied_cross_account_write",
    "trace_pass_release_workflow_published",
    "trace_pass_release_workflow_failed",
    "trace_ask_read_scope_expansion",
    "trace_rewrite_runtime_notification_missing",
    "trace_pass_runtime_notification_delivered",
    "trace_rewrite_runtime_freshness_gap",
    "trace_rewrite_runtime_notification_conflict",
    "trace_rewrite_runtime_scheduler_skip",
    "trace_rewrite_quote_stale_freshness",
    "trace_rewrite_upgrade_stale_timeline",
    "trace_pass_operation_readback_applied",
    "trace_pass_operation_readback_cancelled",
    "trace_pass_upgrade_readback_cancelled",
    "trace_rewrite_release_no_matching_rows",
    "trace_rewrite_candidate_missing_trace",
    "trace_rewrite_upgrade_command_log_missing",
    "trace_denied_prompt_injection_chain",
    "trace_denied_planner_apply",
    "trace_ask_sql_period_scope_expansion",
}
P2_TRACE_ROUTE_MINIMUM_CASES: dict[str, set[str]] = {
    "trace_compact_no_internal_leak": {
        "trace_ask_missing_account",
        "trace_preview_manual_trade",
        "trace_rewrite_upgrade_conflict",
        "trace_fallback_bad_answer",
        "trace_denied_cross_account_write",
        "trace_pass_release_workflow_published",
    },
}


def _test_task_contract(
    *,
    goal: str,
    domain: str = "position",
    task_mode: str = "summarize",
    requested_effect: str = "read",
    scope: dict[str, Any] | None = None,
    required_answer: tuple[str, ...] = ("summary",),
    required_evidence: tuple[str, ...] = ("current_state",),
    answer_shape: tuple[str, ...] = ("conclusion", "evidence_boundary"),
    intent_families: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "goal": goal,
        "domain": domain,
        "task_mode": task_mode,
        "requested_effect": requested_effect,
        "intent_families": list(intent_families),
        "scope": dict(scope or {}),
        "required_answer": list(required_answer),
        "required_evidence": list(required_evidence),
        "answer_shape": list(answer_shape),
    }


def _assigned_stock_pnl_task_contract(*, goal: str = "查看 lx 指派正股持仓盈亏") -> dict[str, Any]:
    return _test_task_contract(
        goal=goal,
        domain="position",
        task_mode="summarize",
        scope={"requested_accounts": ["lx"], "requested_symbols": ["NVDA"]},
        required_answer=("summary", "source_and_policy"),
        required_evidence=("current_state", "quote_freshness"),
        answer_shape=("conclusion", "evidence_boundary"),
        intent_families=("assigned_stock_pnl",),
    )


def _operation_status_task_contract(*, goal: str, operation_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return _test_task_contract(
        goal=goal,
        domain="operation",
        task_mode="diagnose",
        scope={"operation_ids": list(operation_ids)},
        required_answer=("summary", "source_and_policy"),
        required_evidence=("observed_status", "command_status"),
        answer_shape=("observation", "evidence_boundary"),
        intent_families=("upgrade_status",),
    )


def _income_analysis_task_contract(
    *,
    goal: str,
    scope: dict[str, Any] | None = None,
    required_answer: tuple[str, ...] = ("summary", "source_and_policy"),
    required_evidence: tuple[str, ...] = ("summary", "driver_or_breakdown", "source_policy"),
    answer_shape: tuple[str, ...] = ("conclusion", "drivers", "source_policy"),
) -> dict[str, Any]:
    return _test_task_contract(
        goal=goal,
        domain="income",
        task_mode="analyze",
        scope=scope or {"requested_months": ["2026-06"]},
        required_answer=required_answer,
        required_evidence=required_evidence,
        answer_shape=answer_shape,
        intent_families=("income_analysis",),
    )


def _plan_payload(
    *,
    goal: str,
    tool_name: str,
    arguments: dict[str, Any],
    purpose: str,
    task_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": goal,
        "task_contract": task_contract or _test_task_contract(goal=goal),
        "steps": [
            {
                "id": "step_1",
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "purpose": purpose,
            }
        ],
    }


def _plan_step_arguments(plan: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return dict(plan["steps"][index]["arguments"])


def _recipe_payload(name: str) -> dict[str, Any]:
    recipes = {
        "income_analysis_breakdown": {
            "name": "income_analysis_breakdown",
            "evidence_needs": ["summary", "driver_or_breakdown", "source_policy"],
            "followup_tool": "analysis_query",
            "answer_shape": ["conclusion", "drivers", "source_policy"],
        },
        "operation_status_readback": {
            "name": "operation_status_readback",
            "evidence_needs": ["operation_readback", "receipt_status"],
            "followup_tool": "operation_timeline",
            "answer_shape": ["observation", "cause_chain", "evidence_boundary", "next_step"],
        },
        "strategy_replay_review": {
            "name": "strategy_replay_review",
            "evidence_needs": ["risk_premise", "dry_run_or_replay"],
            "followup_tool": "analysis_query",
            "answer_shape": ["judgement", "risk", "premise"],
        },
    }
    return dict(recipes[name])


def _event_plan_result(
    *,
    goal: str,
    tool_name: str,
    arguments: dict[str, Any],
    purpose: str,
    task_contract: dict[str, Any] | None = None,
) -> LlmPlannerResult:
    return LlmPlannerResult(
        plan=None,
        trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        event_plan=EventNativePlanningResult(
            events=(
                ModelToolCallEvent(
                    event_id="model_tool_call_1",
                    tool_call_id="call_1",
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    purpose=purpose,
                    provider="openai",
                    parent_event_id="user_message_1",
                ),
            ),
            task_contract=task_contract or _test_task_contract(goal=goal),
            context_use={
                "schema_version": PLANNER_CONTEXT_USE_SCHEMA_VERSION,
                "mode": "none",
                "referenced_turn_ids": [],
                "referenced_evidence_refs": [],
                "inherited_slots": {},
                "current_message_slots": {},
                "override_slots": {},
                "requires_clarification": False,
                "clarification_question": None,
            },
            provider="openai",
            goal=goal,
        ),
    )


def _load_trace_route_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in TRACE_ROUTE_FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
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


def _assert_no_internal_trace_leak(text: str, *, case_id: str) -> None:
    for code, pattern in TRACE_INTERNAL_LEAK_PATTERNS:
        assert not pattern.search(text), f"{case_id} leaked {code}: {text}"


def _walk_trace_values(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, item in value.items():
            for child_key, child_value in _walk_trace_values(item):
                out.append((f"{key}.{child_key}" if child_key else str(key), child_value))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            for child_key, child_value in _walk_trace_values(item):
                out.append((f"{index}.{child_key}" if child_key else str(index), child_value))
        return out
    return [("", value)]


def _assert_trace_fixture_sensitive_values_forbidden(case: dict[str, Any]) -> None:
    forbidden = "\n".join(str(item) for item in case.get("expect_not_contains") or ())
    missing: list[str] = []
    sensitive_key_terms = (
        "session_id",
        "sql",
        "local_path",
        "raw_log",
        "raw_text",
        "message_id",
        "run_id",
        "github_release_url",
        "stock_lot_id",
        "runtime_root",
        "trace_path",
        "artifact_path",
    )

    for path, value in _walk_trace_values(case.get("trace")):
        path_lower = path.lower()
        if not any(term in path_lower for term in sensitive_key_terms):
            continue
        text = str(value or "").strip()
        if not text:
            continue
        key = path.rsplit(".", 1)[-1]
        if not any(token and (token in text or text in token or key in token) for token in forbidden.splitlines()):
            missing.append(f"{path}={text}")

    assert missing == [], f"{case.get('id')} does not forbid sensitive trace values: {missing}"


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


def test_symbol_resolve_canonical_symbol_supports_symbol_claims() -> None:
    bundle = build_evidence_bundle(
        question="泡泡玛特是什么 symbol？",
        plan={"goal": "解析泡泡玛特的标的身份", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "symbol_resolve",
                "payload": {"symbol": "泡泡玛特"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "symbol_resolve.output.v1",
                    "canonical_renderer": "symbol_resolve",
                    "source_label": "OM symbol identity resolver",
                    "guard_profile": "symbol_identity",
                    "fact_fields": [
                        "symbol",
                        "raw_input",
                        "canonical_symbol",
                        "market",
                        "currency",
                        "futu_code",
                        "source_kind",
                        "status",
                        "message",
                    ],
                },
                "data": {
                    "schema_version": "symbol_resolve.v1",
                    "symbol": "泡泡玛特",
                    "resolved": True,
                    "raw_input": "泡泡玛特",
                    "canonical_symbol": "9992.HK",
                    "market": "HK",
                    "currency": "HKD",
                    "futu_code": "HK.09992",
                    "source_kind": "alias",
                    "status": "ok",
                    "message": "泡泡玛特 -> 9992.HK",
                },
            }
        ],
    )

    canonical = next(item for item in bundle.public_payload()["facts"] if item["path"] == "canonical_symbol")
    assert canonical["unit"] == "symbol"
    good = verify_response_against_evidence("泡泡玛特对应标准代码 9992.HK（HKD，Futu HK.09992）。", evidence_bundle=bundle)
    assert good.violations == ()


def test_evidence_bundle_extracts_analysis_catalog_contract_facts() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    definition = get_tool_definition("analysis_catalog")
    assert definition is not None
    output_contract = definition.resolve_output_contract({})

    bundle = build_evidence_bundle(
        question="能分析哪些数据？",
        plan={"goal": "能分析哪些数据？", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_catalog",
                "payload": {"config_key": "us"},
                "ok": True,
                "error": None,
                "output_contract": output_contract,
                "data": {
                    "source_label": "OM read-only analysis workspace",
                    "view_count": 1,
                    "view_names": ["account_monthly_performance"],
                    "views": {
                        "account_monthly_performance": {
                            "description": "monthly account performance",
                            "fields": ["month", "account", "net_income_cny"],
                            "freshness": "snapshot",
                            "recommended_filters": ["month", "account"],
                        }
                    },
                    "sql_rules": {"allowed_statements": ["SELECT", "WITH"], "writes_allowed": False},
                },
            }
        ],
    )

    facts = bundle.public_payload()["facts"]
    assert any(item["path"] == "view_count" and item["value"] == 1 for item in facts)
    assert any(
        item["path"] == "view_names[]" and item["value"] == "account_monthly_performance"
        for item in facts
    )
    assert any(item["path"] == "sql_rules.allowed_statements[]" and item["value"] == "SELECT" for item in facts)
    assert any(item["path"] == "sql_rules.writes_allowed" and item["value"] is False for item in facts)


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


def test_answer_verifier_rejects_definitive_status_on_conflicting_diagnostics() -> None:
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

    bad = verify_response_against_evidence("升级成功，已经完成。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad.violations)

    ok = verify_response_against_evidence(
        "不能确认升级成功：operation_status=applied 和 outcome_status=failed 存在冲突。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


def test_answer_verifier_rejects_release_success_when_publication_evidence_missing() -> None:
    bundle = build_evidence_bundle(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan={"goal": "检查远端 release 发布状态", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {
                    "sql": (
                        "select command_id, operation_status, current_version, target_version, release_tag, "
                        "receipt_status from upgrade_operation_status"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                },
                "data": {
                    "rows": [
                        {
                            "command_id": "in_release_check",
                            "operation_status": "applied",
                            "current_version": "1.2.272",
                            "target_version": "1.2.273",
                            "release_tag": "v1.2.273",
                            "receipt_status": "not_observed",
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
    assert diagnostic["domain"] == "upgrade"
    assert diagnostic["status"] == "observed_operation_status"
    missing_kinds = {item["kind"] for item in diagnostic["missing_data"]}
    assert "release_publication_status_missing" in missing_kinds
    assert "receipt_not_observed" in missing_kinds

    bad = verify_response_against_evidence("远端 release 已发布成功。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad.violations)

    bad_failed = verify_response_against_evidence("远端 release 发布失败。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad_failed.violations)

    ok = verify_response_against_evidence(
        "不能确认远端 release 已发布成功：只有 release_tag，没有 GitHub Release 发布状态证据。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


def test_task_contract_requires_release_status_for_release_publication_question() -> None:
    contract = build_task_contract(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan={"goal": "检查远端 release 发布状态", "steps": []},
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )

    assert contract.intent_families == ("upgrade_status",)
    assert "command_status" not in contract.required_answer
    assert "current_version" not in contract.required_answer
    assert "target_version" not in contract.required_answer
    assert "release_status" in contract.required_answer


def test_coverage_marks_release_publication_status_missing_unrecoverable() -> None:
    plan = _plan_payload(
        goal="检查远端 release 发布状态",
        task_contract=_test_task_contract(
            goal="检查远端 release 发布状态",
            domain="operation",
            task_mode="diagnose",
            scope={"release_tags": ["v1.2.273"]},
            required_answer=("release_status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": (
                "select command_id, operation_status, current_version, target_version, release_tag, "
                "receipt_status from upgrade_operation_status where release_tag = 'v1.2.273'"
            )
        },
        purpose="读取升级操作中的 release tag 和回执状态",
    )
    contract = build_task_contract(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
                            "command_id": "in_release_check",
                            "operation_status": "applied",
                            "current_version": "1.2.272",
                            "target_version": "1.2.273",
                            "release_tag": "v1.2.273",
                            "receipt_status": "observed",
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
    assert coverage["missing"] == ["release_status"]
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert set(gaps) == {"upgrade_release_publication_status_missing"}
    assert gaps["upgrade_release_publication_status_missing"]["required_answer_key"] == "release_status"
    assert gaps["upgrade_release_publication_status_missing"]["recoverable"] is False
    assert gaps["upgrade_release_publication_status_missing"]["recoverable_by"] == "release_workflow_status"
    assert "suggested_tool" not in gaps["upgrade_release_publication_status_missing"]


def test_coverage_accepts_release_publication_status_evidence() -> None:
    plan = _plan_payload(
        goal="检查远端 release 发布状态",
        task_contract=_test_task_contract(
            goal="检查远端 release 发布状态",
            domain="operation",
            task_mode="diagnose",
            scope={"release_tags": ["v1.2.273"]},
            required_answer=("release_status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": (
                "select current_version, target_version, operation_status, release_tag, release_status, "
                "release_published_at, github_release_url from upgrade_operation_status where release_tag = 'v1.2.273'"
            )
        },
        purpose="读取远端 release 发布证据",
    )
    contract = build_task_contract(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
                            "operation_status": "applied",
                            "current_version": "1.2.272",
                            "target_version": "1.2.273",
                            "release_tag": "v1.2.273",
                            "release_status": "published",
                            "release_published_at": "2026-06-14T19:01:30Z",
                            "github_release_url": "https://github.example/releases/tag/v1.2.273",
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


def test_answer_verifier_allows_release_success_when_publication_evidence_present() -> None:
    bundle = build_evidence_bundle(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan={"goal": "检查远端 release 发布状态", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {
                    "sql": (
                        "select release_tag, release_status, release_published_at, github_release_url "
                        "from upgrade_operation_status"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                },
                "data": {
                    "rows": [
                        {
                            "release_tag": "v1.2.273",
                            "release_status": "published",
                            "release_published_at": "2026-06-14T19:01:30Z",
                            "github_release_url": "https://github.example/releases/tag/v1.2.273",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["upgrade_operation_status"],
                    "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "远端 release 已发布成功：release_status=published，published_at=2026-06-14T19:01:30Z。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


def test_answer_verifier_allows_release_failure_when_failure_evidence_present() -> None:
    bundle = build_evidence_bundle(
        question="v1.2.273 远端 release 发布成功了吗？",
        plan={"goal": "检查远端 release 发布状态", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select release_tag, release_status from upgrade_operation_status"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                },
                "data": {
                    "rows": [{"release_tag": "v1.2.273", "release_status": "failed"}],
                    "row_count": 1,
                    "views_used": ["upgrade_operation_status"],
                    "evidence": {"coverage": {"views": ["upgrade_operation_status"]}},
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "远端 release 发布失败：release_status=failed。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


def test_release_status_conflict_with_outcome_is_unrecoverable() -> None:
    plan = _plan_payload(
        goal="检查远端 release 发布状态冲突",
        task_contract=_test_task_contract(
            goal="检查远端 release 发布状态冲突",
            domain="operation",
            task_mode="diagnose",
            scope={"release_tags": ["v1.2.277"]},
            required_answer=("release_status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": (
                "select current_version, target_version, release_tag, release_status, outcome_status "
                "from upgrade_operation_status where release_tag = 'v1.2.277'"
            )
        },
        purpose="读取远端 release 与升级结果状态",
    )
    contract = build_task_contract(
        question="v1.2.277 远端 release 发布成功了吗？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
                            "current_version": "1.2.276",
                            "target_version": "1.2.277",
                            "release_tag": "v1.2.277",
                            "release_status": "published",
                            "release_published_at": "2026-06-14T20:33:30Z",
                            "outcome_status": "failed",
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
    assert diagnostic["confidence"] == "conflict"
    assert diagnostic["answer_boundary"] == "conflicting_upgrade_operation_evidence_only"
    assert "outcome_status=failed" in diagnostic["observed_reason"]
    assert "release_status=published" in diagnostic["observed_reason"]

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    assert coverage["status"] == "unrecoverable_gap"
    assert coverage["next_action"] == "answer_with_missing_data"
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert gaps["upgrade_status_conflict"]["recoverable"] is False

    bad = verify_response_against_evidence("v1.2.277 远端 release 已发布成功。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad.violations)

    ok = verify_response_against_evidence(
        "不能确认 v1.2.277 远端 release 发布成功：release_status=published 但 outcome_status=failed，状态证据存在冲突。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


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
    plan = _plan_payload(
        goal="检查升级版本和回执",
        task_contract=_test_task_contract(
            goal="检查升级版本和回执",
            domain="operation",
            task_mode="diagnose",
            scope={"operation_ids": ["in_123"]},
            required_answer=("current_version", "target_version", "receipt_status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
        purpose="读取升级 operation 状态",
    )
    contract = build_task_contract(
        question="为什么升级完成后没有显示当前版本和目标版本，也没收到成功回执？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
    plan = _plan_payload(
        goal="检查升级状态冲突",
        task_contract=_test_task_contract(
            goal="检查升级状态冲突",
            domain="operation",
            task_mode="diagnose",
            scope={"operation_ids": ["in_123"]},
            required_answer=("status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
        purpose="读取升级 operation 状态",
    )
    contract = build_task_contract(
        question="升级 in_123 到底成功还是失败？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
    plan = _plan_payload(
        goal="检查升级版本和回执",
        task_contract=_test_task_contract(
            goal="检查升级版本和回执",
            domain="operation",
            task_mode="diagnose",
            scope={"operation_ids": ["in_456"]},
            required_answer=("current_version", "target_version", "receipt_status", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="inbound.upgrade",
        arguments={"target_version": None},
        purpose="生成升级预览",
    )
    contract = build_task_contract(
        question="为什么升级 in_456 当前版本和目标版本是空的？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
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
    plan = _plan_payload(
        goal="检查升级版本和回执",
        task_contract=_test_task_contract(
            goal="检查升级当前版本和目标版本",
            domain="operation",
            task_mode="diagnose",
            scope={"operation_ids": ["in_123"]},
            required_answer=("current_version", "target_version", "source_and_policy"),
            required_evidence=("observed_status", "source_policy"),
            answer_shape=("observation", "evidence_boundary", "next_step"),
            intent_families=("upgrade_status",),
        ),
        tool_name="analysis_query",
        arguments={"sql": "select * from upgrade_operation_status where command_id = 'in_123'"},
        purpose="读取升级 operation 状态",
    )
    contract = build_task_contract(
        question="检查升级 in_123 当前版本和目标版本",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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


def test_evidence_bundle_extracts_candidate_filter_explain_observed_trace() -> None:
    bundle = build_evidence_bundle(
        question="泡泡玛特被哪个参数过滤了",
        plan={"goal": "解释泡泡玛特候选过滤诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "candidate_filter_explain",
                "payload": {"symbol": "泡泡玛特"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "candidate_filter_explain.output.v1",
                    "canonical_renderer": "candidate_filter_explain",
                    "source_label": "OM candidate filter trace",
                    "guard_profile": "candidate_filter",
                    "primary_rows": "functions",
                    "row_count_field": "trace_count",
                    "fact_fields": ["canonical_symbol", "raw_symbol", "trace_count"],
                },
                "data": {
                    "symbol": "9992.HK",
                    "raw_symbol": "泡泡玛特",
                    "canonical_symbol": "9992.HK",
                    "scope": {"account": "sy", "account_semantics": "scan_scope"},
                    "trace_count": 1,
                    "functions": [
                        {
                            "function": "sell_put",
                            "status": "rejected",
                            "reason_counts": {"risk_spread": 1},
                            "events": [
                                {
                                    "rule": "risk_spread",
                                    "metric_value": 0.35,
                                    "threshold": 0.2,
                                    "message": "spread too wide",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "candidate_filter"
    assert diagnostic["status"] == "observed_rejection"
    assert diagnostic["scope"]["accounts"] == ["sy"]
    assert diagnostic["scope"]["symbols"] == ["9992.HK"]
    assert diagnostic["source"]["view"] == "candidate_filter_trace"
    assert diagnostic["confidence"] == "direct"
    assert "价差不合格" in diagnostic["observed_reason"]


def test_contract_verifier_classifies_candidate_filter_metrics_before_symbol_check() -> None:
    bundle = build_evidence_bundle(
        question="泡泡玛特被哪个参数过滤了",
        plan={"goal": "解释泡泡玛特候选过滤诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "candidate_filter_explain",
                "payload": {"symbol": "泡泡玛特"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "candidate_filter_explain.output.v1",
                    "canonical_renderer": "candidate_filter_explain",
                    "source_label": "OM candidate filter trace",
                    "guard_profile": "candidate_filter",
                    "primary_rows": "functions",
                    "row_count_field": "trace_count",
                    "fact_fields": [
                        "canonical_symbol",
                        "raw_symbol",
                        "trace_count",
                        "functions[].function",
                        "functions[].status",
                        "functions[].reason_counts",
                        "functions[].reason_labels",
                        "functions[].rejection_reason_counts",
                        "functions[].rejection_reasons[].rule",
                        "functions[].rejection_reasons[].label",
                        "functions[].rejection_reasons[].count",
                        "functions[].events[].rule",
                        "functions[].events[].rule_label",
                        "functions[].events[].metric_value",
                        "functions[].events[].threshold",
                        "functions[].events[].message",
                    ],
                },
                "data": {
                    "symbol": "9992.HK",
                    "raw_symbol": "泡泡玛特",
                    "canonical_symbol": "9992.HK",
                    "scope": {"account": "sy", "account_semantics": "scan_scope"},
                    "trace_count": 6,
                    "functions": [
                        {
                            "function": "sell_put",
                            "status": "rejected",
                            "reason_counts": {
                                "vol_edge_ratio_below_min": 2,
                                "risk_spread": 1,
                                "annualized_return_below_min": 1,
                                "open_interest_below_min": 1,
                                "dte_out_of_range": 1,
                                "delta_too_high": 1,
                            },
                            "reason_labels": {
                                "vol_edge_ratio_below_min": "IV/RV 不足",
                                "risk_spread": "价差不合格",
                                "annualized_return_below_min": "年化收益不足",
                                "open_interest_below_min": "OI 不足",
                                "dte_out_of_range": "DTE 不符合",
                                "delta_too_high": "Delta 过高",
                            },
                            "rejection_reason_counts": {
                                "vol_edge_ratio_below_min": 2,
                                "risk_spread": 1,
                                "annualized_return_below_min": 1,
                            },
                            "rejection_reasons": [
                                {"rule": "vol_edge_ratio_below_min", "label": "IV/RV 不足", "count": 2},
                                {"rule": "risk_spread", "label": "价差不合格", "count": 1},
                                {"rule": "annualized_return_below_min", "label": "年化收益不足", "count": 1},
                                {"rule": "open_interest_below_min", "label": "OI 不足", "count": 1},
                                {"rule": "dte_out_of_range", "label": "DTE 不符合", "count": 1},
                                {"rule": "delta_too_high", "label": "Delta 过高", "count": 1},
                            ],
                            "events": [
                                {
                                    "rule": "vol_edge_ratio_below_min",
                                    "rule_label": "IV/RV 不足",
                                    "metric_value": 0.91,
                                    "threshold": 1.1,
                                    "message": "IV/RV edge below minimum",
                                },
                                {
                                    "rule": "risk_spread",
                                    "rule_label": "价差不合格",
                                    "metric_value": 0.35,
                                    "threshold": 0.2,
                                    "message": "spread too wide",
                                },
                            ],
                        }
                    ],
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        (
            "9992.HK 本次被 IV/RV 不足、OI 不足、DTE 不符合、Delta 过高、"
            "annualized_return_below_min 和 risk_spread 过滤。"
        ),
        evidence_bundle=bundle,
    )
    assert ok.violations == ()
    classifications = {item["claim"]: item for item in ok.public_payload()["claim_classification"]}
    assert classifications["9992.HK"]["classification"] == "supported_symbol"
    assert classifications["IV"]["classification"] == "domain_evidence_term"
    assert classifications["RV"]["classification"] == "domain_evidence_term"
    assert classifications["OI"]["classification"] == "domain_evidence_term"
    assert classifications["DTE"]["classification"] == "domain_evidence_term"

    bad = verify_response_against_evidence(
        "NVDA 也被 risk_spread 过滤。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_contract_symbol" and item["claim"] == "NVDA" for item in bad.violations)
    bad_classifications = {item["claim"]: item for item in bad.public_payload()["claim_classification"]}
    assert bad_classifications["NVDA"]["classification"] == "unsupported_symbol"


def test_evidence_bundle_marks_candidate_filter_explain_no_matching_trace_as_missing() -> None:
    bundle = build_evidence_bundle(
        question="为什么 PDD 没出现在候选里",
        plan={"goal": "解释 PDD 候选诊断缺失边界", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "candidate_filter_explain",
                "payload": {"symbol": "PDD"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "candidate_filter_explain.output.v1",
                    "canonical_renderer": "candidate_filter_explain",
                    "source_label": "OM candidate filter trace",
                    "guard_profile": "candidate_filter",
                    "primary_rows": "functions",
                    "row_count_field": "trace_count",
                    "fact_fields": ["canonical_symbol", "trace_count"],
                },
                "data": {
                    "symbol": "PDD",
                    "canonical_symbol": "PDD",
                    "trace_count": 0,
                    "functions": [],
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "candidate_filter"
    assert diagnostic["status"] == "no_matching_rows"
    assert diagnostic["confidence"] == "missing"
    assert diagnostic["missing_data"][0]["kind"] == "candidate_filter_trace_no_matching_rows"

    bad = verify_response_against_evidence(
        "PDD 没出现在候选里的原因是 liquidity 过滤。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_diagnostic_root_cause_claim" for item in bad.violations)


def test_task_contract_and_coverage_accept_candidate_filter_explain_trace_evidence() -> None:
    contract = build_task_contract(
        question="为什么 NVDA 没出现在候选里？",
        plan={
            "goal": "解释 NVDA 单标的候选过滤 trace",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "candidate_filter_explain",
                    "arguments": {"symbol": "NVDA", "account": "lx"},
                    "purpose": "读取 NVDA 候选过滤 trace",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    assert contract.intent_families == ("candidate_filter_diagnostic",)
    assert contract.required_answer == ("summary", "source_and_policy")
    assert "main_drivers" not in contract.required_answer

    observed_bundle = build_evidence_bundle(
        question=contract.question,
        plan=contract.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "candidate_filter_explain",
                "payload": {"symbol": "NVDA", "account": "lx"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "candidate_filter_explain.output.v1",
                    "canonical_renderer": "candidate_filter_explain",
                    "source_label": "OM candidate filter trace",
                    "guard_profile": "candidate_filter",
                    "primary_rows": "functions",
                    "row_count_field": "trace_count",
                    "fact_fields": ["canonical_symbol", "trace_count"],
                },
                "data": {
                    "symbol": "NVDA",
                    "canonical_symbol": "NVDA",
                    "account": "lx",
                    "scope": {"account": "lx", "account_semantics": "scan_scope"},
                    "trace_count": 1,
                    "functions": [
                        {
                            "function": "sell_put",
                            "status": "rejected",
                            "reason_counts": {"liquidity": 1},
                            "events": [{"rule": "liquidity", "message": "open interest too low"}],
                        }
                    ],
                },
            }
        ],
    )
    observed = verify_coverage(task_contract=contract, evidence_bundle=observed_bundle).public_payload()
    assert observed["status"] == "complete"
    assert observed["missing"] == []
    assert observed["gaps"] == []


def test_task_contract_and_coverage_accept_candidate_filter_missing_trace_boundary() -> None:
    contract = build_task_contract(
        question="为什么 PDD 没出现在候选里？",
        plan={
            "goal": "解释 PDD 候选 trace 缺失边界",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "candidate_filter_explain",
                    "arguments": {"symbol": "PDD"},
                    "purpose": "读取 PDD 候选过滤 trace",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )
    assert contract.intent_families == ("candidate_filter_diagnostic",)
    assert contract.required_answer == ("summary", "source_and_policy")

    missing_bundle = build_evidence_bundle(
        question=contract.question,
        plan=contract.public_payload(),
        observations=[
            {
                "index": 1,
                "tool_name": "candidate_filter_explain",
                "payload": {"symbol": "PDD"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "candidate_filter_explain.output.v1",
                    "canonical_renderer": "candidate_filter_explain",
                    "source_label": "OM candidate filter trace",
                    "guard_profile": "candidate_filter",
                    "primary_rows": "functions",
                    "row_count_field": "trace_count",
                    "fact_fields": ["canonical_symbol", "trace_count"],
                },
                "data": {
                    "symbol": "PDD",
                    "canonical_symbol": "PDD",
                    "trace_count": 0,
                    "functions": [],
                },
            }
        ],
    )
    missing = verify_coverage(task_contract=contract, evidence_bundle=missing_bundle).public_payload()
    assert missing["status"] == "complete"
    assert missing["missing"] == []
    assert missing["gaps"] == []


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


def test_answer_verifier_allows_direct_runtime_skip_root_cause() -> None:
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
                    "evidence": {"coverage": {"views": ["runtime_tick_status"], "accounts": ["lx"]}},
                },
            }
        ],
    )

    result = verify_response_against_evidence(
        "今天 lx 没推送的原因是 runtime 记录显示 scheduler_skip，skip_reason 是 market_closed。",
        evidence_bundle=bundle,
    )
    assert result.violations == ()


def test_evidence_bundle_infers_runtime_scheduler_reason_from_rows() -> None:
    bundle = build_evidence_bundle(
        question="今天 sy 为什么没推送",
        plan={"goal": "解释 runtime scheduler 跳过原因", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {
                    "sql": (
                        "select account, notification_status, scheduler_should_run_scan, "
                        "scheduler_reason from runtime_tick_status"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "rows[].account",
                        "rows[].notification_status",
                        "rows[].scheduler_should_run_scan",
                        "rows[].scheduler_reason",
                    ],
                },
                "data": {
                    "rows": [
                        {
                            "account": "sy",
                            "notification_status": "scheduler_skipped",
                            "scheduler_should_run_scan": False,
                            "scheduler_reason": "market_closed",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"], "accounts": ["sy"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "runtime_tick"
    assert diagnostic["status"] == "observed_scheduler_skip"
    assert diagnostic["scope"]["accounts"] == ["sy"]
    assert diagnostic["confidence"] == "direct"
    assert diagnostic["observed_reason"] == "scheduler skipped because market_closed"
    assert diagnostic["answer_boundary"] == "observed_runtime_status_only"

    bad = verify_response_against_evidence(
        "今天 sy 没推送是通知通道故障。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_runtime_notification_root_cause_claim" for item in bad.violations)

    ok = verify_response_against_evidence(
        "今天 sy 没推送当前只能归因到调度器跳过：scheduler_reason=market_closed；这不是通知通道失败证据。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


def test_answer_verifier_rejects_runtime_freshness_gap_root_cause() -> None:
    bundle = build_evidence_bundle(
        question="今天为什么没推送",
        plan={"goal": "解释 runtime 推送诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select market, account, freshness_status, summary from runtime_tick_status"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].market", "rows[].account", "rows[].freshness_status", "rows[].summary"],
                },
                "data": {
                    "rows": [
                        {
                            "market": "us",
                            "account": "lx",
                            "freshness_status": "stale",
                            "summary": "runtime snapshot is stale",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"], "accounts": ["lx"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["status"] == "observed_runtime_freshness_gap"
    assert diagnostic["confidence"] == "direct"

    bad = verify_response_against_evidence(
        "今天 lx 没推送的原因是 market_closed。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_diagnostic_root_cause_claim" for item in bad.violations)

    caveated = verify_response_against_evidence(
        "runtime 记录是旧快照，不能确认今天 lx 没推送的原因。",
        evidence_bundle=bundle,
    )
    assert caveated.violations == ()


def test_answer_verifier_rejects_runtime_notification_missing_success_claim() -> None:
    bundle = build_evidence_bundle(
        question="今天 lx 推送成功了吗",
        plan={"goal": "解释 runtime 通知送达诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select account, latest_status, notification_status from runtime_tick_status"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].account", "rows[].latest_status", "rows[].notification_status"],
                },
                "data": {
                    "rows": [
                        {
                            "account": "lx",
                            "latest_status": "success",
                            "notification_status": "not_observed",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"], "accounts": ["lx"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["status"] == "observed_notification_missing"
    assert diagnostic["confidence"] == "direct"

    bad = verify_response_against_evidence(
        "今天 lx 推送成功。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad.violations)

    caveated = verify_response_against_evidence(
        "只能确认 runtime latest_status=success，但 notification_status=not_observed，不能确认推送成功。",
        evidence_bundle=bundle,
    )
    assert caveated.violations == ()


def test_evidence_bundle_infers_runtime_notification_status_conflict_from_rows() -> None:
    bundle = build_evidence_bundle(
        question="今天 lx 推送成功了吗",
        plan={"goal": "解释 runtime 通知送达冲突", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {
                    "sql": (
                        "select account, latest_status, notification_status "
                        "from runtime_tick_status where account = 'lx'"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].account", "rows[].latest_status", "rows[].notification_status"],
                },
                "data": {
                    "rows": [
                        {
                            "account": "lx",
                            "latest_status": "success",
                            "notification_status": "failed",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["runtime_tick_status"],
                    "evidence": {"coverage": {"views": ["runtime_tick_status"], "accounts": ["lx"]}},
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "runtime_tick"
    assert diagnostic["status"] == "conflicting_evidence"
    assert diagnostic["confidence"] == "conflict"
    assert diagnostic["answer_boundary"] == "conflicting_runtime_evidence_only"
    assert "latest_status=success" in diagnostic["observed_reason"]
    assert "notification_status=failed" in diagnostic["observed_reason"]

    bad = verify_response_against_evidence(
        "今天 lx 推送成功。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_diagnostic_status_claim" for item in bad.violations)

    caveated = verify_response_against_evidence(
        "不能确认今天 lx 推送成功：latest_status=success 但 notification_status=failed，运行和通知证据存在冲突。",
        evidence_bundle=bundle,
    )
    assert caveated.violations == ()


def test_answer_verifier_rejects_partial_confidence_root_cause() -> None:
    bundle = build_evidence_bundle(
        question="为什么 NVDA 没出现在候选里",
        plan={"goal": "解释 NVDA 候选诊断", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select symbol, diagnostic_status, summary from candidate_filter_diagnostics"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol", "rows[].diagnostic_status", "rows[].summary"],
                },
                "data": {
                    "rows": [
                        {
                            "symbol": "NVDA",
                            "diagnostic_status": "observed_candidate_summary_only",
                            "summary": "candidate diagnostics only have summary-level evidence",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["candidate_filter_diagnostics"],
                    "evidence": {
                        "coverage": {"views": ["candidate_filter_diagnostics"], "symbols": ["NVDA"]},
                        "diagnostics": [
                            {
                                "view": "candidate_filter_diagnostics",
                                "status": "observed_candidate_summary_only",
                                "severity": "warning",
                                "symbols": ["NVDA"],
                                "summary": "candidate diagnostics only have summary-level evidence",
                                "answer_boundary": "summary_diagnostic_evidence_only",
                            }
                        ],
                    },
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["confidence"] == "partial"

    bad = verify_response_against_evidence(
        "NVDA 没出现在候选里的原因是 liquidity 过滤。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_diagnostic_root_cause_claim" for item in bad.violations)


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


def test_answer_verifier_rejects_analysis_quote_gap_upstream_root_cause() -> None:
    bundle = build_evidence_bundle(
        question="为什么 FUTU 行情是旧的",
        plan={"goal": "解释 FUTU 行情新鲜度", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select symbol, quote_status from quote_freshness where symbol = 'FUTU'"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol", "rows[].quote_status"],
                },
                "data": {
                    "rows": [{"symbol": "FUTU", "quote_status": "stale"}],
                    "row_count": 1,
                    "views_used": ["quote_freshness"],
                    "evidence": {
                        "coverage": {"views": ["quote_freshness"], "symbols": ["FUTU"]},
                        "diagnostics": [
                            {
                                "view": "quote_freshness",
                                "status": "observed_quote_freshness_gap",
                                "severity": "warning",
                                "symbols": ["FUTU"],
                                "summary": "quote rows include stale quote status",
                                "answer_boundary": "quote_dependent_calculations_only",
                            }
                        ],
                    },
                },
            }
        ],
    )

    bad = verify_response_against_evidence(
        "FUTU 行情是旧的，原因是 OpenD 连接失败。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_quote_upstream_root_cause_claim" for item in bad.violations)


def test_evidence_bundle_infers_quote_freshness_gap_with_as_of_from_rows() -> None:
    bundle = build_evidence_bundle(
        question="FUTU 指派正股现在浮盈亏是多少？",
        plan={"goal": "解释 FUTU stale quote 对当前浮盈亏的影响", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select symbol, quote_status, spot_time from quote_freshness where symbol = 'FUTU'"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol", "rows[].quote_status", "rows[].spot_time"],
                },
                "data": {
                    "rows": [
                        {
                            "symbol": "FUTU",
                            "quote_status": "stale",
                            "spot_time": "2026-06-14T21:30:00+08:00",
                        }
                    ],
                    "row_count": 1,
                    "views_used": ["quote_freshness"],
                    "evidence": {
                        "coverage": {"views": ["quote_freshness"], "symbols": ["FUTU"]},
                        "freshness": [
                            {
                                "view": "quote_freshness",
                                "symbol": "FUTU",
                                "quote_status": "stale",
                                "as_of": "2026-06-14T21:30:00+08:00",
                            }
                        ],
                    },
                },
            }
        ],
    )

    diagnostic = bundle.public_payload()["diagnostics"][0]
    assert diagnostic["domain"] == "quote_freshness"
    assert diagnostic["status"] == "observed_quote_freshness_gap"
    assert diagnostic["confidence"] == "direct"
    assert diagnostic["answer_boundary"] == "quote_dependent_calculations_only"
    assert "quote_status=stale" in diagnostic["observed_reason"]
    assert "as_of=2026-06-14T21:30:00+08:00" in diagnostic["observed_reason"]

    bad = verify_response_against_evidence(
        "FUTU 现在行情是最新的，可以确认当前浮盈亏。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_analysis_freshness_claim" for item in bad.violations)

    ok = verify_response_against_evidence(
        "FUTU quote_status=stale，as_of=2026-06-14T21:30:00+08:00，不能确认当前浮盈亏。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()


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
    plan = _plan_payload(
        goal="对比 lx 和 sy 的账户收益",
        task_contract=_test_task_contract(
            goal="对比 lx 和 sy 的账户收益",
            domain="income",
            task_mode="compare",
            scope={"accounts": ["lx", "sy"]},
            required_answer=("summary", "comparison_winner", "amount_difference", "source_and_policy"),
            required_evidence=("same_scope_comparable_data", "source_policy"),
            answer_shape=("conclusion", "same_scope_comparison", "difference", "source_policy"),
            intent_families=("account_comparison",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": "select month, account, net_income_cny from account_monthly_performance where account = 'lx'",
            "limit": 20,
        },
        purpose="读取 lx 账户收益",
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan,
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
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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


def test_planner_task_contract_payload_survives_plan_like_payload() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "分析 2026-06 收益来源",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "分析 2026-06 收益来源",
            "domain": "income",
            "task_mode": "analyze",
            "requested_effect": "read",
            "scope": {"months": ["2026-06"], "accounts": ["lx", "sy"], "config_keys": ["us"]},
            "required_answer": ["summary", "main_drivers", "source_and_policy"],
            "required_evidence": ["summary", "driver_or_breakdown", "source_policy"],
            "answer_shape": ["conclusion", "drivers", "source_policy"],
        },
        "selected_recipe": {
            **_recipe_payload("income_analysis_breakdown"),
            "reason": "income analysis requires driver evidence",
        },
        "required_capabilities": ["analysis_query", "read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": "select month, account, net_income_cny from account_monthly_performance where month = '2026-06'",
                    "limit": 20,
                },
                "purpose": "读取收益汇总",
            }
        ],
    }
    assert plan["task_contract"]["task_mode"] == "analyze"
    assert plan["selected_recipe"]["name"] == "income_analysis_breakdown"
    assert plan["selected_recipe"]["followup_tool"] == "analysis_query"
    assert "driver_or_breakdown" in plan["selected_recipe"]["evidence_needs"]

    contract = build_task_contract(
        question="6月收益分析",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )

    assert contract.planner_declared is True
    assert contract.domain == "income"
    assert contract.task_mode == "analyze"
    assert "driver_or_breakdown" in contract.required_evidence
    assert "drivers" in contract.answer_shape
    assert contract.scope["planned_months"] == ["2026-06"]
    assert contract.scope["planned_accounts"] == ["lx", "sy"]
    assert contract.public_payload()["selected_recipe"]["name"] == "income_analysis_breakdown"
    assert "driver_or_breakdown" in contract.public_payload()["selected_recipe"]["evidence_needs"]


def test_analyze_task_contract_requires_breakdown_when_only_summary_view_is_covered() -> None:
    plan = _plan_payload(
        goal="分析 2026-06 收益来源",
        task_contract={
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "domain": "income",
            "task_mode": "analyze",
            "requested_effect": "read",
            "scope": {"months": ["2026-06"]},
            "required_answer": ["summary", "main_drivers", "source_and_policy"],
            "required_evidence": ["summary", "driver_or_breakdown", "source_policy"],
            "answer_shape": ["conclusion", "drivers", "source_policy"],
        },
        tool_name="analysis_query",
        arguments={
            "sql": "select month, account, net_income_cny from account_monthly_performance where month = '2026-06'",
            "limit": 20,
        },
        purpose="读取收益汇总",
    )
    contract = build_task_contract(
        question="6月收益分析",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="6月收益分析",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
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

    assert coverage["status"] == "recoverable_gap"
    assert coverage["gaps"][0]["kind"] == "analysis_breakdown_needed"
    assert coverage["gaps"][0]["suggested_views"] == ["account_monthly_income_components", "symbol_income_attribution"]


def test_selected_recipe_requires_income_breakdown_when_task_contract_is_weak() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "查看 2026-06 指标",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "查看 2026-06 指标",
            "domain": "income",
            "task_mode": "analyze",
            "requested_effect": "read",
            "scope": {"months": ["2026-06"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["summary", "source_policy"],
            "answer_shape": ["conclusion", "source_policy"],
        },
        "selected_recipe": _recipe_payload("income_analysis_breakdown"),
        "required_capabilities": ["analysis_query", "read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": "select month, account, net_income_cny from account_monthly_performance where month = '2026-06'",
                    "limit": 20,
                },
                "purpose": "读取收益汇总",
            }
        ],
    }
    contract = build_task_contract(
        question="看一下这个月",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="看一下这个月",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
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

    assert contract.required_evidence == ("summary", "source_policy")
    assert coverage["status"] == "recoverable_gap"
    assert coverage["gaps"][0]["kind"] == "analysis_breakdown_needed"


def test_selected_recipe_operation_readback_gap_uses_planner_scope_operation_id() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "检查操作状态和回执",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "检查操作状态和回执",
            "domain": "operation",
            "task_mode": "diagnose",
            "requested_effect": "read",
            "scope": {"operation_ids": ["op_recipe_1"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["observed_status"],
            "answer_shape": ["observation", "evidence_boundary"],
        },
        "selected_recipe": _recipe_payload("operation_status_readback"),
        "required_capabilities": ["healthcheck", "read_only"],
        "steps": [{"id": "step_1", "tool_name": "healthcheck", "arguments": {}, "purpose": "读取系统状态"}],
    }
    contract = build_task_contract(
        question="检查这次操作状态",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(question="检查这次操作状态", plan=plan, observations=[])

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    gaps = {item["kind"]: item for item in coverage["gaps"]}

    assert contract.scope["operation_ids"] == ["op_recipe_1"]
    assert coverage["status"] == "recoverable_gap"
    assert gaps["recipe_operation_readback_missing"]["suggested_tool"] == "operation_timeline"
    assert gaps["recipe_operation_readback_missing"]["suggested_arguments"] == {"limit": 5, "operation_id": "op_recipe_1"}
    assert gaps["recipe_receipt_status_missing"]["suggested_tool"] == "operation_timeline"


def test_selected_recipe_strategy_replay_gap_is_recoverable_by_read_surface() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "评估 NVDA covered call 策略是否要调整",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "评估 NVDA covered call 策略是否要调整",
            "domain": "strategy",
            "task_mode": "recommend",
            "requested_effect": "read",
            "scope": {"symbols": ["NVDA"], "config_keys": ["us"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["current_state", "risk_premise", "dry_run_or_replay"],
            "answer_shape": ["judgement", "risk", "premise"],
        },
        "selected_recipe": _recipe_payload("strategy_replay_review"),
        "required_capabilities": ["analysis_query", "read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": "select symbol, account, reason from candidate_filter_diagnostics where symbol = 'NVDA'",
                    "limit": 20,
                },
                "purpose": "读取候选风险前提",
            }
        ],
    }
    contract = build_task_contract(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {
                    "columns": ["symbol", "account", "reason"],
                    "rows": [{"symbol": "NVDA", "account": "lx", "reason": "delta risk above target"}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["candidate_filter_diagnostics"],
                            "months": [],
                            "accounts": ["lx"],
                            "symbols": ["NVDA"],
                        }
                    },
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    gaps = {item["kind"]: item for item in coverage["gaps"]}

    assert coverage["status"] == "recoverable_gap"
    assert gaps["recipe_strategy_replay_evidence_missing"]["recoverable"] is True
    assert gaps["recipe_strategy_replay_evidence_missing"]["recoverable_by"] == "analysis_query"
    assert gaps["recipe_strategy_replay_evidence_missing"]["suggested_tool"] == "analysis_query"
    assert gaps["recipe_strategy_replay_evidence_missing"]["suggested_views"] == ["strategy_replay_read_surface"]


def test_selected_recipe_strategy_replay_gap_ignores_tool_name_without_read_surface() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "评估 NVDA covered call 策略是否要调整",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "评估 NVDA covered call 策略是否要调整",
            "domain": "strategy",
            "task_mode": "recommend",
            "requested_effect": "read",
            "scope": {"symbols": ["NVDA"], "config_keys": ["us"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["dry_run_or_replay"],
            "answer_shape": ["judgement", "risk", "premise"],
        },
        "selected_recipe": _recipe_payload("strategy_replay_review"),
        "required_capabilities": ["read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "strategy_replay_analyze",
                "arguments": {"symbol": "NVDA", "dry_run": True},
                "purpose": "尝试读取 replay 证据",
            }
        ],
    }
    contract = build_task_contract(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "strategy_replay_analyze",
                "payload": _plan_step_arguments(plan),
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "strategy_replay_analyze.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "strategy replay analyzer",
                    "guard_profile": "analysis_rows",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": [],
                },
                "data": {"rows": [], "row_count": 0},
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    gap_kinds = {item["kind"] for item in coverage["gaps"]}

    assert "recipe_strategy_replay_evidence_missing" in gap_kinds


def test_selected_recipe_strategy_replay_gap_requires_read_surface_evidence_columns() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "评估 NVDA covered call 策略是否要调整",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "评估 NVDA covered call 策略是否要调整",
            "domain": "strategy",
            "task_mode": "recommend",
            "requested_effect": "read",
            "scope": {"symbols": ["NVDA"], "config_keys": ["us"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["dry_run_or_replay"],
            "answer_shape": ["judgement", "risk", "premise"],
        },
        "selected_recipe": _recipe_payload("strategy_replay_review"),
        "required_capabilities": ["analysis_query", "read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": "select count(*) as row_count from strategy_replay_read_surface",
                    "limit": 20,
                },
                "purpose": "检查 replay 读面是否存在",
            }
        ],
    }
    contract = build_task_contract(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
                    "columns": ["row_count"],
                    "rows": [{"row_count": 42}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {"views": ["strategy_replay_read_surface"]},
                        "diagnostics": [
                            {
                                "view": "strategy_replay_read_surface",
                                "status": "observed_strategy_replay_surface",
                                "severity": "info",
                                "summary": "strategy replay read-surface rows were observed",
                                "answer_boundary": "offline_replay_or_dry_run_evidence_only",
                            }
                        ],
                    },
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    gap_kinds = {item["kind"] for item in coverage["gaps"]}

    assert "recipe_strategy_replay_evidence_missing" in gap_kinds


def test_selected_recipe_strategy_replay_gap_is_satisfied_by_read_surface() -> None:
    plan = {
        "schema_version": TOOL_PLAN_SCHEMA_VERSION,
        "goal": "评估 NVDA covered call 策略是否要调整",
        "task_contract": {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "goal": "评估 NVDA covered call 策略是否要调整",
            "domain": "strategy",
            "task_mode": "recommend",
            "requested_effect": "read",
            "scope": {"symbols": ["NVDA"], "config_keys": ["us"]},
            "required_answer": ["summary", "source_and_policy"],
            "required_evidence": ["current_state", "risk_premise", "dry_run_or_replay"],
            "answer_shape": ["judgement", "risk", "premise"],
        },
        "selected_recipe": _recipe_payload("strategy_replay_review"),
        "required_capabilities": ["analysis_query", "read_only"],
        "steps": [
            {
                "id": "step_1",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": (
                        "select symbol, account, reason from candidate_filter_diagnostics where symbol = 'NVDA'"
                    ),
                    "limit": 20,
                },
                "purpose": "读取候选风险前提",
            },
            {
                "id": "step_2",
                "tool_name": "analysis_query",
                "arguments": {
                    "sql": (
                        "select artifact_kind, data_mode, status "
                        "from strategy_replay_read_surface where data_mode = 'closed_replay'"
                    ),
                    "limit": 20,
                },
                "purpose": "读取离线 replay / dry-run 证据",
            },
        ],
    }
    contract = build_task_contract(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="NVDA covered call 要不要调整？",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
                    "columns": ["symbol", "account", "reason"],
                    "rows": [{"symbol": "NVDA", "account": "lx", "reason": "delta risk above target"}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["candidate_filter_diagnostics"],
                            "accounts": ["lx"],
                            "symbols": ["NVDA"],
                        }
                    },
                },
            },
            {
                "index": 2,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan, index=1),
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
                    "columns": ["artifact_kind", "data_mode", "status"],
                    "rows": [
                        {
                            "artifact_kind": "shadow_replay_candidate_impact",
                            "data_mode": "closed_replay",
                            "status": "ready_for_live_shadow_review",
                        }
                    ],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["strategy_replay_read_surface"],
                            "accounts": ["lx"],
                            "symbols": ["NVDA"],
                        },
                        "diagnostics": [
                            {
                                "view": "strategy_replay_read_surface",
                                "status": "observed_strategy_replay_evidence",
                                "severity": "info",
                                "summary": "strategy replay read-surface rows were observed",
                                "answer_boundary": "offline_replay_or_dry_run_evidence_only",
                            }
                        ],
                    },
                },
            },
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()
    gap_kinds = {item["kind"] for item in coverage["gaps"]}

    assert "recipe_strategy_replay_evidence_missing" not in gap_kinds


def test_income_analysis_contract_accepts_monthly_detail_rows_as_breakdown_evidence() -> None:
    plan = _plan_payload(
        goal="分析 2026-06 收益",
        task_contract=_test_task_contract(
            goal="分析 2026-06 收益",
            domain="income",
            task_mode="analyze",
            scope={"months": ["2026-06"]},
            required_answer=("summary", "main_drivers", "source_and_policy"),
            required_evidence=("summary", "driver_or_breakdown", "source_policy"),
            answer_shape=("conclusion", "drivers", "source_policy"),
            intent_families=("breakdown",),
        ),
        tool_name="monthly_income_report",
        arguments={"month": "2026-06", "include_rows": True},
        purpose="读取收益分析明细",
    )
    contract = build_task_contract(
        question="6月收益分析",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 17),
    )
    bundle = build_evidence_bundle(
        question="6月收益分析",
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "monthly_income_report",
                "payload": {"config_key": "us", "month": "2026-06", "include_rows": True},
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
                    "filters": {"month": "2026-06"},
                    "summary": [{"month": "2026-06", "account": "sy", "currency": "HKD", "net_cashflow_gross": 11200.0}],
                    "cashflow_rows": [
                        {
                            "month": "2026-06",
                            "account": "sy",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "net_cashflow_gross": 11200.0,
                        }
                    ],
                    "row_count": 1,
                    "cashflow_row_count": 1,
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()

    assert contract.domain == "income"
    assert contract.task_mode == "analyze"
    assert "driver_or_breakdown" in contract.required_evidence
    assert "drivers" in contract.answer_shape
    assert coverage["status"] == "complete"
    assert coverage["gaps"] == []


def test_answer_shape_requires_drivers_for_analysis_task() -> None:
    task_contract = {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "domain": "income",
        "task_mode": "analyze",
        "requested_effect": "read",
        "intent_families": ["general_analysis"],
        "required_answer": ["summary", "source_and_policy"],
        "answer_shape": ["conclusion", "drivers", "source_policy"],
        "scope": {"requested_months": ["2026-06"]},
    }

    weak = verify_response_shape(
        "2026-06 净收入 CNY 14,139，收益率 1.79%。",
        task_contract=task_contract,
        coverage={"status": "complete", "missing": [], "gaps": []},
    ).public_payload()
    strong = verify_response_shape(
        "2026-06 收益主要来自 sy 的已实现 PnL 和权利金贡献，lx 贡献较小。",
        task_contract=task_contract,
        coverage={"status": "complete", "missing": [], "gaps": []},
    ).public_payload()

    assert weak["violations"][0]["required_answer_key"] == "drivers"
    assert strong["violations"] == []


def test_task_contract_does_not_treat_month_digits_as_symbols() -> None:
    plan = _plan_payload(
        goal="对比 lx 和 sy 2026-05 的账户收益",
        task_contract=_test_task_contract(
            goal="对比 lx 和 sy 2026-05 的账户收益",
            domain="income",
            task_mode="compare",
            scope={"accounts": ["lx", "sy"], "months": ["2026-05"]},
            required_answer=("summary", "comparison_winner", "amount_difference", "source_and_policy"),
            required_evidence=("same_scope_comparable_data", "source_policy"),
            answer_shape=("conclusion", "same_scope_comparison", "difference", "source_policy"),
            intent_families=("account_comparison",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": "SELECT month, account, net_income_cny FROM account_monthly_performance WHERE month = '2026-05'",
            "limit": 20,
        },
        purpose="读取 2026-05 账户收益",
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 2026-05 的账户收益",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.scope["requested_months"] == ["2026-05"]
    assert contract.scope["planned_months"] == ["2026-05"]
    assert contract.scope["requested_symbols"] == []
    assert contract.scope["planned_symbols"] == []


def test_task_contract_normalizes_user_requested_symbol_aliases() -> None:
    plan = _plan_payload(
        goal="诊断泡泡玛特 sell_put 过滤原因",
        task_contract=_test_task_contract(
            goal="诊断泡泡玛特 sell_put 过滤原因",
            domain="candidate",
            task_mode="diagnose",
            scope={"accounts": ["lx"], "symbols": ["9992.HK"]},
            required_answer=("summary", "root_cause", "source_and_policy"),
            required_evidence=("observed_status", "diagnostic_evidence", "source_policy"),
            answer_shape=("observation", "cause_chain", "evidence_boundary", "next_step"),
            intent_families=("candidate_filter_diagnostic",),
        ),
        tool_name="candidate_filter_explain",
        arguments={"symbol": "泡泡玛特", "account": "lx", "function": "sell_put"},
        purpose="读取候选过滤 trace",
    )
    contract = build_task_contract(
        question="lx 泡泡玛特 sell_put 被哪个参数过滤了？",
        plan=plan,
        request_context={"config_key": "hk"},
        today=date(2026, 6, 14),
    )

    assert contract.scope["requested_accounts"] == ["lx"]
    assert contract.scope["requested_symbols"] == ["9992.HK"]
    assert contract.scope["planned_symbols"] == ["9992.HK"]


def test_task_contract_does_not_treat_lowercase_words_as_symbols() -> None:
    plan = _plan_payload(
        goal="diagnose candidate trace risk reason",
        task_contract=_test_task_contract(
            goal="diagnose candidate trace risk reason",
            domain="candidate",
            task_mode="diagnose",
            scope={"symbols": ["FUTU"]},
            required_answer=("summary", "root_cause", "source_and_policy"),
            required_evidence=("observed_status", "diagnostic_evidence", "source_policy"),
            answer_shape=("observation", "cause_chain", "evidence_boundary", "next_step"),
            intent_families=("candidate_filter_diagnostic",),
        ),
        tool_name="candidate_filter_explain",
        arguments={"symbol": "FUTU", "account": "lx"},
        purpose="读取候选过滤 trace",
    )
    contract = build_task_contract(
        question="diagnose candidate trace risk reason",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.scope["requested_symbols"] == []
    assert contract.scope["planned_symbols"] == ["FUTU"]


def test_task_contract_matches_lowercase_user_symbol_to_planned_symbol() -> None:
    plan = _plan_payload(
        goal="设置 tigr covered call min strike 6.5",
        task_contract=_test_task_contract(
            goal="设置 tigr covered call min strike 6.5",
            domain="config",
            task_mode="preview_write",
            requested_effect="preview_write",
            scope={"symbols": ["TIGR"]},
            required_answer=("preview_receipt", "source_and_policy"),
            required_evidence=("permission_request", "preview_receipt", "source_policy"),
            answer_shape=("preview_summary", "risk", "confirmation_handle"),
        ),
        tool_name="symbol_edit",
        arguments={"symbol": "TIGR", "set": {"sell_call.min_strike": 6.5}},
        purpose="预览监控标的配置修改",
    )
    contract = build_task_contract(
        question="设置 tigr covered call min strike 6.5",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.requested_effect == "preview_write"
    assert contract.scope["requested_symbols"] == ["TIGR"]
    assert contract.scope["planned_symbols"] == ["TIGR"]


def test_task_contract_keeps_planner_requested_scope_out_of_user_requested_scope() -> None:
    plan = _plan_payload(
        goal="诊断 FUTU 过滤原因",
        tool_name="candidate_filter_explain",
        arguments={"symbol": "TSLA", "account": "lx"},
        purpose="读取候选过滤 trace",
        task_contract={
            "scope": {
                "requested_symbols": ["TSLA"],
                "requested_accounts": ["sy"],
            },
        },
    )
    contract = build_task_contract(
        question="lx FUTU 为什么没进候选？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.scope["requested_accounts"] == ["lx"]
    assert contract.scope["requested_symbols"] == ["FUTU"]
    assert contract.scope["planned_accounts"] == ["lx", "sy"]
    assert contract.scope["planned_symbols"] == ["FUTU", "TSLA"]


def test_coverage_rejects_account_comparison_without_same_period_metric() -> None:
    plan = _plan_payload(
        goal="对比 lx 和 sy 的账户收益",
        task_contract=_test_task_contract(
            goal="对比 lx 和 sy 的账户收益",
            domain="income",
            task_mode="compare",
            scope={"accounts": ["lx", "sy"]},
            required_answer=("summary", "comparison_winner", "amount_difference", "source_and_policy"),
            required_evidence=("same_scope_comparable_data", "source_policy"),
            answer_shape=("conclusion", "same_scope_comparison", "difference", "source_policy"),
            intent_families=("account_comparison",),
        ),
        tool_name="analysis_query",
        arguments={
            "sql": (
                "select month, account, net_income_cny "
                "from account_monthly_performance where account in ('lx','sy')"
            ),
            "limit": 20,
        },
        purpose="读取 lx/sy 账户收益",
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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
    plan = _plan_payload(
        goal="对比 lx 和 sy 的账户收益",
        task_contract=_test_task_contract(
            goal="对比 lx 和 sy 的账户收益",
            domain="income",
            task_mode="compare",
            scope={"accounts": ["lx", "sy"]},
            required_answer=("summary", "comparison_winner", "amount_difference", "source_and_policy"),
            required_evidence=("same_scope_comparable_data", "source_policy"),
            answer_shape=("conclusion", "same_scope_comparison", "difference", "source_policy"),
            intent_families=("account_comparison",),
        ),
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
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan=plan,
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )
    bundle = build_evidence_bundle(
        question=contract.question,
        plan=plan,
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": _plan_step_arguments(plan),
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


def test_task_contract_keeps_upgrade_why_out_of_income_breakdown() -> None:
    contract = build_task_contract(
        question="为什么升级 in_456 当前版本和目标版本是空的？也没有成功回执？",
        plan={
            "goal": "检查升级版本和回执",
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "operation_timeline",
                    "arguments": {"operation_types": ["upgrade_now"], "operation_id": "in_456"},
                    "purpose": "读取升级 operation timeline",
                }
            ],
        },
        request_context={"config_key": "us"},
        today=date(2026, 6, 15),
    )

    assert contract.intent_families == ("upgrade_status",)
    assert "current_version" in contract.required_answer
    assert "target_version" in contract.required_answer
    assert "main_drivers" not in contract.required_answer


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


def test_contract_verifier_rejects_unsupported_high_risk_claims() -> None:
    bundle = build_evidence_bundle(
        question="解释泡泡玛特候选过滤和合约信息",
        plan={"goal": "解释泡泡玛特候选过滤和合约信息", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "monthly_income_report",
                "payload": {"account": "lx", "month": "2026-06"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "monthly_income_report.output.v1",
                    "canonical_renderer": "monthly_income_report",
                    "source_label": "OM monthly income report",
                    "guard_profile": "cashflow_rows",
                    "primary_rows": "cashflow_rows",
                    "row_count_field": "row_count",
                    "fact_fields": [
                        "cashflow_rows[].symbol",
                        "cashflow_rows[].currency",
                        "cashflow_rows[].contracts",
                        "cashflow_rows[].net_cashflow_gross",
                        "cashflow_rows[].expiration_ymd",
                    ],
                },
                "data": {
                    "cashflow_rows": [
                        {
                            "symbol": "9992.HK",
                            "currency": "HKD",
                            "contracts": 1,
                            "net_cashflow_gross": 1200.0,
                            "expiration_ymd": "2026-06-18",
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    bad = verify_response_against_evidence(
        "NVDA 的收入是 HKD 9,999，有 3 张合约，日期是 2026-06-19。",
        evidence_bundle=bundle,
    )
    violation_types = {item["type"] for item in bad.violations}
    assert {
        "unsupported_contract_symbol",
        "unsupported_contract_currency_amount",
        "unsupported_contract_quantity",
        "unsupported_contract_date",
    } <= violation_types


def test_assistant_trace_preserves_blocker_tool_name_in_json_without_text_leak(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    request = AssistantRequest(
        text="检查失败工具",
        sender_id="local",
        message_id="msg_failed_tool_trace",
        audit_db=str(audit_db),
    )
    snapshot = {
        "schema_version": "om-agent-session-v1",
        "session_id": "as_failed_tool_trace",
        "request": request.public_payload(),
        "goal": "检查失败工具",
        "task_state": "failed",
        "capability_selection": {},
        "progress": {
            "schema_version": "om-agent-progress-v1",
            "state": "failed",
            "summary": "执行失败，需要查看阻塞项",
            "tool_call_count": 1,
            "completed_step_count": 0,
            "failed_step_count": 1,
            "denied_step_count": 0,
            "coverage_status": "not_applicable",
            "next_action": "inspect_tool_failure",
            "blocked_by": [
                {
                    "kind": "tool_failure",
                    "tool_name": "analysis_query",
                    "code": "TOOL_FAILED",
                }
            ],
        },
        "plan_revisions": [],
        "tool_transcript": [],
        "task_contract": {},
        "evidence_bundle": {},
        "coverage": {},
        "permission_state": {},
        "answer_trace": {
            "final_response": {"status": "failed", "reason": "tool failed"},
            "synthesis": {},
            "followup_decisions": [],
        },
        "audit_ref": {},
    }
    AgentSessionStore(audit_db).upsert_snapshot(
        snapshot=snapshot,
        command_id="cmd_failed_tool_trace",
        request=request,
        response={"data": {"response_text": "工具失败"}},
    )

    trace = collect_assistant_trace(audit_db=str(audit_db), command_id="cmd_failed_tool_trace")
    blocker = trace["traces"][0]["progress"]["blocked_by"][0]

    assert blocker["kind"] == "tool_failure"
    assert blocker["tool_name"] == "analysis_query"
    assert "blocked_by=tool_failure" in trace["response_text"]
    assert "analysis_query" not in trace["response_text"]


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


def test_format_assistant_trace_route_samples_from_fixture() -> None:
    cases = _load_trace_route_cases()
    assert len(cases) >= 5
    ids = {str(case.get("id") or "") for case in cases}
    assert sorted(REQUIRED_TRACE_ROUTE_SAMPLE_IDS - ids) == []
    for case in cases:
        trace = dict(case["trace"])
        identity = trace.get("identity") if isinstance(trace.get("identity"), dict) else {}
        text = format_assistant_trace(
            [trace],
            filters={"command_id": identity.get("command_id") or case["id"], "limit": 10},
            warnings=[],
        )
        for expected in case.get("expect_contains") or ():
            assert str(expected) in text, case["id"]
        for unexpected in case.get("expect_not_contains") or ():
            assert str(unexpected) not in text, case["id"]
        _assert_no_internal_trace_leak(text, case_id=str(case["id"]))


def test_assistant_trace_route_samples_satisfy_online_sample_contract() -> None:
    cases = _load_trace_route_cases()
    failures: dict[str, list[str]] = {}
    for case in cases:
        case_id = str(case.get("id") or "")
        missing: list[str] = []
        if not re.match(r"^trace_(?:ask|preview|rewrite|fallback|denied|pass)_[a-z0-9_]+$", case_id):
            missing.append("trace id naming")
        if not isinstance(case.get("trace"), dict):
            missing.append("trace")
        trace = case.get("trace") if isinstance(case.get("trace"), dict) else {}
        task = trace.get("task") if isinstance(trace.get("task"), dict) else {}
        answer = trace.get("answer") if isinstance(trace.get("answer"), dict) else {}
        if not str(task.get("goal") or "").strip():
            missing.append("task goal")
        if not str(answer.get("response_status") or "").strip():
            missing.append("answer response_status")
        if not case.get("expect_contains"):
            missing.append("expect_contains")
        if not case.get("expect_not_contains"):
            missing.append("expect_not_contains")
        if not any("最终：" in str(item) for item in case.get("expect_contains") or ()):
            missing.append("final route assertion")
        if missing:
            failures[case_id] = missing
            continue
        try:
            _assert_trace_fixture_sensitive_values_forbidden(case)
        except AssertionError as exc:
            failures[case_id] = [str(exc)]

    assert failures == {}


def test_assistant_trace_fixture_covers_documented_p2_minimum_cases() -> None:
    cases = {str(item["id"]): item for item in _load_trace_route_cases()}
    missing: dict[str, list[str]] = {}
    for case_name, fixture_ids in P2_TRACE_ROUTE_MINIMUM_CASES.items():
        absent = sorted(fixture_ids - set(cases))
        if absent:
            missing[case_name] = absent
    assert missing == {}

    required_ids = {case_id for group in P2_TRACE_ROUTE_MINIMUM_CASES.values() for case_id in group}
    for case_id in required_ids:
        case = cases[case_id]
        assert case.get("expect_contains")
        assert case.get("expect_not_contains")


def test_assistant_trace_minimum_case_mapping_matches_design_document() -> None:
    documented_cases = {case for case in _load_documented_p2_minimum_case_names() if case.startswith("trace_")}
    assert set(P2_TRACE_ROUTE_MINIMUM_CASES) == documented_cases


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
        return _event_plan_result(
            goal="查看 lx 指派正股持仓盈亏",
            tool_name="option_positions_read",
            arguments={"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True},
            purpose="读取指派正股持仓盈亏",
            task_contract=_assigned_stock_pnl_task_contract(),
        )

    settings = AssistantSettings(
        llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
    )
    first = handle_assistant_message(
        AssistantRequest(text="查看 lx 指派正股持仓盈亏", sender_id="local", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute,
        settings=settings,
        plan_tools_fn=_plan,
    )
    second = handle_assistant_message(
        AssistantRequest(text="查看 lx 指派正股持仓盈亏", sender_id="local", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute,
        settings=settings,
        plan_tools_fn=_plan,
    )

    assert first["data"]["command_id"] != second["data"]["command_id"]
    rows = AgentSessionStore(audit_db).list_recent(limit=10)
    assert len(rows) == 2
    assert {row["command_id"] for row in rows} == {first["data"]["command_id"], second["data"]["command_id"]}
    assert len({row["session_id"] for row in rows}) == 2
