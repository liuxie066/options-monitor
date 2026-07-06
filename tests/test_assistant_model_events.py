from __future__ import annotations

from src.application.assistant.model_events import (
    MODEL_EVENT_SCHEMA_VERSION,
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    ToolGuardDecisionEvent,
    adapt_tool_result,
    event_transcript_payload,
)


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
    call = ModelToolCallEvent(
        event_id="call_event_1",
        tool_call_id="toolu_001",
        tool_name="monthly_income_report",
        arguments={"month": "2026-06"},
        provider="copilot",
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
