"""Optional bot/API service units: default-empty, explicit and round-trippable."""
from __future__ import annotations

import json

import pytest

from src.application.service_deploy import (
    managed_systemd_unit_names,
    render_service_bundle,
    render_service_bundle_from_profile,
)

BOT_UNIT = "options-monitor-group-query-bot-gq-main.service"
API_UNIT = "options-monitor-group-query-api-gq-main.service"
FEISHU_SECRET = "group_query_bot.feishu.app_secret"
LLM_SECRET = "group_query_bot.llm.api_key"
API_SECRET = "group_query_bot.query_api.token"


def _render(tmp_path, *, target="systemd", **options):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    runtime = tmp_path / "runtime"
    base = dict(target=target, repo_root=repo, runtime_root=runtime,
                accounts=["fixture"], markets=["us"],
                use_default_deploy_user=False,
                deploy_home=tmp_path / "home")
    base.update(options)
    return render_service_bundle(**base)


def _instances(tmp_path):
    return [{"instance_id": "gq-main",
             "bot_config": str(tmp_path / "bot.json"),
             "api_config": str(tmp_path / "api.json")}]


def _files(bundle):
    return {item["relative_path"]: item for item in bundle["files"]}


def _profile(bundle):
    return json.loads(_files(bundle)["service.profile.json"]["content"])


def test_default_render_has_no_group_query_units(tmp_path):
    bundle = _render(tmp_path)

    assert not any("group-query" in path for path in _files(bundle))
    profile = _profile(bundle)
    assert "group_query_bot_instances" not in profile
    assert [entry["name"] for entry in profile["services"] if "group-query" in entry["name"]] == []
    assert not any("group-query" in unit
                   for unit in managed_systemd_unit_names(accounts=["fixture"]))


def test_configured_instance_renders_both_roles_and_binds_credentials(tmp_path):
    instances = _instances(tmp_path)
    bundle = _render(
        tmp_path, group_query_bot_instances=instances,
        include_secret_credentials=True,
        secret_credential_store_root=tmp_path / "credential-store")

    files = _files(bundle)
    bot = files[f"systemd/{BOT_UNIT}"]
    api = files[f"systemd/{API_UNIT}"]
    assert bot["kind"] == "systemd_service"
    assert api["kind"] == "systemd_service"
    assert bot["install_path"] == f"/etc/systemd/system/{BOT_UNIT}"
    assert bot["content"].count("group-query-bot serve") == 1
    assert str(instances[0]["bot_config"]) in bot["content"]
    assert "--confirm" in bot["content"]
    assert api["content"].count("group-query-bot api-serve") == 1
    assert str(instances[0]["api_config"]) in api["content"]

    profile = _profile(bundle)
    assert profile["group_query_bot_instances"] == instances
    names = {entry["name"] for entry in profile["services"]}
    assert {BOT_UNIT, API_UNIT} <= names
    credentials = profile["secret_credentials"]["service_credentials"]
    assert set(credentials[BOT_UNIT]) == {FEISHU_SECRET, LLM_SECRET, API_SECRET}
    assert credentials[API_UNIT] == [API_SECRET]

    managed = managed_systemd_unit_names(
        accounts=["fixture"], group_query_bot_instance_ids=["gq-main"])
    assert {BOT_UNIT, API_UNIT} <= managed


def test_profile_round_trip_recreates_group_query_units(tmp_path):
    bundle = _render(tmp_path, group_query_bot_instances=_instances(tmp_path))
    profile = _profile(bundle)

    rebuilt = render_service_bundle_from_profile(
        profile, provider="systemd", repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime")
    rebuilt_files = _files(rebuilt)
    assert f"systemd/{BOT_UNIT}" in rebuilt_files
    assert f"systemd/{API_UNIT}" in rebuilt_files
    assert "group-query-bot serve" in rebuilt_files[f"systemd/{BOT_UNIT}"]["content"]
    assert "group-query-bot api-serve" in rebuilt_files[f"systemd/{API_UNIT}"]["content"]


def test_launchd_instance_renders_both_labels(tmp_path):
    bundle = _render(tmp_path, target="launchd",
                     group_query_bot_instances=_instances(tmp_path))

    files = _files(bundle)
    bot_label = "com.options-monitor.options-monitor-group-query-bot-gq-main"
    api_label = "com.options-monitor.options-monitor-group-query-api-gq-main"
    bot = files[f"launchd/{bot_label}.plist"]
    api = files[f"launchd/{api_label}.plist"]
    assert bot["install_path"] == f"~/Library/LaunchAgents/{bot_label}.plist"
    assert "<string>group-query-bot</string>" in bot["content"]
    assert "<string>serve</string>" in bot["content"]
    assert "<string>--confirm</string>" in bot["content"]
    assert f"<string>{_instances(tmp_path)[0]['bot_config']}</string>" in bot["content"]
    assert "<string>api-serve</string>" in api["content"]
    assert "<string>--confirm</string>" not in api["content"]
    assert "<key>KeepAlive</key>" in bot["content"]
    assert "<key>KeepAlive</key>" in api["content"]

    names = [entry["name"] for entry in _profile(bundle)["services"]]
    assert {bot_label, api_label} <= set(names)
    assert not any(name.endswith(".service") and "group-query" in name
                   for name in names)


def test_instance_ids_that_share_a_unit_name_are_rejected(tmp_path):
    instances = [
        {"instance_id": "gq_main", "bot_config": str(tmp_path / "bot.json"),
         "api_config": str(tmp_path / "api.json")},
        {"instance_id": "gq-main", "bot_config": str(tmp_path / "bot2.json"),
         "api_config": str(tmp_path / "api2.json")},
    ]
    with pytest.raises(ValueError):
        _render(tmp_path, group_query_bot_instances=instances)


def test_distinct_instances_render_four_distinct_unit_paths(tmp_path):
    instances = [
        {"instance_id": "gq-a", "bot_config": str(tmp_path / "a-bot.json"),
         "api_config": str(tmp_path / "a-api.json")},
        {"instance_id": "gq-b", "bot_config": str(tmp_path / "b-bot.json"),
         "api_config": str(tmp_path / "b-api.json")},
    ]
    files = _files(_render(tmp_path, group_query_bot_instances=instances))
    paths = sorted(path for path in files if "group-query" in path)
    assert paths == sorted([
        "systemd/options-monitor-group-query-bot-gq-a.service",
        "systemd/options-monitor-group-query-api-gq-a.service",
        "systemd/options-monitor-group-query-bot-gq-b.service",
        "systemd/options-monitor-group-query-api-gq-b.service",
    ])
    assert len(set(paths)) == 4


@pytest.mark.parametrize("instances", [
    [{"instance_id": "gq-main", "bot_config": "/tmp/bot.json"}],
    [{"instance_id": "gq-main", "bot_config": "/tmp/bot.json",
      "api_config": "/tmp/api.json", "extra": "unexpected"}],
    [{"instance_id": "gq main", "bot_config": "/tmp/bot.json",
      "api_config": "/tmp/api.json"}],
    [{"instance_id": "gq-main", "bot_config": "/tmp/bot.json",
      "api_config": "/tmp/api.json"},
     {"instance_id": "gq-main", "bot_config": "/tmp/bot2.json",
      "api_config": "/tmp/api2.json"}],
])
def test_invalid_instance_declarations_are_rejected(tmp_path, instances):
    with pytest.raises(ValueError):
        _render(tmp_path, group_query_bot_instances=instances)
