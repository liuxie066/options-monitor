from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantSettings
from src.application.assistant.agent_loop import LlmSynthesisResult, LlmPlannerResult, PlannerPlan, PlannerPlanStep
from src.application.inbound.feishu_ws import (
    FeishuWsSettings,
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_ws_event,
    serve_feishu_ws,
)
from src.infrastructure.feishu_ws_client import feishu_event_model_to_payload


def _message_payload(*, sender: str = "ou_1", text: str = "收益 sy 2026-05") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": "msg_1",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_feishu_ws_delegates_to_inbound_and_replies(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []
    reactions: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(dict(kwargs))
        return {"code": 0, "data": {"reaction_id": "reaction_1"}}

    out = handle_feishu_ws_event(
        _message_payload(),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        reaction_fn=_reaction,
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert out["data"]["reaction"]["reason"] == "sent"
    assert out["data"]["reply"]["reason"] == "sent"
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]
    assert reactions[0]["message_id"] == "msg_1"
    assert reactions[0]["emoji_type"] == "SMILE"
    assert replies[0]["message_id"] == "msg_1"
    assert replies[0]["text"].startswith("收益统计完成")


def test_feishu_ws_can_route_through_assistant(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(json.dumps({"assistant": {"mode": "deterministic"}}), encoding="utf-8")

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    out = handle_feishu_ws_event(
        _message_payload(text="/status"),
        settings=FeishuWsSettings(
            config_path=str(tmp_path / "config.us.json"),
            assistant_config_path=str(assistant_config_path),
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
            assistant_enabled=True,
        ),
        reply_fn=lambda **_kwargs: {"code": 0},
        execute_tool_fn=_execute,
    )

    inbound_result = out["data"]["inbound"]["data"]["inbound_result"]
    assert out["ok"] is True
    assert calls == [("runtime_status", {"config_path": str(tmp_path / "config.us.json")})]
    assert inbound_result["data"]["perception"]["source"] == "command"
    assert inbound_result["data"]["perception"]["intent_name"] == "runtime_status"
    assert inbound_result["meta"]["assistant"]["route"] == "command"
    assert inbound_result["meta"]["assistant"]["llm"]["enabled"] is False


def test_feishu_ws_agent_loop_routes_cashflow_detail_plan(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps(
            {
                "assistant": {
                    "mode": "agent_loop",
                    "llm": {"provider": "openai", "model": "gpt-5.2", "api_key_env": "OM_LLM_API_KEY"},
                }
            }
        ),
        encoding="utf-8",
    )

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "summary": [{"month": "2026-06", "account": "lx", "currency": "HKD", "net_cashflow_gross": 1200}],
                "return_summary": [{"month": "2026-06", "account": "lx", "cash_secured_cny": 10000, "net_income_cny": 1104}],
                "cashflow_rows": [{"symbol": "0700.HK", "trade_action": "sell_open", "currency": "HKD", "net_cashflow_gross": 1200}],
                "row_count": 1,
                "premium_row_count": 1,
            },
        )

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _plan(
        text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        assert text == "分析 lx 6月的净现金流明细"
        return LlmPlannerResult(
            plan=PlannerPlan(
                goal="分析 lx 2026-06 的净现金流明细",
                response_mode="synthesis",
                steps=(
                    PlannerPlanStep(
                        id="step_1",
                        tool_name="monthly_income_report",
                        arguments={"account": "lx", "month": "2026-06", "include_rows": True},
                        purpose="cashflow detail",
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
            },
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        assert observations[0]["data"]["cashflow_rows"][0]["symbol"] == "0700.HK"
        return LlmSynthesisResult(
            response_text="lx 2026-06 净现金流明细\n- 0700.HK sell_open HKD 1,200",
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_feishu_ws_event(
        _message_payload(text="分析 lx 6月的净现金流明细"),
        settings=FeishuWsSettings(
            config_key="us",
            assistant_config_path=str(assistant_config_path),
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        execute_tool_fn=_execute,
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    inbound_result = out["data"]["inbound"]["data"]["inbound_result"]
    assert out["ok"] is True
    assert calls == [
        (
            "monthly_income_report",
            {"account": "lx", "config_key": "us", "include_rows": True, "month": "2026-06"},
        )
    ]
    assert replies[0]["text"].startswith("lx 2026-06 净现金流明细")
    assert inbound_result["meta"]["assistant"]["route"] == "agent_loop"
    assert inbound_result["meta"]["assistant"]["llm"]["agent_loop"]["final_response"]["status"] == "synthesized"


def test_feishu_ws_reaction_failure_does_not_fail_inbound_or_reply(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _reaction(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("no permission")

    out = handle_feishu_ws_event(
        _message_payload(text="状态"),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        reaction_fn=_reaction,
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert out["data"]["reaction"]["ok"] is False
    assert out["data"]["reaction"]["reason"] == "reaction_failed"
    assert out["data"]["reply"]["reason"] == "sent"
    assert replies[0]["message_id"] == "msg_1"


def test_feishu_ws_does_not_reply_to_denied_sender(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []
    reactions: list[dict[str, Any]] = []

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(dict(kwargs))
        return {"code": 0}

    out = handle_feishu_ws_event(
        _message_payload(sender="ou_bad", text="状态"),
        settings=FeishuWsSettings(
            allowed_senders="feishu:ou_good",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        reaction_fn=_reaction,
    )

    assert out["data"]["reaction"]["reason"] == "permission_denied"
    assert out["data"]["reply"]["reason"] == "permission_denied"
    assert reactions == []
    assert replies == []


def test_feishu_ws_replies_when_allowed_sender_hits_write_gate(monkeypatch, tmp_path: Path) -> None:
    for name in (
        "OM_ENV_FILE",
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_TRADE_WRITE_ENABLED",
        "OM_INBOUND_ADMIN_OPEN_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    replies: list[dict[str, Any]] = []

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0}

    out = handle_feishu_ws_event(
        _message_payload(
            sender="ou_1",
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
        ),
        settings=FeishuWsSettings(
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
    )

    assert out["ok"] is False
    assert out["data"]["reply"]["reason"] == "permission_denied_sent"
    assert replies
    assert "inbound write operations are disabled" in replies[0]["text"]
    assert "OM_INBOUND_OPERATIONS_ENABLED=1" in replies[0]["text"]


def test_feishu_ws_settings_uses_unified_bot_config_without_callback_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")

    settings = build_feishu_ws_settings(
        config_path=str(config_path),
        environ={
            "OM_FEISHU_BOT_APP_ID": "bot_app",
            "OM_FEISHU_BOT_APP_SECRET": "bot_secret",
            "OM_FEISHU_BOT_USER_OPEN_ID": "ou_1",
            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_1,ou_2",
            "OM_FEISHU_ACK_REACTION": "smile",
        }
    )

    assert settings.app_id == "bot_app"
    assert settings.app_secret == "bot_secret"
    assert settings.allowed_senders == "feishu:ou_1,feishu:ou_2"
    assert settings.ack_reaction == ""


def test_feishu_ws_settings_can_load_bot_config_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=bot_app_file",
                "OM_FEISHU_BOT_APP_SECRET=bot_secret_file",
                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_file",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = build_feishu_ws_settings(environ={}, env_file=str(env_file))

    assert settings.app_id == "bot_app_file"
    assert settings.app_secret == "bot_secret_file"
    assert settings.allowed_senders == "feishu:ou_file"


def test_feishu_ws_settings_reads_behavior_from_assistant_config(tmp_path: Path) -> None:
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps(
            {
                "inbound": {
                    "feishu_ws": {
                        "reply_enabled": False,
                        "reply_in_thread": True,
                        "max_reply_chars": 1200,
                        "ack_reaction": "smile",
                        "queue_size": 25,
                    }
                },
                "assistant": {
                    "mode": "agent_loop",
                    "context_window_messages": 9,
                    "default_market_scope": "us",
                    "llm": {
                        "provider": "openai",
                        "base_url": "https://llm.example/v1",
                        "model": "gpt-5.2",
                        "api_key_env": "OM_LLM_API_KEY",
                        "confidence_min": 0.8,
                        "timeout_seconds": 31,
                        "max_output_tokens": 769,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = build_feishu_ws_settings(
        config_path=str(tmp_path / "config.us.json"),
        assistant_config_path=str(assistant_config_path),
        queue_size=5,
        environ={
            "OM_FEISHU_BOT_APP_ID": "bot_app",
            "OM_FEISHU_BOT_APP_SECRET": "bot_secret",
            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_1",
            "OM_FEISHU_WS_QUEUE_SIZE": "99",
        },
    )

    assert settings.reply_enabled is False
    assert settings.config_path == str(tmp_path / "config.us.json")
    assert settings.assistant_config_path == str(assistant_config_path)
    assert settings.reply_in_thread is True
    assert settings.max_reply_chars == 1200
    assert settings.ack_reaction == "SMILE"
    assert settings.queue_size == 5
    assert settings.assistant_mode == "agent_loop"
    assert settings.assistant_enabled is True
    assert settings.assistant_context_window_messages == 9
    assert settings.assistant_llm.enabled is True
    assert settings.assistant_llm.provider == "openai"
    assert settings.assistant_llm.base_url == "https://llm.example/v1"
    assert settings.assistant_llm.model == "gpt-5.2"
    assert settings.assistant_llm.confidence_min == 0.8
    assert settings.assistant_llm.timeout_seconds == 31
    assert settings.assistant_llm.max_output_tokens == 769


def test_feishu_ws_settings_enables_command_runtime_by_default(tmp_path: Path) -> None:
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(json.dumps({"inbound": {"feishu_ws": {}}}), encoding="utf-8")

    settings = build_feishu_ws_settings(
        assistant_config_path=str(assistant_config_path),
        environ={
            "OM_FEISHU_BOT_APP_ID": "bot_app",
            "OM_FEISHU_BOT_APP_SECRET": "bot_secret",
            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_1",
        },
    )

    assert settings.assistant_mode == "deterministic"
    assert settings.assistant_enabled is True
    assert settings.assistant_llm.enabled is False


def test_feishu_ws_check_reports_missing_sdk() -> None:
    settings = FeishuWsSettings(config_key="us", allowed_senders="feishu:ou_1", app_id="app_1", app_secret="secret_1")

    out = check_feishu_ws_settings(settings, sdk_available_fn=lambda: False)

    assert out["ok"] is False
    assert out["error"]["code"] == "CONFIG_ERROR"
    assert out["data"]["settings"]["sdk_available"] is False


def test_feishu_ws_check_requires_config_scope() -> None:
    settings = FeishuWsSettings(allowed_senders="feishu:ou_1", app_id="app_1", app_secret="secret_1")

    out = check_feishu_ws_settings(settings, sdk_available_fn=lambda: True)

    assert out["ok"] is False
    assert out["error"]["code"] == "CONFIG_ERROR"
    assert "runtime config scope" in out["error"]["message"]
    assert out["data"]["settings"]["config_key"] is None


def test_feishu_ws_serve_uses_background_worker(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    def _reply(**_kwargs: Any) -> dict[str, Any]:
        return {"code": 0}

    def _reaction(**_kwargs: Any) -> dict[str, Any]:
        return {"code": 0}

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(text="状态"))

    serve_feishu_ws(
        FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        reaction_fn=_reaction,
        execute_tool_fn=_execute,
        start_client_fn=_start_client,
        lock_path=tmp_path / "feishu-ws.lock",
    )

    assert calls == [("runtime_status", {"config_key": "us"})]


def test_feishu_ws_client_converts_sdk_event_model() -> None:
    event = SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(event_id="evt_1", event_type="im.message.receive_v1", create_time="1"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1", user_id="u_1", union_id="on_1"),
                sender_type="user",
                tenant_key="tenant_1",
            ),
            message=SimpleNamespace(
                message_id="msg_1",
                chat_id="oc_1",
                message_type="text",
                content='{"text":"状态"}',
            ),
        ),
    )

    payload = feishu_event_model_to_payload(event)

    assert payload["header"]["event_type"] == "im.message.receive_v1"
    assert payload["event"]["sender"]["sender_id"]["open_id"] == "ou_1"
    assert payload["event"]["message"]["content"] == '{"text":"状态"}'
