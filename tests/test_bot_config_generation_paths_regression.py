"""Publish the Bot generation to existing consumers as one recoverable change."""
from __future__ import annotations

import json

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.control.config_loader import default_bot_config_path, require_inbound_channel
from src.application.channels.wechat_clawbot import binding
from src.application import config_authoring_transaction as transaction
from src.application.config_features import publish_feature_document
from src.interfaces.cli.command_environment import command_environment
from test_channel_configure_setup import command, run, starter  # noqa: F401
from test_cli_feature_configuration import REPO


def test_starter_configure_updates_real_default_and_explicit_legacy_consumers(starter):
    root = starter.parent
    selected = default_bot_config_path(repo_root=root)
    assert selected == root / "config.bot.json"

    class Client:
        def __init__(self, **kwargs):
            pass

        def get_bot_qrcode(self, **kwargs):
            return {"qrcode": "offline-qr"}

        def get_qrcode_status(self, **kwargs):
            return {"status": "confirmed", "bot_token": "fixture-token"}

        def get_updates(self, **kwargs):
            return {"msgs": [{"from_user_id": "qa-user", "context_token": "fixture-context",
                              "message_id": "bind-1", "text_item": {"text": "bind ops"}}]}

    def connect(**kwargs):
        assert kwargs["bot_config_path"] == str(selected)
        return binding.connect_wechat_clawbot_target(**kwargs, client_factory=Client,
            poll_interval_sec=0, bind_timeout_sec=0)

    answers = iter(["wechat_clawbot", "true", "true", "true", "y", "wechat:qa-user", "y"])
    with command_environment(["channel", "configure", "--runtime-root", str(root)], repo_root=REPO):
        result = run(command(starter, "--interactive", "--runtime-root", str(root)),
            input_is_tty=lambda: True, input_fn=lambda _: next(answers), output_fn=lambda _: None,
            wechat_connector=connect)
        assert result["status"] == "configured"
        assert default_bot_config_path(repo_root=root) == selected
        require_inbound_channel("wechat_clawbot", str(selected))
        require_inbound_channel("wechat_clawbot", str(root / "resolved/config.bot.json"))
    assert str(selected) in result["result"]["verified_targets"]
    assert yaml.safe_load(starter.read_text())["channels"]["wechat_clawbot"]["receive_enabled"] is True


@pytest.mark.parametrize("fail_target", ["config.bot.json", "resolved/config.bot.json", "config.yaml"])
def test_legacy_bot_output_is_previewed_and_compensated_with_generation(starter, monkeypatch, fail_target):
    root = starter.parent
    canonical = root / "resolved/config.bot.json"
    canonical.parent.mkdir()
    canonical.write_bytes((root / "config.bot.json").read_bytes())
    paths = [starter, root / "config.bot.json", canonical]
    before = {path: path.read_bytes() for path in paths}
    doc = yaml.safe_load(starter.read_text())
    doc["channels"]["wechat_clawbot"] = {"enabled": True, "receive_enabled": True, "notify_enabled": False}
    kwargs = dict(repo_root=REPO, config_path=starter, config_doc=doc, runtime_root=root)
    preview = publish_feature_document(**kwargs)
    assert preview["bot"]["additional_outputs"][0]["output_config_path"] == str(root / "config.bot.json")
    assert all(path.read_bytes() == raw for path, raw in before.items())
    write = transaction._atomic_write_bytes
    failed = False

    def interrupted(path, payload):
        nonlocal failed
        if path == root / fail_target and not failed:
            failed = True
            raise OSError("isolated publication failure")
        return write(path, payload)

    monkeypatch.setattr(transaction, "_atomic_write_bytes", interrupted)
    with pytest.raises(AgentToolError, match="CONFIG_WRITE_FAILED"):
        publish_feature_document(**kwargs, apply=True)
    assert failed and all(path.read_bytes() == raw for path, raw in before.items())
    result = publish_feature_document(**kwargs, apply=True)
    assert str(root / "config.bot.json") in result["verified_targets"]
    for path in paths[1:]:
        assert json.loads(path.read_bytes())["channels"]["wechat_clawbot"]["receive_enabled"] is True


@pytest.mark.parametrize("link_direction", ["legacy_to_canonical", "canonical_to_legacy"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_same_bot_target_through_symlink_publishes_once_and_can_recover(starter, monkeypatch, link_direction, interrupt):
    root = starter.parent
    legacy = root / "config.bot.json"
    canonical = root / "resolved/config.bot.json"
    canonical.parent.mkdir()
    if link_direction == "legacy_to_canonical":
        canonical.write_bytes(legacy.read_bytes())
        legacy.unlink()
        legacy.symlink_to(canonical)
    else:
        canonical.symlink_to(legacy)
    before = {path: path.read_bytes() for path in (starter, legacy, canonical)}
    doc = yaml.safe_load(starter.read_text())
    doc["channels"]["wechat_clawbot"] = {"enabled": True, "receive_enabled": True, "notify_enabled": False}
    kwargs = dict(repo_root=REPO, config_path=starter, config_doc=doc, runtime_root=root, apply=True)
    if interrupt:
        write = transaction._atomic_write_bytes
        failed = False

        def interrupted(path, payload):
            nonlocal failed
            if path == starter and not failed:
                failed = True
                raise OSError("isolated YAML publication failure")
            return write(path, payload)

        with monkeypatch.context() as patch:
            patch.setattr(transaction, "_atomic_write_bytes", interrupted)
            with pytest.raises(AgentToolError, match="CONFIG_WRITE_FAILED") as caught:
                publish_feature_document(**kwargs)
        assert failed and caught.value.details["recovery_error"] is None
        assert all(path.read_bytes() == raw for path, raw in before.items())
    result = publish_feature_document(**kwargs)
    assert not result["bot"].get("additional_outputs")
    assert legacy.resolve() == canonical.resolve()
    for path in (legacy, canonical):
        require_inbound_channel("wechat_clawbot", str(path))
