from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.audit import InboundAuditStore
from src.application.copilot.contracts import AppResult
import src.application.inbound.feishu_ws as feishu_ws
from src.application.inbound.feishu import prepare_feishu_ack_target
from src.application.inbound.feishu_ws import (
    FeishuWsSettings,
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_ws_event,
    serve_feishu_ws,
)
from src.infrastructure.feishu_ws_client import feishu_event_model_to_payload


def _message_payload(
    *,
    sender: str = "ou_1",
    text: str = "/income sy 2026-05",
    message_id: str = "msg_1",
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_prepare_feishu_ack_target_accepts_allowed_text_message() -> None:
    out = prepare_feishu_ack_target(_message_payload(), allowed_senders="feishu:ou_1")

    assert out["ready"] is True
    assert out["reason"] == "accepted_sender"
    assert out["message_id"] == "msg_1"
    assert out["sender_decision"]["reason"] == "matched_allowlist"


def test_prepare_feishu_ack_target_rejects_denied_sender() -> None:
    out = prepare_feishu_ack_target(_message_payload(sender="ou_denied"), allowed_senders="feishu:ou_1")

    assert out["ready"] is False
    assert out["reason"] == "permission_denied"
    assert out["sender_decision"]["reason"] == "sender_not_allowed"


def test_prepare_feishu_ack_target_requires_supported_text_and_message_id() -> None:
    unsupported = _message_payload()
    unsupported["header"]["event_type"] = "im.message.recalled_v1"
    assert prepare_feishu_ack_target(unsupported, allowed_senders="feishu:ou_1") == {
        "ready": False,
        "reason": "unsupported_event",
    }

    non_text = _message_payload()
    non_text["event"]["message"]["message_type"] = "image"
    assert prepare_feishu_ack_target(non_text, allowed_senders="feishu:ou_1") == {
        "ready": False,
        "reason": "invalid_message",
    }

    missing_message_id = _message_payload()
    missing_message_id["event"]["message"].pop("message_id")
    missing_message_id["header"].pop("event_id")
    assert prepare_feishu_ack_target(missing_message_id, allowed_senders="feishu:ou_1") == {
        "ready": False,
        "reason": "invalid_message",
    }


def test_prepare_feishu_ack_target_handles_malformed_payload() -> None:
    assert prepare_feishu_ack_target({}, allowed_senders="feishu:ou_1") == {
        "ready": False,
        "reason": "unsupported_event",
    }

    malformed = _message_payload()
    malformed["event"]["sender"] = {}
    assert prepare_feishu_ack_target(malformed, allowed_senders="feishu:ou_1") == {
        "ready": False,
        "reason": "invalid_message",
    }


def test_prepare_feishu_ack_target_reuses_public_allowlist_policy() -> None:
    wildcard = prepare_feishu_ack_target(_message_payload(sender="ou_any"), allowed_senders="feishu:*")
    channel_specific = prepare_feishu_ack_target(_message_payload(), allowed_senders="slack:ou_1")

    assert wildcard["ready"] is True
    assert channel_specific["ready"] is False
    assert channel_specific["reason"] == "permission_denied"


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
            ack_reaction="Typing",
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
    assert reactions[0]["emoji_type"] == "Typing"
    assert replies[0]["message_id"] == "msg_1"
    assert replies[0]["text"].startswith("收益统计完成")
    assert out["data"]["reply"]["outbound_message_id"] == "reply_1"

    with sqlite3.connect(tmp_path / "audit.sqlite3") as conn:
        response_json = conn.execute("SELECT response_json FROM inbound_command_audit").fetchone()[0]
    stored = json.loads(response_json)
    reply_receipt = stored["data"]["reply"]
    assert reply_receipt["schema_version"] == "feishu-reply-receipt-v1"
    assert reply_receipt["inbound_message_id"] == "msg_1"
    assert reply_receipt["message_id"] == "reply_1"
    assert reply_receipt["outbound_message_id"] == "reply_1"
    assert reply_receipt["delivery_confirmed"] is True
    assert reply_receipt["api_response"]["data"]["message_id"] == "reply_1"


def test_feishu_ws_failed_business_response_remains_retryable(tmp_path: Path) -> None:
    from src.application.copilot.host_store import CopilotHostStore

    class FakeChannelService:
        def handle_inbound(self, channel: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            del channel, payload, kwargs
            return build_response(
                tool_name="inbound.feishu",
                ok=True,
                data={
                    "kind": "message",
                    "request": {"message_id": "msg_1"},
                    "response_text": "channel service reply",
                    "inbound_result": build_response(
                        tool_name="assistant.handle",
                        ok=True,
                        data={"command_id": "cmd_failed", "response_text": "channel service reply"},
                    ),
                },
            )

    database = tmp_path / "audit.sqlite3"
    out = handle_feishu_ws_event(
        _message_payload(),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(database),
        ),
        reply_fn=lambda **_kwargs: {"code": 230001, "msg": "temporary failure"},
        channel_service=FakeChannelService(),  # type: ignore[arg-type]
    )

    assert out["ok"] is False
    assert out["data"]["reply"]["reason"] == "reply_failed"
    record = CopilotHostStore(database).list_replies()[0]
    assert record["status"] == "retryable_failed"
    assert record["attempt_count"] == 1


def test_feishu_ws_routes_inbound_through_channel_service() -> None:
    inbound_calls: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []

    class FakeChannelService:
        def handle_inbound(self, channel: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            inbound_calls.append({"channel": channel, "payload": payload, "kwargs": kwargs})
            return build_response(
                tool_name="inbound.feishu",
                ok=True,
                data={
                    "kind": "message",
                    "request": {"message_id": "msg_1"},
                    "response_text": "channel service reply",
                    "inbound_result": build_response(
                        tool_name="assistant.handle",
                        ok=True,
                        data={"command_id": "cmd_1", "response_text": "channel service reply"},
                    ),
                },
            )

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    out = handle_feishu_ws_event(
        _message_payload(),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            reply_enabled=True,
        ),
        reply_fn=_reply,
        channel_service=FakeChannelService(),  # type: ignore[arg-type]
    )

    assert out["ok"] is True
    assert inbound_calls[0]["channel"] == "feishu_app"
    assert inbound_calls[0]["payload"]["event"]["message"]["message_id"] == "msg_1"
    assert inbound_calls[0]["kwargs"]["config_key"] == "us"
    assert inbound_calls[0]["kwargs"]["allowed_senders"] == "feishu:ou_1"
    assert replies[0]["text"] == "channel service reply"
    assert replies[0]["uuid"] == "cmd_1"


def test_feishu_ws_can_route_through_assistant(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(json.dumps({"assistant": {"enabled": True, "copilot": {"enabled": False}}}), encoding="utf-8")

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
    assert inbound_result["data"]["control"]["intent_name"] == "runtime_status"
    assert inbound_result["meta"]["assistant"]["route"] == "deterministic_control"
    assert "llm" not in inbound_result["meta"]["assistant"]


def test_feishu_ws_routes_free_form_cashflow_question_to_copilot(monkeypatch: Any, tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps(
            {
                "assistant": {
                    "enabled": True,
                    "copilot": {"enabled": True},
                    "llm": {"provider": "openai", "model": "gpt-5.2", "api_key_env": "OM_LLM_API_KEY"},
                }
            }
        ),
        encoding="utf-8",
    )

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"unexpected": True})

    copilot_calls: list[dict[str, Any]] = []

    def _run_channel_request(**kwargs: Any) -> AppResult:
        copilot_calls.append(dict(kwargs))
        return AppResult(status="completed", user_response="结论：6月净现金流来自已实现收益和权利金。")

    monkeypatch.setattr("src.application.assistant.inbound_service.run_channel_request", _run_channel_request)

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

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
    )

    inbound_result = out["data"]["inbound"]["data"]["inbound_result"]
    assert out["ok"] is True
    assert calls == []
    assert copilot_calls[0]["user_message"] == "分析 lx 6月的净现金流明细"
    assert replies
    assert replies[0]["text"].startswith("结论：")
    assert inbound_result["data"]["decision"]["reason"] == "copilot_freeform"
    assert inbound_result["meta"]["assistant"]["route"] == "copilot"


def test_feishu_ws_free_form_copilot_does_not_read_legacy_audit_context(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    replies: list[dict[str, Any]] = []
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps(
            {
                "assistant": {
                    "copilot": {"enabled": True},
                    "llm": {"provider": "openai", "model": "gpt-5.2", "api_key_env": "OM_LLM_API_KEY"},
                }
            }
        ),
        encoding="utf-8",
    )

    def _broken_list_recent(self: InboundAuditStore, **_kwargs: Any) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(InboundAuditStore, "list_recent", _broken_list_recent)

    monkeypatch.setattr(
        "src.application.assistant.inbound_service.run_channel_request",
        lambda **_kwargs: AppResult(status="completed", user_response="结论：系统运行正常。"),
    )

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "schema_version": "analysis.query.output.v2",
                "views_used": list(payload.get("views") or []),
                "view_datasets": {"runtime_tick_status": {"rows": [{"status": "ok"}]}},
            },
        )

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    out = handle_feishu_ws_event(
        _message_payload(text="系统健康检查"),
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
    )

    inbound_result = out["data"]["inbound"]["data"]["inbound_result"]
    assert out["ok"] is True
    assert replies
    assert replies[0]["text"] == "结论：系统运行正常。"
    assert inbound_result["meta"]["assistant"]["route"] == "copilot"


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
        _message_payload(text="/status"),
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
            text="/record-open sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
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
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text("{}", encoding="utf-8")

    settings = build_feishu_ws_settings(
        config_path=str(config_path),
        assistant_config_path=str(assistant_config_path),
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
                        "copilot": {"enabled": True},
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
    assert settings.assistant_enabled is True
    assert settings.assistant_copilot_enabled is True
    assert settings.assistant_context_window_messages == 9
    assert settings.assistant_llm.enabled is True
    assert settings.assistant_llm.provider == "openai"
    assert settings.assistant_llm.base_url == "https://llm.example/v1"
    assert settings.assistant_llm.model == "gpt-5.2"
    assert settings.assistant_llm.confidence_min == 0.8
    assert settings.assistant_llm.timeout_seconds == 31
    assert settings.assistant_llm.max_output_tokens == 769


def test_feishu_ws_settings_preserves_official_mixed_case_reaction(tmp_path: Path) -> None:
    assistant_config_path = tmp_path / "config.assistant.json"
    assistant_config_path.write_text(
        json.dumps({"inbound": {"feishu_ws": {"ack_reaction": "Typing"}}}),
        encoding="utf-8",
    )

    settings = build_feishu_ws_settings(
        assistant_config_path=str(assistant_config_path),
        environ={
            "OM_FEISHU_BOT_APP_ID": "bot_app",
            "OM_FEISHU_BOT_APP_SECRET": "bot_secret",
            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_1",
        },
    )

    assert settings.ack_reaction == "Typing"


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

    assert settings.assistant_enabled is True
    assert settings.assistant_copilot_enabled is False
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
    reactions: list[dict[str, Any]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    def _reply(**_kwargs: Any) -> dict[str, Any]:
        return {"code": 0}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(dict(kwargs))
        return {"code": 0}

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(text="/status"))

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
    assert reactions == []


def test_feishu_ws_service_ack_is_independent_from_blocked_business(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    business_started = threading.Event()
    release_business = threading.Event()
    b_reacted = threading.Event()
    reactions: list[str] = []

    def _handle(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        message_id = payload["event"]["message"]["message_id"]
        if message_id == "msg_a":
            business_started.set()
            assert release_business.wait(2)
        return {"ok": True}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(kwargs["message_id"])
        if kwargs["message_id"] == "msg_b":
            b_reacted.set()
        return {"code": 0}

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", _handle)

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(message_id="msg_a"))
        assert business_started.wait(2)
        kwargs["on_event"](_message_payload(message_id="msg_b"))
        assert b_reacted.wait(2)
        assert release_business.is_set() is False
        release_business.set()

    serve_feishu_ws(
        FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            queue_size=2,
        ),
        reaction_fn=_reaction,
        start_client_fn=_start_client,
        lock_path=tmp_path / "feishu-ws-independent.lock",
    )

    assert reactions == ["msg_a", "msg_b"]


def test_feishu_ws_service_does_not_ack_business_queue_rejection(
    monkeypatch: Any,
    caplog,
    tmp_path: Path,
) -> None:
    business_started = threading.Event()
    release_business = threading.Event()
    a_reacted = threading.Event()
    b_reacted = threading.Event()
    reactions: list[str] = []

    def _handle(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if payload["event"]["message"]["message_id"] == "msg_a":
            business_started.set()
            assert release_business.wait(5)
        return {"ok": True}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(kwargs["message_id"])
        if kwargs["message_id"] == "msg_a":
            a_reacted.set()
        if kwargs["message_id"] == "msg_b":
            b_reacted.set()
        return {"code": 0}

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", _handle)

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(message_id="msg_a"))
        assert business_started.wait(2)
        assert a_reacted.wait(2)
        kwargs["on_event"](_message_payload(message_id="msg_b"))
        kwargs["on_event"](_message_payload(message_id="msg_c"))
        assert b_reacted.wait(2)
        assert "msg_c" not in reactions
        release_business.set()

    with caplog.at_level(logging.INFO):
        serve_feishu_ws(
            FeishuWsSettings(
                config_key="us",
                allowed_senders="feishu:ou_1",
                app_id="app_1",
                app_secret="secret_1",
                ack_reaction="SMILE",
                queue_size=1,
            ),
            reaction_fn=_reaction,
            start_client_fn=_start_client,
            lock_path=tmp_path / "feishu-ws-queue-full.lock",
        )

    assert reactions == ["msg_a", "msg_b"]
    assert any(
        record.levelno == logging.WARNING
        and "feishu_ws_dispatch" in record.getMessage()
        and "business=queue_full" in record.getMessage()
        for record in caplog.records
    )


def test_feishu_ws_service_handler_preserves_reaction_classification(tmp_path: Path) -> None:
    reactions: list[dict[str, Any]] = []

    allowed = handle_feishu_ws_event(
        _message_payload(text="/status"),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            audit_db=str(tmp_path / "allowed.sqlite3"),
        ),
        reaction_fn=lambda **kwargs: reactions.append(dict(kwargs)) or {"code": 0},
        reply_fn=lambda **_kwargs: {"code": 0},
        execute_tool_fn=lambda tool_name, payload: build_response(
            tool_name=tool_name,
            ok=True,
            data={"status": "ok"},
        ),
        react_in_handler=False,
    )
    denied = handle_feishu_ws_event(
        _message_payload(sender="ou_bad", text="status"),
        settings=FeishuWsSettings(
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            audit_db=str(tmp_path / "denied.sqlite3"),
        ),
        reaction_fn=lambda **kwargs: reactions.append(dict(kwargs)) or {"code": 0},
        reply_fn=lambda **_kwargs: {"code": 0},
        react_in_handler=False,
    )
    disabled = handle_feishu_ws_event(
        _message_payload(text="/status"),
        settings=FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            audit_db=str(tmp_path / "disabled.sqlite3"),
        ),
        reply_fn=lambda **_kwargs: {"code": 0},
        execute_tool_fn=lambda tool_name, payload: build_response(
            tool_name=tool_name,
            ok=True,
            data={"status": "ok"},
        ),
        react_in_handler=False,
    )

    assert allowed["data"]["reaction"]["reason"] == "transport_managed"
    assert denied["data"]["reaction"]["reason"] == "permission_denied"
    assert disabled["data"]["reaction"]["reason"] == "reaction_disabled"
    assert reactions == []


def test_feishu_ack_worker_bounds_queue_and_drops_stale(caplog) -> None:
    reaction_started = threading.Event()
    release_reaction = threading.Event()

    def _reaction(**_kwargs: Any) -> dict[str, Any]:
        reaction_started.set()
        assert release_reaction.wait(2)
        return {"code": 0}

    worker = feishu_ws._FeishuAckWorker(
        settings=FeishuWsSettings(
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
            queue_size=4,
        ),
        reaction_fn=_reaction,
    )
    worker.start()
    now = time.monotonic()
    assert worker.submit(message_id="active", emoji_type="SMILE", received_monotonic=now, event_ref="active") == "accepted"
    assert reaction_started.wait(2)
    for index in range(4):
        assert worker.submit(
            message_id=f"queued-{index}",
            emoji_type="SMILE",
            received_monotonic=now,
            event_ref=f"queued-{index}",
        ) == "accepted"
    assert worker.submit(
        message_id="overflow",
        emoji_type="SMILE",
        received_monotonic=now,
        event_ref="overflow",
    ) == "queue_full"
    release_reaction.set()
    worker.stop()
    assert worker.submit(
        message_id="late",
        emoji_type="SMILE",
        received_monotonic=now,
        event_ref="late",
    ) == "stopped"

    stale_logged = threading.Event()
    original_warning = feishu_ws.LOG.warning

    def _warning(message: str, *args: Any, **kwargs: Any) -> None:
        if "status=stale_dropped" in message:
            stale_logged.set()
        original_warning(message, *args, **kwargs)

    with caplog.at_level(logging.WARNING):
        stale_worker = feishu_ws._FeishuAckWorker(
            settings=FeishuWsSettings(app_id="app_1", app_secret="secret_1", ack_reaction="SMILE"),
            reaction_fn=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("stale job must not send")),
        )
        feishu_ws.LOG.warning = _warning
        try:
            stale_worker.start()
            assert stale_worker.submit(
                message_id="stale",
                emoji_type="SMILE",
                received_monotonic=time.monotonic() - 4,
                event_ref="stale",
            ) == "accepted"
            assert stale_logged.wait(2)
        finally:
            stale_worker.stop()
            feishu_ws.LOG.warning = original_warning
    assert "status=stale_dropped" in caplog.text


def test_feishu_ack_worker_shutdown_drops_queued_job(caplog) -> None:
    reaction_started = threading.Event()
    release_reaction = threading.Event()
    queued_dropped = threading.Event()
    calls: list[str] = []

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["message_id"])
        reaction_started.set()
        assert release_reaction.wait(2)
        return {"code": 0}

    worker = feishu_ws._FeishuAckWorker(
        settings=FeishuWsSettings(app_id="app_1", app_secret="secret_1", ack_reaction="SMILE"),
        reaction_fn=_reaction,
    )
    original_warning = feishu_ws.LOG.warning

    def _warning(message: str, *args: Any, **kwargs: Any) -> None:
        if "status=shutdown_dropped" in message:
            queued_dropped.set()
        original_warning(message, *args, **kwargs)

    feishu_ws.LOG.warning = _warning
    try:
        worker.start()
        now = time.monotonic()
        assert worker.submit(message_id="active", emoji_type="SMILE", received_monotonic=now, event_ref="active") == "accepted"
        assert reaction_started.wait(2)
        assert worker.submit(message_id="queued", emoji_type="SMILE", received_monotonic=now, event_ref="queued") == "accepted"
        stop_thread = threading.Thread(target=worker.stop)
        stop_thread.start()
        assert queued_dropped.wait(2)
        assert calls == ["active"]
        release_reaction.set()
        stop_thread.join(2)
        assert stop_thread.is_alive() is False
    finally:
        release_reaction.set()
        feishu_ws.LOG.warning = original_warning


def test_feishu_ws_preflight_failure_does_not_block_business(
    monkeypatch: Any,
    caplog,
    tmp_path: Path,
) -> None:
    business_done = threading.Event()
    reactions: list[dict[str, Any]] = []

    monkeypatch.setattr(
        feishu_ws,
        "prepare_feishu_ack_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("preflight failed")),
    )
    monkeypatch.setattr(
        feishu_ws,
        "handle_feishu_ws_event",
        lambda *_args, **_kwargs: business_done.set() or {"ok": True},
    )

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload())
        assert business_done.wait(2)

    with caplog.at_level(logging.INFO):
        serve_feishu_ws(
            FeishuWsSettings(
                config_key="us",
                allowed_senders="feishu:ou_1",
                app_id="app_1",
                app_secret="secret_1",
                ack_reaction="SMILE",
            ),
            reaction_fn=lambda **kwargs: reactions.append(dict(kwargs)) or {"code": 0},
            start_client_fn=_start_client,
            lock_path=tmp_path / "feishu-ws-preflight-error.lock",
        )

    assert business_done.is_set()
    assert reactions == []
    assert any(
        record.levelno == logging.WARNING
        and "feishu_ws_dispatch" in record.getMessage()
        and "ack_reason=preflight_failed" in record.getMessage()
        for record in caplog.records
    )


def test_feishu_ws_service_logs_are_sanitized(
    monkeypatch: Any,
    caplog,
    tmp_path: Path,
) -> None:
    reaction_done = threading.Event()
    business_done = threading.Event()
    payload = _message_payload(
        sender="ou_secret_sender",
        text="secret message body",
        message_id="msg_secret_identifier",
    )

    monkeypatch.setattr(
        feishu_ws,
        "handle_feishu_ws_event",
        lambda *_args, **_kwargs: business_done.set() or {"ok": True},
    )

    def _reaction(**_kwargs: Any) -> dict[str, Any]:
        reaction_done.set()
        return {"code": 0}

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](payload)
        assert reaction_done.wait(2)
        assert business_done.wait(2)

    with caplog.at_level(logging.INFO):
        serve_feishu_ws(
            FeishuWsSettings(
                config_key="us",
                allowed_senders="feishu:ou_secret_sender",
                app_id="app_secret_id",
                app_secret="app_secret_value",
                ack_reaction="SMILE",
            ),
            reaction_fn=_reaction,
            start_client_fn=_start_client,
            lock_path=tmp_path / "feishu-ws-sanitized.lock",
        )

    assert "feishu_ws_dispatch" in caplog.text
    assert "feishu_ws_ack" in caplog.text
    assert "feishu_ws_event" in caplog.text
    for secret in (
        "ou_secret_sender",
        "secret message body",
        "msg_secret_identifier",
        "app_secret_id",
        "app_secret_value",
    ):
        assert secret not in caplog.text


def test_feishu_ws_service_sends_one_ack_and_keeps_reply_flow(tmp_path: Path) -> None:
    reaction_done = threading.Event()
    reply_done = threading.Event()
    reactions: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reactions.append(dict(kwargs))
        reaction_done.set()
        return {"code": 0}

    def _reply(**kwargs: Any) -> dict[str, Any]:
        replies.append(dict(kwargs))
        reply_done.set()
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(text="/status"))
        assert reaction_done.wait(2)
        assert reply_done.wait(2)

    serve_feishu_ws(
        FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="Typing",
            audit_db=str(tmp_path / "send-once.sqlite3"),
        ),
        reaction_fn=_reaction,
        reply_fn=_reply,
        execute_tool_fn=lambda tool_name, payload: build_response(
            tool_name=tool_name,
            ok=True,
            data={"status": "ok"},
        ),
        start_client_fn=_start_client,
        lock_path=tmp_path / "feishu-ws-send-once.lock",
    )

    assert len(reactions) == 1
    assert reactions[0]["message_id"] == "msg_1"
    assert reactions[0]["emoji_type"] == "Typing"
    assert len(replies) == 1


def test_feishu_ws_ack_queue_full_does_not_block_business(
    monkeypatch: Any,
    caplog,
    tmp_path: Path,
) -> None:
    reaction_started = threading.Event()
    release_reaction = threading.Event()
    all_business_done = threading.Event()
    business_ids: list[str] = []
    reaction_ids: list[str] = []

    def _handle(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        business_ids.append(payload["event"]["message"]["message_id"])
        if len(business_ids) == 6:
            all_business_done.set()
        return {"ok": True}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reaction_ids.append(kwargs["message_id"])
        reaction_started.set()
        assert release_reaction.wait(2)
        return {"code": 0}

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", _handle)

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload(message_id="msg_0"))
        assert reaction_started.wait(2)
        for index in range(1, 6):
            kwargs["on_event"](_message_payload(message_id=f"msg_{index}"))
        assert all_business_done.wait(2)
        release_reaction.set()

    with caplog.at_level(logging.INFO):
        serve_feishu_ws(
            FeishuWsSettings(
                config_key="us",
                allowed_senders="feishu:ou_1",
                app_id="app_1",
                app_secret="secret_1",
                ack_reaction="SMILE",
                queue_size=10,
            ),
            reaction_fn=_reaction,
            start_client_fn=_start_client,
            lock_path=tmp_path / "feishu-ws-ack-full.lock",
        )

    assert business_ids == [f"msg_{index}" for index in range(6)]
    assert reaction_ids == ["msg_0"]
    assert "ack=queue_full" in caplog.text
    assert any(
        record.levelno == logging.WARNING
        and "feishu_ws_dispatch" in record.getMessage()
        and "ack=queue_full" in record.getMessage()
        for record in caplog.records
    )


def test_feishu_ws_duplicate_delivery_gets_one_logical_ack_each(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    two_reactions = threading.Event()
    two_business_runs = threading.Event()
    reaction_ids: list[str] = []
    business_ids: list[str] = []

    def _handle(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        business_ids.append(payload["event"]["message"]["message_id"])
        if len(business_ids) == 2:
            two_business_runs.set()
        return {"ok": True}

    def _reaction(**kwargs: Any) -> dict[str, Any]:
        reaction_ids.append(kwargs["message_id"])
        if len(reaction_ids) == 2:
            two_reactions.set()
        return {"code": 0}

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", _handle)

    def _start_client(**kwargs: Any) -> None:
        duplicate = _message_payload(message_id="msg_duplicate")
        kwargs["on_event"](duplicate)
        assert two_reactions.is_set() is False
        kwargs["on_event"](duplicate)
        assert two_reactions.wait(2)
        assert two_business_runs.wait(2)

    serve_feishu_ws(
        FeishuWsSettings(
            config_key="us",
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            ack_reaction="SMILE",
        ),
        reaction_fn=_reaction,
        start_client_fn=_start_client,
        lock_path=tmp_path / "feishu-ws-duplicate.lock",
    )

    assert reaction_ids == ["msg_duplicate", "msg_duplicate"]
    assert business_ids == ["msg_duplicate", "msg_duplicate"]


def test_feishu_ws_service_reaction_failure_does_not_block_business(
    monkeypatch: Any,
    caplog,
    tmp_path: Path,
) -> None:
    reaction_attempted = threading.Event()
    business_done = threading.Event()

    monkeypatch.setattr(
        feishu_ws,
        "handle_feishu_ws_event",
        lambda *_args, **_kwargs: business_done.set() or {"ok": True},
    )

    def _reaction(**_kwargs: Any) -> dict[str, Any]:
        reaction_attempted.set()
        raise RuntimeError("reaction unavailable")

    def _start_client(**kwargs: Any) -> None:
        kwargs["on_event"](_message_payload())
        assert reaction_attempted.wait(2)
        assert business_done.wait(2)

    with caplog.at_level(logging.WARNING):
        serve_feishu_ws(
            FeishuWsSettings(
                config_key="us",
                allowed_senders="feishu:ou_1",
                app_id="app_1",
                app_secret="secret_1",
                ack_reaction="SMILE",
            ),
            reaction_fn=_reaction,
            start_client_fn=_start_client,
            lock_path=tmp_path / "feishu-ws-reaction-failure.lock",
        )

    assert reaction_attempted.is_set()
    assert business_done.is_set()
    assert "status=failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text


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
