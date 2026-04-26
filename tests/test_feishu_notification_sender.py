from __future__ import annotations

import json
from pathlib import Path


def test_load_feishu_notification_app_config_uses_default_path(tmp_path: Path) -> None:
    from scripts.infra.service import load_feishu_notification_app_config

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "notifications.feishu.app.json"
    secrets_path.write_text(
        json.dumps({"feishu": {"app_id": "cli_1", "app_secret": "sec_1"}}),
        encoding="utf-8",
    )

    out = load_feishu_notification_app_config(base=tmp_path)

    assert out["app_id"] == "cli_1"
    assert out["app_secret"] == "sec_1"
    assert out["secrets_file"] == str(secrets_path.resolve())


def test_load_feishu_notification_app_config_supports_explicit_path(tmp_path: Path) -> None:
    from scripts.infra.service import load_feishu_notification_app_config

    secrets_path = tmp_path / "custom.json"
    secrets_path.write_text(
        json.dumps({"feishu": {"app_id": "cli_2", "app_secret": "sec_2"}}),
        encoding="utf-8",
    )

    out = load_feishu_notification_app_config(
        base=tmp_path,
        notifications={"secrets_file": "custom.json"},
    )

    assert out["app_id"] == "cli_2"
    assert out["app_secret"] == "sec_2"


def test_load_feishu_notification_app_config_fails_when_file_missing(tmp_path: Path) -> None:
    from scripts.infra.service import load_feishu_notification_app_config

    try:
        load_feishu_notification_app_config(base=tmp_path)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "notification secrets file not found" in str(exc)


def test_load_feishu_notification_app_config_fails_when_credentials_missing(tmp_path: Path) -> None:
    from scripts.infra.service import load_feishu_notification_app_config

    secrets_path = tmp_path / "custom.json"
    secrets_path.write_text(json.dumps({"feishu": {"app_id": "cli_only"}}), encoding="utf-8")

    try:
        load_feishu_notification_app_config(base=tmp_path, secrets_file=secrets_path)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "notification secrets missing feishu.app_id/app_secret" in str(exc)


def test_normalize_feishu_app_send_output_marks_success_with_message_id() -> None:
    from scripts.infra.service import normalize_feishu_app_send_output

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
    from scripts.infra.service import normalize_feishu_app_send_output

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
    from scripts.infra.service import normalize_feishu_app_send_output

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
    from scripts.infra.service import normalize_feishu_app_send_output

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
