from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.application.agent_tool_contracts import build_response
from src.application.inbound.feishu_ws import (
    FeishuWsSettings,
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_ws_event,
    serve_feishu_ws,
)
from src.infrastructure.feishu_ws_client import feishu_event_model_to_payload


def _message_payload(*, sender: str = "ou_1", text: str = "收益 sy 2026-05") -> dict:
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
    replies: list[dict] = []
    reactions: list[dict] = []
    calls: list[tuple[str, dict]] = []

    def _execute(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    def _reply(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _reaction(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        reactions.append(dict(kwargs))
        return {"code": 0, "data": {"reaction_id": "reaction_1"}}

    out = handle_feishu_ws_event(
        _message_payload(),
        settings=FeishuWsSettings(
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


def test_feishu_ws_reaction_failure_does_not_fail_inbound_or_reply(tmp_path: Path) -> None:
    replies: list[dict] = []

    def _execute(tool_name: str, payload: dict) -> dict:
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    def _reply(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    def _reaction(**_kwargs) -> dict:  # type: ignore[no-untyped-def]
        raise RuntimeError("no permission")

    out = handle_feishu_ws_event(
        _message_payload(text="状态"),
        settings=FeishuWsSettings(
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
    replies: list[dict] = []
    reactions: list[dict] = []

    def _reply(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        replies.append(dict(kwargs))
        return {"code": 0}

    def _reaction(**kwargs) -> dict:  # type: ignore[no-untyped-def]
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


def test_feishu_ws_settings_reads_behavior_from_runtime_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.us.json"
    config_path.write_text(
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
                }
            }
        ),
        encoding="utf-8",
    )

    settings = build_feishu_ws_settings(
        config_path=str(config_path),
        queue_size=5,
        environ={
            "OM_FEISHU_BOT_APP_ID": "bot_app",
            "OM_FEISHU_BOT_APP_SECRET": "bot_secret",
            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_1",
            "OM_FEISHU_WS_QUEUE_SIZE": "99",
        },
    )

    assert settings.reply_enabled is False
    assert settings.reply_in_thread is True
    assert settings.max_reply_chars == 1200
    assert settings.ack_reaction == "SMILE"
    assert settings.queue_size == 5


def test_feishu_ws_check_reports_missing_sdk() -> None:
    settings = FeishuWsSettings(allowed_senders="feishu:ou_1", app_id="app_1", app_secret="secret_1")

    out = check_feishu_ws_settings(settings, sdk_available_fn=lambda: False)

    assert out["ok"] is False
    assert out["error"]["code"] == "CONFIG_ERROR"
    assert out["data"]["settings"]["sdk_available"] is False


def test_feishu_ws_serve_uses_background_worker(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def _execute(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    def _reply(**_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"code": 0}

    def _reaction(**_kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {"code": 0}

    def _start_client(**kwargs) -> None:  # type: ignore[no-untyped-def]
        kwargs["on_event"](_message_payload(text="状态"))

    serve_feishu_ws(
        FeishuWsSettings(
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
