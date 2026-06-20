from __future__ import annotations

import json
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.assistant.model_continuation import (
    chat_completions_continuation_messages,
    continue_model_after_tool_result,
    openai_responses_continuation_input,
    provider_continuation_payload,
)
from src.application.assistant.model_events import (
    AssistantEvent,
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    ToolResultEvent,
    adapt_tool_result,
    model_events_from_provider_response,
    model_tool_call_from_provider_block,
)


def _income_tool_call() -> ModelToolCallEvent:
    return model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_income_1",
            "name": "monthly_income_report",
            "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
        },
        provider="openai",
        event_id="model_tool_call_1",
    )


def _income_tool_result(model_event: ModelToolCallEvent | None = None) -> ToolResultEvent:
    event = model_event or _income_tool_call()
    return adapt_tool_result(
        event_id="result_call_income_1",
        parent_event_id="guard_call_income_1",
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        normalized_payload={**event.arguments, "config_key": "us"},
        raw_result=build_response(
            tool_name=event.tool_name,
            ok=True,
            data={
                "row_count": 1,
                "rows": [{"account": "lx", "month": "2026-06", "net_income": 123.45}],
            },
        ),
    ).event


def _analysis_tool_call() -> ModelToolCallEvent:
    return model_tool_call_from_provider_block(
        {
            "type": "function_call",
            "call_id": "call_analysis_1",
            "name": "analysis_query",
            "arguments": '{"sql":"select month, account, net_income_cny from account_monthly_performance","limit":20}',
        },
        provider="openai",
        event_id="model_tool_call_analysis_1",
    )


def _analysis_tool_result(model_event: ModelToolCallEvent | None = None) -> ToolResultEvent:
    event = model_event or _analysis_tool_call()
    return adapt_tool_result(
        event_id="result_call_analysis_1",
        parent_event_id="guard_call_analysis_1",
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        normalized_payload={**event.arguments, "config_key": "us"},
        raw_result=build_response(
            tool_name=event.tool_name,
            ok=True,
            data={
                "schema_version": "analysis.query.output.v2",
                "source_label": "OM read-only analysis workspace",
                "columns": ["month", "account", "net_income_cny"],
                "rows": [
                    {"month": "2026-06", "account": "lx", "net_income_cny": 2414.0},
                    {"month": "2026-06", "account": "sy", "net_income_cny": 11138.0},
                ],
                "row_count": 2,
                "truncated": False,
                "views_used": ["account_monthly_performance"],
                "fallback_text": "分析查询结果：2 行\n| month | account | net_income_cny |",
            },
        ),
    ).event


def test_openai_responses_continuation_input_binds_original_tool_call_id() -> None:
    call = _income_tool_call()
    result = _income_tool_result(call)

    continuation = openai_responses_continuation_input(model_event=call, tool_result_event=result)

    assert continuation[0] == {
        "type": "function_call",
        "call_id": "call_income_1",
        "name": "monthly_income_report",
        "arguments": json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
    }
    output = json.loads(continuation[1]["output"])
    assert continuation[1]["type"] == "function_call_output"
    assert continuation[1]["call_id"] == "call_income_1"
    assert output["type"] == "tool_result"
    assert output["tool_call_id"] == "call_income_1"
    assert output["is_error"] is False
    assert output["content"]["tool_name"] == "monthly_income_report"
    assert output["content"]["data_summary"]["row_count"] == 1


def test_continuation_observation_includes_analysis_query_rows_preview() -> None:
    call = _analysis_tool_call()
    result = _analysis_tool_result(call)

    continuation = openai_responses_continuation_input(model_event=call, tool_result_event=result)

    output = json.loads(continuation[1]["output"])
    preview = output["content"]["data_preview"]
    assert preview["columns"] == ["month", "account", "net_income_cny"]
    assert preview["rows"][0] == {"month": "2026-06", "account": "lx", "net_income_cny": 2414.0}
    assert preview["rows"][1]["net_income_cny"] == 11138.0
    assert "sql" not in json.dumps(preview, ensure_ascii=False).lower()


def test_chat_completions_continuation_messages_bind_tool_call_id() -> None:
    call = _income_tool_call()
    result = _income_tool_result(call)

    messages = chat_completions_continuation_messages(model_event=call, tool_result_event=result)

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == "call_income_1"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "monthly_income_report"
    assert json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]) == call.arguments
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_income_1"
    assert json.loads(messages[1]["content"])["tool_call_id"] == "call_income_1"


def test_provider_continuation_payload_preserves_base_payload_by_provider_kind() -> None:
    call = _income_tool_call()
    result = _income_tool_result(call)

    responses_payload = provider_continuation_payload(
        provider="openai",
        model_event=call,
        tool_result_event=result,
        base_payload={
            "model": "gpt-5.2",
            "instructions": "Use tool observations only.",
            "input": [{"role": "user", "content": "6月收益分析"}],
            "tools": [{"type": "function", "name": "monthly_income_report"}],
        },
    )
    chat_payload = provider_continuation_payload(
        provider="deepseek",
        model_event=call,
        tool_result_event=result,
        base_payload={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "6月收益分析"}],
            "tools": [{"type": "function", "function": {"name": "monthly_income_report"}}],
        },
    )

    assert responses_payload["model"] == "gpt-5.2"
    assert responses_payload["input"][0]["role"] == "user"
    assert [item["type"] for item in responses_payload["input"][1:]] == ["function_call", "function_call_output"]
    assert chat_payload["model"] == "deepseek-chat"
    assert [item["role"] for item in chat_payload["messages"]] == ["user", "assistant", "tool"]


def test_provider_response_maps_final_answer_after_tool_result() -> None:
    events = model_events_from_provider_response(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "6月 lx 收益净收入为 123.45。"}],
                }
            ]
        },
        provider="openai",
        parent_event_id="result_call_income_1",
    )

    assert len(events) == 1
    assert isinstance(events[0], ModelFinalAnswerEvent)
    assert events[0].event_type == "model_final_answer"
    assert events[0].answer_route == "llm_from_tool_observation"
    assert events[0].parent_event_id == "result_call_income_1"


def test_provider_response_tool_call_wins_over_same_turn_text() -> None:
    events = model_events_from_provider_response(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "我先补查组成。"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_income_2",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
                },
            ]
        },
        provider="openai",
        parent_event_id="result_call_income_1",
    )

    assert len(events) == 1
    assert isinstance(events[0], ModelToolCallEvent)
    assert events[0].tool_call_id == "call_income_2"


def test_provider_plain_text_json_plan_is_not_a_continuation_event() -> None:
    assert (
        model_events_from_provider_response(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '```json\n{"steps":[{"tool_name":"monthly_income_report"}]}\n```',
                            }
                        ],
                    }
                ]
            },
            provider="openai",
        )
        == ()
    )


def test_provider_structured_clarification_maps_existing_request_schema() -> None:
    events = model_events_from_provider_response(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "clarification_request",
                            "question": "请指定要查询的账户。",
                            "slot": "account",
                            "reason": "missing_account_scope",
                            "risk_class": "read",
                        }
                    ],
                }
            ]
        },
        provider="openai",
        parent_event_id="result_call_income_1",
    )

    assert len(events) == 1
    assert isinstance(events[0], AssistantEvent)
    assert events[0].event_type == "clarification_request"
    payload = events[0].payload
    assert payload["reason"] == "missing_account_scope"
    assert payload["clarification_request"]["schema_version"] == "om-agent-clarification-request-v1"
    assert payload["clarification_request"]["questions"][0]["slot"] == "account"


def test_continue_model_after_tool_result_calls_provider_once_and_maps_answer() -> None:
    call = _income_tool_call()
    result = _income_tool_result(call)
    requests: list[dict[str, Any]] = []

    def _create_response(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return {"choices": [{"message": {"content": "6月 lx 收益净收入为 123.45。"}}]}

    outcome = continue_model_after_tool_result(
        provider="deepseek",
        create_response_fn=_create_response,
        model_event=call,
        tool_result_event=result,
        base_payload={"model": "deepseek-chat", "messages": [{"role": "user", "content": "6月收益分析"}]},
    )

    assert len(requests) == 1
    assert [message["role"] for message in requests[0]["messages"]] == ["user", "assistant", "tool"]
    assert outcome.public_payload()["event_count"] == 1
    assert isinstance(outcome.events[0], ModelFinalAnswerEvent)
    assert outcome.events[0].answer_text == "6月 lx 收益净收入为 123.45。"
    assert outcome.events[0].parent_event_id == result.event_id


def test_continuation_rejects_mismatched_tool_result_binding() -> None:
    call = _income_tool_call()
    result = _income_tool_result(call)
    mismatched = ToolResultEvent(
        event_id=result.event_id,
        tool_call_id="call_other",
        tool_name=result.tool_name,
        ok=result.ok,
        observation=result.observation,
        evidence_delta=result.evidence_delta,
        trace_payload=result.trace_payload,
    )

    with pytest.raises(AgentToolError) as excinfo:
        openai_responses_continuation_input(model_event=call, tool_result_event=mismatched)

    assert excinfo.value.code == "INVALID_MODEL_EVENT"
