from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.agent_tools.healthcheck_impl import _channel_health_check
from src.application.bot.control import config_loader
from src.application.channels import status as channel_status
from src.interfaces.cli.channel_ops import handle_channel_command


def _save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)


def _exercise(tmp_path, monkeypatch, *, source, kind, location="top", active=True):
    monkeypatch.setattr(config_loader, "default_bot_config_path", lambda **_: tmp_path / "absent-default.json")
    monkeypatch.setattr(channel_status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=False))
    state = tmp_path / "state"
    state.mkdir()
    _save(state / "state.json", {"bot_token": "synthetic-token-private", "base_url": "https://example.invalid"})
    config = tmp_path / "synthetic.bot.json"
    if kind == "malformed":
        with config.open("x", encoding="utf-8") as stream:
            stream.write("{broken")
    elif kind == "invalid":
        _save(config, {"inbound": {"wechat_clawbot": {"reply_enabled": "not-a-boolean"}}})
    elif kind == "valid":
        _save(config, {"inbound": {"wechat_clawbot": {"allowed_senders": "wechat:synthetic-private"}}})
    elif kind == "empty":
        _save(config, {})
    profile = {
        "service_provider": "systemd",
        "runtime_root": str(tmp_path),
        "services": [{"name": "options-monitor-wechat-clawbot.service"}],
        "feishu_ws": {"audit_db": str(tmp_path / "absent-audit.sqlite3")},
        "wechat_clawbot": {
            "enabled": True,
            "state_dir": str(state),
            "label": "synthetic",
            "allowed_senders_configured": True,
            "allowed_senders_source": source,
        },
    }
    if source == "render_argument":
        profile["wechat_clawbot"]["allowed_senders"] = "wechat:synthetic-private"
    raw_path = None if kind == "none" else "  " if kind == "blank" else str(config)
    if raw_path is not None:
        target = profile if location == "top" else profile["wechat_clawbot"]
        target["bot_config_path"] = raw_path
    _save(tmp_path / "service.profile.json", profile)
    probes = []

    def fake_run(command, **_kwargs):
        assert command[0] == "systemctl"
        assert command[1] in {"is-active", "is-enabled"}
        assert command[2] == "options-monitor-wechat-clawbot.service"
        probes.append(command)
        return SimpleNamespace(returncode=0 if active else 3, stdout="active" if active else "inactive", stderr="")

    def status_response(**kwargs):
        kwargs["environ"] = {}
        return channel_status.channel_status_response(**kwargs, run_cmd=fake_run)

    status = handle_channel_command(
        Namespace(channel_command="status", runtime_root=str(tmp_path),
                  profile_path=str(tmp_path / "service.profile.json"), bot_config=None,
                  env_file=None, include_service_status=active is not None),
        repo_base_fn=lambda: tmp_path,
        channel_status_response_fn=status_response,
    )
    check, warnings = _channel_health_check(status["data"], credential_context_available=True)

    def forbidden_serve(*_args, **_kwargs):
        pytest.fail("serve-check must not enter the real service")

    try:
        serve = handle_channel_command(
            Namespace(channel_command="wechat-clawbot", wechat_clawbot_command="serve", label="synthetic",
                      state_dir=str(state), config_key="us", config_path=None, bot_config=raw_path,
                      audit_db=str(tmp_path / "absent-audit.sqlite3"),
                      allowed_senders="wechat:synthetic-private" if source == "render_argument" else None,
                      no_reply=False, max_reply_chars=None, poll_interval_sec=None, timeout_sec=None,
                      check=True, lock_path=None),
            repo_base_fn=lambda: tmp_path, serve_fn=forbidden_serve,
        )
    except AgentToolError as exc:
        serve = {"ok": False, "error": build_error_payload(exc)}
    _save(tmp_path / "responses.json", {"status": status, "health": check, "warnings": warnings, "serve": serve})
    channel = status["data"]["channels"]["wechat_clawbot"]
    assert status["ok"] is True
    assert channel["configured"] is True
    assert channel["allowed_senders_configured"] is True
    assert channel["allowed_senders_source"] == source
    assert channel["service_active"] is active
    assert channel["service_status_checked"] is (active is not None)
    assert len(probes) == (0 if active is None else 2)
    rendered = json.dumps(status)
    assert "synthetic-token-private" not in rendered
    assert "wechat:synthetic-private" not in rendered
    return status, channel, check, serve


@pytest.mark.parametrize("location", ["top", "nested"])
@pytest.mark.parametrize("source", ["config_yaml", "render_argument"])
@pytest.mark.parametrize("kind,active", [("malformed", False), ("missing", None), ("invalid", True)])
def test_required_wechat_config_failure_is_unavailable(tmp_path, monkeypatch, location, source, kind, active):
    status, channel, check, serve = _exercise(
        tmp_path, monkeypatch, source=source, kind=kind, location=location, active=active,
    )
    assert serve["ok"] is False
    assert serve["error"]["code"] == "CONFIG_ERROR"
    assert channel["available"] is False
    assert channel["bot_config_loaded"] is False
    assert "CONFIG_ERROR" in channel["config_error"]
    assert check["status"] == "warn"
    assert status["data"]["summary"]["wechat_clawbot_available"] is False
    assert "wechat_clawbot" not in status["data"]["summary"]["available_channels"]


@pytest.mark.parametrize("source,kind,expected_serve", [
    ("config_yaml", "valid", True),
    ("render_argument", "valid", True),
    ("render_argument", "none", True),
    ("render_argument", "blank", True),
    ("render_argument", "empty", True),
    ("config_yaml", "none", False),  # Legacy profile metadata without a supplied config path.
])
def test_wechat_valid_and_legacy_config_status_remains_compatible(tmp_path, monkeypatch, source, kind, expected_serve):
    status, channel, check, serve = _exercise(tmp_path, monkeypatch, source=source, kind=kind)
    assert channel["available"] is True
    assert "config_error" not in channel
    assert channel["bot_config_loaded"] is (kind == "valid")
    assert check["status"] == "ok"
    assert serve["ok"] is expected_serve
    assert status["data"]["summary"]["wechat_clawbot_available"] is True


@pytest.mark.parametrize("kind", ["missing", "malformed", "valid"])
def test_feishu_config_loading_keeps_existing_availability(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(config_loader, "default_bot_config_path", lambda **_: tmp_path / "absent-default.json")
    monkeypatch.setattr(channel_status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=True))
    config = tmp_path / "synthetic.bot.json"
    if kind == "malformed":
        with config.open("x", encoding="utf-8") as stream:
            stream.write("{broken")
    elif kind == "valid":
        _save(config, {})
    result = channel_status.channel_status_response(
        base=tmp_path, runtime_root=tmp_path,
        payload={"bot_config_path": str(config), "feishu_ws": {"enabled": True, "audit_db": str(tmp_path / "absent.sqlite3")}},
        environ={"OM_FEISHU_BOT_APP_ID": "synthetic-app", "OM_FEISHU_BOT_USER_OPEN_ID": "synthetic-user"},
    )
    feishu = result["data"]["channels"]["feishu"]
    assert feishu["available"] is True
    assert feishu["credentials_configured"] is True
    assert ("config_error" in feishu) is (kind == "malformed")
