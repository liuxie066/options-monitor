from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from src.application.agent_runtime import AgentRuntimeSettings, LlmTranslatorSettings, handle_agent_message
from src.application.agent_runtime.command_catalog import command_catalog_payload, command_specs
from src.application.agent_runtime.command_parser import parse_agent_command
from src.application.agent_runtime.llm_intent_schema import LLM_INTENT_SCHEMA_VERSION, llm_intent_json_schema, llm_intent_schema
from src.application.agent_runtime.llm_translator import LlmTranslationResult, parse_llm_translation_payload, translate_inbound_intent
from src.application.agent_tool_contracts import build_response
from src.application.inbound.contracts import InboundIntent, InboundRequest
from src.infrastructure.openai_chat_completions import create_json_chat_completion, extract_chat_completion_text
from src.infrastructure.openai_responses import OpenAIResponsesError, create_structured_response, extract_response_text


def test_agent_command_parser_maps_read_commands() -> None:
    positions = parse_agent_command("/positions sy")
    assert positions is not None
    assert positions.name == "option_positions_open"
    assert positions.arguments == {"account": "sy", "status": "open"}
    assert positions.parser == "command"

    all_positions = parse_agent_command("/positions all")
    assert all_positions is not None
    assert all_positions.arguments == {"status": "all"}

    income = parse_agent_command("/income sy 上月", now_fn=lambda: date(2026, 1, 3))
    assert income is not None
    assert income.name == "monthly_income_report"
    assert income.arguments == {"account": "sy", "month": "2025-12"}

    runs = parse_agent_command("/runs 20")
    assert runs is not None
    assert runs.arguments == {"limit": 20}

    logs = parse_agent_command("/logs 20260515T182459Z-474761")
    assert logs is not None
    assert logs.arguments == {"run_id": "20260515T182459Z-474761", "kind": "all", "lines": 50}


def test_agent_command_catalog_drives_llm_allowed_surface() -> None:
    llm_allowed = {spec.intent_name for spec in command_specs() if spec.llm_allowed}
    llm_denied = {spec.intent_name for spec in command_specs() if not spec.llm_allowed}
    schema = llm_intent_schema()
    payload = command_catalog_payload()

    assert set(schema["shape"]["intent"]) == llm_allowed
    assert "runtime_status" in llm_allowed
    assert "manual_trade_confirm" in llm_denied
    assert not (llm_allowed & llm_denied)
    assert payload["summary"]["command_count"] == len(command_specs())
    assert "Command：" in payload["help_text"]


def test_agent_command_parser_maps_typed_confirm_commands() -> None:
    confirm = parse_agent_command("/confirm trade in_abc123")
    assert confirm is not None
    assert confirm.name == "manual_trade_confirm"
    assert confirm.arguments == {"operation_id": "in_abc123", "operation_resolution": "explicit"}

    latest_symbol = parse_agent_command("/confirm symbol")
    assert latest_symbol is not None
    assert latest_symbol.name == "symbol_confirm"
    assert latest_symbol.arguments == {"operation_id": None, "operation_resolution": "latest_pending"}

    cancel_upgrade = parse_agent_command("/cancel upgrade in_abc123")
    assert cancel_upgrade is not None
    assert cancel_upgrade.name == "upgrade_cancel"


def test_agent_runtime_executes_slash_command_through_inbound_router(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    out = handle_agent_message(
        InboundRequest(
            text="/status",
            sender_id="local",
            message_id="msg_status",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert calls == [("runtime_status", {"config_key": "us"})]
    assert out["data"]["intent"]["parser"] == "command"
    assert out["meta"]["agent_runtime"] == {
        "enabled": True,
        "mode": "deterministic",
        "route": "command",
        "llm": {
            "enabled": False,
            "attempted": False,
            "reason": "command",
            "provider": "",
            "base_url": "",
            "model": "",
            "api_key_env": "OM_LLM_API_KEY",
            "confidence_min": 0.75,
            "timeout_seconds": 20,
            "max_output_tokens": 512,
        },
        "context": {"provided": False},
        "langgraph": "disabled",
    }


def test_agent_runtime_keeps_deterministic_fallback(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    out = handle_agent_message(
        InboundRequest(
            text="状态",
            sender_id="local",
            message_id="msg_status_cn",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert calls == [("runtime_status", {"config_key": "us"})]
    assert out["data"]["intent"]["parser"] == "deterministic"
    assert out["meta"]["agent_runtime"]["route"] == "deterministic"
    assert out["meta"]["agent_runtime"]["llm"]["reason"] == "not_needed"


def test_agent_runtime_answers_small_talk_without_tool_or_llm(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    out = handle_agent_message(
        InboundRequest(
            text="你好",
            sender_id="local",
            message_id="msg_small_talk",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AgentRuntimeSettings(
            mode="llm_router",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
    )

    assert out["ok"] is True
    assert calls == []
    assert out["data"]["intent"]["name"] == "small_talk"
    assert out["meta"]["agent_runtime"]["route"] == "deterministic"
    assert out["meta"]["agent_runtime"]["llm"]["reason"] == "not_needed"


def test_agent_runtime_unknown_slash_command_returns_clarification(tmp_path: Path) -> None:
    out = handle_agent_message(
        InboundRequest(
            text="/unknown",
            sender_id="local",
            message_id="msg_unknown",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert "使用 /help" in out["error"]["hint"]
    assert out["meta"]["agent_runtime"]["route"] == "command"


def test_agent_runtime_keeps_llm_disabled_for_unrecognized_text(tmp_path: Path) -> None:
    out = handle_agent_message(
        InboundRequest(
            text="查一下",
            sender_id="local",
            message_id="msg_unknown_text",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert out["meta"]["agent_runtime"]["llm"]["enabled"] is False
    assert out["meta"]["agent_runtime"]["llm"]["attempted"] is False
    assert out["meta"]["agent_runtime"]["llm"]["reason"] == "disabled"


def test_agent_runtime_disabled_setting_skips_command_facade(tmp_path: Path) -> None:
    out = handle_agent_message(
        InboundRequest(
            text="/status",
            sender_id="local",
            message_id="msg_runtime_disabled",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        settings=AgentRuntimeSettings(enabled=False),
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert out["meta"]["agent_runtime"]["enabled"] is False
    assert out["meta"]["agent_runtime"]["route"] == "disabled"
    assert out["meta"]["agent_runtime"]["llm"]["reason"] == "runtime_disabled"


def test_agent_runtime_reports_llm_unavailable_when_enabled_without_provider(tmp_path: Path) -> None:
    out = handle_agent_message(
        InboundRequest(
            text="帮我看看现在怎么样",
            sender_id="local",
            message_id="msg_llm_unavailable",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        settings=AgentRuntimeSettings(
            llm=LlmTranslatorSettings(enabled=True),
        ),
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "LLM_UNAVAILABLE"
    assert out["meta"]["agent_runtime"]["llm"]["enabled"] is True
    assert out["meta"]["agent_runtime"]["llm"]["reason"] == "missing_config"
    assert out["meta"]["agent_runtime"]["llm"]["missing"] == ["provider", "model"]


def test_agent_runtime_routes_valid_llm_translation_through_inbound_router(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    def _translate(
        text: str,
        settings: AgentRuntimeSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        assert text == "帮我看看这个月赚了多少"
        assert settings.llm.enabled is True
        assert conversation_context is not None
        assert conversation_context["scope"] == {
            "channel": "local",
            "sender_id": "local",
            "conversation_id": "local:local",
        }
        return LlmTranslationResult(
            intent=InboundIntent(
                name="monthly_income_report",
                arguments={"month": "2026-05"},
                parser="llm",
                confidence=0.92,
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
                "schema_version": LLM_INTENT_SCHEMA_VERSION,
            },
        )

    out = handle_agent_message(
        InboundRequest(
            text="帮我看看这个月赚了多少",
            sender_id="local",
            message_id="msg_llm_route",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AgentRuntimeSettings(
            llm=LlmTranslatorSettings(
                enabled=True,
                provider="openai",
                model="gpt-5.2",
            ),
        ),
        translate_intent_fn=_translate,
    )

    assert out["ok"] is True
    assert calls == [("monthly_income_report", {"config_key": "us", "month": "2026-05"})]
    assert out["data"]["intent"]["parser"] == "llm"
    assert out["meta"]["agent_runtime"]["route"] == "llm"
    assert out["meta"]["agent_runtime"]["llm"]["schema_version"] == LLM_INTENT_SCHEMA_VERSION
    assert out["meta"]["agent_runtime"]["context"] == {
        "provided": True,
        "window_messages": 8,
        "recent_count": 0,
        "pending_count": 0,
    }


def test_agent_runtime_builds_context_from_same_conversation(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict[str, Any]]] = []
    captured_context: dict[str, Any] | None = None

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    first = handle_agent_message(
        InboundRequest(
            text="/status",
            sender_id="ou_1",
            channel="feishu",
            conversation_id="feishu:chat_a:ou_1",
            message_id="msg_context_first",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute,
        allowed_senders="feishu:ou_1",
    )
    assert first["ok"] is True

    def _translate(
        text: str,
        settings: AgentRuntimeSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        nonlocal captured_context
        captured_context = conversation_context
        return LlmTranslationResult(
            intent=InboundIntent(name="runtime_status", arguments={}, parser="llm", confidence=0.91),
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
            },
        )

    second = handle_agent_message(
        InboundRequest(
            text="刚才那个再看一下",
            sender_id="ou_1",
            channel="feishu",
            conversation_id="feishu:chat_a:ou_1",
            message_id="msg_context_second",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute,
        allowed_senders="feishu:ou_1",
        settings=AgentRuntimeSettings(
            context_window_messages=4,
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        translate_intent_fn=_translate,
    )

    assert second["ok"] is True
    assert captured_context is not None
    assert captured_context["window_messages"] == 4
    assert [item["intent_name"] for item in captured_context["recent_messages"]] == ["runtime_status"]
    assert second["meta"]["agent_runtime"]["context"]["recent_count"] == 1


def test_agent_runtime_settings_from_runtime_config() -> None:
    assert AgentRuntimeSettings.from_runtime_config({}).enabled is True

    settings = AgentRuntimeSettings.from_runtime_config(
        {
            "assistant": {
                "mode": "llm_router",
                "context_window_messages": 12,
                "default_market_scope": "all",
                "llm": {
                    "provider": "openai",
                    "base_url": "https://llm.example/v1",
                    "model": "gpt-5.2",
                    "api_key_env": "OM_LLM_API_KEY",
                    "confidence_min": 0.8,
                    "timeout_seconds": 30,
                    "max_output_tokens": 768,
                },
            }
        }
    )

    assert settings.enabled is True
    assert settings.mode == "llm_router"
    assert settings.context_window_messages == 12
    assert settings.default_market_scope == "all"
    assert settings.llm.public_payload() == {
        "enabled": True,
        "provider": "openai",
        "base_url": "https://llm.example/v1",
        "model": "gpt-5.2",
        "api_key_env": "OM_LLM_API_KEY",
        "confidence_min": 0.8,
        "timeout_seconds": 30,
        "max_output_tokens": 768,
    }


def test_agent_runtime_agent_loop_is_bounded_read_only_router(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    def _translate(
        text: str,
        settings: AgentRuntimeSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        assert text == "帮我看一下状态"
        assert settings.mode == "agent_loop"
        assert conversation_context is not None
        return LlmTranslationResult(
            intent=InboundIntent(name="runtime_status", arguments={}, parser="llm", confidence=0.92),
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
            },
        )

    out = handle_agent_message(
        InboundRequest(
            text="帮我看一下状态",
            sender_id="local",
            message_id="msg_agent_loop",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AgentRuntimeSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        translate_intent_fn=_translate,
    )

    assert out["ok"] is True
    assert calls == [("runtime_status", {"config_key": "us"})]
    assert out["meta"]["agent_runtime"]["route"] == "agent_loop"
    assert out["meta"]["agent_runtime"]["langgraph"] == "optional"
    assert out["meta"]["agent_runtime"]["llm"]["agent_loop"] == {
        "enabled": True,
        "planner": "llm_read_only_intent",
        "max_steps": 2,
        "steps_used": 1,
        "writes_allowed": False,
    }


def test_llm_intent_schema_accepts_strict_read_only_payload() -> None:
    result = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "monthly_income_report",
            "arguments": {"account": "sy", "month": "2026-05"},
            "confidence": 0.91,
        },
        settings=LlmTranslatorSettings(enabled=True, confidence_min=0.75),
    )

    assert result.error is None
    assert result.intent is not None
    assert result.intent.name == "monthly_income_report"
    assert result.intent.arguments == {"account": "sy", "month": "2026-05"}
    assert result.intent.parser == "llm"
    assert result.trace["reason"] == "accepted"
    assert result.trace["schema_version"] == LLM_INTENT_SCHEMA_VERSION


def test_llm_intent_schema_ignores_null_argument_slots_from_provider() -> None:
    result = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "runtime_status",
            "arguments": {
                "account": None,
                "status": None,
                "month": None,
                "run_id": None,
                "kind": None,
                "limit": None,
                "lines": None,
            },
            "confidence": 0.91,
        },
        settings=LlmTranslatorSettings(enabled=True),
    )

    assert result.error is None
    assert result.intent is not None
    assert result.intent.name == "runtime_status"
    assert result.intent.arguments == {}


def test_llm_intent_schema_rejects_write_intents_and_extra_arguments() -> None:
    write_result = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "manual_trade_open",
            "arguments": {"raw_text": "记录开仓 sy NVDA"},
            "confidence": 0.95,
        },
        settings=LlmTranslatorSettings(enabled=True),
    )
    assert write_result.intent is None
    assert write_result.error is not None
    assert write_result.error.code == "PERMISSION_DENIED"

    extra_result = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "runtime_status",
            "arguments": {"shell": "rm -rf /"},
            "confidence": 0.95,
        },
        settings=LlmTranslatorSettings(enabled=True),
    )
    assert extra_result.intent is None
    assert extra_result.error is not None
    assert extra_result.error.code == "INPUT_ERROR"


def test_llm_intent_schema_rejects_low_confidence_and_missing_required_args() -> None:
    low_confidence = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "runtime_status",
            "arguments": {},
            "confidence": 0.6,
        },
        settings=LlmTranslatorSettings(enabled=True, confidence_min=0.75),
    )
    assert low_confidence.intent is None
    assert low_confidence.error is not None
    assert low_confidence.error.code == "NEEDS_CLARIFICATION"

    missing_run_id = parse_llm_translation_payload(
        {
            "schema_version": LLM_INTENT_SCHEMA_VERSION,
            "intent": "runtime_logs",
            "arguments": {},
            "confidence": 0.95,
        },
        settings=LlmTranslatorSettings(enabled=True),
    )
    assert missing_run_id.intent is None
    assert missing_run_id.error is not None
    assert missing_run_id.error.code == "NEEDS_CLARIFICATION"


def test_llm_intent_schema_documents_allowed_surface() -> None:
    schema = llm_intent_schema()

    assert schema["schema_version"] == LLM_INTENT_SCHEMA_VERSION
    assert schema["write_intents_allowed"] is False
    assert "manual_trade_open" not in schema["shape"]["intent"]
    assert schema["argument_keys"]["runtime_logs"] == ["kind", "lines", "run_id"]

    json_schema = llm_intent_json_schema()
    assert json_schema["additionalProperties"] is False
    assert "manual_trade_open" not in json_schema["properties"]["intent"]["enum"]
    assert json_schema["properties"]["arguments"]["additionalProperties"] is False
    assert set(json_schema["properties"]["arguments"]["required"]) == {
        "account",
        "status",
        "month",
        "run_id",
        "kind",
        "limit",
        "lines",
    }


def test_llm_translator_calls_openai_provider_and_parses_structured_response() -> None:
    calls: list[dict[str, Any]] = []

    def _create_response(**kwargs: object) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "output_text": json.dumps(
                {
                    "schema_version": LLM_INTENT_SCHEMA_VERSION,
                    "intent": "option_positions_open",
                    "arguments": {
                        "account": "sy",
                        "status": "open",
                        "month": None,
                        "run_id": None,
                        "kind": None,
                        "limit": None,
                        "lines": None,
                    },
                    "confidence": 0.93,
                }
            )
        }

    result = translate_inbound_intent(
        "帮我看 sy 的持仓",
        settings=LlmTranslatorSettings(
            enabled=True,
            provider="openai",
            base_url="https://llm.example/v1",
            model="gpt-5.2",
            timeout_seconds=9,
            max_output_tokens=777,
        ),
        environ={"OM_LLM_API_KEY": "sk-test"},
        create_response_fn=_create_response,
    )

    assert result.error is None
    assert result.intent is not None
    assert result.intent.name == "option_positions_open"
    assert result.intent.arguments == {"account": "sy", "status": "open"}
    assert result.trace["reason"] == "accepted"
    assert result.trace["provider"] == "openai"
    assert result.trace["base_url"] == "https://llm.example/v1"
    assert result.trace["timeout_seconds"] == 9
    assert result.trace["max_output_tokens"] == 777
    assert calls[0]["api_key"] == "sk-test"
    assert calls[0]["base_url"] == "https://llm.example/v1"
    assert calls[0]["model"] == "gpt-5.2"
    assert calls[0]["timeout"] == 9
    assert calls[0]["max_output_tokens"] == 777
    assert calls[0]["input_text"] == "帮我看 sy 的持仓"
    assert "Never execute tools" in str(calls[0]["instructions"])
    assert calls[0]["json_schema"]["properties"]["intent"]["enum"]


def test_llm_translator_calls_deepseek_provider_and_parses_chat_response() -> None:
    calls: list[dict[str, Any]] = []

    def _create_response(**kwargs: object) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": LLM_INTENT_SCHEMA_VERSION,
                                "intent": "option_positions_open",
                                "arguments": {
                                    "account": "sy",
                                    "status": "open",
                                    "month": None,
                                    "run_id": None,
                                    "kind": None,
                                    "limit": None,
                                    "lines": None,
                                },
                                "confidence": 0.93,
                            }
                        )
                    }
                }
            ]
        }

    result = translate_inbound_intent(
        "帮我看 sy 的持仓",
        settings=LlmTranslatorSettings(
            enabled=True,
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key_env="DEEPSEEK_API_KEY",
            timeout_seconds=9,
            max_output_tokens=777,
        ),
        environ={"DEEPSEEK_API_KEY": "sk-test"},
        create_response_fn=_create_response,
    )

    assert result.error is None
    assert result.intent is not None
    assert result.intent.name == "option_positions_open"
    assert result.intent.arguments == {"account": "sy", "status": "open"}
    assert result.trace["reason"] == "accepted"
    assert result.trace["provider"] == "deepseek"
    assert result.trace["base_url"] == "https://api.deepseek.com"
    assert result.trace["model"] == "deepseek-v4-flash"
    assert result.trace["api_key_env"] == "DEEPSEEK_API_KEY"
    assert calls[0]["api_key"] == "sk-test"
    assert calls[0]["base_url"] == "https://api.deepseek.com"
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert calls[0]["timeout"] == 9
    assert calls[0]["max_output_tokens"] == 777
    assert calls[0]["input_text"] == "帮我看 sy 的持仓"
    assert "Example JSON output" in str(calls[0]["instructions"])


def test_llm_translator_sends_structured_conversation_context() -> None:
    calls: list[dict[str, Any]] = []

    def _create_response(**kwargs: object) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "output_text": json.dumps(
                {
                    "schema_version": LLM_INTENT_SCHEMA_VERSION,
                    "intent": "runtime_status",
                    "arguments": {
                        "account": None,
                        "status": None,
                        "month": None,
                        "run_id": None,
                        "kind": None,
                        "limit": None,
                        "lines": None,
                    },
                    "confidence": 0.9,
                }
            )
        }

    result = translate_inbound_intent(
        "刚才那个呢",
        settings=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        environ={"OM_LLM_API_KEY": "sk-test"},
        conversation_context={
            "scope": {"channel": "feishu", "sender_id": "ou_1", "conversation_id": "feishu:chat:ou_1"},
            "window_messages": 3,
            "recent_messages": [{"raw_text": "状态", "intent_name": "runtime_status"}],
            "pending_operations": [
                {
                    "operation_id": "in_123",
                    "operation_type": "manual_open",
                    "summary": "sy NVDA call",
                    "conversation_id": "feishu:chat:ou_1",
                }
            ],
        },
        create_response_fn=_create_response,
    )

    assert result.error is None
    input_payload = json.loads(calls[0]["input_text"])
    assert input_payload["message"] == "刚才那个呢"
    assert "scope" not in input_payload["context"]
    assert input_payload["context"]["recent_messages"][0]["intent_name"] == "runtime_status"
    assert input_payload["context"]["pending_operations"][0] == {
        "operation_id": "in_123",
        "operation_type": "manual_open",
        "summary": "sy NVDA call",
        "status": None,
        "created_at": None,
        "expires_at": None,
    }
    assert result.trace["context"] == {
        "provided": True,
        "window_messages": 3,
        "recent_count": 1,
        "pending_count": 1,
    }


def test_llm_translator_requires_openai_api_key_before_provider_call() -> None:
    def _create_response(**_kwargs: object) -> dict[str, Any]:
        raise AssertionError("provider should not be called without API key")

    result = translate_inbound_intent(
        "帮我看持仓",
        settings=LlmTranslatorSettings(
            enabled=True,
            provider="openai",
            model="gpt-5.2",
        ),
        environ={},
        create_response_fn=_create_response,
    )

    assert result.intent is None
    assert result.error is not None
    assert result.error.code == "LLM_UNAVAILABLE"
    assert result.trace["reason"] == "missing_api_key"
    assert result.trace["missing"] == ["api_key"]


def test_llm_translator_reports_provider_errors_without_executing_tool() -> None:
    def _create_response(**_kwargs: object) -> dict[str, Any]:
        raise OpenAIResponsesError("quota exceeded", http_status=429)

    result = translate_inbound_intent(
        "帮我看持仓",
        settings=LlmTranslatorSettings(
            enabled=True,
            provider="openai",
            model="gpt-5.2",
        ),
        environ={"OM_LLM_API_KEY": "sk-test"},
        create_response_fn=_create_response,
    )

    assert result.intent is None
    assert result.error is not None
    assert result.error.code == "LLM_PROVIDER_ERROR"
    assert result.error.details == {"provider": "openai", "http_status": 429}
    assert result.trace["reason"] == "provider_error"


def test_openai_responses_client_builds_structured_output_request() -> None:
    calls: list[dict[str, Any]] = []

    def _post(
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "schema_version": LLM_INTENT_SCHEMA_VERSION,
                                    "intent": "runtime_status",
                                    "arguments": {},
                                    "confidence": 0.9,
                                }
                            ),
                        }
                    ]
                }
            ]
        }

    response = create_structured_response(
        api_key="sk-test",
        base_url="https://llm.example/v1",
        model="gpt-5.2",
        input_text="状态",
        instructions="translate only",
        json_schema=llm_intent_json_schema(),
        timeout=7,
        http_post_json_fn=_post,
    )

    assert calls[0]["url"] == "https://llm.example/v1/responses"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["payload"]["model"] == "gpt-5.2"
    assert calls[0]["payload"]["input"] == "状态"
    assert calls[0]["payload"]["store"] is False
    assert calls[0]["payload"]["temperature"] == 0.0
    assert calls[0]["payload"]["text"]["format"]["type"] == "json_schema"
    assert calls[0]["payload"]["text"]["format"]["strict"] is True
    assert calls[0]["payload"]["text"]["format"]["schema"]["properties"]["intent"]["enum"]
    assert calls[0]["timeout"] == 7
    assert "runtime_status" in extract_response_text(response)


def test_openai_responses_client_accepts_full_responses_url() -> None:
    calls: list[dict[str, Any]] = []

    def _post(
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {"output_text": "{}"}

    create_structured_response(
        api_key="sk-test",
        base_url="https://llm.example/v1/responses",
        model="gpt-5.2",
        input_text="状态",
        instructions="translate only",
        json_schema=llm_intent_json_schema(),
        http_post_json_fn=_post,
    )

    assert calls[0]["url"] == "https://llm.example/v1/responses"


def test_openai_chat_completions_client_builds_deepseek_json_request() -> None:
    calls: list[dict[str, Any]] = []

    def _post(
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": LLM_INTENT_SCHEMA_VERSION,
                                "intent": "runtime_status",
                                "arguments": {},
                                "confidence": 0.9,
                            }
                        )
                    }
                }
            ]
        }

    response = create_json_chat_completion(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        input_text="状态",
        instructions="translate only as json",
        json_schema=llm_intent_json_schema(),
        timeout=7,
        http_post_json_fn=_post,
    )

    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["payload"]["model"] == "deepseek-v4-flash"
    assert calls[0]["payload"]["messages"][0]["role"] == "system"
    assert calls[0]["payload"]["messages"][1] == {"role": "user", "content": "状态"}
    assert calls[0]["payload"]["max_tokens"] == 512
    assert calls[0]["payload"]["response_format"] == {"type": "json_object"}
    assert calls[0]["payload"]["thinking"] == {"type": "disabled"}
    assert calls[0]["payload"]["stream"] is False
    assert calls[0]["payload"]["temperature"] == 0.0
    assert calls[0]["timeout"] == 7
    assert "runtime_status" in extract_chat_completion_text(response)
