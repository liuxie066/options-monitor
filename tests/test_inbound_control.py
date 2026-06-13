from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.assistant.commands import command_specs
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import (
    AssistantRequest,
    PERCEPTION_RESULT_SCHEMA_VERSION,
    REASONING_RESOLUTION_SCHEMA_VERSION,
    PerceptionResult,
    ToolCall,
)
from src.application.assistant.llm_intent_schema import LLM_INTENT_SCHEMA_VERSION
from src.application.assistant.llm_translator import LlmTranslationResult, parse_llm_translation_payload
from src.application.inbound.feishu import feishu_payload_to_inbound_request, handle_feishu_payload
from src.application.assistant.deterministic_commands import parse_deterministic_text
from src.application.assistant.policy import PURE_READ_TOOLS, check_sender_allowed, enforce_tool_allowed
from src.application.assistant.renderer import render_inbound_text
from src.application.assistant.router import handle_assistant_request
from src.application.assistant.runtime import handle_assistant_message
from src.application.assistant.settings import AssistantSettings, LlmTranslatorSettings


def _write_inbound_runtime_config(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_runtime_cfg(str(data_cfg_path)), ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path, sqlite_path


def _write_symbols_runtime_config(tmp_path: Path) -> Path:
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_runtime_cfg(str(data_cfg_path)), ensure_ascii=False, indent=2), encoding="utf-8")
    hk_cfg_path = tmp_path / "config.hk.json"
    hk_cfg = _runtime_cfg(str(data_cfg_path), market="hk")
    hk_cfg["symbols"] = [
        {
            "symbol": "0883.HK",
            "fetch": {"source": "futu", "limit_expirations": 8},
            "use": ["put_base"],
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": False},
        }
    ]
    hk_cfg_path.write_text(json.dumps(hk_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def _runtime_cfg(data_config_ref: str, *, market: str = "us") -> dict:
    return {
        "_generated": {
            "schema_version": "1.0",
            "generator": "options-monitor",
            "source_format": "yaml",
            "market": market,
        },
        "_resolved": {
            "source_format": "yaml",
            "market": market,
            "runtime_schema": "config-json-v1",
        },
        "accounts": ["sy"],
        "portfolio": {
            "broker": "富途",
            "source": "futu",
            "account": "sy",
            "data_config": data_config_ref,
        },
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_annualized_net_return": 0.1,
                    "min_net_income": 50,
                    "min_open_interest": 10,
                    "min_volume": 1,
                    "max_spread_ratio": 0.3,
                }
            },
            "call_base": {"sell_call": {"strategy": "insurance_underwriting"}},
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "limit_expirations": 8},
                "use": ["put_base"],
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 45,
                    "min_strike": 100,
                    "max_strike": 120,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }


def _read_intent(intent_name: str, arguments: dict[str, Any] | None = None) -> PerceptionResult:
    return PerceptionResult(intent_name=intent_name, arguments=dict(arguments or {}), source="test")


def _enable_inbound_trade_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")


def _enable_inbound_symbol_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_SYMBOL_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")


def _enable_inbound_upgrade_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_UPGRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")


def _enable_inbound_model_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_MODEL_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")


def _write_assistant_model_config(tmp_path: Path) -> tuple[Path, Path]:
    config_yaml = tmp_path / "config.yaml"
    assistant_config = tmp_path / "config.assistant.json"
    config_yaml.write_text(
        """
assistant:
  enabled: true
  planner:
    enabled: true
  active_model: openai-default
  models:
    openai-default:
      provider: openai
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
""".lstrip(),
        encoding="utf-8",
    )
    assistant_config.write_text(
        json.dumps(
            {
                "assistant": {
                    "enabled": True,
                    "planner": {"enabled": True},
                    "llm": {
                        "provider": "openai",
                        "base_url": "https://api.openai.com/v1",
                        "model": "gpt-5.2",
                        "api_key_env": "OM_LLM_API_KEY",
                    },
                },
                "_resolved": {
                    "source_format": "yaml",
                    "config_yaml_path": str(config_yaml),
                    "runtime_schema": "assistant-config-json-v1",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_yaml, assistant_config


def test_inbound_command_surface_maps_core_read_only_commands() -> None:
    assert parse_assistant_command("/status").intent_name == "runtime_status"
    assert parse_assistant_command("/health").intent_name == "healthcheck"
    assert parse_deterministic_text("待确认").intent_name == "pending_operations"
    assert parse_deterministic_text("pending").intent_name == "pending_operations"

    for text in (
        "状态",
        "持仓 sy",
        "收益 本月",
        "日志 20260515T182459Z-474761",
        "查看监控标的",
        "现在泡泡玛特 sell put的max strike是多少？",
    ):
        try:
            parse_deterministic_text(text)
        except AgentToolError as err:
            assert err.code == "NEEDS_CLARIFICATION"
        else:
            raise AssertionError(f"{text} should not be routed by deterministic read parsing")

    positions = parse_assistant_command("/positions sy")
    assert positions.intent_name == "position_query"
    assert positions.arguments == {"account": "sy", "status": "open", "limit": 50}

    all_positions = parse_assistant_command("/positions")
    assert all_positions.intent_name == "position_query"
    assert all_positions.arguments == {"status": "open", "limit": 50}

    may_positions = parse_assistant_command("/positions sy 5月", now_fn=lambda: date(2026, 5, 19))
    assert may_positions.intent_name == "position_query"
    assert may_positions.arguments == {
        "account": "sy",
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }
    may_positions_without_account = parse_assistant_command("/positions 5月", now_fn=lambda: date(2026, 5, 19))
    assert may_positions_without_account.intent_name == "position_query"
    assert may_positions_without_account.arguments == {
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }

    income = parse_assistant_command("/income sy 本月", now_fn=lambda: date(2026, 5, 19))
    assert income.intent_name == "monthly_income_report"
    assert income.arguments == {"account": "sy", "month": "2026-05"}

    all_income = parse_assistant_command("/income 本月", now_fn=lambda: date(2026, 5, 19))
    assert all_income.intent_name == "monthly_income_report"
    assert all_income.arguments == {"month": "2026-05"}

    last_month = parse_assistant_command("/income lx 上月", now_fn=lambda: date(2026, 1, 3))
    assert last_month.arguments == {"account": "lx", "month": "2025-12"}

    june_income = parse_assistant_command("/income sy 6月", now_fn=lambda: date(2026, 6, 1))
    assert june_income.intent_name == "monthly_income_report"
    assert june_income.arguments == {"account": "sy", "month": "2026-06"}

    june_income_year = parse_assistant_command("/income sy 2026年6月", now_fn=lambda: date(2026, 6, 1))
    assert june_income_year.arguments == {"account": "sy", "month": "2026-06"}

    logs = parse_assistant_command("/logs 20260515T182459Z-474761")
    assert logs.intent_name == "runtime_logs"
    assert logs.arguments["run_id"] == "20260515T182459Z-474761"


def test_inbound_parser_requires_clarification_for_unknown_command() -> None:
    with pytest.raises(AgentToolError) as exc:
        parse_deterministic_text("查一下")

    assert exc.value.code == "NEEDS_CLARIFICATION"
    assert "没有识别" in exc.value.message


def test_inbound_model_command_lists_configured_profiles(tmp_path: Path) -> None:
    _config_yaml, assistant_config = _write_assistant_model_config(tmp_path)

    out = handle_assistant_request(
        AssistantRequest(
            text="/model",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_model_list",
            conversation_id="feishu:oc_1:ou_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
            assistant_config_path=str(assistant_config),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    assert out["data"]["perception"]["intent_name"] == "model_list"
    assert out["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.model"
    assert out["data"]["summary"]["active_model"] == "openai-default"
    assert {item["name"] for item in out["data"]["models"]} == {"openai-default", "deepseek-default"}
    assert "当前模型：openai-default" in out["data"]["response_text"]


def test_inbound_model_use_requires_preview_and_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_yaml, assistant_config = _write_assistant_model_config(tmp_path)
    _enable_inbound_model_write(monkeypatch)

    preview = handle_assistant_request(
        AssistantRequest(
            text="/model use deepseek-default",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_model_use",
            conversation_id="feishu:oc_1:ou_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
            assistant_config_path=str(assistant_config),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["data"]["status"] == "previewed"
    assert preview["data"]["operation_type"] == "model_use"
    assert "active_model: openai-default" in config_yaml.read_text(encoding="utf-8")
    assert "模型切换预览" in preview["data"]["response_text"]

    confirm = handle_assistant_request(
        AssistantRequest(
            text="确认模型",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_model_confirm",
            conversation_id="feishu:oc_1:ou_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
            assistant_config_path=str(assistant_config),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirm["ok"] is True
    assert confirm["data"]["status"] == "applied"
    assert confirm["data"]["result"]["active_model"] == "deepseek-default"
    assert "active_model: deepseek-default" in config_yaml.read_text(encoding="utf-8")
    generated = json.loads(assistant_config.read_text(encoding="utf-8"))
    assert generated["assistant"]["llm"]["provider"] == "deepseek"
    assert generated["assistant"]["llm"]["model"] == "deepseek-chat"


def test_inbound_policy_allows_sender_and_rejects_non_pure_read_tool() -> None:
    allowed = check_sender_allowed(channel="feishu", sender_id="ou_1", allowed_senders="feishu:ou_1")
    assert allowed.allowed is True

    denied = check_sender_allowed(channel="feishu", sender_id="ou_2", allowed_senders="feishu:ou_1")
    assert denied.allowed is False
    assert denied.reason == "sender_not_allowed"

    with pytest.raises(AgentToolError) as exc:
        enforce_tool_allowed(ToolCall(tool_name="scan_opportunities", payload={"config_key": "us"}))

    assert exc.value.code == "PERMISSION_DENIED"
    with pytest.raises(AgentToolError) as close_exc:
        enforce_tool_allowed(ToolCall(tool_name="get_close_advice", payload={"config_key": "us"}))

    assert close_exc.value.code == "PERMISSION_DENIED"
    assert "inbound.manual_trade" not in PURE_READ_TOOLS


def test_inbound_read_tool_requires_config_scope(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    out = handle_assistant_request(
        AssistantRequest(
            text="/status",
            sender_id="local",
            channel="local",
            message_id="msg_missing_config_scope",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="local:local",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert out["error"]["details"] == {"intent_name": "runtime_status", "required": "config_key_or_config_path"}
    assert calls == []


def test_inbound_exit_analysis_routes_to_close_advice_read(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "query": payload["query"],
                "source": {"run_id": "run-1", "paths": [".../close_advice.csv"]},
                "row_count": 2,
                "matched_count": 1,
                "returned_count": 1,
                "rows": [
                    {
                        "account": "lx",
                        "symbol": "FUTU",
                        "option_type": "call",
                        "side": "long",
                        "expiration": "2026-08-21",
                        "strike": 152.45,
                        "close_action": "hold_call_as_convexity",
                        "tier_label": "继续持有",
                        "reason": "long call 仍保留右尾 convexity，可继续持有",
                        "realized_if_close": 320,
                        "long_call_cost_basis": 500,
                        "long_call_current_value": 820,
                        "long_call_value_ratio": 1.64,
                        "capture_ratio": 0.42,
                    }
                ],
                "summary": {"tier_counts": {"none": 1}},
            },
        )

    def _translate(
        text: str,
        _settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        assert text == "分析 long call 是不是应该平仓"
        assert conversation_context is not None
        return LlmTranslationResult(
            intent=PerceptionResult(
                intent_name="position_exit_analysis",
                arguments={"status": "open", "option_type": "call", "side": "long", "limit": 50},
                source="llm",
                confidence=0.93,
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

    out = handle_assistant_message(
        AssistantRequest(
            text="分析 long call 是不是应该平仓",
            sender_id="local",
            channel="local",
            message_id="msg_exit_analysis",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="local:local",
        settings=AssistantSettings(
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        translate_intent_fn=_translate,
    )

    assert out["ok"] is True
    assert calls == [
        (
            "close_advice_read",
            {
                "config_key": "us",
                "market_scope": "all",
                "query": {"status": "open", "option_type": "call", "side": "long", "limit": 50},
            },
        )
    ]
    assert out["data"]["perception"]["intent_name"] == "position_exit_analysis"
    assert out["data"]["reasoning"]["tool_call"]["tool_name"] == "close_advice_read"
    assert "平仓建议分析" in out["data"]["response_text"]
    assert "继续持有 Call 凸性腿" in out["data"]["response_text"]
    assert "Call现值 820" in out["data"]["response_text"]
    assert "Call现值/成本 1.64" in out["data"]["response_text"]
    assert "数据源：run run-1" in out["data"]["response_text"]


def test_command_catalog_read_tool_names_match_inbound_policy() -> None:
    special_inbound_tools = {"inbound.pending", "inbound.symbols", "inbound.model"}
    for spec in command_specs():
        if not spec.read_only or not spec.tool_name:
            continue
        assert spec.tool_name in PURE_READ_TOOLS or spec.tool_name in special_inbound_tools


def test_inbound_parser_maps_manual_trade_and_symbol_operations() -> None:
    open_intent = parse_deterministic_text("记录开仓 sy 0700.HK short put strike 450 exp 2026-05-28 6张 premium 2.35 multiplier 100")
    assert open_intent.intent_name == "manual_trade_open"
    assert open_intent.arguments == {
        "raw_text": "记录开仓 sy 0700.HK short put strike 450 exp 2026-05-28 6张 premium 2.35 multiplier 100",
        "account": "sy",
    }

    close_intent = parse_deterministic_text("记录平仓 sy 0700.HK short put strike 450 exp 2026-05-28 2张 close 1.2")
    assert close_intent.intent_name == "manual_trade_close"
    assert close_intent.arguments == {
        "raw_text": "记录平仓 sy 0700.HK short put strike 450 exp 2026-05-28 2张 close 1.2",
        "account": "sy",
    }

    assert parse_deterministic_text("确认记录 in_abc123").arguments == {
        "operation_id": "in_abc123",
        "operation_resolution": "explicit",
    }
    english_confirm = parse_deterministic_text("confirm trade in_abc123")
    assert english_confirm.intent_name == "manual_trade_confirm"
    assert english_confirm.arguments == {
        "operation_id": "in_abc123",
        "operation_resolution": "explicit",
    }
    assert parse_deterministic_text("确认记录").arguments == {
        "operation_id": None,
        "operation_resolution": "latest_pending",
    }
    assert parse_deterministic_text("取消记录 in_abc123").intent_name == "manual_trade_cancel"
    assert parse_deterministic_text("取消交易 in_abc123").intent_name == "manual_trade_cancel"
    trade_update = parse_deterministic_text("premium 改成 2.75")
    assert trade_update.intent_name == "manual_trade_update"
    assert trade_update.arguments == {
        "operation_id": None,
        "operation_resolution": "latest_pending",
        "updates": {"premium_per_share": 2.75},
    }
    trade_update_with_id = parse_deterministic_text("合约数改成2 in_abc123")
    assert trade_update_with_id.arguments == {
        "operation_id": "in_abc123",
        "operation_resolution": "explicit",
        "updates": {"contracts": 2},
    }
    with pytest.raises(AgentToolError) as decimal_contracts:
        parse_deterministic_text("合约数改成1.9")
    assert decimal_contracts.value.code == "INPUT_ERROR"
    assert "整数参数不能写小数" in decimal_contracts.value.message

    assert parse_assistant_command("/symbols").intent_name == "symbol_list"
    symbol_add = parse_deterministic_text("增加监控标的 700 put")
    assert symbol_add.intent_name == "symbol_add"
    assert symbol_add.arguments == {"symbol": "700", "sell_put_enabled": True, "sell_call_enabled": False}
    symbol_edit = parse_deterministic_text("修改监控标的 HK.00700 sell_put.max_strike=480")
    assert symbol_edit.intent_name == "symbol_edit"
    assert symbol_edit.arguments == {"symbol": "HK.00700", "set": {"sell_put.max_strike": 480}}
    covered_call_edit = parse_deterministic_text("配置标的，为tigr做covered call的设置，min strike=6.5")
    assert covered_call_edit.intent_name == "symbol_edit"
    assert covered_call_edit.arguments == {
        "symbol": "tigr",
        "set": {"sell_call.enabled": True, "sell_call.min_strike": 6.5},
        "ensure_use": ["call_base"],
    }
    covered_call_setting = parse_deterministic_text("设置 09898 covered call min strike 85")
    assert covered_call_setting.intent_name == "symbol_edit"
    assert covered_call_setting.arguments == {
        "symbol": "09898",
        "set": {"sell_call.enabled": True, "sell_call.min_strike": 85.0},
        "ensure_use": ["call_base"],
    }
    assert parse_deterministic_text("删除监控标的 腾讯").arguments == {"symbol": "腾讯"}
    assert parse_deterministic_text("确认监控 in_abc123").intent_name == "symbol_confirm"
    assert parse_deterministic_text("cancel monitor in_abc123").intent_name == "symbol_cancel"
    upgrade = parse_deterministic_text("立即升级到 v1.2.111")
    assert upgrade.intent_name == "upgrade_now"
    assert upgrade.arguments == {"target_version": "1.2.111"}
    assert parse_deterministic_text("确认升级 in_abc123").intent_name == "upgrade_confirm"
    assert parse_deterministic_text("取消升级").intent_name == "upgrade_cancel"


def test_inbound_symbol_config_query_executes_read_only_tool_via_llm(tmp_path: Path) -> None:
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False), encoding="utf-8")
    cfg = _runtime_cfg(str(data_cfg_path), market="hk")
    cfg["symbols"] = [
        {
            "symbol": "9992.HK",
            "fetch": {"source": "futu", "limit_expirations": 8},
            "use": ["put_base"],
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 90, "max_strike": 145},
            "sell_call": {"enabled": False},
        }
    ]
    cfg_path = tmp_path / "config.hk.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _translate(
        text: str,
        runtime_settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        assert text == "现在泡泡玛特 sell put的max strike是多少？"
        assert conversation_context is not None
        return parse_llm_translation_payload(
            {
                "schema_version": LLM_INTENT_SCHEMA_VERSION,
                "intent": "symbol_config_query",
                "arguments": {"symbol": "泡泡玛特", "strategy": "sell_put", "field": "max_strike"},
                "confidence": 0.96,
            },
            settings=runtime_settings.llm,
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="现在泡泡玛特 sell put的max strike是多少？",
            sender_id="local",
            channel="local",
            message_id="msg_symbol_config_read",
            config_path=str(cfg_path),
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        allowed_senders="local:local",
        settings=AssistantSettings(
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        translate_intent_fn=_translate,
    )

    assert out["ok"] is True
    assert out["tool_name"] == "symbol_config_read"
    assert out["data"]["perception"]["intent_name"] == "symbol_config_query"
    assert out["data"]["reasoning"]["tool_call"] == {
        "tool_name": "symbol_config_read",
        "payload": {
            "config_path": str(cfg_path),
            "symbol": "泡泡玛特",
            "strategy": "sell_put",
            "field": "max_strike",
        },
    }
    assert "operation_type" not in out["data"]
    assert "9992.HK" in out["data"]["response_text"]
    assert "sell_put.max_strike" in out["data"]["response_text"]
    assert "145" in out["data"]["response_text"]


def test_inbound_request_reports_unwritable_audit_db(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "audit-parent-is-file"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    out = handle_assistant_request(
        AssistantRequest(
            text="状态",
            sender_id="local",
            message_id="msg_unwritable_audit",
            audit_db=str(blocked_parent / "inbound.sqlite3"),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "CONFIG_ERROR"
    assert out["error"]["message"] == "failed to open inbound audit SQLite database"
    assert "Set OM_INBOUND_AUDIT_DB" in out["error"]["hint"]
    assert out["meta"]["audit_db"] == ".../inbound.sqlite3"


def test_inbound_manual_trade_preview_and_confirm_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository
    from src.application.tool_execution import execute_tool as run_tool

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["tool_name"] == "inbound.manual_trade"
    assert preview["data"]["response_text"].startswith("交易记录预览：开仓")
    assert "未写入账本" in preview["data"]["response_text"]
    assert preview["data"]["payload"]["diagnostics"]["raw_symbol"] == "NVDA"
    assert preview["data"]["payload"]["diagnostics"]["multiplier_source"] == "payload"
    assert preview["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.manual_trade"
    assert preview["data"]["reasoning"]["safety_class"] == "write_preview"
    assert preview["data"]["reasoning"]["requires_confirmation"] is True
    assert preview["data"]["reasoning"]["intent_name"] == "manual_trade_open"

    operation_id = preview["data"]["operation_id"]
    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    assert confirmed["data"]["operation_id"] == operation_id
    assert confirmed["data"]["operation_resolution"] == "latest_pending"
    assert confirmed["data"]["resolved_operation_id"] == operation_id
    assert "交易已写入 OM 本地账本：开仓" in confirmed["data"]["response_text"]
    assert confirmed["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.manual_trade"
    assert confirmed["data"]["reasoning"]["safety_class"] == "write_apply"
    assert confirmed["data"]["reasoning"]["requires_confirmation"] is False
    assert confirmed["data"]["reasoning"]["intent_name"] == "manual_trade_confirm"
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert len(repo.list_trade_events()) == 1
    with sqlite3.connect(audit_db) as conn:
        rows = conn.execute(
            """
            SELECT message_id, reasoning_json
            FROM inbound_command_audit
            WHERE message_id IN ('msg_open_preview', 'msg_open_confirm')
            ORDER BY id
            """
        ).fetchall()
    audit_plans = {row[0]: json.loads(row[1]) for row in rows}
    assert audit_plans["msg_open_preview"]["reason"] == "write_preview_operation"
    assert audit_plans["msg_open_preview"]["arguments"]["account"] == "sy"
    assert audit_plans["msg_open_confirm"]["reason"] == "confirmed_write_operation"
    assert audit_plans["msg_open_confirm"]["arguments"] == {
        "operation_id": None,
        "operation_resolution": "latest_pending",
    }
    timeline_out = run_tool("operation_timeline", {"audit_db": str(audit_db), "operation_id": operation_id})
    assert timeline_out["ok"] is True
    assert timeline_out["data"]["schema_version"] == "operation-timeline-v1"
    assert timeline_out["data"]["timeline_count"] == 1
    timeline = timeline_out["data"]["timelines"][0]
    identity = timeline["identity"]
    assert identity["command_id"] == operation_id
    assert identity["operation_id"] == operation_id
    assert identity["inbound_message_id"] == "msg_open_preview"
    assert identity["channel"] == "feishu"
    assert identity["sender_id"] == "ou_1"
    assert identity["ledger_event_id"]
    assert identity["record_id"]
    assert timeline["operation"]["status"] == "applied"
    assert timeline["audit"]["apply_count"] == 1
    assert timeline["ledger"]["present"] is True
    assert "ledger_event_id_missing" not in timeline["warnings"]
    assert "record_id_missing" not in timeline["warnings"]
    assert "apply_audit_missing" not in timeline["warnings"]
    assert "receipt_not_observed" in timeline["warnings"]


def test_operation_timeline_reports_audit_only_operation_when_store_missing(tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore
    from src.application.assistant.operation_diagnostics import collect_operation_timeline

    audit_db = tmp_path / "audit_only.sqlite3"
    store = InboundAuditStore(audit_db)
    store.record_result(
        {
            "command_id": "in_preview",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:oc_1:ou_1",
            "message_id": "msg_preview",
            "raw_text": "记录开仓 sy NVDA short put",
            "parser": "rule",
            "intent_name": "manual_trade_open",
            "tool_name": "inbound.manual_trade",
            "reasoning": {"safety_class": "write_preview", "requires_confirmation": True},
            "decision": "executed",
            "result_ok": True,
            "response": {
                "ok": True,
                "data": {
                    "operation_id": "in_preview",
                    "operation_type": "manual_open",
                    "status": "previewed",
                    "response_text": "preview",
                },
            },
            "created_at": "2026-06-06T10:00:00+00:00",
            "finished_at": "2026-06-06T10:00:01+00:00",
        }
    )
    store.record_result(
        {
            "command_id": "in_confirm",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:oc_1:ou_1",
            "message_id": "msg_confirm",
            "raw_text": "确认记录",
            "parser": "rule",
            "intent_name": "manual_trade_confirm",
            "tool_name": "inbound.manual_trade",
            "reasoning": {"safety_class": "write_apply", "requires_confirmation": False},
            "decision": "executed",
            "result_ok": True,
            "response": {
                "ok": True,
                "data": {
                    "operation_id": "in_preview",
                    "resolved_operation_id": "in_preview",
                    "operation_type": "manual_open",
                    "status": "applied",
                    "result": {"result": {"event_id": "evt_1", "record_id": "lot_evt_1", "created": True}},
                    "reply": {
                        "schema_version": "feishu-reply-receipt-v1",
                        "inbound_message_id": "msg_confirm",
                        "message_id": "reply_1",
                        "outbound_message_id": "reply_1",
                        "delivery_confirmed": True,
                    },
                    "response_text": "applied",
                },
            },
            "created_at": "2026-06-06T10:01:00+00:00",
            "finished_at": "2026-06-06T10:01:01+00:00",
        }
    )
    store.record_result(
        {
            "command_id": "in_pending",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:oc_1:ou_1",
            "message_id": "msg_pending",
            "raw_text": "待确认",
            "parser": "rule",
            "intent_name": "pending_operations",
            "tool_name": "inbound.pending",
            "reasoning": {"safety_class": "read_only", "requires_confirmation": False},
            "decision": "executed",
            "result_ok": True,
            "response": {"ok": True, "data": {"pending_count": 0, "response_text": "none"}},
            "created_at": "2026-06-06T10:02:00+00:00",
            "finished_at": "2026-06-06T10:02:01+00:00",
        }
    )

    timeline_out = collect_operation_timeline(audit_db=str(audit_db), limit=10)

    assert timeline_out["timeline_count"] == 1
    assert "operations_table_missing" in timeline_out["warnings"]
    timeline = timeline_out["timelines"][0]
    assert timeline["operation"]["source"] == "audit_only"
    assert timeline["identity"]["operation_id"] == "in_preview"
    assert timeline["identity"]["ledger_event_id"] == "evt_1"
    assert timeline["identity"]["record_id"] == "lot_evt_1"
    assert timeline["identity"]["inbound_message_id"] == "msg_preview"
    assert timeline["identity"]["outbound_message_id"] == "reply_1"
    assert timeline["audit"]["related_count"] == 2
    assert timeline["audit"]["apply_count"] == 1
    assert timeline["receipt"]["status"] == "observed"
    assert timeline["receipt"]["message_id"] == "reply_1"
    assert "operation_store_missing" in timeline["warnings"]
    assert "operation_missing" in timeline["warnings"]
    assert "ledger_event_id_missing" not in timeline["warnings"]
    assert "record_id_missing" not in timeline["warnings"]
    assert "receipt_not_observed" not in timeline["warnings"]


def test_inbound_manual_trade_confirm_rejects_signature_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_preview_signed",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "different-operation-hmac-key")

    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_confirm_bad_signature",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is False
    assert confirmed["error"]["code"] == "PERMISSION_DENIED"
    assert "signature mismatch" in confirmed["error"]["message"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert repo.list_trade_events() == []


def test_inbound_write_policy_requires_hmac_and_explicit_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.application.assistant.operation_policy import enforce_trade_write_allowed

    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:*")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")

    with pytest.raises(AgentToolError) as wildcard:
        enforce_trade_write_allowed(channel="feishu", sender_id="ou_1")

    assert wildcard.value.code == "CONFIG_ERROR"
    assert "wildcard inbound operation admins are not allowed" in wildcard.value.message

    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.delenv("OM_INBOUND_OPERATION_HMAC_KEY", raising=False)

    with pytest.raises(AgentToolError) as missing_key:
        enforce_trade_write_allowed(channel="feishu", sender_id="ou_1")

    assert missing_key.value.code == "CONFIG_ERROR"
    assert "inbound operation HMAC key is not configured" in missing_key.value.message


def test_inbound_operation_confirm_claim_is_atomic(tmp_path: Path) -> None:
    from src.application.assistant.operation_store import InboundOperationStore

    store = InboundOperationStore(tmp_path / "inbound.sqlite3")
    store.save_preview(
        operation_id="in_atomic_claim",
        command_id="in_atomic_claim",
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        operation_type="manual_open",
        payload_hash="hash_1",
        payload={"operation_type": "manual_open", "arguments": {"account": "sy", "symbol": "NVDA"}},
        preview={"fields": {"account": "sy", "symbol": "NVDA"}},
        ttl_seconds=600,
    )

    assert store.mark_confirmed("in_atomic_claim") is True
    assert store.mark_confirmed("in_atomic_claim") is False
    operation = store.get("in_atomic_claim")
    assert operation is not None
    assert operation["status"] == "confirmed"


def test_inbound_operation_store_expires_previewed_records(tmp_path: Path) -> None:
    from src.application.assistant.operation_store import InboundOperationStore

    store = InboundOperationStore(tmp_path / "inbound.sqlite3")
    store.save_preview(
        operation_id="in_expire_preview",
        command_id="in_expire_preview",
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        operation_type="upgrade_now",
        payload_hash="hash_1",
        payload={"operation_type": "upgrade_now", "arguments": {"target_version": "1.2.164"}},
        preview={"summary": {"current_version": "1.2.163", "target_version": "1.2.164"}},
        ttl_seconds=60,
        created_at="2026-05-30T00:00:00+00:00",
    )

    now = datetime(2026, 5, 30, 0, 2, tzinfo=timezone.utc)
    pending = store.list_pending_operations(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        now=now,
    )

    assert pending == []
    operation = store.get("in_expire_preview")
    assert operation is not None
    assert operation["status"] == "expired"
    assert operation["result"]["reason"] == "confirmation_ttl_expired"

    expired = store.list_pending_operations(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        include_expired=True,
        now=now,
    )
    assert len(expired) == 1
    assert expired[0]["operation_id"] == "in_expire_preview"
    assert expired[0]["status"] == "expired"

    confirm_resolution = store.resolve_pending_operation(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        operation_types={"upgrade_now"},
        explicit_operation_id="in_expire_preview",
        allow_expired=False,
        now=now,
    )
    assert confirm_resolution["status"] == "expired"


def test_inbound_operation_store_fails_stale_confirmed_and_running_records(tmp_path: Path) -> None:
    from src.application.assistant.operation_store import InboundOperationStore

    store = InboundOperationStore(tmp_path / "inbound.sqlite3")
    for operation_id in ("in_stale_confirmed", "in_stale_running", "in_recent_confirmed"):
        store.save_preview(
            operation_id=operation_id,
            command_id=operation_id,
            channel="feishu",
            sender_id="ou_1",
            conversation_id="feishu:chat_a:ou_1",
            operation_type="upgrade_now",
            payload_hash=f"hash_{operation_id}",
            payload={"operation_type": "upgrade_now", "arguments": {"target_version": "1.2.164"}},
            preview={"summary": {"current_version": "1.2.163", "target_version": "1.2.164"}},
            ttl_seconds=600,
            created_at="2026-05-30T00:00:00+00:00",
        )

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
            UPDATE inbound_pending_operations
            SET status = 'confirmed',
                confirmed_at = '2026-05-30T00:10:00+00:00',
                result_json = NULL
            WHERE operation_id = 'in_stale_confirmed'
            """
        )
        conn.execute(
            """
            UPDATE inbound_pending_operations
            SET status = 'running',
                confirmed_at = '2026-05-30T00:20:00+00:00',
                result_json = ?
            WHERE operation_id = 'in_stale_running'
            """,
            (json.dumps({"status": "running"}),),
        )
        conn.execute(
            """
            UPDATE inbound_pending_operations
            SET status = 'confirmed',
                confirmed_at = '2026-05-30T01:55:00+00:00',
                result_json = ?
            WHERE operation_id = 'in_recent_confirmed'
            """,
            (json.dumps({"status": "confirmed"}),),
        )

    now = datetime(2026, 5, 30, 2, 0, tzinfo=timezone.utc)
    pending = store.list_pending_operations(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="feishu:chat_a:ou_1",
        now=now,
    )

    assert pending == []
    assert store.get("in_stale_confirmed")["status"] == "failed"  # type: ignore[index]
    assert store.get("in_stale_running")["status"] == "failed"  # type: ignore[index]
    assert store.get("in_recent_confirmed")["status"] == "confirmed"  # type: ignore[index]
    assert store.get("in_stale_confirmed")["result"]["reason"] == "operation_stale_after_confirmation"  # type: ignore[index]
    assert store.get("in_stale_running")["result"]["previous_status"] == "running"  # type: ignore[index]


def test_inbound_manual_trade_update_pending_preview_then_confirm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_update_open_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    operation_id = preview["data"]["operation_id"]

    updated = handle_assistant_request(
        AssistantRequest(
            text="premium 改成 2.75",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_update_open_premium",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert updated["ok"] is True
    assert updated["data"]["operation_id"] == operation_id
    assert updated["data"]["resolved_operation_id"] == operation_id
    assert updated["data"]["updated_fields"] == ["premium_per_share"]
    assert updated["data"]["payload"]["arguments"]["premium_per_share"] == 2.75
    assert updated["data"]["response_text"].startswith("交易记录预览已更新：开仓")
    assert "已修改：Premium=2.75" in updated["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert repo.list_trade_events() == []

    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_update_open_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    events = repo.list_trade_events()
    assert len(events) == 1
    assert events[0]["price"] == 2.75


def test_inbound_pending_operations_lists_current_conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_trade_write(monkeypatch)
    monkeypatch.setenv("OM_INBOUND_SYMBOL_WRITE_ENABLED", "1")
    cfg_path, _sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    trade_preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_pending_trade_preview",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    pending = handle_assistant_request(
        AssistantRequest(
            text="待确认",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_pending_list_one",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    trade_id = trade_preview["data"]["operation_id"]
    assert pending["ok"] is True
    assert pending["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.pending"
    assert pending["data"]["reasoning"]["read_only"] is True
    assert pending["data"]["reasoning"]["tool_call"]["payload"]["conversation_id"] == "feishu:chat_a:ou_1"
    assert pending["data"]["pending_count"] == 1
    assert pending["data"]["pending_operations"][0]["operation_id"] == trade_id
    assert "当前待确认：1 条" in pending["data"]["response_text"]
    assert "交易开仓" in pending["data"]["response_text"]
    assert "NVDA 2026-06-19 100.0P short put 1张 premium 2.5" in pending["data"]["response_text"]
    assert f"确认：确认记录 {trade_id}" in pending["data"]["response_text"]
    assert f"取消：取消记录 {trade_id}" in pending["data"]["response_text"]

    symbol_preview = handle_assistant_request(
        AssistantRequest(
            text="增加监控标的 TIGR put",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_pending_symbol_preview",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    pending_two = handle_assistant_request(
        AssistantRequest(
            text="pending operations",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_pending_list_two",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    symbol_id = symbol_preview["data"]["operation_id"]
    assert pending_two["ok"] is True
    assert pending_two["data"]["pending_count"] == 2
    assert "当前待确认：2 条" in pending_two["data"]["response_text"]
    assert "监控新增" in pending_two["data"]["response_text"]
    assert "add TIGR put" in pending_two["data"]["response_text"]
    assert f"确认：确认监控 {symbol_id}" in pending_two["data"]["response_text"]
    assert f"确认：确认记录 {trade_id}" in pending_two["data"]["response_text"]


def test_inbound_upgrade_preview_and_confirm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.assistant import upgrade_operations

    _enable_inbound_upgrade_write(monkeypatch)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_test_app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "cli_test_secret")
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[dict[str, object]] = []
    replies: list[dict[str, object]] = []

    def _fake_service_upgrade(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {
            "ok": True,
            "status": "upgraded" if kwargs.get("confirm") else "dry_run",
            "changed": bool(kwargs.get("confirm")),
            "current_version": "1.2.110",
            "target_version": kwargs.get("target_version") or "1.2.111",
            "release_tag": "v1.2.111",
            "repo_root": str(kwargs["repo_root"]),
            "runtime_root": str(kwargs["runtime_root"]),
            "planned_operations": ["materialize v1.2.111", "switch current symlink"],
        }

    def _fake_service_upgrade_check(**kwargs):  # type: ignore[no-untyped-def]
        calls.append({"check": True, **dict(kwargs)})
        return {
            "ok": True,
            "repo_root": str(kwargs["repo_root"]),
            "repo_root_resolved": str(kwargs["repo_root"]),
            "repo_root_resolution": {"status": "input"},
            "runtime_root": str(kwargs["runtime_root"]),
            "current_version": "1.2.110",
            "latest_version": "1.2.111",
            "release_tag": "v1.2.111",
            "upgrade_available": True,
            "version_check": {"ok": True},
        }

    monkeypatch.setattr(upgrade_operations, "service_upgrade_check", _fake_service_upgrade_check)
    monkeypatch.setattr(upgrade_operations, "service_upgrade", _fake_service_upgrade)
    monkeypatch.setattr(
        upgrade_operations,
        "UPGRADE_WORKER_LAUNCHER",
        lambda operation_id, audit_db: {"launcher": "test", "operation_id": operation_id, "audit_db": str(audit_db)},
    )

    preview = handle_assistant_request(
        AssistantRequest(
            text="立即升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_preview",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["tool_name"] == "inbound.upgrade"
    assert preview["data"]["response_text"].startswith("升级预览：立即升级")
    assert "未执行升级" in preview["data"]["response_text"]
    assert preview["data"]["payload"]["arguments"] == {"target_version": "1.2.111", "release_tag": "v1.2.111"}
    assert preview["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.upgrade"
    assert preview["data"]["reasoning"]["safety_class"] == "admin_preview"
    assert preview["data"]["reasoning"]["requires_confirmation"] is True
    assert calls[-1]["check"] is True

    operation_id = preview["data"]["operation_id"]
    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_confirm",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    assert confirmed["data"]["operation_id"] == operation_id
    assert confirmed["data"]["operation_resolution"] == "latest_pending"
    assert "已收到升级确认" in confirmed["data"]["response_text"]
    assert "当前版本：1.2.110" in confirmed["data"]["response_text"]
    assert "目标版本：1.2.111" in confirmed["data"]["response_text"]
    assert "当前版本：-" not in confirmed["data"]["response_text"]
    assert "目标版本：-" not in confirmed["data"]["response_text"]
    assert confirmed["data"]["reasoning"]["tool_call"]["tool_name"] == "inbound.upgrade"
    assert confirmed["data"]["reasoning"]["safety_class"] == "write_apply"
    assert confirmed["data"]["reasoning"]["requires_confirmation"] is False
    assert all("confirm" not in call for call in calls if "check" not in call)

    worker = upgrade_operations.run_confirmed_upgrade_operation(
        operation_id=operation_id,
        audit_db=audit_db,
        reply_fn=lambda **kwargs: replies.append(dict(kwargs)) or {"ok": True, "message_id": "reply_1"},
    )

    assert worker["ok"] is True
    assert worker["data"]["status"] == "applied"
    assert "升级执行完成" in worker["data"]["response_text"]
    assert "升级前版本：1.2.110" in worker["data"]["response_text"]
    assert "当前版本：1.2.111" in worker["data"]["response_text"]
    assert "当前版本：1.2.110" not in worker["data"]["response_text"]
    assert "状态：已执行完成" in worker["data"]["response_text"]
    assert "状态：applied" not in worker["data"]["response_text"]
    assert replies[-1]["message_id"] == "msg_upgrade_confirm"
    assert "升级执行完成" in str(replies[-1]["text"])
    assert "当前版本：1.2.111" in str(replies[-1]["text"])
    assert replies[-1]["uuid"] == f"{operation_id}:upgrade-final"
    assert calls[-1]["confirm"] is True
    assert calls[-1]["auto"] is True
    assert calls[-1]["target_version"] == "1.2.111"
    assert str(calls[-1]["runtime_root"]) == str(tmp_path / "runtime")


def test_inbound_upgrade_confirm_receipt_uses_payload_and_version_check_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application.assistant import upgrade_operations
    from src.application.assistant.operation_signature import hash_operation_payload
    from src.application.assistant.operation_store import InboundOperationStore

    _enable_inbound_upgrade_write(monkeypatch)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    audit_db = tmp_path / "inbound.sqlite3"

    monkeypatch.setattr(
        upgrade_operations,
        "service_upgrade_check",
        lambda **kwargs: {
            "ok": True,
            "repo_root": str(kwargs["repo_root"]),
            "repo_root_resolved": str(kwargs["repo_root"]),
            "repo_root_resolution": {"status": "input"},
            "runtime_root": str(kwargs["runtime_root"]),
            "current_version": "1.2.110",
            "latest_version": "1.2.111",
            "release_tag": "v1.2.111",
            "upgrade_available": True,
            "version_check": {"ok": True},
        },
    )
    monkeypatch.setattr(
        upgrade_operations,
        "UPGRADE_WORKER_LAUNCHER",
        lambda operation_id, audit_db: {"launcher": "test", "operation_id": operation_id},
    )

    preview = handle_assistant_request(
        AssistantRequest(
            text="立即升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_preview",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    operation_id = preview["data"]["operation_id"]
    payload = preview["data"]["payload"]
    assert payload["arguments"]["target_version"] == "1.2.111"

    InboundOperationStore(audit_db).update_preview(
        operation_id,
        payload_hash=hash_operation_payload(payload),
        payload=payload,
        preview={
            "upgrade": {
                "status": "dry_run",
                "version_check": {
                    "current_version": "1.2.110",
                    "latest_version": "1.2.999",
                    "release_tag": "v1.2.999",
                },
            }
        },
    )

    confirmed = handle_assistant_request(
        AssistantRequest(
            text=f"确认升级 {operation_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_confirm",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    response_text = confirmed["data"]["response_text"]
    assert confirmed["ok"] is True
    assert "当前版本：1.2.110" in response_text
    assert "目标版本：1.2.111" in response_text
    assert "目标版本：1.2.999" not in response_text
    assert "当前版本：-" not in response_text
    assert "目标版本：-" not in response_text


def test_inbound_upgrade_worker_retries_final_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.assistant import upgrade_operations

    _enable_inbound_upgrade_write(monkeypatch)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_test_app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "cli_test_secret")
    audit_db = tmp_path / "inbound.sqlite3"
    sleeps: list[float] = []
    reply_attempts: list[dict[str, object]] = []

    monkeypatch.setattr(
        upgrade_operations,
        "service_upgrade_check",
        lambda **kwargs: {
            "ok": True,
            "repo_root": str(kwargs["repo_root"]),
            "repo_root_resolved": str(kwargs["repo_root"]),
            "repo_root_resolution": {"status": "input"},
            "runtime_root": str(kwargs["runtime_root"]),
            "current_version": "1.2.110",
            "latest_version": "1.2.111",
            "release_tag": "v1.2.111",
            "upgrade_available": True,
            "version_check": {"ok": True},
        },
    )
    monkeypatch.setattr(
        upgrade_operations,
        "service_upgrade",
        lambda **kwargs: {
            "ok": True,
            "status": "upgraded",
            "changed": True,
            "current_version": "1.2.110",
            "target_version": kwargs.get("target_version") or "1.2.111",
            "release_tag": "v1.2.111",
            "repo_root": str(kwargs["repo_root"]),
            "runtime_root": str(kwargs["runtime_root"]),
        },
    )
    monkeypatch.setattr(
        upgrade_operations,
        "UPGRADE_WORKER_LAUNCHER",
        lambda operation_id, audit_db: {"launcher": "test", "operation_id": operation_id},
    )
    monkeypatch.setattr(upgrade_operations.time, "sleep", lambda seconds: sleeps.append(float(seconds)))

    preview = handle_assistant_request(
        AssistantRequest(
            text="立即升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_preview",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    operation_id = preview["data"]["operation_id"]
    confirmed = handle_assistant_request(
        AssistantRequest(
            text=f"确认升级 {operation_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_confirm",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert confirmed["ok"] is True

    def _reply_fn(**kwargs):  # type: ignore[no-untyped-def]
        reply_attempts.append(dict(kwargs))
        if len(reply_attempts) == 1:
            raise RuntimeError("temporary feishu reply failure")
        return {"ok": True, "message_id": "reply_final"}

    worker = upgrade_operations.run_confirmed_upgrade_operation(
        operation_id=operation_id,
        audit_db=audit_db,
        reply_fn=_reply_fn,
    )

    final_receipt = worker["data"]["result"]["final_receipt"]
    assert worker["ok"] is True
    assert final_receipt["ok"] is True
    assert final_receipt["attempts"] == 2
    assert final_receipt["previous_errors"][0]["error"] == "RuntimeError: temporary feishu reply failure"
    assert sleeps == [1.0]
    assert len(reply_attempts) == 2
    assert reply_attempts[-1]["message_id"] == "msg_upgrade_confirm"
    assert reply_attempts[-1]["uuid"] == f"{operation_id}:upgrade-final"
    assert "升级执行完成" in str(reply_attempts[-1]["text"])


def test_inbound_upgrade_returns_no_upgrade_without_pending_operation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.assistant import upgrade_operations

    _enable_inbound_upgrade_write(monkeypatch)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[dict[str, object]] = []

    def _fake_service_upgrade_check(**kwargs):  # type: ignore[no-untyped-def]
        calls.append({"check": True, **dict(kwargs)})
        return {
            "ok": True,
            "status": "no_upgrade_available",
            "repo_root": str(kwargs["repo_root"]),
            "repo_root_resolved": str(kwargs["repo_root"]),
            "repo_root_resolution": {"status": "input"},
            "runtime_root": str(kwargs["runtime_root"]),
            "current_version": "1.2.111",
            "latest_version": "1.2.111",
            "release_tag": "v1.2.111",
            "upgrade_available": False,
            "message": "没有可升级版本。当前已是最新版本 1.2.111",
            "version_check": {"ok": True, "update_available": False},
        }

    monkeypatch.setattr(upgrade_operations, "service_upgrade_check", _fake_service_upgrade_check)
    monkeypatch.setattr(
        upgrade_operations,
        "service_upgrade",
        lambda **kwargs: pytest.fail(f"unexpected upgrade apply: {kwargs}"),
    )
    monkeypatch.setattr(
        upgrade_operations,
        "UPGRADE_WORKER_LAUNCHER",
        lambda operation_id, audit_db: pytest.fail(f"unexpected upgrade worker: {operation_id}"),
    )

    preview = handle_assistant_request(
        AssistantRequest(
            text="立即升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_noop",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["tool_name"] == "inbound.upgrade"
    assert preview["data"]["status"] == "no_upgrade_available"
    assert "没有可升级版本" in preview["data"]["response_text"]
    assert "确认执行" not in preview["data"]["response_text"]
    assert "operation_id" not in preview["data"]
    assert calls[-1]["check"] is True

    pending = handle_assistant_request(
        AssistantRequest(
            text="待确认",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_noop_pending",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert pending["ok"] is True
    assert pending["data"]["pending_count"] == 0
    assert pending["data"]["response_text"] == "当前对话没有待确认操作。"


def test_inbound_upgrade_reconfirm_hides_internal_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.assistant import upgrade_operations

    _enable_inbound_upgrade_write(monkeypatch)
    audit_db = tmp_path / "inbound.sqlite3"

    monkeypatch.setattr(
        upgrade_operations,
        "service_upgrade_check",
        lambda **kwargs: {
            "ok": True,
            "repo_root": str(kwargs["repo_root"]),
            "repo_root_resolved": str(kwargs["repo_root"]),
            "repo_root_resolution": {"status": "input"},
            "runtime_root": str(kwargs["runtime_root"]),
            "current_version": "1.2.110",
            "latest_version": "1.2.111",
            "release_tag": "v1.2.111",
            "upgrade_available": True,
            "version_check": {"ok": True},
        },
    )
    monkeypatch.setattr(
        upgrade_operations,
        "UPGRADE_WORKER_LAUNCHER",
        lambda operation_id, audit_db: {"launcher": "test", "operation_id": operation_id},
    )

    preview = handle_assistant_request(
        AssistantRequest(
            text="立即升级",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_preview",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    operation_id = preview["data"]["operation_id"]

    first = handle_assistant_request(
        AssistantRequest(
            text=f"确认升级 {operation_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_confirm_1",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert first["ok"] is True

    second = handle_assistant_request(
        AssistantRequest(
            text=f"确认升级 {operation_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_upgrade_confirm_2",
            conversation_id="feishu:chat_a:ou_1",
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert second["ok"] is False
    response_text = str(second["data"]["response_text"])
    assert "当前进度：确认已收到，正在执行或等待结果" in response_text
    assert "confirmed" not in response_text


def test_upgrade_worker_launcher_passes_env_file_pointer_to_systemd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    from src.application.assistant import upgrade_operations

    root = tmp_path / "repo"
    root.mkdir()
    (root / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_FEISHU_BOT_APP_SECRET=secret-from-file\n", encoding="utf-8")
    captured: list[list[str]] = []

    monkeypatch.setattr(upgrade_operations, "repo_base", lambda: root)
    monkeypatch.setattr(upgrade_operations.sys, "platform", "linux")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setattr(upgrade_operations.shutil, "which", lambda name: "/bin/systemd-run" if name == "systemd-run" else "/usr/bin/sudo" if name == "sudo" else None)

    def _fake_run(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(upgrade_operations.subprocess, "run", _fake_run)

    out = upgrade_operations._default_upgrade_worker_launcher("in_abc", tmp_path / "audit.sqlite3")

    assert out["launcher"] == "systemd-run"
    assert out["env_keys"] == ["OM_ENV_FILE", "OM_RUNTIME_ROOT", "PYTHONPATH", "PYTHONUNBUFFERED"]
    first = captured[0]
    assert "--setenv" in first
    assert f"OM_ENV_FILE={env_file}" in first
    assert f"OM_RUNTIME_ROOT={runtime}" in first
    assert f"PYTHONPATH={root}" in first
    assert "secret-from-file" not in " ".join(first)


def test_upgrade_worker_launcher_falls_back_to_service_profile_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    from src.application.assistant import upgrade_operations

    root = tmp_path / "repo"
    root.mkdir()
    (root / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = tmp_path / "service.env"
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_test\nOM_FEISHU_BOT_APP_SECRET=secret\n", encoding="utf-8")
    (runtime / "service.profile.json").write_text(
        json.dumps({"runtime_root": str(runtime), "env_file": str(env_file)}, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    monkeypatch.setattr(upgrade_operations, "repo_base", lambda: root)
    monkeypatch.setattr(upgrade_operations.sys, "platform", "linux")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.setattr(upgrade_operations.shutil, "which", lambda name: "/bin/systemd-run" if name == "systemd-run" else None)

    def _fake_run(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(upgrade_operations.subprocess, "run", _fake_run)

    out = upgrade_operations._default_upgrade_worker_launcher("in_def", tmp_path / "audit.sqlite3")

    assert out["launcher"] == "systemd-run"
    first = captured[0]
    assert f"OM_ENV_FILE={env_file}" in first
    assert "OM_FEISHU_BOT_APP_SECRET=secret" not in " ".join(first)


def test_inbound_manual_trade_bare_confirm_requires_unique_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    first = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_open_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    second = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 101 exp 2026-06-19 1张 premium 2.4 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_open_2",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    out = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert "有多条待确认的交易记录" in out["data"]["response_text"]
    assert "请带 operation_id。\n候选交易：" in out["data"]["response_text"]
    assert first["data"]["operation_id"] in out["data"]["response_text"]
    assert second["data"]["operation_id"] in out["data"]["response_text"]
    assert "候选交易" in out["data"]["response_text"]
    assert "NVDA 2026-06-19 100.0P short put 1张 premium 2.5" in out["data"]["response_text"]
    assert "NVDA 2026-06-19 101.0P short put 1张 premium 2.4" in out["data"]["response_text"]
    assert f"回复：确认记录 {first['data']['operation_id']}" in out["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert repo.list_trade_events() == []


def test_inbound_manual_trade_update_requires_unique_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    first = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_update_open_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    second = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 101 exp 2026-06-19 1张 premium 2.4 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_update_open_2",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    out = handle_assistant_request(
        AssistantRequest(
            text="premium 改成 2.75",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_ambiguous_update",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert "有多条待修改的交易记录" in out["data"]["response_text"]
    assert "请在修改内容后带 operation_id" in out["data"]["response_text"]
    assert "\n候选交易：" in out["data"]["response_text"]
    assert first["data"]["operation_id"] in out["data"]["response_text"]
    assert second["data"]["operation_id"] in out["data"]["response_text"]
    assert "候选交易" in out["data"]["response_text"]
    assert "premium 2.5" in out["data"]["response_text"]
    assert "premium 2.4" in out["data"]["response_text"]
    assert "premium 改成 2.35 <operation_id>" in out["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert repo.list_trade_events() == []


def test_inbound_bare_symbol_confirm_does_not_confirm_manual_trade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    monkeypatch.setenv("OM_INBOUND_SYMBOL_WRITE_ENABLED", "1")
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_cross_family_trade_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert preview["ok"] is True

    out = handle_assistant_request(
        AssistantRequest(
            text="确认监控",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_cross_family_symbol_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "NEEDS_CLARIFICATION"
    assert "没有可确认的监控标的变更" in out["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert repo.list_trade_events() == []


def test_inbound_bare_confirm_is_scoped_to_conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_conversation_preview",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert preview["ok"] is True

    wrong_chat_pending = handle_assistant_request(
        AssistantRequest(
            text="待确认",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_conversation_wrong_chat_pending",
            conversation_id="feishu:chat_b:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert wrong_chat_pending["ok"] is True
    assert wrong_chat_pending["data"]["pending_count"] == 0
    assert wrong_chat_pending["data"]["response_text"] == "当前对话没有待确认操作。"

    right_chat_pending = handle_assistant_request(
        AssistantRequest(
            text="当前预览",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_conversation_right_chat_pending",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert right_chat_pending["ok"] is True
    assert right_chat_pending["data"]["pending_count"] == 1
    assert right_chat_pending["data"]["pending_operations"][0]["operation_id"] == preview["data"]["operation_id"]

    wrong_chat = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_conversation_wrong_chat",
            conversation_id="feishu:chat_b:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert wrong_chat["ok"] is False
    assert "没有可确认的交易记录" in wrong_chat["data"]["response_text"]

    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认记录",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_conversation_right_chat",
            conversation_id="feishu:chat_a:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert confirmed["ok"] is True
    assert confirmed["data"]["operation_id"] == preview["data"]["operation_id"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert len(repo.list_trade_events()) == 1


def test_inbound_manual_trade_preview_canonicalizes_symbol_and_keeps_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_trade_write(monkeypatch)
    cfg_path, _sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    def _fake_resolve(**_kwargs: object) -> tuple[int, str, dict]:
        return 500, "cache", {"attempted_sources": [{"source": "cache", "status": "resolved", "value": 500}]}

    monkeypatch.setattr("src.application.assistant.manual_trade_parser.resolve_multiplier_with_source_and_diagnostics", _fake_resolve)

    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy 腾讯 short put strike 450 exp 2026-05-28 6张 premium 2.35",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_tencent_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    payload = preview["data"]["payload"]
    assert payload["arguments"]["symbol"] == "0700.HK"
    assert payload["arguments"]["multiplier"] == 500.0
    assert payload["diagnostics"]["raw_symbol"] == "腾讯"
    assert payload["diagnostics"]["canonical_symbol"] == "0700.HK"
    assert payload["diagnostics"]["multiplier_resolution_attempts"][0]["source"] == "cache"

    with sqlite3.connect(audit_db) as conn:
        response_json = conn.execute("SELECT response_json FROM inbound_command_audit").fetchone()[0]
    stored = json.loads(response_json)
    assert stored["data"]["payload"]["diagnostics"]["canonical_symbol"] == "0700.HK"


def test_inbound_manual_trade_preview_and_confirm_close(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    open_preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy 0700.HK short put strike 450 exp 2026-06-19 2张 premium 2.5 multiplier 500",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_open_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    open_id = open_preview["data"]["operation_id"]
    handle_assistant_request(
        AssistantRequest(
            text=f"确认记录 {open_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_open_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    close_preview = handle_assistant_request(
        AssistantRequest(
            text="记录平仓 sy HK.00700 short put strike 450 exp 2026-06-19 1张 close 1.0",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert close_preview["ok"] is True
    assert close_preview["data"]["response_text"].startswith("交易记录预览：平仓")
    assert close_preview["data"]["payload"]["arguments"]["symbol"] == "0700.HK"
    assert close_preview["data"]["payload"]["diagnostics"]["raw_symbol"] == "HK.00700"
    assert close_preview["data"]["payload"]["diagnostics"]["canonical_symbol"] == "0700.HK"

    close_id = close_preview["data"]["operation_id"]
    confirmed = handle_assistant_request(
        AssistantRequest(
            text=f"确认记录 {close_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    assert "交易已写入 OM 本地账本：平仓" in confirmed["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert len(repo.list_trade_events()) == 2


def test_inbound_symbol_add_edit_remove_preview_and_confirm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_symbol_write(monkeypatch)
    cfg_path = _write_symbols_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    listed = handle_assistant_request(
        AssistantRequest(text="/symbols", sender_id="ou_1", channel="feishu", message_id="msg_symbol_list", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert listed["ok"] is True
    assert listed["tool_name"] == "inbound.symbols"
    assert "当前监控标的" in listed["data"]["response_text"]

    add_preview = handle_assistant_request(
        AssistantRequest(text="增加监控标的 700 put", sender_id="ou_1", channel="feishu", message_id="msg_symbol_add", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert add_preview["ok"] is True
    assert "校准为：0700.HK" in add_preview["data"]["response_text"]
    add_id = add_preview["data"]["operation_id"]
    add_confirm = handle_assistant_request(
        AssistantRequest(text="确认监控", sender_id="ou_1", channel="feishu", message_id="msg_symbol_add_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert add_confirm["ok"] is True
    assert add_confirm["data"]["operation_id"] == add_id
    assert add_confirm["data"]["operation_resolution"] == "latest_pending"
    assert add_confirm["data"]["resolved_operation_id"] == add_id

    edit_preview = handle_assistant_request(
        AssistantRequest(text="修改监控标的 HK.00700 sell_put.max_strike=480", sender_id="ou_1", channel="feishu", message_id="msg_symbol_edit", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    edit_id = edit_preview["data"]["operation_id"]
    edit_confirm = handle_assistant_request(
        AssistantRequest(text=f"确认监控 {edit_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_edit_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert edit_confirm["ok"] is True

    covered_call_preview = handle_assistant_request(
        AssistantRequest(text="配置标的，为NVDA做covered call的设置，min strike=140", sender_id="ou_1", channel="feishu", message_id="msg_symbol_covered_call", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert covered_call_preview["ok"] is True
    covered_call_id = covered_call_preview["data"]["operation_id"]
    covered_call_confirm = handle_assistant_request(
        AssistantRequest(text=f"确认监控 {covered_call_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_covered_call_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert covered_call_confirm["ok"] is True

    remove_preview = handle_assistant_request(
        AssistantRequest(text="删除监控标的 腾讯", sender_id="ou_1", channel="feishu", message_id="msg_symbol_remove", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    remove_id = remove_preview["data"]["operation_id"]
    remove_confirm = handle_assistant_request(
        AssistantRequest(text=f"确认监控 {remove_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_remove_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert remove_confirm["ok"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in current["symbols"]] == ["NVDA"]
    nvda = current["symbols"][0]
    assert nvda["use"] == ["put_base", "call_base"]
    assert nvda["sell_call"]["enabled"] is True
    assert nvda["sell_call"]["min_strike"] == 140.0


def test_inbound_llm_symbol_edit_creates_preview_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_symbol_write(monkeypatch)
    cfg_path = _write_symbols_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"
    before = json.loads(cfg_path.read_text(encoding="utf-8"))

    def _translate(
        text: str,
        runtime_settings: AssistantSettings,
        conversation_context: dict[str, Any] | None,
    ) -> LlmTranslationResult:
        assert text == "设置 NVDA covered call min strike 140"
        assert conversation_context is not None
        return parse_llm_translation_payload(
            {
                "schema_version": LLM_INTENT_SCHEMA_VERSION,
                "intent": "symbol_edit",
                "arguments": {
                    "symbol": "NVDA",
                    "set": {"covered_call.min_strike": 140},
                    "ensure_use": None,
                },
                "confidence": 0.96,
            },
            settings=runtime_settings.llm,
        )

    out = handle_assistant_message(
        AssistantRequest(
            text="设置 NVDA covered call min strike 140",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_llm_symbol_edit_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
        settings=AssistantSettings(
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        translate_intent_fn=_translate,
    )

    assert out["ok"] is True
    assert out["tool_name"] == "inbound.symbols"
    assert out["data"]["perception"]["source"] == "llm"
    assert out["data"]["perception"]["intent_name"] == "symbol_edit"
    assert out["data"]["reasoning"]["status"] == "preview_required"
    assert out["data"]["reasoning"]["safety_class"] == "write_preview"
    assert out["data"]["reasoning"]["requires_confirmation"] is True
    assert out["data"]["status"] == "previewed"
    assert out["data"]["operation_type"] == "symbol_edit"
    assert out["data"]["payload"]["arguments"] == {
        "symbol": "NVDA",
        "set": {"sell_call.min_strike": 140.0, "sell_call.enabled": True},
        "ensure_use": ["call_base"],
    }
    assert "未写入配置" in out["data"]["response_text"]
    assert "确认监控" in out["data"]["response_text"]
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == before

    assistant_meta = out["meta"]["assistant"]
    assert assistant_meta["route"] == "agent_loop"
    assert assistant_meta["decision"]["execution_contract"] == {
        "read_only": False,
        "risk_level": "preview_write",
        "operation_action": "preview",
        "operation_target": "symbol",
        "llm_allowed": True,
        "supported": True,
        "direct_writes_allowed": False,
        "llm_write_allowed": False,
        "preview_confirm_required": True,
        "canonical_renderer_required": True,
    }
    assert assistant_meta["perception_trace"]["decision"] == "agent_loop_selected"


def test_inbound_symbol_write_uses_symbol_market_over_default_us_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_symbol_write(monkeypatch)
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    us_cfg_path = tmp_path / "config.us.json"
    hk_cfg_path = tmp_path / "config.hk.json"
    us_cfg = _runtime_cfg(str(data_cfg_path), market="us")
    hk_cfg = _runtime_cfg(str(data_cfg_path), market="hk")
    hk_cfg["symbols"] = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "limit_expirations": 8},
            "use": ["put_base"],
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    us_cfg_path.write_text(json.dumps(us_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    hk_cfg_path.write_text(json.dumps(hk_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="修改监控标的 HK.00700 sell_put.max_strike=480",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_symbol_hk_edit",
            config_path=str(us_cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["data"]["payload"]["config"]["config_key"] == "hk"
    assert preview["data"]["payload"]["config"]["config_path"] == str(hk_cfg_path)
    assert "校准为：0700.HK" in preview["data"]["response_text"]
    assert f"配置：{hk_cfg_path}" in preview["data"]["response_text"]

    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认监控",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_symbol_hk_edit_confirm",
            config_path=str(us_cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    current_us = json.loads(us_cfg_path.read_text(encoding="utf-8"))
    current_hk = json.loads(hk_cfg_path.read_text(encoding="utf-8"))
    assert current_us["symbols"][0]["symbol"] == "NVDA"
    assert current_hk["symbols"][0]["sell_put"]["max_strike"] == 480


def test_inbound_symbol_setting_writes_yaml_and_rebuilds_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.config_yaml import build_yaml_runtime_config_file

    _enable_inbound_symbol_write(monkeypatch)
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
  hk:
    accounts: [lx]
    symbols: ["0700.HK"]
""",
        encoding="utf-8",
    )
    us_cfg_path = tmp_path / "config.us.json"
    hk_cfg_path = tmp_path / "config.hk.json"
    build_yaml_runtime_config_file(repo_root=Path(__file__).resolve().parents[1], market="us", config_path=config_yaml, output_config_path=us_cfg_path)
    build_yaml_runtime_config_file(repo_root=Path(__file__).resolve().parents[1], market="hk", config_path=config_yaml, output_config_path=hk_cfg_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_assistant_request(
        AssistantRequest(
            text="设置 09898 covered call min strike 85",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_yaml_symbol_setting",
            config_path=str(us_cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["data"]["payload"]["config"]["source_format"] == "yaml"
    assert preview["data"]["payload"]["config"]["config_yaml_path"] == str(config_yaml)
    assert preview["data"]["payload"]["config"]["market"] == "hk"
    assert "校准为：9898.HK" in preview["data"]["response_text"]
    assert f"配置：{config_yaml}" in preview["data"]["response_text"]
    assert "9898.HK" not in config_yaml.read_text(encoding="utf-8")

    confirmed = handle_assistant_request(
        AssistantRequest(
            text="确认监控",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_yaml_symbol_setting_confirm",
            config_path=str(us_cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    updated_yaml = config_yaml.read_text(encoding="utf-8")
    assert "9898.HK" in updated_yaml
    current_hk = json.loads(hk_cfg_path.read_text(encoding="utf-8"))
    item = next(row for row in current_hk["symbols"] if row["symbol"] == "9898.HK")
    assert item["sell_put"]["enabled"] is False
    assert item["sell_call"]["enabled"] is True
    assert item["sell_call"]["min_strike"] == 85.0


def test_inbound_write_operations_are_disabled_by_default(tmp_path: Path) -> None:
    cfg_path = _write_symbols_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    trade_out = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_disabled_trade",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    symbol_out = handle_assistant_request(
        AssistantRequest(
            text="增加监控标的 700 put",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_disabled_symbol",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert trade_out["ok"] is False
    assert trade_out["error"]["code"] == "PERMISSION_DENIED"
    assert symbol_out["ok"] is False
    assert symbol_out["error"]["code"] == "PERMISSION_DENIED"


def test_inbound_handle_executes_read_only_tool_and_replays_duplicate_message(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    request = AssistantRequest(
        text="/income sy 2026-05",
        sender_id="ou_1",
        channel="feishu",
        message_id="msg_1",
        config_key="us",
        audit_db=str(audit_db),
    )

    first = handle_assistant_request(request, execute_tool_fn=_execute_tool, allowed_senders="feishu:ou_1")
    second = handle_assistant_request(request, execute_tool_fn=_execute_tool, allowed_senders="feishu:ou_1")

    assert first["ok"] is True
    assert first["data"]["tool_call"] == {
        "tool_name": "monthly_income_report",
        "payload": {"config_key": "us", "account": "sy", "month": "2026-05"},
    }
    assert "基于 OM 本地账本" in first["data"]["response_text"]
    assert second["meta"]["idempotent_replay"] is True
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            """
            SELECT intent_name, tool_name, decision, result_ok, duplicate_count, last_duplicate_sender_id, conversation_id,
                   perception_json, reasoning_json
            FROM inbound_command_audit
            """
        ).fetchone()

    assert row[:7] == ("monthly_income_report", "monthly_income_report", "allowed", 1, 1, "ou_1", "feishu:ou_1")
    perception = json.loads(row[7])
    reasoning = json.loads(row[8])
    assert perception == {
        "schema_version": PERCEPTION_RESULT_SCHEMA_VERSION,
        "intent_name": "monthly_income_report",
        "arguments": {"account": "sy", "month": "2026-05"},
        "source": "command",
        "confidence": 1.0,
        "evidence": {},
    }
    assert reasoning == {
        "schema_version": REASONING_RESOLUTION_SCHEMA_VERSION,
        "status": "supported",
        "intent_name": "monthly_income_report",
        "arguments": {"account": "sy", "month": "2026-05"},
        "safety_class": "read",
        "action_kind": "tool",
        "tool_call": {
            "tool_name": "monthly_income_report",
            "payload": {"config_key": "us", "account": "sy", "month": "2026-05"},
        },
        "read_only": True,
        "requires_confirmation": False,
        "reason": "read_only_capability",
        "message": None,
    }


def test_inbound_handle_omits_account_filter_when_account_not_provided(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": []})

    income = handle_assistant_request(
        AssistantRequest(
            text="/income 2026-05",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_income",
            config_key="us",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )
    positions = handle_assistant_request(
        AssistantRequest(
            text="/positions",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_positions",
            config_key="us",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )

    assert income["ok"] is True
    assert positions["ok"] is True
    assert calls == [
        ("monthly_income_report", {"config_key": "us", "month": "2026-05"}),
        ("option_positions_read", {"config_key": "us", "action": "list", "query": {"status": "open", "limit": 50}}),
    ]
    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            """
            SELECT perception_json, reasoning_json, tool_payload_json
            FROM inbound_command_audit
            WHERE message_id = 'msg_positions'
            """
        ).fetchone()
    perception = json.loads(row[0])
    reasoning = json.loads(row[1])
    tool_payload = json.loads(row[2])
    assert perception == {
        "schema_version": PERCEPTION_RESULT_SCHEMA_VERSION,
        "intent_name": "position_query",
        "arguments": {"status": "open", "limit": 50},
        "source": "command",
        "confidence": 1.0,
        "evidence": {},
    }
    assert reasoning == {
        "schema_version": REASONING_RESOLUTION_SCHEMA_VERSION,
        "status": "supported",
        "intent_name": "position_query",
        "arguments": {"status": "open", "limit": 50},
        "safety_class": "read",
        "action_kind": "tool",
        "tool_call": {
            "tool_name": "option_positions_read",
            "payload": {"config_key": "us", "action": "list", "query": {"status": "open", "limit": 50}},
        },
        "read_only": True,
        "requires_confirmation": False,
        "reason": "read_only_capability",
        "message": None,
    }
    assert tool_payload == {"config_key": "us", "action": "list", "query": {"status": "open", "limit": 50}}


def test_inbound_handle_without_message_id_generates_fresh_command_id(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    request = AssistantRequest(
        text="/status",
        sender_id="local",
        channel="local",
        config_key="us",
        audit_db=str(audit_db),
    )

    first = handle_assistant_request(request, execute_tool_fn=_execute_tool, allowed_senders="local:local")
    second = handle_assistant_request(request, execute_tool_fn=_execute_tool, allowed_senders="local:local")

    assert first["ok"] is True
    assert second["ok"] is True
    assert "idempotent_replay" not in second.get("meta", {})
    assert calls == [
        ("runtime_status", {"config_key": "us"}),
        ("runtime_status", {"config_key": "us"}),
    ]
    with sqlite3.connect(audit_db) as conn:
        rows = conn.execute(
            "SELECT command_id, message_id, duplicate_count FROM inbound_command_audit ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0].startswith("in_")
    assert rows[1][0].startswith("in_")
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] is None
    assert rows[1][1] is None
    assert rows[0][2] == 0
    assert rows[1][2] == 0


def test_inbound_audit_schema_uses_perception_reasoning_action_observation(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    out = handle_assistant_request(
        AssistantRequest(
            text="/status",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_audit_schema",
            config_key="us",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=lambda tool_name, payload: build_response(tool_name=tool_name, ok=True, data={"status": "ok"}),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    with sqlite3.connect(audit_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_command_audit)").fetchall()}
    assert {"perception_json", "reasoning_json", "action_json", "observation_json"} <= columns
    assert "semantic_frame_json" not in columns
    assert "tool_plan_json" not in columns


def test_inbound_monthly_income_renderer_prefers_return_summary() -> None:
    intent = _read_intent("monthly_income_report", {"account": "lx", "month": "2026-05"})
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [{"month": "2026-05", "account": "lx", "currency": "USD"}],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "lx",
                        "net_return_rate": 0.0681,
                        "net_income_by_ccy": {"USD": 5000.0},
                        "net_income_cny": 36097.23,
                        "cash_secured_cny": 530385.93,
                        "annualized_basis_days": 19,
                        "annualized_net_return_rate": 1.3074,
                        "premium_income_by_ccy": {"USD": 5100.0},
                        "premium_income_cny": 36800.0,
                        "premium_return_rate": 0.0697,
                        "realized_pnl_by_ccy": {"USD": -100.0},
                        "realized_pnl_cny": -702.77,
                    }
                ],
            },
        ),
    )

    assert "lx 2026-05 收益摘要" in text
    assert "净现金流：CNY 36,097（USD 5,000） | 现金流率 6.81%" in text
    assert "年化：130.74%（按净现金流，19 天）" in text
    assert "权利金：CNY 36,800（USD 5,100） | 权利金率 6.97%" in text
    assert "已实现PnL：CNY -703（USD -100） | 已实现率 -" in text
    assert "口径：现金流率=净现金流/当前现金担保，不是账户总资产收益率。" in text


def test_inbound_monthly_income_renderer_prefers_combined_return_summary() -> None:
    intent = _read_intent("monthly_income_report", {"month": "2026-05"})
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "combined_return_summary": [
                    {
                        "month": "2026-05",
                        "account": "all",
                        "account_scope": "all",
                        "accounts": ["lx", "sy"],
                        "net_return_rate": 0.071619,
                        "net_income_by_ccy": {"HKD": 50291.0, "USD": 890.0},
                        "net_income_cny": 57295.0,
                        "cash_secured_cny": 800000.0,
                        "annualized_basis_days": 31,
                        "annualized_net_return_rate": 0.843096,
                        "premium_income_by_ccy": {"HKD": 60331.0, "USD": 890.0},
                        "premium_income_cny": 62263.0,
                        "premium_return_rate": 0.077829,
                        "realized_pnl_by_ccy": {"HKD": 16159.0, "USD": 0.0},
                        "realized_pnl_cny": 17167.0,
                        "realized_return_rate": 0.021459,
                    }
                ],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "lx",
                        "net_return_rate": 0.1316,
                        "net_income_by_ccy": {"HKD": 32525.0},
                        "net_income_cny": 35842.0,
                        "cash_secured_cny": 272355.0,
                    },
                    {
                        "month": "2026-05",
                        "account": "sy",
                        "net_return_rate": 0.0406,
                        "net_income_by_ccy": {"HKD": 17766.0, "USD": 890.0},
                        "net_income_cny": 21453.0,
                        "cash_secured_cny": 527645.0,
                    },
                ],
            },
        ),
    )

    assert text.startswith("收益统计完成（OM 本地账本）：\n全部账户 2026-05 收益摘要")
    assert "净现金流：CNY 57,295（HKD 50,291 + USD 890） | 现金流率 7.16%" in text
    assert "分账户：\n- lx：净现金流 CNY 35,842（HKD 32,525） | 现金流率 13.16%" in text
    assert "口径：合并现金流率=sum(净现金流CNY)/sum(当前现金担保CNY)，不是账户收益率平均值。" in text


def test_inbound_monthly_income_renderer_flags_long_option_cash_recovery() -> None:
    intent = _read_intent("monthly_income_report", {"account": "sy", "month": "2026-06"})
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [
                    {
                        "month": "2026-06",
                        "account": "sy",
                        "currency": "HKD",
                        "close_proceeds_gross": 7416.0,
                        "realized_long_pnl_gross": 5036.0,
                    }
                ],
                "return_summary": [
                    {
                        "month": "2026-06",
                        "account": "sy",
                        "net_return_rate": 0.014232,
                        "net_income_by_ccy": {"HKD": 7416.0},
                        "net_income_cny": 6430.132141,
                        "cash_secured_cny": 451822.630243,
                        "annualized_basis_days": 1,
                        "annualized_net_return_rate": 5.19468,
                        "premium_income_by_ccy": {"HKD": 0.0},
                        "premium_income_cny": 0.0,
                        "premium_return_rate": 0.0,
                        "realized_pnl_by_ccy": {"HKD": 5036.0},
                        "realized_pnl_cny": 4366.524469,
                        "realized_return_rate": 0.009664,
                    }
                ],
            },
        ),
    )

    assert "净现金流：CNY 6,430（HKD 7,416） | 现金流率 1.42%" in text
    assert "已实现PnL：CNY 4,367（HKD 5,036） | 已实现率 0.97%" in text
    assert "年化：519.47%（按净现金流，1 天，短周期仅参考）" in text
    assert "提示：净现金流包含 long option 成本回收约 HKD 2,380，交易盈利看已实现PnL" in text


def test_inbound_monthly_income_renderer_does_not_cap_return_summary_rows() -> None:
    intent = _read_intent("monthly_income_report")
    rows = []
    for idx in range(5):
        month = f"2026-0{idx + 1}"
        rows.append(
            {
                "month": month,
                "account": "lx",
                "net_return_rate": 0.01,
                "net_income_by_ccy": {"USD": 100.0 + idx},
                "net_income_cny": 700.0 + idx,
                "cash_secured_cny": 10000.0,
                "annualized_basis_days": 30,
                "annualized_net_return_rate": 0.121667,
                "premium_return_rate": 0.01,
                "premium_income_cny": 700.0 + idx,
                "realized_pnl_cny": 0.0,
            }
        )
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={"return_summary": rows},
        ),
    )

    assert "lx 2026-01 收益摘要" in text
    assert "lx 2026-05 收益摘要" in text
    assert text.count("收益摘要") == 5


def test_inbound_monthly_income_renderer_explains_incomplete_summary() -> None:
    intent = _read_intent("monthly_income_report", {"account": "sy", "month": "2026-05"})
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "sy",
                        "net_return_rate": None,
                        "net_income_cny": None,
                        "cash_secured_cny": None,
                        "annualized_basis_days": 20,
                        "annualized_net_return_rate": None,
                        "premium_return_rate": None,
                    }
                ],
                "diagnostics": [
                    {
                        "account": "sy",
                        "month": "2026-05",
                        "status": "incomplete",
                        "matched_trade_events_count": 0,
                        "matched_lots_count": 13,
                        "closed_lots_count": 0,
                        "premium_rows_count": 0,
                        "cash_secured_available": False,
                        "missing_fields": ["cash_secured", "closed_lots", "currency_conversion", "premium"],
                    }
                ],
            },
        ),
    )

    assert "sy 2026-05 暂无可计算收益。" in text
    assert "账本缺少已平仓/close 数据" in text
    assert "匹配事件：0，持仓 lot：13，已平仓 lot：0，权利金行：0。" in text
    assert "缺失项：cash_secured、closed_lots、currency_conversion、premium" in text


def test_inbound_monthly_income_renderer_shows_original_currency_when_rates_missing() -> None:
    intent = _read_intent("monthly_income_report", {"account": "lx", "month": "2026-05"})
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [{"month": "2026-05", "account": "lx", "currency": "HKD"}],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "lx",
                        "cash_secured_by_ccy": {"HKD": 377500.0, "USD": 29745.0},
                        "cash_secured_cny": None,
                        "net_income_by_ccy": {"HKD": 22751.0, "USD": 2400.0},
                        "net_income_cny": None,
                        "premium_income_by_ccy": {"HKD": 23735.0, "USD": 2400.0},
                        "premium_income_cny": None,
                        "premium_return_rate_by_ccy": {"HKD": 0.062874, "USD": 0.080686},
                        "net_return_rate": None,
                        "premium_return_rate": None,
                    }
                ],
                "diagnostics": [
                    {
                        "account": "lx",
                        "month": "2026-05",
                        "status": "incomplete",
                        "matched_trade_events_count": 17,
                        "matched_lots_count": 17,
                        "closed_lots_count": 0,
                        "premium_rows_count": 16,
                        "cash_secured_available": True,
                        "cash_secured_conversion_missing": True,
                        "currency_conversion_missing": True,
                        "missing_cny_currencies": ["HKD", "USD"],
                        "missing_fields": ["currency_conversion"],
                    }
                ],
            },
        ),
    )

    assert "lx 2026-05 暂无可计算收益。" in text
    assert "本月暂无平仓收益" in text
    assert "现金担保原币存在，但缺少 HKD/USD 到 CNY 汇率，无法折算 CNY" in text
    assert "本月有开仓权利金收入，但缺汇率导致无法计算 CNY 收益率" in text
    assert "当前持仓缺少现金担保金额" not in text
    assert "账本缺少已平仓/close 数据" not in text
    assert "净现金流：HKD 22,751 + USD 2,400" in text
    assert "权利金：HKD 23,735 + USD 2,400" in text
    assert "现金担保：HKD 377,500 + USD 29,745" not in text
    assert "原币权利金收益率：HKD 6.29%，USD 8.07%" in text


def test_inbound_renderer_summarizes_position_rows() -> None:
    intent = parse_assistant_command("/positions sy")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="option_positions_read",
            ok=True,
            data={
                "rows": [
                    {
                        "account": "sy",
                        "symbol": "0700.HK",
                        "option_type": "call",
                        "side": "short",
                        "strike": 510.0,
                        "expiration_ymd": "2026-05-28",
                        "contracts_open": 2,
                    },
                    {
                        "account": "sy",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "strike": 450.0,
                        "expiration_ymd": "2026-06-29",
                        "contracts_open": 3,
                    },
                ],
                "filters": {"account": "sy", "status": "open"},
            },
        ),
    )

    assert "sy · open · 期权持仓：2 条" in text
    assert "0700.HK short call 510 exp 2026-05-28 open 2" in text
    assert "数据源：OM 本地 SQLite position_lots" in text

    all_accounts = render_inbound_text(
        intent=parse_assistant_command("/positions"),
        tool_result=build_response(
            tool_name="option_positions_read",
            ok=True,
            data={"rows": [], "filters": {"account": None, "status": "open"}},
        ),
    )
    assert all_accounts.startswith("全部账户 · open · 期权持仓：0 条。")


def test_inbound_renderer_does_not_cap_position_rows() -> None:
    rows = [
        {
            "account": "sy",
            "symbol": f"SYM{idx}",
            "option_type": "put",
            "side": "short",
            "strike": 400 + idx,
            "expiration_ymd": "2026-06-29",
            "contracts_open": idx,
        }
        for idx in range(1, 13)
    ]

    text = render_inbound_text(
        intent=parse_assistant_command("/positions sy"),
        tool_result=build_response(
            tool_name="option_positions_read",
            ok=True,
            data={"rows": rows, "filters": {"account": "sy", "status": "open"}},
        ),
    )

    assert "sy · open · 期权持仓：12 条" in text
    assert "SYM1 short put 401 exp 2026-06-29 open 1" in text
    assert "SYM12 short put 412 exp 2026-06-29 open 12" in text
    assert "未展示" not in text


def test_inbound_renderer_explains_empty_positions() -> None:
    intent = parse_assistant_command("/positions lx")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="option_positions_read",
            ok=True,
            data={"rows": [], "filters": {"account": "lx", "status": "open"}},
        ),
    )

    assert text.startswith("lx · open · 期权持仓：0 条。")


def test_inbound_renderer_summarizes_runtime_status() -> None:
    intent = parse_assistant_command("/status")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="runtime_status",
            ok=True,
            data={
                "summary": {
                    "ok": False,
                    "latest_status": "ok",
                    "warning_count": 1,
                    "ledger_status": "ok",
                    "ledger_position_lot_count": 3,
                    "ledger_trade_event_count": 12,
                    "projection_verify_ok": True,
                    "projection_verify_mode": "checkpoint_reuse",
                },
                "latest_run": {
                    "path": "output_runs/run-1",
                    "state": {
                        "tick_metrics": {
                            "json": {
                                "ran_scan": True,
                                "notify_summary": {"send_confirmed_count": 1, "send_attempted_count": 1},
                            }
                        }
                    },
                    "accounts": {
                        "sy": {
                            "auto_close_receipt": {"status": "sent"},
                            "expired_position_maintenance": {"json": {"mode": "applied", "applied_closed": 1}},
                        }
                    },
                },
            },
            warnings=["No symbols_notification.txt found."],
        ),
    )

    assert "OM 状态：degraded" in text
    assert "最新运行：run-1 scan=yes notify=1/1" in text
    assert "账本：ok lots=3 events=12" in text
    assert "auto-close sy：sent，closed=1" in text
    assert "异常：No symbols_notification.txt found." in text


def test_inbound_renderer_runtime_status_uses_tool_ok_and_shared_last_run() -> None:
    intent = parse_assistant_command("/status")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="runtime_status",
            ok=True,
            data={
                "summary": {"warning_count": 0, "latest_status": None},
                "shared": {
                    "last_run": {
                        "json": {
                            "sent": True,
                            "results": [
                                {"account": "lx", "ran_scan": True},
                                {"account": "sy", "ran_scan": True},
                            ],
                            "notify_summary": {
                                "account_messages_count": 2,
                                "send_attempted_count": 2,
                                "send_confirmed_count": 2,
                                "send_failed_count": 0,
                            },
                        }
                    }
                },
            },
            warnings=[],
        ),
    )

    assert "OM 状态：ok" in text
    assert "最新通知：scan=yes notify=2/2" in text
    assert "unknown" not in text
    assert "异常：无" in text


def test_inbound_renderer_status_summary_prioritizes_auto_close_failure() -> None:
    intent = parse_assistant_command("/status")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="runtime_status",
            ok=True,
            data={
                "summary": {"ok": False},
                "latest_run": {
                    "path": "output_runs/run-1",
                    "accounts": {
                        "lx": {
                            "auto_close_receipt": {"status": "sent"},
                            "expired_position_maintenance": {
                                "json": {
                                    "mode": "error",
                                    "reason": "missing_data_config",
                                    "applied_closed": 0,
                                    "errors": ["missing_data_config: /var/lib/options-monitor/portfolio.runtime.json"],
                                }
                            },
                        }
                    },
                },
            },
            warnings=["Auto-close lx failed: missing_data_config."],
        ),
    )

    assert "auto-close lx：failed，receipt=sent，closed=0，reason=missing_data_config" in text
    assert "异常：Auto-close lx failed: missing_data_config." in text


def test_inbound_renderer_shows_service_upgrade_failure_details() -> None:
    intent = parse_assistant_command("/status")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="runtime_status",
            ok=True,
            data={
                "summary": {
                    "ok": False,
                    "latest_status": "ok",
                    "warning_count": 1,
                    "service_upgrade_status": "upgraded_restart_failed",
                    "service_upgrade_target_version": "1.2.118",
                    "service_upgrade_current_version": "1.2.118",
                    "service_upgrade_reason": "upgrade_failure_still_requires_remediation",
                    "service_upgrade_runtime_failed": True,
                    "service_upgrade_failed_services": ["options-monitor-feishu-ws.service"],
                    "service_upgrade_remediation": [
                        "manual_restart: sudo systemctl restart options-monitor-feishu-ws.service",
                        "sudoers_minimal:",
                    ],
                },
                "service_upgrade": {
                    "evaluation": {
                        "status": "upgraded_restart_failed",
                        "target_version": "1.2.118",
                        "current_version": "1.2.118",
                        "reason": "upgrade_failure_still_requires_remediation",
                    }
                },
            },
            warnings=["Service upgrade status still indicates an unrecovered runtime failure."],
        ),
    )

    assert "升级状态：upgraded_restart_failed target=1.2.118 current=1.2.118 reason=upgrade_failure_still_requires_remediation" in text
    assert "失败服务：options-monitor-feishu-ws.service" in text
    assert "修复提示：manual_restart: sudo systemctl restart options-monitor-feishu-ws.service；sudoers_minimal:" in text


def test_inbound_renderer_summarizes_healthcheck_and_config() -> None:
    health_text = render_inbound_text(
        intent=parse_assistant_command("/health"),
        tool_result=build_response(
            tool_name="healthcheck",
            ok=True,
            data={
                "summary": {"ok": False, "critical_count": 1, "warning_count": 2},
                "checks": [
                    {"name": "opend_readiness", "status": "error", "message": "OpenD unreachable"},
                    {"name": "feishu_bot", "status": "warn", "message": "missing default recipient"},
                ],
            },
        ),
    )
    config_text = render_inbound_text(
        intent=parse_assistant_command("/config"),
        tool_result=build_response(
            tool_name="config_validate",
            ok=True,
            data={
                "config_path": ".../config.us.json",
                "account_count": 2,
                "accounts": ["lx", "sy"],
                "symbol_count": 12,
                "warnings": ["schedule disabled"],
            },
        ),
    )

    assert "健康检查：degraded" in health_text
    assert "- error opend_readiness: OpenD unreachable" in health_text
    assert "配置检查：有警告" in config_text
    assert "账户：lx, sy（2 个）" in config_text
    assert "警告：schedule disabled" in config_text


def test_inbound_renderer_summarizes_runs_and_logs() -> None:
    runs_text = render_inbound_text(
        intent=parse_assistant_command("/runs"),
        tool_result=build_response(
            tool_name="runtime_runs",
            ok=True,
            data={
                "summary": {"returned_count": 1, "total_count": 1},
                "runs": [
                    {
                        "run_id": "run-1",
                        "status": "success",
                        "mtime_utc": "2026-05-20T01:00:00+00:00",
                        "ran_scan": True,
                        "sent": False,
                        "accounts": ["sy"],
                        "reason": "market_closed",
                    }
                ],
            },
        ),
    )
    logs_text = render_inbound_text(
        intent=parse_assistant_command("/logs run-1"),
        tool_result=build_response(
            tool_name="runtime_logs",
            ok=True,
            data={
                "summary": {"existing_file_count": 1, "kind": "audit", "lines": 50},
                "selected_run": {"run_id": "run-1"},
                "files": [
                    {
                        "path_display": "output_runs/run-1/state/audit_events.jsonl",
                        "exists": True,
                        "tail_line_count": 2,
                        "tail": ['{"phase":"start"}', '{"phase":"done"}'],
                    }
                ],
            },
        ),
    )

    assert "最近运行：1/1 条" in runs_text
    assert "run-1 success 2026-05-20T01:00:00+00:00 scan=yes sent=no accounts=sy reason=market_closed" in runs_text
    assert "日志查询：1/1 个文件，kind=audit，lines=50" in logs_text
    assert "run：run-1" in logs_text
    assert '{"phase":"done"}' in logs_text


def test_inbound_audit_keeps_monthly_income_diagnostics(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "summary": [],
                "return_summary": [],
                "diagnostics": [
                    {
                        "account": "sy",
                        "month": "2026-05",
                        "status": "empty",
                        "matched_trade_events_count": 0,
                        "matched_lots_count": 13,
                        "closed_lots_count": 0,
                        "premium_rows_count": 0,
                        "missing_fields": ["income_rows", "closed_lots", "premium"],
                    }
                ],
            },
        )

    out = handle_assistant_request(
        AssistantRequest(
            text="/income sy 2026-05",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_diag",
            config_key="us",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )

    assert out["data"]["response_text"].startswith("sy 2026-05 暂无可计算收益")
    with sqlite3.connect(audit_db) as conn:
        response_json = conn.execute("SELECT response_json FROM inbound_command_audit").fetchone()[0]

    stored = json.loads(response_json)
    diagnostics = stored["data"]["tool_result"]["data"]["diagnostics"]
    assert diagnostics[0]["status"] == "empty"
    assert diagnostics[0]["matched_lots_count"] == 13


def test_inbound_duplicate_message_from_other_sender_is_denied_and_marked(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        return build_response(tool_name=tool_name, ok=True, data={"summary": []})

    first = handle_assistant_request(
        AssistantRequest(text="/income sy", sender_id="ou_1", channel="feishu", message_id="msg_1", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1,feishu:ou_2",
    )
    second = handle_assistant_request(
        AssistantRequest(text="/income sy", sender_id="ou_2", channel="feishu", message_id="msg_1", config_key="us", audit_db=str(audit_db)),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1,feishu:ou_2",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "PERMISSION_DENIED"

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            "SELECT duplicate_count, last_duplicate_sender_id, last_duplicate_decision FROM inbound_command_audit"
        ).fetchone()

    assert row == (1, "ou_2", "sender_conflict")


def test_inbound_handle_denies_unknown_remote_sender_and_audits(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={})

    out = handle_assistant_request(
        AssistantRequest(
            text="/positions sy",
            sender_id="ou_bad",
            channel="feishu",
            message_id="msg_bad",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_good",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "PERMISSION_DENIED"
    assert calls == []

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute("SELECT decision, error_code FROM inbound_command_audit").fetchone()

    assert row == ("denied", "PERMISSION_DENIED")


def test_feishu_payload_adapter_extracts_text_message_and_calls_inbound(tmp_path: Path) -> None:
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1", "user_id": "user_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": '<at user_id="bot">Bot</at> /income sy 2026-05'}, ensure_ascii=False),
            },
        },
    }
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    request = feishu_payload_to_inbound_request(payload, audit_db=str(tmp_path / "audit.sqlite3"))
    assert request == AssistantRequest(
        text="/income sy 2026-05",
        sender_id="ou_1",
        channel="feishu",
        message_id="om_1",
        conversation_id="feishu:oc_1:ou_1",
        audit_db=str(tmp_path / "audit.sqlite3"),
    )

    out = handle_feishu_payload(
        payload,
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    assert out["tool_name"] == "inbound.feishu"
    assert out["data"]["response_text"].startswith("收益统计完成")
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]


def test_feishu_payload_adapter_assistant_reads_assistant_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False), encoding="utf-8")
    cfg = _runtime_cfg(str(data_cfg_path))
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(json.dumps({"assistant": {
        "enabled": True,
        "planner": {"enabled": True},
        "context_window_messages": 7,
        "default_market_scope": "us",
        "llm": {
            "provider": "openai",
            "base_url": "https://llm.example/v1",
            "model": "gpt-5.2",
            "api_key_env": "OM_LLM_API_KEY",
            "confidence_min": 0.82,
            "timeout_seconds": 32,
            "max_output_tokens": 770,
        },
    }}, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_agent", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_agent",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": "/status"}, ensure_ascii=False),
            },
        },
    }
    seen: list[dict] = []

    def _handle_assistant_message(request: AssistantRequest, **kwargs) -> dict:
        seen.append({"request": request, "kwargs": kwargs})
        return build_response(
            tool_name="assistant.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
            meta={"assistant": {"route": "command"}},
        )

    monkeypatch.setattr("src.application.assistant.runtime.handle_assistant_message", _handle_assistant_message)

    out = handle_feishu_payload(
        payload,
        config_path=str(cfg_path),
        assistant_config_path=str(assistant_config_path),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    assert out["data"]["response_text"] == "状态查询完成。"
    assert len(seen) == 1
    settings = seen[0]["kwargs"]["settings"]
    assert settings.enabled is True
    assert settings.planner.enabled is True
    assert settings.context_window_messages == 7
    assert settings.llm.enabled is True
    assert settings.llm.provider == "openai"
    assert settings.llm.base_url == "https://llm.example/v1"
    assert settings.llm.model == "gpt-5.2"
    assert settings.llm.confidence_min == 0.82
    assert settings.llm.timeout_seconds == 32
    assert settings.llm.max_output_tokens == 770


def test_feishu_payload_adapter_defaults_to_assistant_from_assistant_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False), encoding="utf-8")
    cfg = _runtime_cfg(str(data_cfg_path))
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps({"assistant": {"enabled": True, "planner": {"enabled": False}, "context_window_messages": 5, "llm": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_agent_default", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_agent_default",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": "/status"}, ensure_ascii=False),
            },
        },
    }
    seen: list[dict] = []

    def _handle_assistant_message(request: AssistantRequest, **kwargs) -> dict:
        seen.append({"request": request, "kwargs": kwargs})
        return build_response(
            tool_name="assistant.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
            meta={"assistant": {"route": "command"}},
        )

    monkeypatch.setattr("src.application.assistant.runtime.handle_assistant_message", _handle_assistant_message)

    out = handle_feishu_payload(
        payload,
        config_path=str(cfg_path),
        assistant_config_path=str(assistant_config_path),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    assert len(seen) == 1
    settings = seen[0]["kwargs"]["settings"]
    assert settings.enabled is True
    assert settings.planner.enabled is False
    assert settings.context_window_messages == 5


def test_feishu_payload_adapter_ignores_non_message_events() -> None:
    out = handle_feishu_payload(
        {
            "schema": "2.0",
            "header": {"event_id": "evt_1", "event_type": "im.message.message_read_v1"},
            "event": {},
        }
    )

    assert out["ok"] is True
    assert out["data"]["kind"] == "ignored_event"
    assert out["data"]["reason"] == "unsupported_event_type"


def test_assistant_cli_handle_wires_request(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    seen: list[AssistantRequest] = []

    def _handle(request: AssistantRequest, **kwargs) -> dict:
        del kwargs
        seen.append(request)
        return build_response(
            tool_name="assistant.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_assistant_message", _handle)

    rc = cli.main(
        [
            "assistant",
            "handle",
            "--text",
            "状态",
            "--sender",
            "ou_1",
            "--channel",
            "feishu",
            "--message-id",
            "msg_1",
            "--conversation-id",
            "feishu:oc_1:ou_1",
            "--config-key",
            "us",
            "--audit-db",
            str(tmp_path / "audit.sqlite3"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "assistant.handle"
    assert seen == [
        AssistantRequest(
            text="状态",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_1",
            conversation_id="feishu:oc_1:ou_1",
            config_key="us",
            audit_db=str(tmp_path / "audit.sqlite3"),
        )
    ]


def test_assistant_cli_handle_loads_settings_from_config(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    cfg = _runtime_cfg(str(tmp_path / "portfolio.runtime.json"))
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(json.dumps({"assistant": {
        "enabled": True,
        "planner": {"enabled": True},
        "context_window_messages": 6,
        "default_market_scope": "us",
        "llm": {
            "provider": "openai",
            "base_url": "https://llm.example/v1",
            "model": "gpt-5.2",
            "api_key_env": "OM_LLM_API_KEY",
            "confidence_min": 0.81,
            "timeout_seconds": 33,
            "max_output_tokens": 771,
        },
    }}, ensure_ascii=False, indent=2), encoding="utf-8")
    seen = []

    def _handle_assistant(request: AssistantRequest, **kwargs) -> dict:
        seen.append({"request": request, "settings": kwargs.get("settings")})
        return build_response(
            tool_name="assistant.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_assistant_message", _handle_assistant)

    rc = cli.main(
        [
            "assistant",
            "handle",
            "--config-path",
            str(cfg_path),
            "--assistant-config",
            str(assistant_config_path),
            "--text",
            "/status",
            "--sender",
            "local",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "assistant.handle"
    assert len(seen) == 1
    settings = seen[0]["settings"]
    assert settings.enabled is True
    assert settings.context_window_messages == 6
    assert settings.llm.enabled is True
    assert settings.llm.provider == "openai"
    assert settings.llm.base_url == "https://llm.example/v1"
    assert settings.llm.model == "gpt-5.2"
    assert settings.llm.confidence_min == 0.81
    assert settings.llm.timeout_seconds == 33
    assert settings.llm.max_output_tokens == 771


def test_assistant_cli_handle_uses_planner_disabled_config(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    cfg = _runtime_cfg(str(tmp_path / "portfolio.runtime.json"))
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps({"assistant": {"enabled": True, "planner": {"enabled": False}, "context_window_messages": 4, "llm": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    seen = []

    def _handle_assistant(request: AssistantRequest, **kwargs) -> dict:
        seen.append({"request": request, "settings": kwargs.get("settings")})
        return build_response(
            tool_name="assistant.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_assistant_message", _handle_assistant)

    rc = cli.main(
        [
            "assistant",
            "handle",
            "--config-path",
            str(cfg_path),
            "--assistant-config",
            str(assistant_config_path),
            "--text",
            "/status",
            "--sender",
            "local",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "assistant.handle"
    assert len(seen) == 1
    settings = seen[0]["settings"]
    assert settings.enabled is True
    assert settings.planner.enabled is False
    assert settings.context_window_messages == 4


def test_assistant_cli_pending_and_audit_diagnostics(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, _sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"
    preview = handle_assistant_request(
        AssistantRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_cli_pending_preview",
            conversation_id="feishu:oc_1:ou_1",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    capsys.readouterr()

    pending_rc = cli.main(
        [
            "assistant",
            "pending",
            "list",
            "--channel",
            "feishu",
            "--sender",
            "ou_1",
            "--conversation-id",
            "feishu:oc_1:ou_1",
            "--audit-db",
            str(audit_db),
        ]
    )
    pending_payload = json.loads(capsys.readouterr().out)

    assert pending_rc == 0
    assert pending_payload["tool_name"] == "assistant.pending.list"
    assert pending_payload["data"]["pending_count"] == 1
    assert pending_payload["data"]["pending_operations"][0]["operation_id"] == preview["data"]["operation_id"]
    assert "NVDA 2026-06-19 100.0P short put 1张 premium 2.5" in pending_payload["data"]["response_text"]

    text_rc = cli.main(
        [
            "assistant",
            "pending",
            "list",
            "--channel",
            "feishu",
            "--sender",
            "ou_1",
            "--conversation-id",
            "feishu:oc_1:ou_1",
            "--audit-db",
            str(audit_db),
            "--format",
            "text",
        ]
    )
    pending_text = capsys.readouterr().out
    assert text_rc == 0
    assert "Inbound pending：1 条" in pending_text
    assert f"确认：确认记录 {preview['data']['operation_id']}" in pending_text

    audit_rc = cli.main(
        [
            "assistant",
            "audit",
            "recent",
            "--channel",
            "feishu",
            "--sender",
            "ou_1",
            "--audit-db",
            str(audit_db),
        ]
    )
    audit_payload = json.loads(capsys.readouterr().out)

    assert audit_rc == 0
    assert audit_payload["tool_name"] == "assistant.audit.recent"
    assert audit_payload["data"]["audit_count"] == 1
    assert audit_payload["data"]["audit_rows"][0]["intent_name"] == "manual_trade_open"
    assert audit_payload["data"]["audit_rows"][0]["message_id"] == "msg_cli_pending_preview"
    assert "交易记录预览：开仓" in audit_payload["data"]["audit_rows"][0]["response_text"]

    audit_text_rc = cli.main(
        [
            "assistant",
            "audit",
            "recent",
            "--channel",
            "feishu",
            "--sender",
            "ou_1",
            "--audit-db",
            str(audit_db),
            "--format",
            "text",
        ]
    )
    audit_text = capsys.readouterr().out
    assert audit_text_rc == 0
    assert "Inbound audit recent：1 条" in audit_text
    assert "manual_trade_open" in audit_text
    assert "msg_cli_pending_preview" in audit_text


def test_inbound_cli_feishu_wires_payload(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    seen: list[dict] = []

    def _handle(payload: dict, **kwargs) -> dict:
        seen.append({"payload": payload, "kwargs": kwargs})
        return build_response(
            tool_name="inbound.feishu",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_feishu_payload", _handle)
    payload_path = tmp_path / "feishu.json"
    payload_path.write_text(json.dumps({"event": {"message": {"content": "{}"}}}), encoding="utf-8")

    rc = cli.main(
        [
            "inbound",
            "feishu",
            "--input-file",
            str(payload_path),
            "--config-key",
            "us",
            "--audit-db",
            str(tmp_path / "audit.sqlite3"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "inbound.feishu"
    assert seen == [
        {
            "payload": {"event": {"message": {"content": "{}"}}},
            "kwargs": {
                "config_key": "us",
                "config_path": None,
                "audit_db": str(tmp_path / "audit.sqlite3"),
                "assistant_config_path": None,
            },
        }
    ]


def test_inbound_cli_feishu_reports_invalid_json(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main(["inbound", "feishu", "--input-json", "{bad"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"


def test_inbound_cli_feishu_ws_check_reports_redacted_config(capsys, monkeypatch) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "app_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "secret_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_1")
    monkeypatch.setattr("src.application.inbound.feishu_ws.is_feishu_ws_sdk_available", lambda: True)

    rc = cli.main(
        [
            "inbound",
            "feishu-ws",
            "--config-key",
            "us",
            "--check",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["settings"]["app_id_configured"] is True
    assert "secret_1" not in json.dumps(payload, ensure_ascii=False)


def test_inbound_cli_feishu_ws_rejects_secret_override_flags(capsys) -> None:
    import src.interfaces.cli.main as cli

    try:
        cli.main(["inbound", "feishu-ws", "--app-id", "app_1", "--check"])
    except SystemExit as exc:
        assert int(exc.code or 0) == 2
    else:
        raise AssertionError("expected argparse to reject --app-id")
    _ = capsys.readouterr()
