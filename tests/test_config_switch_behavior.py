import json
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_features import feature_document
from src.application.settings.effective import resolve_write_gates
from tests.test_cli_feature_configuration import args, run, source


@pytest.mark.parametrize("enabled", [False, True])
def test_bot_configure_changes_one_switch(enabled):
    original = {"bot": {"enabled": not enabled, "read_markets": ["us"]}}
    changed = feature_document(original, feature="bot", enabled=enabled)
    assert changed["bot"] == {"enabled": enabled, "read_markets": ["us"]}
    assert original["bot"]["enabled"] is not enabled
    assert "assistant" not in changed


@pytest.mark.parametrize("entry", ["local", "channel"])
def test_real_model_entry_stops_before_credentials_or_provider(tmp_path, monkeypatch, entry):
    from src.application.bot import local_harness, model_config
    from tests.test_bot_phase1 import _request
    path = tmp_path / "config.bot.json"
    path.write_text(json.dumps({"bot": {'enabled': False, 'llm': {'provider': 'ollama', 'model': 'fixture', 'base_url': 'http://127.0.0.1:11434/v1', 'context_window_tokens': 24000, 'max_output_tokens': 2048}}}))
    def forbidden(*args, **kwargs):
        pytest.fail("disabled Bot must not resolve credentials or run model")
    monkeypatch.setattr(local_harness, "_resolve_model", forbidden)
    monkeypatch.setattr(local_harness, "_resolve_model_api_key", forbidden)
    monkeypatch.setattr(local_harness, "run_contract", forbidden)
    assert model_config.load_bot_llm_config(config_path=path, require_config=True) == (None, None)
    result = local_harness.run_local_request(_request("inspect", environment=entry), reference_year=2026, bot_config_path=str(path))
    assert result.status == "disabled" and not result.ok
    assert result.error["reason"] == "bot_disabled"


def test_bot_master_disabled_stops_deterministic_entry_before_audit(tmp_path, monkeypatch):
    from src.application.bot.control import runtime
    from src.application.bot.control.settings import BotSettings
    from tests.test_bot_runtime import _request
    monkeypatch.setattr(runtime, "_run_bot_turn_response", lambda *args, **kwargs: pytest.fail("disabled entry"))
    result = runtime.handle_bot_turn(_request(tmp_path, "/help"), settings=BotSettings(enabled=False))
    assert result.status == "disabled"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["preflight", "cancel", "stale"])
def test_holdings_failure_or_cancel_leaves_pm_source_and_connection_unchanged(tmp_path, monkeypatch, failure):
    from src.application import config_yaml_holdings as mod
    path = source(tmp_path)
    original = path.read_bytes()
    def probe(config, **kwargs):
        assert config["portfolio_management"]["enabled"]
        if failure == "preflight":
            raise ValueError("fixture unavailable")
        return {"status": "ready_empty", "approved_non_futu_brokers": {"lx": []}}
    monkeypatch.setattr(mod, "_probe_holdings", probe)
    if failure in {"preflight", "cancel"}:
        answers = iter(["true", "http://127.0.0.1:8765", "n"])
        result = run(args("holdings", path, "--interactive"), input_is_tty=lambda: True,
                     input_fn=lambda _: next(answers), output_fn=lambda _: None)
        assert result["status"] == ("preflight_failed" if failure == "preflight" else "cancelled")
    else:
        with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
            from src.application.config_authoring_transaction import config_source_sha256
            run(args("holdings", path, "--enabled", "true", "--enable-pm", "--service-url", "http://127.0.0.1:8765",
                     "--apply", "--confirm", "--expected-source-sha256", config_source_sha256(path),
                     "--expected-preview-sha256", "wrong"))
    assert path.read_bytes() == original
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "options-monitor.env").exists()


def test_holdings_publish_failure_rolls_back_both_flags(tmp_path, monkeypatch):
    from src.application import config_yaml_holdings as mod, config_authoring_transaction as transaction
    path = source(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(mod, "_probe_holdings", lambda *args, **kwargs: {
        "status": "ready_empty", "approved_non_futu_brokers": {"lx": []}})
    namespace = args("holdings", path, "--enabled", "true", "--enable-pm")
    preview = run(namespace)["result"]
    atomic = transaction._atomic_write_bytes
    def fail(target, payload):
        if Path(target) == path:
            raise OSError("fixture failure")
        return atomic(target, payload)
    monkeypatch.setattr(transaction, "_atomic_write_bytes", fail)
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["preview_sha256"]
    with pytest.raises(AgentToolError):
        run(namespace)
    assert path.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()


@pytest.mark.parametrize("literal", ["1", "true", "yes", "y", "on", "false", "0", "", "invalid"])
def test_permission_execution_and_doctor_use_identical_interpretation(tmp_path, monkeypatch, literal):
    from src.application.settings import diagnose_effective_settings
    from src.application.bot.control.operation_policy import load_operation_policy_from_env, enforce_model_write_allowed
    from src.application.agent_tool_config import write_tools_enabled
    env = {"OM_INBOUND_OPERATIONS_ENABLED": "0", "OM_INBOUND_MODEL_WRITE_ENABLED": literal,
           "OM_INBOUND_TRADE_WRITE_ENABLED": literal, "OM_AGENT_ENABLE_WRITE_TOOLS": literal,
           "OM_INBOUND_ADMIN_OPEN_IDS": "feishu:fixture"}
    path = tmp_path / "options-monitor.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n")
    monkeypatch.setenv("OM_ENV_FILE", str(path))
    policy = load_operation_policy_from_env()
    doctor = diagnose_effective_settings(environ={}, env_file=path, include_local_env_file=False)
    gates = next(item["value"] for item in doctor["checks"] if item["name"] == "write_gates")
    assert gates == resolve_write_gates(env)
    assert gates["model_write_enabled"] == policy.model_write_enabled
    assert gates["trade_write_enabled"] == policy.trade_write_enabled
    assert gates["agent_write_tools_enabled"] == write_tools_enabled()
    with pytest.raises(AgentToolError, match="operations are disabled"):
        enforce_model_write_allowed(channel="feishu", sender_id="fixture", policy=policy)
    # Even with a scope enabled, master off remains a kill switch.
    assert not policy.operations_enabled


def test_model_gate_is_visible_in_inspect_and_explain():
    from src.application.settings import inspect_effective_settings, explain_effective_setting
    env = {"OM_INBOUND_MODEL_WRITE_ENABLED": "y"}
    inspected = inspect_effective_settings(environ=env, include_local_env_file=False)
    assert "OM_INBOUND_MODEL_WRITE_ENABLED" in json.dumps(inspected)
    explained = explain_effective_setting("inbound.model_write_enabled", environ=env, include_local_env_file=False)
    assert "y" in json.dumps(explained)


def test_holdings_yaml_failure_reports_previously_confirmed_connection(tmp_path, monkeypatch):
    from src.application import config_yaml_holdings as mod, config_authoring_transaction as transaction
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    path = source(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(mod, "_probe_holdings", lambda *args, **kwargs: {
        "status": "ready_empty", "approved_non_futu_brokers": {"lx": []}})
    namespace = args("holdings", path, "--enabled", "true", "--enable-pm", "--service-url", "http://127.0.0.1:8765")
    preview = run(namespace)
    namespace.apply = namespace.confirm = True
    namespace.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    namespace.expected_preview_sha256 = preview["result"]["preview_sha256"]
    namespace.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
    atomic = transaction._atomic_write_bytes
    def fail(target, payload):
        if Path(target) == path:
            raise OSError("fixture YAML failure after connection save")
        return atomic(target, payload)
    monkeypatch.setattr(transaction, "_atomic_write_bytes", fail)
    with pytest.raises(AgentToolError) as error:
        run(namespace)
    assert path.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()
    assert "http://127.0.0.1:8765" in (tmp_path / "options-monitor.env").read_text()
    assert error.value.details["completed_steps"][0]["write_applied"] is True


def test_bot_off_blocks_inbound_deterministic_commands(tmp_path, monkeypatch):
    from src.application.bot.control import inbound_service
    from src.application.bot.control.runtime import handle_bot_turn
    from src.application.bot.control.settings import BotSettings
    from src.application.agent_tool_contracts import build_response
    from tests.test_bot_runtime import _request
    calls = []
    monkeypatch.setattr(inbound_service, "run_channel_request", lambda **kwargs: pytest.fail("must not run model"))
    def execute(name, payload):
        calls.append(name)
        return build_response(tool_name=name, ok=True, data={"status": "ok"})
    result = handle_bot_turn(_request(tmp_path, "/status"), execute_tool_fn=execute,
        allowed_senders="u_runtime", settings=BotSettings(enabled=False))
    assert not result.ok and result.status == "disabled" and not calls


def test_diagnostics_reports_single_disabled_bot(tmp_path):
    from tests.test_bot_diagnostics import _bot_config, _check_llm, _llm
    cfg = _bot_config(llm=_llm(provider="ollama", model="fixture", base_url="http://127.0.0.1:11434/v1"))
    cfg["bot"]["enabled"] = False
    result = _check_llm(tmp_path, cfg)
    assert result["summary"]["configured_enabled"] is False
    assert result["summary"]["bot_enabled"] is False
    assert result["summary"]["readiness_scope"] == "configuration"
    assert result["summary"]["activity_observed"] is None


@pytest.mark.parametrize("enabled", [None, False, True])
def test_service_credentials_follow_notification_opt_in(enabled):
    from src.application.service_deploy import _systemd_secret_bindings
    from src.application.secret_store import FEISHU_BOT_APP_SECRET
    notifications = {"provider": "feishu_app"}
    if enabled is not None:
        notifications["enabled"] = enabled
    bindings = _systemd_secret_bindings(service_names=["options-monitor-tick-us.service"],
        bot_credential_name=None, feature_configs={"us": {"notifications": notifications}})
    assert (FEISHU_BOT_APP_SECRET in bindings.get("options-monitor-tick-us.service", ())) is (enabled is True)


def test_inline_model_override_cannot_bypass_missing_bot_config(tmp_path, monkeypatch):
    from src.application.bot import local_harness, model_config
    from tests.test_bot_phase1 import _request
    monkeypatch.setattr(model_config, "_bot_config_path", lambda **kwargs: tmp_path / "missing.json")
    monkeypatch.setattr(local_harness, "_resolve_model", lambda **kwargs: pytest.fail("must not load a model"))
    result = local_harness.run_local_request(_request("inspect"), reference_year=2026,
        model_config_json=json.dumps({"provider": "ollama", "model": "fixture"}))
    assert not result.ok and result.error["reason"] == "bot_config_not_found"
