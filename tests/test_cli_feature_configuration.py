from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_env import env_source_sha256, write_feature_env
from src.interfaces.cli.feature_ops import add_feature_configure_parser, run_feature_configure

REPO = Path(__file__).resolve().parents[1]


def source(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "accounts": {"lx": {"type": "futu", "futu_account_id": "12345678"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
        "bot": {'enabled': False},
        "notifications": {"enabled": False},
    }))
    return path


def args(feature, path, *flags):
    parser = argparse.ArgumentParser()
    add_feature_configure_parser(parser.add_subparsers(), feature)
    return parser.parse_args(["configure", "--config-yaml", str(path), *flags])


def run(namespace, **kwargs):
    return run_feature_configure(namespace, repo_base_fn=lambda: REPO, **kwargs)


def test_env_authoring_preview_revision_backup_readback(tmp_path):
    path = tmp_path / "options-monitor.env"
    original = b"# retain\nUNRELATED=123\nOM_FEISHU_BOT_APP_ID=old\n"
    path.write_bytes(original)
    updates = {"OM_FEISHU_BOT_APP_ID": "cli-fixture", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_fixture"}
    preview = write_feature_env(path=path, updates=updates)
    assert path.read_bytes() == original
    path.write_bytes(original + b"# concurrent\n")
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        write_feature_env(path=path, updates=updates, apply=True,
                          expected_source_sha256=preview["source_revision"]["before_sha256"])
    fresh = env_source_sha256(path)
    result = write_feature_env(path=path, updates=updates, apply=True, expected_source_sha256=fresh)
    assert result["verified"] and Path(result["backup_path"]).is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "UNRELATED=123" in path.read_text() and "# concurrent" in path.read_text()


def test_env_io_failure_reports_backup_and_preserves_original(tmp_path, monkeypatch):
    import src.application.config_env as mod
    path = tmp_path / "options-monitor.env"
    path.write_text("OM_FEISHU_BOT_APP_ID=old\n")
    original = path.read_bytes()
    atomic = mod._atomic
    def fail_target(target, data):
        if target == path:
            raise PermissionError("fixture cannot replace target")
        return atomic(target, data)
    monkeypatch.setattr(mod, "_atomic", fail_target)
    with pytest.raises(AgentToolError) as error:
        write_feature_env(path=path, updates={"OM_FEISHU_BOT_APP_ID": "new"}, apply=True,
                          expected_source_sha256=env_source_sha256(path))
    assert error.value.details["write_applied"] is False
    assert Path(error.value.details["backup_path"]).read_bytes() == original
    assert path.read_bytes() == original


@pytest.mark.parametrize("updates", [
    {"OM_FEISHU_BOT_APP_SECRET": "not-a-real-secret"},
    {"OM_FEISHU_BOT_APP_ID": "a\nEVIL=1"},
    {"PORTFOLIO_SERVICE_URL": "https://example.invalid"},
])
def test_env_rejects_secret_injection_and_remote_pm(tmp_path, updates):
    with pytest.raises((AgentToolError, ValueError)):
        write_feature_env(path=tmp_path / "options-monitor.env", updates=updates)
    assert list(tmp_path.iterdir()) == []


def test_duplicate_managed_env_and_symlink_are_rejected(tmp_path):
    path = tmp_path / "options-monitor.env"
    path.write_text("OM_FEISHU_BOT_APP_ID=a\nOM_FEISHU_BOT_APP_ID=b\n")
    with pytest.raises(AgentToolError, match="duplicate"):
        write_feature_env(path=path, updates={"OM_FEISHU_BOT_APP_ID": "c"})
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(AgentToolError, match="symbolic link"):
        env_source_sha256(alias)


def test_close_advice_preview_apply_and_cancel(tmp_path):
    path = source(tmp_path)
    before = path.read_bytes()
    namespace = args("close-advice", path, "--enabled", "false")
    preview = run(namespace)
    assert preview["status"] == "preview" and path.read_bytes() == before
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    applied = run(namespace)
    assert applied["status"] == "configured"
    assert json.loads((tmp_path / "config.us.json").read_text())["close_advice"]["enabled"] is False
    assert applied["result"]["verified_targets"]
    before = path.read_bytes()
    cancelled = run(args("close-advice", path, "--interactive"), input_is_tty=lambda: True,
                    input_fn=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cancelled["status"] == "cancelled" and path.read_bytes() == before


def test_form_keeps_current_enabled_default_and_rejects_invalid_answer(tmp_path):
    path = source(tmp_path)
    answers = iter(["", "n"])
    result = run(args("close-advice", path, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None)
    assert result["enabled"] is True and result["status"] == "cancelled"
    with pytest.raises(AgentToolError, match="INPUT_ERROR"):
        run(args("bot", path, "--interactive"), input_is_tty=lambda: True,
            input_fn=lambda _: "maybe", output_fn=lambda _: None)
    assert not (tmp_path / "config.us.json").exists()


def test_bot_profile_applies_generated_runtime_without_credentials_input(tmp_path):
    path = source(tmp_path)
    namespace = args("bot", path, "--enabled", "true", "--profile", "local", "--provider", "ollama",
                     "--model", "fixture-model", "--context-window-tokens", "24000", "--max-output-tokens", "2048")
    preview = run(namespace)
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    result = run(namespace, secret_runner=lambda _: pytest.fail("Ollama has no secret"))
    runtime = json.loads(Path(result["result"]["bot"]["output_config_path"]).read_text())
    assert runtime["bot"]["enabled"] is True
    assert "assistant" not in runtime
    assert preview["result"]["requested_setting"]["enabled"] is True
    assert result["enabled"] is True
    assert "bot_enabled" not in result
    assert runtime["bot"]["llm"]["model"] == "fixture-model"
    assert result["external_check"] == "not_performed" and not result["service_restarted"]


def test_feishu_noninteractive_separate_env_confirmation_and_route(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    namespace = args("channel", path, "--enabled", "true", "--provider", "feishu_app",
                     "--app-id", "cli_fixture", "--recipient", "ou_recipient", "--allowed-senders", "ou_sender")
    preview = run(namespace)
    assert not (tmp_path / "options-monitor.env").exists()
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["result"]["preview_sha256"]
    with pytest.raises(AgentToolError, match="expected-env-sha256"):
        run(namespace)
    namespace.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
    result = run(namespace, secret_runner=lambda _: pytest.fail("no TTY secret prompt"))
    assert result["credential"]["status"] == "not_checked"
    env = (tmp_path / "options-monitor.env").read_text()
    assert "ou_recipient" in env and "ou_sender" in env and "SECRET" not in env
    assert json.loads((tmp_path / "config.us.json").read_text())["notifications"]["provider"] == "feishu_app"


def test_interactive_reject_cancel_preserves_partial_env_save(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    before = path.read_bytes()
    answers = iter(["true", "feishu_app", "cli_fixture", "ou_fixture", "ou_fixture", "y", "n"])
    result = run(args("channel", path, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None)
    assert result["status"] == "cancelled" and len(result["completed_steps"]) == 1
    assert path.read_bytes() == before and (tmp_path / "options-monitor.env").exists()


def test_form_failure_reports_earlier_saved_env_step(tmp_path, monkeypatch):
    import src.interfaces.cli.feature_ops as mod
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    answers = iter(["true", "feishu_app", "cli_fixture", "ou_fixture", "ou_fixture", "y"])
    def reject_generation(**kwargs):
        raise ValueError("fixture invalid config")
    monkeypatch.setattr(mod, "publish_feature_document", reject_generation)
    with pytest.raises(AgentToolError) as error:
        run(args("channel", path, "--interactive"), input_is_tty=lambda: True,
            input_fn=lambda _: next(answers), output_fn=lambda _: None)
    assert error.value.details["completed_steps"][0]["write_applied"] is True
    assert (tmp_path / "options-monitor.env").exists()


def test_holdings_disable_delegates_without_probe_url(tmp_path):
    path = source(tmp_path)
    calls = []
    def setter(**kwargs):
        calls.append(kwargs)
        return {"write_applied": False, "preview_sha256": "fixture", "source_revision": {"before_sha256": "fixture"}}
    result = run(args("holdings", path, "--enabled", "false"), holdings_setter=setter)
    assert result["enabled"] is False and len(calls) == 1
    assert "service_url" not in calls[0]


def test_non_tty_missing_values_and_shadow_env_are_actionable(tmp_path, monkeypatch):
    path = source(tmp_path)
    with pytest.raises(AgentToolError, match="terminal"):
        run(args("bot", path), input_is_tty=lambda: False)
    monkeypatch.setenv("OM_ENV_FILE", str(tmp_path / "other.env"))
    with pytest.raises(AgentToolError, match="shadows"):
        run(args("channel", path, "--enabled", "true", "--provider", "feishu_app", "--app-id", "cli_fixture",
                 "--recipient", "ou_fixture", "--allowed-senders", "ou_fixture"))


def test_wechat_existing_binding_publishes_selected_runtime_and_inbound_scope(tmp_path):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    path = source(tmp_path)
    state_dir = tmp_path / "output_shared/state/channels/wechat_clawbot/default"
    store = WechatClawbotStateStore(state_dir)
    store.save_bindings({"bindings": {"ops": {"to_user_id": "fixture-user", "context_token": "fixture-only-token"}}})
    namespace = args("channel", path, "--enabled", "true", "--provider", "wechat_clawbot",
                     "--target", "wechat:default:ops", "--allowed-senders", "wechat:fixture-user")
    preview = run(namespace, wechat_connector=lambda **_: pytest.fail("existing binding needs no QR call"))
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["result"]["preview_sha256"]
    result = run(namespace)
    runtime = json.loads(Path(result["result"]["bot"]["output_config_path"]).read_text())
    assert runtime["inbound"]["wechat_clawbot"]["state_dir"] == str(state_dir)
    assert runtime["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:fixture-user"
    assert "fixture-only-token" not in json.dumps(result)


def test_wechat_connect_cancel_does_not_claim_no_effect(tmp_path):
    path = source(tmp_path)
    answers = iter(["true", "wechat_clawbot", "y"])
    def interrupted(**kwargs):
        raise KeyboardInterrupt
    result = run(args("channel", path, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None, wechat_connector=interrupted)
    assert result["status"] == "cancelled"
    assert result["completed_steps"][0]["write_applied"] is None


def test_linux_secret_permission_is_pending_without_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.interfaces.cli.feature_ops.sys.platform", "linux")
    path = source(tmp_path)
    answers = iter(["true", "fixture", "deepseek", "deepseek-chat", "24000", "2048", "y", "y", "set"])
    calls = []
    def secret(namespace):
        calls.append(namespace.logical_name)
        raise AgentToolError(code="CONFIG_ERROR", message="systemd store requires deployment administration")
    result = run(args("bot", path, "--interactive"), input_is_tty=lambda: True,
                 input_fn=lambda _: next(answers), output_fn=lambda _: None, secret_runner=secret)
    assert result["status"] == "configured" and result["credential"]["status"] == "pending"
    assert result["credential"]["next_step"] == 'sudo "$(command -v om)" secrets set llm.deepseek.api_key --backend systemd'
    assert calls == ["llm.deepseek.api_key"] and result["service_restarted"] is False


def test_holdings_enables_pm_in_one_confirmed_generation(tmp_path, monkeypatch):
    import src.application.config_yaml_holdings as mod
    path = source(tmp_path)
    observed = []
    def probe(config, **kwargs):
        observed.append(config["portfolio_management"]["enabled"])
        assert yaml.safe_load(path.read_text()).get("portfolio_management") is None
        return {"status": "ready_empty", "approved_non_futu_brokers": {"lx": []}}
    monkeypatch.setattr(mod, "_probe_holdings", probe)
    namespace = args("holdings", path, "--enabled", "true", "--enable-pm")
    preview = run(namespace)
    assert preview["status"] == "preview" and observed == [True]
    assert preview["result"]["pm_dependency"] == {"configured_before": False, "configured_after": True}
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["result"]["preview_sha256"]
    result = run(namespace)
    assert result["status"] == "configured"
    for configured in (yaml.safe_load(path.read_text()), json.loads((tmp_path / "config.us.json").read_text())):
        assert configured["portfolio_management"]["enabled"] is True
        assert configured["portfolio"]["holdings"]["enabled"] is True
    off = args("holdings", path, "--enabled", "false")
    preview = run(off)
    off.apply = off.confirm = True
    off.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    off.expected_preview_sha256 = preview["result"]["preview_sha256"]
    run(off)
    assert yaml.safe_load(path.read_text())["portfolio_management"]["enabled"] is True
    assert not yaml.safe_load(path.read_text())["portfolio"]["holdings"]["enabled"]


def test_bot_and_feishu_aliases_share_real_parsers():
    from src.interfaces.cli.main import parse_args
    bot = parse_args(["bot", "model", "use", "fixture", "--config-yaml", "/tmp/config.yaml"])
    legacy = parse_args(["assistant", "model", "use", "fixture", "--config-yaml", "/tmp/config.yaml"])
    assert bot.bot_control_command == legacy.bot_control_command == "model"
    assert bot.bot_model_command == legacy.bot_model_command == "use"
    feishu = parse_args(["channel", "feishu", "serve", "--check", "--no-local-env-file", "--env-file", "fixture.env"])
    inbound = parse_args(["inbound", "feishu-ws", "--check", "--no-local-env-file", "--env-file", "fixture.env"])
    assert feishu.inbound_command == inbound.inbound_command == "feishu-ws"
    assert feishu.check is True and feishu.env_file == inbound.env_file


def test_public_bot_configure_applies_runtime_and_restores_command_env(tmp_path, capsys, monkeypatch):
    from src.interfaces.cli.main import main
    path = source(tmp_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "other-instance"))
    argv = ["bot", "configure", "--config-yaml", str(path), "--enabled", "true",
            "--profile", "local", "--provider", "ollama", "--model", "fixture",
            "--context-window-tokens", "24000", "--max-output-tokens", "2048"]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview" and not (tmp_path / "config.us.json").exists()
    assert main([*argv, "--apply", "--confirm", "--expected-source-sha256",
                 preview["result"]["source_revision"]["before_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    runtime = json.loads(Path(result["result"]["bot"]["output_config_path"]).read_text())
    assert runtime["bot"]["llm"]["model"] == "fixture"
    import os
    assert os.environ["OM_RUNTIME_ROOT"] == str(tmp_path / "other-instance")


def test_feature_stale_during_prompts_does_not_overwrite(tmp_path):
    path = source(tmp_path)
    answers = iter(["false", "y"])
    def input_fn(_):
        value = next(answers)
        if value == "y":
            path.write_text(path.read_text() + "# another operator\n")
        return value
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run(args("close-advice", path, "--interactive"), input_is_tty=lambda: True,
            input_fn=input_fn, output_fn=lambda _: None)
    assert "# another operator" in path.read_text()
    assert not (tmp_path / "config.us.json").exists()


def test_channel_enable_resume_effect_requires_bound_preview_before_any_write(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    original = path.read_bytes()
    namespace = args("channel", path, "--enabled", "true", "--provider", "feishu_app",
                     "--app-id", "cli_fixture", "--recipient", "ou_fixture", "--allowed-senders", "ou_fixture")
    preview = run(namespace)
    effect = preview["result"]["notification_resume"]
    assert effect["includes_accumulated_while_disabled"] is True
    assert effect["confirmed_receipts"] == "not_replayed"
    assert run(namespace)["result"]["preview_sha256"] == preview["result"]["preview_sha256"]
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
    with pytest.raises(AgentToolError, match="expected-preview-sha256"):
        run(namespace)
    namespace.expected_preview_sha256 = "wrong-preview"
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run(namespace)
    assert path.read_bytes() == original
    assert not (tmp_path / "options-monitor.env").exists()
    assert not (tmp_path / "config.us.json").exists()
    namespace.expected_preview_sha256 = preview["result"]["preview_sha256"]
    result = run(namespace)
    assert result["status"] == "configured"
    assert result["result"]["notification_resume"] == effect


def test_channel_interactive_resume_notice_is_in_confirmation_and_cancel_keeps_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    original = path.read_bytes()
    answers = iter(["true", "feishu_app", "cli_fixture", "ou_fixture", "ou_fixture", "y", "n"])
    prompts, previews = [], []
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    result = run(args("channel", path, "--interactive"), input_is_tty=lambda: True,
                 input_fn=answer, output_fn=previews.append)
    assert "关闭期间积累" in prompts[-1] and "不会重复" in prompts[-1]
    assert any("notification_resume" in item for item in previews)
    assert result["status"] == "cancelled" and path.read_bytes() == original
    assert result["completed_steps"][0]["write_applied"] is True  # separately confirmed ordinary settings


def test_enabled_channel_update_and_disabling_do_not_require_resume_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["notifications"] = {"enabled": True, "provider": "feishu_app"}
    path.write_text(yaml.safe_dump(doc))
    for enabled in ("true", "false"):
        namespace = args("channel", path, "--enabled", enabled, "--provider", "feishu_app",
                         "--app-id", "cli_fixture", "--recipient", "ou_fixture", "--allowed-senders", "ou_fixture")
        preview = run(namespace)
        assert "notification_resume" not in preview["result"]
        namespace.apply = namespace.confirm = True
        namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
        if "env" in preview:
            namespace.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
        assert run(namespace)["status"] == "configured"
