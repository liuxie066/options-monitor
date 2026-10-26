from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.service_deploy_test_support import (
    CURRENT_PYTHON,
    _futu_service_account,
    _write_service_account_config,
    _two_futu_service_accounts,
    _write_upgrade_release_skeleton,
    _write_systemd_units_from_bundle,
    _create_fake_venv_python_at,
    _fake_git_cache_materialize,
    _fake_release_drift_command,
    _fake_release_target_query,
    _mock_published_releases,
    _fake_upgrade_release_runner,
    _fake_legacy_config_build,
    _write_cloned_release_with_server_deps,
    _write_cloned_release_with_configs,
    _render_bundle,
    _drift_at,
    _drift_roots,
    _fake_systemd_query_runner,
    _synthetic_drift_context,
    _guard_original_credential_migration,
    _legacy_credential_migration_fixture,
    _credential_migration_runner,
)


@pytest.fixture(autouse=True)
def _offline_published_release_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_published_releases(monkeypatch)

def test_render_systemd_bundle_can_own_feishu_agent_credential_assets(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import (
        FEISHU_AGENT_CREDENTIAL_DROPIN,
        FEISHU_AGENT_CREDENTIAL_SERVICE,
        render_service_bundle,
        write_service_bundle,
    )

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    helper = tmp_path / "installed" / "libexec" / "credential-helper"
    agent_store = tmp_path / "credstore" / "agent"
    holdings_store = tmp_path / "credstore" / "holdings"
    runtime_env = tmp_path / "run" / "options-monitor-feishu-agent.env"
    repo.mkdir()

    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], include_opend=True, opend_root=tmp_path / "opend", include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=helper, feishu_agent_credential_store=agent_store,
        feishu_holdings_credential_store=holdings_store, feishu_agent_credential_env_file=runtime_env,
        deploy_user="liuxie",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    unit = files[f"systemd/{FEISHU_AGENT_CREDENTIAL_SERVICE}"]
    helper_item = files[
        "systemd/libexec/options-monitor-materialize-feishu-agent-credential"
    ]
    profile = json.loads(files["service.profile.json"]["content"])
    credential_profile = profile["feishu_agent_credential"]
    dropins = [
        item
        for item in bundle["files"]
        if item.get("kind") == "systemd_dropin"
    ]

    assert unit["install_path"] == f"/etc/systemd/system/{FEISHU_AGENT_CREDENTIAL_SERVICE}"
    assert f"ExecStart={helper}" in unit["content"]
    assert "UMask=0077" in unit["content"]
    assert f"--agent-store {agent_store}" in unit["content"]
    assert f"--holdings-store {holdings_store}" in unit["content"]
    assert f"--runtime-env-file {runtime_env}" in unit["content"]
    assert "--deploy-group liuxie" in unit["content"]
    assert helper_item["install_path"] == str(helper)
    assert helper_item["mode"] == 0o755
    assert "systemd-creds decrypt" in helper_item["content"]
    assert "OM_FEISHU_APP_SECRET=%s" in helper_item["content"]
    assert "OM_FEISHU_BOT_APP_SECRET=%s" in helper_item["content"]
    assert "^[A-Za-z0-9_-]+$" in helper_item["content"]
    syntax = subprocess.run(
        ["bash", "-n"],
        input=helper_item["content"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert credential_profile == {
        "enabled": True,
        "service_name": FEISHU_AGENT_CREDENTIAL_SERVICE,
        "helper_path": str(helper),
        "agent_store": str(agent_store),
        "holdings_store": str(holdings_store),
        "runtime_env_file": str(runtime_env),
        "consumer_services": credential_profile["consumer_services"],
    }
    assert credential_profile["consumer_services"]
    assert FEISHU_AGENT_CREDENTIAL_SERVICE not in credential_profile["consumer_services"]
    assert not any(
        name.startswith("options-monitor-opend")
        for name in credential_profile["consumer_services"]
    )
    assert len(dropins) == len(credential_profile["consumer_services"])
    assert all(
        f"Before={consumer}" in unit["content"]
        for consumer in credential_profile["consumer_services"]
    )
    assert {
        Path(item["install_path"]).parent.name.removesuffix(".d")
        for item in dropins
    } == set(credential_profile["consumer_services"])
    assert all(
        Path(item["install_path"]).name == FEISHU_AGENT_CREDENTIAL_DROPIN
        for item in dropins
    )
    assert all(
        f"EnvironmentFile={runtime_env}" in item["content"]
        and f"Requires={FEISHU_AGENT_CREDENTIAL_SERVICE}" in item["content"]
        for item in dropins
    )
    assert (
        f"systemctl enable --now {FEISHU_AGENT_CREDENTIAL_SERVICE}"
        in bundle["commands"]["enable"]
    )

    output_dir = tmp_path / "rendered"
    written = write_service_bundle(bundle, output_dir)
    rendered_helper = output_dir / helper_item["relative_path"]
    assert str(rendered_helper) in written
    assert rendered_helper.stat().st_mode & 0o777 == 0o755

def test_deepseek_credential_is_bound_only_to_selected_bot_service(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"
    store = tmp_path / "credstore.encrypted"
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "accounts: {}\n"
        "markets: {}\n"
        "bot:\n"
        "  enabled: true\n"
        "  active_model: deepseek-default\n"
        "  models:\n"
        "    deepseek-default:\n"
        "      provider: deepseek\n"
        "      model: deepseek-chat\n"
        "      context_window_tokens: 24000\n"
        "      max_output_tokens: 2048\n"
        "      api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )

    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], config_yaml=config_yaml, include_feishu_ws=True,
        include_secret_credentials=True, secret_credential_store_root=store, deploy_user="liuxie",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files[
        "systemd/options-monitor-tick-us.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assistant = files[
        "systemd/options-monitor-feishu-ws.service.d/zzzz-secret-credentials.conf"
    ]["content"]

    assert "om-llm-deepseek-api-key" not in tick
    assert "om-copilot-cursor-hmac-key" not in tick
    assert (
        f"om-llm-deepseek-api-key:{store}/om-llm-deepseek-api-key"
        in assistant
    )
    assert "om-inbound-operation-hmac-key" in assistant
    assert "om-copilot-cursor-hmac-key" not in assistant

def test_service_drift_installs_and_repairs_feishu_agent_credential_assets(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import (
        FEISHU_AGENT_CREDENTIAL_SERVICE,
        render_service_bundle,
    )
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    helper = tmp_path / "libexec" / "credential-helper"
    repo.mkdir()
    runtime.mkdir()
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=helper, feishu_agent_credential_store=tmp_path / "credstore" / "agent",
        feishu_holdings_credential_store=tmp_path / "credstore" / "holdings",
        feishu_agent_credential_env_file=tmp_path / "run" / "credential.env", use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={FEISHU_AGENT_CREDENTIAL_SERVICE},
    )
    calls: list[list[str]] = []
    execution_result = {"value": "success"}

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        if "show" in command and "--property=Result" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{execution_result['value']}\n",
                stderr="",
            )
        if command[-2:] == ["start", FEISHU_AGENT_CREDENTIAL_SERVICE]:
            execution_result["value"] = "success"
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    before = _drift_at(repo, runtime, systemd_root, run_cmd=_run_cmd)

    assert before["summary"]["status"] == "error"
    assert before["missing_required_units"] == [FEISHU_AGENT_CREDENTIAL_SERVICE]
    assert str(helper) in before["missing_managed_files"]
    assert before["missing_managed_files"]

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["summary"]["status"] == "ok"
    assert out["changed"] is True
    assert out["missing_managed_files"] == []
    assert out["mismatched_managed_files"] == []
    assert out["mode_mismatched_managed_files"] == []
    assert out["execution_states"] == {FEISHU_AGENT_CREDENTIAL_SERVICE: "success"}
    assert (systemd_root / FEISHU_AGENT_CREDENTIAL_SERVICE).is_file()
    assert helper.is_file()
    assert helper.stat().st_mode & 0o777 == 0o755
    for consumer in profile["feishu_agent_credential"]["consumer_services"]:
        assert (
            systemd_root
            / f"{consumer}.d"
            / "zzzz-feishu-agent-credential.conf"
        ).is_file()
    assert any(
        command[-3:] == ["enable", "--now", FEISHU_AGENT_CREDENTIAL_SERVICE]
        for command in calls
    )

    helper.chmod(0o644)
    execution_result["value"] = "exit-code"
    mode_drift = _drift_at(repo, runtime, systemd_root, run_cmd=_run_cmd)
    assert mode_drift["mode_mismatched_managed_files"] == [str(helper)]
    assert mode_drift["execution_drift_units"] == [
        FEISHU_AGENT_CREDENTIAL_SERVICE
    ]

    repaired = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)
    assert repaired["summary"]["status"] == "ok"
    assert helper.stat().st_mode & 0o777 == 0o755
    assert repaired["applied"]["started_services"] == [
        FEISHU_AGENT_CREDENTIAL_SERVICE
    ]
    assert any(
        command[-2:] == ["start", FEISHU_AGENT_CREDENTIAL_SERVICE]
        for command in calls
    )

    stale_dropin = (
        systemd_root
        / "options-monitor-retired.service.d"
        / "zzzz-feishu-agent-credential.conf"
    )
    stale_dropin.parent.mkdir(parents=True)
    stale_dropin.write_text("stale\n", encoding="utf-8")
    stale_key = str(
        Path("/etc/systemd/system")
        / stale_dropin.parent.name
        / stale_dropin.name
    )
    stale_drift = _drift_at(repo, runtime, systemd_root, run_cmd=_run_cmd)
    assert stale_drift["extra_managed_files"] == [stale_key]

    retired = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)
    assert retired["summary"]["status"] == "ok"
    assert retired["applied"]["retired_managed_files"] == [stale_key]
    assert not stale_dropin.exists()

def test_service_drift_manages_per_unit_secret_credential_dropins(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import (
        SECRET_CREDENTIAL_DROPIN,
        render_service_bundle,
    )
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], include_secret_credentials=True, use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(
        files["service.profile.json"]["content"],
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    secret_items = [
        item
        for item in bundle["files"]
        if item.get("kind") == "systemd_secret_dropin"
    ]
    secret_keys = sorted(str(item["install_path"]) for item in secret_items)

    before = _drift_at(repo, runtime, systemd_root, managed_root_uid=os.getuid(), managed_root_gid=os.getgid())
    assert secret_keys
    assert all(key in before["missing_managed_files"] for key in secret_keys)

    installed = _drift_at(repo, runtime, systemd_root, confirm=True, managed_root_uid=os.getuid(), managed_root_gid=os.getgid())
    assert installed["missing_managed_files"] == []
    assert installed["mismatched_managed_files"] == []
    assert installed["applied"]["written_managed_files"] == secret_keys
    for item in secret_items:
        relative = Path(str(item["install_path"])).relative_to(
            "/etc/systemd/system"
        )
        assert (systemd_root / relative).read_text(encoding="utf-8") == item["content"]

    first_item = secret_items[0]
    first_relative = Path(str(first_item["install_path"])).relative_to(
        "/etc/systemd/system"
    )
    first_path = systemd_root / first_relative
    first_path.write_text("stale mapping\n", encoding="utf-8")
    mismatched = _drift_at(repo, runtime, systemd_root)
    assert mismatched["mismatched_managed_files"] == [
        str(first_item["install_path"])
    ]

    repaired = _drift_at(repo, runtime, systemd_root, confirm=True)
    assert repaired["mismatched_managed_files"] == []
    assert first_path.read_text(encoding="utf-8") == first_item["content"]

    stale_dropin = (
        systemd_root
        / "options-monitor-retired.service.d"
        / SECRET_CREDENTIAL_DROPIN
    )
    stale_dropin.parent.mkdir(parents=True)
    stale_dropin.write_text("stale grant\n", encoding="utf-8")
    stale_key = str(
        Path("/etc/systemd/system")
        / stale_dropin.parent.name
        / stale_dropin.name
    )
    stale = _drift_at(repo, runtime, systemd_root)
    assert stale["extra_managed_files"] == [stale_key]

    retired = _drift_at(repo, runtime, systemd_root, confirm=True)
    assert retired["applied"]["retired_managed_files"] == [stale_key]
    assert not stale_dropin.exists()
    assert profile["secret_credentials"]["enabled"] is True

def test_service_drift_manages_runtime_credential_helper_and_dropins(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_deploy as service_deploy_module
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    helper_path = tmp_path / "libexec" / "options-monitor-materialize-service-credentials"
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(
        service_deploy_module,
        "DEFAULT_SECRET_CREDENTIAL_HELPER",
        helper_path,
    )
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], include_secret_credentials=True,
        secret_credential_delivery="runtime-files", use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    (runtime / "service.profile.json").write_text(
        files["service.profile.json"]["content"],
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    before = _drift_at(repo, runtime, systemd_root, managed_root_uid=os.getuid(), managed_root_gid=os.getgid())
    assert str(helper_path) in before["missing_managed_files"]
    assert before["missing_managed_files"]

    installed = _drift_at(repo, runtime, systemd_root, confirm=True, managed_root_uid=os.getuid(), managed_root_gid=os.getgid())
    assert installed["missing_managed_files"] == []
    assert installed["mismatched_managed_files"] == []
    assert installed["mode_mismatched_managed_files"] == []
    assert helper_path.is_file()
    assert helper_path.stat().st_mode & 0o777 == 0o755
    assert "systemd-creds" in helper_path.read_text(encoding="utf-8")

    unsafe_owner = _drift_at(repo, runtime, systemd_root, managed_root_uid=os.getuid() + 1, managed_root_gid=os.getgid())
    assert str(helper_path) in unsafe_owner["mode_mismatched_managed_files"]

    helper_copy = tmp_path / "credential-helper-copy"
    helper_copy.write_text(helper_path.read_text(encoding="utf-8"), encoding="utf-8")
    helper_copy.chmod(0o755)
    helper_path.unlink()
    helper_path.symlink_to(helper_copy)
    symlinked = _drift_at(repo, runtime, systemd_root, managed_root_uid=os.getuid(), managed_root_gid=os.getgid())
    assert str(helper_path) in symlinked["mode_mismatched_managed_files"]

def test_service_drift_adopts_legacy_feishu_agent_credential_installation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_deploy import (
        FEISHU_AGENT_CREDENTIAL_DROPIN,
        FEISHU_AGENT_CREDENTIAL_SERVICE,
        render_service_bundle,
    )

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    helper = tmp_path / "libexec" / "credential-helper"
    legacy_unit = tmp_path / "usr-lib-systemd" / FEISHU_AGENT_CREDENTIAL_SERVICE
    repo.mkdir()
    runtime.mkdir()
    legacy_unit.parent.mkdir(parents=True)
    legacy_unit.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    monkeypatch.setattr(
        service_drift_module,
        "LEGACY_FEISHU_AGENT_CREDENTIAL_UNIT_PATH",
        legacy_unit,
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_FEISHU_AGENT_CREDENTIAL_HELPER",
        helper,
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_FEISHU_AGENT_CREDENTIAL_STORE",
        tmp_path / "credstore" / "agent",
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_FEISHU_HOLDINGS_CREDENTIAL_STORE",
        tmp_path / "credstore" / "holdings",
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_FEISHU_AGENT_CREDENTIAL_ENV_FILE",
        tmp_path / "run" / "credential.env",
    )

    base_bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], use_default_deploy_user=False)
    files = {item["relative_path"]: item for item in base_bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(base_bundle, systemd_root)
    legacy_dropin = (
        systemd_root
        / "options-monitor-tick-us.service.d"
        / FEISHU_AGENT_CREDENTIAL_DROPIN
    )
    legacy_dropin.parent.mkdir(parents=True)
    legacy_dropin.write_text("legacy\n", encoding="utf-8")
    calls: list[list[str]] = []

    _run_cmd = _fake_systemd_query_runner(lambda command, _kwargs: calls.append(command))

    before = service_drift_module.service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        run_cmd=_run_cmd,
    )

    assert before["missing_profile_units"] == [FEISHU_AGENT_CREDENTIAL_SERVICE]
    assert before["compatibility_warnings"] == [
        {
            "code": "legacy_feishu_agent_credential_inferred",
            "source": "installed_assets",
            "unit_path": str(legacy_unit),
            "dropin_count": 1,
        }
    ]

    adopted = service_drift_module.service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
    refreshed = json.loads(
        (runtime / "service.profile.json").read_text(encoding="utf-8")
    )

    assert adopted["summary"]["status"] == "ok"
    assert refreshed["feishu_agent_credential"]["enabled"] is True
    assert refreshed["feishu_agent_credential"]["helper_path"] == str(helper)
    assert {"name": FEISHU_AGENT_CREDENTIAL_SERVICE} in refreshed["services"]
    assert (systemd_root / FEISHU_AGENT_CREDENTIAL_SERVICE).is_file()
    assert legacy_unit.is_file()

def test_service_drift_detects_missing_projection_verify_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    config_yaml = runtime / "config.yaml"
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], config_yaml=config_yaml)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )

    out = _drift_at(repo, runtime, systemd_root)

    assert out["summary"]["status"] == "error"
    assert out["summary"]["ok"] is False
    assert out["missing_profile_units"] == [
        "options-monitor-projection-verify.service",
        "options-monitor-projection-verify.timer",
    ]
    assert out["missing_installed_units"] == [
        "options-monitor-projection-verify.service",
        "options-monitor-projection-verify.timer",
    ]
    assert out["missing_required_units"] == ["options-monitor-projection-verify.timer"]

def test_service_drift_missing_heartbeat_does_not_suggest_enable(monkeypatch, tmp_path: Path) -> None:
    import src.application.service_drift as service_drift_module

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = next(item for item in bundle["files"] if item["relative_path"] == "service.profile.json")
    (runtime / "service.profile.json").write_text(profile["content"], encoding="utf-8")
    missing = {"options-monitor-trade-intake-heartbeat.service", "options-monitor-trade-intake.service"}
    _write_systemd_units_from_bundle(bundle, systemd_root, skip=missing)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("isolated drift must not query the host service manager")

    monkeypatch.setattr(service_drift_module.subprocess, "run", unexpected_run)
    out = _drift_at(repo, runtime, systemd_root)

    assert out["missing_installed_units"] == sorted(missing)
    enable_actions = [action for action in out["manual_actions"] if "systemctl enable --now" in action]
    assert enable_actions == [
        "manual_enable_long_running_service: sudo systemctl enable --now options-monitor-trade-intake.service"
    ]
    assert f"./om service drift --profile-path {runtime / 'service.profile.json'} --confirm" in out["manual_actions"]


def test_service_drift_detects_mismatched_timer_content(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    timer_path = systemd_root / "options-monitor-auto-close-us.timer"
    original_timer = timer_path.read_text(encoding="utf-8")
    changed_timer = original_timer.replace("OnCalendar=*-*-* 00:07:00 America/New_York", "OnCalendar=*-*-* 05:30:00 Asia/Shanghai")
    assert changed_timer != original_timer
    timer_path.write_text(
        changed_timer,
        encoding="utf-8",
    )
    (systemd_root / "options-monitor-projection-verify.timer").write_text(
        (systemd_root / "options-monitor-projection-verify.timer")
        .read_text(encoding="utf-8")
        .replace("OnCalendar=*-*-* 09:30:00 Asia/Shanghai", "OnCalendar=*-*-* 06:00:00 Asia/Shanghai"),
        encoding="utf-8",
    )

    out = _drift_at(repo, runtime, systemd_root)

    assert out["summary"]["status"] == "error"
    assert out["summary"]["ok"] is False
    assert out["summary"]["mismatched_count"] == 2
    assert out["mismatched_units"] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-projection-verify.timer",
    ]
    assert f"./om service drift --profile-path {runtime / 'service.profile.json'} --confirm" in out["manual_actions"]

def test_service_drift_does_not_query_host_systemctl_for_custom_unit_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_deploy import render_service_bundle

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"]
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    unexpected_calls = []

    def _unexpected_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        unexpected_calls.append((_args, _kwargs))
        raise AssertionError("custom unit roots must not query host systemctl")

    monkeypatch.setattr(service_drift_module, "subprocess", SimpleNamespace(run=_unexpected_run))

    out = service_drift_module.service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
    )

    assert out["summary"]["status"] == "ok"
    assert out["activation_states"] == {}
    assert out["active_states"] == {}
    assert out["activation_drift_units"] == []
    assert out["activation_unknown_units"] == []
    assert out["summary"]["activation_unknown_count"] == 0
    assert unexpected_calls == []

def test_service_drift_confirm_writes_missing_timer_and_profile(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    config_yaml = runtime / "config.yaml"
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], config_yaml=config_yaml)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["summary"]["status"] == "ok"
    assert out["changed"] is True
    assert (systemd_root / "options-monitor-projection-verify.service").exists()
    assert (systemd_root / "options-monitor-projection-verify.timer").exists()
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert {"name": "options-monitor-projection-verify.timer"} in refreshed["services"]
    assert refreshed["config_authoring"]["config_yaml"] == str(config_yaml)
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.service"] not in calls

def test_service_drift_confirm_updates_mismatched_timer_content(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    timer_path = systemd_root / "options-monitor-projection-verify.timer"
    timer_path.write_text(
        timer_path.read_text(encoding="utf-8").replace(
            "OnCalendar=*-*-* 09:30:00 Asia/Shanghai",
            "OnCalendar=*-*-* 06:00:00 Asia/Shanghai",
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["summary"]["status"] == "ok"
    assert out["changed"] is True
    assert out["before"]["mismatched_units"] == ["options-monitor-projection-verify.timer"]
    assert out["mismatched_units"] == []
    assert "OnCalendar=*-*-* 09:30:00 Asia/Shanghai" in timer_path.read_text(encoding="utf-8")
    assert out["applied"]["written_units"] == ["options-monitor-projection-verify.timer"]
    assert out["applied"]["restarted_timers"] == ["options-monitor-projection-verify.timer"]
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "restart", "options-monitor-projection-verify.timer"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] not in calls

def test_service_drift_confirm_uses_sudo_fallback_for_systemd_permission_errors(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    original_write_text = Path.write_text

    def _write_text(path: Path, content: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if systemd_root in path.parents:
            raise PermissionError("permission denied")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _write_text)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:4] == ["sudo", "-n", "sh", "-c"]:
            original_write_text(Path(command[-1]), str(kwargs.get("input") or ""), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="written\n", stderr="")
        if command[0:1] == ["systemctl"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Access denied\n")
        return _fake_systemd_query_runner()(command, **kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["summary"]["status"] == "ok"
    assert any(item.get("sudo_fallback") for item in out["operations"] if item.get("operation") == "write_unit")
    assert ["sudo", "-n", "systemctl", "daemon-reload"] in calls
    assert ["sudo", "-n", "systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls

def test_service_drift_retires_installed_feishu_ws_when_profile_no_longer_declares_it(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], include_feishu_ws=True)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile.pop("feishu_ws", None)
    profile["services"] = [item for item in profile["services"] if item["name"] != "options-monitor-feishu-ws.service"]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))

    assert "options-monitor-feishu-ws.service" not in out["expected_services"]
    assert {"name": "options-monitor-feishu-ws.service"} not in refreshed["services"]
    assert "feishu_ws" not in refreshed
    assert not (systemd_root / "options-monitor-feishu-ws.service").exists()
    assert out["before"]["extra_installed_units"] == ["options-monitor-feishu-ws.service"]
    assert out["applied"]["retired_units"] == ["options-monitor-feishu-ws.service"]
    assert ["systemctl", "disable", "--now", "options-monitor-feishu-ws.service"] in calls


def test_service_drift_preserves_backups_nonunits_and_custom_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from src.interfaces.cli.service_ops import add_service_update_commands, handle_service_update_command

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], use_default_deploy_user=False,
    )
    profile = json.loads(
        next(item["content"] for item in bundle["files"] if item["relative_path"] == "service.profile.json")
    )
    retired = "options-monitor-ai-evidence-collector.service"
    profile["services"].append({"name": retired})
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    (systemd_root / retired).write_bytes(b"legacy product unit\n")
    custom = {
        "options-monitor-custom.service", "options-monitor-custom.timer",
        "options-monitor-opend-other.service", "options-monitor-custom.bak.service",
    }
    nonunits = {
        f"{retired}.bak",
        "options-monitor-tick-us.service.om-backup-0123456789abcdef",
        "options-monitor-tick-us.service.disabled",
        "options-monitor-tick-us.timer~",
        "options-monitor-notes.txt",
    }
    protected = {
        name: f"# 用户自定义 {name}\r\n".encode("utf-8")
        for name in custom | nonunits
    }
    for name, content in protected.items():
        (systemd_root / name).write_bytes(content)
    directory = systemd_root / "options-monitor-directory.service"
    directory.mkdir()
    (directory / "keep.txt").write_bytes(b"preserve directory contents\n")
    original_profile = profile_path.read_bytes()
    original_units = {path.name: path.read_bytes() for path in systemd_root.iterdir() if path.is_file()}
    calls: list[list[str]] = []
    runner = _fake_systemd_query_runner(record=lambda command, _kwargs: calls.append(command))

    before = _drift_at(repo, runtime, systemd_root, run_cmd=runner)

    assert before["confirmed"] is False
    assert before["changed"] is False
    assert before["operations"] == []
    assert before["extra_installed_units"] == sorted(custom | {retired})
    assert set(before["installed_units"]) == set(original_units) - nonunits
    assert before["summary"]["status"] == "error"
    assert before["summary"]["ok"] is False
    assert before["summary"]["error_count"] > 0
    assert profile_path.read_bytes() == original_profile
    assert {path.name: path.read_bytes() for path in systemd_root.iterdir() if path.is_file()} == original_units
    assert not any("disable" in command for command in calls)

    applied = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=runner)

    assert applied["before"]["extra_installed_units"] == before["extra_installed_units"]
    assert applied["applied"]["retired_units"] == [retired]
    assert applied["apply_errors"] == []
    assert applied["summary"]["status"] == "warn"
    assert applied["summary"]["error_count"] == 0
    assert applied["summary"]["warning_count"] > 0
    parser = argparse.ArgumentParser()
    add_service_update_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["service", "drift", "--runtime-root", str(runtime), "--confirm"])
    response = handle_service_update_command(args, service_drift_fn=lambda **_kwargs: applied)
    assert response["ok"] is True
    from src.interfaces.cli import main as cli

    for result, expected_code in ((before, 2), (applied, 0)):
        monkeypatch.setattr(cli, "service_drift", lambda **_kwargs: result)
        assert cli.main(["service", "drift", "--runtime-root", str(runtime)]) == expected_code
        stdout = capsys.readouterr().out
        payload = json.loads(stdout)
        assert payload["ok"] is (expected_code == 0)

    assert applied["extra_installed_units"] == sorted(custom)
    assert set(applied["installed_units"]) == set(original_units) - nonunits - {retired}
    assert not (systemd_root / retired).exists()
    assert ["systemctl", "disable", "--now", retired] in calls
    assert {
        item["path"] for item in applied["operations"] if item.get("operation") == "delete_unit"
    } == {str(systemd_root / retired)}
    assert not any("disable" in command and command[-1] in protected for command in calls)
    for result in (before, applied):
        for name in custom:
            assert f"manual_review_extra_unit: systemctl cat {name}" in result["manual_actions"]
        assert not any(
            "manual_retire_unit" in action and any(name in action for name in custom)
            for action in result["manual_actions"]
        )
    for name, content in protected.items():
        assert (systemd_root / name).read_bytes() == content
    assert (directory / "keep.txt").read_bytes() == b"preserve directory contents\n"


@pytest.mark.parametrize("accounts", [None, [], "lx", [123, "lx"]])
def test_service_drift_never_persists_inferred_retirement_accounts(
    tmp_path: Path, accounts: object,
) -> None:
    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], use_default_deploy_user=False,
    )
    profile = json.loads(
        next(item["content"] for item in bundle["files"] if item["relative_path"] == "service.profile.json")
    )
    if accounts is None:
        profile.pop("accounts")
    else:
        profile["accounts"] = accounts
    profile["services"].append({"name": "options-monitor-ai-evidence-collector.service"})
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    protected = "options-monitor-opend-123.service" if isinstance(accounts, list) and accounts else "options-monitor-opend-lx.service"
    target = systemd_root / protected
    target.write_bytes(b"operator-owned unit\n")
    calls: list[list[str]] = []
    runner = _fake_systemd_query_runner(record=lambda command, _kwargs: calls.append(command))
    for attempt in range(3):
        out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=runner)
        assert out["apply_errors"] == []
        assert out["applied"]["retired_units"] == []
        assert out["summary"]["status"] == "warn"
        assert out["extra_installed_units"] == [protected]
        assert out["applied"]["profile_written"] is (attempt == 0)
        refreshed = json.loads(profile_path.read_text(encoding="utf-8"))
        if accounts is None:
            assert "accounts" not in refreshed
        else:
            assert refreshed["accounts"] == accounts
        assert target.read_bytes() == b"operator-owned unit\n"
    assert not any("disable" in command and command[-1] == protected for command in calls)


@pytest.mark.parametrize("failure", ["disable", "delete"])
@pytest.mark.parametrize("retired", [
    "options-monitor-ai-evidence-collector.service", "options-monitor-opend-lx.service",
])
def test_service_drift_retries_retirement_after_failure_and_profile_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, retired: str,
) -> None:
    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], use_default_deploy_user=False,
    )
    profile = json.loads(
        next(item["content"] for item in bundle["files"] if item["relative_path"] == "service.profile.json")
    )
    profile["services"].append({"name": retired})
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    target = systemd_root / retired
    original_content = b"legacy product unit\n"
    target.write_bytes(original_content)
    calls: list[list[str]] = []
    query_runner = _fake_systemd_query_runner(record=lambda command, _kwargs: calls.append(command))
    failure_active = True
    original_unlink = Path.unlink

    def _unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if failure_active and failure == "delete" and path == target:
            raise PermissionError("retirement blocked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        blocked = (
            failure == "disable" and command[-3:] == ["disable", "--now", retired]
            or failure == "delete" and command == ["sudo", "-n", "rm", "--", str(target)]
        )
        if failure_active and blocked:
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="retirement blocked")
        return query_runner(command, **kwargs)

    failed = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert failed["summary"]["status"] == "error"
    assert failed["summary"]["ok"] is False
    assert failed["summary"]["error_count"] > 0
    assert len(failed["apply_errors"]) == 1
    assert retired in failed["apply_errors"][0]
    assert "retirement blocked" in failed["apply_errors"][0]
    assert failed["applied"]["retired_units"] == []
    assert failed["applied"]["profile_written"] is True
    assert failed["extra_profile_units"] == []
    assert failed["extra_installed_units"] == [retired]
    assert target.read_bytes() == original_content
    refreshed_profile = profile_path.read_bytes()
    assert retired not in {item["name"] for item in json.loads(refreshed_profile)["services"]}
    assert ["systemctl", "disable", "--now", retired] in calls
    if failure == "delete":
        assert ["sudo", "-n", "rm", "--", str(target)] in calls
    else:
        assert not any(item.get("operation") == "delete_unit" for item in failed["operations"])

    failure_active = False
    calls.clear()
    retried = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert retried["before"]["extra_profile_units"] == []
    assert retried["before"]["extra_installed_units"] == [retired]
    assert retried["summary"]["status"] == "ok"
    assert retried["apply_errors"] == []
    assert retried["applied"]["profile_written"] is False
    assert retried["applied"]["retired_units"] == [retired]
    assert retried["extra_installed_units"] == []
    assert retried["changed"] is True
    assert not target.exists()
    assert ["systemctl", "disable", "--now", retired] in calls

    calls.clear()
    final = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert final["summary"]["status"] == "ok"
    assert final["apply_errors"] == []
    assert final["applied"]["retired_units"] == []
    assert final["changed"] is False
    assert final["operations"] == []
    assert not any("disable" in command or "rm" in command for command in calls)
    assert profile_path.read_bytes() == refreshed_profile

def test_service_drift_retires_removed_position_advice_promotion_units(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}[
            "service.profile.json"
        ]["content"]
    )
    retired_units = (
        "options-monitor-position-advice-promotion.service",
        "options-monitor-position-advice-promotion.timer",
    )
    profile["services"].extend({"name": name} for name in retired_units)
    profile["position_advice_promotion"] = {
        "enabled": True,
        "schedule_beijing": "05:15",
    }
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    systemd_root.mkdir(exist_ok=True)
    for name in retired_units:
        (systemd_root / name).write_text("legacy\n", encoding="utf-8")
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)
    refreshed = json.loads(
        (runtime / "service.profile.json").read_text(encoding="utf-8")
    )

    assert out["before"]["extra_installed_units"] == sorted(retired_units)
    assert out["applied"]["retired_units"] == sorted(retired_units)
    assert "position_advice_promotion" not in refreshed
    assert not any(
        "position-advice" in str(item.get("name") or "")
        for item in refreshed["services"]
    )
    for name in retired_units:
        assert not (systemd_root / name).exists()
        assert ["systemctl", "disable", "--now", name] in calls

def test_service_drift_reports_retired_ai_collector_units_without_applying(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}[
            "service.profile.json"
        ]["content"]
    )
    retired_units = (
        "options-monitor-ai-evidence-collector.service",
        "options-monitor-ai-evidence-collector.timer",
    )
    profile["services"].extend({"name": name} for name in retired_units)
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    systemd_root.mkdir(exist_ok=True)
    for name in retired_units:
        (systemd_root / name).write_text("legacy\n", encoding="utf-8")

    out = _drift_at(repo, runtime, systemd_root, confirm=False)

    assert out["extra_profile_units"] == sorted(retired_units)
    assert out["extra_installed_units"] == sorted(retired_units)
    assert out["confirmed"] is False
    assert out["changed"] is False
    assert out["operations"] == []
    assert all((systemd_root / name).is_file() for name in retired_units)

def test_service_drift_repairs_masked_expected_timer_and_reads_back_enabled(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    target = "options-monitor-projection-verify.timer"
    states = {target: "masked"}
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[-2:] == ["is-enabled", target]:
            state = states[target]
            return subprocess.CompletedProcess(command, 0 if state == "enabled" else 1, stdout=f"{state}\n", stderr="")
        if command[-2:] == ["unmask", target]:
            states[target] = "disabled"
        if command[-3:] == ["enable", "--now", target]:
            states[target] = "enabled"
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["before"]["activation_states"][target] == "masked"
    assert out["before"]["activation_drift_units"] == [target]
    assert out["activation_states"][target] == "enabled"
    assert out["activation_drift_units"] == []
    assert out["summary"]["status"] == "ok"
    assert ["systemctl", "unmask", target] in calls
    assert ["systemctl", "enable", "--now", target] in calls

def test_service_drift_repairs_enabled_but_inactive_expected_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"]
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    target = "options-monitor-projection-verify.timer"
    active_states = {target: "inactive"}
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[-2:] == ["is-enabled", target]:
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if command[-2:] == ["is-active", target]:
            state = active_states[target]
            return subprocess.CompletedProcess(
                command,
                0 if state == "active" else 3,
                stdout=f"{state}\n",
                stderr="",
            )
        if command[-3:] == ["enable", "--now", target]:
            active_states[target] = "active"
        if len(command) >= 2 and command[-2] == "is-active":
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=_run_cmd)

    assert out["before"]["activation_states"][target] == "enabled"
    assert out["before"]["active_states"][target] == "inactive"
    assert out["before"]["activation_drift_units"] == [target]
    assert out["active_states"][target] == "active"
    assert out["activation_drift_units"] == []
    assert out["summary"]["status"] == "ok"
    assert ["systemctl", "enable", "--now", target] in calls

@pytest.mark.parametrize("paused_activation", ["enabled", "disabled", "masked"])
def test_service_drift_preserves_paused_timer_while_updating_definition(
    tmp_path: Path, paused_activation: str,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import (
        SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
        service_drift,
    )

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["hk"])
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    target = "options-monitor-tick-hk.timer"
    other_inactive = "options-monitor-projection-verify.timer"
    expected_target_content = next(
        item["content"]
        for item in bundle["files"]
        if Path(str(item.get("install_path") or "")).name == target
    )
    target_path = systemd_root / target
    target_path.write_text("stale timer definition\n", encoding="utf-8")
    active_states = {target: "inactive", other_inactive: "inactive"}
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[-2:] in (
            ["is-enabled", target],
            ["is-enabled", other_inactive],
        ):
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{paused_activation if command[-1] == target else 'enabled'}\n", stderr=""
            )
        if len(command) >= 2 and command[-2] == "is-active" and command[-1] in active_states:
            state = active_states[command[-1]]
            return subprocess.CompletedProcess(
                command,
                0 if state == "active" else 3,
                stdout=f"{state}\n",
                stderr="",
            )
        if command[-3:] == ["enable", "--now", other_inactive]:
            active_states[other_inactive] = "active"
        if len(command) >= 2 and command[-2] == "is-active":
            return subprocess.CompletedProcess(
                command, 0, stdout="active\n", stderr=""
            )
        if "show" in command and "--property=Result" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout="success\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command, 0, stdout="enabled\n", stderr=""
        )

    out = _drift_at(
        repo, runtime, systemd_root, confirm=True, activation_policy=SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
        preserved_activation_states={ target: { "activation_state": paused_activation, "active_state": "inactive", } },
        run_cmd=_run_cmd,
    )

    assert out["before"]["observed_activation_drift_units"] == sorted(
        [other_inactive, target]
    )
    assert out["before"]["activation_drift_units"] == [other_inactive]
    assert out["before"]["preserved_activation_units"] == [target]
    assert out["active_states"][target] == "inactive"
    assert out["activation_states"][target] == paused_activation
    assert out["activation_drift_units"] == []
    assert out["preserved_activation_units"] == [target]
    assert out["summary"]["status"] == "ok"
    assert out["summary"]["ok"] is True
    assert out["summary"]["warning_count"] == 0
    assert out["applied"]["deferred_restart_units"] == [target]
    assert target_path.read_text(encoding="utf-8") == expected_target_content
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", other_inactive] in calls
    assert ["systemctl", "enable", "--now", target] not in calls
    assert ["systemctl", "start", target] not in calls
    assert ["systemctl", "restart", target] not in calls
    assert ["systemctl", "unmask", target] not in calls

def test_service_drift_removes_legacy_cursor_binding_without_resuming_paused_timers(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import (
        SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
        service_drift,
    )

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    store = tmp_path / "credstore.encrypted"
    config_yaml = tmp_path / "config.yaml"
    repo.mkdir()
    runtime.mkdir()
    config_yaml.write_text(
        "accounts:\n"
        "  lx:\n"
        "    type: futu\n"
        "    futu:\n"
        "      account_id: REAL_12345678\n"
        "      host: 127.0.0.1\n"
        "      port: 11111\n"
        "markets:\n"
        "  hk:\n"
        "    accounts: [lx]\n"
        "    symbols: [0700.HK]\n"
        "bot:\n"
        "  enabled: true\n"
        "  active_model: deepseek-default\n"
        "  models:\n"
        "    deepseek-default:\n"
        "      provider: deepseek\n"
        "      model: deepseek-chat\n"
        "      context_window_tokens: 128000\n"
        "      max_output_tokens: 2048\n"
        "      api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["hk"], config_yaml=config_yaml,
        env_file=runtime / "options-monitor.env", include_feishu_ws=True, include_wechat_clawbot=True,
        wechat_clawbot_allowed_senders="wechat:test-user", include_secret_credentials=True,
        secret_credential_store_root=store, use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    old_profile = json.loads(files["service.profile.json"]["content"])
    inbound_services = (
        "options-monitor-feishu-ws.service",
        "options-monitor-wechat-clawbot.service",
    )
    for service_name in inbound_services:
        old_profile["secret_credentials"]["service_credentials"][service_name].append(
            "bot.cursor_hmac_key"
        )
    (runtime / "service.profile.json").write_text(
        json.dumps(old_profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    expected_changed_files: list[str] = []
    for item in bundle["files"]:
        if item.get("kind") not in {"systemd_dropin", "systemd_secret_dropin"}:
            continue
        install_path = Path(str(item["install_path"]))
        target_path = systemd_root / install_path.relative_to("/etc/systemd/system")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        content = str(item["content"])
        if install_path.parent.name in inbound_services:
            content += (
                "LoadCredentialEncrypted=om-copilot-cursor-hmac-key:"
                f"{store}/om-copilot-cursor-hmac-key\n"
            )
            expected_changed_files.append(str(install_path))
        target_path.write_text(content, encoding="utf-8")

    paused_timers = {
        "options-monitor-tick-hk.timer",
        "options-monitor-auto-close-hk.timer",
    }
    snapshot = {
        name: {"activation_state": "disabled", "active_state": "inactive"}
        for name in paused_timers
    }
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        name = command[-1]
        if command[-2] == "is-enabled":
            state = "disabled" if name in paused_timers else "enabled"
            return subprocess.CompletedProcess(command, 0, stdout=f"{state}\n", stderr="")
        if command[-2] == "is-active":
            state = "inactive" if name in paused_timers else "active"
            return subprocess.CompletedProcess(
                command,
                3 if state == "inactive" else 0,
                stdout=f"{state}\n",
                stderr="",
            )
        if "show" in command and "--property=Result" in command:
            return subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    drift_kwargs = {
        "repo_root": repo,
        "runtime_root": runtime,
        "systemd_unit_root": systemd_root,
        "activation_policy": SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
        "preserved_activation_states": snapshot,
        "run_cmd": _run_cmd,
    }
    before = service_drift(**drift_kwargs)
    assert before["mismatched_managed_files"] == sorted(expected_changed_files)
    assert before["profile_content_changed"] is True
    assert before["preserved_activation_units"] == sorted(paused_timers)
    assert before["activation_preservation_conflicts"] == []

    applied = service_drift(**drift_kwargs, confirm=True)
    assert applied["apply_errors"] == []
    assert applied["applied"]["written_units"] == []
    assert applied["applied"]["written_managed_files"] == sorted(
        expected_changed_files
    )
    assert applied["applied"]["profile_written"] is True
    assert applied["applied"]["enabled_timers"] == []
    assert applied["applied"]["restarted_timers"] == []
    assert applied["activation_preservation_conflicts"] == []
    assert applied["mismatched_managed_files"] == []
    assert applied["profile_content_changed"] is False
    for name in paused_timers:
        assert ["systemctl", "enable", "--now", name] not in calls
        assert ["systemctl", "start", name] not in calls
        assert ["systemctl", "restart", name] not in calls
        assert ["systemctl", "unmask", name] not in calls

    refreshed_profile = json.loads(
        (runtime / "service.profile.json").read_text(encoding="utf-8")
    )
    assert all(
        "bot.cursor_hmac_key"
        not in refreshed_profile["secret_credentials"]["service_credentials"][name]
        for name in inbound_services
    )
    final = service_drift(**drift_kwargs)
    assert final["mismatched_managed_files"] == []
    assert final["profile_content_changed"] is False
    assert final["activation_drift_units"] == []
    assert final["preserved_activation_units"] == sorted(paused_timers)


def test_service_drift_discovers_installed_wechat_clawbot_as_managed_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us"], include_wechat_clawbot=True, wechat_clawbot_label="ops",
        wechat_clawbot_allowed_senders="wechat:user_1",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [item for item in profile["services"] if item["name"] != "options-monitor-wechat-clawbot.service"]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = _drift_at(repo, runtime, systemd_root, confirm=True)
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))

    assert "options-monitor-wechat-clawbot.service" in out["expected_services"]
    assert {"name": "options-monitor-wechat-clawbot.service"} in refreshed["services"]
    assert refreshed["wechat_clawbot"]["enabled"] is True
    assert refreshed["wechat_clawbot"]["label"] == "ops"
    assert refreshed["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-wechat-clawbot.service",
    ]

def test_service_drift_preserves_profile_opend_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    opend = tmp_path / "futu-opend" / "current"
    repo.mkdir()
    runtime.mkdir()
    opend.mkdir(parents=True)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], include_opend=True, opend_root=opend)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = _drift_at(repo, runtime, systemd_root)

    assert out["summary"]["status"] == "ok"
    assert "options-monitor-opend.service" in out["expected_services"]
    assert out["mismatched_units"] == []


def test_service_drift_preserves_quality_monitoring_opt_in_and_detects_metadata_drift(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us", "hk"], include_quality_monitoring=True)
    profile_item = {item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]
    profile = json.loads(profile_item["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    clean = _drift_at(repo, runtime, systemd_root)

    assert clean["summary"]["status"] == "ok"
    assert clean["profile_content_changed"] is False
    assert clean["extra_profile_units"] == []
    assert clean["extra_installed_units"] == []
    assert "options-monitor-quality-http.service" not in clean["expected_services"]
    assert "options-monitor-quality-day-end-us.timer" in clean["expected_services"]
    assert "options-monitor-quality-day-end-hk.timer" in clean["expected_services"]

    profile["quality_monitoring"]["regular_refresh_interval"] = "99min"
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    drifted = _drift_at(repo, runtime, systemd_root)

    assert drifted["summary"]["status"] == "error"
    assert drifted["summary"]["ok"] is False
    assert drifted["profile_content_changed"] is True
    assert drifted["mismatched_units"] == []


def test_service_drift_retires_legacy_quality_http_and_keeps_local_quality(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from tests.service_deploy_test_support import _install_complete_systemd_bundle
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(
        repo, runtime, accounts=["lx"], markets=["us", "hk"], include_quality_monitoring=True,
        include_secret_credentials=True,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])
    retired = "options-monitor-quality-http.service"
    profile["services"].append({"name": retired})
    profile["secret_credentials"]["service_credentials"][retired] = ["quality.read_token"]
    profile["quality_monitoring"]["http"] = {"host": "127.0.0.1", "port": 8792}
    profile.setdefault("restart", {}).setdefault("services", []).append(retired)
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    _install_complete_systemd_bundle(bundle, systemd_root)
    (systemd_root / retired).write_text(
        "[Unit]\nDescription=Options Monitor quality artifact HTTP endpoint\n"
        "[Service]\nExecStart=/old/om quality serve\n", encoding="utf-8",
    )
    dropin = systemd_root / (retired + ".d") / "zzzz-secret-credentials.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nLoadCredentialEncrypted=om-quality-read-token:/old/token\n")
    calls = []

    def run_cmd(command, **_kwargs):
        calls.append(list(command))
        return _fake_systemd_query_runner()(command, **_kwargs)

    out = _drift_at(repo, runtime, systemd_root, confirm=True, run_cmd=run_cmd)
    refreshed = json.loads((runtime / "service.profile.json").read_text())
    assert out["summary"]["status"] == "ok"
    assert out["applied"]["retired_units"] == [retired]
    assert ["systemctl", "disable", "--now", retired] in calls
    assert not (systemd_root / retired).exists()
    assert not dropin.exists()
    assert retired not in refreshed["secret_credentials"]["service_credentials"]
    assert "http" not in refreshed["quality_monitoring"]
    assert retired not in refreshed.get("restart", {}).get("services", [])
    for suffix in ("refresh", "recheck", "day-end-us", "day-end-hk"):
        name = f"options-monitor-quality-{suffix}.timer"
        assert {"name": name} in refreshed["services"]
        assert (systemd_root / name).exists()

def test_service_upgrade_verify_returns_compact_read_only_summary(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_verify, write_upgrade_status

    releases = tmp_path / "releases"
    release = releases / "1.2.3"
    release.mkdir(parents=True)
    (release / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (release / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (release / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (release / "configs").mkdir()
    system_config = release / "configs" / "system.json"
    system_config.write_text('{"runtime": {}}\n', encoding="utf-8")
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")

    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_runtime_config(market: str) -> Path:
        path = runtime / f"config.{market}.json"
        payload = {
            "_generated": {
                "schema_version": "1.0",
                "generator": "options-monitor",
                "source_format": "yaml",
                "version": "1.2.3",
                "market": market,
                "sources": [
                    {
                        "role": "system",
                        "loaded": True,
                        "optional": False,
                        "enabled": True,
                        "path": str(system_config),
                        "sha256": _sha(system_config),
                    },
                    {
                        "role": "market_user",
                        "loaded": True,
                        "optional": False,
                        "enabled": True,
                        "path": str(config_yaml),
                        "sha256": _sha(config_yaml),
                    },
                ],
            },
            "runtime": {},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    config_us = _write_runtime_config("us")
    config_hk = _write_runtime_config("hk")
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "repo_root": str(current),
                "config_paths": {"us": str(config_us), "hk": str(config_hk)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = service_upgrade_verify(repo_root=current, runtime_root=runtime, check_latest=False)

    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["repo_root"] == str(current)
    assert out["repo_root_resolved"] == str(release)
    assert out["version"]["current"] == "1.2.3"
    assert out["version"]["update_status"] == "not_checked"
    assert out["config"]["us"]["ok"] is True
    assert out["config"]["hk"]["freshness_ok"] is True
    assert "event_source" not in out
    assert out["services"]["status"] == "unknown"
    assert out["upgrade"] == {"available": False, "has_status_record": False, "last_status": None}

    write_upgrade_status(
        runtime_root=runtime,
        payload={
            "ok": True,
            "status": "upgraded",
            "current_version": "1.2.2",
            "target_version": "1.2.3",
            "release_tag": "v1.2.3",
            "updated_at": "2026-06-09T00:00:00Z",
            "symlink_switched": True,
            "config_rebuilt": True,
        },
    )

    with_status = service_upgrade_verify(repo_root=current, runtime_root=runtime, check_latest=False)
    assert with_status["version"]["upgrade_available"] is None
    assert with_status["upgrade"]["available"] is True
    assert with_status["upgrade"]["has_status_record"] is True
    assert with_status["upgrade"]["last_status"] == "upgraded"
    assert with_status["upgrade"]["status"] == "upgraded"

def test_write_service_bundle_writes_relative_files(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle, write_service_bundle

    bundle = _render_bundle(tmp_path / "repo", tmp_path / "runtime", markets=["us"])

    written = write_service_bundle(bundle, tmp_path / "rendered")

    assert str(tmp_path / "rendered" / "service.profile.json") in written
    assert (tmp_path / "rendered" / "systemd" / "options-monitor-tick-us.service").exists()

def test_service_preflight_reports_runtime_dirs_and_config_metadata(tmp_path: Path) -> None:
    from src.application.service_deploy import service_preflight

    runtime = tmp_path / "runtime"
    (runtime / "locks").mkdir(parents=True)
    (runtime / "output_accounts").mkdir()
    (runtime / "output_shared").mkdir()
    (runtime / "output").mkdir()
    cfg = tmp_path / "config.us.json"
    cfg.write_text('{"accounts":["lx"]}', encoding="utf-8")

    out = service_preflight(
        runtime_root=runtime,
        accounts=["lx"],
        config_paths={"us": cfg},
    )
    checks = {item["name"]: item for item in out["checks"]}

    assert out["summary"]["ok"] is False
    assert set(checks) == {"runtime_root", "locks", "output_accounts", "output_shared", "runtime_config_us"}
    assert out["repair_commands"] == []
    assert checks["runtime_config_us"]["status"] == "error"
    assert "generation metadata" in checks["runtime_config_us"]["message"]

def test_service_preflight_reports_json_line_and_column(tmp_path: Path) -> None:
    from src.application.service_deploy import service_preflight

    runtime = tmp_path / "runtime"
    (runtime / "locks").mkdir(parents=True)
    (runtime / "output_accounts").mkdir()
    (runtime / "output_shared").mkdir()
    cfg = tmp_path / "config.us.json"
    cfg.write_text('{"accounts":["lx",],\n}', encoding="utf-8")

    out = service_preflight(runtime_root=runtime, accounts=["lx"], config_paths={"us": cfg})
    check = next(item for item in out["checks"] if item["name"] == "runtime_config_us")

    assert check["status"] == "error"
    assert check["value"]["line"] == 1
    assert check["value"]["column"] > 0

def test_service_status_from_profile_checks_provider_with_injected_runner() -> None:
    from src.application.service_deploy import service_status_from_profile

    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

    out = service_status_from_profile(
        {
            "service_provider": "systemd",
            "runtime_root": "/var/lib/options-monitor",
            "services": [{"name": "options-monitor-trade-intake.service"}],
        },
        include_status=True,
        run_cmd=_run_cmd,
    )

    assert out["status_checked"] is True
    assert out["services"][0]["status"] == "ok"
    assert calls == [["systemctl", "is-active", "options-monitor-trade-intake.service"]]

def test_service_status_from_profile_can_check_systemd_enabled_state() -> None:
    from src.application.service_deploy import service_status_from_profile

    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if list(command)[1] == "is-enabled":
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

    out = service_status_from_profile(
        {
            "service_provider": "systemd",
            "services": [{"name": "options-monitor-wechat-clawbot.service"}],
        },
        include_status=True,
        include_enabled=True,
        run_cmd=_run_cmd,
    )

    service = out["services"][0]
    assert service["status"] == "ok"
    assert service["active"]["stdout"] == "active"
    assert service["enabled"]["stdout"] == "enabled"
    assert calls == [
        ["systemctl", "is-active", "options-monitor-wechat-clawbot.service"],
        ["systemctl", "is-enabled", "options-monitor-wechat-clawbot.service"],
    ]

def test_upgrade_activation_snapshot_captures_only_preexisting_paused_timers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as service_upgrade_module

    observed_calls: list[dict[str, object]] = []

    def _service_drift(**kwargs):  # type: ignore[no-untyped-def]
        observed_calls.append(dict(kwargs))
        return {
            "activation_states": {
                "options-monitor-tick-hk.timer": "enabled",
                "options-monitor-tick-us.timer": "enabled",
                "options-monitor-quality-refresh.timer": "disabled",
                "options-monitor-feishu-agent-credential.service": "disabled",
            },
            "active_states": {
                "options-monitor-tick-hk.timer": "inactive",
                "options-monitor-tick-us.timer": "active",
                "options-monitor-quality-refresh.timer": "active",
            },
        }

    monkeypatch.setattr(service_upgrade_module, "service_drift", _service_drift)

    snapshot = service_upgrade_module.capture_preserved_timer_activation_states(
        repo_root=tmp_path / "current",
        runtime_root=tmp_path / "runtime",
        profile={"service_provider": "systemd"},
        run_cmd=lambda *_args, **_kwargs: None,
    )

    assert snapshot == {
        "options-monitor-quality-refresh.timer": {
            "activation_state": "disabled",
            "active_state": "active",
        },
        "options-monitor-tick-hk.timer": {
            "activation_state": "enabled",
            "active_state": "inactive",
        },
    }
    assert observed_calls[0]["confirm"] is False

@pytest.mark.parametrize(
    ("activation_state", "active_state"),
    [("enabled", "unknown"), ("unknown", "active")],
)
def test_upgrade_activation_snapshot_fails_closed_when_timer_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    activation_state: str,
    active_state: str,
) -> None:
    import src.application.service_upgrade as service_upgrade_module

    target = "options-monitor-tick-hk.timer"
    monkeypatch.setattr(
        service_upgrade_module,
        "service_drift",
        lambda **_kwargs: {
            "checked": True,
            "supported": True,
            "expected_services": [target],
            "installed_units": [target],
            "activation_states": {target: activation_state},
            "active_states": {target: active_state},
        },
    )

    with pytest.raises(
        service_upgrade_module.ServiceTransitionError,
        match="could not determine whether managed timers were active",
    ) as exc_info:
        service_upgrade_module.capture_preserved_timer_activation_states(
            repo_root=tmp_path / "current",
            runtime_root=tmp_path / "runtime",
            profile={"service_provider": "systemd"},
            run_cmd=lambda *_args, **_kwargs: None,
        )

    assert exc_info.value.status == "service_activation_snapshot_failed"
    assert target in exc_info.value.remediation[0]

def test_service_upgrade_reuses_paused_timer_snapshot_for_compensation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as service_upgrade_module

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    _write_upgrade_release_skeleton(v101, "1.0.1")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = "options-monitor-tick-hk.timer"
    profile = {
        "service_provider": "systemd",
        "services": [{"name": target}],
    }
    (runtime / "service.profile.json").write_text(
        json.dumps(profile), encoding="utf-8"
    )
    drift_calls: list[dict[str, object]] = []
    transition_events: list[str] = []

    def _service_drift(**kwargs):  # type: ignore[no-untyped-def]
        drift_calls.append(dict(kwargs))
        if not kwargs.get("confirm"):
            transition_events.append("capture")
            return {
                "activation_states": {target: "enabled"},
                "active_states": {target: "inactive"},
            }
        confirmed_count = sum(bool(item.get("confirm")) for item in drift_calls)
        if confirmed_count == 1:
            return {
                "summary": {"status": "error"},
                "manual_actions": ["repair service reconcile"],
            }
        return {"summary": {"status": "ok", "ok": True}, "manual_actions": []}

    monkeypatch.setattr(
        service_upgrade_module,
        "service_upgrade_check",
        lambda **_kwargs: {
            "ok": True,
            "latest_version": "1.0.1",
            "release_tag": "v1.0.1",
        },
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_materialize_release_from_git_cache",
        lambda **_kwargs: {"status": "reused", "target_dir": str(v101)},
    )

    def _ensure_release_runtime(**_kwargs):  # type: ignore[no-untyped-def]
        transition_events.append("prepare")
        return {"status": "ready"}

    monkeypatch.setattr(
        service_upgrade_module, "_ensure_release_runtime", _ensure_release_runtime
    )
    monkeypatch.setattr(
        service_upgrade_module, "_run_required", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_prepare_runtime_configs_for_release",
        lambda **_kwargs: {"status": "prepared"},
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_commit_prepared_runtime_configs",
        lambda **_kwargs: {"status": "committed"},
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_validate_committed_runtime_configs",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_restore_committed_runtime_configs",
        lambda **_kwargs: {
            "ok": True,
            "status": "restored",
            "restored": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(service_upgrade_module, "service_drift", _service_drift)

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        # The forward reconcile now runs as the new release's own `om`, so its
        # drift call reaches this test through the child runner rather than
        # through the module attribute — but it is still the reconcile that
        # `_service_drift` below sees first, and still the one that must fail.
        drift = _fake_release_drift_command(
            list(command), run_cmd=_run_cmd, service_drift_fn=_service_drift
        )
        if drift is not None:
            return drift
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    out = service_upgrade_module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        preserve_activation_state=True,
        run_cmd=_run_cmd,
    )

    expected_snapshot = {
        target: {
            "activation_state": "enabled",
            "active_state": "inactive",
        }
    }
    confirmed_calls = [item for item in drift_calls if item.get("confirm")]
    assert out["status"] == "upgrade_failed_rolled_back"
    assert out["rolled_back"] is True
    assert current.resolve() == v100.resolve()
    assert out["activation_policy"] == "preserve-existing"
    assert out["preserved_activation_units"] == [target]
    assert transition_events[:2] == ["capture", "prepare"]
    assert len(confirmed_calls) == 2
    assert all(
        item["activation_policy"] == "preserve-existing"
        and item["preserved_activation_states"] == expected_snapshot
        for item in confirmed_calls
    )


def test_service_upgrade_preview_reports_preserved_timer_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as service_upgrade_module

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    current_release = releases / "1.0.0"
    _write_upgrade_release_skeleton(current_release, "1.0.0")
    current = install / "current"
    current.symlink_to(current_release, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = "options-monitor-upgrade.timer"
    profile = {
        "service_provider": "systemd",
        "services": [{"name": target}],
    }
    (runtime / "service.profile.json").write_text(
        json.dumps(profile), encoding="utf-8"
    )
    snapshot = {
        target: {
            "activation_state": "disabled",
            "active_state": "inactive",
        }
    }
    monkeypatch.setattr(
        service_upgrade_module,
        "service_upgrade_check",
        lambda **_kwargs: {
            "ok": True,
            "latest_version": "1.0.1",
            "release_tag": "v1.0.1",
        },
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "capture_preserved_timer_activation_states",
        lambda **_kwargs: snapshot,
    )

    out = service_upgrade_module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        preserve_activation_state=True,
    )

    assert out["status"] == "dry_run"
    assert out["preserved_activation_units"] == [target]
    assert out["operations"] == []


@pytest.mark.parametrize("auto", [False, True])
def test_service_upgrade_dry_run_and_confirm_switches_current_symlink(
    monkeypatch,
    tmp_path: Path,
    auto: bool,
) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v100 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    credential_helper = tmp_path / "libexec" / "credential-helper"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {"requires_sudo": False},
                "feishu_agent_credential": {
                    "enabled": True,
                    "service_name": "options-monitor-feishu-agent-credential.service",
                    "helper_path": str(credential_helper),
                    "agent_store": str(tmp_path / "credstore" / "agent"),
                    "holdings_store": str(tmp_path / "credstore" / "holdings"),
                    "runtime_env_file": str(tmp_path / "run" / "credential.env"),
                },
                "services": [
                    {"name": "options-monitor-tick-us.timer"},
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-agent-credential.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        clone_release=_write_cloned_release_with_server_deps,
        tail=lambda command: (
            subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
            if "show" in command and "--property=Result" in command
            else None
        ),
    )

    dry = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        auto=auto,
        run_cmd=_run_cmd,
    )
    assert dry["status"] == "dry_run"
    assert dry["changed"] is False
    assert dry["repo_root_is_symlink"] is True
    assert dry["auto"] is auto
    assert dry["warnings"] == []
    assert current.resolve() == v100.resolve()

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        auto=auto,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["changed"] is True
    assert out["auto"] is auto
    assert current.resolve() == (releases / "1.0.1").resolve()
    cache_repo = install / "_cache" / "git" / "options-monitor.git"
    assert ["git", "clone", "--mirror", "--origin", "origin", "https://github.com/example/options-monitor.git", str(cache_repo)] in calls
    assert any(command[:3] == ["git", f"--git-dir={cache_repo}", "archive"] for command in calls)
    assert any(command[:2] == ["tar", "-xf"] for command in calls)
    assert not any(command[:4] == ["git", "clone", "--depth", "1"] for command in calls)
    assert any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in calls)
    target_python = str(releases / "1.0.1" / ".venv" / "bin" / "python")
    assert [target_python, "scripts/release_check.py", "--tag", "v1.0.1"] in calls
    runtime_prepare = out["runtime_prepare"]
    venv_python = str(Path(runtime_prepare["shared_venv_build_path"]) / "bin" / "python")
    assert [venv_python, "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert [
        venv_python,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements/server.txt",
        "-c",
        "constraints/server.txt",
    ] in calls
    release_python = str(releases / "1.0.1" / ".venv" / "bin" / "python")
    assert any(command[:2] == [release_python, "-c"] for command in calls)
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "is-active", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "is-enabled", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls
    assert [
        "systemctl",
        "enable",
        "--now",
        "options-monitor-feishu-agent-credential.service",
    ] in calls
    refreshed_profile = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert {"name": "options-monitor-projection-verify.timer"} in refreshed_profile["services"]
    assert not any(
        "position-advice" in str(item.get("name") or "")
        for item in refreshed_profile["services"]
    )
    assert refreshed_profile["feishu_agent_credential"]["enabled"] is True
    assert refreshed_profile["feishu_agent_credential"]["helper_path"] == str(
        credential_helper
    )
    assert (systemd_root / "options-monitor-projection-verify.timer").exists()
    assert not (
        systemd_root / "options-monitor-position-advice-promotion.timer"
    ).exists()
    assert (
        systemd_root / "options-monitor-feishu-agent-credential.service"
    ).exists()
    assert credential_helper.is_file()
    assert credential_helper.stat().st_mode & 0o777 == 0o755
    assert (
        systemd_root
        / "options-monitor-trade-intake.service.d"
        / "zzzz-feishu-agent-credential.conf"
    ).is_file()
    assert out["service_reconcile"]["summary"]["status"] == "ok"
    assert out["service_health"]["status"] == "ok"
    assert out["release_materialize"]["method"] == "git_cache_archive"
    assert out["release_materialize"]["cache_repo"] == str(cache_repo)
    assert out["release_materialize"]["cache_initialized"] is False
    assert out["release_materialize"]["fetched"] is True
    assert out["runtime_prepare"]["installer"] == "pip"
    assert out["runtime_prepare"]["fallback"] is False
    assert out["runtime_prepare"]["venv_reused"] is False
    assert "duration_seconds" in out["runtime_prepare"]
    assert out["runtime_prepare"]["uv_cache_dir"] == str(install / "_cache" / "uv")
    assert out["runtime_prepare"]["pip_cache_dir"] == str(install / "_cache" / "pip")
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["release_materialize"]["method"] == "git_cache_archive"
    assert status["runtime_prepare"]["installer"] == "pip"
    assert status["target_version"] == "1.0.1"
    assert status["status"] == "upgraded"

def test_service_upgrade_restarts_feishu_ws_from_refreshed_profile_after_reconcile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / "options-monitor.env"
    env_file.write_text(
        "OM_FEISHU_BOT_APP_ID=cli_1\n"
        "OM_FEISHU_BOT_APP_SECRET=secret_1\n"
        "OM_FEISHU_BOT_USER_OPEN_ID=ou_1\n",
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "repo_root": str(current),
                "runtime_root": str(runtime),
                "env_file": str(env_file),
                "config_paths": {"us": str(runtime / "config.us.json")},
                "feishu_ws": {"enabled": True, "config_key": "us"},
                "restart": {
                    "requires_sudo": False,
                    "services": ["options-monitor-trade-intake.service"],
                },
                "services": [
                    {"name": "options-monitor-tick-us.timer"},
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        venv_stdout="ok\n",
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "restart", "options-monitor-feishu-ws.service"] in calls
    assert ["systemctl", "is-active", "options-monitor-feishu-ws.service"] in calls
    assert ["systemctl", "is-enabled", "options-monitor-feishu-ws.service"] in calls
    assert [
        str(current / "om"),
        "inbound",
        "feishu-ws",
        "--check",
        "--config-key",
        "us",
        "--config-path",
        str(runtime / "config.us.json"),
        "--env-file",
        str(env_file),
    ] in calls
    refreshed_profile = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert refreshed_profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]

def test_service_upgrade_check_falls_back_to_git_cache_when_current_release_has_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.application.service_upgrade import service_upgrade_check

    repo = tmp_path / "releases" / "1.0.0"
    _write_upgrade_release_skeleton(repo, "1.0.0")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    _responses, http_calls = _mock_published_releases(monkeypatch, repository="cached/repository")
    calls: list[tuple[list[str], str | None]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(command), kwargs.get("cwd")))
        if command == ["git", f"--git-dir={cache_repo}", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(command, 0, stdout="git@github.com:cached/repository.git", stderr="")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "fetch"]:
            return subprocess.CompletedProcess(command, 0, stdout="fetched\n", stderr="")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "for-each-ref"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "a refs/tags/v1.0.0\n"
                    "b refs/tags/v1.0.1\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade_check(repo_root=repo, runtime_root=runtime, cache_root=cache_root, run_cmd=_run_cmd)

    assert out["ok"] is True
    assert out["latest_version"] == "1.0.1"
    assert out["version_check"]["source"] == "latest_from_cache"
    assert out["version_check"]["cache_fetched"] is True
    assert any(command[:3] == ["git", f"--git-dir={cache_repo}", "for-each-ref"] for command, _cwd in calls)

    assert http_calls[0][0] == "https://api.github.com/repos/cached/repository/releases/tags/v1.0.1"

def test_service_upgrade_check_reports_no_upgrade_available(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_check

    repo = tmp_path / "releases" / "1.0.1"
    _write_upgrade_release_skeleton(repo, "1.0.1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)

    _run_cmd = _fake_upgrade_release_runner(tags=("1.0.1",), materialize=False)

    out = service_upgrade_check(repo_root=repo, runtime_root=runtime, cache_root=cache_root, run_cmd=_run_cmd)

    assert out["ok"] is True
    assert out["status"] == "no_upgrade_available"
    assert out["upgrade_available"] is False
    assert out["current_version"] == "1.0.1"
    assert out["latest_version"] == "1.0.1"
    assert out["message"] == "没有可升级版本。当前已是最新版本 1.0.1"

def test_service_upgrade_confirm_uses_cached_remote_when_current_release_has_no_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = install / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", f"--git-dir={cache_repo}", "for-each-ref"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "a refs/tags/v1.0.0\n"
                    "b refs/tags/v1.0.1\n"
                ),
                stderr="",
            )
        if command[:3] == ["git", "config", "--get"]:
            if kwargs.get("cwd") == str(cache_repo):
                return subprocess.CompletedProcess(command, 0, stdout="https://github.com/example/options-monitor.git\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="fatal: not a git repository")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "config"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/example/options-monitor.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        cache_root=cache_root,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert current.resolve() == (releases / "1.0.1").resolve()
    assert ["git", f"--git-dir={cache_repo}", "config", "--get", "remote.origin.url"] in calls
    assert ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", "origin"] in calls
    assert not (releases / "1.0.1" / ".git").exists()

def test_service_upgrade_confirm_noops_when_latest_is_current(tmp_path: Path) -> None:
    from src.application.service_upgrade import load_upgrade_status, service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v101 = releases / "1.0.1"
    _write_upgrade_release_skeleton(v101, "1.0.1")
    current = install / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(v101, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = install / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls: list[list[str]] = []

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        materialize=False,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        cache_root=cache_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is True
    assert out["status"] == "already_current"
    assert out["changed"] is False
    assert out["message"] == "没有可升级版本。当前已是最新版本 1.0.1"
    assert current.resolve() == v101.resolve()
    assert not any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in calls)
    assert load_upgrade_status(runtime_root=runtime)["message"] == "没有可升级版本。当前已是最新版本 1.0.1"

def test_service_upgrade_bot_prepare_failure_keeps_current_and_precedes_config(monkeypatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as service_upgrade_module

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    install = tmp_path / "install"
    releases = install / "releases"
    previous = releases / "1.0.0"
    target = releases / "1.0.1"
    _write_upgrade_release_skeleton(previous, "1.0.0")
    _write_upgrade_release_skeleton(target, "1.0.1")
    current = install / "current"
    current.symlink_to(previous, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        service_upgrade_module,
        "service_upgrade_check",
        lambda **_kwargs: {
            "ok": True,
            "latest_version": "1.0.1",
            "release_tag": "v1.0.1",
        },
    )
    config_prepare_called = False

    def _prepare_config(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal config_prepare_called
        config_prepare_called = True
        return {"status": "prepared"}

    monkeypatch.setattr(service_upgrade_module, "_prepare_runtime_configs_for_release", _prepare_config)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[1:] == ["-c", "import src.application.bot.host; import src.application.bot.runtime"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Bot import failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade_module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == previous.resolve()
    assert config_prepare_called is False
    assert "import src.application.bot.host" in out["error"]
    assert out["operations"][-1]["returncode"] == 1
    assert out["operations"][-1]["stderr"] == "Bot import failed\n"
    assert out["operations"][-1]["command"] == [str(target / ".venv/bin/python"), "-c",
                                                "import src.application.bot.host; import src.application.bot.runtime"]
    assert out["operations"][-1]["ok"] is False
    assert out["runtime_prepare"]["duration_seconds"] >= 0

def test_service_upgrade_restart_failure_is_non_success_and_restores_previous_symlink(monkeypatch, tmp_path: Path) -> None:
    import pwd

    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v100 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "deploy_user": pwd.getpwuid(os.geteuid()).pw_name,
                "restart": {
                    "requires_sudo": True,
                    "command_prefix": ["sudo", "-n", "systemctl"],
                    "services": [
                        "options-monitor-trade-intake.service",
                        "options-monitor-feishu-ws.service",
                    ],
                },
                "services": [
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
                "feishu_ws": {"enabled": True, "config_key": "us"},
            }
        ),
        encoding="utf-8",
    )

    _run_cmd = _fake_upgrade_release_runner(
        tags=("1.0.1",),
        clone_release=_write_cloned_release_with_server_deps,
        tail=lambda command: (
            subprocess.CompletedProcess(command, 1, stdout="", stderr="Access denied\n")
            if command[:4] == ["sudo", "-n", "systemctl", "restart"]
            else None
        ),
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        auto=True,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is False
    assert out["status"] == "upgraded_restart_failed"
    assert out["changed"] is True
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert out["compensation"]["symlink_restored"] is True
    assert out["restart_failed_services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert "manual_restart: sudo systemctl restart options-monitor-feishu-ws.service" in out["manual_remediation"]
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "upgraded_restart_failed"
    assert status["restart_failed_services"] == out["restart_failed_services"]

def test_service_upgrade_requires_yaml_authoring_source_before_switch(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v100 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (v100 / "configs").mkdir()
    for name in ("user.common.json", "user.hk.json", "user.us.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    us_runtime = runtime / "config.us.json"
    hk_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": "configs/user.common.json"},
                        {"role": "market_user", "loaded": True, "path": "configs/user.hk.json"},
                    ]
                },
                "inbound": {"feishu_ws": {"ack_reaction": "SMILE"}},
            }
        ),
        encoding="utf-8",
    )
    us_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": "configs/user.common.json"},
                        {"role": "market_user", "loaded": True, "path": "configs/user.us.json"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk", "us"],
                "config_paths": {"hk": str(hk_runtime), "us": str(us_runtime)},
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        clone_release=_write_cloned_release_with_configs,
        tail=_fake_legacy_config_build,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert out["remediation"] == [
        "rerender_service_profile: ./om service render ... --config-yaml <path>",
    ]
    assert not any(command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"] for command in calls)
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] not in calls

def test_service_upgrade_missing_user_config_fails_before_switch_with_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v100 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (v100 / "configs").mkdir()
    (v100 / "configs" / "user.common.json").write_text("{}", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(runtime / "config.hk.json")},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        clone_release=lambda target: _write_cloned_release_with_configs(target, system_json=False),
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert out["remediation"][0] == "rerender_service_profile: ./om service render ... --config-yaml <path>"
    assert not any(command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"] for command in calls)
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] not in calls

def test_service_upgrade_does_not_recover_legacy_user_configs_from_older_release(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v090 = releases / "0.9.0"
    _write_upgrade_release_skeleton(v090, "0.9.0")
    for name in ("user.common.json", "user.hk.json", "user.us.json"):
        (v090 / "configs" / name).write_text(json.dumps({"source": "0.9.0", "name": name}), encoding="utf-8")
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    us_runtime = runtime / "config.us.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk", "us"],
                "config_paths": {"hk": str(hk_runtime), "us": str(us_runtime)},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, kwargs: calls.append({"command": command, "cwd": kwargs.get("cwd")}),
        tags=("1.0.1",),
        clone_release=lambda target: _write_upgrade_release_skeleton(target, "1.0.1"),
        tail=_fake_legacy_config_build,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert not any(
        call["command"][:6] == ["./om", "config", "build", "--source", "legacy", "--market"]
        for call in calls
    )

def test_service_upgrade_ignores_runtime_legacy_overlay_dir(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v090 = releases / "0.9.0"
    _write_upgrade_release_skeleton(v090, "0.9.0")
    for name in ("user.common.json", "user.hk.json"):
        (v090 / "configs" / name).write_text(json.dumps({"source": "older", "name": name}), encoding="utf-8")
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime_configs = runtime / "configs"
    runtime_configs.mkdir()
    for name in ("user.common.json", "user.hk.json"):
        (runtime_configs / name).write_text(json.dumps({"source": "runtime", "name": name}), encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    _run_cmd = _fake_upgrade_release_runner(
        tags=("1.0.1",),
        clone_release=lambda target: _write_upgrade_release_skeleton(target, "1.0.1"),
        tail=_fake_legacy_config_build,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()

def test_service_upgrade_ignores_runtime_config_legacy_metadata_overlay_source(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    authoring = tmp_path / "authoring"
    authoring.mkdir()
    common_source = authoring / "user.common.json"
    market_source = authoring / "user.hk.json"
    common_source.write_text(json.dumps({"source": "metadata", "name": "user.common.json"}), encoding="utf-8")
    market_source.write_text(json.dumps({"source": "metadata", "name": "user.hk.json"}), encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    hk_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": str(common_source)},
                        {"role": "market_user", "loaded": True, "path": str(market_source)},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    _run_cmd = _fake_upgrade_release_runner(
        tags=("1.0.1",),
        clone_release=lambda target: _write_upgrade_release_skeleton(target, "1.0.1"),
        tail=_fake_legacy_config_build,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()

def test_service_upgrade_rebuild_failure_fails_before_switch_with_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    for name in ("user.common.json", "user.hk.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "config_authoring": {
                    "source": "yaml",
                    "config_yaml": str(config_yaml),
                    "markets": ["hk"],
                },
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        clone_release=lambda target: _write_upgrade_release_skeleton(target, "1.0.1"),
        tail=lambda command: (
            subprocess.CompletedProcess(command, 1, stdout="", stderr="build failed")
            if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "hk"]
            else None
        ),
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert any(item.startswith("manual_rebuild: ") for item in out["remediation"])
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] not in calls

def test_service_upgrade_uses_yaml_authoring_source_for_runtime_rebuild(tmp_path: Path) -> None:
    from src.application.config_yaml import build_yaml_runtime_config_file
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
markets:
  hk:
    accounts: [lx]
    symbols: ["0700.HK"]
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
        encoding="utf-8",
    )
    hk_runtime = runtime / "config.hk.json"
    us_runtime = runtime / "config.us.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk", "us"],
                "config_paths": {"hk": str(hk_runtime), "us": str(us_runtime)},
                "config_authoring": {
                    "source": "yaml",
                    "config_yaml": str(config_yaml),
                    "markets": ["hk", "us"],
                },
                "services": [],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _tail(command):
        if command[:6] == ["./om", "config", "build", "--source", "yaml", "--market"]:
            output_path = Path(command[-1])
            assert not output_path.exists()
            build_yaml_runtime_config_file(
                repo_root=Path(__file__).resolve().parents[1],
                market=command[6],
                config_path=command[8],
                output_config_path=output_path,
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"built {command[6]}\n",
                stderr="",
            )
        if command[:4] == ["./om", "config", "build-bot", "--source"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="legacy build should not run")
        return None

    _run_cmd = _fake_upgrade_release_runner(
        lambda command, _kwargs: calls.append(command),
        tags=("1.0.1",),
        tail=_tail,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["runtime_config_prepare"]["overlays"] == []
    assert out["runtime_config_prepare"]["preserved_hotfixes"] == []
    artifacts = out["runtime_config_prepare"]["artifacts"]
    artifact_by_kind_market = {(item["kind"], item.get("market")): item for item in artifacts}
    assert artifact_by_kind_market[("runtime_config", "hk")]["live_path"] == str(hk_runtime)
    assert artifact_by_kind_market[("runtime_config", "us")]["live_path"] == str(us_runtime)
    assert artifact_by_kind_market[("bot_config", None)]["live_path"] == str(
        runtime / "resolved" / "config.bot.json"
    )
    assert hk_runtime.exists()
    assert us_runtime.exists()
    for runtime_config in (hk_runtime, us_runtime):
        payload = json.loads(runtime_config.read_text(encoding="utf-8"))
        assert '"output_mode"' not in json.dumps(payload, sort_keys=True)
    assert (runtime / "resolved" / "config.bot.json").exists()
    assert not (releases / "1.0.1" / "configs" / "user.hk.json").exists()
    refreshed_profile = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert refreshed_profile["config_authoring"]["config_yaml"] == str(config_yaml)

def test_service_upgrade_post_switch_config_validation_failure_restores_symlink_and_bundle(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")
    us_runtime = runtime / "config.us.json"
    bot_runtime = runtime / "resolved" / "config.bot.json"
    bot_runtime.parent.mkdir()
    us_runtime.write_text('{"generation": "old"}\n', encoding="utf-8")
    bot_runtime.write_text('{"generation": "old-assistant"}\n', encoding="utf-8")
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["us"],
                "config_paths": {"us": str(us_runtime)},
                "config_authoring": {
                    "source": "yaml",
                    "config_yaml": str(config_yaml),
                    "markets": ["us"],
                },
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        child = _fake_release_drift_command(list(command), run_cmd=_run_cmd)
        if child is not None:
            return child
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "us"]:
            assert json.loads(us_runtime.read_text(encoding="utf-8")) == {"generation": "old"}
            Path(command[-1]).write_text('{"generation": "new"}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        if command[:4] == ["./om", "config", "build-bot", "--source"]:
            assert json.loads(bot_runtime.read_text(encoding="utf-8")) == {"generation": "old-assistant"}
            Path(command[-1]).write_text('{"generation": "new-assistant"}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        if command[:4] == ["./om", "config", "validate", "--config-path"] and command[4] == str(us_runtime) and current.resolve().name == "1.0.1":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="target validation failed")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is False
    assert out["status"] == "upgrade_failed_rolled_back"
    assert out["rolled_back"] is True
    assert out["changed"] is False
    assert current.resolve() == v100.resolve()
    assert json.loads(us_runtime.read_text(encoding="utf-8")) == {"generation": "old"}
    assert json.loads(bot_runtime.read_text(encoding="utf-8")) == {"generation": "old-assistant"}

def test_service_upgrade_blocks_major_by_default(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (repo / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        target_version="2.0.0",
        run_cmd=_fake_upgrade_release_runner(materialize=False),
    )

    assert out["status"] == "blocked_major_upgrade"
    assert out["changed"] is False

def test_service_upgrade_dry_run_warns_when_repo_root_is_not_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (repo / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        query = _fake_release_target_query(list(command))
        if query is not None:
            return query
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        target_version="1.0.1",
        run_cmd=_run_cmd,
    )

    assert out["status"] == "dry_run"
    assert out["repo_root_is_symlink"] is False
    assert out["warnings"] == ["confirmed upgrade requires repo_root to be a current symlink"]

def test_service_upgrade_confirm_fails_fast_when_repo_root_is_not_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    releases = tmp_path / "releases"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (repo / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        query = _fake_release_target_query(list(command))
        if query is not None:
            return query
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "repo_root_not_symlink"
    assert out["changed"] is False
    assert not releases.exists()
    assert not any(command[:2] == ["git", "clone"] for command in calls)
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "repo_root_not_symlink"

def test_service_upgrade_coerces_release_entity_repo_root_to_current_symlink(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "repo_root": str(current),
                "runtime_root": str(runtime),
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        drift = _fake_release_drift_command(list(command), run_cmd=_run_cmd)
        if drift is not None:
            return drift
        return _fake_systemd_query_runner()(command, **kwargs)

    out = service_upgrade(
        repo_root=v100,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["repo_root"] == str(current)
    assert out["repo_root_is_symlink"] is True
    assert out["repo_root_resolution"]["coerced"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()

def test_service_upgrade_cleanup_after_success_deletes_older_releases(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v080 = releases / "0.8.0"
    v090 = releases / "0.9.0"
    v100 = releases / "1.0.0"
    for release in (v080, v090, v100):
        _write_upgrade_release_skeleton(release, release.name)
    for name in ("user.common.json", "user.hk.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    downloads = install / "_downloads"
    downloads.mkdir()
    (downloads / "old.tar.gz").write_text("cache", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "config_authoring": {
                    "source": "yaml",
                    "config_yaml": str(config_yaml),
                    "markets": ["hk"],
                },
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    def _tail(command):
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "hk"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        if command[:4] == ["./om", "config", "build-bot", "--source"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        return None

    _run_cmd = _fake_upgrade_release_runner(
        tags=("1.0.1",),
        clone_release=lambda target: _write_upgrade_release_skeleton(target, "1.0.1"),
        tail=_tail,
    )

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        restart_services=False,
        cleanup_after_upgrade=True,
        cleanup_keep_releases=2,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["symlink_switched"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()
    cleanup = out["post_upgrade_cleanup"]
    assert cleanup["status"] == "cleaned"
    assert {Path(item["path"]).name for item in cleanup["kept_releases"]} == {"1.0.1", "1.0.0"}
    assert (releases / "1.0.1").exists()
    assert v100.exists()
    assert not v090.exists()
    assert not v080.exists()
    assert not downloads.exists()
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["post_upgrade_cleanup"]["status"] == "cleaned"

@pytest.mark.parametrize("child_error", [False, True])
def test_service_rollback_preserves_paused_timer_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    child_error: bool,
) -> None:
    import src.application.service_upgrade as service_upgrade_module

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    _write_upgrade_release_skeleton(v101, "1.0.1")
    current = install / "current"
    current.symlink_to(v101, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = "options-monitor-tick-hk.timer"
    profile = {
        "service_provider": "systemd",
        "services": [{"name": target}],
    }
    (runtime / "service.profile.json").write_text(
        json.dumps(profile), encoding="utf-8"
    )
    drift_calls: list[dict[str, object]] = []
    child_calls: list[list[str]] = []
    transition_events: list[str] = []

    def _service_drift(**kwargs):  # type: ignore[no-untyped-def]
        drift_calls.append(dict(kwargs))
        if not kwargs.get("confirm"):
            transition_events.append("capture")
            return {
                "activation_states": {target: "enabled"},
                "active_states": {target: "inactive"},
            }
        return {
            "summary": {"status": "warn", "ok": True},
            "preserved_activation_units": [target],
        }

    def _prepare_runtime_configs(**_kwargs):  # type: ignore[no-untyped-def]
        transition_events.append("prepare")
        return {"status": "prepared"}

    monkeypatch.setattr(
        service_upgrade_module,
        "_prepare_runtime_configs_for_release",
        _prepare_runtime_configs,
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_commit_prepared_runtime_configs",
        lambda **_kwargs: {"status": "committed"},
    )
    monkeypatch.setattr(
        service_upgrade_module,
        "_validate_committed_runtime_configs",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(service_upgrade_module, "service_drift", _service_drift)

    child_snapshot = {
        "options-monitor-target-only.timer": {
            "activation_state": "disabled", "active_state": "inactive",
        }
    }

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        child_calls.append(list(command))
        if child_error:
            return subprocess.CompletedProcess(command, 2, stdout=json.dumps({
                "tool_name": "om", "ok": False,
                "error": {"message": "target release cannot capture timer activation state"},
            }), stderr="unknown option --preserve-activation-state")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "tool_name": "service.drift", "ok": True,
            "data": {"summary": {"status": "ok", "ok": True}, "preserved_activation_states": child_snapshot},
        }), stderr="")

    if child_error:
        monkeypatch.setattr(
            service_upgrade_module, "_compensate_service_transition",
            lambda **_kwargs: {"ok": False, "remediation": []},
        )

    out = service_upgrade_module.service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        to_version="1.0.0",
        confirm=True,
        restart_services=False,
        preserve_activation_state=True,
        run_cmd=_run_cmd,
    )

    confirmed_calls = [item for item in drift_calls if item.get("confirm")]
    assert transition_events[:2] == ["capture", "prepare"]
    assert confirmed_calls == []
    assert child_calls == [[
        str(v100 / "om"), "service", "drift",
        "--repo-root", str(current),
        "--runtime-root", str(runtime),
        "--profile-path", str(runtime / "service.profile.json"),
        "--confirm", "--preserve-activation-state",
    ]]
    assert out["activation_policy"] == "preserve-existing"
    assert out["preserved_activation_units"] == [target]
    if child_error:
        assert out["ok"] is False
        assert out["failure_status"] == "rollback_service_reconcile_failed"
        assert "target release cannot capture" in out["error"]
        assert any("returncode: 2" in item for item in out["remediation"])
        assert any("--preserve-activation-state" in item for item in out["remediation"])
        return

    assert out["status"] == "rolled_back"
    assert current.resolve() == v100.resolve()
    assert out["preserved_activation_state_difference"] == {
        "before_switch": {
            target: {
                "activation_state": "enabled",
                "active_state": "inactive",
            }
        },
        "reconcile": child_snapshot,
    }


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.parametrize("target", [
    "../outside", "/absolute", "1.0.0/../../outside", "1.0.0/child", ".", "..",
    "01.0.0", "1.00.0", "1.0", "1.0.0-", "1.0.0-rc..1", "1.0.0-01",
    "１.0.0", "1.0.0\nextra", "1.0.0+build", "1.0.0\\child",
])
def test_rollback_identity_rejects_invalid_target(transition_case, monkeypatch, target, implicit, confirm):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text(json.dumps({"current_version": target if implicit else "1.0.0"}))
    session = case.runtime / "output_shared/state/pi_sessions.sqlite3"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"isolated-session-sentinel")
    before = case.snapshot()

    def unexpected(**kwargs):
        pytest.fail("invalid target reached transition work")

    monkeypatch.setattr(upgrade, "_prepare_runtime_configs_for_release", unexpected)
    monkeypatch.setattr(upgrade, "_runtime_readiness", unexpected)
    monkeypatch.setattr(upgrade, "_compensate_service_transition", unexpected)
    out = case.invoke("rollback", None if implicit else target, confirm=confirm)
    data = out["data"]
    assert out["ok"] is False
    assert data["status"] == "rollback_target_invalid"
    assert data["target_version"] == target
    assert data["changed"] is False and data["operations"] == []
    assert data["dry_run"] is (not confirm) and data["write_applied"] is False
    assert data["error"] and data["remediation"]
    after = case.snapshot()
    assert after[:3] == before[:3] and after[4:] == before[4:]
    assert session.read_bytes() == b"isolated-session-sentinel"
    assert case.commands == [] and case.queries == [] and case.child_requests == []
    assert not (case.runtime / "upgrade_staging").exists() and not case.pid.exists()
    if confirm:
        assert json.loads(case.status.read_text())["status"] == "rollback_target_invalid"
    else:
        assert after == before and not case.guard.exists()


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("fault", [
    "outside", "outside_missing", "sibling_prefix", "loop", "root", "file",
    "missing_version", "empty_version", "mismatch", "prefixed", "noncanonical",
    "multiline", "invalid_utf8", "permission", "io_error",
])
def test_rollback_identity_rejects_path_and_version_failures(transition_case, monkeypatch, fault, confirm):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    target = case.releases / "3.0.0"
    if fault in {"outside", "outside_missing", "sibling_prefix"}:
        outside = case.root / ("releases-extra" if fault == "sibling_prefix" else "outside")
        if fault != "outside_missing":
            outside.mkdir()
            (outside / "VERSION").write_text("3.0.0\n")
            (outside / "src/application/bot").mkdir(parents=True, exist_ok=True)
            (outside / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
        target.symlink_to(outside, target_is_directory=True)
    elif fault == "loop":
        target.symlink_to(target)
    elif fault == "root":
        target.symlink_to(case.releases, target_is_directory=True)
    elif fault == "file":
        target.write_bytes(b"not a directory")
    else:
        target.mkdir()
        if fault != "missing_version":
            content = {
                "empty_version": b"", "mismatch": b"3.0.1\n", "prefixed": b"v3.0.0\n",
                "noncanonical": b"03.0.0\n", "multiline": b"3.0.0\nother\n",
                "invalid_utf8": b"\xff", "permission": b"3.0.0\n", "io_error": b"3.0.0\n",
            }[fault]
            (target / "VERSION").write_bytes(content)
    before = case.snapshot()
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == target / "VERSION":
            if fault == "permission":
                raise PermissionError("synthetic VERSION access failure")
            if fault == "io_error":
                raise OSError("synthetic VERSION read failure")
        if path.name == "VERSION":
            assert path.parent == case.current.resolve() or path.parent.is_relative_to(case.releases)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    out = case.invoke("rollback", "3.0.0", confirm=confirm)
    assert out["ok"] is False
    data = out["data"]
    assert data["status"] == "rollback_target_invalid"
    assert data["changed"] is False and data["operations"] == []
    assert data["error"] and data["remediation"]
    after = case.snapshot()
    assert after[:3] == before[:3] and after[4:] == before[4:]
    assert case.commands == [] and case.child_requests == [] and not case.pid.exists()
    assert not (case.runtime / "upgrade_staging").exists()
    if not confirm:
        assert after == before and not case.guard.exists()


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.parametrize("target", ["1.0.0", "0.0.0", "1.0.0-rc.1", "1.0.0-0", "1.0.0-01a", " v1.0.0 "])
def test_rollback_identity_admits_supported_versions(transition_case, target, implicit, confirm):
    case = transition_case()
    version = target.strip().removeprefix("v")
    release = case.releases / version
    _write_upgrade_release_skeleton(release, version)
    case.status.write_text(json.dumps({"current_version": target}))
    before = case.snapshot()
    out = case.invoke("rollback", None if implicit else target, confirm=confirm)
    data = out["data"]
    assert out["ok"] is True
    assert data["target_version"] == version and data["target_dir"] == str(release)
    assert data["status"] == ("rolled_back" if confirm else "dry_run")
    assert data["changed"] is confirm
    assert not case.pid.exists()
    if confirm:
        assert case.current.resolve() == release
        assert data["runtime_config_commit"]["status"] == "committed"
        assert any(command[0] == str(release / "om") for command in case.commands)
    else:
        assert case.snapshot() == before and case.commands == [] and not case.guard.exists()


@pytest.mark.parametrize("confirm", [False, True])
def test_rollback_identity_uses_admitted_in_root_alias(transition_case, confirm):
    case = transition_case()
    admitted = case.releases / "release-storage"
    (case.releases / "1.0.0").rename(admitted)
    (case.releases / "1.0.0").symlink_to(admitted, target_is_directory=True)
    out = case.invoke("rollback", "1.0.0", confirm=confirm)["data"]
    assert out["ok"] is True and out["target_dir"] == str(admitted)
    if confirm:
        assert case.current.readlink() == admitted
        assert out["runtime_config_prepare"]["staging_root"].endswith("1.0.1-to-release-storage")
        assert any(command[0] == str(admitted / "om") for command in case.commands)


@pytest.mark.parametrize("target", ["../outside", "9.9.9", "1.0.0"])
def test_rollback_identity_busy_skips_admission(transition_case, monkeypatch, target):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status":"holder-sentinel"}\n')
    before = case.snapshot()
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "VERSION":
            assert path.parent == case.current.resolve(), "busy contender read target VERSION"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
        identity = case.guard.stat().st_ino
        data = case.invoke("rollback", target)["data"]
        assert data["status"] == data["failure_status"] == "rollback_failed"
        assert "upgrade lock already exists" in data["error"]
        assert not case.pid.exists() and case.guard.stat().st_ino == identity
        assert case.snapshot() == before and case.commands == []
    assert not case.pid.exists()


def test_rollback_identity_failure_receipt_error_releases_lock_and_retry(transition_case, monkeypatch):
    from src.application import service_upgrade as upgrade
    from src.application.agent_tool_contracts import AgentToolError

    case = transition_case()
    writer = upgrade.write_upgrade_status
    before = case.snapshot()
    error = OSError("synthetic failure receipt write")

    def fail(**kwargs):
        assert kwargs["payload"]["status"] == "rollback_target_invalid"
        with pytest.raises(AgentToolError, match="fixture guard busy") as busy:
            with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
                pytest.fail("failure receipt writer did not hold guard")
        assert busy.value.code == "RESOURCE_BUSY"
        assert busy.value.details == {"lock_path": str(case.guard)}
        raise error

    monkeypatch.setattr(upgrade, "write_upgrade_status", fail)
    with pytest.raises(OSError) as caught:
        case.invoke("rollback", "../outside")
    assert caught.value is error
    assert case.snapshot() == before and case.commands == [] and not case.pid.exists()
    monkeypatch.setattr(upgrade, "write_upgrade_status", writer)
    assert case.invoke("rollback", "1.0.0")["data"]["status"] == "rolled_back"


def test_service_rollback_switches_current_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_rollback, write_upgrade_status

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v100.mkdir(parents=True)
    v101.mkdir()
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v100 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (v101 / "VERSION").write_text("1.0.1\n", encoding="utf-8")
    (v101 / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (v101 / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v101, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    session_db = runtime / "output_shared" / "state" / "pi_sessions.sqlite3"
    session_db.parent.mkdir(parents=True)
    session_sentinel = b"SQLite format 3\x00pi-session-sentinel\xff"
    session_db.write_bytes(session_sentinel)
    write_upgrade_status(
        runtime_root=runtime,
        payload={"status": "upgraded", "current_version": "1.0.0", "target_version": "1.0.1"},
    )

    dry = service_rollback(repo_root=current, runtime_root=runtime, releases_root=releases)
    assert dry["status"] == "dry_run"
    assert current.resolve() == v101.resolve()
    assert session_db.read_bytes() == session_sentinel

    out = service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
    )
    assert out["status"] == "rolled_back"
    assert current.resolve() == v100.resolve()
    assert session_db.read_bytes() == session_sentinel

def test_service_rollback_rebuilds_and_commits_target_runtime_config_bundle(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_rollback

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    _write_upgrade_release_skeleton(v101, "1.0.1")
    current = install / "current"
    current.symlink_to(v101, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")
    us_runtime = runtime / "config.us.json"
    us_runtime.write_text('{"release": "1.0.1"}\n', encoding="utf-8")
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["us"],
                "config_paths": {"us": str(us_runtime)},
                "config_authoring": {
                    "source": "yaml",
                    "config_yaml": str(config_yaml),
                    "markets": ["us"],
                },
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    drift_commands: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "us"]:
            assert current.resolve() == v101.resolve()
            assert json.loads(us_runtime.read_text(encoding="utf-8")) == {"release": "1.0.1"}
            Path(command[-1]).write_text('{"release": "1.0.0"}\n', encoding="utf-8")
        elif command[:4] == ["./om", "config", "build-bot", "--source"]:
            Path(command[-1]).write_text('{"release": "1.0.0"}\n', encoding="utf-8")
        elif command[:3] == [str(v100 / "om"), "service", "drift"]:
            drift_commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                "tool_name": "service.drift", "ok": True,
                "data": {"summary": {"status": "ok", "ok": True}},
            }), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        to_version="1.0.0",
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is True
    assert out["status"] == "rolled_back"
    assert current.resolve() == v100.resolve()
    assert json.loads(us_runtime.read_text(encoding="utf-8")) == {"release": "1.0.0"}
    assert out["runtime_config_commit"]["status"] == "committed"
    assert drift_commands == [[
        str(v100 / "om"), "service", "drift",
        "--repo-root", str(current),
        "--runtime-root", str(runtime),
        "--profile-path", str(runtime / "service.profile.json"),
        "--confirm",
    ]]


def test_runtime_status_loads_service_profile_paths(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool
    from src.application.service_deploy import render_service_bundle

    cfg_path = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_config)},
                "notifications": {"provider": "wechat_clawbot", "target": "route"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    bundle = _render_bundle(tmp_path, tmp_path, accounts=["lx"], markets=["us"], config_paths={"us": cfg_path})
    _write_systemd_units_from_bundle(bundle, systemd_root)
    profile_path = tmp_path / "service.profile.json"
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["paths"] = {
        "report_dir": str(tmp_path / "output_shared" / "reports"),
        "state_dir": str(tmp_path / "output_shared" / "state"),
        "shared_state_dir": str(tmp_path / "output_shared" / "state"),
        "accounts_root": str(tmp_path / "output_accounts"),
        "runs_root": str(tmp_path / "output_runs"),
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    out = execute_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["lx"]
    assert out["data"]["service_profile"]["loaded"] is True
    assert out["data"]["service_profile"]["provider"] == "systemd"
    assert out["data"]["service_drift"]["summary"]["status"] == "ok"

def test_runtime_status_warns_when_required_service_timer_is_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool
    from src.application.service_deploy import render_service_bundle

    cfg_path = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}", encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_config)},
                "notifications": {"provider": "wechat_clawbot", "target": "route"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    bundle = _render_bundle(tmp_path, tmp_path, accounts=["lx"], markets=["us"], config_paths={"us": cfg_path})
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    out = execute_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    assert "SERVICE_DRIFT_REQUIRED_UNIT_MISSING" in out["data"]["summary"]["warning_codes"]
    assert out["data"]["service_drift"]["summary"]["missing_required_count"] == 1
    assert "missing_required_units" not in out["data"]["service_drift"]

def test_service_cleanup_dry_run_reports_releases_and_caches(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
        (release / "payload.txt").write_text(release.name, encoding="utf-8")
    internal_cache = releases / "_cache"
    internal_cache.mkdir()
    current = apps / "current"
    current.symlink_to(v102, target_is_directory=True)
    downloads = apps / "_downloads"
    downloads.mkdir()
    (downloads / "release.tar.gz").write_text("download-cache", encoding="utf-8")

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        cleanup_downloads=True,
    )

    assert out["ok"] is True
    assert out["status"] == "dry_run"
    assert out["changed"] is False
    assert out["active_release"] == str(v102.resolve())
    assert [item["version"] for item in out["kept_releases"]] == ["1.0.2", "1.0.1"]
    assert [Path(item["path"]).name for item in out["delete_releases"]] == ["1.0.0"]
    assert out["cache_dirs"][0]["path"] == str(downloads)
    assert out["estimated_freed_bytes"] > 0
    assert out["freed_bytes"] == 0
    assert out["deleted_paths"] == []
    assert v100.exists()
    assert internal_cache.exists()
    assert downloads.exists()
    repeated = service_cleanup(repo_root=current, releases_root=releases, cleanup_downloads=True)
    assert repeated["status"] == "dry_run"
    assert repeated["deleted_paths"] == []
    assert repeated["delete_releases"] == out["delete_releases"]

def test_service_cleanup_confirm_deletes_only_old_releases_and_selected_caches(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
    internal_cache = releases / "_cache"
    internal_cache.mkdir()
    current = apps / "current"
    current.symlink_to(v102, target_is_directory=True)
    downloads = apps / "_downloads"
    downloads.mkdir()
    (downloads / "release.tar.gz").write_text("download-cache", encoding="utf-8")

    out = service_cleanup(
        runtime_root=tmp_path / "runtime",
        repo_root=current,
        releases_root=releases,
        cleanup_downloads=True,
        confirm=True,
    )

    assert out["ok"] is True
    assert out["status"] == "cleaned"
    assert out["changed"] is True
    assert v102.exists()
    assert v101.exists()
    assert not v100.exists()
    assert internal_cache.exists()
    assert not downloads.exists()
    assert str(v100) in out["deleted_paths"]
    assert str(downloads) in out["deleted_paths"]
    assert out["freed_bytes"] == out["estimated_freed_bytes"]

def test_service_cleanup_keeps_active_release_even_when_it_is_not_newest(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
    current = apps / "current"
    current.symlink_to(v100, target_is_directory=True)

    out = service_cleanup(
        runtime_root=tmp_path / "runtime",
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
    )

    kept = {Path(item["path"]).name for item in out["kept_releases"]}
    assert kept == {"1.0.0", "1.0.2"}
    assert v100.exists()
    assert v102.exists()
    assert not v101.exists()

def test_service_cleanup_dry_run_reports_expired_output_runs_and_protects_latest_pointer(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    active = releases / "1.0.2"
    old = releases / "1.0.1"
    for release in (active, old):
        _write_upgrade_release_skeleton(release, release.name)
    current = apps / "current"
    current.symlink_to(active, target_is_directory=True)

    runtime = tmp_path / "runtime"
    runs_root = runtime / "output_runs"
    state_root = runtime / "output_shared" / "state"
    state_root.mkdir(parents=True)

    def _run(name: str, ts: int) -> Path:
        run = runs_root / name
        state = run / "state"
        state.mkdir(parents=True)
        (state / "audit_events.jsonl").write_text("x" * 32, encoding="utf-8")
        os.utime(state / "audit_events.jsonl", (ts, ts))
        os.utime(state, (ts, ts))
        os.utime(run, (ts, ts))
        return run

    new_run = _run("run-new", 1_780_000_000)
    keep_count_run = _run("run-keep-count", 1_779_900_000)
    delete_run = _run("run-delete", 1_700_000_000)
    pointer_run = _run("run-pointer", 1_690_000_000)
    (state_root / "last_run_dir.txt").write_text(str(pointer_run), encoding="utf-8")

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        runtime_root=runtime,
        cleanup_output_runs=True,
        output_runs_keep_days=14,
        output_runs_keep_count=2,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    cleanup = out["output_runs_cleanup"]
    assert out["status"] == "dry_run"
    assert cleanup["scanned_count"] == 4
    assert [Path(item["path"]).name for item in cleanup["delete_runs"]] == ["run-delete"]
    protected = {Path(item["path"]).name: item["reason"] for item in cleanup["protected_runs"]}
    assert protected[new_run.name] == "latest_keep_count"
    assert protected[keep_count_run.name] == "latest_keep_count"
    assert protected[pointer_run.name] == "last_run_dir_pointer"
    assert delete_run.exists()

def test_service_cleanup_confirm_deletes_expired_output_runs_and_runtime_logs(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    active = releases / "1.0.2"
    old = releases / "1.0.1"
    for release in (active, old):
        _write_upgrade_release_skeleton(release, release.name)
    current = apps / "current"
    current.symlink_to(active, target_is_directory=True)

    runtime = tmp_path / "runtime"
    runs_root = runtime / "output_runs"
    logs_root = runtime / "logs"
    logs_root.mkdir(parents=True)

    def _run(name: str, ts: int) -> Path:
        run = runs_root / name
        state = run / "state"
        state.mkdir(parents=True)
        (state / "audit_events.jsonl").write_text("x" * 32, encoding="utf-8")
        os.utime(state / "audit_events.jsonl", (ts, ts))
        os.utime(state, (ts, ts))
        os.utime(run, (ts, ts))
        return run

    kept_run = _run("run-kept", 1_780_000_000)
    expired_run = _run("run-expired", 1_700_000_000)
    old_log = logs_root / "old.log"
    new_log = logs_root / "new.log"
    audit_file = logs_root / "audit.jsonl"
    for path in (old_log, new_log, audit_file):
        path.write_text(path.name, encoding="utf-8")
    os.utime(old_log, (1_700_000_000, 1_700_000_000))
    os.utime(new_log, (1_780_000_000, 1_780_000_000))
    os.utime(audit_file, (1_700_000_000, 1_700_000_000))

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        runtime_root=runtime,
        keep_releases=2,
        cleanup_output_runs=True,
        output_runs_keep_days=14,
        output_runs_keep_count=1,
        cleanup_runtime_logs=True,
        runtime_logs_keep_days=14,
        confirm=True,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    assert out["status"] == "cleaned"
    assert out["changed"] is True
    assert kept_run.exists()
    assert not expired_run.exists()
    assert not old_log.exists()
    assert new_log.exists()
    assert audit_file.exists()
    assert {item["kind"] for item in out["operations"]} >= {"output_run", "runtime_log"}

def test_service_cleanup_requires_repo_root_symlink(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (repo / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")

    out = service_cleanup(repo_root=repo, releases_root=tmp_path / "releases", confirm=True, runtime_root=tmp_path / "runtime")

    assert out["ok"] is False
    assert out["status"] == "repo_root_not_symlink"

def test_service_cleanup_reports_journal_command_failure(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    releases = tmp_path / "releases"
    active = releases / "1.2.0"
    previous = releases / "1.1.0"
    for path in (active, previous):
        path.mkdir(parents=True)
        (path / "VERSION").write_text(path.name, encoding="utf-8")
        (path / "src/application/bot").mkdir(parents=True, exist_ok=True)
        (path / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    current = tmp_path / "current"
    current.symlink_to(active, target_is_directory=True)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")

    out = service_cleanup(
        runtime_root=tmp_path / "runtime",
        repo_root=current,
        releases_root=releases,
        journal_vacuum_size="256M",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is False
    assert out["status"] == "cleanup_failed"
    assert out["changed"] is False
    assert out["failure_count"] == 1
    assert out["freed_bytes"] == 0
    assert out["changed"] is False

def test_service_credential_migration_dry_run_is_read_only(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_drift import migrate_service_credentials

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    profile_before = (fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8")

    out = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls),
    )

    assert out["ok"] is True
    assert out["status"] == "dry_run"
    assert out["changed"] is False
    assert out["values_exposed"] is False
    assert out["secret_credential_delivery"] == "runtime-files"
    assert out["credential_ids"]
    assert (fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8") == profile_before
    assert fixture["legacy_env"].is_file()
    assert fixture["legacy_helper"].is_file()
    assert not fixture["target_helper"].exists()
    assert not any("systemd-creds" in part for command in calls for part in command)
    assert not list(fixture["runtime"].glob("service.profile.json.pre-credential-migration-*.bak"))

def test_service_credential_migration_preflight_failure_changes_nothing(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_drift import migrate_service_credentials

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    profile_before = (fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8")

    out = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls, decrypt_ok=False),
    )

    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert out["reason"] == "encrypted_credential_preflight_failed"
    assert out["preflight"]["values_exposed"] is False
    assert (fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8") == profile_before
    assert fixture["legacy_env"].is_file()
    assert fixture["legacy_helper"].is_file()
    assert not fixture["target_helper"].exists()
    assert "must-not-be-captured" not in json.dumps(out)

def test_service_credential_migration_retires_legacy_env_after_verified_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import FEISHU_AGENT_CREDENTIAL_SERVICE
    from src.application.service_drift import migrate_service_credentials

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    out = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls),
    )

    profile = json.loads((fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8"))
    assert out["ok"] is True
    assert out["status"] == "migrated"
    assert out["post_restart_drift"]["summary"]["status"] == "ok"
    assert out["final_drift"]["summary"]["status"] == "ok"
    assert out["restart"]["restarted"]
    assert profile["secret_credentials"]["delivery"] == "runtime-files"
    assert "feishu_agent_credential" not in profile
    assert not fixture["legacy_env"].exists()
    assert not fixture["legacy_helper"].exists()
    assert not fixture["compat"].exists()
    assert fixture["target_helper"].is_file()
    assert not (fixture["systemd_root"] / FEISHU_AGENT_CREDENTIAL_SERVICE).exists()
    assert list(fixture["runtime"].glob("service.profile.json.pre-credential-migration-*.bak"))
    assert "must-not-be-captured" not in json.dumps(out)

def test_service_credential_migration_restart_failure_restores_legacy_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import FEISHU_AGENT_CREDENTIAL_SERVICE
    from src.application.service_drift import migrate_service_credentials

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    out = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls, restart_failures=[1]),
    )

    profile = json.loads((fixture["runtime"] / "service.profile.json").read_text(encoding="utf-8"))
    assert out["ok"] is False
    assert out["status"] == "rolled_back"
    assert out["reason"] == "credential_consumer_restart_failed"
    assert out["rollback"]["ok"] is True
    assert profile["feishu_agent_credential"]["enabled"] is True
    assert "secret_credentials" not in profile
    assert fixture["legacy_env"].is_file()
    assert fixture["legacy_helper"].is_file()
    assert not fixture["target_helper"].exists()
    assert (fixture["systemd_root"] / FEISHU_AGENT_CREDENTIAL_SERVICE).is_file()
    legacy_dropins = list(fixture["systemd_root"].glob("options-monitor-*.service.d/zzzz-feishu-agent-credential.conf"))
    assert legacy_dropins
    assert all("Environment=OM_SECRET_BACKEND=env" in path.read_text(encoding="utf-8") for path in legacy_dropins)

def test_service_credential_migration_verifies_target_drift_before_legacy_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import copy

    import src.application.service_drift as service_drift_module

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    real_service_drift = service_drift_module.service_drift
    drift_calls = 0

    def _service_drift_with_post_restart_failure(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal drift_calls
        drift_calls += 1
        result = real_service_drift(**kwargs)
        if drift_calls == 4:
            result = copy.deepcopy(result)
            result["summary"]["ok"] = False
            result["summary"]["status"] = "error"
        return result

    monkeypatch.setattr(
        service_drift_module,
        "service_drift",
        _service_drift_with_post_restart_failure,
    )
    calls: list[list[str]] = []

    out = service_drift_module.migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls),
    )

    assert out["ok"] is False
    assert out["status"] == "rolled_back"
    assert out["reason"] == "post_restart_target_drift_failed"
    assert out["rollback"]["ok"] is True
    assert fixture["legacy_env"].is_file()
    assert fixture["legacy_helper"].is_file()
    assert not fixture["target_helper"].exists()

def test_service_credential_migration_restart_failure_restores_inferred_legacy_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_deploy import FEISHU_AGENT_CREDENTIAL_SERVICE

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    profile_path = fixture["runtime"] / "service.profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile.pop("feishu_agent_credential")
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] != FEISHU_AGENT_CREDENTIAL_SERVICE
    ]
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(
        service_drift_module,
        "LEGACY_FEISHU_AGENT_CREDENTIAL_UNIT_PATH",
        fixture["systemd_root"] / FEISHU_AGENT_CREDENTIAL_SERVICE,
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_FEISHU_AGENT_CREDENTIAL_HELPER",
        fixture["legacy_helper"],
    )
    calls: list[list[str]] = []

    out = service_drift_module.migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls, restart_failures=[1]),
    )

    restored = json.loads(profile_path.read_text(encoding="utf-8"))
    assert out["status"] == "rolled_back"
    assert out["rollback"]["ok"] is True
    assert restored["feishu_agent_credential"]["enabled"] is True
    assert {item["name"] for item in restored["services"]} >= {
        FEISHU_AGENT_CREDENTIAL_SERVICE,
    }
    assert (fixture["systemd_root"] / FEISHU_AGENT_CREDENTIAL_SERVICE).is_file()
    assert fixture["legacy_helper"].is_file()
    assert fixture["legacy_env"].is_file()

def test_service_credential_migration_blocks_cross_delivery_transition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.service_drift import migrate_service_credentials

    fixture = _legacy_credential_migration_fixture(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    first = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls),
    )
    assert first["status"] == "migrated"
    profile_path = fixture["runtime"] / "service.profile.json"
    profile_before = profile_path.read_text(encoding="utf-8")
    backup_count = len(
        list(fixture["runtime"].glob("service.profile.json.pre-credential-migration-*.bak"))
    )
    calls.clear()

    out = migrate_service_credentials(
        repo_root=fixture["repo"],
        runtime_root=fixture["runtime"],
        secret_credential_delivery="load-credential-encrypted",
        secret_credential_store_root=fixture["store"],
        confirm=True,
        systemd_unit_root=fixture["systemd_root"],
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
        run_cmd=_credential_migration_runner(calls),
    )

    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert out["reason"] == "existing_secure_delivery_transition_not_supported"
    assert out["current_secret_credential_delivery"] == "runtime-files"
    assert profile_path.read_text(encoding="utf-8") == profile_before
    assert fixture["target_helper"].is_file()
    assert len(
        list(fixture["runtime"].glob("service.profile.json.pre-credential-migration-*.bak"))
    ) == backup_count
    assert not any("systemd-creds" in part for command in calls for part in command)


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("actual_uid", [0, 2002])
def test_update_wrong_user_is_read_only_at_cli_boundary(monkeypatch, tmp_path, operation, confirm, actual_uid):
    import argparse
    import pwd
    from types import SimpleNamespace

    from src.application import service_upgrade as upgrade
    from src.interfaces.cli.service_ops import add_service_update_commands, handle_service_update_command

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = runtime / "service.profile.json"
    profile.write_text(json.dumps({"deploy_user": "operator"}))
    status = runtime / "upgrade_status.json"
    status.write_text('{"status":"previous_success"}')
    before = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_uid) for p in runtime.iterdir()}
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001))
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: actual_uid)
    monkeypatch.setenv("USER", "operator")  # Environment identity cannot override effective UID.
    monkeypatch.setenv("SUDO_USER", "operator")
    def unexpected(*args, **kwargs):
        pytest.fail("rejected user must not query targets, write status, or acquire a lock")
    monkeypatch.setattr(upgrade, "service_upgrade_check", unexpected)
    monkeypatch.setattr(upgrade, "write_upgrade_status", unexpected)
    monkeypatch.setattr(upgrade, "_UpgradeLock", unexpected)
    parser = argparse.ArgumentParser()
    add_service_update_commands(parser.add_subparsers(dest="command"))
    argv = ["update", operation, "--repo-root", str(tmp_path / "missing-repo"), "--runtime-root", str(runtime)]
    if confirm:
        argv += ["--confirm", "--yes"]
    out = handle_service_update_command(parser.parse_args(argv))
    assert out["ok"] is False
    assert out["data"]["status"] == "deployment_user_check_failed"
    assert "部署用户 operator" in out["data"]["message"]
    assert out["data"]["write_applied"] is False
    assert before == {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_uid) for p in runtime.iterdir()}


@pytest.mark.parametrize("uid", [0, 1001])
def test_deployment_user_check_uses_uid_and_rejects_unknown_user(monkeypatch, tmp_path, uid):
    import pwd
    from types import SimpleNamespace
    from src.application import service_upgrade as upgrade

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "VERSION").write_text("1.0.0")
    (repo / "src/application/bot").mkdir(parents=True, exist_ok=True)
    (repo / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (tmp_path / "service.profile.json").write_text(json.dumps({"deploy_user": "operator"}))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: uid)
    monkeypatch.setattr(upgrade, "service_upgrade_check", lambda **kwargs: {"ok": True, "latest_version": "1.0.0"})
    assert upgrade.service_upgrade(repo_root=repo, runtime_root=tmp_path)["status"] == "already_current"
    before = (tmp_path / "upgrade_status.json").read_bytes()
    def unknown(name):
        raise KeyError(name)
    monkeypatch.setattr(pwd, "getpwnam", unknown)
    assert upgrade.service_upgrade(repo_root=repo, runtime_root=tmp_path)["status"] == "deployment_user_check_failed"
    assert (tmp_path / "upgrade_status.json").read_bytes() == before


def test_preserved_drift_returns_success_through_public_cli(monkeypatch, capsys, tmp_path):
    from functools import partial
    from src.interfaces.cli import main as cli
    from src.application import service_upgrade as upgrade
    from src.application.service_drift import service_drift

    repo, runtime, systemd_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["hk"])
    files = {item["relative_path"]: item for item in bundle["files"]}
    (runtime / "service.profile.json").write_text(files["service.profile.json"]["content"])
    _write_systemd_units_from_bundle(bundle, systemd_root)
    target = "options-monitor-tick-hk.timer"
    commands = []

    def run_cmd(command, **_kwargs):
        commands.append(list(command))
        if "is-active" in command:
            state = "inactive" if command[-1] == target else "active"
            return subprocess.CompletedProcess(command, 3 if state == "inactive" else 0, stdout=state, stderr="")
        if "is-enabled" in command:
            state = "disabled" if command[-1] == target else "enabled"
            return subprocess.CompletedProcess(command, 1 if state == "disabled" else 0, stdout=state, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="success", stderr="")

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    monkeypatch.setattr(cli, "service_drift", partial(service_drift, run_cmd=run_cmd))
    monkeypatch.setattr(cli, "capture_preserved_timer_activation_states",
                        partial(upgrade.capture_preserved_timer_activation_states, run_cmd=run_cmd))
    # Snapshot capture also reaches the real owner, with only system commands faked.
    monkeypatch.setattr(upgrade, "service_drift", partial(service_drift, run_cmd=run_cmd))
    assert cli.main(["service", "drift", "--repo-root", str(repo), "--runtime-root", str(runtime),
                     "--preserve-activation-state", "--confirm"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_name"] == "service.drift" and payload["ok"] is True
    assert payload["data"]["summary"]["warning_count"] == 0
    assert payload["data"]["summary"]["preserved_activation_count"] == 1
    assert payload["data"]["activation_states"][target] == "disabled"
    assert not any(target in cmd and any(action in cmd for action in ("enable", "start", "restart", "unmask"))
                   for cmd in commands)


@pytest.mark.parametrize("legacy", [False, True])
def test_service_drift_preserves_timeout_while_repairing_and_retrying(tmp_path, legacy):
    repo, runtime, units = _drift_roots(tmp_path)
    timeout = 600 if legacy else 1800
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us", "hk"],
                           timeout_seconds=timeout, use_default_deploy_user=False)
    profile = json.loads(next(item["content"] for item in bundle["files"]
                              if item["kind"] == "service_profile"))
    if legacy:
        profile.pop("timeout_seconds", None)
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile))
    _write_systemd_units_from_bundle(bundle, units)
    original = {name: (units / name).read_bytes() for name in
                ("options-monitor-tick-us.service", "options-monitor-tick-hk.service")}
    damaged = units / "options-monitor-projection-verify.timer"
    expected = damaged.read_bytes()
    damaged.write_text("[Timer]\nOnCalendar=daily\n")
    runner = _fake_systemd_query_runner()
    before = _drift_at(repo, runtime, units, run_cmd=runner)
    assert before["mismatched_units"] == [damaged.name]
    after = _drift_at(repo, runtime, units, confirm=True, run_cmd=runner)
    assert after["apply_errors"] == []
    assert after["summary"]["ok"] is True
    assert damaged.read_bytes() == expected
    assert json.loads(profile_path.read_text())["timeout_seconds"] == timeout
    for name, content in original.items():
        assert (units / name).read_bytes() == content
        assert f"--timeout {timeout}" in content.decode()
        assert f"TimeoutStartSec={timeout + 300}" in content.decode()
    repeat = _drift_at(repo, runtime, units, confirm=True, run_cmd=runner)
    assert repeat["summary"]["ok"] is True
    assert repeat["changed"] is False


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("timeout_seconds", [None, True, False, "1800", 1800.5, 0, -1, [], {}])
def test_service_drift_invalid_timeout_has_no_effects(tmp_path, confirm, timeout_seconds):
    repo, runtime, units = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"],
                           use_default_deploy_user=False)
    profile = json.loads(next(item["content"] for item in bundle["files"]
                              if item["kind"] == "service_profile"))
    profile["timeout_seconds"] = timeout_seconds
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile))
    _write_systemd_units_from_bundle(bundle, units)
    profile_before = profile_path.read_bytes()
    units_before = {item.name: item.read_bytes() for item in units.iterdir()}
    calls = []
    runner = _fake_systemd_query_runner(record=lambda command, kwargs: calls.append(command))
    with pytest.raises(ValueError, match="timeout_seconds must be a positive integer"):
        _drift_at(repo, runtime, units, confirm=confirm, run_cmd=runner)
    assert profile_path.read_bytes() == profile_before
    assert {item.name: item.read_bytes() for item in units.iterdir()} == units_before
    assert calls == []


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("timeout_seconds", [1800, 0])
def test_service_timeout_public_wrapper(tmp_path, target, timeout_seconds):
    import sys

    repo = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = runtime / "config.yaml"
    source.write_text("accounts: [lx]\n")
    output = tmp_path / "rendered"
    result = subprocess.run([
        str(repo / "om"), "service", "render", "--target", target,
        "--runtime-root", str(runtime), "--config-yaml", str(source),
        "--accounts", "lx", "--markets", "us", "--timeout", str(timeout_seconds),
        "--output-dir", str(output),
    ], cwd=tmp_path, env={"PATH": os.environ["PATH"], "OM_PYTHON": sys.executable,
                         "OM_RUNTIME_ROOT": str(runtime)},
        capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout)
    if timeout_seconds == 0:
        assert result.returncode != 0
        assert payload["ok"] is False
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert "timeout_seconds" in payload["error"]["message"]
        assert not output.exists()
        return
    assert result.returncode == 0, result.stderr + result.stdout
    assert payload["ok"] is True
    profile = json.loads((output / "service.profile.json").read_text())
    assert profile["timeout_seconds"] == timeout_seconds
    if target == "systemd":
        tick = (output / "systemd/options-monitor-tick-us.service").read_text()
        assert "--timeout 1800" in tick
        assert "TimeoutStartSec=2100" in tick
    else:
        import plistlib

        args = plistlib.loads((output / "launchd/com.options-monitor.tick-us.plist").read_bytes())["ProgramArguments"]
        assert args[args.index("--timeout") + 1] == "1800"
    assert not (runtime / "service.profile.json").exists()


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("field", ["runtime-root", "opend-executable"])
def test_service_render_line_boundary_public_wrapper_has_no_effects(tmp_path, newline, field):
    repo = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = runtime / "config.yaml"
    source.write_text("accounts: [fixture]\n", encoding="utf-8")
    env_file = tmp_path / "fixture.env"
    env_file.write_text("", encoding="utf-8")
    output = tmp_path / "rendered"
    command = [str(repo / "om"), "service", "render", "--target", "systemd",
               "--repo-root", str(tmp_path / "repo"), "--config-yaml", str(source),
               "--env-file", str(env_file), "--accounts", "fixture", "--markets", "us",
               "--output-dir", str(output), "--no-content"]
    if field == "opend-executable":
        command.extend(["--runtime-root", str(runtime), "--include-opend",
                        "--opend-root", str(tmp_path / "opend")])
    environment = {"PATH": os.environ["PATH"], "OM_PYTHON": CURRENT_PYTHON,
                   "OM_RUNTIME_ROOT": str(runtime), "OM_ENV_FILE": str(env_file)}
    bad_value = str(runtime) + newline + "X-OM-Renderer-Probe=present"
    for existing_output in (False, True):
        if existing_output:
            output.mkdir()
            (output / "sentinel").write_bytes(b"preserve existing output\r\n")
        result = subprocess.run(command + ["--" + field, bad_value], cwd=tmp_path,
                                env=environment, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout)
        assert result.returncode == 2, result.stderr + result.stdout
        assert payload["ok"] is False
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert payload["error"]["message"] == "systemd unit values must not contain CR or LF"
        assert not (runtime / "service.profile.json").exists()
        if existing_output:
            assert list(output.iterdir()) == [output / "sentinel"]
            assert (output / "sentinel").read_bytes() == b"preserve existing output\r\n"
        else:
            assert not output.exists()

    good_value = str(runtime) if field == "runtime-root" else str(tmp_path / "opend" / "FutuOpenD")
    result = subprocess.run(command + ["--" + field, good_value], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr + result.stdout
    assert payload["ok"] is True
    assert (output / "sentinel").read_bytes() == b"preserve existing output\r\n"
    assert str(output / "service.profile.json") in payload["data"]["written_files"]
    assert all("content" not in item for item in payload["data"]["files"])
    assert not (runtime / "service.profile.json").exists()


def test_service_render_single_line_compatibility_public_wrapper(tmp_path):
    from src.application.service_deploy import render_service_bundle

    repo = Path(__file__).resolve().parents[2]
    special = "空 格\"quote'back\\slash;end"
    rendering_repo = tmp_path / ("repo " + special)
    runtime = tmp_path / ("runtime " + special)
    runtime.mkdir()
    source = runtime / "config.yaml"
    source.write_text("accounts: [fixture]\n", encoding="utf-8")
    env_file = tmp_path / (special + ".env")
    env_file.write_text("", encoding="utf-8")
    output = tmp_path / "rendered"
    result = subprocess.run([
        str(repo / "om"), "service", "render", "--target", "systemd",
        "--repo-root", str(rendering_repo), "--runtime-root", str(runtime),
        "--config-yaml", str(source), "--env-file", str(env_file),
        "--accounts", "fixture", "--markets", "us", "--timeout", "1800",
        "--output-dir", str(output), "--no-content",
    ], cwd=tmp_path, env={"PATH": os.environ["PATH"], "OM_PYTHON": CURRENT_PYTHON,
                         "OM_RUNTIME_ROOT": str(runtime), "OM_ENV_FILE": str(env_file)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    expected = render_service_bundle(target="systemd", repo_root=rendering_repo, runtime_root=runtime,
                                     config_yaml=source, env_file=env_file, accounts=["fixture"],
                                     markets=["us"], timeout_seconds=1800, use_default_deploy_user=False)
    expected_paths = []
    for item in expected["files"]:
        path = output / item["relative_path"]
        expected_paths.append(str(path))
        assert path.read_bytes() == item.pop("content").encode("utf-8")
    assert payload["data"] == {**expected, "written_files": expected_paths}
    assert not (runtime / "service.profile.json").exists()


def test_service_drift_timeout_override_persists_before_next_reconcile(tmp_path):
    repo, runtime, units = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"],
                           timeout_seconds=600, use_default_deploy_user=False)
    profile = json.loads(next(item["content"] for item in bundle["files"]
                              if item["kind"] == "service_profile"))
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile))
    _write_systemd_units_from_bundle(bundle, units)
    desired = {**profile, "timeout_seconds": 1800}
    runner = _fake_systemd_query_runner()
    before = _drift_at(repo, runtime, units, profile=desired, run_cmd=runner)
    assert before["profile_content_changed"] is True
    after = _drift_at(repo, runtime, units, profile=desired, confirm=True, run_cmd=runner)
    assert after["summary"]["ok"] is True
    assert after["apply_errors"] == []
    assert json.loads(profile_path.read_text())["timeout_seconds"] == 1800
    tick = (units / "options-monitor-tick-us.service").read_text()
    assert "--timeout 1800" in tick
    assert "TimeoutStartSec=2100" in tick
    retry = _drift_at(repo, runtime, units, confirm=True, run_cmd=runner)
    assert retry["summary"]["ok"] is True
    assert retry["changed"] is False
    assert (units / "options-monitor-tick-us.service").read_text() == tick


@pytest.mark.parametrize("holding_operation", ["apply", "rollback"])
@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_update_handler_contends_with_real_application_owner(transition_case, holding_operation, operation):
    case = transition_case()
    case.status.write_text('{"status": "owner-sentinel"}\n')
    holder_target = "1.0.2" if holding_operation == "apply" else "1.0.0"
    contender_target = "1.0.3" if operation == "apply" else "0.9.0"
    with _hold_transition(case, "build", holding_operation, holder_target) as future:
        pid_identity = (case.pid.stat().st_dev, case.pid.stat().st_ino)
        pid_bytes = case.pid.read_bytes()
        _assert_busy_transition(case, operation, contender_target)
        _assert_busy_transition(case, "apply", None)
        assert (case.pid.stat().st_dev, case.pid.stat().st_ino) == pid_identity
        assert case.pid.read_bytes() == pid_bytes
    assert future.result()["ok"] is True
    assert not case.pid.exists()
    retry = case.invoke(operation, contender_target)
    assert retry["ok"] is True
    assert retry["data"]["write_applied"] is True
    assert case.current.resolve() == case.releases / contender_target
    assert json.loads(case.status.read_text())["target_version"] == contender_target
    assert not case.pid.exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("phase", [
    "acquire", "baseline", "build", "after_switch", "config_committed",
    "compensation_enter", "compensation_config_restored", "terminal",
])
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_update_transition_preserves_exception_boundaries(
    monkeypatch, transition_case, operation, phase, exception_type,
):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    error = exception_type("fixture cancellation")
    real_lock, writer = upgrade.single_instance_lock, upgrade.write_upgrade_status
    terminal_writes = []

    def cancel(**kwargs):
        raise error

    def write(**kwargs):
        if kwargs["payload"]["status"] not in {"runtime_preparing", "runtime_prepared"}:
            terminal_writes.append(kwargs["payload"]["status"])
            case.emit("terminal")
        writer(**kwargs)

    monkeypatch.setattr(upgrade, "write_upgrade_status", write)
    if phase == "acquire":
        @contextmanager
        def lock(*args, **kwargs):
            raise error
            yield
        monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    elif phase == "baseline":
        read_text = Path.read_text

        def read(self, *args, **kwargs):
            if self == case.releases / case.initial / "VERSION":
                raise error
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    else:
        case.hooks[phase] = cancel
    if phase.startswith("compensation"):
        case.failure = "health"
    with pytest.raises(exception_type) as raised:
        case.invoke(operation, "1.0.2" if operation == "apply" else "1.0.0")
    assert raised.value is error
    assert not case.pid.exists()
    assert len(terminal_writes) == (1 if phase == "terminal" else 0)
    assert len(case.child_requests) == (1 if phase == "compensation_config_restored" else 0)
    if phase in {"acquire", "baseline", "build"}:
        assert case.current.resolve() == case.releases / case.initial
    elif phase == "compensation_config_restored":
        assert case.current.resolve() == case.releases / case.initial
    elif phase != "compensation_config_restored":
        assert case.current.resolve() == case.releases / ("1.0.2" if operation == "apply" else "1.0.0")
    generations = [json.loads(path.read_text())["generation"] for path in case.configs]
    if phase in {"acquire", "baseline", "build", "after_switch", "compensation_config_restored"}:
        assert generations == [case.initial] * 3
    elif phase == "config_committed":
        assert generations == ["1.0.2" if operation == "apply" else "1.0.0", case.initial, case.initial]
    else:
        assert generations == ["1.0.2" if operation == "apply" else "1.0.0"] * 3
    before = case.snapshot()
    with real_lock(case.guard, busy_message="fixture guard busy"):
        assert case.snapshot() == before
    assert case.snapshot() == before


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("failure", [None, "health", "restart"])
def test_update_terminal_write_error_does_not_repeat_compensation(monkeypatch, transition_case, operation, failure):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.failure = failure
    writer = upgrade.write_upgrade_status
    error = OSError("fixture terminal disk failure")
    attempts = []

    def write(**kwargs):
        if kwargs["payload"]["status"] not in {"runtime_preparing", "runtime_prepared"}:
            attempts.append(kwargs["payload"])
            raise error
        writer(**kwargs)

    monkeypatch.setattr(upgrade, "write_upgrade_status", write)
    target = "1.0.2" if operation == "apply" else "1.0.0"
    with pytest.raises(OSError) as raised:
        case.invoke(operation, target)
    assert raised.value is error and len(attempts) == 1
    assert case.current.resolve() == case.releases / (case.initial if failure else target)
    assert len(case.child_requests) == (1 if failure else 0)
    assert not case.pid.exists()
    before = case.snapshot()
    with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
        pass
    assert case.snapshot() == before


@pytest.mark.parametrize("failure", ["plan_error", "apply_error", "failed_plan", "failed_apply"])
def test_update_cleanup_keeps_guard_without_new_compensation(monkeypatch, transition_case, failure):
    from src.application import service_cleanup as cleanup
    from src.application import service_upgrade as upgrade

    case = transition_case()
    error = OSError("fixture cleanup failure")
    calls = []

    def clean(**kwargs):
        calls.append(kwargs["confirm"])
        _assert_busy_transition(case, "rollback", "1.0.0")
        if failure == ("apply_error" if kwargs["confirm"] else "plan_error"):
            raise error
        return {"ok": failure != ("failed_apply" if kwargs["confirm"] else "failed_plan"),
                "status": "fixture_cleanup", "changed": False, "kept_releases": ["1.0.1", "1.0.2"]}

    monkeypatch.setattr(cleanup, "_service_cleanup_under_transition_lock", clean)
    if failure.endswith("error"):
        with pytest.raises(OSError) as raised:
            case.invoke("apply", "1.0.2", cleanup=True)
        assert raised.value is error
    else:
        data = case.invoke("apply", "1.0.2", cleanup=True)["data"]
        assert data["status"] == "upgraded" and data["changed"] is True
        assert data["post_upgrade_cleanup"]["ok"] is False
        assert data["post_upgrade_cleanup"]["status"] == ("skipped" if failure == "failed_plan" else "fixture_cleanup")
    assert calls == ([False] if failure in {"plan_error", "failed_plan"} else [False, True])
    assert case.current.resolve() == case.releases / "1.0.2"
    assert case.child_requests == [] and not case.pid.exists()
    with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
        pass


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_update_transition_rechecks_deployment_user_under_guard(monkeypatch, transition_case, operation):
    import pwd

    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    real_lock = upgrade.single_instance_lock
    status_before = case.status.read_bytes()

    @contextmanager
    def lock(path, **kwargs):
        with real_lock(path, **kwargs):
            profile = json.loads(case.profile.read_text())
            profile["deploy_user"] = "fixture-other-user"
            case.profile.write_text(json.dumps(profile))
            yield

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.geteuid() + 1))
    monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    data = case.invoke(operation, "1.0.2" if operation == "apply" else "1.0.0")["data"]
    assert data["status"] == "deployment_user_check_failed"
    assert data["changed"] is data["write_applied"] is False
    assert case.queries == [] and case.commands == []
    assert case.status.read_bytes() == status_before
    assert case.current.resolve() == case.releases / case.initial
    assert not case.pid.exists()
    with real_lock(case.guard, busy_message="fixture guard busy"):
        pass


def test_update_transition_runtime_roots_remain_independent(monkeypatch, transition_case):
    from src.application import service_upgrade as upgrade

    first, second = transition_case("first"), transition_case("second")
    real_lock = upgrade.single_instance_lock
    holding, resume = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    @contextmanager
    def lock(path, **kwargs):
        with real_lock(path, **kwargs):
            if Path(path) == first.guard and threading.get_ident() != main_thread:
                holding.set()
                assert resume.wait(15)
            yield

    monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.invoke, "apply", "1.0.1")
        try:
            assert holding.wait(15)
            _assert_busy_transition(first, "rollback", "1.0.0")
            assert second.invoke("apply", "1.0.1")["data"]["status"] == "already_current"
            assert second.commands == [] and first.commands == []
            assert not first.status.exists()
        finally:
            resume.set()
        assert future.result(timeout=15)["data"]["status"] == "already_current"
    assert not first.pid.exists() and not second.pid.exists()


@pytest.fixture
def transition_case(monkeypatch, tmp_path):
    """Real handlers, locks and config I/O; only subprocess/service leaves are fake."""
    from src.application import service_upgrade as upgrade
    from src.interfaces.cli.service_ops import add_service_update_commands, handle_service_update_command

    cases = []
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    real_replace = os.replace

    def replace(source, destination, **kwargs):
        for case in cases:
            if Path(destination) == case.current:
                restoring = Path(source).resolve() == case.releases / case.initial
                case.emit("compensation_enter" if restoring else "before_switch")
                result = real_replace(source, destination, **kwargs)
                case.emit("after_restore_switch" if restoring else "after_switch")
                return result
            if Path(destination) in case.configs and str(source).endswith(".upgrade-tmp"):
                result = real_replace(source, destination, **kwargs)
                case.emit("config_committed")
                return result
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    def query(**kwargs):
        case = next(item for item in cases if item.runtime == Path(kwargs["runtime_root"]))
        case.queries.append(dict(kwargs))
        case.emit("query")
        return {"ok": True, "latest_version": kwargs["target_version"] or case.latest}

    monkeypatch.setattr(upgrade, "service_upgrade_check", query)

    def capture(**kwargs):
        profile = kwargs["profile"]
        name = profile["activation_name"]
        return {
            "checked": True, "supported": True,
            "activation_states": {name: "disabled"}, "active_states": {name: "inactive"},
            "expected_services": [name], "installed_units": [name],
        }

    monkeypatch.setattr(upgrade, "service_drift", capture)

    def make(name="case"):
        root = tmp_path / name
        releases = root / "releases"
        for version in ("0.9.0", "1.0.0", "1.0.1", "1.0.2", "1.0.3", "2.0.0"):
            _write_upgrade_release_skeleton(releases / version, version)
            _create_fake_venv_python_at(releases / version / ".venv")
        current = root / "current"
        current.symlink_to(releases / "1.0.1", target_is_directory=True)
        runtime = root / "runtime"
        runtime.mkdir()
        authoring = runtime / "config.yaml"
        authoring.write_text("accounts: {}\nmarkets: {}\n")
        configs = [runtime / "config.us.json", runtime / "config.hk.json", runtime / "resolved" / "config.bot.json"]
        configs[-1].parent.mkdir()
        for config in configs:
            config.write_text(json.dumps({"generation": "1.0.1", "kind": config.name}) + "\n")
        profile_path = runtime / "service.profile.json"
        profile_path.write_text(json.dumps({
            "service_provider": "systemd", "repo_root": str(current), "runtime_root": str(runtime),
            "generation": "1.0.1", "activation_name": "fixture-1.0.1.timer",
            "markets": ["us", "hk"],
            "config_paths": {"us": str(configs[0]), "hk": str(configs[1])},
            "config_authoring": {"source": "yaml", "config_yaml": str(authoring), "markets": ["us", "hk"]},
            "restart": {"command_prefix": ["systemctl"], "services": ["fixture.service"]},
            "services": ["fixture.service"],
        }))
        case = SimpleNamespace(
            root=root, releases=releases, current=current, runtime=runtime, configs=configs,
            profile=profile_path, status=runtime / "upgrade_status.json",
            guard=runtime / "locks" / "upgrade.lock.guard",
            pid=runtime / "locks" / "upgrade.lock", initial="1.0.1", latest="1.0.2",
            hooks={}, commands=[], queries=[], child_requests=[], failure=None,
        )

        def emit(phase, **details):
            callback = case.hooks.get(phase)
            if callback:
                callback(**details)

        case.emit = emit

        def runner(command, **kwargs):
            command = list(command)
            case.commands.append(command)
            emit("command", command=command)
            def result(code=0, stdout="ok\n"):
                return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")
            if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
                _create_fake_venv_python_at(Path(command[-1]))
                return result()
            if command[1:3] == ["-m", "pip"]:
                return result()
            if command[1:2] == ["-c"] and "import src.application.multi_account_tick" in command[2]:
                return result()
            if command[1:2] == ["scripts/release_check.py"] or command == ["./om-agent", "spec"]:
                return result()
            if command[1:] == ["-c", "import src.application.bot.host; import src.application.bot.runtime"]:
                return result()
            if command[:3] in (["./om", "config", "build"], ["./om", "config", "build-bot"]):
                emit("build", command=command)
                destination = Path(command[command.index("--output") + 1])
                generation = Path(kwargs["cwd"]).resolve().name
                destination.write_text(json.dumps({"generation": generation, "kind": destination.name}) + "\n")
                return result()
            if len(command) > 2 and Path(command[0]).name == "om" and command[1:3] == ["config", "validate"]:
                emit("validate", command=command)
                failed = case.failure == "validate" and "--config-path" in command and Path(command[command.index("--config-path") + 1]) in configs
                return result(1 if failed else 0)
            if len(command) > 2 and Path(command[0]).name == "om" and command[1:3] == ["service", "drift"]:
                emit("reconcile")
                profile = json.loads(profile_path.read_text())
                snapshot = {profile["activation_name"]: {"activation_state": "disabled", "active_state": "inactive"}}
                profile.update(generation=current.resolve().name, activation_name=f"fixture-{current.resolve().name}.timer")
                profile_path.write_text(json.dumps(profile))
                data = {"summary": {"ok": True}, "apply_errors": [], "preserved_activation_states": snapshot}
                return result(stdout=json.dumps({"tool_name": "service.drift", "ok": True, "data": data}))
            if command[:1] == ["systemctl"] and command[1] in {"restart", "is-active", "is-enabled"}:
                emit("restart" if command[1] == "restart" else "health")
                failed = case.failure == "restart" and command[1] == "restart"
                failed = failed or (case.failure == "health" and command[1] == "is-active")
                return result(1 if failed else 0, "active\n" if command[1] == "is-active" else "enabled\n")
            if len(command) == 6 and command[1:3] == ["-I", "-c"]:
                # The real owner chooses the old-release executable and serializes this request.
                assert Path(command[0]) == Path(command[4]) / ".venv" / "bin" / "python"
                assert current.resolve() == Path(command[4])
                request = json.loads(Path(command[5]).read_text())
                case.child_requests.append(request)
                assert request["profile"]["generation"] == current.resolve().name
                assert all(json.loads(config.read_text())["generation"] == current.resolve().name for config in configs)
                emit("compensation_config_restored", request=request)
                profile_path.write_text(json.dumps(request["profile"]))
                restored = {
                    "ok": True, "config_validate": [{"phase": "restored"}],
                    "service_reconcile": {"summary": {"ok": True}, "apply_errors": []},
                    "restarted_services": ["fixture.service"],
                    "service_health": {"ok": True}, "operations": [],
                    "errors": [], "remediation": [],
                }
                emit("compensation_services_restored", request=request)
                return result(stdout=json.dumps(restored))
            pytest.fail(f"unexpected transition subprocess: {command}")

        parser = argparse.ArgumentParser()
        add_service_update_commands(parser.add_subparsers(dest="command"))

        def invoke(operation="apply", target="1.0.2", *, confirm=True, preserve=False, cleanup=False, repo_root=None):
            argv = ["update", operation, "--repo-root", str(repo_root or current), "--runtime-root", str(runtime),
                    "--releases-root", str(releases)]
            if target is not None:
                argv += ["--target-version" if operation == "apply" else "--to-version", target]
            if operation == "apply":
                argv += ["--cache-root", str(root / "cache")]
                if cleanup:
                    argv += ["--cleanup-after-upgrade"]
            if confirm:
                argv.append("--confirm")
            if preserve:
                argv.append("--preserve-activation-state")
            return handle_service_update_command(
                parser.parse_args(argv), service_upgrade_fn=partial(upgrade.service_upgrade, run_cmd=runner),
                service_rollback_fn=partial(upgrade.service_rollback, run_cmd=runner),
            )

        case.invoke = invoke
        case.runner = runner
        case.snapshot = lambda: (
            current.resolve(), tuple(config.read_bytes() for config in configs),
            profile_path.read_bytes(), case.status.read_bytes() if case.status.exists() else None,
            authoring.read_bytes(),
            {path: path.read_bytes() for path in releases.rglob("*") if path.is_file()},
        )
        cases.append(case)
        return case

    return make


@contextmanager
def _hold_transition(case, phase, operation, target, **kwargs):
    entered, release = threading.Event(), threading.Event()

    def pause(**_details):
        entered.set()
        assert release.wait(15), f"timed out releasing {phase}"

    case.hooks[phase] = pause
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(case.invoke, operation, target, **kwargs)
        try:
            assert entered.wait(15), f"transition did not reach {phase}"
            yield future
        finally:
            release.set()
            case.hooks.pop(phase, None)
        future.result(timeout=15)


def _assert_busy_transition(case, operation, target):
    # Snapshot after the holder pauses, including filesystem writes outside runner.
    before = case.snapshot()
    identity = (case.guard.stat().st_dev, case.guard.stat().st_ino)
    queries, commands = len(case.queries), len(case.commands)
    out = case.invoke(operation, target)
    data = out["data"]
    expected_status = "failed" if operation == "apply" else "rollback_failed"
    assert out["tool_name"] == f"update.{operation}"
    assert out["ok"] is False
    assert data["status"] == data["failure_status"] == expected_status
    assert data["error"] == f"RuntimeError: upgrade lock already exists: {case.pid}"
    for key in ("ok", "changed", "write_applied", "dry_run", "symlink_switched",
                "rolled_back" if operation == "apply" else "restored_original"):
        assert data[key] is False
    assert data["confirmed"] is True and data["audit_id"]
    assert data["operation"] == ("upgrade" if operation == "apply" else "rollback")
    assert data["operations"] == [] and data["post_switch_runtime_config_validate"] == []
    for key in ("compensation", "runtime_config_prepare", "runtime_config_commit",
                "service_reconcile", "service_health", "runtime_readiness"):
        assert data[key] == {}
    assert len(case.queries) == queries and len(case.commands) == commands
    assert case.snapshot() == before
    assert (case.guard.stat().st_dev, case.guard.stat().st_ino) == identity
    if operation == "apply":
        assert data["runtime_prepare"] == {} and data["config_rebuilt"] is False
        assert data["target_version"] == target
        assert data["release_tag"] == (f"v{target}" if target else None)
        assert data["target_dir"] == (str(case.releases / target) if target else None)
        if target is None:
            assert data["release_materialize"] == {}
    return data


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("contender", ["apply", "rollback"])
@pytest.mark.parametrize("phase", ["compensation_enter", "compensation_config_restored", "compensation_services_restored"])
def test_update_transition_holds_guard_through_compensation(transition_case, operation, contender, phase):
    case = transition_case()
    original = case.snapshot()
    case.failure = "health"
    target = "1.0.2" if operation == "apply" else "1.0.0"
    next_target = "1.0.3" if contender == "apply" else "0.9.0"
    with _hold_transition(case, phase, operation, target, preserve=True) as future:
        _assert_busy_transition(case, contender, next_target)
        if phase != "compensation_enter":
            assert case.current.resolve() == case.releases / case.initial
            assert tuple(path.read_bytes() for path in case.configs) == original[1]
    data = future.result()["data"]
    assert data["status"] == ("upgrade_failed_rolled_back" if operation == "apply" else "rollback_failed_restored")
    assert data["failure_status"] == ("upgraded_service_health_failed" if operation == "apply" else "rollback_service_health_failed")
    assert data["rolled_back" if operation == "apply" else "restored_original"] is True
    assert data["changed"] is data["symlink_switched"] is False
    assert data["compensation"]["ok"] is True
    assert data["compensation"]["config_restore"]["ok"] is True
    assert json.loads(Path(data["runtime_config_commit"]["manifest_path"]).read_text())["state"] == "restored"
    assert case.snapshot()[:3] == original[:3]
    assert len(case.child_requests) == 1
    assert case.child_requests[0]["preserved_activation_states"] == {
        "fixture-1.0.1.timer": {"activation_state": "disabled", "active_state": "inactive"},
    }
    assert json.loads(case.status.read_text())["status"] == data["status"]
    assert "audit_id" not in json.loads(case.status.read_text())
    case.failure = None
    retry = case.invoke(contender, next_target)
    assert retry["ok"] is True
    assert case.current.resolve() == case.releases / next_target
    assert all(json.loads(path.read_text())["generation"] == next_target for path in case.configs)
    assert json.loads(case.profile.read_text())["generation"] == next_target
    assert json.loads(case.status.read_text())["target_version"] == next_target
    assert len(case.child_requests) == 1 and not case.pid.exists()


def test_update_transition_restart_failure_keeps_guard_during_compensation(transition_case):
    case = transition_case()
    case.failure = "restart"
    with _hold_transition(case, "compensation_config_restored", "apply", "1.0.2") as future:
        _assert_busy_transition(case, "rollback", "1.0.0")
    data = future.result()["data"]
    assert data["failure_status"] == "upgraded_restart_failed"
    assert data["status"] == "upgrade_failed_rolled_back"
    assert data["restart_failed_services"] == ["fixture.service"]
    assert data["rolled_back"] is True and data["changed"] is False


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("failure", [None, "health"])
def test_update_transition_holds_guard_through_terminal_write(monkeypatch, transition_case, operation, failure):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.failure = failure
    writer = upgrade.write_upgrade_status

    def write(**kwargs):
        if kwargs["payload"]["status"] not in {"runtime_preparing", "runtime_prepared"}:
            case.emit("terminal")
        writer(**kwargs)

    monkeypatch.setattr(upgrade, "write_upgrade_status", write)
    with _hold_transition(case, "terminal", operation, "1.0.2" if operation == "apply" else "1.0.0") as future:
        _assert_busy_transition(case, "apply", "1.0.3")
        _assert_busy_transition(case, "rollback", "0.9.0")
    result = future.result()["data"]
    assert json.loads(case.status.read_text())["status"] == result["status"]
    case.failure = None
    assert case.invoke("apply", "1.0.3")["ok"] is True
    assert json.loads(case.status.read_text())["target_version"] == "1.0.3"


@pytest.mark.parametrize(("operation", "target", "expected_target"), [
    ("apply", "1.0.3", "1.0.3"), ("rollback", None, "1.0.1"), ("rollback", "1.0.0", "1.0.0"),
])
def test_update_transition_binds_baseline_after_acquiring_guard(
    monkeypatch, transition_case, operation, target, expected_target,
):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    real_lock = upgrade.single_instance_lock
    main_thread = threading.get_ident()
    arrived, resume = threading.Event(), threading.Event()

    @contextmanager
    def lock(path, **kwargs):
        if threading.get_ident() != main_thread and not arrived.is_set():
            arrived.set()
            assert resume.wait(15)
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(case.invoke, operation, target, preserve=True)
        try:
            assert arrived.wait(15)
            assert case.queries == [] and case.commands == []
            assert case.invoke("apply", "1.0.2")["ok"] is True
            baseline = case.snapshot()
        finally:
            resume.set()
        data = future.result(timeout=15)["data"]
    assert data["current_version"] == "1.0.2"
    assert data["target_version"] == expected_target
    assert data["preserved_activation_units"] == ["fixture-1.0.2.timer"]
    assert data["runtime_config_prepare"]["staging_root"].endswith(f"1.0.2-to-{expected_target}")
    for item, before in zip(data["runtime_config_commit"]["artifacts"], baseline[1], strict=True):
        assert Path(item["backup_path"]).read_bytes() == before
    assert data["service_reconcile"]["preserved_activation_states"] == {
        "fixture-1.0.2.timer": {"activation_state": "disabled", "active_state": "inactive"},
    }
    if operation == "apply":
        assert data["previous_dir"] == str(case.releases / "1.0.2")
    assert case.current.resolve() == case.releases / expected_target


@pytest.mark.parametrize(("operation", "target", "latest", "status"), [
    ("apply", None, "", "no_target_version"),
    ("apply", "1.0.1", None, "already_current"),
    ("apply", "1.0.0", None, "target_older_than_current"),
    ("apply", "2.0.0", None, "blocked_major_upgrade"),
    ("rollback", None, None, "no_rollback_target"),
    ("rollback", "9.9.9", None, "rollback_target_missing"),
])
@pytest.mark.parametrize("confirm", [False, True])
def test_update_early_preview_persists_only_with_unchanged_baseline(
    monkeypatch, transition_case, operation, target, latest, status, confirm,
):
    from src.application import service_upgrade as upgrade
    from src.application.agent_tool_contracts import AgentToolError

    case = transition_case()
    case.latest = latest
    writer = upgrade.write_upgrade_status
    writes = []

    def write(**kwargs):
        with pytest.raises(AgentToolError) as busy:
            with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
                pytest.fail("preview writer must hold the real guard")
        assert busy.value.code == "RESOURCE_BUSY"
        writes.append(kwargs["payload"]["status"])
        writer(**kwargs)

    monkeypatch.setattr(upgrade, "write_upgrade_status", write)
    data = case.invoke(operation, target, confirm=confirm)["data"]
    assert data["status"] == status
    assert data["dry_run"] is (not confirm) and data["write_applied"] is False
    assert writes == [status]
    receipt = json.loads(case.status.read_text())
    assert receipt["status"] == status and "audit_id" not in receipt
    assert not case.pid.exists()
    assert len(case.queries) == (1 if operation == "apply" else 0)
    assert case.commands == []


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("gate_failure", [False, True])
def test_update_normal_preview_adds_no_transition_lock_or_receipt(
    monkeypatch, transition_case, operation, gate_failure,
):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    before = case.snapshot()

    @contextmanager
    def unexpected_lock(*args, **kwargs):
        pytest.fail("normal preview must not acquire a transition guard")
        yield

    monkeypatch.setattr(upgrade, "single_instance_lock", unexpected_lock)
    if gate_failure:
        monkeypatch.setattr(upgrade, "service_drift", lambda **kwargs: {"checked": False, "reason": "fixture unavailable"})
    data = case.invoke(operation, "1.0.2" if operation == "apply" else "1.0.0", confirm=False, preserve=gate_failure)["data"]
    assert data["status"] == ("service_activation_snapshot_failed" if gate_failure else "dry_run")
    assert case.snapshot() == before and not case.guard.exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("change", [
    "owner_failure", "same_bytes", "absent_to_present", "default_status",
    "current", "profile", "target_appears",
])
def test_update_early_preview_cannot_overwrite_new_status(monkeypatch, transition_case, operation, change):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    # No current_version: the initial default rollback target is unavailable.
    if change != "absent_to_present":
        case.status.write_text('{"status": "sentinel"}\n')
    original = case.snapshot()
    real_lock = upgrade.single_instance_lock
    main_thread = threading.get_ident()
    arrived, resume = threading.Event(), threading.Event()

    @contextmanager
    def lock(path, **kwargs):
        if threading.get_ident() != main_thread and not arrived.is_set():
            arrived.set()
            assert resume.wait(15)
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    preview_target = "1.0.1" if operation == "apply" else None
    if operation == "rollback" and change == "target_appears":
        preview_target = "9.9.9"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(case.invoke, operation, preview_target, confirm=False)
        try:
            assert arrived.wait(15)
            preview_queries = len(case.queries)
            if change == "owner_failure":
                def fail_build(**kwargs):
                    raise RuntimeError("fixture fails before switching")
                case.hooks["build"] = fail_build
                owner = case.invoke("apply", "1.0.2")
                assert owner["ok"] is False
                assert case.snapshot()[:3] == original[:3]
            elif change == "same_bytes":
                old_identity = (case.status.stat().st_dev, case.status.stat().st_ino)
                replacement = case.runtime / "same-status.json"
                replacement.write_bytes(case.status.read_bytes())
                replacement.replace(case.status)
                assert (case.status.stat().st_dev, case.status.stat().st_ino) != old_identity
            elif change == "absent_to_present":
                case.status.write_text('{"status": "new-owner"}\n')
            elif change == "default_status":
                upgrade.write_upgrade_status(runtime_root=case.runtime, payload={"current_version": "1.0.0", "status": "new-owner"})
            elif change == "current":
                replacement = case.root / "new-current"
                replacement.symlink_to(case.releases / "1.0.2", target_is_directory=True)
                replacement.replace(case.current)
            elif change == "profile":
                profile = json.loads(case.profile.read_text())
                profile["generation"] = "changed"
                case.profile.write_text(json.dumps(profile))
            else:
                (case.releases / "9.9.9").mkdir()
                if operation == "apply":
                    # A target-existence fact belongs only to rollback; change its
                    # current input here so both directions still exercise revalidation.
                    (case.releases / "1.0.1" / "VERSION").write_text("1.0.0\n")
            after_change = case.snapshot()
            expected_queries = preview_queries + (1 if change == "owner_failure" else 0)
        finally:
            resume.set()
        data = future.result(timeout=15)["data"]
    expected = "already_current" if operation == "apply" else "rollback_target_missing" if preview_target else "no_rollback_target"
    assert data["status"] == expected
    assert case.snapshot() == after_change
    assert len(case.queries) == expected_queries
    assert not case.pid.exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("input_name", ["upgrade_status.json", "service.profile.json"])
@pytest.mark.parametrize("defect", [
    "malformed", "nonobject", "invalid_utf8", "open_error", "missing_open",
    "stat_error", "missing_stat", "replaced_during_read",
])
def test_update_early_preview_skips_unprovable_publication(
    monkeypatch, transition_case, operation, input_name, defect,
):
    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    path = case.runtime / input_name
    if defect == "malformed":
        path.write_text("{invalid")
    elif defect == "nonobject":
        path.write_text("[]")
    elif defect == "invalid_utf8":
        path.write_bytes(b"\xff")
    original_open, original_stat = Path.open, Path.stat
    replacement_done = False

    def open_file(self, mode="r", *args, **kwargs):
        if self == path and mode == "rb" and defect == "open_error":
            raise PermissionError("fixture snapshot open failure")
        if self == path and mode == "rb" and defect == "missing_open":
            raise FileNotFoundError("fixture pathname remains present")
        return original_open(self, mode, *args, **kwargs)

    def stat_file(self, *args, **kwargs):
        nonlocal replacement_done
        if self == path and defect == "stat_error":
            raise OSError("fixture snapshot stat failure")
        if self == path and defect == "missing_stat":
            raise FileNotFoundError("fixture pathname disappeared after opening")
        if self == path and defect == "replaced_during_read" and not replacement_done:
            replacement_done = True
            with original_open(path, "rb") as stream:
                raw = stream.read()
            replacement = case.runtime / "replacement.json"
            replacement.write_bytes(raw)
            replacement.replace(path)
        return original_stat(self, *args, **kwargs)

    before = case.status.read_bytes()
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(Path, "stat", stat_file)
    data = case.invoke(operation, "1.0.1" if operation == "apply" else None, confirm=False)["data"]
    assert data["status"] == ("already_current" if operation == "apply" else "no_rollback_target")
    with original_open(case.status, "rb") as stream:
        assert stream.read() == before
    assert not case.guard.exists()
    if defect == "replaced_during_read":
        assert replacement_done


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("failure", ["busy", "acquire_error", "acquire_runtime_error", "writer_error"])
def test_update_early_preview_preserves_publication_errors(monkeypatch, transition_case, operation, failure):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    before = case.snapshot()
    target = "1.0.1" if operation == "apply" else None
    error = RuntimeError("fixture nonbusy lock failure") if failure == "acquire_runtime_error" else OSError("fixture publication failure")
    real_lock = upgrade.single_instance_lock
    if failure == "busy":
        with real_lock(case.guard, busy_message="fixture guard busy"):
            data = case.invoke(operation, target, confirm=False)["data"]
        assert data["status"] == ("already_current" if operation == "apply" else "no_rollback_target")
        assert case.snapshot() == before
    else:
        if failure in {"acquire_error", "acquire_runtime_error"}:
            @contextmanager
            def lock(*args, **kwargs):
                raise error
                yield
            monkeypatch.setattr(upgrade, "single_instance_lock", lock)
        else:
            def write(**kwargs):
                raise error
            monkeypatch.setattr(upgrade, "write_upgrade_status", write)
        with pytest.raises(type(error)) as raised:
            case.invoke(operation, target, confirm=False)
        assert raised.value is error
        assert case.snapshot() == before
    with real_lock(case.guard, busy_message="fixture guard busy"):
        pass
    assert not case.pid.exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("exception_type", [OSError, RuntimeError])
def test_update_acquisition_error_returns_failure_without_shared_writes(
    monkeypatch, transition_case, operation, exception_type,
):
    from src.application import service_upgrade as upgrade

    case = transition_case()
    case.status.write_text('{"status": "owner-sentinel"}\n')
    before = case.snapshot()
    error = exception_type("fixture acquisition failure")

    @contextmanager
    def lock(*args, **kwargs):
        raise error
        yield

    def unexpected_comparison(*args):
        pytest.fail("acquisition failure must not evaluate a version verdict")

    monkeypatch.setattr(upgrade, "single_instance_lock", lock)
    monkeypatch.setattr(upgrade, "compare_versions", unexpected_comparison)
    data = case.invoke(operation, "not-a-version")["data"]
    assert data["failure_status"] == ("failed" if operation == "apply" else "rollback_failed")
    assert data["error"] == f"{exception_type.__name__}: {error}"
    assert data["target_version"] == "not-a-version"
    assert data["compensation"] == {} and data["operations"] == []
    assert case.queries == [] and case.commands == []
    assert case.snapshot() == before


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_update_early_preview_rejects_mixed_generation_baseline(monkeypatch, transition_case, operation):
    case = transition_case()
    case.status.write_text('{"status": "before"}\n')
    open_file = Path.open
    changed = False

    def open_profile(self, mode="r", *args, **kwargs):
        nonlocal changed
        if self == case.profile and mode == "rb" and not changed:
            changed = True
            case.status.write_text('{"status": "after"}\n')
            profile = json.loads(case.profile.read_text())
            profile["generation"] = "after"
            case.profile.write_text(json.dumps(profile))
        return open_file(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_profile)
    data = case.invoke(operation, "1.0.1" if operation == "apply" else None, confirm=False)["data"]
    assert changed
    assert data["status"] == ("already_current" if operation == "apply" else "no_rollback_target")
    assert json.loads(case.status.read_text()) == {"status": "after"}
    assert len(case.queries) == (1 if operation == "apply" else 0)


@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("input_name", ["upgrade_status.json", "service.profile.json"])
def test_update_early_preview_dangling_input_is_not_proven_absent(
    transition_case, operation, input_name,
):
    case = transition_case()
    path = case.runtime / input_name
    replacement = case.runtime / "dangling-input"
    replacement.symlink_to(case.runtime / "missing-input")
    replacement.replace(path)
    data = case.invoke(operation, "1.0.1" if operation == "apply" else None, confirm=False)["data"]
    assert data["status"] == ("already_current" if operation == "apply" else "no_rollback_target")
    assert path.is_symlink() and not path.exists()
    assert not case.guard.exists()



@pytest.mark.parametrize("operation", ["apply", "rollback"])
@pytest.mark.parametrize("input_name", ["upgrade_status.json", "service.profile.json"])
def test_update_early_preview_rechecks_witness_after_current_read(
    monkeypatch, transition_case, operation, input_name,
):
    case = transition_case()
    case.status.write_text('{"status": "sentinel"}\n')
    read_text = Path.read_text
    version_reads = 0
    before = case.status.read_bytes()

    def read(self, *args, **kwargs):
        nonlocal version_reads
        if self == case.releases / case.initial / "VERSION":
            version_reads += 1
            if version_reads == 2:
                path = case.runtime / input_name
                replacement = case.runtime / "replacement-after-current.json"
                replacement.write_bytes(path.read_bytes())
                replacement.replace(path)
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    data = case.invoke(operation, "1.0.1" if operation == "apply" else None, confirm=False)["data"]
    assert data["status"] == ("already_current" if operation == "apply" else "no_rollback_target")
    assert version_reads == 2
    assert case.status.read_bytes() == before
    assert len(case.queries) == (1 if operation == "apply" else 0)
    assert not case.pid.exists()


def test_update_confirmed_nonsymlink_receipt_holds_guard(monkeypatch, transition_case):
    from src.application import service_upgrade as upgrade
    from src.application.agent_tool_contracts import AgentToolError

    case = transition_case()
    plain = case.root / "plain" / "repo"
    _write_upgrade_release_skeleton(plain, "1.0.1")
    writer = upgrade.write_upgrade_status
    writes = []

    def write(**kwargs):
        with pytest.raises(AgentToolError) as busy:
            with upgrade.single_instance_lock(case.guard, busy_message="fixture guard busy"):
                pytest.fail("early confirmed receipt must hold the guard")
        assert busy.value.code == "RESOURCE_BUSY"
        writes.append(kwargs["payload"]["status"])
        writer(**kwargs)

    monkeypatch.setattr(upgrade, "write_upgrade_status", write)
    data = case.invoke(repo_root=plain)["data"]
    assert data["status"] == "repo_root_not_symlink"
    assert writes == ["repo_root_not_symlink"]
    assert case.commands == [] and not case.pid.exists()


@pytest.fixture
def unknown_drift_case(monkeypatch, tmp_path, capsys, request):
    from src.application import service_drift as owner
    from src.interfaces.cli import main as cli

    boundary = getattr(request, "param", "systemd")
    missing = {"options-monitor-projection-verify.timer"} if boundary == "missing-timer" else set()
    fixture = _synthetic_drift_context(
        monkeypatch, tmp_path, target="launchd" if boundary == "launchd" else "systemd", skip=missing,
    )
    observed = {}
    commands = []
    repairable = set()
    query_units = set(fixture["timers"]) - missing

    def run_cmd(command, **kwargs):
        command = list(command)
        commands.append(command)
        if kwargs != {"capture_output": True, "text": True, "timeout": 60, "check": False}:
            fixture["fail"]("runner kwargs")()
        if (len(command) == 3 and command[0] == "systemctl"
                and command[1] in {"is-enabled", "is-active"} and command[2] in query_units):
            kind, unit = command[1:]
            state = observed.get((kind, unit), "enabled" if kind == "is-enabled" else "active")
            if isinstance(state, Exception):
                raise state
            code = 3 if state == "inactive" else (1 if state in {"disabled", "masked"} else 0)
            return subprocess.CompletedProcess(command, code, stdout=state, stderr="")
        if (len(command) == 4 and command[:3] == ["systemctl", "enable", "--now"]
                and command[3] in repairable):
            unit = command[3]
            observed[("is-enabled", unit)] = "enabled"
            observed[("is-active", unit)] = "active"
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        fixture["fail"]("unexpected command: " + " ".join(command))()

    monkeypatch.setattr(cli, "service_drift", partial(owner.service_drift, run_cmd=run_cmd))

    def public_check(*, confirm=False):
        argv = [
            "service", "drift", "--repo-root", str(fixture["repo"]),
            "--runtime-root", str(fixture["runtime"]), "--profile-path", str(fixture["profile_path"]),
        ]
        if confirm:
            argv.append("--confirm")
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert captured.err == ""
        return code, json.loads(captured.out)

    before = fixture["inventory"]()
    yield {
        **fixture, "observed": observed, "commands": commands, "repairable": repairable,
        "run_cmd": run_cmd, "public_check": public_check,
    }
    assert fixture["forbidden"] == []
    assert fixture["inventory"]() == before


@pytest.mark.parametrize("query_kind", ["enabled", "active"])
@pytest.mark.parametrize("failure", ["blank", "token", "oserror", "timeout"])
def test_service_drift_unknown_query_through_public_cli(unknown_drift_case, query_kind, failure):
    fixture = unknown_drift_case
    target = "options-monitor-projection-verify.timer"
    command = ["systemctl", f"is-{query_kind}", target]
    response = {
        "blank": "", "token": "unavailable-fixture-state",
        "oserror": OSError("synthetic query unavailable"),
        "timeout": subprocess.TimeoutExpired(command, 60),
    }[failure]
    fixture["observed"][(f"is-{query_kind}", target)] = response
    code, payload = fixture["public_check"]()
    assert code == 2
    assert payload["tool_name"] == "service.drift"
    assert payload["ok"] is False
    result = payload["data"]
    assert result["activation_states" if query_kind == "enabled" else "active_states"][target] == "unknown"
    assert result["activation_unknown_units"] == [target]
    assert result["summary"]["activation_unknown_count"] == 1
    assert result["summary"]["ok"] is False
    assert result["summary"]["status"] == "error"
    assert result["summary"]["error_count"] == result["summary"]["warning_count"] == 1
    assert result["activation_drift_units"] == result["observed_activation_drift_units"] == []
    assert result["manual_actions"] == [
        f"manual_check_activation_state: systemctl is-enabled {target}; systemctl is-active {target}",
    ]
    assert result["profile_content_changed"] is False
    assert result["changed"] is False and result["operations"] == []
    assert result["write_applied"] is False
    assert command in fixture["commands"]
    assert all(cmd[1] in {"is-enabled", "is-active"} for cmd in fixture["commands"])


def test_service_drift_unknown_only_confirm_and_recovery(unknown_drift_case):
    fixture = unknown_drift_case
    code, healthy = fixture["public_check"]()
    assert code == 0 and healthy["ok"] is True
    assert healthy["data"]["summary"]["status"] == "ok"
    assert healthy["data"]["summary"]["error_count"] == healthy["data"]["summary"]["warning_count"] == 0
    assert healthy["data"]["profile_content_changed"] is False
    targets = sorted(["options-monitor-tick-us.timer", "options-monitor-projection-verify.timer"])
    fixture["observed"][("is-enabled", targets[0])] = ""
    fixture["observed"][("is-active", targets[0])] = ""
    fixture["observed"][("is-active", targets[1])] = ""
    for _attempt in range(2):
        code, payload = fixture["public_check"](confirm=True)
        result = payload["data"]
        assert code == 2 and payload["ok"] is False
        for source in (result, result["before"]):
            assert source["activation_unknown_units"] == targets
            assert source["summary"]["activation_unknown_count"] == 2
            assert source["summary"]["error_count"] == source["summary"]["warning_count"] == 1
            assert source["activation_drift_units"] == []
            assert source["profile_content_changed"] is False
        assert result["changed"] is False and result["operations"] == []
        assert result["write_applied"] is False and result["apply_errors"] == []
        assert result["applied"]["profile_written"] is False
        for key in ("written_units", "written_managed_files", "retired_units", "retired_managed_files",
                    "enabled_timers", "enabled_services", "started_services", "restarted_timers"):
            assert result["applied"][key] == []
        assert result["manual_actions"] == [
            f"manual_check_activation_state: systemctl is-enabled {name}; systemctl is-active {name}"
            for name in targets
        ]
    for (kind, unit) in tuple(fixture["observed"]):
        fixture["observed"][(kind, unit)] = "enabled" if kind == "is-enabled" else "active"
    code, recovered = fixture["public_check"]()
    assert code == 0 and recovered["ok"] is True
    assert recovered["data"]["summary"]["status"] == "ok"
    assert recovered["data"]["summary"]["error_count"] == recovered["data"]["summary"]["warning_count"] == 0
    assert recovered["data"]["activation_unknown_units"] == []
    assert recovered["data"]["summary"]["activation_unknown_count"] == 0
    assert recovered["data"]["manual_actions"] == []
    assert all(cmd[1] in {"is-enabled", "is-active"} for cmd in fixture["commands"])


@pytest.mark.parametrize("activation, active", [
    pytest.param("", "inactive", id="unknown-inactive"),
    pytest.param("disabled", "", id="disabled-unknown"),
    pytest.param("masked", "", id="masked-unknown"),
])
def test_service_drift_unknown_activation_isolated_from_known_repairs(unknown_drift_case, activation, active):
    fixture = unknown_drift_case
    unknown = "options-monitor-projection-verify.timer"
    known = "options-monitor-tick-us.timer"
    fixture["observed"].update({
        ("is-enabled", unknown): activation, ("is-active", unknown): active,
        ("is-enabled", known): "disabled", ("is-active", known): "inactive",
    })
    fixture["repairable"].add(known)
    code, payload = fixture["public_check"](confirm=True)
    result = payload["data"]
    assert code == 2 and payload["ok"] is False
    assert result["before"]["observed_activation_drift_units"] == sorted([unknown, known])
    assert result["before"]["activation_drift_units"] == [known]
    assert result["before"]["activation_states"][known] == "disabled"
    assert result["before"]["active_states"][known] == "inactive"
    assert result["activation_unknown_units"] == [unknown]
    assert result["activation_drift_units"] == []
    assert result["observed_activation_drift_units"] == [unknown]
    assert result["activation_states"][known] == "enabled" and result["active_states"][known] == "active"
    assert result["summary"]["error_count"] == result["summary"]["warning_count"] == 1
    assert result["summary"]["activation_unknown_count"] == 1
    assert result["changed"] is True and result["write_applied"] is True
    assert result["applied"]["enabled_timers"] == [known]
    assert result["applied"]["profile_written"] is False
    assert result["apply_errors"] == []
    assert len(result["operations"]) == 1
    assert result["operations"][0]["command"] == ["systemctl", "enable", "--now", known]
    mutations = [cmd for cmd in fixture["commands"] if cmd[1] not in {"is-enabled", "is-active"}]
    assert mutations == [["systemctl", "enable", "--now", known]]
    assert not any(unknown in action and "enable --now" in action for action in result["before"]["manual_actions"])


@pytest.mark.parametrize("activation, active", [
    pytest.param("", "inactive", id="unknown-inactive"),
    pytest.param("disabled", "", id="disabled-unknown"),
])
def test_service_drift_unknown_preserved_activation_remains_unresolved(unknown_drift_case, activation, active):
    fixture = unknown_drift_case
    target = "options-monitor-projection-verify.timer"
    fixture["observed"].update({("is-enabled", target): activation, ("is-active", target): active})
    out = _drift_at(
        fixture["repo"], fixture["runtime"], fixture["units"], confirm=True,
        activation_policy="preserve-existing",
        preserved_activation_states={target: {"activation_state": "disabled", "active_state": "inactive"}},
        run_cmd=fixture["run_cmd"],
    )
    assert out["summary"]["ok"] is False and out["summary"]["status"] == "error"
    assert out["summary"]["activation_unknown_count"] == 1
    assert out["activation_unknown_units"] == [target]
    assert out["observed_activation_drift_units"] == [target]
    assert out["activation_states"][target] == (activation or "unknown")
    assert out["active_states"][target] == (active or "unknown")
    assert out["preserved_activation_units"] == out["activation_preservation_conflicts"] == [target]
    assert out["activation_drift_units"] == []
    assert out["changed"] is False and out["operations"] == []
    assert out["applied"]["profile_written"] is False
    assert all(cmd[1] in {"is-enabled", "is-active"} for cmd in fixture["commands"])


@pytest.mark.parametrize("unknown_drift_case", ["launchd", "missing-timer"], indirect=True)
def test_service_drift_nonqueried_boundary_has_no_unknown(unknown_drift_case):
    fixture = unknown_drift_case
    code, payload = fixture["public_check"]()
    result = payload["data"]
    assert result["activation_unknown_units"] == []
    assert result["summary"]["activation_unknown_count"] == 0
    assert result["changed"] is False and result["operations"] == []
    if result["provider"] == "launchd":
        assert code == 0 and payload["ok"] is True
        assert result["activation_states"] == result["active_states"] == {}
        assert fixture["commands"] == []
        assert result["missing_installed_units"] == []
    else:
        missing = "options-monitor-projection-verify.timer"
        assert code == 2 and payload["ok"] is False
        assert result["missing_installed_units"] == result["missing_required_units"] == [missing]
        assert missing not in result["activation_states"] and missing not in result["active_states"]
        assert fixture["commands"] and all(cmd[-1] != missing for cmd in fixture["commands"])


@pytest.mark.parametrize("confirm", [pytest.param(False, id="preview"), pytest.param(True, id="confirm")])
def test_service_credential_migration_blocks_unknown_before_target_work(unknown_drift_case, monkeypatch, confirm):
    from src.application import service_drift as owner

    fixture = unknown_drift_case
    target = "options-monitor-projection-verify.timer"
    fixture["observed"].update({("is-enabled", target): "masked", ("is-active", target): ""})
    original_calls, render_calls = _guard_original_credential_migration(monkeypatch, fixture)
    result = owner.migrate_service_credentials(
        repo_root=fixture["repo"], runtime_root=fixture["runtime"], profile_path=fixture["profile_path"],
        systemd_unit_root=fixture["units"], secret_credential_delivery="load-credential-encrypted",
        secret_credential_store_root=fixture["runtime"] / "synthetic-credstore",
        confirm=confirm, run_cmd=fixture["run_cmd"],
    )
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["reason"] == "unrelated_service_drift_must_be_reconciled_first"
    assert result["confirmed"] is confirm and result["changed"] is False
    assert result["values_exposed"] is False
    assert result["unrelated_drift"] == {"activation_unknown_units": [target]}
    source = result["precondition_drift"]
    assert source["activation_drift_units"] == []
    assert source["observed_activation_drift_units"] == [target]
    assert source["activation_unknown_units"] == [target]
    assert source["summary"]["ok"] is False
    assert source["profile_content_changed"] is False
    assert len(original_calls) == len(render_calls) == 1
    assert all(cmd[1] in {"is-enabled", "is-active"} for cmd in fixture["commands"])


def _published_upgrade_case(tmp_path):
    releases = tmp_path / "releases"
    old = releases / "1.0.0"
    _write_upgrade_release_skeleton(old, "1.0.0")
    current = tmp_path / "current"
    current.symlink_to(old, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return dict(repo_root=current, runtime_root=runtime, releases_root=releases,
                cache_root=tmp_path / "cache", restart_services=False)


@pytest.mark.parametrize("failure", ["draft", "missing", "empty", "starter", "404", "timeout", "bad_metadata"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_published_release_failure_blocks_real_service_apply(monkeypatch, tmp_path, failure, explicit, existing):
    from src.application import service_upgrade as upgrade

    args = _published_upgrade_case(tmp_path)
    responses, http_calls = _mock_published_releases(monkeypatch)
    base = "https://api.github.com/repos/example/options-monitor"
    release_url = f"{base}/releases/tags/v1.0.1"
    asset_url = f"{base}/releases/2/assets?per_page=100&page=1"
    if failure == "draft":
        responses[release_url]["draft"] = True
    elif failure in {"404", "timeout"}:
        responses[release_url] = ValueError("release_not_found_or_inaccessible: HTTP 404" if failure == "404"
                                            else "release_metadata_unavailable: TimeoutError")
    elif failure == "bad_metadata":
        responses[release_url] = []
    elif failure == "missing":
        responses[asset_url].pop()
    elif failure == "empty":
        responses[asset_url][0]["size"] = 0
    else:
        responses[asset_url][0]["state"] = "starter"
    target = args["releases_root"] / "1.0.1"
    if existing:
        _write_upgrade_release_skeleton(target, "1.0.1")
    calls = []
    runner = _fake_upgrade_release_runner(record=lambda command, kwargs: calls.append(command))
    def forbidden(**kwargs):
        pytest.fail("qualification failure reached materialization/configuration/runtime preparation")
    for name in ("_materialize_release_from_git_cache", "_ensure_release_runtime", "_prepare_runtime_configs_for_release"):
        monkeypatch.setattr(upgrade, name, forbidden)
    check = upgrade.service_upgrade_check(repo_root=args["repo_root"], runtime_root=args["runtime_root"],
                                           cache_root=args["cache_root"], run_cmd=runner,
                                           target_version="1.0.1" if explicit else None)
    assert check["ok"] is False and check["status"] == "upgrade_check_failed"
    assert check["upgrade_available"] is False and check["release_tag"] is None
    out = upgrade.service_upgrade(**args, target_version="1.0.1" if explicit else None,
                                   confirm=True, auto=True, allow_major=True, run_cmd=runner)
    assert out["ok"] is False and out["status"] == "no_target_version"
    assert out["changed"] is False and out["target_version"] is out["release_tag"] is None
    assert out["check"]["status"] == "upgrade_check_failed"
    assert args["repo_root"].resolve() == (args["releases_root"] / "1.0.0").resolve()
    assert target.exists() is existing
    assert not any("archive" in c or c[:2] == ["tar", "-xf"] or c[0] in {"systemctl", "launchctl"} for c in calls)
    assert not list(args["runtime_root"].glob("config*"))
    assert sum("/tags/v1.0.1" in url for url, _ in http_calls) == 2


@pytest.mark.parametrize("change", ["draft", "missing", "unavailable"])
def test_published_release_is_rechecked_after_service_preview(monkeypatch, tmp_path, change):
    from src.application import service_upgrade as upgrade

    args = _published_upgrade_case(tmp_path)
    _write_upgrade_release_skeleton(args["releases_root"] / "1.0.1", "1.0.1")
    responses, calls = _mock_published_releases(monkeypatch)
    runner = _fake_upgrade_release_runner(materialize=False)
    preview = upgrade.service_upgrade(**args, target_version="1.0.1", run_cmd=runner)
    assert preview["status"] == "dry_run" and preview["planned_operations"]
    base = "https://api.github.com/repos/example/options-monitor"
    if change == "draft":
        responses[f"{base}/releases/tags/v1.0.1"]["draft"] = True
    elif change == "missing":
        responses[f"{base}/releases/2/assets?per_page=100&page=1"] = []
    else:
        responses[f"{base}/releases/tags/v1.0.1"] = ValueError("release_metadata_unavailable: TimeoutError")
    out = upgrade.service_upgrade(**args, target_version="1.0.1", confirm=True, run_cmd=runner)
    assert out["status"] == "no_target_version" and out["changed"] is False
    assert out["target_version"] is out["release_tag"] is None
    assert sum("/tags/v1.0.1" in url for url, _ in calls) == 2
    assert args["repo_root"].resolve().name == "1.0.0"


def test_failed_check_cannot_smuggle_target_into_apply(monkeypatch, tmp_path):
    from src.application import service_upgrade as upgrade

    args = _published_upgrade_case(tmp_path)
    monkeypatch.setattr(upgrade, "service_upgrade_check", lambda **kw: {
        "ok": False, "latest_version": "1.0.1", "release_tag": "v1.0.1", "status": "upgrade_check_failed",
    })
    out = upgrade.service_upgrade(**args, target_version="1.0.1", confirm=True)
    assert out["status"] == "no_target_version" and out["changed"] is False
    assert out["target_version"] is out["release_tag"] is None
    assert out["check"]["latest_version"] == "1.0.1"
    assert args["repo_root"].resolve().name == "1.0.0"


def test_source_failure_does_not_guess_mirror_remote(monkeypatch, tmp_path):
    from src.application import service_upgrade as upgrade

    args = _published_upgrade_case(tmp_path)
    cache_repo = args["cache_root"] / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        assert command == ["git", f"--git-dir={cache_repo}", "config", "--get", "remote.origin.url"]
        return subprocess.CompletedProcess(command, 1, "", "no such remote")
    _, http_calls = _mock_published_releases(monkeypatch)
    out = upgrade.service_upgrade_check(repo_root=args["repo_root"], runtime_root=args["runtime_root"],
                                        cache_root=args["cache_root"], remote_name="upstream", run_cmd=runner)
    assert out["status"] == "upgrade_check_failed"
    assert out["version_check"]["error"] == "upgrade_source_unavailable"
    assert "origin" in out["version_check"]["message"]
    assert len(calls) == 1 and not http_calls



def _release(path: Path, version: str) -> Path:
    _write_upgrade_release_skeleton(path, version)
    return path


def _upgrade_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    releases = tmp_path / "releases"
    previous = _release(releases / "3.5.1", "3.5.1")
    target = _release(releases / "3.5.2", "3.5.2")
    current = tmp_path / "current"
    current.symlink_to(previous, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return current, previous, target, runtime



def _stub_upgrade_preparation(monkeypatch: pytest.MonkeyPatch, module, target: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        module,
        "service_upgrade_check",
        lambda **_kwargs: {"ok": True, "latest_version": "3.5.2", "release_tag": "v3.5.2"},
    )
    monkeypatch.setattr(
        module,
        "_materialize_release_from_git_cache",
        lambda **_kwargs: {"status": "reused", "target_dir": str(target)},
    )
    monkeypatch.setattr(module, "_ensure_release_runtime", lambda **_kwargs: {"status": "ready"})
    monkeypatch.setattr(module, "_run_required", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        module,
        "_prepare_runtime_configs_for_release",
        lambda **_kwargs: {"status": "prepared", "items": []},
    )
    monkeypatch.setattr(module, "_commit_prepared_runtime_configs", lambda **_kwargs: {"status": "committed"})
    monkeypatch.setattr(module, "_validate_committed_runtime_configs", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_load_service_profile", lambda _runtime: {})



def _fail_lambda(message: str):
    """A seam stub that fails the test if the seam is reached."""

    def _fail(**_kwargs):
        pytest.fail(message)

    return _fail



def _upgrade(
    module,
    *,
    current: Path,
    runtime: Path,
    target: Path,
    confirm: bool,
    restart_services: bool,
):  # type: ignore[no-untyped-def]
    """Run the upgrade entrypoint with the fixture's fixed release/runtime arguments."""
    return module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=target.parent,
        target_version="3.5.2",
        confirm=confirm,
        restart_services=restart_services,
        preserve_activation_state=True,
    )



def _cleanup_fixture(tmp_path: Path, versions: list[str]) -> tuple[Path, list[Path], Path]:
    """Create one release dir per version (newest first) and point `current` at the newest."""
    releases = tmp_path / "releases"
    created = [_release(releases / version, version) for version in versions]
    current = tmp_path / "current"
    current.symlink_to(created[0], target_is_directory=True)
    return releases, created, current



def _cleanup(module, *, current: Path, releases: Path, **kwargs):  # type: ignore[no-untyped-def]
    """Run the cleanup entrypoint with the fixture's fixed keep/confirm arguments."""
    return module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
        **kwargs,
    )



def test_upgrade_verify_reports_unsupported_runtime(monkeypatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    current, previous, _target, runtime = _upgrade_fixture(tmp_path)
    (previous / "src/application/bot/runtime.py").unlink()
    (runtime / "config.us.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(module, "_load_service_profile", lambda _runtime: {})
    monkeypatch.setattr(module, "_runtime_config_verify_summary", lambda **_kwargs: {"ok": True})
    out = module.service_upgrade_verify(repo_root=current, runtime_root=runtime, check_latest=False)
    assert out["ok"] is False
    assert out["status"] == "attention_required"
    assert out["repo_root_resolved"] == str(previous)
    assert out["runtime_readiness"]["ok"] is False
    assert out["runtime_readiness"]["status"] == "unsupported_runtime"
    assert str(previous) in out["runtime_readiness"]["error"]
    assert out["runtime_readiness"]["remediation"]
    assert current.resolve() == previous


def test_upgrade_preview_is_read_only_and_keeps_services_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    monkeypatch.setattr(module, "_materialize_release_from_git_cache", _fail_lambda("preview materialized release"))
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_lambda("preview switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", _fail_lambda("preview restarted service"))

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=False, restart_services=False)

    assert out["status"] == "dry_run"
    assert current.resolve() == previous



def test_upgrade_checks_target_runtime_before_switch_and_does_not_restart_in_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    assert current.resolve() == previous
    monkeypatch.setattr(
        module, "_restart_services_from_loaded_profile", _fail_lambda("maintenance restarted service")
    )

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=True, restart_services=False)

    assert out["status"] == "upgraded"
    assert out["ok"] is True
    assert out["runtime_readiness"] == {"ok": True, "runtime": "python"}
    assert out["symlink_switched"] is True
    assert out["target_dir"] == str(target)
    assert out["restarted_services"] == []
    assert current.resolve() == target



@pytest.mark.parametrize("confirm", [False, True])
def test_rollback_rejects_legacy_runtime_before_config_switch_or_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, confirm: bool) -> None:
    import src.application.service_upgrade as module

    current, old, new, runtime = _upgrade_fixture(tmp_path)
    current.unlink()
    current.symlink_to(new, target_is_directory=True)
    (old / "src/application/bot/runtime.py").unlink()
    monkeypatch.setattr(module, "_prepare_runtime_configs_for_release", _fail_lambda("legacy rollback prepared config"))
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_lambda("rollback switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", _fail_lambda("rollback restarted service"))

    out = module.service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=old.parent,
        to_version="3.5.1",
        confirm=confirm,
    )

    assert out["status"] == "unsupported_runtime"
    assert out["changed"] is False
    assert current.resolve() == new


def test_failure_before_switch_does_not_resume_old_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    monkeypatch.setattr(
        module,
        "_load_service_profile",
        lambda _runtime: {"service_provider": "systemd"},
    )

    def _fail_switch(**_kwargs):
        raise module.ServiceTransitionError(
            "switch failed before current changed", status="switch_failed"
        )

    monkeypatch.setattr(
        module, "capture_preserved_timer_activation_states", lambda **_kwargs: {}
    )
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_switch)
    monkeypatch.setattr(module, "_compensate_service_transition", _fail_lambda("failure before switch compensated"))
    monkeypatch.setattr(
        module, "_restart_services_from_loaded_profile", _fail_lambda("failure path started old service")
    )

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=True, restart_services=True)

    assert out["status"] == "switch_failed"
    assert out["compensation"] == {}
    assert out["symlink_switched"] is False
    assert current.resolve() == previous



def test_activation_snapshot_failure_stops_before_runtime_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    monkeypatch.setattr(
        module,
        "_load_service_profile",
        lambda _runtime: {"service_provider": "systemd"},
    )

    def _fail_activation(**_kwargs):
        raise module.ServiceTransitionError(
            "activation snapshot failed",
            status="service_activation_snapshot_failed",
        )

    monkeypatch.setattr(
        module,
        "capture_preserved_timer_activation_states",
        _fail_activation,
    )
    monkeypatch.setattr(
        module,
        "_ensure_release_runtime",
        _fail_lambda("snapshot failure prepared target runtime"),
    )
    monkeypatch.setattr(
        module,
        "_runtime_readiness",
        _fail_lambda("snapshot failure checked target runtime"),
    )

    out = _upgrade(
        module,
        current=current,
        runtime=runtime,
        target=target,
        confirm=True,
        restart_services=True,
    )

    assert out["status"] == "service_activation_snapshot_failed"
    assert out["compensation"] == {}
    assert out["symlink_switched"] is False
    assert current.resolve() == previous



@pytest.mark.parametrize("runtime_provided", [False, True])
def test_cleanup_retains_legacy_release_without_parsing_receipts(monkeypatch, tmp_path: Path, runtime_provided: bool) -> None:
    import src.application.service_cleanup as module

    releases, created, current = _cleanup_fixture(tmp_path, ["3.5.3", "3.5.2", "3.5.1", "3.5.0"])
    _active, _previous, legacy, stale = created
    (legacy / "src/application/bot/runtime.py").unlink()
    historical = legacy / "output_shared/state/pi_sessions.sqlite3.migration.json"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b"invalid historical receipt; never parsed")
    before = historical.read_bytes()
    runtime = tmp_path / "runtime"
    original_lock = module.single_instance_lock
    guard_paths: list[Path] = []

    def isolated_default_guard(path, **kwargs):
        guard_paths.append(path)
        assert path == Path("/var/lib/options-monitor/locks/upgrade.lock.guard")
        return original_lock(runtime / "locks/upgrade.lock.guard", **kwargs)

    if not runtime_provided:
        monkeypatch.setattr(module, "single_instance_lock", isolated_default_guard)
    out = _cleanup(module, current=current, releases=releases, **({"runtime_root": runtime} if runtime_provided else {}))
    assert out["status"] == "cleaned"
    assert out["runtime_root"] == (str(runtime) if runtime_provided else None)
    assert guard_paths == ([] if runtime_provided else [Path("/var/lib/options-monitor/locks/upgrade.lock.guard")])
    assert {item["version"] for item in out["kept_releases"]} == {"3.5.3", "3.5.2", "3.5.1"}
    assert out["retained_legacy_releases"] == [{"path": str(legacy), "version": "3.5.1"}]
    assert "pi_retained_releases" not in out and "pi_migration_receipt_count" not in out
    assert historical.read_bytes() == before
    assert legacy.exists()
    assert not stale.exists()
