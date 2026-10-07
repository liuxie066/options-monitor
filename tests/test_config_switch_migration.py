from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_switch_migration import migrate_switch_document, migrate_yaml_switches
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.notification_delivery_route import resolve_notification_delivery_route

REPO = Path(__file__).resolve().parents[1]


def legacy():
    return {"accounts": {"lx": {"type": "futu", "futu_account_id": "12345678"}},
            "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"],
                                "features": {"wheel": {"enabled": False, "accounts": []}}},
                        "hk": {"accounts": ["lx"], "symbols": ["0700.HK"], "notifications": {"enabled": False}}},
            "assistant": {"bot": {"enabled": False, "tool_loading_mode": "directory", "toolsets": {"portfolio": True}}},
            "trade_intake": {"combo_reconciliation": {"default_mode": "off", "accounts": {"lx": "observe"}}},
            "notifications": {"daily_brief": {"enabled": False}}}


def test_migration_preserves_intent_without_activation_or_account_guessing():
    old = legacy()
    snapshot = deepcopy(old)
    new, changes = migrate_switch_document(old)
    assert old == snapshot
    assert new["notifications"]["enabled"] is True
    assert new["markets"]["hk"]["notifications"]["enabled"] is False
    assert "enabled" not in new["markets"]["us"]["features"]["wheel"]
    assert "activation_by_account" not in new["markets"]["us"]["features"]["wheel"]
    assert new["bot"] == {"enabled": False}
    assert new["trade_intake"]["combo_reconciliation"] == {"accounts": {"lx": "observe"}}
    assert len(changes) == 7
    assert migrate_switch_document(new) == (new, [])


@pytest.mark.parametrize("patch", [
    {"assistant": {"copilot": {"enabled": False}}},
    {"accounts": {"lx": {"holdings_account": "other"}}},
    {"notifications": {"enabled": "false"}},
    {"trade_intake": {"combo_reconciliation": {"default_mode": "auto"}}},
    {"assistant": {"bot": {"toolsets": {"unknown": True}}}},
])
def test_migration_rejects_ambiguous_values(patch):
    old = legacy(); old.update(patch)
    with pytest.raises(AgentToolError, match="CONFIG_MIGRATION_CONFLICT"):
        migrate_switch_document(old)


def test_migration_preview_apply_readback_and_stale_retry(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(legacy(), sort_keys=False))
    old = source.read_bytes()
    preview = migrate_yaml_switches(repo_root=REPO, config_path=source)
    assert source.read_bytes() == old
    assert not (tmp_path / "config.us.json").exists()
    args = dict(repo_root=REPO, config_path=source, apply=True, confirm=True,
                expected_source_sha256=preview["source_revision"]["before_sha256"],
                expected_preview_sha256=preview["preview_sha256"])
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        migrate_yaml_switches(**{**args, "runtime_root": tmp_path / "other"})
    assert source.read_bytes() == old
    result = migrate_yaml_switches(**args)
    assert result["write_applied"] and len(result["verified_targets"]) == 4
    assert json.loads((tmp_path / "config.us.json").read_text())["notifications"]["enabled"] is True
    assert json.loads((tmp_path / "config.hk.json").read_text())["notifications"]["enabled"] is False
    bot_snapshot = json.loads((tmp_path / "resolved" / "config.bot.json").read_text())
    assert bot_snapshot["bot"]["enabled"] is False
    assert "assistant" not in bot_snapshot and "bot" not in bot_snapshot["bot"]
    assert Path(result["backup_path"]).read_bytes() == old
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        migrate_yaml_switches(**args)


def test_new_configuration_and_route_require_notification_opt_in(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump({"accounts": legacy()["accounts"], "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}}}))
    config, _ = resolve_yaml_runtime_config(repo_root=REPO, market="us", config_path=source)
    assert config["notifications"]["enabled"] is False
    def must_not_resolve(**kwargs):
        raise AssertionError("disabled route must not resolve a delivery target")
    for candidate in ({}, config, {"notifications": {"enabled": False}}):
        assert resolve_notification_delivery_route(config=candidate, route_resolver=must_not_resolve)["enabled"] is False


def test_cli_migration_preview_and_confirmed_apply(tmp_path):
    import subprocess
    import sys
    source = tmp_path / "config.yaml"
    old = legacy(); old["notifications"]["enabled"] = False
    source.write_text(yaml.safe_dump(old))
    def run(*args):
        result = subprocess.run([sys.executable, "-m", "src.interfaces.cli.main", "config", "migrate-switches",
                                 "--config-yaml", str(source), "--runtime-root", str(tmp_path), *args],
                                cwd=REPO, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    preview = run()
    assert preview["dry_run"] and not preview["write_applied"]
    applied = run("--apply", "--confirm", "--expected-source-sha256", preview["source_revision"]["before_sha256"],
                  "--expected-preview-sha256", preview["preview_sha256"])
    assert applied["write_applied"]
    assert yaml.safe_load(source.read_text())["notifications"]["enabled"] is False


def test_failed_generation_publish_restores_legacy_source(tmp_path, monkeypatch):
    from src.application import config_authoring_transaction as transaction
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(legacy()))
    before = source.read_bytes()
    preview = migrate_yaml_switches(repo_root=REPO, config_path=source)
    original = transaction._atomic_write_bytes
    def fail_once(path, payload):
        if Path(path) == source:
            raise OSError("injected publication failure")
        return original(path, payload)
    monkeypatch.setattr(transaction, "_atomic_write_bytes", fail_once)
    with pytest.raises(AgentToolError):
        migrate_yaml_switches(repo_root=REPO, config_path=source, apply=True, confirm=True,
                              expected_source_sha256=preview["source_revision"]["before_sha256"],
                              expected_preview_sha256=preview["preview_sha256"])
    assert source.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()


@pytest.mark.parametrize("master", [None, False, True])
@pytest.mark.parametrize("nested", [None, False, True])
def test_bot_migration_collapses_old_switches_without_enabling(master, nested):
    old = {"assistant": {"bot": {"read_markets": ["us", "hk"]},
                         "active_model": "fixture", "models": {"fixture": {"provider": "ollama", "model": "fixture"}}},
           "notifications": {"enabled": False}}
    if master is not None:
        old["assistant"]["enabled"] = master
    if nested is not None:
        old["assistant"]["bot"]["enabled"] = nested
    migrated, _ = migrate_switch_document(old)
    assert "assistant" not in migrated
    assert migrated["bot"]["enabled"] is (master is not False and nested is True)
    assert migrated["bot"]["read_markets"] == ["us", "hk"]
    assert migrated["bot"]["models"] == old["assistant"]["models"]
    assert migrated["bot"]["active_model"] == "fixture"
    assert "bot" not in migrated["bot"]
    assert migrate_switch_document(migrated) == (migrated, [])


@pytest.mark.parametrize("source", [
    {"assistant": {}, "bot": {}},
    {"assistant": {"enabled": "false"}},
    {"assistant": {"bot": {"enabled": None}}},
    {"assistant": {"bot": {"unknown": False}}},
])
def test_bot_migration_rejects_duplicate_or_ambiguous_configuration(source):
    before = deepcopy(source)
    with pytest.raises(AgentToolError, match="CONFIG_MIGRATION_CONFLICT"):
        migrate_switch_document(source)
    assert source == before
