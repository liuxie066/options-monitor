from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantRequest, AssistantSettings, AssistantLlmSettings, handle_assistant_message
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
    plan = PlannerPlan(
        goal="检查远端 release 发布状态",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": (
                        "select command_id, operation_status, current_version, target_version, release_tag, "
                        "receipt_status from upgrade_operation_status where release_tag = 'v1.2.273'"
                    )
                },
                purpose="读取升级操作中的 release tag 和回执状态",
            ),
        ),
    )
    contract = build_task_contract(
        question="v1.2.273 远端 release 发布成功了吗？",
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
    plan = PlannerPlan(
        goal="检查远端 release 发布状态",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": (
                        "select current_version, target_version, operation_status, release_tag, release_status, "
                        "release_published_at, github_release_url from upgrade_operation_status where release_tag = 'v1.2.273'"
                    )
                },
                purpose="读取远端 release 发布证据",
            ),
        ),
    )
    contract = build_task_contract(
        question="v1.2.273 远端 release 发布成功了吗？",
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
    plan = PlannerPlan(
        goal="检查远端 release 发布状态冲突",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": (
                        "select current_version, target_version, release_tag, release_status, outcome_status "
                        "from upgrade_operation_status where release_tag = 'v1.2.277'"
                    )
                },
                purpose="读取远端 release 与升级结果状态",
            ),
        ),
    )
    contract = build_task_contract(
        question="v1.2.277 远端 release 发布成功了吗？",
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
    plan = PlannerPlan(
        goal="检查升级版本和回执",
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
    plan = PlannerPlan(
        goal="对比 lx 和 sy 的账户收益",
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


def test_task_contract_does_not_treat_month_digits_as_symbols() -> None:
    plan = PlannerPlan(
        goal="对比 lx 和 sy 2026-05 的账户收益",
        steps=(
            PlannerPlanStep(
                id="step_1",
                tool_name="analysis_query",
                arguments={
                    "sql": "SELECT month, account, net_income_cny FROM account_monthly_performance WHERE month = '2026-05'",
                    "limit": 20,
                },
                purpose="读取 2026-05 账户收益",
            ),
        ),
    )
    contract = build_task_contract(
        question="对比 lx 和 sy 2026-05 的账户收益",
        plan=plan.public_payload(),
        request_context={"config_key": "us"},
        today=date(2026, 6, 14),
    )

    assert contract.scope["requested_months"] == ["2026-05"]
    assert contract.scope["planned_months"] == ["2026-05"]
    assert contract.scope["requested_symbols"] == []
    assert contract.scope["planned_symbols"] == []


def test_coverage_rejects_account_comparison_without_same_period_metric() -> None:
    plan = PlannerPlan(
        goal="对比 lx 和 sy 的账户收益",
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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
    assert any(item["hook"] == "action_safety" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    assert any(item["hook"] == "output_contract" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    assert any(item["hook"] == "evidence_contract" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    assert any(item["hook"] == "result_status" and item["status"] == "pass" for item in session["tool_transcript"][0]["hook_results"])
    hook_text = json.dumps(session["tool_transcript"][0]["hook_results"], ensure_ascii=False)
    for unexpected in ("NVDA", "assigned-stock-assign_1", "stock_lot_id", "assigned_stock_unrealized_pnl"):
        assert unexpected not in hook_text
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
    persisted_snapshot_text = json.dumps(persisted[0]["snapshot"], ensure_ascii=False)
    for unexpected in ("assigned-stock-assign_1", "stock_lot_id", "assigned_stock_unrealized_pnl"):
        assert unexpected not in persisted_snapshot_text

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
    trace_entry_text = json.dumps(trace_entry, ensure_ascii=False)
    for unexpected in ("assigned-stock-assign_1", "stock_lot_id", "assigned_stock_unrealized_pnl"):
        assert unexpected not in trace_entry_text

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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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


def test_agent_loop_stops_after_one_followup_for_same_quote_gap(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    planner_followup_contexts: list[dict[str, Any]] = []

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
                        "stock_lot_id": "assigned-stock-assign_1",
                        "account": "lx",
                        "symbol": "NVDA",
                        "currency": "USD",
                        "status": "open",
                        "shares_remaining": 100,
                        "stock_cost_per_share": 100,
                        "remaining_stock_cost_basis": 10000,
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
            arguments: dict[str, Any] = {
                "action": "assigned-stock",
                "account": "lx",
                "status": "open",
                "refresh_quotes": True,
            }
            if len(planner_followup_contexts) > 1:
                arguments["symbol"] = "NVDA"
            return LlmPlannerResult(
                plan=PlannerPlan(
                    goal="查看 lx 指派正股持仓盈亏",
                    required_capabilities=("assigned_stock_positions", "read_only"),
                    steps=(
                        PlannerPlanStep(
                            id=f"step_followup_{len(planner_followup_contexts)}",
                            tool_name="option_positions_read",
                            arguments=arguments,
                            purpose="补查指派正股实时行情",
                        ),
                    ),
                ),
                trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
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
            trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="查看 lx 指派正股持仓盈亏",
            sender_id="local",
            message_id="msg_agent_quote_gap_attempted_once",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
    )

    assert out["ok"] is True
    assert calls == [
        ("option_positions_read", {"action": "assigned-stock", "account": "lx", "status": "open", "config_key": "us"}),
        (
            "option_positions_read",
            {"action": "assigned-stock", "account": "lx", "status": "open", "refresh_quotes": True, "config_key": "us"},
        ),
    ]
    assert len(planner_followup_contexts) == 1
    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert len(tool_plan_data["plan_revisions"]) == 2
    assert [item["status"] for item in tool_plan_data["followup_decisions"]] == ["accepted", "stopped"]
    assert (
        tool_plan_data["followup_decisions"][1]["reason"]
        == "recoverable evidence gap already attempted once for this scope"
    )
    assert any(
        event["phase"] == "replan" and event["status"] == "gap_already_attempted"
        for event in tool_plan_data["tool_events"]
    )
    assert tool_plan_data["evidence_gaps"][0]["kind"] == "recoverable_missing_quote"
    assert "quote=missing_quote" in out["data"]["response_text"]
    assert "正股浮盈亏 -" in out["data"]["response_text"]
    assert "生命周期PnL -" in out["data"]["response_text"]
    assert "缺口：缺少实时行情：NVDA，不能计算当前正股浮盈亏和生命周期PnL。" in out["data"]["response_text"]


def test_agent_loop_replans_operation_timeline_for_recoverable_upgrade_gap(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    planner_followup_contexts: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        if tool_name == "operation_timeline":
            return build_response(
                tool_name=tool_name,
                ok=True,
                data={
                    "schema_version": "operation-timeline-v1",
                    "filters": {"operation_types": ["upgrade_now"], "operation_id": "in_456"},
                    "timeline_count": 1,
                    "timelines": [
                        {
                            "identity": {"command_id": "in_456", "operation_id": "in_456"},
                            "operation": {
                                "operation_id": "in_456",
                                "command_id": "in_456",
                                "operation_type": "upgrade_now",
                                "status": "applied",
                                "current_version": "1.2.279",
                                "target_version": "1.2.280",
                            },
                            "receipt": {"status": "observed"},
                            "outcome": {"status": "applied", "ok": True},
                            "warnings": [],
                        }
                    ],
                    "warnings": [],
                },
            )
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"status": "ok", "summary": {"latest_status": "ok"}},
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
                    goal="补查升级版本和回执",
                    required_capabilities=("operation_timeline", "read_only"),
                    steps=(
                        PlannerPlanStep(
                            id="step_2",
                            tool_name="operation_timeline",
                            arguments={"operation_types": ["upgrade_now"], "operation_id": "in_456", "limit": 5},
                            purpose="补查升级 operation timeline",
                        ),
                    ),
                ),
                trace={"schema_version": TOOL_PLAN_SCHEMA_VERSION, "attempted": True, "reason": "followup"},
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="检查升级版本和回执",
                required_capabilities=("healthcheck", "read_only"),
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="healthcheck",
                        arguments={},
                        purpose="先读取系统健康状态",
                    ),
                ),
            ),
            trace={"schema_version": TOOL_PLAN_SCHEMA_VERSION, "attempted": True, "reason": "initial"},
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        operation_observation = next(item for item in observations if item["tool_name"] == "operation_timeline")
        assert operation_observation["ok"] is True
        return LlmSynthesisResult(
            response_text="升级记录显示状态已应用，当前版本 1.2.279，目标版本 1.2.280，最终回执已观测到。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="为什么升级 in_456 当前版本和目标版本是空的？也没有成功回执？",
            sender_id="local",
            message_id="msg_agent_upgrade_recoverable_followup",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    assert calls[0] == ("healthcheck", {"config_key": "us"})
    assert calls[1][0] == "operation_timeline"
    assert calls[1][1]["operation_types"] == ["upgrade_now"]
    assert calls[1][1]["operation_id"] == "in_456"
    assert calls[1][1]["limit"] == 5
    assert calls[1][1]["audit_db"] == str(tmp_path / "inbound.sqlite3")
    assert len(calls) == 2
    assert planner_followup_contexts
    followup = planner_followup_contexts[0]
    assert {gap["kind"] for gap in followup["evidence_gaps"]} >= {
        "upgrade_current_version_missing",
        "upgrade_target_version_missing",
    }
    assert followup["decision_contract"]["schema_version"] == "om-agent-loop-followup-decision-v1"
    assert "operation_timeline" in followup["decision_contract"]["allowed_tools"]

    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert len(tool_plan_data["plan_revisions"]) == 2
    assert tool_plan_data["tool_calls_used"] == 2
    assert tool_plan_data["coverage"]["status"] == "complete"
    assert tool_plan_data["coverage"]["gaps"] == []
    assert tool_plan_data["evidence_gaps"] == []
    assert len(tool_plan_data["followup_decisions"]) == 1
    assert tool_plan_data["followup_decisions"][0]["status"] == "accepted"
    assert tool_plan_data["followup_decisions"][0]["tool_name"] == "operation_timeline"
    assert "当前版本 1.2.279" in out["data"]["response_text"]
    assert "目标版本 1.2.280" in out["data"]["response_text"]


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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
        llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
            plan=PlannerPlan(goal="查看 lx 指派正股持仓盈亏", steps=steps),
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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
                    steps=(PlannerPlanStep(id="step_2", tool_name="runtime_status", arguments={}, purpose="无关补查"),),
                ),
                trace={"attempted": True, "reason": "accepted", "schema_version": TOOL_PLAN_SCHEMA_VERSION},
            )
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="查看 lx 指派正股持仓盈亏",
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
            llm=AssistantLlmSettings(enabled=True, provider="openai", model="gpt-5.2"),
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
    assert any(
        event["phase"] == "replan" and event["status"] == "tool_not_allowed_for_gap"
        for event in tool_plan_data["tool_events"]
    )
    assert tool_plan_data["followup_decisions"][0]["status"] == "rejected"
    assert "runtime_status" in tool_plan_data["followup_decisions"][0]["reason"]
    assert "not allowed for the recoverable evidence gap" in tool_plan_data["followup_decisions"][0]["reason"]
