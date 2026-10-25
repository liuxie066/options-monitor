from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.service_deploy import render_service_bundle
from src.application.service_drift import service_drift
from src.interfaces.cli import main as cli
from tests.service_deploy_test_support import (
    _fake_systemd_query_runner,
    _two_futu_service_accounts,
    _write_systemd_units_from_bundle,
)


def _write_configs(source, paths, settings, scopes):
    if source == "yaml":
        paths["yaml"].write_text(json.dumps({
            "accounts": settings,
            "markets": {market: {"accounts": accounts, "symbols": ["NVDA" if market == "us" else "0700.HK"]}
                        for market, accounts in scopes.items()},
        }))
    else:
        for market, accounts in scopes.items():
            paths[market].write_text(json.dumps({
                "accounts": accounts,
                "account_settings": {account: settings[account] for account in accounts},
            }))


def _fixture(tmp_path, source, *, split=False, target="systemd", accounts=("lx", "sy")):
    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    settings = dict(zip(accounts, _two_futu_service_accounts(tmp_path).values()))
    for index, value in enumerate(settings.values()):
        value["futu"]["account_id"] = f"REAL_{12345678 + index}"
    scopes = {"us": [accounts[0]], "hk": [accounts[1]]} if split else {"us": list(accounts), "hk": list(accounts)}
    paths = {key: runtime / f"config.{key}.json" for key in scopes}
    paths["yaml"] = runtime / "config.yaml"
    _write_configs(source, paths, settings, scopes)
    kwargs = dict(target=target, repo_root=repo, runtime_root=runtime, accounts=list(accounts),
                  markets=["us", "hk"], include_opend=True)
    kwargs.update({"config_yaml": paths["yaml"]} if source == "yaml" else {"config_paths": {key: paths[key] for key in scopes}})
    return paths, settings, scopes, kwargs


def _profile(bundle):
    return json.loads(next(item["content"] for item in bundle["files"] if item["kind"] == "service_profile"))


def _install(tmp_path, kwargs):
    bundle = render_service_bundle(**kwargs)
    units = tmp_path / "units"
    _write_systemd_units_from_bundle(bundle, units)
    profile_path = kwargs["runtime_root"] / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(bundle)))
    calls = []
    options = dict(repo_root=kwargs["repo_root"], runtime_root=kwargs["runtime_root"],
                   systemd_unit_root=units, managed_root_uid=os.getuid(), managed_root_gid=os.getgid(),
                   run_cmd=_fake_systemd_query_runner(record=lambda command, kwargs: calls.append(command)))
    return units, profile_path, calls, options


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("damage", ["missing", "invalid", "no_roots", "one_root_lost"])
def test_drift_rejects_incomplete_opend_plan_before_any_effect(tmp_path, source, confirm, damage):
    paths, settings, scopes, kwargs = _fixture(tmp_path, source)
    units, profile_path, calls, options = _install(tmp_path, kwargs)
    before = {path.name: path.read_bytes() for path in units.iterdir()}
    profile_before = profile_path.read_bytes()
    source_path = paths["yaml"] if source == "yaml" else paths["us"]
    if damage == "missing":
        source_path.unlink()
    elif damage == "invalid":
        source_path.write_text("{invalid")
    else:
        for account in (["lx", "sy"] if damage == "no_roots" else ["sy"]):
            settings[account]["futu"].pop("opend_root")
        _write_configs(source, paths, settings, scopes)
    with pytest.raises((ValueError, AgentToolError)):
        service_drift(**options, confirm=confirm)
    assert calls == []
    assert profile_path.read_bytes() == profile_before
    assert {path.name: path.read_bytes() for path in units.iterdir()} == before


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_split_market_accounts_keep_complete_opend_plan(tmp_path, source, target):
    _, settings, _, kwargs = _fixture(tmp_path, source, split=True, target=target)
    bundle = render_service_bundle(**kwargs)
    services = _profile(bundle)["opend"]["services"]
    assert [item["account"] for item in services] == ["lx", "sy"]
    for service in services:
        account = service["account"]
        assert service["root"] == settings[account]["futu"]["opend_root"]
        assert service["port"] == settings[account]["futu"]["port"]
        assert service["service_name"] == (f"options-monitor-opend-{account}.service" if target == "systemd" else f"com.options-monitor.opend.{account}")
    if target == "systemd":
        intake = next(item["content"] for item in bundle["files"] if item["relative_path"] == "systemd/options-monitor-trade-intake.service")
        assert "Wants=network-online.target options-monitor-opend-lx.service options-monitor-opend-sy.service" in intake


@pytest.mark.parametrize("source", ["json", "yaml"])
def test_split_market_profile_round_trip_and_retry_preserve_units(tmp_path, source):
    paths, _, _, kwargs = _fixture(tmp_path, source, split=True)
    units, profile_path, calls, options = _install(tmp_path, kwargs)
    before = {path.name: path.read_bytes() for path in units.iterdir()}
    profile_before = profile_path.read_bytes()
    source_path = paths["yaml"] if source == "yaml" else paths["hk"]
    saved = source_path.read_bytes()
    source_path.unlink()
    with pytest.raises((ValueError, AgentToolError)):
        service_drift(**options, confirm=True)
    assert calls == []
    source_path.write_bytes(saved)
    for _ in range(2):
        out = service_drift(**options, confirm=True)
        assert out["summary"]["ok"] is True
        assert out["applied"]["retired_units"] == []
        assert out["changed"] is False
    assert profile_path.read_bytes() == profile_before
    assert {path.name: path.read_bytes() for path in units.iterdir()} == before
    assert not any("disable" in command for command in calls)


@pytest.mark.parametrize("fact,value", [("opend_root", "/different/root"), ("host", "127.0.0.2"), ("port", 22222)])
def test_same_account_conflicting_market_facts_fail(tmp_path, fact, value):
    paths, settings, scopes, kwargs = _fixture(tmp_path, "json")
    other = copy.deepcopy(settings)
    other["lx"]["futu"][fact] = value
    paths["hk"].write_text(json.dumps({"accounts": scopes["hk"], "account_settings": other}))
    with pytest.raises(ValueError, match="conflicting OpenD settings.*lx"):
        render_service_bundle(**kwargs)


@pytest.mark.parametrize("source", ["json", "yaml"])
def test_mixed_local_and_remote_accounts_remain_supported(tmp_path, source):
    paths, settings, scopes, kwargs = _fixture(tmp_path, source)
    settings["sy"]["futu"].pop("opend_root")
    _write_configs(source, paths, settings, scopes)
    units, _, calls, options = _install(tmp_path, kwargs)
    out = service_drift(**options, confirm=True)
    assert out["summary"]["ok"] is True
    assert out["changed"] is False
    assert {path.name for path in units.iterdir() if "opend" in path.name} == {"options-monitor-opend.service"}
    assert not any("disable" in command for command in calls)


def test_explicit_and_recorded_legacy_root_work_without_source(tmp_path):
    _, _, _, kwargs = _fixture(tmp_path, "json")
    kwargs["opend_root"] = tmp_path / "explicit-opend"
    units, profile_path, _, options = _install(tmp_path, kwargs)
    for path in kwargs["config_paths"].values():
        path.unlink()
    out = service_drift(**options, confirm=True)
    assert out["summary"]["ok"] is True
    assert out["changed"] is False
    assert _profile(render_service_bundle(**kwargs))["opend"]["root"] == str(kwargs["opend_root"])
    assert json.loads(profile_path.read_text())["opend"]["root"] == str(kwargs["opend_root"])
    assert (units / "options-monitor-opend.service").exists()


def test_explicit_account_scope_reduction_remains_possible(tmp_path):
    paths, settings, scopes, kwargs = _fixture(tmp_path, "json")
    units, profile_path, _, options = _install(tmp_path, kwargs)
    for market in scopes:
        scopes[market] = ["lx"]
    _write_configs("json", paths, settings, scopes)
    profile = json.loads(profile_path.read_text())
    profile["accounts"] = ["lx"]
    profile_path.write_text(json.dumps(profile))
    out = service_drift(**options, confirm=True)
    # Removed accounts no longer authorize retirement of their unit.
    assert out["applied"]["retired_units"] == ["options-monitor-opend-lx.service"]
    assert (units / "options-monitor-opend-sy.service").exists()
    assert (units / "options-monitor-opend.service").exists()


def test_service_render_cli_no_roots_fails_without_output(tmp_path, capsys):
    paths, settings, scopes, kwargs = _fixture(tmp_path, "yaml")
    for value in settings.values():
        value["futu"].pop("opend_root")
    _write_configs("yaml", paths, settings, scopes)
    output = tmp_path / "rendered"
    code = cli.main(["service", "render", "--target", "systemd", "--repo-root", str(kwargs["repo_root"]),
                     "--runtime-root", str(kwargs["runtime_root"]), "--markets", "us", "hk", "--accounts", "lx", "sy",
                     "--config-yaml", str(paths["yaml"]), "--include-opend", "--output-dir", str(output)])
    response = json.loads(capsys.readouterr().out)
    assert code != 0
    assert response["ok"] is False
    assert response["error"]["code"] == "CONFIG_ERROR"
    assert not output.exists()


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("accounts,slug", [(("a_b", "a-b"), "a-b"), (("a_", "a"), "a")])
def test_render_rejects_colliding_opend_identities(tmp_path, source, target, accounts, slug):
    _, settings, _, kwargs = _fixture(tmp_path, source, target=target, accounts=accounts)
    with pytest.raises(ValueError, match="OpenD service identity collision") as error:
        render_service_bundle(**kwargs)
    message = str(error.value)
    assert f"accounts {accounts[0]!r} and {accounts[1]!r}" in message
    assert f"options-monitor-opend-{slug}.service" in message
    assert f"com.options-monitor.opend.{slug}" in message
    for account in settings.values():
        assert account["futu"]["account_id"] not in message
        assert account["futu"]["opend_root"] not in message


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_distinct_opend_slugs_preserve_order_and_single_instance(tmp_path, source, target):
    _, settings, _, kwargs = _fixture(tmp_path, source, target=target, accounts=("a_b", "a-c"))
    bundle = render_service_bundle(**kwargs)
    names = (["options-monitor-opend-a-b.service", "options-monitor-opend-a-c.service"]
             if target == "systemd" else ["com.options-monitor.opend.a-b", "com.options-monitor.opend.a-c"])
    assert _profile(bundle)["opend"]["services"] == [
        {"account": account, "root": settings[account]["futu"]["opend_root"],
         "executable": str(Path(settings[account]["futu"]["opend_root"]) / "FutuOpenD"),
         "service_name": name, "host": settings[account]["futu"]["host"], "port": settings[account]["futu"]["port"]}
        for account, name in zip(("a_b", "a-c"), names)
    ]
    paths = [item["relative_path"] for item in bundle["files"] if "opend" in item["relative_path"]]
    assert len(paths) == len(set(paths)) == 2
    if target == "systemd":
        intake = next(item["content"] for item in bundle["files"]
                      if item["relative_path"] == "systemd/options-monitor-trade-intake.service")
        for dependency in ("After", "Wants"):
            assert f"{dependency}=network-online.target {' '.join(names)}" in intake
    kwargs["accounts"] = ["a_b"]
    services = _profile(render_service_bundle(**kwargs))["opend"]["services"]
    assert len(services) == 1
    assert services[0]["account"] == "a_b"
    assert services[0]["root"] == settings["a_b"]["futu"]["opend_root"]
    assert services[0]["service_name"] == ("options-monitor-opend.service" if target == "systemd" else "com.options-monitor.opend")


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_explicit_opend_root_keeps_legacy_identity_with_colliding_accounts(tmp_path, source, target):
    _, _, _, kwargs = _fixture(tmp_path, source, target=target, accounts=("a_b", "a-b"))
    kwargs["opend_root"] = tmp_path / "explicit-opend"
    services = _profile(render_service_bundle(**kwargs))["opend"]["services"]
    assert services == [{"root": str(kwargs["opend_root"]),
                         "executable": str(kwargs["opend_root"] / "FutuOpenD"),
                         "service_name": "options-monitor-opend.service" if target == "systemd" else "com.options-monitor.opend"}]


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("existing_output", [False, True])
def test_service_render_cli_collision_preserves_output(tmp_path, capsys, target, existing_output):
    paths, _, _, kwargs = _fixture(tmp_path, "yaml", target=target, accounts=("a_b", "a-b"))
    output = tmp_path / "rendered"
    if existing_output:
        name = "options-monitor-opend-a-b.service" if target == "systemd" else "com.options-monitor.opend.a-b.plist"
        sentinel = output / target / name
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"original OpenD definition")
        (output / "service.profile.json").write_bytes(b"original profile")
    before = {path.relative_to(output): path.read_bytes() if path.is_file() else None for path in output.rglob("*")}
    argv = ["service", "render", "--target", target, "--repo-root", str(kwargs["repo_root"]),
            "--runtime-root", str(kwargs["runtime_root"]), "--markets", "us", "hk", "--accounts", "a_b", "a-b",
            "--config-yaml", str(paths["yaml"]), "--include-opend", "--output-dir", str(output)]
    if existing_output:
        argv.append("--no-content")
    code = cli.main(argv)
    response = json.loads(capsys.readouterr().out)
    assert code == 2
    assert response["ok"] is False
    assert response["error"]["code"] == "CONFIG_ERROR"
    message = response["error"]["message"]
    assert "OpenD service identity collision" in message
    assert "accounts 'a_b' and 'a-b'" in message
    assert "options-monitor-opend-a-b.service" in message
    assert "com.options-monitor.opend.a-b" in message
    assert output.exists() is existing_output
    assert {path.relative_to(output): path.read_bytes() if path.is_file() else None for path in output.rglob("*")} == before


@pytest.mark.parametrize("source", ["json", "yaml"])
@pytest.mark.parametrize("confirm", [False, True])
def test_drift_collision_is_effect_free_and_recovers(tmp_path, source, confirm):
    paths, settings, scopes, kwargs = _fixture(tmp_path, source, accounts=("a_b", "a-b"))
    second_root = settings["a-b"]["futu"].pop("opend_root")
    _write_configs(source, paths, settings, scopes)
    units, profile_path, calls, options = _install(tmp_path, kwargs)
    original_units = {path.name: path.read_bytes() for path in units.iterdir()}
    original_profile = profile_path.read_bytes()
    settings["a-b"]["futu"]["opend_root"] = second_root
    _write_configs(source, paths, settings, scopes)
    before = {path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    with pytest.raises(ValueError, match="OpenD service identity collision"):
        service_drift(**options, confirm=confirm)
    assert calls == []
    assert {path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")} == before
    settings["a-b"]["futu"].pop("opend_root")
    _write_configs(source, paths, settings, scopes)
    for _ in range(2):
        out = service_drift(**options, confirm=True)
        assert out["summary"]["ok"] is True
        assert out["changed"] is False
        assert out["applied"]["retired_units"] == []
    assert profile_path.read_bytes() == original_profile
    assert {path.name: path.read_bytes() for path in units.iterdir()} == original_units
    assert not any("disable" in command for command in calls)
