from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.agent_loop import (
    _assistant_tool_loop_response_text,
    build_synthesis_observation,
    execute_model_tool_call_event,
    execute_tool_loop_payload,
    run_assistant_tool_event_loop,
)
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.model_events import (
    AssistantEvent,
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    model_tool_call_from_provider_block,
)
from src.application.assistant.settings import AssistantSettings


def test_execute_model_tool_call_event_runs_read_tool_through_guard() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "row_count": 1,
                "rows": [{"account": "lx", "month": "2026-06", "net_income": 123.45}],
            },
        )

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )
    result = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="6月收益分析", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        execute_tool_fn=_execute,
    )

    assert result.allowed is True
    assert result.ok is True
    assert calls == [
        (
            "monthly_income_report",
            {"account": "lx", "month": "2026-06", "include_rows": True, "config_key": "us"},
        )
    ]
    assert result.guard_event.event_type == "tool_guard_decision"
    assert result.guard_event.allowed is True
    assert result.guard_event.risk_class == "READ_AUTO"
    assert result.guard_event.normalized_payload == {
        "account": "lx",
        "month": "2026-06",
        "include_rows": True,
        "config_key": "us",
    }
    provider_result = result.public_payload()["provider_tool_result"]
    assert provider_result["is_error"] is False
    assert provider_result["tool_call_id"] == "call_income_1"
    assert result.result_adapter.raw_result["data"]["row_count"] == 1


def test_execute_model_tool_call_event_projects_contract_preview_to_model_observation() -> None:
    def _execute(tool_name: str, _payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "symbol": "中国海洋石油",
                "canonical_symbol": "0883.HK",
                "found": True,
                "strategies": {
                    "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 90, "max_strike": 20},
                    "sell_call": {"enabled": True, "min_strike": 30},
                },
            },
        )

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_symbol_config",
            "name": "symbol_config_read",
            "arguments": '{"symbol":"中国海洋石油"}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )
    result = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="中国海洋石油 sell put max strike 是多少？", sender_id="u1", config_key="hk"),
        task_contract={"requested_effect": "read", "scope": {"symbols": ["中国海洋石油"]}},
        execute_tool_fn=_execute,
    )

    provider_result = result.public_payload()["provider_tool_result"]

    assert provider_result["is_error"] is False
    assert provider_result["content"]["data_preview"]["canonical_symbol"] == "0883.HK"
    assert provider_result["content"]["data_preview"]["strategies"]["sell_put"]["max_strike"] == 20


def test_execute_model_tool_call_event_blocks_write_tool_before_execution() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_write_1",
            "name": "manage_symbols",
            "arguments": '{"action":"edit","symbol":"NVDA","set":{"sell_put.enabled":false},"confirm":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    result = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="把 NVDA sell put 关掉", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {}},
        execute_tool_fn=lambda tool_name, payload: calls.append((tool_name, payload)) or {},
    )

    assert calls == []
    assert result.allowed is False
    assert result.ok is False
    assert result.guard_event.decision == "not_read_auto"
    assert result.guard_event.error_code == "PERMISSION_DENIED"
    assert result.public_payload()["provider_tool_result"]["is_error"] is True


def test_execute_model_tool_call_event_blocks_scope_violation_before_execution() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"sy","month":"2026-06"}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    result = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="lx 6月收益", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        execute_tool_fn=lambda tool_name, payload: calls.append((tool_name, payload)) or {},
    )

    assert calls == []
    assert result.allowed is False
    assert result.ok is False
    assert result.guard_event.decision == "pre_tool_check_failed"
    assert result.guard_event.error_code == "PRE_TOOL_CHECK_FAILED"
    assert result.guard_event.normalized_payload["account"] == "sy"
    assert result.public_payload()["provider_tool_result"]["is_error"] is True


def test_execute_model_tool_call_event_rejects_duplicate_payload() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"row_count": 1})

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"month":"2026-06"}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )
    attempted_signatures: set[str] = set()

    first = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="6月收益", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {}},
        execute_tool_fn=_execute,
        attempted_signatures=attempted_signatures,
    )
    second = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="6月收益", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {}},
        execute_tool_fn=_execute,
        attempted_signatures=attempted_signatures,
        tool_call_count=1,
    )

    assert first.ok is True
    assert second.allowed is False
    assert second.guard_event.decision == "duplicate_call"
    assert second.guard_event.error_code == "DUPLICATE_TOOL_CALL"
    assert len(calls) == 1


def test_execute_model_tool_call_event_rejects_budget_exhaustion() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"month":"2026-06"}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    result = execute_model_tool_call_event(
        model_event=event,
        request=AssistantRequest(text="6月收益", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {}},
        execute_tool_fn=lambda tool_name, payload: calls.append((tool_name, payload)) or {},
        tool_call_count=5,
        max_tool_calls=5,
    )

    assert calls == []
    assert result.allowed is False
    assert result.guard_event.decision == "tool_budget_exhausted"
    assert result.guard_event.error_code == "TOOL_BUDGET_EXHAUSTED"
    assert result.public_payload()["provider_tool_result"]["is_error"] is True


def test_run_assistant_tool_event_loop_returns_event_outcome_without_planner_plan() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"row_count": 1, "rows": [{"account": "lx", "month": "2026-06", "net_income": 123.45}]},
        )

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    outcome = run_assistant_tool_event_loop(
        question="lx 6月收益分析",
        request=AssistantRequest(text="lx 6月收益分析", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        initial_events=(event,),
        execute_tool_fn=_execute,
    )

    assert outcome.status == "stopped"
    assert outcome.stop_reason == "awaiting_model_continuation"
    assert calls == [
        (
            "monthly_income_report",
            {"account": "lx", "month": "2026-06", "include_rows": True, "config_key": "us"},
        )
    ]
    assert [event["event_type"] for event in outcome.public_payload()["events"]] == [
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "evidence_updated",
    ]
    trace = outcome.public_payload()["trace"]
    assert trace["planner_plan_used"] is False
    assert trace["loop_stop_reason"] == "awaiting_model_continuation"
    assert trace["tool_call_count"] == 1
    assert trace["repair_attempted"] is False
    assert trace["capability_selection"]["selected"][0]["tool_name"] == "monthly_income_report"
    assert outcome.evidence_bundle is not None


def test_run_assistant_tool_event_loop_continues_to_final_answer() -> None:
    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"row_count": 1, "rows": [{"account": "lx", "month": "2026-06", "net_income": 123.45}]},
        )

    def _continue(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "lx 2026-06 已实现收益为 123.45。",
                        }
                    ],
                }
            ]
        }

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    outcome = run_assistant_tool_event_loop(
        question="lx 6月收益分析",
        request=AssistantRequest(text="lx 6月收益分析", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        initial_events=(event,),
        execute_tool_fn=_execute,
        provider="openai",
        create_continuation_response_fn=_continue,
    )

    assert outcome.status == "done"
    assert outcome.stop_reason == "model_final_answer"
    assert outcome.final_answer == "lx 2026-06 已实现收益为 123.45。"
    assert [event["event_type"] for event in outcome.public_payload()["events"]] == [
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "evidence_updated",
        "model_final_answer",
    ]
    trace = outcome.public_payload()["trace"]
    assert trace["answer_verification"]["status"] == "passed"
    assert trace["answer_route"] == "llm_from_tool_observation"
    assert trace["loop_stop_reason"] == "model_final_answer"
    assert trace["scope_source"] == "task_contract"


def test_run_assistant_tool_event_loop_rejects_business_final_answer_without_tool_evidence() -> None:
    event = ModelFinalAnswerEvent(
        event_id="model_final_answer_1",
        answer_text="这是因为成交记录存在歧义，所以未写入。",
        answer_route="llm_direct",
    )

    outcome = run_assistant_tool_event_loop(
        question="FUTU 成交为什么未写入？",
        request=AssistantRequest(text="FUTU 成交为什么未写入？", sender_id="u1", config_key="us"),
        task_contract={
            "requested_effect": "read",
            "domain": "position",
            "task_mode": "diagnose",
            "scope": {"requested_accounts": ["lx"], "requested_symbols": ["FUTU"]},
            "required_evidence": ["observed_status", "diagnostic_evidence"],
        },
        initial_events=(event,),
        execute_tool_fn=lambda _tool_name, _payload: build_response(tool_name="unused", ok=True, data={}),
    )

    assert outcome.status == "stopped"
    assert outcome.stop_reason == "answer_verification_failed"
    assert outcome.final_answer is None
    assert _assistant_tool_loop_response_text(outcome) == "需要先读取相关 OM 证据后才能回答；本次没有执行工具。"

    trace = outcome.public_payload()["trace"]
    assert trace["answer_route"] == "answer_verification_failed"
    assert trace["answer_verification"]["status"] == "failed"
    assert trace["answer_verification"]["trace"]["violation_type"] == "missing_required_tool_evidence"


def test_run_assistant_tool_event_loop_allows_general_final_answer_without_tool_evidence() -> None:
    event = ModelFinalAnswerEvent(
        event_id="model_final_answer_1",
        answer_text="你好，我可以帮你查询 OM 状态或解释最近的记录。",
        answer_route="llm_direct",
    )

    outcome = run_assistant_tool_event_loop(
        question="你好",
        request=AssistantRequest(text="你好", sender_id="u1", config_key="us"),
        task_contract={
            "requested_effect": "read",
            "domain": "general",
            "task_mode": "summarize",
            "scope": {},
            "required_evidence": [],
        },
        initial_events=(event,),
        execute_tool_fn=lambda _tool_name, _payload: build_response(tool_name="unused", ok=True, data={}),
    )

    assert outcome.status == "done"
    assert outcome.stop_reason == "model_final_answer"
    assert outcome.final_answer == "你好，我可以帮你查询 OM 状态或解释最近的记录。"
    assert outcome.public_payload()["trace"]["answer_verification"]["status"] == "passed"


def test_run_assistant_tool_event_loop_skips_internal_catalog_fallback_on_budget_exhaustion() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "view_count": 1,
                "views": {
                    "account_monthly_performance": {
                        "fields": ["month", "account", "net_income_cny"],
                        "recommended_filters": ["month", "account"],
                    }
                },
                "sql_rules": {"allowed_statements": ["SELECT", "WITH"], "writes_allowed": False},
            },
        )

    def _continue(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_analysis_query_1",
                    "name": "analysis_query",
                    "arguments": '{"sql":"select month from account_monthly_performance","limit":20}',
                }
            ]
        }

    event = ModelToolCallEvent(
        event_id="model_tool_call_1",
        tool_call_id="call_analysis_catalog_1",
        tool_name="analysis_catalog",
        arguments={},
        purpose="Inspect analysis catalog before querying fields",
        provider="openai",
    )

    outcome = run_assistant_tool_event_loop(
        question="查看分析目录",
        request=AssistantRequest(text="查看分析目录", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        initial_events=(event,),
        execute_tool_fn=_execute,
        provider="openai",
        create_continuation_response_fn=_continue,
        max_tool_calls=1,
    )

    assert calls == [("analysis_catalog", {"config_key": "us"})]
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "tool_budget_exhausted"
    assert outcome.final_answer is None
    assert _assistant_tool_loop_response_text(outcome) == "已完成工具调用，但当前结果没有可渲染的文本。"


def test_run_assistant_tool_event_loop_recovers_from_scope_denial_once() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    continuation_payloads: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"row_count": 1, "rows": [{"account": "lx", "month": "2026-06", "net_income": 123.45}]},
        )

    def _continue(payload: dict[str, Any]) -> dict[str, Any]:
        continuation_payloads.append(payload)
        if len(continuation_payloads) == 1:
            assert '"is_error": true' in payload["input"][-1]["output"]
            assert "PRE_TOOL_CHECK_FAILED" in payload["input"][-1]["output"]
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_income_repair_1",
                        "name": "monthly_income_report",
                        "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
                    }
                ]
            }
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "lx 2026-06 已实现收益为 123.45。"}],
                }
            ]
        }

    wrong_scope_event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_wrong_scope_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"sy","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    outcome = run_assistant_tool_event_loop(
        question="lx 6月收益分析",
        request=AssistantRequest(text="lx 6月收益分析", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        initial_events=(wrong_scope_event,),
        execute_tool_fn=_execute,
        provider="openai",
        create_continuation_response_fn=_continue,
    )

    assert outcome.status == "done"
    assert outcome.stop_reason == "model_final_answer"
    assert calls == [
        (
            "monthly_income_report",
            {"account": "lx", "month": "2026-06", "include_rows": True, "config_key": "us"},
        )
    ]
    assert [event["event_type"] for event in outcome.public_payload()["events"]] == [
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "evidence_updated",
        "model_final_answer",
    ]
    assert outcome.public_payload()["events"][1]["error_code"] == "PRE_TOOL_CHECK_FAILED"
    trace = outcome.public_payload()["trace"]
    assert trace["continuation_count"] == 2
    assert trace["repair_attempted"] is True
    assert trace["capability_selection"]["selected_count"] == 2


def test_run_assistant_tool_event_loop_turns_duplicate_query_into_final_answer() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    continuation_payloads: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "schema_version": "analysis.query.output.v2",
                "source_label": "OM read-only analysis workspace",
                "columns": ["month", "account", "net_income_cny"],
                "rows": [
                    {"month": "2026-06", "account": "lx", "net_income_cny": 3000.27},
                    {"month": "2026-06", "account": "sy", "net_income_cny": 11138.28},
                ],
                "row_count": 2,
                "truncated": False,
                "views_used": ["account_monthly_performance"],
            },
        )

    def _continue(payload: dict[str, Any]) -> dict[str, Any]:
        continuation_payloads.append(payload)
        if len(continuation_payloads) == 1:
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_analysis_duplicate_1",
                        "name": "analysis_query",
                        "arguments": (
                            '{"sql":"select month, account, net_income_cny from account_monthly_performance '
                            "where month = '2026-06' order by account\","
                            '"limit":200}'
                        ),
                    }
                ]
            }
        assert "tools" not in payload
        assert "tool_choice" not in payload
        assert "DUPLICATE_TOOL_CALL" in payload["input"][-1]["output"]
        assert "Do not call any more tools" in payload["instructions"]
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "6月收益已基于已有查询结果完成总结。"}],
                }
            ]
        }

    event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_analysis_1",
            "name": "analysis_query",
            "arguments": (
                '{"sql":"select month, account, net_income_cny from account_monthly_performance '
                "where month = '2026-06' order by account\"}"
            ),
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    outcome = run_assistant_tool_event_loop(
        question="总结分析6月的收益情况",
        request=AssistantRequest(text="总结分析6月的收益情况", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"months": ["2026-06"]}},
        initial_events=(event,),
        execute_tool_fn=_execute,
        provider="openai",
        create_continuation_response_fn=_continue,
    )

    assert outcome.status == "done"
    assert outcome.stop_reason == "model_final_answer"
    assert outcome.final_answer == "6月收益已基于已有查询结果完成总结。"
    assert calls == [
        (
            "analysis_query",
            {
                "sql": "select month, account, net_income_cny from account_monthly_performance where month = '2026-06' order by account",
                "config_key": "us",
            },
        )
    ]
    assert len(continuation_payloads) == 2
    trace = outcome.public_payload()["trace"]
    assert trace["continuation_count"] == 2
    assert trace["repair_attempted"] is True
    assert trace["answer_route"] == "llm_from_tool_observation"


def test_synthesis_observation_keeps_moderate_analysis_rows_complete() -> None:
    result = build_response(
        tool_name="analysis_query",
        ok=True,
        data={
            "schema_version": "analysis.query.output.v2",
            "source_label": "OM read-only analysis workspace",
            "columns": ["month", "account", "symbol", "amount_gross"],
            "rows": [
                {
                    "month": "2026-06",
                    "account": "lx" if index % 2 == 0 else "sy",
                    "symbol": f"SYM{index:02d}",
                    "amount_gross": float(index),
                }
                for index in range(31)
            ],
            "row_count": 31,
            "truncated": False,
            "views_used": ["symbol_income_attribution"],
        },
    )

    observation = build_synthesis_observation(
        index=1,
        tool_name="analysis_query",
        payload={"config_key": "us", "sql": "select * from symbol_income_attribution"},
        result=result,
    )

    data = observation["data"]
    assert len(data["rows"]) == 31
    assert data["rows_complete"] is True
    assert data["row_preview_limit"] == 31


def test_synthesis_observation_keeps_moderate_monthly_income_rows_complete() -> None:
    rows = [
        {
            "month": "2026-06",
            "account": "lx" if index % 2 == 0 else "sy",
            "symbol": f"SYM{index:02d}",
            "currency": "USD",
            "net_cashflow_gross": float(index),
        }
        for index in range(31)
    ]
    result = build_response(
        tool_name="monthly_income_report",
        ok=True,
        data={
            "summary": [{"month": "2026-06", "account": "lx", "currency": "USD", "net_cashflow_gross": 12.0}],
            "return_summary": [{"month": "2026-06", "account": "lx", "net_income_cny": 88.0}],
            "cashflow_rows": rows,
            "row_count": 1,
            "cashflow_row_count": 31,
        },
    )

    observation = build_synthesis_observation(
        index=1,
        tool_name="monthly_income_report",
        payload={"config_key": "us", "month": "2026-06", "include_rows": True},
        result=result,
    )

    data = observation["data"]
    assert len(data["cashflow_rows"]) == 31
    assert data["cashflow_rows_complete"] is True
    assert data["cashflow_rows_preview_limit"] == 31


def test_run_assistant_tool_event_loop_stops_repeated_recoverable_denial() -> None:
    continuation_payloads: list[dict[str, Any]] = []

    def _continue(payload: dict[str, Any]) -> dict[str, Any]:
        continuation_payloads.append(payload)
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": f"call_income_wrong_scope_retry_{len(continuation_payloads)}",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"sy","month":"2026-06","include_rows":true}',
                }
            ]
        }

    wrong_scope_event = model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_wrong_scope_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"sy","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )

    outcome = run_assistant_tool_event_loop(
        question="lx 6月收益分析",
        request=AssistantRequest(text="lx 6月收益分析", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "scope": {"requested_accounts": ["lx"]}},
        initial_events=(wrong_scope_event,),
        execute_tool_fn=lambda _tool_name, _payload: build_response(tool_name="monthly_income_report", ok=True, data={}),
        provider="openai",
        create_continuation_response_fn=_continue,
    )

    assert outcome.status == "stopped"
    assert outcome.stop_reason == "repeated_recoverable_error"
    assert len(continuation_payloads) == 1
    assert [event["event_type"] for event in outcome.public_payload()["events"]] == [
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "evidence_updated",
    ]
    trace = outcome.public_payload()["trace"]
    assert trace["guard_denial_recoverable"] is True
    assert trace["repair_attempted"] is True


def test_execute_tool_loop_payload_preserves_provider_protocol_error() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    response = execute_tool_loop_payload(
        question="记录 sy 期权被指派通知",
        request=AssistantRequest(text="记录 sy 期权被指派通知", sender_id="u1", config_key="us"),
        loop_payload={
            "provider": "deepseek",
            "task_contract": {
                "requested_effect": "preview_write",
                "scope": {"requested_accounts": ["sy"]},
            },
            "events": [
                {
                    "event_type": "model_tool_call",
                    "event_id": "model_tool_call_1",
                    "tool_call_id": "call_bad_args_1",
                    "tool_name": "manual_assignment",
                    "arguments": {},
                    "protocol_error": {
                        "code": "INVALID_MODEL_EVENT",
                        "message": "provider tool call arguments are not valid JSON",
                        "details": {"reason": "provider_arguments_malformed"},
                    },
                }
            ],
        },
        settings=AssistantSettings(),
        conversation_context=None,
        execute_tool_fn=lambda tool_name, payload: calls.append((tool_name, payload)) or {},
    )

    assert response["ok"] is False
    assert calls == []
    events = response["data"]["event_loop"]["events"]
    assert events[0]["protocol_error"]["details"]["reason"] == "provider_arguments_malformed"
    assert events[1]["event_type"] == "tool_guard_decision"
    assert events[1]["decision"] == "provider_protocol_error"
    assert events[1]["error_code"] == "INVALID_MODEL_EVENT"


def test_run_assistant_tool_event_loop_prechecks_direct_preview_tool_call() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    event = ModelToolCallEvent(
        event_id="model_tool_call_1",
        tool_call_id="call_assignment_1",
        tool_name="manual_assignment",
        arguments={},
        purpose="model selected assignment preview",
        provider="openai",
    )

    outcome = run_assistant_tool_event_loop(
        question="记录期权被指派通知 PDD 已被指派",
        request=AssistantRequest(text="记录期权被指派通知 PDD 已被指派", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "preview_write", "scope": {"requested_accounts": []}},
        initial_events=(event,),
        execute_tool_fn=lambda tool_name, payload: calls.append((tool_name, payload)) or {},
    )

    assert outcome.status == "needs_clarification"
    assert outcome.stop_reason == "clarification_request"
    assert calls == []
    assert outcome.clarification_request is not None
    assert outcome.clarification_request["questions"][0]["slot"] == "account"
    trace = outcome.public_payload()["trace"]
    assert trace["preview_error"]["code"] == "NEEDS_CLARIFICATION"
