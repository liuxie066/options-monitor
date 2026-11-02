"""First configuration uses setup consent, never the runtime receive switch."""
from __future__ import annotations

import json
import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.control import config_loader
from src.application.channels.wechat_clawbot import binding, inbound
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotError
from tests.test_channel_configure_setup import command, controls, run, starter  # noqa: F401


@pytest.fixture
def setup(starter, tmp_path, monkeypatch):
    # A separate selected instance must win over the default/operator instance.
    root = tmp_path / "selected-runtime"
    bot = root / "resolved/config.bot.json"
    bot.parent.mkdir(parents=True)
    bot.write_bytes((starter.parent / "config.bot.json").read_bytes())
    monkeypatch.setattr(config_loader, "default_bot_config_path", lambda **kw:
                        bot if kw.get("repo_root") in (None, root) else tmp_path / "wrong-instance.json")
    calls = []
    failure = {"stage": None}

    class Client:
        def __init__(self, **kwargs):
            self.token = kwargs["bot_token"]

        def get_bot_qrcode(self, **kwargs):
            calls.append("qr")
            return {"qrcode": "offline-qr"}

        def get_qrcode_status(self, **kwargs):
            calls.append("login")
            if failure["stage"] == "login_cancel":
                raise KeyboardInterrupt
            return {"status": "confirmed", "bot_token": "fixture-token", "get_updates_buf": "buf"}

        def get_updates(self, **kwargs):
            assert self.token == "fixture-token"
            calls.append("updates")
            if failure["stage"] == "bind_cancel":
                raise KeyboardInterrupt
            if failure["stage"] == "bind_error":
                raise AgentToolError(code="UPSTREAM_ERROR", message="offline failure")
            if failure["stage"] == "provider_error":
                raise WechatClawbotError("offline iLink failure after login")
            if failure["stage"] == "bind_timeout":
                return {"msgs": []}
            return {"msgs": [{"from_user_id": "qa-user", "context_token": "fixture-context",
                              "message_id": "bind-1", "text_item": {"text": "bind ops"}}]}

    def connect(**kwargs):
        return binding.connect_wechat_clawbot_target(
            **kwargs, client_factory=Client, poll_interval_sec=0, bind_timeout_sec=0)

    return starter, root, bot, calls, failure, connect


def configure(setup, *, receive=True, cancel=None):
    source, root, _, _, _, connect = setup
    answers = iter(["wechat_clawbot", "true", str(receive).lower(), str(not receive).lower(),
                    "n" if cancel == "consent" else "y", "wechat:qa-user",
                    "n" if cancel == "publication" else "y"])
    def output(text):
        if cancel in {"login_confirmed", "bound"} and json.loads(text).get("event") == cancel:
            raise KeyboardInterrupt

    return run(command(source, "--interactive", "--runtime-root", str(root)),
               input_is_tty=lambda: True, input_fn=lambda _: next(answers),
               output_fn=output, wechat_connector=connect)


@pytest.mark.parametrize("receive", [True, False])
def test_first_real_connector_configures_selected_instance(setup, receive):
    source, root, bot, calls, _, _ = setup
    other_before = (source.parent / "config.bot.json").read_bytes()
    result = configure(setup, receive=receive)
    assert result["status"] == "configured"
    assert calls == ["qr", "login", "updates"]
    expected = controls(True, receive, not receive)
    assert yaml.safe_load(source.read_text())["channels"]["wechat_clawbot"] == expected
    assert json.loads(bot.read_text())["channels"]["wechat_clawbot"] == expected
    assert (source.parent / "config.bot.json").read_bytes() == other_before
    step = result["completed_steps"][0]
    assert step["bot_config_path"] == str(bot)
    assert step["readback"] == {"login_saved": True, "binding_saved": True}
    assert "fixture-token" not in json.dumps(result)
    assert "fixture-context" not in json.dumps(result)
    if not receive:
        with pytest.raises(AgentToolError, match="CHANNEL_RECEIVE_DISABLED"):
            inbound.poll_wechat_clawbot_once(base=root, bot_config_path=str(bot),
                client_factory=lambda **_: pytest.fail("notify-only cannot poll"))


@pytest.mark.parametrize("stage", ["login_cancel", "login_confirmed", "bind_cancel", "bound",
                                   "bind_timeout", "bind_error", "provider_error", "publication"])
def test_partial_setup_reports_saved_facts_without_publishing(setup, stage):
    source, root, bot, calls, failure, _ = setup
    before = source.read_bytes(), bot.read_bytes()
    failure["stage"] = stage
    if stage in {"bind_error", "provider_error"}:
        with pytest.raises(AgentToolError, match="UPSTREAM_ERROR|WECHAT_CONNECTION_ERROR") as exc:
            configure(setup)
        assert exc.value.code == ("UPSTREAM_ERROR" if stage == "bind_error" else "WECHAT_CONNECTION_ERROR")
        steps = exc.value.details["completed_steps"]
    else:
        result = configure(setup, cancel=stage)
        assert result["status"] == ("binding_incomplete" if stage == "bind_timeout" else "cancelled")
        steps = result["completed_steps"]
    assert source.read_bytes() == before[0]
    assert bot.read_bytes() == before[1]
    assert steps[0]["readback"] == {"login_saved": stage != "login_cancel",
                                    "binding_saved": stage in {"publication", "bound"}}
    assert steps[0]["state_dir"] == str(root / "output_shared/state/channels/wechat_clawbot/default")
    assert "fixture-token" not in json.dumps(steps)
    assert "fixture-context" not in json.dumps(steps)
    assert calls[:2] == ["qr", "login"]


def test_declining_setup_never_contacts_provider(setup):
    source, _, bot, calls, _, _ = setup
    before = source.read_bytes(), bot.read_bytes()
    result = configure(setup, cancel="consent")
    assert result["status"] == "cancelled"
    assert calls == []
    assert (source.read_bytes(), bot.read_bytes()) == before


def test_ordinary_binding_keeps_disabled_guard(setup):
    _, root, bot, calls, _, _ = setup
    with pytest.raises(AgentToolError, match="CHANNEL_RECEIVE_DISABLED"):
        binding.bind_wechat_clawbot_target(base=root, name="ops", bot_config_path=str(bot),
            client_factory=lambda **_: pytest.fail("disabled binding cannot contact provider"))
    assert calls == []


@pytest.mark.parametrize("invalid", ["missing_path", "missing_config", "malformed_config", "missing_plugin"])
def test_setup_authority_rejects_invalid_instance_before_qr(setup, monkeypatch, invalid):
    _, root, bot, calls, _, connect = setup
    path = str(bot)
    if invalid == "missing_path":
        path = None
    elif invalid == "missing_config":
        bot.unlink()
    elif invalid == "malformed_config":
        bot.write_text("{")
    else:
        monkeypatch.setattr("src.application.channels.plugins.channel_plugin_installed", lambda _: False)
    with pytest.raises(AgentToolError, match="INPUT_ERROR|CONFIG_ERROR|CHANNEL_PLUGIN_NOT_INSTALLED"):
        connect(base=root, name="ops", bot_config_path=path, setup_authorized=True)
    assert calls == []


def test_selected_instance_wins_over_process_default(setup, monkeypatch):
    source, _, _, _, _, _ = setup
    wrong = source.parent / "wrong-instance.json"
    wrong.write_text("{")
    monkeypatch.setattr(config_loader, "default_bot_config_path", lambda **_: wrong)
    assert configure(setup)["status"] == "configured"
