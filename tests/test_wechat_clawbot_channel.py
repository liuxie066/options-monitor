from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def test_wechat_clawbot_qrcode_writes_pending_login(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import start_wechat_clawbot_qrcode

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            captured["bot_token"] = bot_token
            captured["base_url"] = base_url
            captured["timeout"] = timeout

        def get_bot_qrcode(self, *, bot_type: int):  # type: ignore[no-untyped-def]
            captured["bot_type"] = bot_type
            return {"data": {"qrcode": "qr_1", "qrcode_img_content": "https://example.invalid/qr.png"}}

    out = start_wechat_clawbot_qrcode(
        base=tmp_path,
        label="ops",
        state_dir=str(tmp_path / "wechat-state"),
        base_url="https://example.invalid",
        timeout_sec=7,
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["qrcode"] == "qr_1"
    assert out["data"]["qrcode_artifact_path"].endswith("login_qrcode.html")
    assert out["data"]["qrcode_artifact_open_command"].endswith("login_qrcode.html'")
    assert (tmp_path / "wechat-state" / "login_qrcode.html").exists()
    assert captured == {"bot_token": None, "base_url": "https://example.invalid", "timeout": 7, "bot_type": 3}
    pending = json.loads((tmp_path / "wechat-state" / "pending_login.json").read_text(encoding="utf-8"))
    assert pending["qrcode"] == "qr_1"
    assert pending["qrcode_artifact_path"].endswith("login_qrcode.html")
    assert pending["qrcode_artifact_open_command"].endswith("login_qrcode.html'")


def test_wechat_clawbot_state_store_lists_safe_bindings(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore

    state_dir = tmp_path / "wechat-state"
    store = WechatClawbotStateStore(state_dir)
    store.save_bindings(
        {
            "bindings": {
                "ops": {
                    "to_user_id": "user_1",
                    "context_token": "ctx_1",
                    "group_id": "group_1",
                }
            }
        }
    )

    safe = store.safe_bindings()

    assert safe["ops"]["to_user_id"] == "user_1"
    assert safe["ops"]["group_id"] == "group_1"
    assert "context_token" not in safe["ops"]


def test_wechat_clawbot_qr_status_persists_bot_token(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import check_wechat_clawbot_qrcode

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "pending_login.json").write_text(
        json.dumps({"qrcode": "qr_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            assert bot_token is None
            assert base_url == "https://example.invalid"

        def get_qrcode_status(self, *, qrcode: str):  # type: ignore[no-untyped-def]
            assert qrcode == "qr_1"
            return {"data": {"status": "confirmed", "bot_token": "bot_1", "get_updates_buf": "buf_1"}}

    out = check_wechat_clawbot_qrcode(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["bound"] is True
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["bot_token"] == "bot_1"
    assert state["get_updates_buf"] == "buf_1"


def test_wechat_clawbot_bind_persists_context_token(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import bind_wechat_clawbot_target

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"
            assert base_url == "https://example.invalid"

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "data": {
                    "get_updates_buf": "buf_2",
                    "message_list": [
                        {
                            "from_user_id": "user_1",
                            "group_id": "group_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "text_item": {"text": "bind ops"},
                        }
                    ],
                }
            }

    out = bind_wechat_clawbot_target(
        base=tmp_path,
        label="ops",
        name="prod",
        match_text="bind ops",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["target"] == "wechat:ops:prod"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["prod"]["to_user_id"] == "user_1"
    assert bindings["prod"]["context_token"] == "ctx_1"
    assert bindings["prod"]["group_id"] == "group_1"
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["get_updates_buf"] == "buf_2"


def test_wechat_clawbot_refreshes_matching_binding_from_inbound_message(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import refresh_wechat_clawbot_bindings_from_message

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "user_1",
                        "context_token": "ctx_old",
                        "group_id": None,
                        "chat_key": "user_1",
                        "last_message_id": "msg_old",
                        "last_text": "bind ops",
                        "updated_at_utc": "2026-06-10T00:00:00+00:00",
                    },
                    "other": {
                        "to_user_id": "user_2",
                        "context_token": "ctx_other",
                        "group_id": None,
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = refresh_wechat_clawbot_bindings_from_message(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        message={
            "from_user_id": "user_1",
            "context_token": "ctx_new",
            "message_id": "msg_new",
            "item_list": [{"type": 1, "text_item": {"text": "状态"}}],
        },
    )

    assert out["reason"] == "refreshed"
    assert out["updated_bindings"] == ["ops"]
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["context_token"] == "ctx_new"
    assert bindings["ops"]["last_message_id"] == "msg_new"
    assert bindings["ops"]["last_text"] == "状态"
    assert bindings["ops"]["refreshed_from_inbound_at_utc"]
    assert bindings["other"]["context_token"] == "ctx_other"


def test_wechat_clawbot_refreshes_notification_binding_from_successful_reply(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import refresh_wechat_clawbot_binding_from_reply

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "stale_user",
                        "context_token": "stale_ctx",
                        "group_id": "stale_group",
                        "chat_key": "stale_chat",
                        "last_message_id": "msg_old",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = refresh_wechat_clawbot_binding_from_reply(
        base=tmp_path,
        target="wechat:ops",
        notifications={"wechat_clawbot_state_dir": str(state_dir)},
        message={
            "from_user_id": "user_1",
            "group_id": "group_1",
            "context_token": "ctx_1",
            "message_id": "msg_1",
            "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
        },
        reply_status={"attempted": True, "ok": True, "outbound_message_id": "reply_1"},
    )

    assert out["reason"] == "refreshed_from_reply"
    assert out["updated_bindings"] == ["ops"]
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "user_1"
    assert bindings["ops"]["context_token"] == "ctx_1"
    assert bindings["ops"]["group_id"] == "group_1"
    assert bindings["ops"]["chat_key"] == "group_1"
    assert bindings["ops"]["last_message_id"] == "msg_1"
    assert bindings["ops"]["last_text"] == "/status"
    assert bindings["ops"]["refreshed_from_reply_at_utc"]
    assert bindings["ops"]["last_inbound_message_id"] == "msg_1"
    assert bindings["ops"]["reply_message_id"] == "reply_1"


def test_wechat_clawbot_message_adapter_builds_assistant_request(tmp_path: Path) -> None:
    from src.application.assistant.contracts import AssistantRequest
    from src.application.channels.wechat_clawbot.inbound import wechat_clawbot_message_to_assistant_request

    request = wechat_clawbot_message_to_assistant_request(
        {
            "from_user_id": "user_1",
            "group_id": "group_1",
            "context_token": "ctx_1",
            "message_id": "msg_1",
            "item_list": [{"type": 1, "text_item": {"text": "状态"}}],
        },
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
    )

    assert request == AssistantRequest(
        text="状态",
        sender_id="user_1",
        channel="wechat",
        message_id="msg_1",
        conversation_id="wechat:group_1",
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
        reply_context={
            "provider": "wechat_clawbot",
            "base": str(Path.cwd()),
            "label": "default",
            "state_dir": str((Path.cwd() / "output_shared" / "state" / "channels" / "wechat_clawbot" / "default").resolve()),
            "to_user_id": "user_1",
            "context_token": "ctx_1",
            "group_id": "group_1",
        },
    )


def test_reply_wechat_clawbot_text_reuses_idempotent_receipt(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import reply_wechat_clawbot_text

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    sends: list[dict[str, object]] = []
    client_inits: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            client_inits.append({"bot_token": bot_token, "base_url": base_url, "timeout": timeout})

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            sends.append(dict(kwargs))
            return {"ret": 0, "data": {"message_id": "reply_1"}}

    first = reply_wechat_clawbot_text(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        to_user_id="user_1",
        context_token="ctx_1",
        group_id="group_1",
        text="升级执行完成。",
        idempotency_key="in_123:upgrade-final",
        client_factory=FakeClient,
    )
    second = reply_wechat_clawbot_text(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        to_user_id="user_1",
        context_token="ctx_1",
        group_id="group_1",
        text="升级执行完成。",
        idempotency_key="in_123:upgrade-final",
        client_factory=FakeClient,
    )

    assert first["ok"] is True
    assert first["reason"] == "sent"
    assert first["client_id"] == sends[0]["client_id"]
    assert second["ok"] is True
    assert second["reason"] == "idempotent_replay"
    assert second["replayed"] is True
    assert second["message_id"] == "reply_1"
    assert len(sends) == 1
    assert len(client_inits) == 1
    receipts = json.loads((state_dir / "outbound_receipts.json").read_text(encoding="utf-8"))
    assert receipts["receipts"]["in_123:upgrade-final"]["message_id"] == "reply_1"


def test_wechat_clawbot_poll_once_routes_inbound_and_replies(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import build_response
    from src.application.assistant.audit import InboundAuditStore
    from src.application.channels.wechat_clawbot.inbound import poll_wechat_clawbot_once

    state_dir = tmp_path / "wechat-state"
    config_path = tmp_path / "config.us.json"
    config_path.write_text(
        json.dumps({"notifications": {"provider": "wechat_clawbot", "target": "wechat:ops"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "stale_user",
                        "context_token": "ctx_old",
                        "group_id": "stale_group",
                        "chat_key": "stale_chat",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, dict]] = []
    replies: list[dict[str, object]] = []
    typing_calls: list[dict[str, object]] = []
    events: list[str] = []

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"
            assert base_url == "https://example.invalid"
            assert timeout == 9

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "ret": 0,
                "get_updates_buf": "buf_2",
                "msgs": [
                        {
                            "from_user_id": "user_1",
                            "group_id": "group_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
                        }
                ],
            }

        def get_config(self, **kwargs):  # type: ignore[no-untyped-def]
            events.append("get_config")
            typing_calls.append({"method": "get_config", **dict(kwargs)})
            return {"ret": 0, "typing_ticket": "typing_ticket_1"}

        def send_typing(self, **kwargs):  # type: ignore[no-untyped-def]
            events.append(f"typing:{kwargs.get('status')}")
            typing_calls.append({"method": "send_typing", **dict(kwargs)})
            return {"ret": 0}

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            events.append("reply")
            replies.append(dict(kwargs))
            return {"ret": 0, "data": {"message_id": "reply_1"}}

    def _execute(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    out = poll_wechat_clawbot_once(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_path=str(config_path),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        timeout_sec=9,
        client_factory=FakeClient,
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert out["data"]["processed_count"] == 1
    assert out["data"]["reply_count"] == 1
    assert calls == [("runtime_status", {"config_path": str(config_path)})]
    assert replies[0]["to_user_id"] == "user_1"
    assert replies[0]["context_token"] == "ctx_1"
    assert replies[0]["group_id"] == "group_1"
    assert str(replies[0]["text"]).strip()
    assert typing_calls == [
        {"method": "get_config", "ilink_user_id": "user_1", "context_token": "ctx_1"},
        {"method": "send_typing", "ilink_user_id": "user_1", "typing_ticket": "typing_ticket_1", "status": 1},
        {"method": "send_typing", "ilink_user_id": "user_1", "typing_ticket": "typing_ticket_1", "status": 2},
    ]
    assert events == ["get_config", "typing:1", "reply", "typing:2"]
    assert out["data"]["results"][0]["typing"]["reason"] == "typing_started"
    assert out["data"]["results"][0]["typing"]["stop"]["reason"] == "typing_cancelled"
    assert "typing_ticket" not in out["data"]["results"][0]["typing"]
    assert out["data"]["results"][0]["binding_refresh"]["reason"] == "refreshed_from_reply"
    assert out["data"]["results"][0]["binding_refresh"]["updated_bindings"] == ["ops"]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["get_updates_buf"] == "buf_2"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "user_1"
    assert bindings["ops"]["context_token"] == "ctx_1"
    assert bindings["ops"]["last_message_id"] == "msg_1"
    assert bindings["ops"]["refreshed_from_reply_at_utc"]
    assert bindings["ops"]["last_inbound_message_id"] == "msg_1"
    assert bindings["ops"]["reply_message_id"] == "reply_1"
    audited = InboundAuditStore(str(tmp_path / "audit.sqlite3")).find_by_message(
        channel="wechat",
        message_id="msg_1",
    )
    assert audited is not None
    stored = json.loads(str(audited["response_json"] or "{}"))
    receipt = stored["data"]["reply"]
    assert receipt["schema_version"] == "wechat-clawbot-reply-receipt-v1"
    assert receipt["attempted"] is True
    assert receipt["ok"] is True
    assert receipt["reason"] == "sent"
    assert receipt["provider"] == "wechat_clawbot"
    assert receipt["sender_id"] == "user_1"
    assert receipt["inbound_message_id"] == "msg_1"
    assert receipt["message_id"] == "reply_1"
    assert receipt["outbound_message_id"] == "reply_1"
    assert receipt["delivery_confirmed"] is True
    assert receipt["api_response"] == {"ret": 0, "data": {"message_id": "reply_1"}}


def test_wechat_clawbot_poll_once_persists_failed_reply_receipt(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import build_response
    from src.application.assistant.audit import InboundAuditStore
    from src.application.channels.wechat_clawbot.inbound import poll_wechat_clawbot_once

    state_dir = tmp_path / "wechat-state"
    config_path = tmp_path / "config.us.json"
    config_path.write_text(
        json.dumps({"notifications": {"provider": "wechat_clawbot", "target": "wechat:ops"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "stale_user",
                        "context_token": "ctx_old",
                        "group_id": "stale_group",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"
            assert base_url == "https://example.invalid"
            del timeout

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "ret": 0,
                "get_updates_buf": "buf_2",
                "msgs": [
                        {
                            "from_user_id": "user_1",
                            "group_id": "group_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
                        }
                ],
            }

        def get_config(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 0, "typing_ticket": "typing_ticket_1"}

        def send_typing(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 0}

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 91, "errmsg": "context expired"}

    def _execute(tool_name: str, payload: dict) -> dict:
        del payload
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    out = poll_wechat_clawbot_once(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_path=str(config_path),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        client_factory=FakeClient,
        execute_tool_fn=_execute,
    )

    assert out["ok"] is False
    assert out["data"]["processed_count"] == 1
    assert out["data"]["reply_count"] == 0
    assert out["data"]["results"][0]["reply"]["reason"] == "reply_failed"
    assert out["data"]["results"][0]["binding_refresh"]["reason"] == "reply_not_successful"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "stale_user"
    assert bindings["ops"]["context_token"] == "ctx_old"
    assert "refreshed_from_reply_at_utc" not in bindings["ops"]
    audited = InboundAuditStore(str(tmp_path / "audit.sqlite3")).find_by_message(
        channel="wechat",
        message_id="msg_1",
    )
    assert audited is not None
    stored = json.loads(str(audited["response_json"] or "{}"))
    receipt = stored["data"]["reply"]
    assert receipt["attempted"] is True
    assert receipt["ok"] is False
    assert receipt["reason"] == "reply_failed"
    assert receipt["provider"] == "wechat_clawbot"
    assert receipt["inbound_message_id"] == "msg_1"
    assert "delivery_confirmed" not in receipt
    assert receipt["api_response"] == {"ret": 91, "errmsg": "context expired"}


def test_wechat_clawbot_poll_once_accepts_empty_sendmessage_response(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import build_response
    from src.application.assistant.audit import InboundAuditStore
    from src.application.channels.wechat_clawbot.inbound import poll_wechat_clawbot_once

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            del timeout
            assert bot_token == "bot_1"
            assert base_url == "https://example.invalid"

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "ret": 0,
                "get_updates_buf": "buf_2",
                "msgs": [
                    {
                        "from_user_id": "user_1",
                        "group_id": "group_1",
                        "context_token": "ctx_1",
                        "message_id": "msg_1",
                        "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
                    }
                ],
            }

        def get_config(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 0, "typing_ticket": "typing_ticket_1"}

        def send_typing(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {}

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {}

    def _execute(tool_name: str, payload: dict) -> dict:
        del payload
        return build_response(tool_name=tool_name, ok=True, data={"status": "ok"})

    out = poll_wechat_clawbot_once(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        client_factory=FakeClient,
        execute_tool_fn=_execute,
    )

    assert out["ok"] is True
    assert out["data"]["reply_count"] == 1
    assert out["data"]["results"][0]["reply"]["reason"] == "sent"
    audited = InboundAuditStore(str(tmp_path / "audit.sqlite3")).find_by_message(
        channel="wechat",
        message_id="msg_1",
    )
    assert audited is not None
    stored = json.loads(str(audited["response_json"] or "{}"))
    receipt = stored["data"]["reply"]
    assert receipt["attempted"] is True
    assert receipt["ok"] is True
    assert receipt["api_response"] == {}
    assert "delivery_confirmed" not in receipt


def test_wechat_clawbot_poll_once_stays_silent_for_unauthorized_sender(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import poll_wechat_clawbot_once

    state_dir = tmp_path / "wechat-state"
    config_path = tmp_path / "config.us.json"
    config_path.write_text(
        json.dumps({"notifications": {"provider": "wechat_clawbot", "target": "wechat:ops"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps({"bindings": {"ops": {"to_user_id": "stale_user", "context_token": "ctx_old"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    replies: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            del bot_token, base_url, timeout

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            del get_updates_buf
            return {
                "ret": 0,
                "get_updates_buf": "buf_2",
                "msgs": [
                    {
                        "from_user_id": "user_1",
                        "context_token": "ctx_1",
                        "message_id": "msg_1",
                        "text_item": {"text": "状态"},
                    }
                ],
            }

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            replies.append(dict(kwargs))
            return {"ret": 0}

    out = poll_wechat_clawbot_once(
        base=tmp_path,
        state_dir=str(state_dir),
        config_path=str(config_path),
        allowed_senders="wechat:user_2",
        client_factory=FakeClient,
    )

    assert out["ok"] is False
    assert out["data"]["processed_count"] == 1
    assert out["data"]["reply_count"] == 0
    assert out["data"]["results"][0]["reply"]["reason"] == "permission_denied"
    assert out["data"]["results"][0]["binding_refresh"]["reason"] == "reply_not_successful"
    assert replies == []
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "stale_user"
    assert bindings["ops"]["context_token"] == "ctx_old"
    assert "refreshed_from_reply_at_utc" not in bindings["ops"]


def test_wechat_clawbot_poll_once_replies_to_non_silent_permission_denied(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import poll_wechat_clawbot_once

    state_dir = tmp_path / "wechat-state"
    config_path = tmp_path / "config.us.json"
    config_path.write_text(
        json.dumps({"notifications": {"provider": "wechat_clawbot", "target": "wechat:ops"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps({"bindings": {"ops": {"to_user_id": "stale_user", "context_token": "ctx_old"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    replies: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            del bot_token, base_url, timeout

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            del get_updates_buf
            return {
                "ret": 0,
                "get_updates_buf": "buf_2",
                "msgs": [
                    {
                        "from_user_id": "user_1",
                        "context_token": "ctx_1",
                        "message_id": "msg_1",
                        "text_item": {"text": "买入 NVDA"},
                    }
                ],
            }

        def get_config(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 0, "typing_ticket": "typing_ticket_1"}

        def send_typing(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {"ret": 0}

        def send_text_message(self, **kwargs):  # type: ignore[no-untyped-def]
            replies.append(dict(kwargs))
            return {"ret": 0, "data": {"message_id": "reply_1"}}

    channel_service = SimpleNamespace(
        handle_inbound=lambda *_args, **_kwargs: {
            "ok": False,
            "data": {
                "kind": "message",
                "response_text": "",
                "inbound_result": {
                    "ok": False,
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "message": "写入权限未开启",
                        "hint": "请先确认。",
                    },
                },
            },
        }
    )

    out = poll_wechat_clawbot_once(
        base=tmp_path,
        state_dir=str(state_dir),
        config_path=str(config_path),
        allowed_senders="wechat:user_1",
        client_factory=FakeClient,
        channel_service=channel_service,
    )

    assert out["ok"] is False
    assert out["data"]["reply_count"] == 1
    assert out["data"]["results"][0]["reply"]["reason"] == "permission_denied_sent"
    assert out["data"]["results"][0]["binding_refresh"]["reason"] == "inbound_not_successful"
    assert replies[0]["text"] == "写入权限未开启 请先确认。"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "stale_user"
    assert bindings["ops"]["context_token"] == "ctx_old"
    assert "refreshed_from_reply_at_utc" not in bindings["ops"]


def test_wechat_clawbot_bind_failure_does_not_advance_cursor(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import bind_wechat_clawbot_target

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "data": {
                    "get_updates_buf": "buf_2",
                    "message_list": [
                        {
                            "from_user_id": "user_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "text_item": {"text": "bind ops"},
                        }
                    ],
                }
            }

    out = bind_wechat_clawbot_target(
        base=tmp_path,
        label="ops",
        name="prod",
        match_text="missing text",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "BINDING_MESSAGE_NOT_FOUND"
    assert out["data"]["candidate_count"] == 1
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["get_updates_buf"] == "buf_1"
    assert not (state_dir / "bindings.json").exists()


def test_wechat_clawbot_connect_logs_in_and_binds_target(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import connect_wechat_clawbot_target

    state_dir = tmp_path / "wechat-state"
    progress_events: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            self.bot_token = bot_token
            self.base_url = base_url
            self.timeout = timeout

        def get_bot_qrcode(self, *, bot_type: int):  # type: ignore[no-untyped-def]
            assert bot_type == 3
            return {"data": {"qrcode": "qr_1"}}

        def get_qrcode_status(self, *, qrcode: str):  # type: ignore[no-untyped-def]
            assert qrcode == "qr_1"
            return {"data": {"status": "confirmed", "bot_token": "bot_1", "get_updates_buf": "buf_1"}}

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert self.bot_token == "bot_1"
            assert get_updates_buf == "buf_1"
            return {
                "data": {
                    "get_updates_buf": "buf_2",
                    "message_list": [
                        {
                            "from_user_id": "user_1",
                            "group_id": "group_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "text_item": {"text": "bind ops"},
                        }
                    ],
                }
            }

    out = connect_wechat_clawbot_target(
        base=tmp_path,
        label="default",
        name="ops",
        state_dir=str(state_dir),
        base_url="https://example.invalid",
        client_factory=FakeClient,
        progress_fn=progress_events.append,
        sleep_fn=lambda _seconds: None,
    )

    assert out["ok"] is True
    assert out["data"]["target"] == "wechat:default:ops"
    assert [event["event"] for event in progress_events] == [
        "qrcode",
        "login_confirmed",
        "bind_instruction",
        "bound",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["bot_token"] == "bot_1"
    assert state["get_updates_buf"] == "buf_2"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["ops"]["to_user_id"] == "user_1"


def test_wechat_clawbot_connect_times_out_when_qr_not_confirmed(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import connect_wechat_clawbot_target

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            del bot_token, base_url, timeout

        def get_bot_qrcode(self, *, bot_type: int):  # type: ignore[no-untyped-def]
            del bot_type
            return {"data": {"qrcode": "qr_1"}}

        def get_qrcode_status(self, *, qrcode: str):  # type: ignore[no-untyped-def]
            del qrcode
            return {"data": {"status": "pending"}}

    out = connect_wechat_clawbot_target(
        base=tmp_path,
        name="ops",
        state_dir=str(tmp_path / "wechat-state"),
        login_timeout_sec=0,
        poll_interval_sec=0,
        client_factory=FakeClient,
        sleep_fn=lambda _seconds: None,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "QRCODE_LOGIN_TIMEOUT"
    assert not (tmp_path / "wechat-state" / "state.json").exists()
    assert not (tmp_path / "wechat-state" / "bindings.json").exists()


def test_cli_channel_wechat_clawbot_connect_routes_to_connect_handler(tmp_path: Path) -> None:
    from src.interfaces.cli.channel_ops import handle_channel_command

    captured: dict[str, object] = {}

    def fake_connect(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"ok": True, "data": {"target": "wechat:default:ops"}}

    args = SimpleNamespace(
        channel_command="wechat-clawbot",
        wechat_clawbot_command="connect",
        label="default",
        state_dir=str(tmp_path / "wechat-state"),
        name="ops",
        match_text=None,
        from_user_id=None,
        base_url="https://example.invalid",
        timeout_sec=7,
        login_timeout_sec=11,
        bind_timeout_sec=13,
        poll_interval_sec=0.5,
    )

    out = handle_channel_command(
        args,
        repo_base_fn=lambda: tmp_path,
        connect_target_fn=fake_connect,
    )

    assert out["ok"] is True
    assert captured["base"] == tmp_path
    assert captured["name"] == "ops"
    assert captured["state_dir"] == str(tmp_path / "wechat-state")
    assert captured["login_timeout_sec"] == 11
    assert captured["bind_timeout_sec"] == 13


def test_cli_channel_wechat_clawbot_poll_once_routes_to_handler(tmp_path: Path) -> None:
    from src.interfaces.cli.channel_ops import handle_channel_command

    captured: dict[str, object] = {}

    def fake_poll_once(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"ok": True, "data": {"processed_count": 0}}

    args = SimpleNamespace(
        channel_command="wechat-clawbot",
        wechat_clawbot_command="poll-once",
        label="default",
        state_dir=str(tmp_path / "wechat-state"),
        config_key="us",
        config_path=None,
        assistant_config=str(tmp_path / "config.assistant.json"),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        no_reply=True,
        max_reply_chars=88,
        timeout_sec=7,
    )

    out = handle_channel_command(
        args,
        repo_base_fn=lambda: tmp_path,
        poll_once_fn=fake_poll_once,
    )

    assert out["ok"] is True
    assert captured["base"] == tmp_path
    assert captured["state_dir"] == str(tmp_path / "wechat-state")
    assert captured["config_key"] == "us"
    assert captured["assistant_config_path"] == str(tmp_path / "config.assistant.json")
    assert captured["audit_db"] == str(tmp_path / "audit.sqlite3")
    assert captured["allowed_senders"] == "wechat:user_1"
    assert captured["reply_enabled"] is False
    assert captured["max_reply_chars"] == 88
    assert captured["timeout_sec"] == 7


def test_wechat_clawbot_serve_check_reports_redacted_settings(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import (
        build_wechat_clawbot_serve_settings,
        check_wechat_clawbot_serve_settings,
    )

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )

    settings = build_wechat_clawbot_serve_settings(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_key="us",
        allowed_senders="wechat:user_1",
        poll_interval_sec=0.25,
    )

    out = check_wechat_clawbot_serve_settings(settings)

    assert out["ok"] is True
    assert out["data"]["settings"]["label"] == "ops"
    assert out["data"]["settings"]["allowed_senders_configured"] is True
    assert out["data"]["settings"]["bot_token_configured"] is True
    assert "bot_1" not in json.dumps(out, ensure_ascii=False)


def test_wechat_clawbot_serve_check_requires_allowed_senders(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import (
        build_wechat_clawbot_serve_settings,
        check_wechat_clawbot_serve_settings,
    )

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({"bot_token": "bot_1"}, ensure_ascii=False), encoding="utf-8")

    settings = build_wechat_clawbot_serve_settings(
        base=tmp_path,
        state_dir=str(state_dir),
        config_key="us",
    )

    out = check_wechat_clawbot_serve_settings(settings)

    assert out["ok"] is False
    assert out["error"]["code"] == "CONFIG_ERROR"
    assert "sender allowlist" in out["error"]["message"]


def test_wechat_clawbot_serve_check_suggests_connect_when_bot_token_missing(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import (
        build_wechat_clawbot_serve_settings,
        check_wechat_clawbot_serve_settings,
    )

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()

    settings = build_wechat_clawbot_serve_settings(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_key="us",
        allowed_senders="wechat:user_1",
    )

    out = check_wechat_clawbot_serve_settings(settings)

    assert out["ok"] is False
    assert out["error"]["code"] == "STATE_ERROR"
    assert "./om channel wechat-clawbot connect --label ops --name ops --state-dir" in out["error"]["hint"]
    assert out["data"]["settings"]["connect_command_template"].startswith("./om channel wechat-clawbot connect")


def test_wechat_clawbot_serve_settings_reads_behavior_from_assistant_config(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import build_wechat_clawbot_serve_settings

    assistant_config = tmp_path / "config.assistant.json"
    assistant_config.write_text(
        json.dumps(
            {
                "assistant": {"default_market_scope": "us"},
                "inbound": {
                    "wechat_clawbot": {
                        "label": "ops",
                        "state_dir": str(tmp_path / "wechat-state"),
                        "allowed_senders": "wechat:user_1",
                        "reply_enabled": False,
                        "max_reply_chars": 1200,
                        "poll_interval_sec": 0.75,
                        "timeout_sec": 9,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    settings = build_wechat_clawbot_serve_settings(base=tmp_path, assistant_config_path=str(assistant_config))

    assert settings.label == "ops"
    assert settings.state_dir == str(tmp_path / "wechat-state")
    assert settings.config_key == "us"
    assert settings.allowed_senders == "wechat:user_1"
    assert settings.reply_enabled is False
    assert settings.max_reply_chars == 1200
    assert settings.poll_interval_sec == 0.75
    assert settings.timeout_sec == 9

    overridden = build_wechat_clawbot_serve_settings(
        base=tmp_path,
        label="cli",
        assistant_config_path=str(assistant_config),
        allowed_senders="wechat:user_2",
        reply_enabled=True,
        max_reply_chars=88,
        poll_interval_sec=0.25,
        timeout_sec=7,
    )

    assert overridden.label == "cli"
    assert overridden.allowed_senders == "wechat:user_2"
    assert overridden.reply_enabled is True
    assert overridden.max_reply_chars == 88
    assert overridden.poll_interval_sec == 0.25
    assert overridden.timeout_sec == 7


def test_wechat_clawbot_serve_polls_until_stop_condition(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.inbound import build_wechat_clawbot_serve_settings, serve_wechat_clawbot

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({"bot_token": "bot_1"}, ensure_ascii=False), encoding="utf-8")
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []

    settings = build_wechat_clawbot_serve_settings(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        reply_enabled=False,
        max_reply_chars=88,
        poll_interval_sec=0.25,
        timeout_sec=7,
    )

    def _poll_once(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {"ok": True, "data": {"processed_count": 0}}

    serve_wechat_clawbot(
        settings,
        poll_once_fn=_poll_once,
        sleep_fn=sleeps.append,
        lock_path=tmp_path / "wechat-clawbot.lock",
        stop_after_batches=2,
    )

    assert len(calls) == 2
    assert sleeps == [0.25]
    assert calls[0]["base"] == tmp_path
    assert calls[0]["label"] == "ops"
    assert calls[0]["state_dir"] == str(state_dir)
    assert calls[0]["config_key"] == "us"
    assert calls[0]["audit_db"] == str(tmp_path / "audit.sqlite3")
    assert calls[0]["allowed_senders"] == "wechat:user_1"
    assert calls[0]["reply_enabled"] is False
    assert calls[0]["max_reply_chars"] == 88
    assert calls[0]["timeout_sec"] == 7


def test_cli_channel_wechat_clawbot_serve_routes_to_handler(tmp_path: Path) -> None:
    from src.interfaces.cli.channel_ops import handle_channel_command

    captured: dict[str, object] = {}

    def fake_build_settings(**kwargs: Any) -> str:
        captured["build"] = kwargs
        return "settings"

    def fake_serve(settings: Any, *, lock_path: str | None = None) -> dict[str, Any]:
        captured["settings"] = settings
        captured["lock_path"] = lock_path
        return {"ok": True, "data": {"served": True}}

    args = SimpleNamespace(
        channel_command="wechat-clawbot",
        wechat_clawbot_command="serve",
        label="default",
        state_dir=str(tmp_path / "wechat-state"),
        config_key="us",
        config_path=None,
        assistant_config=str(tmp_path / "config.assistant.json"),
        audit_db=str(tmp_path / "audit.sqlite3"),
        allowed_senders="wechat:user_1",
        no_reply=True,
        max_reply_chars=88,
        timeout_sec=7,
        poll_interval_sec=0.25,
        lock_path=str(tmp_path / "wechat-clawbot.lock"),
        check=False,
    )

    out = handle_channel_command(
        args,
        repo_base_fn=lambda: tmp_path,
        build_serve_settings_fn=fake_build_settings,
        serve_fn=fake_serve,
    )

    build_kwargs = captured["build"]
    assert out["ok"] is True
    assert isinstance(build_kwargs, dict)
    assert build_kwargs["base"] == tmp_path
    assert build_kwargs["state_dir"] == str(tmp_path / "wechat-state")
    assert build_kwargs["config_key"] == "us"
    assert build_kwargs["assistant_config_path"] == str(tmp_path / "config.assistant.json")
    assert build_kwargs["audit_db"] == str(tmp_path / "audit.sqlite3")
    assert build_kwargs["allowed_senders"] == "wechat:user_1"
    assert build_kwargs["reply_enabled"] is False
    assert build_kwargs["max_reply_chars"] == 88
    assert build_kwargs["poll_interval_sec"] == 0.25
    assert build_kwargs["timeout_sec"] == 7
    assert captured["settings"] == "settings"
    assert captured["lock_path"] == str(tmp_path / "wechat-clawbot.lock")


def test_cli_channel_wechat_clawbot_list_reads_local_state(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "bindings.json").write_text(
        json.dumps({"bindings": {"ops": {"to_user_id": "user_1", "context_token": "ctx_1"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    rc = cli.main(["channel", "wechat-clawbot", "list", "--state-dir", str(state_dir)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["binding_count"] == 1
    assert payload["data"]["allowed_sender_hints"] == ["wechat:user_1"]
    assert payload["data"]["bindings"]["ops"]["inbound_sender_id_hint"] == "wechat:user_1"
    assert "context_token" not in payload["data"]["bindings"]["ops"]


def test_cli_channel_status_reports_feishu_and_wechat_without_secrets(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    runtime = tmp_path / "runtime"
    assistant_config = runtime / "resolved" / "config.assistant.json"
    state_dir = runtime / "output_shared" / "state" / "channels" / "wechat_clawbot" / "ops"
    assistant_config.parent.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    assistant_config.write_text(
        json.dumps(
            {
                "inbound": {
                    "wechat_clawbot": {
                        "label": "ops",
                        "allowed_senders": "wechat:user_1",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_secret_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "wx_user_1",
                        "context_token": "ctx_secret_1",
                        "group_id": "group_1",
                        "chat_key": "chat_secret_1",
                        "last_message_id": "msg_1",
                        "last_text": "private bind text",
                        "updated_at_utc": "2026-06-18T01:00:00+00:00",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(runtime),
                "assistant_config_path": str(assistant_config),
                "feishu_ws": {
                    "enabled": True,
                    "assistant_config_path": str(assistant_config),
                },
                "wechat_clawbot": {
                    "enabled": True,
                    "label": "ops",
                    "state_dir": str(state_dir),
                    "assistant_config_path": str(assistant_config),
                    "allowed_senders_configured": True,
                    "allowed_senders_source": "config_yaml",
                },
                "services": [
                    {"name": "options-monitor-feishu-ws.service"},
                    {"name": "options-monitor-wechat-clawbot.service"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env_file = runtime / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_1",
                "OM_FEISHU_BOT_APP_SECRET=secret_1",
                "OM_FEISHU_BOT_USER_OPEN_ID=ou_1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        [
            "channel",
            "status",
            "--runtime-root",
            str(runtime),
            "--profile-path",
            str(profile_path),
            "--env-file",
            str(env_file),
        ]
    )
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["channels"]["feishu"]["available"] is True
    assert payload["data"]["channels"]["wechat_clawbot"]["available"] is True
    assert payload["data"]["channels"]["wechat_clawbot"]["allowed_senders_configured"] is True
    assert payload["data"]["channels"]["wechat_clawbot"]["binding_count"] == 1
    assert payload["data"]["channels"]["wechat_clawbot"]["bindings"]["ops"]["has_context_token"] is True
    assert payload["data"]["channels"]["wechat_clawbot"]["bindings"]["ops"]["last_text_present"] is True
    assert payload["data"]["summary"]["available_channels"] == ["feishu", "wechat_clawbot"]
    assert "bot_secret_1" not in rendered
    assert "ctx_secret_1" not in rendered
    assert "private bind text" not in rendered
    assert "chat_secret_1" not in rendered
    assert "wechat:user_1" not in rendered


def test_channel_status_reports_wechat_cursor_and_service_state(tmp_path: Path) -> None:
    from src.application.channels.status import build_channel_status

    runtime = tmp_path / "runtime"
    state_dir = runtime / "output_shared" / "state" / "channels" / "wechat_clawbot" / "ops"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "bot_token": "bot_secret_1",
                "base_url": "https://example.invalid",
                "get_updates_buf": "cursor_1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "wx_user_1",
                        "context_token": "ctx_secret_1",
                        "group_id": "group_1",
                        "last_message_id": "msg_1",
                        "last_text": "private bind text",
                        "updated_at_utc": "2026-06-18T01:00:00+00:00",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        stdout = "enabled\n" if list(command)[1] == "is-enabled" else "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    out = build_channel_status(
        base=tmp_path,
        runtime_root=runtime,
        payload={
            "service_provider": "systemd",
            "wechat_clawbot": {
                "enabled": True,
                "label": "ops",
                "state_dir": str(state_dir),
                "allowed_senders_configured": True,
                "allowed_senders_source": "config_yaml",
            },
            "services": [{"name": "options-monitor-wechat-clawbot.service"}],
        },
        include_service_status=True,
        run_cmd=_run_cmd,
    )

    health = out["channels"]["wechat_clawbot"]
    assert health["available"] is True
    assert health["cursor_configured"] is True
    assert health["cursor_length"] == len("cursor_1")
    assert health["binding_count"] == 1
    assert health["binding_names"] == ["ops"]
    assert health["bindings"]["ops"]["target"] == "wechat:ops:ops"
    assert health["bindings"]["ops"]["has_to_user_id"] is True
    assert health["bindings"]["ops"]["has_context_token"] is True
    assert health["bindings"]["ops"]["has_group_id"] is True
    assert health["bindings"]["ops"]["last_message_id"] == "msg_1"
    assert health["bindings"]["ops"]["last_text_present"] is True
    assert isinstance(health["bindings"]["ops"]["age_seconds"], int)
    assert "ctx_secret_1" not in json.dumps(health, ensure_ascii=False)
    assert "private bind text" not in json.dumps(health, ensure_ascii=False)
    assert health["service_present"] is True
    assert health["service_status_checked"] is True
    assert health["service_active"] is True
    assert health["service_enabled"] is True
    assert calls == [
        ["systemctl", "is-active", "options-monitor-wechat-clawbot.service"],
        ["systemctl", "is-enabled", "options-monitor-wechat-clawbot.service"],
    ]
