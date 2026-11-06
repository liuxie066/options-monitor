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


@pytest.mark.parametrize("scope", [(), ("defaults",), ("markets", "us")], ids=["root", "system-defaults", "market"])
def test_display_budget_migration_preserves_new_keys_and_standalone_close_advice(scope):
    old = {"notifications": {"enabled": False}, "close_advice": {"max_items_per_account": 2}}
    parent = old
    for key in scope:
        parent = parent.setdefault(key, {})
    new_limits = {
        "max_total_items": 19,
        "max_close_items": 0,
        "max_roll_up_items": 7,
        "max_roll_down_items": 2,
        "max_candidates_per_strategy": 4,
        "max_rejection_reasons": 3,
    }
    parent.setdefault("notifications", {})["daily_brief"] = {
        **new_limits, "max_items_per_account": 0, "max_actions_per_priority": 20,
    }
    snapshot = deepcopy(old)
    new, changes = migrate_switch_document(old)
    assert old == snapshot
    migrated_parent = new
    for key in scope:
        migrated_parent = migrated_parent[key]
    assert migrated_parent["notifications"]["daily_brief"] == new_limits
    prefix = ".".join((*scope, "notifications", "daily_brief"))
    assert changes == [
        {"path": f"{prefix}.max_items_per_account", "before": 0, "action": "remove_inert_field"},
        {"path": f"{prefix}.max_actions_per_priority", "before": 20, "action": "remove_inert_field"},
    ]
    assert new["close_advice"] == {"max_items_per_account": 2}
    assert migrate_switch_document(new) == (new, [])


@pytest.mark.parametrize("scope", [(), ("defaults",), ("markets", "hk")], ids=["root", "system-defaults", "market"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_items_per_account", True),
        ("max_items_per_account", False),
        ("max_items_per_account", -1),
        ("max_items_per_account", 21),
        ("max_items_per_account", 5.0),
        ("max_items_per_account", None),
        ("max_items_per_account", "5"),
        ("max_actions_per_priority", True),
        ("max_actions_per_priority", False),
        ("max_actions_per_priority", 0),
        ("max_actions_per_priority", -1),
        ("max_actions_per_priority", 21),
        ("max_actions_per_priority", 5.0),
        ("max_actions_per_priority", None),
        ("max_actions_per_priority", "5"),
    ],
)
def test_display_budget_migration_rejects_invalid_legacy_values_without_mutation(scope, key, value):
    old = {"notifications": {"enabled": False}}
    parent = old
    for name in scope:
        parent = parent.setdefault(name, {})
    parent.setdefault("notifications", {})["daily_brief"] = {key: value, "max_close_items": 7}
    snapshot = deepcopy(old)
    with pytest.raises(AgentToolError) as caught:
        migrate_switch_document(old)
    assert caught.value.code == "CONFIG_MIGRATION_CONFLICT"
    assert ".".join((*scope, "notifications", "daily_brief", key)) in str(caught.value)
    assert old == snapshot


@pytest.mark.parametrize("account_limit", [0, 5, 20])
@pytest.mark.parametrize("action_limit", [1, 5, 20])
def test_display_budget_migration_accepts_legacy_integer_boundaries(account_limit, action_limit):
    old = {"notifications": {"enabled": False, "daily_brief": {
        "max_items_per_account": account_limit, "max_actions_per_priority": action_limit,
    }}}
    migrated, changes = migrate_switch_document(old)
    assert migrated["notifications"]["daily_brief"] == {}
    assert [change["before"] for change in changes] == [account_limit, action_limit]
    assert "max_close_items" not in migrated["notifications"]["daily_brief"]


def test_display_budget_migration_preview_apply_and_market_readback(tmp_path):
    old = legacy()
    new_limits = {"max_total_items": 24, "max_close_items": 0,
                  "max_roll_up_items": 8, "max_roll_down_items": 2,
                  "max_candidates_per_strategy": 6, "max_rejection_reasons": 4}
    old["notifications"] = {"enabled": False, "daily_brief": {
        **new_limits, "max_items_per_account": 2, "max_actions_per_priority": 1,
    }}
    old["close_advice"] = {"max_items_per_account": 1}
    old["markets"]["us"]["notifications"] = {"daily_brief": {
        "max_close_items": 9, "max_items_per_account": 0, "max_actions_per_priority": 20,
    }}
    old["markets"]["hk"]["notifications"]["daily_brief"] = {
        "max_roll_down_items": 0, "max_items_per_account": 7,
    }
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(old, sort_keys=False))
    before = source.read_bytes()
    preview = migrate_yaml_switches(repo_root=REPO, config_path=source)
    paths = {change["path"] for change in preview["changes"]}
    assert {
        "notifications.daily_brief.max_items_per_account",
        "notifications.daily_brief.max_actions_per_priority",
        "markets.us.notifications.daily_brief.max_items_per_account",
        "markets.us.notifications.daily_brief.max_actions_per_priority",
        "markets.hk.notifications.daily_brief.max_items_per_account",
    } <= paths
    assert source.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert not (tmp_path / "resolved").exists()
    args = dict(repo_root=REPO, config_path=source, apply=True, confirm=True,
                expected_source_sha256=preview["source_revision"]["before_sha256"],
                expected_preview_sha256=preview["preview_sha256"])
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        migrate_yaml_switches(**{**args, "expected_preview_sha256": "0" * 64})
    assert source.read_bytes() == before
    result = migrate_yaml_switches(**args)
    assert result["write_applied"] and len(result["verified_targets"]) == 4
    assert Path(result["backup_path"]).read_bytes() == before
    migrated = yaml.safe_load(source.read_text())
    assert migrated["notifications"]["daily_brief"] == new_limits
    assert migrated["close_advice"] == {"max_items_per_account": 1}
    for market, overrides in (("us", {"max_close_items": 9}), ("hk", {"max_roll_down_items": 0})):
        runtime = json.loads((tmp_path / f"config.{market}.json").read_text())
        assert runtime["notifications"]["daily_brief"] == {**new_limits, **overrides}
        assert runtime["close_advice"]["max_items_per_account"] == 1
    assert json.loads((tmp_path / "resolved" / "config.bot.json").read_text())["bot"]["enabled"] is False
    assert migrated["notifications"]["enabled"] is False
    again = migrate_yaml_switches(repo_root=REPO, config_path=source)
    assert again["changes"] == [] and not again["write_applied"]
    assert source.read_text() == yaml.safe_dump(migrated, sort_keys=False)
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
    old["notifications"]["daily_brief"].update(
        max_items_per_account=0, max_actions_per_priority=5,
        max_close_items=8, max_roll_up_items=0, max_roll_down_items=2, max_total_items=17,
    )
    source.write_text(yaml.safe_dump(old))
    before = source.read_bytes()
    def run(*args):
        result = subprocess.run([sys.executable, "-m", "src.interfaces.cli.main", "config", "migrate-switches",
                                 "--config-yaml", str(source), "--runtime-root", str(tmp_path), *args],
                                cwd=REPO, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    preview = run()
    assert preview["dry_run"] and not preview["write_applied"]
    assert source.read_bytes() == before
    assert {"notifications.daily_brief.max_items_per_account",
            "notifications.daily_brief.max_actions_per_priority"} <= {
        change["path"] for change in preview["changes"]
    }
    applied = run("--apply", "--confirm", "--expected-source-sha256", preview["source_revision"]["before_sha256"],
                  "--expected-preview-sha256", preview["preview_sha256"])
    assert applied["write_applied"]
    notifications = yaml.safe_load(source.read_text())["notifications"]
    assert notifications["enabled"] is False
    assert notifications["daily_brief"] == {
        "max_close_items": 8, "max_roll_up_items": 0, "max_roll_down_items": 2, "max_total_items": 17,
    }
    assert run()["changes"] == []


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
