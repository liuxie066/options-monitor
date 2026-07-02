from __future__ import annotations

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.model_events import (
    MODEL_EVENT_SCHEMA_VERSION,
    ModelFinalAnswerEvent,
    ToolGuardDecisionEvent,
    adapt_tool_result,
    chat_completions_tools_payload,
    event_transcript_payload,
    model_tool_call_from_provider_block,
    model_tool_calls_from_provider_content,
    model_tool_calls_from_provider_response,
    openai_responses_tools_payload,
    provider_tool_schema_from_manifest,
)


def test_provider_content_maps_tool_use_block_and_ignores_markdown_text() -> None:
    events = model_tool_calls_from_provider_content(
        [
            {
                "type": "text",
                "text": '```json\n{"tool_name":"wrong","arguments":{"month":"2026-05"}}\n```',
            },
            {
                "type": "tool_use",
                "id": "toolu_001",
                "name": "monthly_income_report",
                "input": {"month": "2026-06", "include_rows": True},
            },
        ],
        provider="anthropic",
        parent_event_id="user_1",
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "model_tool_call"
    assert event.tool_call_id == "toolu_001"
    assert event.tool_name == "monthly_income_report"
    assert event.arguments == {"month": "2026-06", "include_rows": True}
    assert event.parent_event_id == "user_1"


def test_provider_function_call_arguments_are_structured_provider_payload_not_text_plan() -> None:
    event = model_tool_call_from_provider_block(
        {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "analysis_query",
                "arguments": '{"view":"account_monthly_income_components","month":"2026-06"}',
            },
        },
        provider="deepseek",
    )

    assert event.tool_call_id == "call_123"
    assert event.tool_name == "analysis_query"
    assert event.arguments == {
        "view": "account_monthly_income_components",
        "month": "2026-06",
    }


def test_provider_tool_call_malformed_arguments_marks_protocol_reason() -> None:
    event = model_tool_call_from_provider_block(
        {
            "id": "call_bad",
            "type": "function",
            "function": {
                "name": "manual_assignment",
                "arguments": '{"account"',
            },
        },
        provider="deepseek",
    )

    assert event.tool_call_id == "call_bad"
    assert event.tool_name == "manual_assignment"
    assert event.arguments == {}
    assert event.protocol_error is not None
    assert event.protocol_error["code"] == "INVALID_MODEL_EVENT"
    assert event.protocol_error["message"] == "provider tool call arguments are not valid JSON"
    assert event.protocol_error["details"]["reason"] == "provider_arguments_malformed"
    assert event.protocol_error["details"]["argument_type"] == "str"
    assert event.protocol_error["details"]["argument_chars"] == len('{"account"')
    assert event.public_payload()["protocol_error"]["details"]["reason"] == "provider_arguments_malformed"


def test_plain_text_block_is_not_a_provider_tool_call() -> None:
    with pytest.raises(AgentToolError) as excinfo:
        model_tool_call_from_provider_block(
            {
                "type": "text",
                "text": '{"tool_name":"monthly_income_report","arguments":{"month":"2026-06"}}',
            },
            provider="openai",
        )

    assert excinfo.value.code == "INVALID_MODEL_EVENT"


def test_openai_responses_response_maps_function_call_and_ignores_output_text_json_plan() -> None:
    events = model_tool_calls_from_provider_response(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"steps":[{"tool_name":"wrong","arguments":{"month":"2026-05"}}]}',
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_001",
                    "name": "monthly_income_report",
                    "arguments": '{"month":"2026-06","include_rows":true}',
                },
            ]
        },
        provider="openai",
        parent_event_id="user_1",
    )

    assert len(events) == 1
    assert events[0].tool_call_id == "call_001"
    assert events[0].tool_name == "monthly_income_report"
    assert events[0].arguments == {"month": "2026-06", "include_rows": True}
    assert events[0].provider == "openai"
    assert events[0].parent_event_id == "user_1"


def test_chat_completions_response_maps_message_tool_calls_not_markdown_json_plan() -> None:
    events = model_tool_calls_from_provider_response(
        {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"tool_name":"wrong","arguments":{"symbol":"POP"}}\n```',
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "symbol_resolve",
                                    "arguments": '{"symbol":"泡泡玛特","config_key":"hk"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        provider="deepseek",
    )

    assert len(events) == 1
    assert events[0].tool_call_id == "call_abc"
    assert events[0].tool_name == "symbol_resolve"
    assert events[0].arguments == {"symbol": "泡泡玛特", "config_key": "hk"}


def test_provider_plain_text_json_plan_response_does_not_parse_as_tool_call() -> None:
    assert (
        model_tool_calls_from_provider_response(
            {"output_text": '{"tool_name":"monthly_income_report","arguments":{"month":"2026-06"}}'},
            provider="openai",
        )
        == ()
    )
    assert (
        model_tool_calls_from_provider_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"tool_name":"monthly_income_report","arguments":{"month":"2026-06"}}'
                        }
                    }
                ]
            },
            provider="deepseek",
        )
        == ()
    )


def test_provider_tool_schema_from_manifest_omits_system_scoped_arguments() -> None:
    schema = provider_tool_schema_from_manifest(
        {
            "name": "monthly_income_report",
            "description": "Return monthly option income statistics.",
            "capabilities": ["income_report", "read_only"],
            "input_schema": {
                "config_key": "us|hk",
                "config_path": "optional explicit config path",
                "account": "optional account label",
                "month": "optional YYYY-MM filter",
                "include_rows": "optional bool; include detail rows",
            },
        }
    )

    parameters = schema["parameters"]
    assert schema["schema_version"] == "om-assistant-provider-tool-schema-v1"
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"account", "month", "include_rows"}
    assert parameters["properties"]["include_rows"]["type"] == "boolean"
    assert "config_key" not in parameters["properties"]
    assert "config_path" not in parameters["properties"]


def test_provider_tool_schema_omits_host_owned_arguments() -> None:
    schema = provider_tool_schema_from_manifest(
        {
            "name": "manual_assignment",
            "description": "Create assignment preview.",
            "capabilities": ["preview_operation"],
            "input_schema": {
                "raw_text": {
                    "type": "string",
                    "description": "Original user message.",
                    "host_owned": True,
                },
                "account": {
                    "type": ["string", "null"],
                    "enum": ["lx", "sy", None],
                    "description": "Optional account label.",
                },
            },
        }
    )

    properties = schema["parameters"]["properties"]
    assert set(properties) == {"account"}
    assert properties["account"]["type"] == "string"
    assert properties["account"]["enum"] == ["lx", "sy"]
    assert "raw_text" not in properties


def test_provider_tool_schema_normalizes_nested_nullable_fields() -> None:
    schema = provider_tool_schema_from_manifest(
        {
            "name": "nested_example",
            "description": "Example.",
            "capabilities": ["read_only"],
            "input_schema": {
                "query": {
                    "type": ["object", "null"],
                    "properties": {
                        "symbol": {"type": ["string", "null"], "enum": ["PDD", None]},
                        "tags": {"type": "array", "items": {"type": ["string", "null"]}},
                    },
                },
            },
        }
    )

    query = schema["parameters"]["properties"]["query"]
    assert query["type"] == "object"
    assert query["properties"]["symbol"]["type"] == "string"
    assert query["properties"]["symbol"]["enum"] == ["PDD"]
    assert query["properties"]["tags"]["items"]["type"] == "string"


def test_provider_tool_schema_treats_pipe_enums_as_scalar_strings() -> None:
    schema = provider_tool_schema_from_manifest(
        {
            "name": "option_positions_read",
            "description": "Read local option position lots.",
            "capabilities": ["option_positions", "read_only"],
            "input_schema": {
                "action": "list|events|history|inspect|assigned-stock",
                "status": "list-only open|close|all",
                "quote_snapshots": "assigned-stock optional quote snapshot list/dict",
            },
        }
    )

    properties = schema["parameters"]["properties"]
    assert properties["action"]["type"] == "string"
    assert properties["action"]["enum"] == ["list", "events", "history", "inspect", "assigned-stock"]
    assert properties["status"]["type"] == "string"
    assert properties["quote_snapshots"]["type"] == ["array", "object"]


def test_provider_tools_payloads_expose_only_allowed_read_tool_subset() -> None:
    manifest = [
        {
            "name": "monthly_income_report",
            "description": "Return monthly option income statistics.",
            "read_only": True,
            "risk_level": "read_only",
            "capabilities": ["income_report", "read_only"],
            "input_schema": {
                "config_key": "us|hk",
                "account": "optional account label",
                "month": "optional YYYY-MM filter",
                "include_rows": "optional bool; include detail rows",
            },
            "launcher": {"command": ["./om-agent"]},
            "examples": [{"input": {"month": "2026-06"}}],
        },
        {
            "name": "manage_symbols",
            "description": "Mutate symbols.",
            "read_only": False,
            "risk_level": "local_write",
            "requires_confirm": True,
            "capabilities": ["config_write"],
            "input_schema": {"symbol": "required symbol", "confirm": "required true"},
        },
    ]

    responses_payload = openai_responses_tools_payload(
        manifest,
        allowed_tool_names={"monthly_income_report", "manage_symbols"},
    )
    chat_payload = chat_completions_tools_payload(
        manifest,
        allowed_tool_names={"monthly_income_report", "manage_symbols"},
    )

    assert [item["name"] for item in responses_payload] == ["monthly_income_report"]
    assert set(responses_payload[0]) == {"type", "name", "description", "parameters"}
    assert responses_payload[0]["type"] == "function"
    assert "config_key" not in responses_payload[0]["parameters"]["properties"]
    assert "launcher" not in responses_payload[0]
    assert "examples" not in responses_payload[0]

    assert [item["function"]["name"] for item in chat_payload] == ["monthly_income_report"]
    assert chat_payload[0]["type"] == "function"
    assert "config_key" not in chat_payload[0]["function"]["parameters"]["properties"]


def test_tool_result_adapter_splits_raw_result_from_model_observation() -> None:
    guard = ToolGuardDecisionEvent(
        event_id="guard_1",
        parent_event_id="call_event_1",
        tool_call_id="toolu_001",
        tool_name="monthly_income_report",
        allowed=True,
        decision="allow",
        reason="read_auto_in_scope",
        risk_class="READ_AUTO",
        scope_source="host_task_contract",
        normalized_payload={"month": "2026-06", "include_rows": True},
        duplicate_signature="monthly_income_report:2026-06",
    )
    adapted = adapt_tool_result(
        event_id="result_1",
        parent_event_id=guard.event_id,
        tool_call_id="toolu_001",
        tool_name="monthly_income_report",
        normalized_payload={"month": "2026-06", "include_rows": True},
        guard_decision=guard,
        output_contract={"canonical_renderer": "monthly_income", "source_label": "OM 本地账本"},
        raw_result={
            "schema_version": "1.0",
            "tool_name": "monthly_income_report",
            "ok": True,
            "data": {
                "row_count": 2,
                "rows": [
                    {"account": "lx", "net_income": 100},
                    {"account": "sy", "net_income": 80},
                ],
                "internal_sql": "select * from internal_table",
                "artifact_path": "/tmp/private/report.json",
            },
        },
    )

    payload = adapted.public_payload()
    event_payload = payload["event"]
    observation = event_payload["observation"]

    assert payload["schema_version"] == "om-assistant-tool-result-adapter-v1"
    assert adapted.raw_result["data"]["internal_sql"] == "select * from internal_table"
    assert event_payload["event_type"] == "tool_result"
    assert event_payload["ok"] is True
    assert observation["schema_version"] == "om-assistant-model-observation-v1"
    assert observation["data_summary"]["row_count"] == 2
    assert observation["data_quality"]["row_count"] == 2
    assert observation["data_quality"]["missing_data_count"] == 0
    assert observation["continuation_advice"]["may_request_more_read_tools"] is True
    assert observation["output_contract"]["canonical_renderer"] == "monthly_income"
    assert "internal_sql" not in observation["data_summary"]["keys"]
    assert "artifact_path" not in observation["data_summary"]["keys"]
    assert "select * from internal_table" not in str(observation)
    assert "/tmp/private/report.json" not in str(observation)
    assert event_payload["trace_payload"]["guard_decision"]["reason"] == "read_auto_in_scope"
    assert event_payload["evidence_delta"]["datasets"][0]["tool_name"] == "monthly_income_report"


def test_tool_result_adapter_previews_symbol_config_values_for_model() -> None:
    adapted = adapt_tool_result(
        event_id="result_1",
        tool_call_id="call_symbol_config",
        tool_name="symbol_config_read",
        normalized_payload={"symbol": "中国海洋石油"},
        output_contract={
            "canonical_renderer": "symbol_config",
            "model_preview_fields": ["symbol", "canonical_symbol", "found", "strategies"],
        },
        raw_result={
            "schema_version": "1.0",
            "tool_name": "symbol_config_read",
            "ok": True,
            "data": {
                "symbol": "中国海洋石油",
                "canonical_symbol": "0883.HK",
                "found": True,
                "strategies": {
                    "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 90, "max_strike": 20},
                    "sell_call": {"enabled": True, "min_strike": 30},
                    "combo_yield": {"enabled": False, "_explicit_fields": ["enabled"]},
                },
            },
        },
    )

    observation = adapted.event.provider_tool_result_payload()["content"]

    assert observation["data_summary"]["keys"] == ["canonical_symbol", "found", "strategies", "symbol"]
    assert observation["output_contract"]["canonical_renderer"] == "symbol_config"
    assert observation["query_scope"]["payload"]["symbol"] == "中国海洋石油"
    assert observation["data_preview"]["canonical_symbol"] == "0883.HK"
    assert observation["data_preview"]["strategies"]["sell_put"]["max_strike"] == 20
    assert "_explicit_fields" not in observation["data_preview"]["strategies"]["combo_yield"]


def test_tool_result_adapter_uses_scalar_fact_fields_as_model_preview() -> None:
    adapted = adapt_tool_result(
        event_id="result_1",
        tool_call_id="call_symbol_resolve",
        tool_name="symbol_resolve",
        normalized_payload={"symbol": "中国海洋石油"},
        output_contract={
            "result_shape": "scalar",
            "fact_fields": ["symbol", "canonical_symbol", "market", "currency"],
        },
        raw_result={
            "schema_version": "1.0",
            "tool_name": "symbol_resolve",
            "ok": True,
            "data": {
                "symbol": "中国海洋石油",
                "canonical_symbol": "0883.HK",
                "market": "HK",
                "currency": "HKD",
                "config_path": "/private/runtime/config.hk.json",
            },
        },
    )

    observation = adapted.event.provider_tool_result_payload()["content"]

    assert observation["data_preview"] == {
        "symbol": "中国海洋石油",
        "canonical_symbol": "0883.HK",
        "market": "HK",
        "currency": "HKD",
    }
    assert "config_path" not in observation["data_preview"]


def test_event_transcript_payload_preserves_order_for_minimal_tool_loop() -> None:
    call = model_tool_call_from_provider_block(
        {
            "type": "tool_use",
            "id": "toolu_001",
            "name": "monthly_income_report",
            "input": {"month": "2026-06"},
        },
        provider="anthropic",
        event_id="call_event_1",
    )
    guard = ToolGuardDecisionEvent(
        event_id="guard_1",
        parent_event_id=call.event_id,
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        allowed=True,
        decision="allow",
        reason="read_auto_in_scope",
        risk_class="READ_AUTO",
        scope_source="host_task_contract",
        normalized_payload=call.arguments,
    )
    result = adapt_tool_result(
        event_id="result_1",
        parent_event_id=guard.event_id,
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        normalized_payload=call.arguments,
        guard_decision=guard,
        raw_result={"schema_version": "1.0", "tool_name": call.tool_name, "ok": True, "data": {"row_count": 1}},
    ).event
    answer = ModelFinalAnswerEvent(
        event_id="answer_1",
        parent_event_id=result.event_id,
        answer_text="6月收益已基于工具证据汇总。",
    )

    transcript = event_transcript_payload([call, guard, result, answer])

    assert [item["event_type"] for item in transcript] == [
        "model_tool_call",
        "tool_guard_decision",
        "tool_result",
        "model_final_answer",
    ]
    assert all(item["schema_version"] == MODEL_EVENT_SCHEMA_VERSION for item in transcript)
    assert result.provider_tool_result_payload() == {
        "type": "tool_result",
        "tool_call_id": "toolu_001",
        "is_error": False,
        "content": result.observation,
    }
