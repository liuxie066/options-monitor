from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.channels import status
from src.application.config_features import feature_document
from src.application.config_yaml import default_yaml_bot_config_path
from tests.test_cli_feature_configuration import args, run, source


def controls(enabled=False, receive=False, notify=False):
    return {"enabled": enabled, "receive_enabled": receive, "notify_enabled": notify}


@pytest.mark.parametrize("provider,selected,other", [
    ("feishu_app", "feishu", "wechat_clawbot"),
    ("wechat_clawbot", "wechat_clawbot", "feishu"),
])
@pytest.mark.parametrize("flag", ["enabled", "receive_enabled", "notify_enabled"])
def test_each_control_changes_only_selected_platform_flag(provider, selected, other, flag):
    doc = {"channels": {selected: controls(True, True, True), other: controls(True, True, True)},
           "notifications": {"enabled": False, "provider": "wechat_clawbot", "target": "wechat:old:ops",
                             "routes": [{"id": "keep", "provider": "feishu_app", "target": "ou_fixture", "accounts": ["lx"]}]}}
    original = deepcopy(doc)
    values = {"provider": provider, **({flag: False} if flag != "enabled" else {})}
    changed = feature_document(doc, feature="channel", enabled=False if flag == "enabled" else None, values=values)
    expected = deepcopy(doc)
    expected["channels"][selected][flag] = False
    assert changed == expected
    assert doc == original


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_authoring_rejects_non_boolean_activity_controls(bad):
    with pytest.raises(AgentToolError, match="booleans"):
        feature_document({}, feature="channel", enabled=True, values={"provider": "feishu", "receive_enabled": bad})


def test_first_materialization_preserves_explicit_legacy_intent_without_credentials():
    doc = {"notifications": {"enabled": True, "provider": "feishu_app"},
           "inbound": {"wechat_clawbot": {"allowed_senders": "wechat:fixture"}}}
    changed = feature_document(doc, feature="channel", enabled=None,
                               values={"provider": "feishu_app", "receive_enabled": False})
    assert changed["channels"] == {"feishu": controls(True, False, True), "wechat_clawbot": controls(True, True, False)}
    assert changed["notifications"] == doc["notifications"]


def test_first_materialization_retains_both_explicit_routes():
    doc = {"notifications": {"enabled": True, "routes": [
        {"provider": "feishu_app"}, {"provider": "wechat_clawbot"}]}}
    changed = feature_document(doc, feature="channel", enabled=None,
                               values={"provider": "feishu_app", "receive_enabled": False})
    assert changed["channels"] == {"feishu": controls(True, False, True), "wechat_clawbot": controls(True, False, True)}


def test_materialization_preserves_inherited_market_notification_intent():
    doc = {"notifications": {"enabled": False, "provider": "feishu_app"},
           "markets": {"us": {"notifications": {"enabled": True}}}}
    changed = feature_document(doc, feature="channel", enabled=True,
                               values={"provider": "wechat_clawbot", "receive_enabled": True})
    assert changed["channels"] == {"feishu": controls(True, False, True), "wechat_clawbot": controls(True, True, False)}
    assert changed["markets"] == doc["markets"]
    assert changed["notifications"] == doc["notifications"]


def test_credentials_do_not_enable_other_channel_on_materialization(monkeypatch):
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_fixture")
    changed = feature_document({"notifications": {"enabled": False}}, feature="channel", enabled=True,
                               values={"provider": "wechat_clawbot", "receive_enabled": True})
    assert changed["channels"] == {"feishu": controls(), "wechat_clawbot": controls(True, True, False)}
    assert changed["notifications"]["enabled"] is False


def test_explicit_empty_channels_never_inherits_legacy_activity():
    changed = feature_document({"channels": {}, "inbound": {"feishu_ws": {"reply_enabled": True}},
                                "notifications": {"enabled": True, "provider": "feishu_app"}},
                               feature="channel", enabled=True, values={"provider": "feishu_app"})
    assert changed["channels"] == {"feishu": controls(True)}


def test_new_setup_controls_preview_apply_readback_without_env_or_credentials(tmp_path, monkeypatch):
    from src.interfaces.cli import feature_ops

    path = source(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["channels"] = {"feishu": controls(), "wechat_clawbot": controls()}
    path.write_text(yaml.safe_dump(doc))
    before = path.read_bytes()
    monkeypatch.setattr(feature_ops, "write_feature_env", lambda **_: pytest.fail("controls must not write env"))
    namespace = args("channel", path, "--provider", "feishu_app", "--enabled", "true", "--receive-enabled", "true",
                     "--notify-enabled", "false")
    preview = run(namespace, input_is_tty=lambda: False)
    assert path.read_bytes() == before
    assert preview["result"]["requested_setting"]["controls"] == controls(True, True, False)
    assert "env" not in preview
    assert "notification_resume" not in preview["result"]
    namespace.apply = True
    with pytest.raises(AgentToolError, match="CONFIRMATION_REQUIRED"):
        run(namespace)
    namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    result = run(namespace, secret_runner=lambda *_: pytest.fail("controls do not provision secrets"))
    assert result["status"] == "configured"
    expected = {"feishu": controls(True, True, False), "wechat_clawbot": controls()}
    assert yaml.safe_load(path.read_text())["channels"] == expected
    for target in result["result"]["verified_targets"]:
        if target.endswith(".json"):
            assert json.loads(Path(target).read_text())["channels"] == expected
    assert result["result"]["restart_performed"] is False
    assert not (tmp_path / "options-monitor.env").exists()


@pytest.mark.parametrize("flag,value", [("--notify-enabled", "true"), ("--enabled", "true")])
def test_resuming_channel_proactive_delivery_requires_bound_preview(tmp_path, flag, value):
    path = source(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc.update(channels={"feishu": controls(flag != "--enabled", True, flag != "--notify-enabled")},
               notifications={"enabled": True, "provider": "feishu_app"})
    path.write_text(yaml.safe_dump(doc))
    before = path.read_bytes()
    namespace = args("channel", path, "--provider", "feishu_app", flag, value)
    preview = run(namespace)["result"]
    assert preview["notification_resume"]["includes_accumulated_while_disabled"] is True
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["source_revision"]["before_sha256"]
    for digest, error in [(None, "CONFIRMATION_REQUIRED"), ("wrong", "STALE_PREVIEW")]:
        namespace.expected_preview_sha256 = digest
        with pytest.raises(AgentToolError, match=error):
            run(namespace)
        assert path.read_bytes() == before
        assert not (tmp_path / "config.us.json").exists()
    namespace.expected_preview_sha256 = preview["preview_sha256"]
    assert run(namespace)["status"] == "configured"


def test_controls_reject_stale_source_and_interactive_cancel(tmp_path):
    path = source(tmp_path)
    namespace = args("channel", path, "--provider", "wechat_clawbot", "--enabled", "true", "--receive-enabled", "true")
    preview = run(namespace)["result"]
    path.write_text(path.read_text() + "# concurrent operator\n")
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["source_revision"]["before_sha256"]
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run(namespace)
    before = path.read_bytes()
    cancel = args("channel", path, "--provider", "wechat_clawbot", "--enabled", "true", "--receive-enabled", "true", "--interactive")
    assert run(cancel, input_is_tty=lambda: True, input_fn=lambda _: "n", output_fn=lambda _: None)["status"] == "cancelled"
    assert path.read_bytes() == before


def test_activity_only_preserves_master_and_missing_provider_is_rejected(tmp_path):
    path = source(tmp_path)
    namespace = args("channel", path, "--notify-enabled", "false")
    with pytest.raises(AgentToolError, match="--provider"):
        run(namespace, input_is_tty=lambda: False)
    namespace.provider = "wechat_clawbot"
    assert run(namespace, input_is_tty=lambda: False)["result"]["requested_setting"]["controls"] == controls()


@pytest.mark.parametrize("channel", ["feishu", "wechat_clawbot"])
@pytest.mark.parametrize("installed,master,receive,notify", [
    (False, True, True, True), (True, False, True, True),
    (True, True, False, False), (True, True, True, False),
    (True, True, False, True), (True, True, True, True),
])
def test_status_separates_package_configuration_and_controls(tmp_path, monkeypatch, channel, installed, master, receive, notify):
    monkeypatch.setattr(status, "channel_plugin_installed", lambda name: installed if name == channel else False)
    monkeypatch.setattr(status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=True))
    state = tmp_path / "wechat-state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"bot_token": "fixture-private-token"}))
    config = tmp_path / "config.bot.json"
    config.write_text(json.dumps({"channels": {channel: controls(master, receive, notify)},
                                  "inbound": {"wechat_clawbot": {"allowed_senders": "wechat:fixture"}}}))
    result = status.build_channel_status(
        base=tmp_path, runtime_root=tmp_path,
        payload={"wechat_clawbot": {"state_dir": str(state), "enabled": True}, "feishu_ws": {"enabled": True}},
        environ={"OM_FEISHU_BOT_APP_ID": "fixture", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_fixture"})
    health = result["channels"][channel]
    assert health["configured"] is True
    assert health["installed"] is installed
    assert health["master_enabled"] is master
    assert health["receive_enabled"] is receive
    assert health["notify_enabled"] is notify
    assert health["available"] is (installed and master and (receive or notify))
    assert health["reply_enabled"] is (installed and master and receive)
    assert "fixture-private-token" not in json.dumps(result)
    other = "wechat_clawbot" if channel == "feishu" else "feishu"
    assert result["channels"][other]["enabled"] is False
    assert result["channels"][other]["available"] is False


def test_installed_and_credentials_do_not_make_explicit_missing_channel_available(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "channel_plugin_installed", lambda _: True)
    monkeypatch.setattr(status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=True))
    (tmp_path / "config.bot.json").write_text(json.dumps({"channels": {}}))
    result = status.build_channel_status(base=tmp_path, runtime_root=tmp_path,
        payload={"feishu_ws": {"enabled": True}},
        environ={"OM_FEISHU_BOT_APP_ID": "fixture", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_fixture"})
    assert result["channels"]["feishu"]["configured"] is True
    assert result["channels"]["feishu"]["installed"] is True
    assert result["channels"]["feishu"]["available"] is False


@pytest.mark.parametrize("repo_local_legacy", [False, True])
def test_status_discovers_canonical_generated_bot_config_with_repo_local_precedence(tmp_path, monkeypatch, repo_local_legacy):
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    # A caller-selected runtime must beat a different inherited environment.
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "other-runtime"))
    config = default_yaml_bot_config_path(repo_root=repo, runtime_root=runtime)
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"channels": {"feishu": controls(), "wechat_clawbot": controls()}}))
    legacy = repo / "config.bot.json"
    if repo_local_legacy:
        legacy.write_text("{}")
    monkeypatch.setattr(status, "channel_plugin_installed", lambda _: True)
    monkeypatch.setattr(status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=True))
    result = status.build_channel_status(base=repo, runtime_root=runtime, environ={
        "OM_FEISHU_BOT_APP_ID": "fixture", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_fixture"},
        mask_path=str)
    assert result["profile"] == {"loaded": False}
    for health in result["channels"].values():
        assert health["bot_config_path"] == str(legacy if repo_local_legacy else config)
        assert health["controls_explicit"] is (not repo_local_legacy)
        assert health["master_enabled"] is repo_local_legacy
    assert result["channels"]["feishu"]["available"] is repo_local_legacy


def test_status_explicit_bot_path_beats_discovered_paths(tmp_path, monkeypatch):
    (tmp_path / "config.bot.json").write_text("{}")
    config = default_yaml_bot_config_path(repo_root=tmp_path, runtime_root=tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("{}")
    explicit = tmp_path / "selected.bot.json"
    explicit.write_text(json.dumps({"channels": {}}))
    monkeypatch.setattr(status, "channel_plugin_installed", lambda _: True)
    result = status.build_channel_status(base=tmp_path, runtime_root=tmp_path,
        payload={"bot_config_path": str(explicit)}, environ={}, mask_path=str)
    for health in result["channels"].values():
        assert health["bot_config_path"] == str(explicit)
        assert health["controls_explicit"] is True
        assert health["available"] is False
