from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

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
    _fake_pi_runtime_prepare,
    _fake_release_target_query,
    _legacy_credential_migration_fixture,
    _credential_migration_runner,
)

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

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_opend=True,
        include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=helper,
        feishu_agent_credential_store=agent_store,
        feishu_holdings_credential_store=holdings_store,
        feishu_agent_credential_env_file=runtime_env,
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

def test_deepseek_credential_is_bound_only_to_selected_assistant_service(
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
        "assistant:\n"
        "  enabled: true\n"
        "  copilot:\n"
        "    enabled: true\n"
        "  active_model: deepseek-default\n"
        "  models:\n"
        "    deepseek-default:\n"
        "      provider: deepseek\n"
        "      model: deepseek-chat\n"
        "      context_window_tokens: 24000\n"
        "      api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_yaml=config_yaml,
        include_feishu_ws=True,
        include_secret_credentials=True,
        secret_credential_store_root=store,
        deploy_user="liuxie",
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
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=helper,
        feishu_agent_credential_store=tmp_path / "credstore" / "agent",
        feishu_holdings_credential_store=tmp_path / "credstore" / "holdings",
        feishu_agent_credential_env_file=tmp_path / "run" / "credential.env",
        use_default_deploy_user=False,
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

    before = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        run_cmd=_run_cmd,
    )

    assert before["summary"]["status"] == "error"
    assert before["missing_required_units"] == [FEISHU_AGENT_CREDENTIAL_SERVICE]
    assert str(helper) in before["missing_managed_files"]
    assert before["missing_managed_files"]

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

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
    mode_drift = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        run_cmd=_run_cmd,
    )
    assert mode_drift["mode_mismatched_managed_files"] == [str(helper)]
    assert mode_drift["execution_drift_units"] == [
        FEISHU_AGENT_CREDENTIAL_SERVICE
    ]

    repaired = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
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
    stale_drift = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        run_cmd=_run_cmd,
    )
    assert stale_drift["extra_managed_files"] == [stale_key]

    retired = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_secret_credentials=True,
        use_default_deploy_user=False,
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

    before = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
    )
    assert secret_keys
    assert all(key in before["missing_managed_files"] for key in secret_keys)

    installed = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
    )
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
    mismatched = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
    )
    assert mismatched["mismatched_managed_files"] == [
        str(first_item["install_path"])
    ]

    repaired = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
    )
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
    stale = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
    )
    assert stale["extra_managed_files"] == [stale_key]

    retired = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
    )
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
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_secret_credentials=True,
        secret_credential_delivery="runtime-files",
        use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    (runtime / "service.profile.json").write_text(
        files["service.profile.json"]["content"],
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    before = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
    )
    assert str(helper_path) in before["missing_managed_files"]
    assert before["missing_managed_files"]

    installed = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
    )
    assert installed["missing_managed_files"] == []
    assert installed["mismatched_managed_files"] == []
    assert installed["mode_mismatched_managed_files"] == []
    assert helper_path.is_file()
    assert helper_path.stat().st_mode & 0o777 == 0o755
    assert "systemd-creds" in helper_path.read_text(encoding="utf-8")

    unsafe_owner = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        managed_root_uid=os.getuid() + 1,
        managed_root_gid=os.getgid(),
    )
    assert str(helper_path) in unsafe_owner["mode_mismatched_managed_files"]

    helper_copy = tmp_path / "credential-helper-copy"
    helper_copy.write_text(helper_path.read_text(encoding="utf-8"), encoding="utf-8")
    helper_copy.chmod(0o755)
    helper_path.unlink()
    helper_path.symlink_to(helper_copy)
    symlinked = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        managed_root_uid=os.getuid(),
        managed_root_gid=os.getgid(),
    )
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

    base_bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        use_default_deploy_user=False,
    )
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        if "show" in command and "--property=Result" in command:
            return subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_yaml=config_yaml,
    )
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

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

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

def test_service_drift_detects_mismatched_timer_content(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    (systemd_root / "options-monitor-auto-close-us.timer").write_text(
        (systemd_root / "options-monitor-auto-close-us.timer")
        .read_text(encoding="utf-8")
        .replace("OnCalendar=*-*-* 09:07:00 Asia/Shanghai", "OnCalendar=*-*-* 05:30:00 Asia/Shanghai"),
        encoding="utf-8",
    )
    (systemd_root / "options-monitor-projection-verify.timer").write_text(
        (systemd_root / "options-monitor-projection-verify.timer")
        .read_text(encoding="utf-8")
        .replace("OnCalendar=*-*-* 09:30:00 Asia/Shanghai", "OnCalendar=*-*-* 06:00:00 Asia/Shanghai"),
        encoding="utf-8",
    )

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert out["summary"]["status"] == "warn"
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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"]
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    def _unexpected_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("custom unit roots must not query host systemctl")

    monkeypatch.setattr(service_drift_module.subprocess, "run", _unexpected_run)

    out = service_drift_module.service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
    )

    assert out["summary"]["status"] == "ok"
    assert out["activation_states"] == {}
    assert out["active_states"] == {}
    assert out["activation_drift_units"] == []

def test_service_drift_confirm_writes_missing_timer_and_profile(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_yaml=config_yaml,
    )
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["summary"]["status"] == "ok"
    assert any(item.get("sudo_fallback") for item in out["operations"] if item.get("operation") == "write_unit")
    assert ["sudo", "-n", "systemctl", "daemon-reload"] in calls
    assert ["sudo", "-n", "systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls

def test_service_drift_retires_installed_feishu_ws_when_profile_no_longer_declares_it(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"], include_feishu_ws=True)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile.pop("feishu_ws", None)
    profile["services"] = [item for item in profile["services"] if item["name"] != "options-monitor-feishu-ws.service"]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))

    assert "options-monitor-feishu-ws.service" not in out["expected_services"]
    assert {"name": "options-monitor-feishu-ws.service"} not in refreshed["services"]
    assert "feishu_ws" not in refreshed
    assert not (systemd_root / "options-monitor-feishu-ws.service").exists()
    assert out["before"]["extra_installed_units"] == ["options-monitor-feishu-ws.service"]
    assert out["applied"]["retired_units"] == ["options-monitor-feishu-ws.service"]
    assert ["systemctl", "disable", "--now", "options-monitor-feishu-ws.service"] in calls

def test_service_drift_retires_removed_position_advice_promotion_units(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )
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

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=False,
    )

    assert out["extra_profile_units"] == sorted(retired_units)
    assert out["extra_installed_units"] == sorted(retired_units)
    assert out["confirmed"] is False
    assert out["changed"] is False
    assert out["operations"] == []
    assert all((systemd_root / name).is_file() for name in retired_units)

def test_service_drift_repairs_masked_expected_timer_and_reads_back_enabled(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
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
        return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

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

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )
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

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["before"]["activation_states"][target] == "enabled"
    assert out["before"]["active_states"][target] == "inactive"
    assert out["before"]["activation_drift_units"] == [target]
    assert out["active_states"][target] == "active"
    assert out["activation_drift_units"] == []
    assert out["summary"]["status"] == "ok"
    assert ["systemctl", "enable", "--now", target] in calls

def test_service_drift_preserves_paused_timer_while_updating_definition(
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
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
    )
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
                command, 0, stdout="enabled\n", stderr=""
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

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        activation_policy=SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
        preserved_activation_states={
            target: {
                "activation_state": "enabled",
                "active_state": "inactive",
            }
        },
        run_cmd=_run_cmd,
    )

    assert out["before"]["observed_activation_drift_units"] == sorted(
        [other_inactive, target]
    )
    assert out["before"]["activation_drift_units"] == [other_inactive]
    assert out["before"]["preserved_activation_units"] == [target]
    assert out["active_states"][target] == "inactive"
    assert out["activation_drift_units"] == []
    assert out["preserved_activation_units"] == [target]
    assert out["summary"]["status"] == "warn"
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
        "assistant:\n"
        "  enabled: true\n"
        "  copilot:\n"
        "    enabled: true\n"
        "  active_model: deepseek-default\n"
        "  models:\n"
        "    deepseek-default:\n"
        "      provider: deepseek\n"
        "      model: deepseek-chat\n"
        "      context_window_tokens: 128000\n"
        "      api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
        config_yaml=config_yaml,
        env_file=runtime / "options-monitor.env",
        include_feishu_ws=True,
        include_wechat_clawbot=True,
        wechat_clawbot_allowed_senders="wechat:test-user",
        include_secret_credentials=True,
        secret_credential_store_root=store,
        use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    old_profile = json.loads(files["service.profile.json"]["content"])
    inbound_services = (
        "options-monitor-feishu-ws.service",
        "options-monitor-wechat-clawbot.service",
    )
    for service_name in inbound_services:
        old_profile["secret_credentials"]["service_credentials"][service_name].append(
            "copilot.cursor_hmac_key"
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
        "copilot.cursor_hmac_key"
        not in refreshed_profile["secret_credentials"]["service_credentials"][name]
        for name in inbound_services
    )
    final = service_drift(**drift_kwargs)
    assert final["mismatched_managed_files"] == []
    assert final["profile_content_changed"] is False
    assert final["activation_drift_units"] == []
    assert final["preserved_activation_units"] == sorted(paused_timers)


@pytest.mark.parametrize(
    "retired_suffixes",
    [("recorder",), ("top1",), ("recorder", "top1")],
)
def test_service_drift_removes_retired_strategy_lab_profile_keys(
    tmp_path: Path,
    retired_suffixes: tuple[str, ...],
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
    )
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}[
            "service.profile.json"
        ]["content"]
    )
    retired_keys = [f"strategy_lab_{suffix}" for suffix in retired_suffixes]
    for key in retired_keys:
        profile[key] = {"enabled": True}
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    drift_kwargs = {
        "repo_root": repo,
        "runtime_root": runtime,
        "systemd_unit_root": systemd_root,
    }
    before = service_drift(**drift_kwargs)
    assert before["profile_content_changed"] is True
    assert before["mismatched_units"] == []
    assert before["extra_installed_units"] == []

    applied = service_drift(**drift_kwargs, confirm=True)
    assert applied["applied"]["profile_written"] is True
    assert applied["applied"]["written_units"] == []
    assert applied["applied"]["retired_units"] == []
    assert applied["profile_content_changed"] is False
    readback = json.loads(
        (runtime / "service.profile.json").read_text(encoding="utf-8")
    )
    assert all(key not in readback for key in retired_keys)

    final = service_drift(**drift_kwargs)
    assert final["summary"]["status"] == "ok"
    assert final["profile_content_changed"] is False


@pytest.mark.parametrize("provider", ["systemd", "launchd"])
@pytest.mark.parametrize(
    "retired_suffixes",
    [("recorder",), ("top1",), ("recorder", "top1")],
)
def test_service_drift_removes_retired_keys_from_empty_services_profile(
    tmp_path: Path,
    provider: str,
    retired_suffixes: tuple[str, ...],
) -> None:
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    profile = {
        "schema_version": 1,
        "service_provider": provider,
        "repo_root": str(repo),
        "runtime_root": str(runtime),
        "accounts": ["lx"],
        "markets": ["hk"],
        "config_paths": {"hk": str(runtime / "config.hk.json")},
        "services": [],
    }
    retired_keys = [f"strategy_lab_{suffix}" for suffix in retired_suffixes]
    for key in retired_keys:
        profile[key] = {"enabled": True}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    drift_kwargs = {
        "repo_root": repo,
        "runtime_root": runtime,
        "systemd_unit_root": systemd_root,
    }

    before = service_drift(**drift_kwargs)
    assert before["reason"] == "retired_service_profile_keys_present"
    assert before["profile_content_changed"] is True
    assert before["expected_services"] == []
    assert before["summary"]["status"] == "warn"

    applied = service_drift(**drift_kwargs, confirm=True)
    assert applied["applied"]["profile_written"] is True
    assert applied["applied"]["written_units"] == []
    assert applied["apply_errors"] == []
    readback = json.loads(profile_path.read_text(encoding="utf-8"))
    assert readback["services"] == []
    assert all(key not in readback for key in retired_keys)

    final = service_drift(**drift_kwargs)
    assert final["reason"] == "service_profile_has_no_services"
    assert final["profile_content_changed"] is False
    assert final["summary"]["status"] == "skipped"


def test_service_drift_discovers_installed_wechat_clawbot_as_managed_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_wechat_clawbot=True,
        wechat_clawbot_label="ops",
        wechat_clawbot_allowed_senders="wechat:user_1",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [item for item in profile["services"] if item["name"] != "options-monitor-wechat-clawbot.service"]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root, confirm=True)
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
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_opend=True,
        opend_root=opend,
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert out["summary"]["status"] == "ok"
    assert "options-monitor-opend.service" in out["expected_services"]
    assert out["mismatched_units"] == []

def test_service_drift_preserves_quality_monitoring_opt_in_and_detects_metadata_drift(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us", "hk"],
        include_quality_monitoring=True,
    )
    profile_item = {item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]
    profile = json.loads(profile_item["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    clean = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert clean["summary"]["status"] == "ok"
    assert clean["profile_content_changed"] is False
    assert clean["extra_profile_units"] == []
    assert clean["extra_installed_units"] == []
    assert "options-monitor-quality-http.service" in clean["expected_services"]
    assert "options-monitor-quality-day-end-us.timer" in clean["expected_services"]
    assert "options-monitor-quality-day-end-hk.timer" in clean["expected_services"]

    profile["quality_monitoring"]["http"]["port"] = 9999
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    drifted = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert drifted["summary"]["status"] == "warn"
    assert drifted["profile_content_changed"] is True
    assert drifted["mismatched_units"] == []

def test_service_upgrade_verify_returns_compact_read_only_summary(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_verify, write_upgrade_status

    releases = tmp_path / "releases"
    release = releases / "1.2.3"
    release.mkdir(parents=True)
    (release / "VERSION").write_text("1.2.3\n", encoding="utf-8")
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

    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime",
        markets=["us"],
    )

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

    def _service_drift(**kwargs):  # type: ignore[no-untyped-def]
        drift_calls.append(dict(kwargs))
        if not kwargs.get("confirm"):
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
        return {"summary": {"status": "ok"}, "manual_actions": []}

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
    monkeypatch.setattr(
        service_upgrade_module,
        "_ensure_release_runtime",
        lambda **_kwargs: {"status": "ready"},
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

    out = service_upgrade_module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        preserve_activation_state=True,
        run_cmd=lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
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
    assert len(confirmed_calls) == 2
    assert all(
        item["activation_policy"] == "preserve-existing"
        and item["preserved_activation_states"] == expected_snapshot
        for item in confirmed_calls
    )

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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if "show" in command and "--property=Result" in command:
            return subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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
    assert ["git", "clone", "--mirror", "https://example.invalid/repo.git", str(cache_repo)] in calls
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

def test_service_upgrade_check_falls_back_to_git_cache_when_current_release_has_no_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_check

    repo = tmp_path / "releases" / "1.0.0"
    _write_upgrade_release_skeleton(repo, "1.0.0")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls: list[tuple[list[str], str | None]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(command), kwargs.get("cwd")))
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

def test_service_upgrade_check_reports_no_upgrade_available(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_check

    repo = tmp_path / "releases" / "1.0.1"
    _write_upgrade_release_skeleton(repo, "1.0.1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="fatal: not a git repository")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "config"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

def test_service_upgrade_pi_prepare_failure_keeps_current_and_precedes_config(monkeypatch, tmp_path: Path) -> None:
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
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            if command[:2] == ["npm", "ci"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="npm failed\n")
            return pi_runtime
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
    assert out["runtime_prepare"]["pi_runtime"]["node_version"] == "v22.19.0"

def test_service_upgrade_restart_failure_is_non_success_and_restores_previous_symlink(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "deploy_user": "liuxie",
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["sudo", "-n", "systemctl", "restart"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Access denied\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "configs").mkdir()
            (target / "configs" / "system.json").write_text("{}", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "configs").mkdir()
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"command": list(command), "cwd": kwargs.get("cwd")})
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "hk"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="build failed")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
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
        if command[:4] == ["./om", "config", "build-assistant", "--source"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        if command[:6] == ["./om", "config", "build", "--source", "legacy", "--market"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="legacy build should not run")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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
    assert artifact_by_kind_market[("assistant_config", None)]["live_path"] == str(
        runtime / "resolved" / "config.assistant.json"
    )
    assert hk_runtime.exists()
    assert us_runtime.exists()
    for runtime_config in (hk_runtime, us_runtime):
        payload = json.loads(runtime_config.read_text(encoding="utf-8"))
        assert '"output_mode"' not in json.dumps(payload, sort_keys=True)
    assert (runtime / "resolved" / "config.assistant.json").exists()
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
    assistant_runtime = runtime / "resolved" / "config.assistant.json"
    assistant_runtime.parent.mkdir()
    us_runtime.write_text('{"generation": "old"}\n', encoding="utf-8")
    assistant_runtime.write_text('{"generation": "old-assistant"}\n', encoding="utf-8")
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
        if command[:4] == ["./om", "config", "build-assistant", "--source"]:
            assert json.loads(assistant_runtime.read_text(encoding="utf-8")) == {"generation": "old-assistant"}
            Path(command[-1]).write_text('{"generation": "new-assistant"}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        if command[:4] == ["./om", "config", "validate", "--config-path"] and command[4] == str(us_runtime):
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
    assert json.loads(assistant_runtime.read_text(encoding="utf-8")) == {"generation": "old-assistant"}

def test_service_upgrade_blocks_major_by_default(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        target_version="2.0.0",
        run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
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
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        target_query = _fake_release_target_query(list(command), tags=("1.0.1",))
        if target_query is not None:
            return target_query
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "hk"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        if command[:4] == ["./om", "config", "build-assistant", "--source"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built assistant\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

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

def test_service_rollback_preserves_paused_timer_snapshot(
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

    def _service_drift(**kwargs):  # type: ignore[no-untyped-def]
        drift_calls.append(dict(kwargs))
        if not kwargs.get("confirm"):
            return {
                "activation_states": {target: "enabled"},
                "active_states": {target: "inactive"},
            }
        return {
            "summary": {"status": "warn"},
            "preserved_activation_units": [target],
        }

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
    monkeypatch.setattr(service_upgrade_module, "service_drift", _service_drift)

    out = service_upgrade_module.service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        to_version="1.0.0",
        confirm=True,
        restart_services=False,
        preserve_activation_state=True,
        run_cmd=lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
    )

    confirmed_calls = [item for item in drift_calls if item.get("confirm")]
    assert out["status"] == "rolled_back"
    assert current.resolve() == v100.resolve()
    assert out["activation_policy"] == "preserve-existing"
    assert out["preserved_activation_units"] == [target]
    assert len(confirmed_calls) == 1
    assert confirmed_calls[0]["activation_policy"] == "preserve-existing"
    assert confirmed_calls[0]["preserved_activation_states"] == {
        target: {
            "activation_state": "enabled",
            "active_state": "inactive",
        }
    }

def test_service_rollback_switches_current_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_rollback, write_upgrade_status

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v100.mkdir(parents=True)
    v101.mkdir()
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v101 / "VERSION").write_text("1.0.1\n", encoding="utf-8")
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

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:7] == ["./om", "config", "build", "--source", "yaml", "--market", "us"]:
            assert current.resolve() == v101.resolve()
            assert json.loads(us_runtime.read_text(encoding="utf-8")) == {"release": "1.0.1"}
            Path(command[-1]).write_text('{"release": "1.0.0"}\n', encoding="utf-8")
        elif command[:4] == ["./om", "config", "build-assistant", "--source"]:
            Path(command[-1]).write_text('{"release": "1.0.0"}\n', encoding="utf-8")
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
    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path,
        runtime_root=tmp_path,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": cfg_path},
    )
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
    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path,
        runtime_root=tmp_path,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": cfg_path},
    )
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

    out = service_cleanup(repo_root=repo, releases_root=tmp_path / "releases", confirm=True)

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
    current = tmp_path / "current"
    current.symlink_to(active, target_is_directory=True)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")

    out = service_cleanup(
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
