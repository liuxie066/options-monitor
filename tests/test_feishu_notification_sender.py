from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


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
        captured.update({key: str(value) for key, value in kwargs.items() if key in {"app_id", "app_secret", "open_id", "text"}})
        return {"code": 0, "msg": "success", "data": {"message_id": "om_1"}}

    monkeypatch.setattr(service, "send_text_message", _send_text_message)

    out = service.send_feishu_app_message(
        base=tmp_path,
        channel="feishu_app",
        target="",
        message="hello",
        notifications={},
    )

    assert out["ok"] is True
    assert captured["app_id"] == "cli_1"
    assert captured["app_secret"] == "sec_1"
    assert captured["open_id"] == "ou_1"
    assert captured["text"] == "hello"


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


def test_select_notification_delivery_adapter_routes_wechat_clawbot_to_openclaw() -> None:
    from domain.domain import normalize_notify_subprocess_output
    from src.application.notification_delivery_adapter import (
        select_notification_delivery_adapter,
    )
    from src.infrastructure.external_services import (
        send_openclaw_message_process,
    )

    adapter = select_notification_delivery_adapter("wechat_clawbot")

    assert adapter.send_fn is send_openclaw_message_process
    assert adapter.normalize_fn is normalize_notify_subprocess_output
    assert adapter.failure_stage == "send_openclaw_message"


def test_send_openclaw_message_translates_wechat_clawbot_channel(monkeypatch, tmp_path: Path) -> None:
    from src.infrastructure import external_services as service

    captured: dict[str, object] = {}

    def fake_run_command(cmd, *, cwd, capture_output=False, text=False, timeout_sec=None, env=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout_sec"] = timeout_sec
        return SimpleNamespace(returncode=0, stdout='{"message_id":"msg_1"}', stderr="")

    monkeypatch.setattr(service, "run_command", fake_run_command)

    out = service.send_openclaw_message(
        base=tmp_path,
        channel="wechat_clawbot",
        target="clawbot:test",
        message="hello",
    )

    assert out.returncode == 0
    assert captured["cwd"] == tmp_path
    assert captured["timeout_sec"] is None
    cmd = captured["cmd"]
    assert cmd[cmd.index("--channel") + 1] == "openclaw-weixin"
    assert cmd[cmd.index("--target") + 1] == "clawbot:test"


def test_send_openclaw_message_process_uses_configured_timeout(monkeypatch, tmp_path: Path) -> None:
    from src.infrastructure import external_services as service

    captured: dict[str, object] = {}

    def fake_run_command(cmd, *, cwd, capture_output=False, text=False, timeout_sec=None, env=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout_sec"] = timeout_sec
        return SimpleNamespace(returncode=0, stdout='{"message_id":"msg_1"}', stderr="")

    monkeypatch.setattr(service, "run_command", fake_run_command)

    out = service.send_openclaw_message_process(
        base=tmp_path,
        channel="wechat_clawbot",
        target="clawbot:test",
        message="hello",
        notifications={"send_timeout_sec": 12},
    )

    assert out.returncode == 0
    assert captured["timeout_sec"] == 12


def test_select_notification_delivery_adapter_rejects_unknown_provider() -> None:
    from src.application.notification_delivery_adapter import select_notification_delivery_adapter

    try:
        select_notification_delivery_adapter("sms")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unsupported notification provider" in str(exc)
