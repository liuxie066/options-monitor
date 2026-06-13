from __future__ import annotations

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
    TOOL_PLAN_SCHEMA_VERSION,
)
from src.application.assistant.answer_verifier import verify_response_against_evidence
from src.application.assistant.evidence import EVIDENCE_BUNDLE_SCHEMA_VERSION, build_evidence_bundle
from src.application.assistant.session_store import AgentSessionStore, collect_assistant_trace
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
    assert payload["guard_contracts"][0]["guard_profile"] == "position_rows"


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
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98，正股浮盈亏 USD -200。",
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
    assert session["evidence_bundle"]["fact_count"] == len(evidence["facts"])
    assert session["permission_state"] == {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [],
        "apply_allowed": False,
    }
    assert session["tool_transcript"][0]["tool_name"] == "option_positions_read"
    assert session["tool_transcript"][0]["authorization_reason"] == "pure_read_whitelist"

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
    assert trace_entry["evidence"]["fact_count"] == len(evidence["facts"])
    assert trace_entry["answer"]["response_status"] == "synthesized"

    tool_trace = run_tool("assistant_trace", {"audit_db": str(audit_db), "command_id": out["data"]["command_id"]})
    assert tool_trace["ok"] is True
    assert tool_trace["data"]["trace_count"] == 1
    assert tool_trace["data"]["traces"][0]["identity"]["command_id"] == out["data"]["command_id"]


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
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98，正股浮盈亏 USD -200。",
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
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98。",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        ),
    )
    second = handle_assistant_message(
        AssistantRequest(text="查看 lx 指派正股持仓盈亏", sender_id="local", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute,
        settings=settings,
        plan_tools_fn=_plan,
        synthesize_response_fn=lambda *_args: LlmSynthesisResult(
            response_text="lx 当前 NVDA 指派正股剩余 100 股，spot USD 98。",
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
