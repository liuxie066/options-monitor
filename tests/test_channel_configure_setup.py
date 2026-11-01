"""Public parser regressions for connection setup and legacy receive migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.control import config_loader
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.application.config_yaml_init import init_yaml_config
from src.interfaces.cli.main import parse_args
from tests.test_channel_authoring_controls import controls
from tests.test_cli_feature_configuration import REPO, run, source


@pytest.fixture
def starter(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = tmp_path / "config.yaml"
    init_yaml_config(repo_root=REPO, account_label="qa", output_config_yaml_path=path,
                     runtime_output_dir=tmp_path, markets=["us"], us_symbols=["NVDA"],
                     futu_acc_id="12345", dry_run=False,
                     symbol_policies={"NVDA": {"strategy": "csp", "csp_max_strike": 100}})
    return path


def command(path, *flags):
    return parse_args(["channel", "configure", "--config-yaml", str(path), *flags])


def confirm(namespace, preview):
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["result"].get("preview_sha256")
    if "env" in preview:
        namespace.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]


def binding(root, label="qa", name="ops"):
    state = root / "output_shared/state/channels/wechat_clawbot" / label
    WechatClawbotStateStore(state).save_bindings({"bindings": {name: {
        "to_user_id": "qa-user", "context_token": "synthetic-context"}}})
    return f"wechat:{label}:{name}"


@pytest.mark.parametrize("combined", [False, True])
def test_starter_feishu_old_identity_flags_preview_apply_and_readback(starter, combined):
    switches = ["--receive-enabled", "true", "--notify-enabled", "false"] if combined else []
    namespace = command(starter, "--provider", "feishu_app", "--enabled", "true", *switches,
                        "--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender")
    before = starter.read_bytes()
    preview = run(namespace)
    assert preview["env"]["changes"] == {"OM_FEISHU_BOT_APP_ID": "qa-app", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_qa",
                                            "OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "ou_sender"}
    assert starter.read_bytes() == before
    assert not (starter.parent / "options-monitor.env").exists()
    confirm(namespace, preview)
    result = run(namespace, secret_runner=lambda *_: pytest.fail("noninteractive setup must not write credentials"))
    assert result["status"] == "configured"
    assert result["env"]["verified"] is True
    assert result["result"]["verified_targets"]
    doc = yaml.safe_load(starter.read_text())
    assert doc["notifications"] == {"enabled": False, "provider": "feishu_app", "channel": "feishu_app"}
    assert doc["channels"]["wechat_clawbot"] == controls()
    assert doc["channels"]["feishu"] == controls(True, combined, False)
    assert "ou_sender" in (starter.parent / "options-monitor.env").read_text()


@pytest.mark.parametrize("combined", [False, True])
def test_starter_wechat_all_old_binding_flags_are_consumed(starter, combined):
    target = binding(starter.parent, label="named", name="ops2")
    switches = ["--receive-enabled", "true", "--notify-enabled", "false"] if combined else []
    namespace = command(starter, "--provider", "wechat_clawbot", "--enabled", "true", *switches,
                        "--target", target, "--label", "named", "--binding-name", "ops2", "--allowed-senders", "wechat:qa-user")
    before = starter.read_bytes()
    preview = run(namespace, wechat_connector=lambda **_: pytest.fail("existing target must stay offline"))
    assert starter.read_bytes() == before
    confirm(namespace, preview)
    result = run(namespace, wechat_connector=lambda **_: pytest.fail("existing target must stay offline"))
    assert result["status"] == "configured"
    doc = yaml.safe_load(starter.read_text())
    assert doc["notifications"]["enabled"] is False
    assert doc["notifications"]["target"] == target
    assert doc["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:qa-user"
    assert doc["inbound"]["wechat_clawbot"]["label"] == "named"
    assert doc["channels"]["feishu"] == controls()
    bot = json.loads(Path(result["result"]["bot"]["output_config_path"]).read_text())
    for key, value in doc["inbound"]["wechat_clawbot"].items():
        assert bot["inbound"]["wechat_clawbot"][key] == value
    assert not (starter.parent / "options-monitor.env").exists()


@pytest.mark.parametrize("cancel", ["env", "config", None])
def test_starter_interactive_feishu_setup_and_cancellation(starter, cancel):
    before = starter.read_bytes()
    replies = ["feishu_app", "true", "true", "false", "qa-app", "ou_qa", "ou_sender", "n" if cancel == "env" else "y"]
    if cancel != "env":
        replies += ["n" if cancel == "config" else "y"]
    if cancel is None:
        replies += ["y", "set"]
    answers = iter(replies)
    secrets = []
    result = run(command(starter, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None,
                 secret_runner=lambda args: secrets.append(args.logical_name) or {"write_applied": True})
    if cancel:
        assert result["status"] == "cancelled"
        assert starter.read_bytes() == before
        assert not secrets
    else:
        assert result["status"] == "configured"
        assert secrets == ["feishu.bot.app_secret"]
        assert yaml.safe_load(starter.read_text())["channels"]["feishu"] == controls(True, True, False)
    assert (starter.parent / "options-monitor.env").exists() is (cancel != "env")


@pytest.mark.parametrize("cancel", [False, True])
def test_starter_interactive_wechat_binding_flow(starter, cancel):
    before = starter.read_bytes()
    answers = iter(["wechat_clawbot", "true", "true", "false", "y", "wechat:qa-user", "y"])
    calls = []
    def connect(**kwargs):
        calls.append(kwargs)
        if cancel:
            raise KeyboardInterrupt
        return {"ok": True, "data": {"target": binding(starter.parent, label="default")}}
    result = run(command(starter, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None, wechat_connector=connect)
    assert len(calls) == 1
    assert calls[0]["base"] == starter.parent
    if cancel:
        assert result["status"] == "cancelled"
        assert result["completed_steps"][0]["write_applied"] is None
        assert starter.read_bytes() == before
    else:
        assert result["status"] == "configured"
        assert yaml.safe_load(starter.read_text())["channels"]["wechat_clawbot"] == controls(True, True, False)


@pytest.mark.parametrize("provider,flags", [
    ("feishu_app", ["--target", "wechat:default:ops"]),
    ("feishu_app", ["--label", "qa"]), ("feishu_app", ["--binding-name", "ops"]),
    ("wechat_clawbot", ["--app-id", "qa-app"]), ("wechat_clawbot", ["--recipient", "ou_qa"]),
])
def test_foreign_platform_flags_are_rejected_before_any_effect(starter, provider, flags):
    before = starter.read_bytes()
    with pytest.raises(AgentToolError, match="INPUT_ERROR"):
        run(command(starter, "--provider", provider, "--enabled", "true", *flags),
            wechat_connector=lambda **_: pytest.fail("unsupported flags must not connect"),
            secret_runner=lambda *_: pytest.fail("unsupported flags must not provision"))
    assert starter.read_bytes() == before
    assert not (starter.parent / "options-monitor.env").exists()


@pytest.mark.parametrize("routes", [[], [{"id": "qa", "provider": "feishu_app", "target": "ou_qa", "accounts": ["qa"]}]])
def test_connection_setup_explicitly_rejects_multi_route_form_before_env_or_qr(starter, routes):
    doc = yaml.safe_load(starter.read_text())
    doc["notifications"]["routes"] = routes
    starter.write_text(yaml.safe_dump(doc))
    for provider, flags in [("feishu_app", ["--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender"]),
                            ("wechat_clawbot", ["--target", "wechat:default:ops"])]:
        with pytest.raises(AgentToolError, match="notifications.routes"):
            run(command(starter, "--provider", provider, "--enabled", "true", *flags))
    assert yaml.safe_load(starter.read_text()) == doc
    assert not (starter.parent / "options-monitor.env").exists()


@pytest.mark.parametrize("changed", ["source", "env"])
def test_combined_setup_rejects_stale_source_or_env_without_side_effects(starter, changed):
    namespace = command(starter, "--provider", "feishu_app", "--enabled", "true", "--receive-enabled", "true",
                        "--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender")
    preview = run(namespace)
    confirm(namespace, preview)
    if changed == "source":
        starter.write_text(starter.read_text() + "# concurrent source\n")
    else:
        (starter.parent / "options-monitor.env").write_text("UNRELATED=keep\n")
    before = starter.read_bytes()
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run(namespace)
    assert starter.read_bytes() == before
    if changed == "env":
        assert (starter.parent / "options-monitor.env").read_text() == "UNRELATED=keep\n"


def test_combined_setup_validates_resume_hash_before_env_write(starter):
    doc = yaml.safe_load(starter.read_text())
    doc["notifications"] = {"enabled": True, "provider": "feishu_app"}
    starter.write_text(yaml.safe_dump(doc))
    namespace = command(starter, "--provider", "feishu_app", "--enabled", "true", "--notify-enabled", "true",
                        "--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender")
    preview = run(namespace)
    assert preview["result"]["notification_resume"]["includes_accumulated_while_disabled"]
    confirm(namespace, preview)
    expected = namespace.expected_preview_sha256
    for digest, code in [(None, "CONFIRMATION_REQUIRED"), ("wrong", "STALE_PREVIEW")]:
        namespace.expected_preview_sha256 = digest
        with pytest.raises(AgentToolError, match=code):
            run(namespace)
        assert not (starter.parent / "options-monitor.env").exists()
    namespace.expected_preview_sha256 = expected
    assert run(namespace)["status"] == "configured"


@pytest.mark.parametrize("provider,flags", [
    ("feishu_app", ["--app-id", "qa-app"]),
    ("wechat_clawbot", ["--target", "wechat:default:ops"]),
])
def test_setup_plus_disabled_master_is_explicitly_rejected(starter, provider, flags):
    with pytest.raises(AgentToolError, match="--enabled false"):
        run(command(starter, "--provider", provider, "--enabled", "false", *flags))
    assert not (starter.parent / "options-monitor.env").exists()


@pytest.mark.parametrize("activity_flag", [False, True])
@pytest.mark.parametrize("provider,flags", [
    ("feishu_app", ["--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender"]),
    ("wechat_clawbot", ["--target", "wechat:default:ops", "--allowed-senders", "wechat:user"]),
])
def test_omitted_master_off_rejects_setup_before_effects(starter, monkeypatch, provider, flags, activity_flag):
    from src.interfaces.cli import feature_ops
    from src.application.channels.wechat_clawbot import state

    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled setup must reject before effects or binding reads")
    monkeypatch.setattr(feature_ops, "write_feature_env", forbidden)
    monkeypatch.setattr(feature_ops, "publish_feature_document", forbidden)
    monkeypatch.setattr(state, "load_wechat_clawbot_binding", forbidden)
    before = starter.read_bytes()
    switches = ["--receive-enabled", "true"] if activity_flag else []
    with pytest.raises(AgentToolError, match="INPUT_ERROR.*--enabled false"):
        run(command(starter, "--provider", provider, *switches, *flags),
            input_is_tty=lambda: True, input_fn=lambda _: "", output_fn=lambda _: None,
            secret_runner=forbidden, wechat_connector=forbidden)
    assert calls == []
    assert starter.read_bytes() == before
    assert not (starter.parent / "options-monitor.env").exists()


def test_setup_cannot_replace_other_platform_route(starter):
    doc = yaml.safe_load(starter.read_text())
    doc["notifications"].update(provider="feishu_app", channel="feishu_app")
    starter.write_text(yaml.safe_dump(doc))
    with pytest.raises(AgentToolError, match="another platform"):
        run(command(starter, "--provider", "wechat_clawbot", "--enabled", "true", "--target", "wechat:default:ops"))
    assert yaml.safe_load(starter.read_text()) == doc


def profile(path, *, channel="feishu", runtime=None):
    key = "feishu_ws" if channel == "feishu" else channel
    profile_path = path.parent / "service.profile.json"
    profile_path.write_text(json.dumps({"runtime_root": str(runtime or path.parent),
        "config_authoring": {"source": "yaml", "config_yaml": str(path)}, key: {"enabled": True}}))
    return profile_path


@pytest.mark.parametrize("other,selected", [("feishu", "wechat_clawbot"), ("wechat_clawbot", "feishu_app")])
def test_first_migration_preserves_profile_only_receiver_through_real_publish_and_guard(tmp_path, monkeypatch, other, selected):
    path = source(tmp_path)
    profile_path = profile(path, channel=other)
    before_profile = profile_path.read_bytes()
    namespace = command(path, "--provider", selected, "--receive-enabled", "true")
    before = path.read_bytes()
    preview = run(namespace)
    assert path.read_bytes() == before
    assert preview["result"]["legacy_service_profile"]["receive_channels"] == [other]
    confirm(namespace, preview)
    result = run(namespace)
    doc = yaml.safe_load(path.read_text())
    assert doc["channels"][other] == controls(True, True, False)
    assert doc["channels"]["wechat_clawbot" if other == "feishu" else "feishu"] == controls(False, True, False)
    assert profile_path.read_bytes() == before_profile
    monkeypatch.setattr(config_loader.plugins, "channel_plugin_installed", lambda _: True)
    config_loader.require_inbound_channel(other, result["result"]["bot"]["output_config_path"])


def test_explicit_authoring_disable_wins_over_profile(starter):
    profile(starter)
    namespace = command(starter, "--provider", "wechat_clawbot", "--receive-enabled", "true")
    preview = run(namespace)
    assert "legacy_service_profile" not in preview["result"]
    confirm(namespace, preview)
    run(namespace)
    assert yaml.safe_load(starter.read_text())["channels"]["feishu"] == controls()


def test_legacy_authoring_receive_disable_wins_over_profile(tmp_path):
    path = source(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["inbound"] = {"feishu_ws": {"enabled": False}}
    path.write_text(yaml.safe_dump(doc))
    profile(path)
    namespace = command(path, "--provider", "wechat_clawbot", "--receive-enabled", "true")
    preview = run(namespace)
    confirm(namespace, preview)
    run(namespace)
    assert yaml.safe_load(path.read_text())["channels"]["feishu"] == controls()


def test_migration_profile_revision_is_bound_to_confirmed_preview(tmp_path):
    path = source(tmp_path)
    profile_path = profile(path)
    namespace = command(path, "--provider", "wechat_clawbot", "--receive-enabled", "true")
    preview = run(namespace)
    confirm(namespace, preview)
    saved = profile_path.read_bytes()
    profile_path.write_text(json.dumps({"runtime_root": str(tmp_path), "feishu_ws": {"enabled": False}}))
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run(namespace)
    assert "channels" not in yaml.safe_load(path.read_text())
    profile_path.write_bytes(saved)
    assert run(namespace)["status"] == "configured"


def test_unbound_profile_cannot_supply_migration_intent(tmp_path):
    path = source(tmp_path)
    profile(path, runtime=tmp_path / "other-instance")
    with pytest.raises(AgentToolError, match="not bound"):
        run(command(path, "--provider", "wechat_clawbot", "--receive-enabled", "true"))
    assert "channels" not in yaml.safe_load(path.read_text())


@pytest.mark.parametrize("selection,expected", [
    ({"services": [{"name": "options-monitor-feishu-ws.service"}]}, True),
    ({"services": [{"name": "unrelated-feishu.service"}]}, False),
    ({"services": [{"name": "options-monitor-feishu-ws.service"}], "feishu_ws": {"enabled": False}}, False),
    ({}, False),
])
def test_migration_uses_exact_service_choices_not_credentials(tmp_path, monkeypatch, selection, expected):
    path = source(tmp_path)
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "qa-app")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_qa")
    (tmp_path / "service.profile.json").write_text(json.dumps({"runtime_root": str(tmp_path), **selection}))
    namespace = command(path, "--provider", "wechat_clawbot", "--receive-enabled", "true")
    preview = run(namespace)
    confirm(namespace, preview)
    run(namespace)
    assert yaml.safe_load(path.read_text())["channels"]["feishu"] == controls(expected, expected, False)
