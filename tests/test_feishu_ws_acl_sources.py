from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from src.application.bot.control.policy import ReplyAuthorizationError
from src.application.inbound.feishu import prepare_feishu_ack_target
from src.application.inbound.feishu_ws import build_feishu_ws_settings


ACL_KEYS = ("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "OM_FEISHU_BOT_USER_OPEN_ID")


def _settings(tmp_path, *, environ, env_file=None, credential_env_file=None):
    bot_config = tmp_path / "bot.json"
    bot_config.write_text('{"bot":{"enabled":true}}', encoding="utf-8")
    return build_feishu_ws_settings(
        environ=environ,
        env_file=env_file,
        credential_env_file=credential_env_file,
        bot_config_path=str(bot_config),
        metadata_only=True,
    )


def _deny_reads(monkeypatch, *paths):
    # Simulate EACCES from a root-owned 0600 file even when CI runs as root.
    original = Path.read_text
    denied = set(paths)
    attempts = []

    def read_text(path, *args, **kwargs):
        if path in denied:
            attempts.append(path)
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return attempts


@pytest.mark.parametrize("acl_key", ACL_KEYS)
@pytest.mark.parametrize("process_environment", [False, True])
def test_process_acl_ignores_unreadable_ordinary_env_file(
    tmp_path, monkeypatch, acl_key, process_environment,
):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text("OM_INBOUND_REQUIRE_ALLOWLIST=1\n", encoding="utf-8")
    env_file.chmod(0o600)
    attempts = _deny_reads(monkeypatch, env_file)
    environment = {"OM_ENV_FILE": str(env_file), acl_key: "ou_1"}
    if process_environment:
        for key in ACL_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
    settings = _settings(
        tmp_path, environ=None if process_environment else environment,
    )
    assert settings.allowed_senders == "feishu:ou_1"
    assert set(attempts) == {env_file}
    attempts.clear()

    assert settings.current_allowed_senders() == "feishu:ou_1"
    assert attempts == []
    if process_environment:
        monkeypatch.delenv(acl_key)
        assert settings.current_allowed_senders() == ""
        assert attempts == []
        monkeypatch.setenv(acl_key, "ou_1")
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "msg_1", "message_type": "text",
                "content": json.dumps({"text": "hello"}),
            },
        },
    }
    assert prepare_feishu_ack_target(
        payload, allowed_senders=settings.current_allowed_senders(),
    )["ready"] is True
    payload["event"]["sender"]["sender_id"]["open_id"] = "ou_denied"
    assert prepare_feishu_ack_target(
        payload, allowed_senders=settings.current_allowed_senders(),
    )["ready"] is False


def test_ordinary_file_without_acl_does_not_become_refresh_dependency(tmp_path, monkeypatch):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text("OM_INBOUND_REQUIRE_ALLOWLIST=1\n", encoding="utf-8")
    settings = _settings(
        tmp_path, environ={ACL_KEYS[0]: "ou_1"}, env_file=env_file,
    )
    attempts = _deny_reads(monkeypatch, env_file)
    assert settings.current_allowed_senders() == "feishu:ou_1"
    assert attempts == []


@pytest.mark.parametrize("acl_key", ACL_KEYS)
def test_file_acl_refreshes_readable_values_and_removal(tmp_path, acl_key):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text(f"{acl_key}=ou_file\n", encoding="utf-8")
    settings = _settings(
        tmp_path, environ={acl_key: "ou_old_process"}, env_file=env_file,
    )
    assert settings.current_allowed_senders() == "feishu:ou_file"
    env_file.write_text(f"{acl_key}=ou_updated\n", encoding="utf-8")
    assert settings.current_allowed_senders() == "feishu:ou_updated"
    env_file.write_text("OM_INBOUND_REQUIRE_ALLOWLIST=1\n", encoding="utf-8")
    assert settings.current_allowed_senders() == ""


@pytest.mark.parametrize("acl_key", ACL_KEYS)
@pytest.mark.parametrize("with_credentials", [False, True])
def test_file_acl_read_failure_does_not_fall_back_to_process(
    tmp_path, monkeypatch, acl_key, with_credentials,
):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text(f"{acl_key}=ou_file\n", encoding="utf-8")
    credential_file = tmp_path / "credential.env"
    credential_file.write_text("OM_FEISHU_BOT_APP_ID=synthetic_app\n", encoding="utf-8")
    settings = _settings(
        tmp_path, environ={acl_key: "ou_old_process"}, env_file=env_file,
        credential_env_file=credential_file if with_credentials else None,
    )
    assert settings.current_allowed_senders() == "feishu:ou_file"
    attempts = _deny_reads(monkeypatch, env_file)
    with pytest.raises(ReplyAuthorizationError, match="sender authorization source unavailable"):
        settings.current_allowed_senders()
    assert attempts == [env_file]


@pytest.mark.parametrize("credential_has_acl", [False, True])
def test_credential_overlay_does_not_bind_unreadable_ordinary_file(
    tmp_path, monkeypatch, credential_has_acl,
):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text("OM_INBOUND_REQUIRE_ALLOWLIST=1\n", encoding="utf-8")
    credential_file = tmp_path / "credential.env"
    credential_file.write_text(
        f"{ACL_KEYS[0]}=ou_credential\n" if credential_has_acl
        else "OM_FEISHU_BOT_APP_ID=synthetic_app\n", encoding="utf-8",
    )
    attempts = _deny_reads(monkeypatch, env_file)
    settings = _settings(
        tmp_path, environ={ACL_KEYS[0]: "ou_1"}, env_file=env_file,
        credential_env_file=credential_file,
    )
    attempts.clear()
    expected = "feishu:ou_credential" if credential_has_acl else "feishu:ou_1"
    assert settings.current_allowed_senders() == expected
    assert attempts == []
    credential_file.write_text(f"{ACL_KEYS[0]}=ou_rotated\n", encoding="utf-8")
    assert settings.current_allowed_senders() == "feishu:ou_rotated"
    if credential_has_acl:
        credential_file.write_text("OM_FEISHU_BOT_APP_ID=synthetic_app\n", encoding="utf-8")
        assert settings.current_allowed_senders() == ""
    assert attempts == []


@pytest.mark.parametrize("initially_unreadable", [False, True])
def test_explicit_credential_read_failure_still_fails_closed(
    tmp_path, monkeypatch, initially_unreadable,
):
    credential_file = tmp_path / "credential.env"
    credential_file.write_text("OM_FEISHU_BOT_APP_ID=synthetic_app\n", encoding="utf-8")
    if initially_unreadable:
        attempts = _deny_reads(monkeypatch, credential_file)
    settings = _settings(
        tmp_path, environ={ACL_KEYS[0]: "ou_1"}, credential_env_file=credential_file,
    )
    if initially_unreadable:
        attempts.clear()
    else:
        attempts = _deny_reads(monkeypatch, credential_file)
    with pytest.raises(ReplyAuthorizationError, match="sender authorization source unavailable"):
        settings.current_allowed_senders()
    assert attempts == [credential_file]


@pytest.mark.parametrize("implicit_pointer", [False, True])
def test_readable_ordinary_and_credential_acl_keep_overlay_precedence(tmp_path, implicit_pointer):
    env_file = tmp_path / "ordinary.env"
    env_file.write_text(f"{ACL_KEYS[0]}=ou_ordinary\n", encoding="utf-8")
    credential_file = tmp_path / "credential.env"
    credential_file.write_text(f"{ACL_KEYS[0]}=ou_credential\n", encoding="utf-8")
    environment = {ACL_KEYS[0]: "ou_process"}
    if implicit_pointer:
        environment["OM_ENV_FILE"] = str(env_file)
    settings = _settings(
        tmp_path, environ=environment, env_file=None if implicit_pointer else env_file,
        credential_env_file=credential_file,
    )
    assert settings.current_allowed_senders() == "feishu:ou_credential"
    credential_file.write_text(f"{ACL_KEYS[0]}=ou_new_credential\n", encoding="utf-8")
    assert settings.current_allowed_senders() == "feishu:ou_new_credential"
