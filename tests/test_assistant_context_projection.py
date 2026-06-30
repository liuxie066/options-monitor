from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.context_projection import (
    CONTEXT_PROJECTION_SCHEMA_VERSION,
    build_context_projection,
    context_projection_trace,
)
from src.application.assistant.conversation_context import build_conversation_context, context_trace
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.operation_store import InboundOperationStore


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_context_projection.jsonl"


def test_context_projection_fixture_keeps_refs_and_sanitizes_payloads() -> None:
    case = _fixture_case("projection_keeps_recent_tool_ref")

    projection = build_context_projection(
        current_user_message=str(case["current_user_message"]),
        conversation_context=dict(case["conversation_context"]),
        recent_sessions=list(case["recent_sessions"]),
    )

    assert projection["schema_version"] == CONTEXT_PROJECTION_SCHEMA_VERSION
    assert projection["current_user_message"]["text"] == "这个怎么算？"
    assert len(projection["recent_turns"]) >= case["expect"]["recent_turn_count_min"]
    assert len(projection["recent_successful_tools"]) == case["expect"]["recent_successful_tool_count"]
    assert len(projection["available_evidence_refs"]) == case["expect"]["evidence_ref_count"]
    assert len(projection["open_evidence_gaps"]) == case["expect"]["open_gap_count"]
    assert len(projection["pending_operations"]) == case["expect"]["pending_operation_count"]

    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    for key in case["expect"]["forbidden_payload_keys_absent"]:
        assert key not in serialized

    analysis_tool = next(
        item for item in projection["recent_successful_tools"] if item["tool_name"] == "analysis_query"
    )
    assert analysis_tool["safe_slots"] == case["expect"]["safe_slots"]
    assert analysis_tool["data_shape"]["row_count"] == case["expect"]["data_shape"]["row_count"]
    assert analysis_tool["data_shape"]["columns"] == case["expect"]["data_shape"]["columns"]
    assert analysis_tool["evidence_refs"] == [projection["available_evidence_refs"][0]["ref_id"]]

    failed_tools = [
        item
        for item in projection["recent_successful_tools"]
        if item.get("tool_name") == "healthcheck"
    ]
    assert failed_tools == []


def test_context_projection_budget_truncation_records_system_boundary() -> None:
    recent_messages = [
        {
            "created_at": f"2026-06-18T10:0{index}:00+08:00",
            "raw_text": f"status {index}",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "tool_payload": {"account": "lx", "status": "ok"},
            "result_ok": True,
        }
        for index in range(3)
    ]

    projection = build_context_projection(
        current_user_message="继续",
        conversation_context={"recent_messages": recent_messages},
        max_recent_turns=1,
        max_successful_tools=1,
        max_open_gaps=1,
    )

    assert len(projection["recent_turns"]) == 1
    assert len(projection["recent_successful_tools"]) == 1
    assert len(projection["available_evidence_refs"]) == 1
    assert projection["budget"]["truncated"] is True
    assert "recent_turn_limit" in projection["budget"]["truncation_reason"]
    assert "successful_tool_limit" in projection["budget"]["truncation_reason"]
    assert projection["system_events"][0]["event_type"] == "system_boundary"


def test_context_projection_bounds_and_redacts_text_excerpts() -> None:
    long_symbol = "NVDA" * 100
    projection = build_context_projection(
        current_user_message="hello\npassword: sk-should-not-leak\n" + ("x" * 500),
        conversation_context={
            "recent_messages": [
                {
                    "created_at": "2026-06-18T10:00:00+08:00",
                    "raw_text": "token: should-not-leak",
                    "intent_name": "runtime_status",
                    "tool_name": "runtime_status",
                    "tool_payload": {"symbol": long_symbol, "account": "lx"},
                    "result_ok": True,
                }
            ],
        },
    )

    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    assert "sk-should-not-leak" not in serialized
    assert "should-not-leak" not in serialized
    assert "[redacted sensitive line]" in serialized
    assert len(projection["current_user_message"]["text"]) <= 360
    symbol = projection["recent_successful_tools"][0]["safe_slots"]["symbol"][0]
    assert len(symbol) <= 240
    assert symbol.endswith("...")


def test_context_projection_includes_sanitized_relevant_memories() -> None:
    projection = build_context_projection(
        current_user_message="怎么优化参数",
        conversation_context={
            "assistant_memory": {
                "provided": True,
                "source": "assistant_memory",
                "format": "markdown_topic_files",
                "memory_count": 1,
                "memories": [
                    {
                        "memory_id": "parameter-tuning",
                        "type": "parameter_tuning_preference",
                        "title": "参数调优偏好",
                        "summary": "用户希望先看候选过滤证据。",
                        "content": "先看 replay 和候选过滤证据。\ntoken: sk-should-not-leak",
                        "tags": ["参数", "候选"],
                        "relevance": {"score": "2", "matched_terms": ["参数", "候选"]},
                    }
                ],
            }
        },
    )

    memories = projection["relevant_memories"]
    assert len(memories) == 1
    assert memories[0]["memory_id"] == "parameter-tuning"
    assert memories[0]["type"] == "parameter_tuning_preference"
    assert memories[0]["relevance"]["score"] == 2
    assert "sk-should-not-leak" not in json.dumps(projection, ensure_ascii=False, sort_keys=True)
    assert "[redacted sensitive line]" in memories[0]["content"]
    assert projection["policy"]["memory_is_hint"] is True
    assert projection["policy"]["tool_evidence_wins_memory"] is True
    assert projection["policy"]["memory_cannot_authorize_writes"] is True
    assert context_projection_trace(projection)["relevant_memory_count"] == 1


def test_context_projection_exposes_symbol_config_setting_slots() -> None:
    projection = build_context_projection(
        current_user_message="改为90",
        conversation_context={},
        recent_sessions=[
            {
                "session_id": "s_symbol_config",
                "created_at": "2026-06-23T22:09:00+08:00",
                "updated_at": "2026-06-23T22:10:00+08:00",
                "raw_text": "FUTU sell put的max strike设置的是多少？",
                "response_text": "FUTU sell_put.max_strike = 120。",
                "snapshot": {
                    "tool_transcript": [
                        {
                            "tool_name": "symbol_config_read",
                            "payload": {"symbol": "FUTU", "strategy": "sell_put", "field": "max_strike"},
                            "ok": True,
                            "summary": {
                                "canonical_symbol": "FUTU",
                                "strategy": "sell_put",
                                "field": "max_strike",
                                "path": "sell_put.max_strike",
                                "value": 120.0,
                                "found": True,
                            },
                        }
                    ]
                },
            }
        ],
    )

    ref = projection["available_evidence_refs"][0]
    assert ref["source_tool"] == "symbol_config_read"
    assert ref["safe_slots"] == {
        "symbol": ["FUTU"],
        "strategy": ["sell_put"],
        "setting_path": ["sell_put.max_strike"],
        "setting_field": ["max_strike"],
    }
    assert ref["data_shape"]["kind"] == "single_symbol_setting"
    assert ref["data_shape"]["setting_path"] == "sell_put.max_strike"
    assert ref["data_shape"]["current_value"] == 120.0
    assert ref["data_shape"]["value_type"] == "float"
    assert projection["active_frames"] == [
        {
            "frame_id": "frame_ev_001",
            "type": "symbol_setting",
            "source_tool": "symbol_config_read",
            "source_ref_id": "ev_001",
            "turn_id": "session:s_symbol_config",
            "symbol": "FUTU",
            "strategy": "sell_put",
            "setting_path": "sell_put.max_strike",
            "setting_field": "max_strike",
            "current_value": 120.0,
            "allowed_deltas": ["set_value", "explain"],
        }
    ]
    assert context_projection_trace(projection)["active_frame_count"] == 1


def test_context_projection_symbol_setting_frame_prefers_canonical_symbol() -> None:
    projection = build_context_projection(
        current_user_message="改为16",
        conversation_context={},
        recent_sessions=[
            {
                "session_id": "s_symbol_config",
                "created_at": "2026-06-23T22:09:00+08:00",
                "updated_at": "2026-06-23T22:10:00+08:00",
                "raw_text": "中国海洋石油 sell put的max strike是多少？",
                "response_text": "中国海洋石油（0883.HK）sell_put.max_strike = 18。",
                "snapshot": {
                    "tool_transcript": [
                        {
                            "tool_name": "symbol_config_read",
                            "payload": {"symbol": "中国海洋石油", "strategy": "sell_put", "field": "max_strike"},
                            "ok": True,
                            "summary": {
                                "canonical_symbol": "0883.HK",
                                "strategy": "sell_put",
                                "field": "max_strike",
                                "path": "sell_put.max_strike",
                                "value": 18.0,
                                "found": True,
                            },
                        }
                    ]
                },
            }
        ],
    )

    assert projection["available_evidence_refs"][0]["safe_slots"]["symbol"] == ["中国海洋石油", "0883.HK"]
    assert projection["available_evidence_refs"][0]["data_shape"]["canonical_symbol"] == "0883.HK"
    assert projection["active_frames"][0]["symbol"] == "0883.HK"


def test_build_conversation_context_attaches_shadow_projection(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    store = InboundAuditStore(audit_db)
    store.record_result(
        {
            "command_id": "in_runtime_status",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:chat_a:ou_1",
            "message_id": "msg_runtime_status",
            "raw_text": "状态",
            "parser": "deterministic",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "tool_payload": {"account": "lx", "config_path": "/tmp/config.us.json"},
            "decision": "allowed",
            "result_ok": True,
            "response": {"data": {"response_text": "runtime ok"}},
            "created_at": "2026-06-18T10:00:00+08:00",
            "finished_at": "2026-06-18T10:00:01+08:00",
        }
    )

    context = build_conversation_context(
        AssistantRequest(
            text="继续",
            sender_id="ou_1",
            channel="feishu",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        audit_store=store,
        max_messages=4,
    )

    projection = context["context_projection"]
    assert projection["schema_version"] == CONTEXT_PROJECTION_SCHEMA_VERSION
    assert projection["current_user_message"]["text"] == "继续"
    assert projection["recent_turns"][0]["tools"] == ["runtime_status"]
    assert projection["recent_successful_tools"][0]["tool_name"] == "runtime_status"
    assert "config_path" not in json.dumps(projection, ensure_ascii=False, sort_keys=True)

    trace = context_trace(context)
    assert trace["context_projection"]["recent_turn_count"] == 1
    assert trace["context_projection"]["recent_successful_tool_count"] == 1
    assert context_projection_trace(projection)["evidence_ref_count"] == 1


def test_build_conversation_context_uses_wechat_window_history_across_senders(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    store = InboundAuditStore(audit_db)
    store.record_result(
        {
            "command_id": "in_symbol_config",
            "channel": "wechat",
            "sender_id": "user_a",
            "conversation_id": "wechat:group_1",
            "message_id": "msg_symbol_config",
            "raw_text": "FUTU sell put的max strike设置的是多少？",
            "parser": "llm",
            "intent_name": "symbol_config_query",
            "tool_name": "symbol_config_read",
            "tool_payload": {"symbol": "FUTU", "strategy": "sell_put", "field": "max_strike"},
            "decision": "allowed",
            "result_ok": True,
            "response": {"data": {"response_text": "FUTU sell_put.max_strike = 120"}},
            "created_at": "2026-06-23T22:09:00+08:00",
            "finished_at": "2026-06-23T22:09:01+08:00",
        }
    )
    InboundOperationStore(audit_db).save_preview(
        operation_id="op_user_a",
        command_id="in_symbol_edit",
        channel="wechat",
        sender_id="user_a",
        conversation_id="wechat:group_1",
        operation_type="symbol_edit",
        payload_hash="hash_user_a",
        payload={"symbol": "FUTU"},
        preview={"summary": "edit FUTU"},
        ttl_seconds=600,
        created_at="2026-06-23T22:10:00+08:00",
    )

    context = build_conversation_context(
        AssistantRequest(
            text="改为90",
            sender_id="user_b",
            channel="wechat",
            conversation_id="wechat:group_1",
            audit_db=str(audit_db),
        ),
        audit_store=store,
        max_messages=4,
    )

    assert [item["intent_name"] for item in context["recent_messages"]] == ["symbol_config_query"]
    assert context["scope"]["sender_id"] == "user_b"
    assert context["pending_operations"] == []
    ref = context["context_projection"]["available_evidence_refs"][0]
    assert ref["safe_slots"]["symbol"] == ["FUTU"]


def test_context_projection_includes_notification_system_event_refs() -> None:
    projection = build_context_projection(
        current_user_message="刚才那条为什么没发？",
        conversation_context={
            "recent_system_events": [
                {
                    "created_at_utc": "2026-06-23T14:00:00+00:00",
                    "event_kind": "notification_delivery_decided",
                    "run_id": "run_1",
                    "summary": "notification_delivery_decided accounts=lx reason=no_send",
                    "safe_slots": {"run_id": ["run_1"], "account": ["lx"], "action": ["skip_no_send"]},
                    "delivery": {"action": "skip_no_send", "reason": "no_send", "should_send": False},
                    "message_count": 1,
                    "notify_candidate_count": 3,
                    "threshold_met": True,
                }
            ]
        },
    )

    turn = projection["recent_turns"][0]
    ref = projection["available_evidence_refs"][0]
    assert turn["event_type"] == "system_event"
    assert turn["evidence_refs"] == [ref["ref_id"]]
    assert ref["source_type"] == "system_event"
    assert ref["source_tool"] == "notification_perception"
    assert ref["safe_slots"]["run_id"] == ["run_1"]
    assert ref["data_shape"]["delivery_action"] == "skip_no_send"
    assert projection["system_events"][0]["event_type"] == "notification_perception"


def test_build_conversation_context_reads_notification_system_events(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    store = InboundAuditStore(audit_db)
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text(
        json.dumps(
            {
                "event_type": "assistant_perception",
                "action": "notification_prepared",
                "run_id": "run_context",
                "event_at_utc": "2026-06-23T14:00:00+00:00",
                "extra": {
                    "event_kind": "notification_prepared",
                    "run_id": "run_context",
                    "created_at_utc": "2026-06-23T14:00:00+00:00",
                    "conversation_scope": {"channel": "wechat", "conversation_id": "wechat:group_1"},
                    "safe_slots": {"run_id": ["run_context"], "action": ["notification_prepared"]},
                    "summary": "notification prepared",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    context = build_conversation_context(
        AssistantRequest(
            text="刚才那条",
            sender_id="user_1",
            channel="wechat",
            conversation_id="wechat:group_1",
            audit_db=str(audit_db),
            reply_context={"base": str(tmp_path)},
        ),
        audit_store=store,
        max_messages=4,
    )

    assert context["recent_system_events"][0]["run_id"] == "run_context"
    assert context["context_projection"]["available_evidence_refs"][0]["source_type"] == "system_event"
    assert context_trace(context)["system_event_count"] == 1


def _fixture_case(case_id: str) -> dict[str, Any]:
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("id") == case_id:
            return item
    raise AssertionError(f"missing fixture case: {case_id}")
