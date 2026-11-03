from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.application.channels.plugins import channel_activity_enabled
from src.application.config_validator import validate_bot_config, validate_config
from src.application.config_yaml import resolve_yaml_bot_config, resolve_yaml_runtime_config
from src.application.notification_delivery_route import resolve_notification_delivery_routes


def test_explicit_controls_default_closed_and_separate_receive_notify():
    cfg = {"channels": {"feishu": {"enabled": True, "receive_enabled": True}}}
    assert channel_activity_enabled(cfg, "feishu_app", "receive")
    assert not channel_activity_enabled(cfg, "feishu", "notify")
    assert not channel_activity_enabled(cfg, "wechat", "receive")
    cfg["channels"]["feishu"]["enabled"] = False
    assert not channel_activity_enabled(cfg, "feishu", "receive")


@pytest.mark.parametrize("channels", [None, [], {"unknown": {}}, {"feishu": []},
    {"feishu": {"enabled": "false"}}, {"feishu": {"receive": True}}])
def test_bot_rejects_invalid_channel_controls(channels):
    with pytest.raises(SystemExit):
        validate_bot_config({"channels": channels})


def test_explicit_routes_bind_account_and_actual_feishu_target(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_sender")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_legacy")
    cfg = {"channels": {"feishu": {"enabled": True, "notify_enabled": True}},
           "notifications": {"enabled": True, "routes": [
               {"id": "a", "provider": "feishu_app", "target": "ou_a", "accounts": ["alpha"]},
               {"id": "b", "provider": "feishu_app", "target": "ou_b", "accounts": ["beta"]}]}}
    a = resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path)
    b = resolve_notification_delivery_routes(config=cfg, account="beta", base=tmp_path)
    assert [r["target"] for r in a] == ["ou_a"]
    assert [r["target"] for r in b] == ["ou_b"]
    assert a[0]["identity_key"] != b[0]["identity_key"]
    cfg["notifications"]["routes"] = []
    assert resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path) == []


def test_global_stop_and_channel_stop_do_not_resolve_destinations(tmp_path):
    cfg = {"channels": {}, "notifications": {"enabled": True, "routes": [
        {"id": "a", "provider": "feishu_app", "target": "ou_a", "accounts": ["alpha"]}]}}
    assert resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path) == []
    cfg["channels"] = {"feishu": {"enabled": True, "notify_enabled": True}}
    cfg["notifications"]["enabled"] = False
    assert resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path) == []


def test_generated_bot_and_market_share_controls(tmp_path):
    from tests.test_config_yaml import _minimal_yaml, REPO_ROOT
    path = tmp_path / "config.yaml"
    doc = yaml.safe_load(_minimal_yaml())
    doc["channels"] = {"feishu": {"enabled": True, "receive_enabled": True, "notify_enabled": False}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bot, _ = resolve_yaml_bot_config(repo_root=REPO_ROOT, config_path=path)
    market, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, config_path=path, market="us")
    assert bot["channels"] == market["channels"] == doc["channels"]


def test_generation_selects_routes_for_market_accounts(tmp_path):
    from tests.test_config_yaml import _minimal_yaml, REPO_ROOT
    path = tmp_path / "config.yaml"
    doc = yaml.safe_load(_minimal_yaml())
    doc["markets"]["us"]["accounts"] = ["lx"]
    doc["notifications"] = {"enabled": False, "routes": [
        {"id": "a", "provider": "feishu_app", "target": "ou_a", "accounts": ["lx", "sy"]},
        {"id": "b", "provider": "feishu_app", "target": "ou_b", "accounts": ["sy"]}]}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    market, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, config_path=path, market="us")
    assert market["notifications"]["routes"] == [
        {"id": "a", "provider": "feishu_app", "target": "ou_a", "accounts": ["lx"]}]


@pytest.mark.parametrize("change", [
    {"accounts": ["unknown"]}, {"accounts": ["lx", "lx"]}, {"id": " "},
    {"provider": "webhook"}, {"target": ""}, {"extra": True}])
def test_invalid_notification_routes_rejected(change):
    from src.application.config_defaults import default_config
    cfg = deepcopy(default_config())
    cfg["accounts"] = ["lx"]
    cfg["notifications"] = {"enabled": False, "routes": [
        {"id": "a", "provider": "feishu_app", "target": "ou_a", "accounts": ["lx"], **change}]}
    with pytest.raises(SystemExit):
        validate_config(cfg)
