from __future__ import annotations

import json
from pathlib import Path


def test_normalize_feishu_app_send_output_marks_success_with_message_id() -> None:
    from src.application.notification_delivery_adapter import normalize_feishu_app_send_output

    out = normalize_feishu_app_send_output(
        send_result={
            "http_status": 200,
            "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
            "response_json": {"code": 0, "msg": "success", "data": {"message_id": "om_123"}},
            "response_tail": '{"code":0}',
        }
    )

    assert out["ok"] is True
    assert out["command_ok"] is True
    assert out["delivery_confirmed"] is True
    assert out["message_id"] == "om_123"
    assert out["returncode"] == 0


def test_normalize_feishu_app_send_output_exposes_idempotency_and_retry_diagnostics() -> None:
    from src.application.notification_delivery_adapter import normalize_feishu_app_send_output

    out = normalize_feishu_app_send_output(
        send_result={
            "http_status": 200,
            "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
            "response_json": {"code": 0, "msg": "success", "data": {"message_id": "om_123"}},
            "response_tail": '{"code":0}',
            "idempotency_key": "idem-1",
            "http_attempts": [
                {"level": "warn", "category": "transient", "http_status": 500, "feishu_code": 2200, "attempt": 1},
                {"level": "info", "category": "success", "http_status": 200, "feishu_code": 0, "attempt": 2, "message_id": "om_123"},
            ],
        }
    )

    assert out["ok"] is True
    assert out["idempotency_key"] == "idem-1"
    assert out["retry_attempt_count"] == 1
    assert out["ambiguous_send"] is True
    assert out["duplicate_risk"] is False
    assert len(out["http_attempts"]) == 2


def test_normalize_feishu_app_send_output_marks_unconfirmed_when_message_id_missing() -> None:
    from src.application.notification_delivery_adapter import normalize_feishu_app_send_output

    out = normalize_feishu_app_send_output(
        send_result={
            "http_status": 200,
            "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
            "response_json": {"code": 0, "msg": "success", "data": {}},
            "response_tail": '{"code":0}',
        }
    )

    assert out["ok"] is False
    assert out["command_ok"] is True
    assert out["delivery_confirmed"] is False
    assert out["message_id"] is None
    assert out["returncode"] == 0
    assert "message_id is missing" in str(out["message"])


def test_normalize_feishu_app_send_output_marks_failed_on_non_200() -> None:
    from src.application.notification_delivery_adapter import normalize_feishu_app_send_output

    out = normalize_feishu_app_send_output(
        send_result={
            "http_status": 500,
            "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
            "response_json": {"code": 999, "msg": "oops"},
            "response_tail": "oops-tail",
        }
    )

    assert out["ok"] is False
    assert out["command_ok"] is False
    assert out["delivery_confirmed"] is False
    assert out["returncode"] == 1
    assert out["http_status"] == 500
    assert out["feishu_code"] == 999
    assert out["feishu_msg"] == "oops"
    assert "request_path=/open-apis/im/v1/messages?receive_id_type=open_id" in str(out["message"])


def test_normalize_feishu_app_send_output_marks_failed_on_feishu_code() -> None:
    from src.application.notification_delivery_adapter import normalize_feishu_app_send_output

    out = normalize_feishu_app_send_output(
        send_result={
            "http_status": 200,
            "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
            "response_json": {"code": 230001, "msg": "denied", "data": {}},
            "response_tail": "denied-tail",
        }
    )

    assert out["ok"] is False
    assert out["command_ok"] is True
    assert out["delivery_confirmed"] is False
    assert out["returncode"] == 0
    assert out["feishu_code"] == 230001
    assert out["feishu_msg"] == "denied"


def test_send_feishu_app_message_uses_bot_user_open_id_when_target_empty(monkeypatch, tmp_path: Path) -> None:
    from src.application import notification_delivery_adapter as service

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "sec_1")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_1")
    captured: dict[str, str] = {}

    def _send_text_message(**kwargs):  # type: ignore[no-untyped-def]
        captured.update({key: str(value) for key, value in kwargs.items() if key in {"app_id", "app_secret", "open_id", "text", "uuid"}})
        return {"code": 0, "msg": "success", "data": {"message_id": "om_1"}}

    monkeypatch.setattr(service, "send_text_message", _send_text_message)

    out = service.send_feishu_app_message(
        base=tmp_path,
        channel="feishu_app",
        target="",
        message="hello",
        notifications={},
        idempotency_key="idem-1",
    )

    assert out["ok"] is True
    assert captured["app_id"] == "cli_1"
    assert captured["app_secret"] == "sec_1"
    assert captured["open_id"] == "ou_1"
    assert captured["text"] == "hello"
    assert captured["uuid"] == "idem-1"


def test_send_feishu_app_message_ignores_config_target_for_bot_channel(monkeypatch, tmp_path: Path) -> None:
    from src.application import notification_delivery_adapter as service

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "sec_1")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")
    captured: dict[str, str] = {}

    def _send_text_message(**kwargs):  # type: ignore[no-untyped-def]
        captured.update({key: str(value) for key, value in kwargs.items() if key in {"open_id"}})
        return {"code": 0, "msg": "success", "data": {"message_id": "om_1"}}

    monkeypatch.setattr(service, "send_text_message", _send_text_message)

    out = service.send_feishu_app_message(
        base=tmp_path,
        channel="feishu_app",
        target="ou_other",
        message="hello",
        notifications={},
    )

    assert out["ok"] is True
    assert captured["open_id"] == "ou_bot"


def test_select_notification_delivery_adapter_keeps_feishu_app_provider_on_app_sender() -> None:
    from src.application.notification_delivery_adapter import (
        normalize_feishu_app_send_output,
        select_notification_delivery_adapter,
        send_feishu_app_message_process,
    )

    adapter = select_notification_delivery_adapter("feishu_app")

    assert adapter.send_fn is send_feishu_app_message_process
    assert adapter.normalize_fn is normalize_feishu_app_send_output
    assert adapter.failure_stage == "send_feishu_app_message"


def test_notification_channel_registry_exposes_channel_capabilities() -> None:
    from src.application.notification_delivery_adapter import build_notification_channel_registry

    registry = build_notification_channel_registry()
    feishu = registry.require("feishu_app")
    wechat = registry.require("wechat_clawbot")

    assert feishu.capabilities.outbound is True
    assert feishu.capabilities.inbound is True
    assert feishu.capabilities.reply is True
    assert feishu.inbound_fn is not None
    inbound = feishu.inbound_fn({"header": {"event_type": "im.message.message_read_v1"}, "event": {}})
    assert inbound["ok"] is True
    assert inbound["data"]["kind"] == "ignored_event"
    assert wechat.capabilities.outbound is True
    assert wechat.capabilities.bind is True
    assert wechat.capabilities.inbound is True
    assert wechat.capabilities.reply is True
    assert wechat.inbound_fn is not None
    wechat_inbound = wechat.inbound_fn({"message_id": "msg_1"})
    assert wechat_inbound["ok"] is True
    assert wechat_inbound["data"]["kind"] == "ignored_message"


def test_select_notification_delivery_adapter_routes_wechat_clawbot_directly() -> None:
    from src.application.channels.wechat_clawbot.notification import (
        normalize_wechat_clawbot_send_output,
        send_wechat_clawbot_message_process,
    )
    from src.application.notification_delivery_adapter import (
        select_notification_delivery_adapter,
    )

    adapter = select_notification_delivery_adapter("wechat_clawbot")

    assert adapter.send_fn is send_wechat_clawbot_message_process
    assert adapter.normalize_fn is normalize_wechat_clawbot_send_output
    assert adapter.failure_stage == "send_wechat_clawbot_message"


def test_send_wechat_clawbot_message_uses_bound_context(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.notification import send_wechat_clawbot_message

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            captured["bot_token"] = bot_token
            captured["base_url"] = base_url
            captured["timeout"] = timeout

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {"ret": 0, "data": {"message_id": "msg_1"}}

    state_dir = tmp_path / "wechat"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "wx_user_1",
                        "context_token": "ctx_1",
                        "group_id": "group_1",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = send_wechat_clawbot_message(
        base=tmp_path,
        channel="wechat_clawbot",
        target="ops",
        message="hello",
        notifications={"wechat_clawbot_state_dir": str(state_dir), "send_timeout_sec": 12},
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["message_id"] == "msg_1"
    assert captured["bot_token"] == "bot_1"
    assert captured["base_url"] == "https://example.invalid"
    assert captured["timeout"] == 12
    assert captured["to_user_id"] == "wx_user_1"
    assert captured["context_token"] == "ctx_1"
    assert captured["group_id"] == "group_1"
    assert captured["text"] == "hello"


def test_wechat_clawbot_client_wraps_sendmessage_payload_in_msg() -> None:
    from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient

    captured: dict[str, object] = {}

    def fake_http_json(method, url, payload, *, headers, timeout):  # type: ignore[no-untyped-def]
        captured.update({"method": method, "url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {"ret": 0, "data": {"message_id": "msg_1"}}

    client = WechatClawbotClient(
        bot_token="bot_1",
        base_url="https://example.invalid",
        timeout=12,
        http_json_fn=fake_http_json,
    )

    out = client.send_text_message(
        to_user_id="wx_user_1",
        context_token="ctx_1",
        text="hello",
        group_id="group_1",
    )

    assert out == {"ret": 0, "data": {"message_id": "msg_1"}}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.invalid/ilink/bot/sendmessage"
    assert captured["timeout"] == 12
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert isinstance(payload["client_id"], str)
    assert payload["client_id"]
    assert payload["base_info"] == {"session_id": "", "scene": ""}
    assert payload["msg"] == {
        "message_type": 2,
        "message_state": 2,
        "context_token": "ctx_1",
        "from_user_id": "",
        "to_user_id": "wx_user_1",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        "group_id": "group_1",
    }
    assert captured["headers"]["Authorization"] == "Bearer bot_1"  # type: ignore[index]


def test_wechat_clawbot_client_sends_typing_payloads() -> None:
    from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient

    calls: list[dict[str, object]] = []

    def fake_http_json(method, url, payload, *, headers, timeout):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if str(url).endswith("/getconfig"):
            return {"ret": 0, "typing_ticket": "ticket_1"}
        return {"ret": 0}

    client = WechatClawbotClient(
        bot_token="bot_1",
        base_url="https://example.invalid",
        timeout=12,
        http_json_fn=fake_http_json,
    )

    assert client.get_config(ilink_user_id="user_1", context_token="ctx_1") == {"ret": 0, "typing_ticket": "ticket_1"}
    assert client.send_typing(ilink_user_id="user_1", typing_ticket="ticket_1", status=1) == {"ret": 0}
    assert calls[0]["url"] == "https://example.invalid/ilink/bot/getconfig"
    assert calls[0]["payload"] == {"ilink_user_id": "user_1", "context_token": "ctx_1"}
    assert calls[1]["url"] == "https://example.invalid/ilink/bot/sendtyping"
    assert calls[1]["payload"] == {"ilink_user_id": "user_1", "typing_ticket": "ticket_1", "status": 1}
    assert calls[1]["headers"]["Authorization"] == "Bearer bot_1"  # type: ignore[index]


def test_wechat_clawbot_success_without_upstream_message_id_is_unconfirmed() -> None:
    from src.application.channels.wechat_clawbot.notification import normalize_wechat_clawbot_send_output

    out = normalize_wechat_clawbot_send_output(
        send_result={
            "ok": True,
            "http_status": 200,
            "response_json": {"ret": 0},
            "response_tail": '{"ret":0}',
            "local_receipt_id": "om-local-1",
            "message_id": None,
        }
    )

    assert out["command_ok"] is True
    assert out["delivery_confirmed"] is False
    assert out["ok"] is False
    assert out["message_id"] is None
    assert out["local_receipt_id"] == "om-local-1"
    assert "upstream message_id is missing" in out["message"]


def test_send_wechat_clawbot_message_does_not_synthesize_message_id(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.notification import send_wechat_clawbot_message

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            pass

        def send_text_message(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {"ret": 0}

    state_dir = tmp_path / "wechat"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps({"bindings": {"ops": {"to_user_id": "wx_user_1", "context_token": "ctx_1"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = send_wechat_clawbot_message(
        base=tmp_path,
        channel="wechat_clawbot",
        target="ops",
        message="hello",
        notifications={"wechat_clawbot_state_dir": str(state_dir)},
        idempotency_key="om-idem-1",
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["message_id"] is None
    assert out["local_receipt_id"] == "om-idem-1"


def test_select_notification_delivery_adapter_rejects_openclaw_provider() -> None:
    from src.application.notification_delivery_adapter import select_notification_delivery_adapter

    try:
        select_notification_delivery_adapter("openclaw")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unsupported notification provider" in str(exc)
        assert "wechat_clawbot" in str(exc)


def test_select_notification_delivery_adapter_rejects_unknown_provider() -> None:
    from src.application.notification_delivery_adapter import select_notification_delivery_adapter

    try:
        select_notification_delivery_adapter("sms")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unsupported notification provider" in str(exc)
