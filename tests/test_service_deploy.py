from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


CURRENT_PYTHON = sys.executable
PYTHON_MINOR = f"{sys.version_info.major}.{sys.version_info.minor}"


def _futu_service_account(
    *,
    host: str = "127.0.0.1",
    port: object = 11111,
    opend_root: str | Path | None = None,
) -> dict[str, object]:
    futu: dict[str, object] = {"host": host, "port": port}
    if opend_root is not None:
        futu["opend_root"] = str(opend_root)
    return {"type": "futu", "futu": futu}


def _write_service_account_config(path: Path, settings: dict[str, dict[str, object]]) -> Path:
    path.write_text(
        json.dumps({"accounts": list(settings), "account_settings": settings}),
        encoding="utf-8",
    )
    return path


def _two_futu_service_accounts(
    tmp_path: Path,
    *,
    lx_port: object = 11111,
    sy_port: object = 11112,
) -> dict[str, dict[str, object]]:
    return {
        "lx": _futu_service_account(port=lx_port, opend_root=tmp_path / "opend-lx"),
        "sy": _futu_service_account(port=sy_port, opend_root=tmp_path / "opend-sy"),
    }


def _write_upgrade_release_skeleton(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (path / "configs").mkdir(exist_ok=True)
    (path / "requirements").mkdir(exist_ok=True)
    (path / "constraints").mkdir(exist_ok=True)
    (path / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
    (path / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
    (path / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
    (path / "agent-runtime").mkdir(exist_ok=True)
    (path / "agent-runtime" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (path / "scripts").mkdir(exist_ok=True)
    smoke = path / "scripts" / "pi_runtime_smoke.sh"
    smoke.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    smoke.chmod(0o755)


def test_upgrade_lock_removes_stale_pid_file(tmp_path: Path) -> None:
    from src.application.service_upgrade import _UpgradeLock

    lock_path = tmp_path / "runtime" / "locks" / "upgrade.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("999999999\n", encoding="utf-8")

    with _UpgradeLock(lock_path):
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())

    assert not lock_path.exists()


def test_upgrade_lock_keeps_active_pid_file(tmp_path: Path) -> None:
    from src.application.service_upgrade import _UpgradeLock

    lock_path = tmp_path / "runtime" / "locks" / "upgrade.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(RuntimeError, match="upgrade lock already exists"):
        with _UpgradeLock(lock_path):
            pass

    assert lock_path.exists()


def test_render_systemd_bundle_uses_runtime_root_and_canonical_entrypoints(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    tick_timer = files["systemd/options-monitor-tick-us.timer"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    verify = files["systemd/options-monitor-projection-verify.service"]["content"]

    assert 'Environment="OM_RUNTIME_ROOT=' + str(runtime) + '"' in tick
    assert "User=" not in tick
    assert 'Environment="HOME=' not in tick
    assert str(repo / "om") + " run tick-cron --market us" in tick
    assert "--lock-path " + str(runtime / "locks" / "tick-us.lock") in tick
    assert str(repo / "om") + " option-positions auto-close-expired" in files["systemd/options-monitor-auto-close-us.service"]["content"]
    auto_close = files["systemd/options-monitor-auto-close-us.service"]["content"]
    assert "--apply --yes --quiet" in auto_close
    assert "TimeoutStartSec=600" in auto_close
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 America/New_York" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer
    assert str(repo / "om") + " run trade-intake" in intake
    runtime_status = files["systemd/options-monitor-runtime-status.service"]["content"]
    assert str(repo / "om") + " status --profile-path " + str(runtime / "service.profile.json") in runtime_status
    assert "--journal-summary" in runtime_status
    assert str(repo / "om-agent") not in runtime_status
    assert "Restart=always" in intake
    assert "RestartPreventExitStatus=78" in intake
    assert "RestartPreventExitStatus=" not in tick
    assert "RestartPreventExitStatus=" not in runtime_status
    assert "RestartPreventExitStatus=" not in verify
    assert "UMask=0077" in tick
    assert "UMask=0077" in intake


def test_render_systemd_bundle_omits_retired_ai_evidence_collector(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "accounts:\n"
        "  lx:\n"
        "    type: futu\n"
        "    futu:\n"
        '      account_id: "REAL_12345678"\n'
        "      host: 127.0.0.1\n"
        "      port: 11111\n"
        "markets:\n"
        "  us:\n"
        "    accounts: [lx]\n"
        "    symbols: [NVDA]\n",
        encoding="utf-8",
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_yaml=config_yaml,
    )
    paths = {item["relative_path"] for item in bundle["files"]}
    assert "systemd/options-monitor-ai-evidence-collector.service" not in paths
    assert "systemd/options-monitor-ai-evidence-collector.timer" not in paths
    assert "ai_evidence_collector" not in str(bundle)


def test_render_systemd_bundle_service_hardening() -> None:
    from src.application.service_deploy import render_service_bundle

    repo = Path("/tmp/om-svc-repo")
    runtime = Path("/tmp/om-svc-runtime")
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us", "hk"],
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    runtime_status = files["systemd/options-monitor-runtime-status.service"]["content"]
    verify = files["systemd/options-monitor-projection-verify.service"]["content"]
    verify_timer = files["systemd/options-monitor-projection-verify.timer"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    auto_close_us_timer = files["systemd/options-monitor-auto-close-us.timer"]["content"]
    auto_close_hk_timer = files["systemd/options-monitor-auto-close-hk.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])
    assert "TimeoutStartSec=" not in tick
    assert "TimeoutStartSec=" not in runtime_status
    assert "TimeoutStartSec=" not in verify
    assert "TimeoutStartSec=" not in intake
    assert "RuntimeMaxSec=" not in "\n".join(item["content"] for item in files.values())
    assert "[Install]\nWantedBy=multi-user.target" in intake
    assert "[Install]\nWantedBy=multi-user.target" not in tick
    assert "OnCalendar=*-*-* 09:07:00 Asia/Shanghai" in auto_close_us_timer
    assert "OnCalendar=*-*-* 09:05:00 Asia/Shanghai" in auto_close_hk_timer
    assert str(repo / "om") + " option-positions --data-config " + str(runtime / "portfolio.runtime.json") in verify
    assert "verify-projection --mode auto" in verify
    assert "[Install]\nWantedBy=multi-user.target" not in verify
    assert "OnCalendar=*-*-* 09:30:00 Asia/Shanghai" in verify_timer
    assert profile["service_provider"] == "systemd"
    assert profile["runtime_root"] == str(runtime)
    assert {"name": "options-monitor-tick-us.service"} in profile["services"]
    assert {"name": "options-monitor-tick-us.timer"} in profile["services"]
    assert {"name": "options-monitor-projection-verify.timer"} in profile["services"]
    assert not any(
        "position-advice" in str(item.get("name") or "")
        for item in profile["services"]
    )
    assert "position_advice_promotion" not in profile
    assert "deploy_user" not in profile
    assert "deploy_home" not in profile


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


def test_render_systemd_bundle_uses_per_unit_encrypted_credentials(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.secret_store import legacy_secret_env_names

    repo = tmp_path / "repo"
    repo.mkdir()
    store = tmp_path / "credstore.encrypted"
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        include_opend=True,
        include_feishu_ws=True,
        include_quality_monitoring=True,
        include_secret_credentials=True,
        secret_credential_store_root=store,
        deploy_user="liuxie",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])

    tick = files[
        "systemd/options-monitor-tick-us.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assert f"om-feishu-holdings-app-secret:{store}/om-feishu-holdings-app-secret" in tick
    assert f"om-feishu-bot-app-secret:{store}/om-feishu-bot-app-secret" in tick
    assert "om-quality-read-token" not in tick
    assert "om-inbound-operation-hmac-key" not in tick
    assert (
        "UnsetEnvironment=" + " ".join(sorted(legacy_secret_env_names()))
    ) in tick

    quality = files[
        "systemd/options-monitor-quality-http.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assert f"om-quality-read-token:{store}/om-quality-read-token" in quality
    assert "om-feishu-bot-app-secret" not in quality

    feishu_ws = files[
        "systemd/options-monitor-feishu-ws.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assert "om-feishu-bot-app-secret" in feishu_ws
    assert "om-feishu-holdings-app-secret" in feishu_ws
    assert "om-inbound-operation-hmac-key" in feishu_ws
    assert "om-quality-read-token" not in feishu_ws

    assert not any(
        item["relative_path"].endswith("options-monitor-materialize-feishu-agent-credential")
        for item in bundle["files"]
    )
    assert not any(
        item["relative_path"].endswith(
            "options-monitor-materialize-service-credentials"
        )
        for item in bundle["files"]
    )
    assert profile["secret_credentials"]["backend"] == "systemd"
    assert profile["secret_credentials"]["delivery"] == "load-credential-encrypted"
    assert profile["secret_credentials"]["legacy_env_materializer_enabled"] is False
    assert "options-monitor-opend.service" not in profile["secret_credentials"]["service_credentials"]


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
    assert (
        f"om-copilot-cursor-hmac-key:{store}/om-copilot-cursor-hmac-key"
        in assistant
    )


def test_render_systemd_bundle_uses_per_unit_runtime_file_credentials(tmp_path: Path) -> None:
    from src.application.service_deploy import (
        DEFAULT_SECRET_CREDENTIAL_HELPER,
        DEFAULT_SECRET_CREDENTIAL_RUNTIME_ROOT,
        render_service_bundle,
    )
    from src.application.secret_store import legacy_secret_env_names

    repo = tmp_path / "repo"
    repo.mkdir()
    store = tmp_path / "credstore.encrypted"
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        include_opend=True,
        include_feishu_ws=True,
        include_quality_monitoring=True,
        include_secret_credentials=True,
        secret_credential_delivery="runtime-files",
        secret_credential_store_root=store,
        deploy_user="liuxie",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])

    helper = files[
        "systemd/libexec/options-monitor-materialize-service-credentials"
    ]
    assert helper["install_path"] == str(DEFAULT_SECRET_CREDENTIAL_HELPER)
    assert helper["mode"] == 0o755
    assert helper["owner_uid"] == 0
    assert helper["owner_gid"] == 0
    assert "systemd-creds" in helper["content"]
    assert "print(secret" not in helper["content"]

    tick = files[
        "systemd/options-monitor-tick-us.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    tick_directory = DEFAULT_SECRET_CREDENTIAL_RUNTIME_ROOT / "options-monitor-tick-us.service"
    assert "LoadCredential=\n" in tick
    assert "LoadCredentialEncrypted=\n" in tick
    assert 'Environment="OM_SECRET_BACKEND=systemd"' in tick
    assert f'Environment="CREDENTIALS_DIRECTORY={tick_directory}"' in tick
    assert f"ExecStartPre=+{DEFAULT_SECRET_CREDENTIAL_HELPER} materialize" in tick
    assert "--unit options-monitor-tick-us.service" in tick
    assert "--deploy-user liuxie" in tick
    assert "--credential-id om-feishu-holdings-app-secret" in tick
    assert "--credential-id om-feishu-bot-app-secret" in tick
    assert "om-quality-read-token" not in tick
    assert f"ExecStopPost=+{DEFAULT_SECRET_CREDENTIAL_HELPER} cleanup" in tick
    assert "EnvironmentFile=" not in tick
    assert "OM_SECRET_BACKEND=env" not in tick
    assert (
        "UnsetEnvironment=" + " ".join(sorted(legacy_secret_env_names()))
    ) in tick

    quality = files[
        "systemd/options-monitor-quality-http.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assert "--credential-id om-quality-read-token" in quality
    assert "om-feishu-bot-app-secret" not in quality

    secret_profile = profile["secret_credentials"]
    assert secret_profile["backend"] == "systemd"
    assert secret_profile["delivery"] == "runtime-files"
    assert secret_profile["helper_path"] == str(DEFAULT_SECRET_CREDENTIAL_HELPER)
    assert secret_profile["runtime_root"] == str(DEFAULT_SECRET_CREDENTIAL_RUNTIME_ROOT)
    assert secret_profile["legacy_env_materializer_enabled"] is False


def test_render_rejects_unknown_secret_credential_delivery(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    with pytest.raises(ValueError, match="secret credential delivery"):
        render_service_bundle(
            target="systemd",
            repo_root=tmp_path,
            markets=["us"],
            include_secret_credentials=True,
            secret_credential_delivery="automatic-fallback",
        )


@pytest.mark.parametrize(
    ("deploy_user", "store_root", "error"),
    (
        (
            "liuxie\nExecStartPre=+/tmp/injected",
            "/etc/credstore.encrypted",
            "deploy user",
        ),
        (
            "liuxie",
            "/etc/credstore.encrypted\nExecStartPre=+/tmp/injected",
            "control characters",
        ),
        ("liuxie", "/etc/credstore.%n", "systemd expansion"),
        ("liuxie", "/etc/$CREDENTIAL_STORE", "systemd expansion"),
    ),
)
def test_render_runtime_credentials_rejects_systemd_exec_injection_inputs(
    tmp_path: Path,
    deploy_user: str,
    store_root: str,
    error: str,
) -> None:
    from src.application.service_deploy import render_service_bundle

    with pytest.raises(ValueError, match=error):
        render_service_bundle(
            target="systemd",
            repo_root=tmp_path,
            markets=["us"],
            include_secret_credentials=True,
            secret_credential_delivery="runtime-files",
            secret_credential_store_root=store_root,
            deploy_user=deploy_user,
        )


def test_render_native_credentials_rejects_systemd_store_expansion(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    with pytest.raises(ValueError, match="systemd expansion"):
        render_service_bundle(
            target="systemd",
            repo_root=tmp_path,
            markets=["us"],
            include_secret_credentials=True,
            secret_credential_delivery="load-credential-encrypted",
            secret_credential_store_root="/etc/credstore.%n",
        )


def test_render_rejects_mixed_legacy_and_per_unit_secret_modes(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    with pytest.raises(ValueError, match="mutually exclusive"):
        render_service_bundle(
            target="systemd",
            repo_root=tmp_path,
            markets=["us"],
            include_secret_credentials=True,
            include_feishu_agent_credential=True,
        )


def test_render_launchd_bundle_rejects_feishu_agent_credential(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    with pytest.raises(ValueError, match="supported only for systemd"):
        render_service_bundle(
            target="launchd",
            repo_root=tmp_path,
            include_feishu_agent_credential=True,
        )




def test_systemd_unit_rejects_non_positive_start_timeout(tmp_path: Path) -> None:
    from src.application.service_deploy import _systemd_unit

    with pytest.raises(ValueError, match="timeout_start_sec must be positive"):
        _systemd_unit(
            description="invalid",
            repo_root=tmp_path,
            runtime_root=tmp_path,
            exec_args=["/bin/true"],
            timeout_start_sec=0,
        )


def test_render_launchd_runtime_status_uses_bounded_journal_summary(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    content = files["launchd/com.options-monitor.runtime-status.plist"]["content"]
    assert str(repo / "om") in content
    assert "status" in content
    assert "--journal-summary" in content
    assert str(repo / "om-agent") not in content


def test_render_systemd_bundle_can_include_opend_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    opend = tmp_path / "futu-opend" / "current"
    repo.mkdir()
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

    files = {item["relative_path"]: item for item in bundle["files"]}
    opend_service = files["systemd/options-monitor-opend.service"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "WorkingDirectory=" + str(opend) in opend_service
    assert "ExecStart=" + str(opend / "FutuOpenD") in opend_service
    assert "Restart=always" in opend_service
    assert "[Install]\nWantedBy=multi-user.target" in opend_service
    assert "Before=options-monitor-trade-intake.service" in opend_service
    assert "After=network-online.target options-monitor-opend.service" in intake
    assert "Wants=network-online.target options-monitor-opend.service" in intake
    assert {"name": "options-monitor-opend.service"} in profile["services"]
    assert profile["opend"]["enabled"] is True
    assert profile["opend"]["root"] == str(opend)
    assert profile["opend"]["executable"] == str(opend / "FutuOpenD")
    assert profile["restart"]["services"] == [
        "options-monitor-opend.service",
        "options-monitor-trade-intake.service",
    ]
    assert "systemctl enable --now options-monitor-opend.service" in bundle["commands"]["enable"]


def test_render_systemd_bundle_uses_account_opend_services_from_config(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    opend_lx = tmp_path / "futu-opend-lx" / "current"
    opend_sy = tmp_path / "futu-opend-sy" / "current"
    config_path = tmp_path / "config.us.json"
    repo.mkdir()
    opend_lx.mkdir(parents=True)
    opend_sy.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "accounts": ["lx", "sy"],
                "account_settings": {
                    "lx": {
                        "type": "futu",
                        "futu": {
                            "account_id": "999000000000000001",
                            "host": "127.0.0.1",
                            "port": 11111,
                            "opend_root": str(opend_lx),
                        },
                    },
                    "sy": {
                        "type": "futu",
                        "futu": {
                            "account_id": "281756479859383817",
                            "host": "127.0.0.1",
                            "port": 11112,
                            "opend_root": str(opend_sy),
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    lx_service = files["systemd/options-monitor-opend-lx.service"]["content"]
    sy_service = files["systemd/options-monitor-opend-sy.service"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "WorkingDirectory=" + str(opend_lx) in lx_service
    assert "ExecStart=" + str(opend_lx / "FutuOpenD") in lx_service
    assert "WorkingDirectory=" + str(opend_sy) in sy_service
    assert "ExecStart=" + str(opend_sy / "FutuOpenD") in sy_service
    assert "After=network-online.target options-monitor-opend-lx.service options-monitor-opend-sy.service" in intake
    assert "Wants=network-online.target options-monitor-opend-lx.service options-monitor-opend-sy.service" in intake
    assert {"name": "options-monitor-opend-lx.service"} in profile["services"]
    assert {"name": "options-monitor-opend-sy.service"} in profile["services"]
    assert profile["opend"]["services"] == [
        {
            "account": "lx",
            "root": str(opend_lx),
            "executable": str(opend_lx / "FutuOpenD"),
            "service_name": "options-monitor-opend-lx.service",
        },
        {
            "account": "sy",
            "root": str(opend_sy),
            "executable": str(opend_sy / "FutuOpenD"),
            "service_name": "options-monitor-opend-sy.service",
        },
    ]
    assert "root" not in profile["opend"]
    assert profile["restart"]["services"] == [
        "options-monitor-opend-lx.service",
        "options-monitor-opend-sy.service",
        "options-monitor-trade-intake.service",
    ]
    assert "systemctl enable --now options-monitor-opend-lx.service" in bundle["commands"]["enable"]
    assert "systemctl enable --now options-monitor-opend-sy.service" in bundle["commands"]["enable"]


def test_render_systemd_bundle_uses_account_opend_services_from_yaml_config(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    opend_lx = tmp_path / "futu-opend-lx" / "current"
    opend_sy = tmp_path / "futu-opend-sy" / "current"
    config_yaml = tmp_path / "config.yaml"
    repo.mkdir()
    runtime.mkdir()
    opend_lx.mkdir(parents=True)
    opend_sy.mkdir(parents=True)
    config_yaml.write_text(
        f"""\
accounts:
  lx:
    type: futu
    futu:
      account_id: "REAL_12345678"
      host: 127.0.0.1
      port: 11111
      opend_root: {opend_lx}
  sy:
    type: futu
    futu:
      account_id: "REAL_87654321"
      host: 127.0.0.1
      port: 11112
      opend_root: {opend_sy}
markets:
  us:
    accounts: [lx, sy]
    symbols: [NVDA]
    overrides:
      NVDA:
        sell_put:
          max_strike: 150
""",
        encoding="utf-8",
    )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_yaml=config_yaml,
        include_opend=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    assert "systemd/options-monitor-opend-lx.service" in files
    assert "systemd/options-monitor-opend-sy.service" in files
    assert "systemd/options-monitor-opend.service" not in files
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    assert "After=network-online.target options-monitor-opend-lx.service options-monitor-opend-sy.service" in intake
    profile = json.loads(files["service.profile.json"]["content"])
    assert profile["opend"]["services"] == [
        {
            "account": "lx",
            "root": str(opend_lx),
            "executable": str(opend_lx / "FutuOpenD"),
            "service_name": "options-monitor-opend-lx.service",
        },
        {
            "account": "sy",
            "root": str(opend_sy),
            "executable": str(opend_sy / "FutuOpenD"),
            "service_name": "options-monitor-opend-sy.service",
        },
    ]


def test_render_systemd_bundle_rejects_invalid_opend_config_json(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    config_path = tmp_path / "config.us.json"
    repo.mkdir()
    runtime.mkdir()
    config_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError, match="failed to parse service config JSON"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            runtime_root=runtime,
            accounts=["lx", "sy"],
            markets=["us"],
            config_paths={"us": config_path},
            include_opend=True,
        )


def _write_systemd_units_from_bundle(bundle: dict, systemd_root: Path, *, skip: set[str] | None = None) -> None:
    skip = skip or set()
    systemd_root.mkdir(parents=True, exist_ok=True)
    for item in bundle["files"]:
        if item.get("kind") not in {"systemd_service", "systemd_timer"}:
            continue
        name = Path(item["install_path"]).name
        if name in skip:
            continue
        (systemd_root / name).write_text(item["content"], encoding="utf-8")


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


def test_service_drift_sudo_fallback_applies_managed_file_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.service_drift import _write_text_with_sudo_fallback

    target = tmp_path / "protected" / "credential-helper"
    original_write_text = Path.write_text

    def _write_text(path: Path, content: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == target:
            raise PermissionError("permission denied")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _write_text)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[:4] == ["sudo", "-n", "sh", "-c"]:
            original_write_text(
                Path(command[-1]),
                str(kwargs.get("input") or ""),
                encoding="utf-8",
            )
        elif command[:3] == ["sudo", "-n", "chmod"]:
            Path(command[-1]).chmod(int(command[-2], 8))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _write_text_with_sudo_fallback(
        target,
        "#!/bin/sh\n",
        ctx={"provider": "systemd"},
        run_cmd=_run_cmd,
        mode=0o755,
    )

    assert out["ok"] is True
    assert out["sudo_fallback"] is True
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert ["sudo", "-n", "chmod", "0755", str(target)] in calls


def test_service_drift_privileged_executable_uses_root_owned_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_drift import _write_text_with_sudo_fallback

    target = tmp_path / "protected" / "credential-helper"
    expected_uid = os.getuid()
    expected_gid = os.getgid()
    monkeypatch.setattr(
        service_drift_module.os,
        "geteuid",
        lambda: expected_uid + 1,
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[:4] == ["sudo", "-n", "install", "-d"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        elif command[:3] == ["sudo", "-n", "install"] and "/dev/stdin" in command:
            target.write_text(str(kwargs.get("input") or ""), encoding="utf-8")
            target.chmod(int(command[command.index("-m") + 1], 8))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _write_text_with_sudo_fallback(
        target,
        "#!/usr/bin/python3\n",
        ctx={"provider": "systemd"},
        run_cmd=_run_cmd,
        mode=0o755,
        owner_uid=expected_uid,
        owner_gid=expected_gid,
    )

    assert out["ok"] is True
    assert out["sudo_fallback"] is True
    assert target.read_text(encoding="utf-8") == "#!/usr/bin/python3\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert [
        "sudo",
        "-n",
        "install",
        "-o",
        str(expected_uid),
        "-g",
        str(expected_gid),
        "-m",
        "0755",
        "/dev/stdin",
        str(target),
    ] in calls


def test_service_drift_privileged_executable_rejects_symlink_target(
    tmp_path: Path,
) -> None:
    from src.application.service_drift import _write_text_with_sudo_fallback

    outside = tmp_path / "outside"
    outside.write_text("must remain\n", encoding="utf-8")
    target = tmp_path / "credential-helper"
    target.symlink_to(outside)
    calls: list[list[str]] = []

    out = _write_text_with_sudo_fallback(
        target,
        "#!/usr/bin/python3\n",
        ctx={"provider": "systemd"},
        run_cmd=lambda command, **_kwargs: calls.append(list(command)),
        mode=0o755,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert out["ok"] is False
    assert "symbolic link" in out["error"]
    assert calls == []
    assert outside.read_text(encoding="utf-8") == "must remain\n"


def test_service_drift_privileged_executable_fails_if_install_did_not_publish(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_drift import _write_text_with_sudo_fallback

    expected_uid = os.getuid()
    target = tmp_path / "missing" / "credential-helper"
    monkeypatch.setattr(
        service_drift_module.os,
        "geteuid",
        lambda: expected_uid + 1,
    )

    out = _write_text_with_sudo_fallback(
        target,
        "#!/usr/bin/python3\n",
        ctx={"provider": "systemd"},
        run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="ok\n",
            stderr="",
        ),
        mode=0o755,
        owner_uid=expected_uid,
        owner_gid=os.getgid(),
    )

    assert out["ok"] is False
    assert out["returncode"] == 1
    assert "ownership verification failed" in out["error"]


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


def test_render_systemd_bundle_aligns_hk_tick_timer_to_calendar_boundaries(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-hk.service"]["content"]
    tick_timer = files["systemd/options-monitor-tick-hk.timer"]["content"]

    assert str(repo / "om") + " run tick-cron --market hk" in tick
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer


def test_render_systemd_bundle_can_include_auto_upgrade_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    default_bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, markets=["us"])
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-upgrade.service" not in default_files

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-upgrade.service"]["content"]
    timer = files["systemd/options-monitor-upgrade.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " update apply" in service
    assert "--repo-root " + str(repo) in service
    assert "--auto --confirm --preserve-activation-state" in service
    assert "OnCalendar=*-*-* 06:10:00 Asia/Shanghai" in timer
    assert profile["auto_upgrade"]["enabled"] is True
    assert profile["config_paths"]["us"] == str(runtime / "config.us.json")


def test_render_systemd_bundle_can_include_quality_monitoring(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    default_bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us", "hk"],
    )
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-quality-http.service" not in default_files

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us", "hk"],
        include_opend=True,
        include_quality_monitoring=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    quality_http = files["systemd/options-monitor-quality-http.service"]["content"]
    refresh = files["systemd/options-monitor-quality-refresh.service"]["content"]
    refresh_timer = files["systemd/options-monitor-quality-refresh.timer"]["content"]
    recheck = files["systemd/options-monitor-quality-recheck.service"]["content"]
    recheck_timer = files["systemd/options-monitor-quality-recheck.timer"]["content"]
    day_end_us = files["systemd/options-monitor-quality-day-end-us.service"]["content"]
    day_end_us_timer = files["systemd/options-monitor-quality-day-end-us.timer"]["content"]
    day_end_hk_timer = files["systemd/options-monitor-quality-day-end-hk.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " quality serve --host 127.0.0.1 --port 8792" in quality_http
    assert "Type=simple" in quality_http
    assert "Restart=always" in quality_http
    assert str(repo / "om") + " quality refresh --config-key us --config-key hk --no-deep" in refresh
    assert "TimeoutStartSec=300" in refresh
    assert "OnUnitActiveSec=15min" in refresh_timer
    assert str(repo / "om") + " quality recheck-due --config-key us --config-key hk" in recheck
    assert "After=network-online.target options-monitor-opend.service" in recheck
    assert "TimeoutStartSec=300" in recheck
    assert "OnUnitActiveSec=1min" in recheck_timer
    assert str(repo / "om") + " quality refresh --config-key us --day-end-strict" in day_end_us
    assert "TimeoutStartSec=300" in day_end_us
    assert "OnCalendar=Mon..Fri *-*-* 16:30:00 America/New_York" in day_end_us_timer
    assert "OnCalendar=Mon..Fri *-*-* 16:30:00 Asia/Hong_Kong" in day_end_hk_timer
    assert "systemctl enable --now options-monitor-quality-http.service" in bundle["commands"]["enable"]
    assert profile["quality_monitoring"] == {
        "enabled": True,
        "artifact_path": str(runtime / "output_shared" / "state" / "quality" / "status.v1.json"),
        "http": {
            "host": "127.0.0.1",
            "port": 8792,
            "credential_name": "quality.read_token",
        },
        "regular_refresh_interval": "15min",
        "recheck_interval": "1min",
        "day_end_calendars": {
            "us": "Mon..Fri *-*-* 16:30:00 America/New_York",
            "hk": "Mon..Fri *-*-* 16:30:00 Asia/Hong_Kong",
        },
    }


def test_render_launchd_bundle_rejects_quality_monitoring(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    repo.mkdir()

    with pytest.raises(ValueError, match="supported only for systemd"):
        render_service_bundle(
            target="launchd",
            repo_root=repo,
            include_quality_monitoring=True,
        )


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


def test_render_systemd_bundle_can_include_strategy_lab_recorder_timers(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    opend_root = tmp_path / "opend-lx"
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        {"lx": _futu_service_account(opend_root=opend_root)},
    )

    default_bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, markets=["us"])
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-strategy-lab-build.service" not in default_files

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_max_datasets=3,
        strategy_lab_recorder_mark_stale_hours=2,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    build_service = files["systemd/options-monitor-strategy-lab-build.service"]["content"]
    build_timer = files["systemd/options-monitor-strategy-lab-build.timer"]["content"]
    sample_service = files["systemd/options-monitor-strategy-lab-sample.service"]["content"]
    sample_timer = files["systemd/options-monitor-strategy-lab-sample.timer"]["content"]
    assert "TimeoutStartSec=600" in sample_service
    settle_service = files["systemd/options-monitor-strategy-lab-settle.service"]["content"]
    settle_timer = files["systemd/options-monitor-strategy-lab-settle.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " research strategy-lab update --latest" in build_service
    assert "--profile-path " + str(runtime / "service.profile.json") in build_service
    assert "--include-close-decisions" in build_service
    assert "--build-dataset --include-close-decisions --write --source local" in build_service
    assert "--max-datasets 0" in build_service
    assert "--settle-after-collect" not in build_service
    assert "OnUnitActiveSec=6h" in build_timer
    assert str(repo / "om") + " research strategy-lab update --profile-path" in sample_service
    assert "--source opend --write --action collect_marks --max-datasets 3" in sample_service
    assert "--opend-host 127.0.0.1 --opend-port 11111" in sample_service
    assert "--settle-after-collect" not in sample_service
    assert "After=network-online.target options-monitor-opend.service" in sample_service
    assert "OnUnitActiveSec=2h" in sample_timer
    assert "--write --action settle --min-sample 30" in settle_service
    assert "--max-datasets" not in settle_service
    assert "OnCalendar=*-*-* 07:20:00 Asia/Shanghai" in settle_timer
    assert {"name": "options-monitor-strategy-lab-build.timer"} in profile["services"]
    assert {"name": "options-monitor-strategy-lab-sample.timer"} in profile["services"]
    assert {"name": "options-monitor-strategy-lab-settle.timer"} in profile["services"]
    assert profile["strategy_lab_recorder"] == {
        "enabled": True,
        "include_close_decisions": True,
        "source": "opend",
        "max_datasets": 3,
        "mark_stale_hours": 2,
        "build_interval": "6h",
        "sample_interval": "2h",
        "settle_schedule_beijing": "07:20",
        "binding": {
            "account": "lx",
            "host": "127.0.0.1",
            "port": 11111,
            "service_name": "options-monitor-opend.service",
        },
    }


def test_render_systemd_bundle_can_include_strategy_lab_top1_timer(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    env_file = runtime / "options-monitor.env"
    repo.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.hk.json",
        {"lx": _futu_service_account(opend_root=tmp_path / "opend-lx")},
    )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
        config_paths={"hk": config_path},
        env_file=env_file,
        include_opend=True,
        include_strategy_lab_top1=True,
        strategy_lab_top1_advance_interval_seconds=300,
        strategy_lab_top1_timeout_start_sec=120,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files[
        "systemd/options-monitor-strategy-lab-top1-advance.service"
    ]["content"]
    timer = files["systemd/options-monitor-strategy-lab-top1-advance.timer"][
        "content"
    ]
    profile = json.loads(files["service.profile.json"]["content"])
    assert (
        str(repo / "om")
        + " research strategy-lab top1-loop advance --scheduled --market hk "
        "--account lx --profile-path "
        + str(runtime / "service.profile.json")
        + " --write"
    ) in service
    assert f"EnvironmentFile={env_file}" in service
    assert "After=network-online.target options-monitor-opend.service" in service
    assert "Wants=network-online.target options-monitor-opend.service" in service
    assert "TimeoutStartSec=120" in service
    assert "OnUnitActiveSec=300s" in timer
    assert profile["strategy_lab_top1"] == {
        "enabled": True,
        "market": "hk",
        "account": "lx",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
        "advance_interval": 300,
        "timeout_start_sec": 120,
    }


def test_render_strategy_lab_top1_rejects_missing_explicit_contract(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="supported only for systemd"):
        render_service_bundle(
            target="launchd",
            repo_root=repo,
            accounts=["lx"],
            markets=["hk"],
            include_strategy_lab_top1=True,
            strategy_lab_top1_advance_interval_seconds=300,
            strategy_lab_top1_timeout_start_sec=120,
        )
    for env_file in (None, "", " \t "):
        with pytest.raises(ValueError, match="non-empty service env file"):
            render_service_bundle(
                target="systemd",
                repo_root=repo,
                accounts=["lx"],
                markets=["hk"],
                env_file=env_file,
                include_strategy_lab_top1=True,
                strategy_lab_top1_advance_interval_seconds=300,
                strategy_lab_top1_timeout_start_sec=120,
            )
    with pytest.raises(ValueError, match="explicit positive integers"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["hk"],
            env_file=tmp_path / "env",
            include_strategy_lab_top1=True,
            strategy_lab_top1_advance_interval_seconds=0,
            strategy_lab_top1_timeout_start_sec=120,
        )
    with pytest.raises(ValueError, match="requires selected market hk and account lx"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["sy"],
            markets=["hk"],
            env_file=tmp_path / "env",
            include_strategy_lab_top1=True,
            strategy_lab_top1_advance_interval_seconds=300,
            strategy_lab_top1_timeout_start_sec=120,
        )
    invalid_config = _write_service_account_config(
        tmp_path / "invalid-config.hk.json",
        {"lx": _futu_service_account(port=0)},
    )
    with pytest.raises(ValueError, match="Strategy Lab Top1 OpenD port is invalid"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["hk"],
            config_paths={"hk": invalid_config},
            env_file=tmp_path / "env",
            include_strategy_lab_top1=True,
            strategy_lab_top1_advance_interval_seconds=300,
            strategy_lab_top1_timeout_start_sec=120,
        )


def test_service_drift_round_trips_tampers_and_retires_strategy_lab_top1(
    tmp_path: Path,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.hk.json", {"lx": _futu_service_account()}
    )
    render_args = {
        "target": "systemd",
        "repo_root": repo,
        "runtime_root": runtime,
        "accounts": ["lx"],
        "markets": ["hk"],
        "config_paths": {"hk": config_path},
        "env_file": tmp_path / "env",
    }
    bundle = render_service_bundle(
        **render_args,
        include_strategy_lab_top1=True,
        strategy_lab_top1_advance_interval_seconds=300,
        strategy_lab_top1_timeout_start_sec=120,
    )
    profile = json.loads(
        {item["relative_path"]: item for item in bundle["files"]}[
            "service.profile.json"
        ]["content"]
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)

    clean = service_drift(
        repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root
    )
    assert clean["summary"]["status"] == "ok"
    assert clean["profile_content_changed"] is False

    for invalid_timing in (None, True, "abc"):
        invalid_profile = json.loads(json.dumps(profile))
        if invalid_timing is None:
            invalid_profile["strategy_lab_top1"].pop("advance_interval")
        else:
            invalid_profile["strategy_lab_top1"]["advance_interval"] = invalid_timing
        (runtime / "service.profile.json").write_text(
            json.dumps(invalid_profile, ensure_ascii=False), encoding="utf-8"
        )
        invalid = service_drift(
            repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root
        )
        assert invalid["summary"]["status"] == "error"
        assert invalid["reason"] == "strategy_lab_top1_profile_invalid"

    (runtime / "service.profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )

    service_path = systemd_root / "options-monitor-strategy-lab-top1-advance.service"
    service_path.write_text(service_path.read_text(encoding="utf-8") + "# tampered\n")
    tampered = service_drift(
        repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root
    )
    assert "options-monitor-strategy-lab-top1-advance.service" in tampered[
        "mismatched_units"
    ]

    default_bundle = render_service_bundle(**render_args)
    default_profile = json.loads(
        {item["relative_path"]: item for item in default_bundle["files"]}[
            "service.profile.json"
        ]["content"]
    )
    assert "strategy_lab_top1" not in default_profile
    assert not any(
        "strategy-lab-top1" in item["relative_path"]
        for item in default_bundle["files"]
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(default_profile, ensure_ascii=False), encoding="utf-8"
    )
    retired = service_drift(
        repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root
    )
    assert "options-monitor-strategy-lab-top1-advance.service" in retired[
        "extra_installed_units"
    ]
    assert "options-monitor-strategy-lab-top1-advance.timer" in retired[
        "extra_installed_units"
    ]


def test_render_launchd_strategy_lab_recorder_separates_actions(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        {"lx": _futu_service_account(opend_root=tmp_path / "opend-lx")},
    )

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_max_datasets=3,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}

    def args(name: str) -> list[str]:
        content = files[f"launchd/com.options-monitor.strategy-lab-{name}.plist"]["content"]
        return plistlib.loads(content.encode("utf-8"))["ProgramArguments"]

    build_args = args("build")
    sample_args = args("sample")
    settle_args = args("settle")
    build_plist = files["launchd/com.options-monitor.strategy-lab-build.plist"]["content"]
    build_payload = plistlib.loads(build_plist.encode("utf-8"))
    sample_payload = plistlib.loads(
        files["launchd/com.options-monitor.strategy-lab-sample.plist"]["content"].encode("utf-8")
    )
    profile = json.loads(files["service.profile.json"]["content"])

    assert build_args[build_args.index("--max-datasets") + 1] == "0"
    assert "--include-close-decisions" in build_args
    assert "--settle-after-collect" not in build_args
    assert build_payload["StartInterval"] == 21600
    assert sample_args[sample_args.index("--action") + 1] == "collect_marks"
    assert sample_args[sample_args.index("--max-datasets") + 1] == "3"
    assert sample_args[sample_args.index("--opend-host") + 1] == "127.0.0.1"
    assert sample_args[sample_args.index("--opend-port") + 1] == "11111"
    assert "--settle-after-collect" not in sample_args
    assert "After" not in sample_payload
    assert "Wants" not in sample_payload
    assert profile["strategy_lab_recorder"]["binding"]["service_name"] == "com.options-monitor.opend"
    assert settle_args[settle_args.index("--action") + 1] == "settle"
    assert "--max-datasets" not in settle_args


def test_render_strategy_lab_recorder_requires_account_for_multiple_futu_accounts(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )

    with pytest.raises(ValueError, match="required when multiple Futu accounts"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx", "sy"],
            markets=["us"],
            config_paths={"us": config_path},
            include_opend=True,
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
        )


@pytest.mark.parametrize(
    ("recorder_account", "expected_account", "expected_port", "expected_service", "other_service"),
    [
        ("LX", "lx", 11111, "options-monitor-opend-lx.service", "options-monitor-opend-sy.service"),
        ("sy", "sy", 11112, "options-monitor-opend-sy.service", "options-monitor-opend-lx.service"),
    ],
)
def test_render_strategy_lab_recorder_binds_only_selected_systemd_opend(
    tmp_path: Path,
    recorder_account: str,
    expected_account: str,
    expected_port: int,
    expected_service: str,
    other_service: str,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account=recorder_account,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    sample = files["systemd/options-monitor-strategy-lab-sample.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert f"--opend-host 127.0.0.1 --opend-port {expected_port}" in sample
    assert f"After=network-online.target {expected_service}" in sample
    assert f"Wants=network-online.target {expected_service}" in sample
    assert other_service not in sample
    assert profile["schema_version"] == 1
    assert profile["strategy_lab_recorder"]["binding"] == {
        "account": expected_account,
        "host": "127.0.0.1",
        "port": expected_port,
        "service_name": expected_service,
    }


@pytest.mark.parametrize(
    ("settings", "accounts", "recorder_account", "error_pattern"),
    [
        ({"lx": _futu_service_account()}, ["lx"], "sy", "must be included in accounts"),
        ({"lx": _futu_service_account()}, ["lx", "sy"], "sy", "not configured for markets us"),
        ({"lx": {"type": "external_holdings"}}, ["lx"], "lx", "selected Futu account"),
        ({"lx": _futu_service_account(host="")}, ["lx"], "lx", "host is missing"),
        ({"lx": _futu_service_account(port=None)}, ["lx"], "lx", "port is invalid"),
        ({"lx": _futu_service_account(port=11111.9)}, ["lx"], "lx", "port is invalid"),
        ({"lx": _futu_service_account(port=True)}, ["lx"], "lx", "port is invalid"),
        ({"lx": _futu_service_account(port="11111.0")}, ["lx"], "lx", "port is invalid"),
        ({"lx": _futu_service_account(port=70000)}, ["lx"], "lx", "port is invalid"),
    ],
)
def test_render_strategy_lab_recorder_rejects_invalid_account_or_endpoint(
    tmp_path: Path,
    settings: dict[str, dict[str, object]],
    accounts: list[str],
    recorder_account: str,
    error_pattern: str,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_service_account_config(tmp_path / "config.us.json", settings)

    with pytest.raises(ValueError, match=error_pattern):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=accounts,
            markets=["us"],
            config_paths={"us": config_path},
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
            strategy_lab_recorder_account=recorder_account,
        )


def test_render_strategy_lab_recorder_rejects_non_integer_yaml_port(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
    futu:
      account_id: "REAL_12345678"
      host: 127.0.0.1
      port: 11111.9
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as _caught:
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["us"],
            config_yaml=config_yaml,
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
            strategy_lab_recorder_account="lx",
        )
    exc = _caught.value
    assert "account_settings.lx.futu.port must be an integer" in str(exc)


@pytest.mark.parametrize(
    ("hk_settings", "error_pattern"),
    [
        ({"lx": _futu_service_account(port=11112)}, "endpoint differs across markets"),
        ({"lx": {"type": "external_holdings"}}, "account type differs across markets"),
    ],
)
def test_render_strategy_lab_recorder_rejects_cross_market_binding_mismatch(
    tmp_path: Path,
    hk_settings: dict[str, dict[str, object]],
    error_pattern: str,
) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    us_config = _write_service_account_config(
        tmp_path / "config.us.json",
        {"lx": _futu_service_account(port=11111)},
    )
    hk_config = _write_service_account_config(
        tmp_path / "config.hk.json",
        hk_settings,
    )

    with pytest.raises(ValueError, match=error_pattern):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["us", "hk"],
            config_paths={"us": us_config, "hk": hk_config},
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
            strategy_lab_recorder_account="lx",
        )


def test_render_strategy_lab_recorder_rejects_cross_market_opend_root_mismatch(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    us_config = _write_service_account_config(
        tmp_path / "config.us.json",
        {"lx": _futu_service_account(opend_root=tmp_path / "opend-us")},
    )
    hk_config = _write_service_account_config(
        tmp_path / "config.hk.json",
        {"lx": _futu_service_account(opend_root=tmp_path / "opend-hk")},
    )

    with pytest.raises(ValueError, match="root differs across markets"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["us", "hk"],
            config_paths={"us": us_config, "hk": hk_config},
            include_opend=True,
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
        )


def test_render_strategy_lab_recorder_external_opend_has_endpoint_without_dependency(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=False,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    sample = files["systemd/options-monitor-strategy-lab-sample.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "--opend-host 127.0.0.1 --opend-port 11111" in sample
    assert "After=network-online.target\n" in sample
    assert "options-monitor-opend" not in sample
    assert profile["strategy_lab_recorder"]["binding"] == {
        "account": "lx",
        "host": "127.0.0.1",
        "port": 11111,
    }


def test_render_strategy_lab_recorder_allows_only_unambiguous_legacy_opend_plan(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    sole_config = _write_service_account_config(
        tmp_path / "config.sole.json",
        {"lx": _futu_service_account()},
    )
    sole = render_service_bundle(
        target="systemd",
        repo_root=repo,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": sole_config},
        include_opend=True,
        opend_root=tmp_path / "legacy-opend",
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
    )
    sole_profile = json.loads({item["relative_path"]: item for item in sole["files"]}["service.profile.json"]["content"])
    assert sole_profile["strategy_lab_recorder"]["binding"]["service_name"] == "options-monitor-opend.service"

    multi_config = _write_service_account_config(
        tmp_path / "config.multi.json",
        _two_futu_service_accounts(tmp_path),
    )
    with pytest.raises(ValueError, match="service is not uniquely mapped"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx", "sy"],
            markets=["us"],
            config_paths={"us": multi_config},
            include_opend=True,
            opend_root=tmp_path / "legacy-opend",
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="opend",
            strategy_lab_recorder_account="lx",
        )


def test_render_local_strategy_lab_recorder_rejects_account_and_has_no_binding(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="requires include_strategy_lab_recorder"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["us"],
            strategy_lab_recorder_account="lx",
        )
    with pytest.raises(ValueError, match="not valid.*source=local"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            accounts=["lx"],
            markets=["us"],
            include_strategy_lab_recorder=True,
            strategy_lab_recorder_source="local",
            strategy_lab_recorder_account="lx",
        )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        accounts=["lx"],
        markets=["us"],
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="local",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    sample = files["systemd/options-monitor-strategy-lab-sample.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])
    assert "--opend-host" not in sample
    assert "--opend-port" not in sample
    assert "binding" not in profile["strategy_lab_recorder"]


def test_strategy_lab_recorder_profile_round_trips_and_reresolves_endpoint(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    clean = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)
    assert clean["summary"]["status"] == "ok"
    assert clean["profile_content_changed"] is False
    assert clean["compatibility_warnings"] == []

    _write_service_account_config(
        config_path,
        _two_futu_service_accounts(tmp_path, lx_port=11113),
    )
    changed = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)
    assert changed["summary"]["status"] == "warn"
    assert changed["profile_content_changed"] is True
    assert "options-monitor-strategy-lab-sample.service" in changed["mismatched_units"]
    assert changed["compatibility_warnings"] == []


def test_legacy_strategy_lab_recorder_binding_warns_then_confirm_migrates(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["strategy_lab_recorder"].pop("binding")
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    sample_path = systemd_root / "options-monitor-strategy-lab-sample.service"
    sample_path.write_text(
        sample_path.read_text(encoding="utf-8").replace(" --opend-host 127.0.0.1 --opend-port 11111", ""),
        encoding="utf-8",
    )

    warning = {
        "code": "legacy_strategy_lab_recorder_binding_inferred",
        "account": "lx",
        "host": "127.0.0.1",
        "port": 11111,
    }
    before = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)
    assert before["compatibility_warnings"] == [warning]
    assert before["summary"]["status"] == "warn"
    assert before["summary"]["warning_count"] >= 1
    assert before["profile_content_changed"] is True
    assert "options-monitor-strategy-lab-sample.service" in before["mismatched_units"]

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[1] == "is-enabled":
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if command[1] == "is-active":
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    migrated = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert migrated["before"]["compatibility_warnings"] == [warning]
    assert migrated["compatibility_warnings"] == []
    assert migrated["summary"]["status"] == "ok"
    assert refreshed["strategy_lab_recorder"]["binding"] == {
        "account": "lx",
        "host": "127.0.0.1",
        "port": 11111,
        "service_name": "options-monitor-opend-lx.service",
    }
    subsequent = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)
    assert subsequent["compatibility_warnings"] == []
    assert subsequent["summary"]["status"] == "ok"


@pytest.mark.parametrize(("lx_port", "sy_port"), [(11113, 11112), (11111, 11111)])
def test_legacy_strategy_lab_recorder_binding_ambiguity_blocks_confirmed_writes(
    tmp_path: Path,
    lx_port: int,
    sy_port: int,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path, lx_port=lx_port, sy_port=sy_port),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["strategy_lab_recorder"].pop("binding")
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    original_profile = profile_path.read_text(encoding="utf-8")
    original_sample = (systemd_root / "options-monitor-strategy-lab-sample.service").read_text(encoding="utf-8")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
    )

    assert out["supported"] is False
    assert out["reason"] == "strategy_lab_recorder_binding_invalid"
    assert out["summary"]["status"] == "error"
    assert out["changed"] is False
    assert out["operations"] == []
    assert profile_path.read_text(encoding="utf-8") == original_profile
    assert (systemd_root / "options-monitor-strategy-lab-sample.service").read_text(encoding="utf-8") == original_sample


@pytest.mark.parametrize("root_case", ["missing", "cross_market_mismatch"])
def test_legacy_strategy_lab_recorder_counts_endpoint_matches_before_opend_root_validation(
    tmp_path: Path,
    root_case: str,
) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    us_settings = _two_futu_service_accounts(tmp_path, lx_port=11111, sy_port=11111)
    markets = ["us"]
    config_paths = {
        "us": _write_service_account_config(tmp_path / "config.us.json", us_settings),
    }
    if root_case == "missing":
        sy_futu = us_settings["sy"]["futu"]
        assert isinstance(sy_futu, dict)
        sy_futu.pop("opend_root")
        _write_service_account_config(config_paths["us"], us_settings)
    else:
        markets.append("hk")
        hk_settings = _two_futu_service_accounts(tmp_path, lx_port=11111, sy_port=11111)
        sy_hk_futu = hk_settings["sy"]["futu"]
        assert isinstance(sy_hk_futu, dict)
        sy_hk_futu["opend_root"] = str(tmp_path / "opend-sy-hk")
        config_paths["hk"] = _write_service_account_config(tmp_path / "config.hk.json", hk_settings)

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=markets,
        config_paths=config_paths,
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["strategy_lab_recorder"].pop("binding")
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    original_profile = profile_path.read_bytes()
    original_units = {
        path.name: path.read_bytes()
        for path in systemd_root.iterdir()
        if path.is_file()
    }

    dry_run = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
    )
    confirmed = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
    )

    for out in (dry_run, confirmed):
        assert out["supported"] is False
        assert out["reason"] == "strategy_lab_recorder_binding_invalid"
        assert "matched lx,sy" in out["error"]
        assert out["summary"]["status"] == "error"
        assert out["changed"] is False
        assert out["operations"] == []
    assert profile_path.read_bytes() == original_profile
    assert {
        path.name: path.read_bytes()
        for path in systemd_root.iterdir()
        if path.is_file()
    } == original_units


def test_legacy_strategy_lab_recorder_invalid_runtime_port_blocks_confirmed_writes(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    config_path = _write_service_account_config(
        tmp_path / "config.us.json",
        _two_futu_service_accounts(tmp_path),
    )
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us"],
        config_paths={"us": config_path},
        include_opend=True,
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="opend",
        strategy_lab_recorder_account="lx",
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["strategy_lab_recorder"].pop("binding")
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)
    _write_service_account_config(
        config_path,
        _two_futu_service_accounts(tmp_path, lx_port=11111.9),
    )
    original_profile = profile_path.read_bytes()
    original_units = {
        path.name: path.read_bytes()
        for path in systemd_root.iterdir()
        if path.is_file()
    }

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
    )

    assert out["supported"] is False
    assert out["reason"] == "strategy_lab_recorder_binding_invalid"
    assert "OpenD port is invalid: lx" in out["error"]
    assert out["changed"] is False
    assert out["operations"] == []
    assert profile_path.read_bytes() == original_profile
    assert {
        path.name: path.read_bytes()
        for path in systemd_root.iterdir()
        if path.is_file()
    } == original_units


def test_service_render_cli_exposes_strategy_lab_recorder_account(capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import parse_args

    args = parse_args([
        "service",
        "render",
        "--target",
        "systemd",
        "--config-yaml",
        "/tmp/config.yaml",
        "--include-strategy-lab-recorder",
        "--strategy-lab-recorder-account",
        "sy",
    ])
    assert args.strategy_lab_recorder_account == "sy"

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["service", "render", "--help"])
    assert exc_info.value.code == 0
    assert "--strategy-lab-recorder-account" in capsys.readouterr().out

    top1 = parse_args(
        [
            "service",
            "render",
            "--target",
            "systemd",
            "--config-yaml",
            "/tmp/config.yaml",
            "--include-strategy-lab-top1",
            "--strategy-lab-top1-advance-interval-seconds",
            "300",
            "--strategy-lab-top1-timeout-start-sec",
            "120",
        ]
    )
    assert top1.include_strategy_lab_top1 is True
    assert top1.strategy_lab_top1_advance_interval_seconds == 300
    assert top1.strategy_lab_top1_timeout_start_sec == 120


def test_service_drift_preserves_strategy_lab_recorder_opt_in(tmp_path: Path) -> None:
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
        include_strategy_lab_recorder=True,
        strategy_lab_recorder_source="local",
        strategy_lab_recorder_max_datasets=2,
        strategy_lab_recorder_mark_stale_hours=4,
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert out["summary"]["status"] == "ok"
    assert "options-monitor-strategy-lab-build.timer" in out["expected_services"]
    assert "options-monitor-strategy-lab-sample.timer" in out["expected_services"]
    assert "options-monitor-strategy-lab-settle.timer" in out["expected_services"]
    assert out["mismatched_units"] == []


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


def test_render_systemd_bundle_records_yaml_authoring_source(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    config_yaml = runtime / "config.yaml"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us", "hk"],
        config_yaml=config_yaml,
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])

    assert profile["config_authoring"] == {
        "source": "yaml",
        "config_yaml": str(config_yaml),
        "markets": ["us", "hk"],
    }
    assert profile["config_paths"]["us"] == str(runtime / "config.us.json")
    assert profile["config_paths"]["hk"] == str(runtime / "config.hk.json")


def test_render_systemd_bundle_can_include_feishu_ws_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        config_yaml=config_yaml,
        include_feishu_ws=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-feishu-ws.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " inbound feishu-ws" in service
    assert "--config-path " + str(repo / "config.us.json") in service
    assert "--config-key" not in service
    assert "--assistant-config " + str(runtime / "resolved" / "config.assistant.json") in service
    assert "--audit-db " + str(runtime / "output_shared" / "state" / "inbound_control.sqlite3") in service
    assert "--lock-path " + str(runtime / "locks" / "feishu-ws.lock") in service
    assert "Restart=always" in service
    assert "[Install]\nWantedBy=multi-user.target" in service
    assert {"name": "options-monitor-feishu-ws.service"} in profile["services"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert profile["feishu_ws"]["enabled"] is True
    assert profile["assistant_config_path"] == str(runtime / "resolved" / "config.assistant.json")
    assert profile["feishu_ws"]["assistant_config_path"] == str(runtime / "resolved" / "config.assistant.json")
    assert profile["feishu_ws"]["lock_path"] == str(runtime / "locks" / "feishu-ws.lock")
    assert "systemctl enable --now options-monitor-feishu-ws.service" in bundle["commands"]["enable"]


def test_render_systemd_bundle_can_include_wechat_clawbot_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        config_yaml=config_yaml,
        include_wechat_clawbot=True,
        wechat_clawbot_label="ops",
        wechat_clawbot_allowed_senders="wechat:user_1",
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-wechat-clawbot.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " channel wechat-clawbot serve" in service
    assert "--label ops" in service
    assert "--state-dir " + str(runtime / "output_shared" / "state" / "channels" / "wechat_clawbot" / "ops") in service
    assert "--config-path " + str(repo / "config.us.json") in service
    assert "--config-key" not in service
    assert "--assistant-config " + str(runtime / "resolved" / "config.assistant.json") in service
    assert "--audit-db " + str(runtime / "output_shared" / "state" / "inbound_control.sqlite3") in service
    assert "--allowed-senders wechat:user_1" in service
    assert "--lock-path " + str(runtime / "locks" / "wechat-clawbot.lock") in service
    assert "Restart=always" in service
    assert "[Install]\nWantedBy=multi-user.target" in service
    assert {"name": "options-monitor-wechat-clawbot.service"} in profile["services"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-wechat-clawbot.service",
    ]
    assert profile["wechat_clawbot"]["enabled"] is True
    assert profile["wechat_clawbot"]["label"] == "ops"
    assert profile["wechat_clawbot"]["config_key"] == "us"
    assert profile["wechat_clawbot"]["allowed_senders"] == "wechat:user_1"
    assert profile["wechat_clawbot"]["allowed_senders_configured"] is True
    assert profile["wechat_clawbot"]["allowed_senders_source"] == "render_argument"
    assert profile["wechat_clawbot"]["assistant_config_path"] == str(runtime / "resolved" / "config.assistant.json")
    assert profile["wechat_clawbot"]["lock_path"] == str(runtime / "locks" / "wechat-clawbot.lock")
    assert "systemctl enable --now options-monitor-wechat-clawbot.service" in bundle["commands"]["enable"]


def test_render_wechat_clawbot_requires_explicit_allowed_senders(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="wechat_clawbot_allowed_senders"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            runtime_root=tmp_path / "runtime",
            markets=["us"],
            include_wechat_clawbot=True,
        )


def test_render_wechat_clawbot_can_use_yaml_inbound_allowlist(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text(
        """\
accounts: {}
markets: {}
inbound:
  wechat_clawbot:
    label: ops
    allowed_senders: wechat:user_1
""",
        encoding="utf-8",
    )

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        config_yaml=config_yaml,
        include_wechat_clawbot=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-wechat-clawbot.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "--label ops" in service
    assert "--assistant-config " + str(runtime / "resolved" / "config.assistant.json") in service
    assert "--allowed-senders" not in service
    assert profile["wechat_clawbot"]["label"] == "ops"
    assert profile["wechat_clawbot"]["allowed_senders_configured"] is True
    assert profile["wechat_clawbot"]["allowed_senders_source"] == "config_yaml"
    assert "allowed_senders" not in profile["wechat_clawbot"]
    assert profile["wechat_clawbot"]["assistant_config_path"] == str(runtime / "resolved" / "config.assistant.json")


def test_render_systemd_auto_upgrade_preserves_symlink_repo_root(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    releases = tmp_path / "releases"
    release_dir = releases / "1.2.71"
    release_dir.mkdir(parents=True)
    current = tmp_path / "options-monitor"
    current.symlink_to(release_dir, target_is_directory=True)
    runtime = tmp_path / "runtime"

    bundle = render_service_bundle(
        target="systemd",
        repo_root=current,
        runtime_root=runtime,
        markets=["hk"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    upgrade = files["systemd/options-monitor-upgrade.service"]["content"]
    tick = files["systemd/options-monitor-tick-hk.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "WorkingDirectory=" + str(current) in upgrade
    assert str(current / "om") + " update apply" in upgrade
    assert "--repo-root " + str(current) in upgrade
    assert str(release_dir) not in upgrade
    assert "--config " + str(runtime / "config.hk.json") in tick
    assert profile["repo_root"] == str(current)
    assert profile["config_paths"]["hk"] == str(runtime / "config.hk.json")


def test_render_systemd_bundle_allows_deploy_identity_override(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        deploy_user="ops",
        deploy_home="/srv/options-home",
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "User=ops" in tick
    assert 'Environment="HOME=/srv/options-home"' in tick
    assert profile["deploy_user"] == "ops"
    assert profile["deploy_home"] == "/srv/options-home"
    assert profile["restart"]["requires_sudo"] is True
    assert profile["restart"]["command_prefix"] == ["sudo", "-n", "systemctl"]
    assert profile["restart"]["services"] == ["options-monitor-trade-intake.service"]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in profile["restart"]["sudoers"]


def test_render_systemd_feishu_ws_sudoers_cover_all_long_running_services(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        deploy_user="ops",
        include_feishu_ws=True,
    )

    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])

    assert profile["restart"]["command_prefix"] == ["sudo", "-n", "systemctl"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in profile["restart"]["sudoers"]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service" in profile["restart"]["sudoers"]


def test_render_systemd_bundle_quotes_paths_with_spaces(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo with space"
    runtime = tmp_path / "runtime with space"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]

    assert f'WorkingDirectory="{repo}"' in tick
    assert f'Environment="OM_RUNTIME_ROOT={runtime}"' in tick
    assert f'ExecStart="{repo / "om"}" run tick-cron' in tick
    assert f'--lock-path "{runtime / "locks" / "tick-us.lock"}"' in tick


def test_render_systemd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "etc" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        env_file=env_file,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert f"EnvironmentFile={env_file}" in tick
    assert f"EnvironmentFile={env_file}" in intake
    assert bundle["env_file"] == str(env_file)
    assert profile["env_file"] == str(env_file)


def test_render_launchd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "Library" / "Application Support" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        env_file=env_file,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["launchd/com.options-monitor.tick-us.plist"]["content"]
    intake = files["launchd/com.options-monitor.trade-intake.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<key>OM_ENV_FILE</key>" in tick
    assert f"<string>{env_file}</string>" in tick
    assert "<key>OM_ENV_FILE</key>" in intake
    assert bundle["env_file"] == str(env_file)
    assert profile["env_file"] == str(env_file)


def test_render_launchd_channel_services_use_one_data_scope(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_feishu_ws=True,
        include_wechat_clawbot=True,
        wechat_clawbot_label="ops",
        wechat_clawbot_allowed_senders="wechat:user_1",
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    channels = (
        files["launchd/com.options-monitor.feishu-ws.plist"]["content"],
        files["launchd/com.options-monitor.wechat-clawbot.plist"]["content"],
    )

    for service in channels:
        assert "<string>--config-path</string>" in service
        assert "<string>--config-key</string>" not in service


def test_render_launchd_bundle_uses_launch_agents_and_logs(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["us", "hk"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["launchd/com.options-monitor.tick-hk.plist"]["content"]
    auto_close_hk = files["launchd/com.options-monitor.auto-close-hk.plist"]["content"]
    auto_close_us = files["launchd/com.options-monitor.auto-close-us.plist"]["content"]
    verify = files["launchd/com.options-monitor.projection-verify.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<key>Label</key>" in tick
    assert "<key>Umask</key>" in tick
    assert "<string>077</string>" in tick
    assert "<string>com.options-monitor.tick-hk</string>" in tick
    assert str(runtime / "logs" / "com.options-monitor.tick-hk.out.log") in tick
    assert "--market" in tick
    assert "hk" in tick
    assert "<string>com.options-monitor.auto-close-hk</string>" in auto_close_hk
    assert "<key>Hour</key>" in auto_close_hk
    assert "<integer>9</integer>" in auto_close_hk
    assert "<key>Minute</key>" in auto_close_hk
    assert "<integer>5</integer>" in auto_close_hk
    assert "<string>com.options-monitor.auto-close-us</string>" in auto_close_us
    assert "<key>Minute</key>" in auto_close_us
    assert "<integer>7</integer>" in auto_close_us
    assert "<string>com.options-monitor.projection-verify</string>" in verify
    assert "<key>Hour</key>" in verify
    assert "<integer>9</integer>" in verify
    assert "<key>Minute</key>" in verify
    assert "<integer>30</integer>" in verify
    assert "verify-projection" in verify
    assert profile["service_provider"] == "launchd"
    assert {"name": "com.options-monitor.tick-hk"} in profile["services"]
    assert {"name": "com.options-monitor.projection-verify"} in profile["services"]
    assert not any(
        "position-advice" in str(item.get("name") or "")
        for item in profile["services"]
    )


def test_render_launchd_bundle_can_include_auto_upgrade_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    upgrade = files["launchd/com.options-monitor.upgrade.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<string>com.options-monitor.upgrade</string>" in upgrade
    assert "update" in upgrade
    assert "apply" in upgrade
    assert "--preserve-activation-state" in upgrade
    assert "<key>Hour</key>" in upgrade
    assert "<integer>6</integer>" in upgrade
    assert "<key>Minute</key>" in upgrade
    assert "<integer>10</integer>" in upgrade
    assert profile["auto_upgrade"]["schedule_beijing"] == "06:10"


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
                "options-monitor-quality-refresh.timer": "unknown",
            },
        }

    monkeypatch.setattr(service_upgrade_module, "service_drift", _service_drift)

    snapshot = service_upgrade_module._capture_preserved_timer_activation_states(
        repo_root=tmp_path / "current",
        runtime_root=tmp_path / "runtime",
        profile={"service_provider": "systemd"},
        run_cmd=lambda *_args, **_kwargs: None,
    )

    assert snapshot == {
        "options-monitor-quality-refresh.timer": {
            "activation_state": "disabled",
        },
        "options-monitor-tick-hk.timer": {
            "activation_state": "enabled",
            "active_state": "inactive",
        },
    }
    assert observed_calls[0]["confirm"] is False


def test_upgrade_activation_snapshot_fails_closed_when_timer_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            "activation_states": {target: "enabled"},
            "active_states": {target: "unknown"},
        },
    )

    with pytest.raises(
        service_upgrade_module.ServiceTransitionError,
        match="could not determine whether managed timers were active",
    ) as exc_info:
        service_upgrade_module._capture_preserved_timer_activation_states(
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


def test_post_upgrade_service_health_reports_feishu_ws_check_failure(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    repo = tmp_path / "repo"
    repo.mkdir()
    profile = {
        "service_provider": "systemd",
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:3] == [str(repo / "om"), "inbound", "feishu-ws"]:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="missing Feishu app credentials\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=operations)

    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["failed_checks"][0]["check"] == "feishu-ws-check"
    assert "manual_check: source the env file, then run ./om inbound feishu-ws --check" in out["remediation"]


def test_post_upgrade_service_health_reports_precise_wechat_check_failure(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "wechat-state"
    audit_db = tmp_path / "inbound.sqlite3"
    assistant_config = tmp_path / "config.assistant.json"
    config_path = tmp_path / "config.us.json"
    profile = {
        "service_provider": "systemd",
        "config_paths": {"us": str(config_path)},
        "wechat_clawbot": {
            "enabled": True,
            "label": "ops",
            "state_dir": str(state_dir),
            "config_key": "us",
            "assistant_config_path": str(assistant_config),
            "audit_db": str(audit_db),
            "allowed_senders": "wechat:user_1",
        },
        "services": [{"name": "options-monitor-wechat-clawbot.service"}],
    }

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:4] == [str(repo / "om"), "channel", "wechat-clawbot", "serve"]:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="missing bot_token\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[])

    assert out["ok"] is False
    assert out["failed_checks"][0]["check"] == "wechat-clawbot-check"
    assert (
        "manual_check: "
        + " ".join(
            [
                str(repo / "om"),
                "channel",
                "wechat-clawbot",
                "serve",
                "--check",
                "--label",
                "ops",
                "--state-dir",
                str(state_dir),
                "--config-key",
                "us",
                "--config-path",
                str(config_path),
                "--assistant-config",
                str(assistant_config),
                "--audit-db",
                str(audit_db),
                "--allowed-senders",
                "wechat:user_1",
            ]
        )
    ) in out["remediation"]


def test_post_upgrade_feishu_ws_check_preserves_systemd_loaded_env(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "systemd-loaded-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "systemd-loaded-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-systemd-loaded")
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "options-monitor.env"
    repo.mkdir()
    runtime.mkdir()
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_1\n", encoding="utf-8")
    profile = {
        "service_provider": "systemd",
        "runtime_root": str(runtime),
        "env_file": str(env_file),
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:3] == [str(repo / "om"), "inbound", "feishu-ws"]:
            assert "--env-file" in command
            assert command[command.index("--env-file") + 1] == str(env_file)
            env = kwargs.get("env") or {}
            assert env["OM_ENV_FILE"] == str(env_file)
            assert env["OM_RUNTIME_ROOT"] == str(runtime)
            assert env["OM_FEISHU_BOT_APP_ID"] == "systemd-loaded-app"
            assert env["OM_FEISHU_BOT_APP_SECRET"] == "systemd-loaded-secret"
            assert env["OM_FEISHU_BOT_ALLOWED_OPEN_IDS"] == "ou_1"
            assert env["DEEPSEEK_API_KEY"] == "sk-systemd-loaded"
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[])

    assert out["ok"] is True


def test_post_upgrade_feishu_ws_check_uses_sudo_when_env_file_is_not_readable(monkeypatch, tmp_path: Path) -> None:
    from src.application import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "options-monitor.env"
    repo.mkdir()
    runtime.mkdir()
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_1\n", encoding="utf-8")
    profile = {
        "service_provider": "systemd",
        "runtime_root": str(runtime),
        "env_file": str(env_file),
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }
    monkeypatch.setattr(service_upgrade, "_is_root_process", lambda: False)
    monkeypatch.setattr(service_upgrade.os, "access", lambda *_args, **_kwargs: False)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:5] == ["sudo", "-n", str(repo / "om"), "inbound", "feishu-ws"]:
            assert "--env-file" in command
            assert command[command.index("--env-file") + 1] == str(env_file)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade._post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[])

    assert out["ok"] is True


def test_post_upgrade_feishu_ws_check_passes_managed_credential_file_through_sudo(monkeypatch, tmp_path: Path) -> None:
    from src.application import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "options-monitor.env"
    credential_env_file = tmp_path / "run" / "feishu-agent.env"
    repo.mkdir()
    runtime.mkdir()
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_1\n", encoding="utf-8")
    profile = {
        "service_provider": "systemd",
        "runtime_root": str(runtime),
        "env_file": str(env_file),
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "feishu_agent_credential": {
            "enabled": True,
            "runtime_env_file": str(credential_env_file),
        },
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }
    monkeypatch.setattr(service_upgrade, "_is_root_process", lambda: False)
    monkeypatch.setattr(service_upgrade.os, "access", lambda *_args, **_kwargs: False)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:5] == ["sudo", "-n", str(repo / "om"), "inbound", "feishu-ws"]:
            assert "--env-file" in command
            assert command[command.index("--env-file") + 1] == str(env_file)
            assert "--credential-env-file" in command
            assert command[command.index("--credential-env-file") + 1] == str(credential_env_file)
            assert "secret" not in " ".join(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade._post_upgrade_service_health(
        profile=profile,
        repo_root=repo,
        run_cmd=_run_cmd,
        operations=[],
    )

    assert out["ok"] is True


def test_service_upgrade_restart_uses_sudo_prefix_from_deploy_profile(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

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
                },
                "services": [
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == ["options-monitor-trade-intake.service", "options-monitor-feishu-ws.service"]
    assert calls == [
        ["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-feishu-ws.service"],
    ]


def test_service_upgrade_restart_includes_opend_when_profile_declares_it(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {"requires_sudo": False},
                "services": [
                    {"name": "options-monitor-opend.service"},
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == [
        "options-monitor-opend.service",
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert calls == [
        ["systemctl", "restart", "options-monitor-opend.service"],
        ["systemctl", "restart", "options-monitor-trade-intake.service"],
        ["systemctl", "restart", "options-monitor-feishu-ws.service"],
    ]


def test_service_upgrade_restart_uses_sudo_fallback_for_legacy_non_root_systemd_profile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"]]
    assert operations[0]["command_source"] == "non_root_sudo_fallback"


def test_service_upgrade_restart_honors_explicit_non_sudo_profile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["systemctl", "restart", "options-monitor-trade-intake.service"]]
    assert operations[0]["command_source"] == "profile.requires_sudo_false"


def _write_runtime_target_with_server_deps(path: Path) -> None:
    _write_upgrade_release_skeleton(path, "1.0.1")
    (path / "requirements" / "server.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "server.txt").write_text("", encoding="utf-8")


def _create_fake_venv_python(target: Path) -> None:
    _create_fake_venv_python_at(target / ".venv")


def _create_fake_venv_python_at(venv_dir: Path) -> None:
    venv_python = venv_dir / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)


def _fake_git_cache_materialize(command: list[str], *, version: str = "1.0.1") -> subprocess.CompletedProcess | None:
    pi_runtime = _fake_pi_runtime_prepare(command)
    if pi_runtime is not None:
        return pi_runtime
    if command[:3] == ["git", "clone", "--mirror"]:
        Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="mirrored\n", stderr="")
    if len(command) >= 3 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "fetch":
        return subprocess.CompletedProcess(command, 0, stdout="fetched\n", stderr="")
    if len(command) >= 4 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "archive":
        tar_path = Path(command[command.index("-o") + 1])
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        tar_path.write_text("fake tar\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="archived\n", stderr="")
    if command[:2] == ["tar", "-xf"]:
        target = Path(command[command.index("-C") + 1])
        _write_upgrade_release_skeleton(target, version)
        (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
        (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="extracted\n", stderr="")
    return None


def _fake_pi_runtime_prepare(command: list[str]) -> subprocess.CompletedProcess | None:
    if command == ["node", "--version"]:
        return subprocess.CompletedProcess(command, 0, stdout="v22.19.0\n", stderr="")
    if command == ["npm", "--version"]:
        return subprocess.CompletedProcess(command, 0, stdout="10.8.2\n", stderr="")
    if command[:2] == ["npm", "ci"] or command[:2] == ["bash", "scripts/pi_runtime_smoke.sh"]:
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")
    return None


def _fake_release_target_query(
    command: list[str],
    *,
    tags: tuple[str, ...] = ("1.0.0", "1.0.1"),
    remote_url: str = "https://example.invalid/repo.git",
) -> subprocess.CompletedProcess | None:
    if command[:3] == ["git", "config", "--get"]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{remote_url}\n", stderr="")
    if len(command) >= 4 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2:4] == ["config", "--get"]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{remote_url}\n", stderr="")
    if len(command) >= 3 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "for-each-ref":
        stdout = "".join(f"{index:x} refs/tags/v{version}\n" for index, version in enumerate(tags, start=1))
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
    return None


def test_service_upgrade_materialize_uses_existing_git_cache_fetch(tmp_path: Path) -> None:
    from src.application.service_upgrade import _materialize_release_from_git_cache

    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    target = tmp_path / "releases" / "1.0.1"
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _materialize_release_from_git_cache(
        remote_url="https://example.invalid/repo.git",
        tag="v1.0.1",
        target_dir=target,
        cache_root=cache_root,
        run_cmd=_run_cmd,
        operations=[],
    )

    assert out["method"] == "git_cache_archive"
    assert out["cache_initialized"] is False
    assert out["fetched"] is True
    assert target.exists()
    assert ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", "origin"] in calls
    assert ["git", "clone", "--mirror", "https://example.invalid/repo.git", str(cache_repo)] not in calls


def test_service_upgrade_materialize_reuses_existing_target_without_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import _materialize_release_from_git_cache

    target = tmp_path / "releases" / "1.0.1"
    _write_upgrade_release_skeleton(target, "1.0.1")
    calls: list[list[str]] = []

    out = _materialize_release_from_git_cache(
        remote_url="https://example.invalid/repo.git",
        tag="v1.0.1",
        target_dir=target,
        cache_root=tmp_path / "_cache",
        run_cmd=lambda command, **_kwargs: calls.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr=""),
        operations=[],
    )

    assert out["method"] == "reuse_existing_release"
    assert calls == []


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


def test_service_upgrade_runtime_prepare_auto_uses_pip_when_uv_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=operations)

    build_python = str(Path(out["shared_venv_build_path"]) / "bin" / "python")
    assert out["installer"] == "pip"
    assert out["fallback"] is False
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements.txt", "-c", "constraints.txt"] not in calls
    assert [build_python, "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert [build_python, "-m", "pip", "install", "-r", "requirements/server.txt", "-c", "constraints/server.txt"] in calls
    assert (target / ".venv").is_symlink()
    assert Path(out["shared_venv_path"]).exists()
    assert not Path(out["shared_venv_build_path"]).exists()
    assert out["pi_runtime"] == {
        "node_version": "v22.19.0",
        "npm_version": "10.8.2",
        "package_lock_sha256": hashlib.sha256(
            (target / "agent-runtime" / "package-lock.json").read_bytes()
        ).hexdigest(),
        "install_strategy": "npm_ci",
        "reused_from": None,
    }
    assert [
        "npm",
        "ci",
        "--omit=dev",
        "--ignore-scripts",
        "--prefer-offline",
        "--no-audit",
        "--prefix",
        "agent-runtime",
    ] in calls
    assert any(command[:2] == ["bash", "scripts/pi_runtime_smoke.sh"] for command in calls)
    assert operations and all("duration_seconds" in item for item in operations)


def test_service_upgrade_pi_runtime_reuses_previous_modules_when_lock_matches(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    previous = tmp_path / "previous"
    target = tmp_path / "target"
    _write_runtime_target_with_server_deps(previous)
    _write_runtime_target_with_server_deps(target)
    previous_module = previous / "agent-runtime" / "node_modules" / "example" / "index.js"
    previous_module.parent.mkdir(parents=True)
    previous_module.write_text("module.exports = true;\n", encoding="utf-8")
    partial_module = target / "agent-runtime" / "node_modules" / "partial" / "index.js"
    partial_module.parent.mkdir(parents=True)
    partial_module.write_text("partial\n", encoding="utf-8")
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(
        target_dir=target,
        previous_dir=previous,
        run_cmd=_run_cmd,
        operations=[],
    )

    assert out["pi_runtime"]["install_strategy"] == "reuse_previous"
    assert out["pi_runtime"]["reused_from"] == str(previous / "agent-runtime" / "node_modules")
    assert (target / "agent-runtime" / "node_modules" / "example" / "index.js").read_text() == (
        "module.exports = true;\n"
    )
    assert not partial_module.exists()
    assert not any(command[:2] == ["npm", "ci"] for command in calls)
    assert any(command[:2] == ["bash", "scripts/pi_runtime_smoke.sh"] for command in calls)


def test_service_upgrade_runtime_prepare_auto_uses_uv_and_maps_pip_index(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []
    uv_envs: list[dict[str, str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/uv\n", stderr="")
        if command[:4] == ["uv", "venv", "--python", CURRENT_PYTHON]:
            _create_fake_venv_python_at(Path(command[-1]))
        if command[:3] == ["uv", "pip", "install"]:
            uv_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    build_venv = Path(out["shared_venv_build_path"])
    build_python = str(build_venv / "bin" / "python")
    assert out["installer"] == "uv"
    assert out["fallback"] is False
    assert ["uv", "venv", "--python", CURRENT_PYTHON, str(build_venv)] in calls
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements/server.txt", "-c", "constraints/server.txt"] in calls
    assert uv_envs and uv_envs[0]["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert uv_envs[0]["UV_CACHE_DIR"] == str(tmp_path / "_cache" / "uv")
    assert uv_envs[0]["PIP_CACHE_DIR"] == str(tmp_path / "_cache" / "pip")
    assert out["python_spec"] == PYTHON_MINOR
    assert out["uv_cache_dir"] == str(tmp_path / "_cache" / "uv")


def test_service_upgrade_runtime_prepare_pip_mode_skips_uv(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    assert out["installer"] == "pip"
    assert ["sh", "-lc", "command -v uv"] not in calls


def test_service_upgrade_runtime_prepare_reuses_dependency_cached_venv(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    cache_root = tmp_path / "_cache"
    first = tmp_path / "release-a"
    second = tmp_path / "release-b"
    _write_runtime_target_with_server_deps(first)
    _write_runtime_target_with_server_deps(second)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    first_out = _ensure_release_runtime(target_dir=first, cache_root=cache_root, run_cmd=_run_cmd, operations=[])
    first_call_count = len(calls)
    second_out = _ensure_release_runtime(target_dir=second, cache_root=cache_root, run_cmd=_run_cmd, operations=[])
    second_calls = calls[first_call_count:]

    assert first_out["installer"] == "pip"
    assert second_out["installer"] == "cache"
    assert second_out["venv_reused"] is True
    assert second_out["dependency_hash"] == first_out["dependency_hash"]
    assert second_out["dependency_context"]["python_spec"] == PYTHON_MINOR
    assert "duration_seconds" in second_out
    assert Path(first_out["shared_venv_path"]) == Path(second_out["shared_venv_path"])
    assert (second / ".venv").is_symlink()
    assert not Path(second_out["shared_venv_build_path"]).exists()
    assert not any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in second_calls)
    assert not any(command[1:4] == ["-m", "pip", "install"] for command in second_calls)


def test_service_upgrade_dependency_hash_changes_with_dependency_files(tmp_path: Path) -> None:
    from src.application.service_upgrade import _dependency_hash

    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)

    before = _dependency_hash(target, include_server=True)
    (target / "constraints" / "server.txt").write_text("lark-oapi==1.6.6\n", encoding="utf-8")
    after = _dependency_hash(target, include_server=True)

    assert before != after


def test_service_upgrade_runtime_prepare_removes_temp_venv_on_install_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[1:4] == ["-m", "pip", "install"] and "-r" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="install failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    with pytest.raises(RuntimePrepareError) as _caught:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    exc = _caught.value
    assert "install failed" in str(exc)
    assert "duration_seconds" in exc.runtime_prepare
    assert not Path(exc.runtime_prepare["shared_venv_build_path"]).exists()
    assert not Path(exc.runtime_prepare["shared_venv_path"]).exists()


def test_service_upgrade_runtime_prepare_uv_mode_failure_does_not_fallback(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "uv")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command[:4] == ["uv", "venv", "--python", CURRENT_PYTHON]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="uv failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    with pytest.raises(RuntimePrepareError) as _caught:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    exc = _caught.value
    assert exc.runtime_prepare["installer"] == "uv"
    assert exc.runtime_prepare["fallback"] is False
    assert "uv failed" in str(exc.runtime_prepare["uv_error"])

    assert not any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in calls)


def test_service_upgrade_runtime_prepare_auto_falls_back_to_pip_after_uv_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            return pi_runtime
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/uv\n", stderr="")
        if command[:4] == ["uv", "venv", "--python", CURRENT_PYTHON]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="uv venv\n", stderr="")
        if command[:3] == ["uv", "pip", "install"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="uv install failed\n")
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    assert out["installer"] == "pip"
    assert out["fallback"] is True
    assert out["fallback_from"] == "uv"
    assert "uv install failed" in str(out["uv_error"])
    assert any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in calls)


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


def test_service_upgrade_pi_prepare_reruns_npm_after_partial_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    npm_attempts = 0

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal npm_attempts
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if pi_runtime is not None:
            if command[:2] == ["npm", "ci"]:
                npm_attempts += 1
                if npm_attempts == 1:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="partial npm failure\n")
            return pi_runtime
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    with pytest.raises(RuntimePrepareError):
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    assert npm_attempts == 2
    assert out["pi_runtime"]["node_version"] == "v22.19.0"


def test_service_upgrade_pi_smoke_timeout_keeps_structured_prepare_evidence(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        pi_runtime = _fake_pi_runtime_prepare(list(command))
        if command[:2] == ["bash", "scripts/pi_runtime_smoke.sh"]:
            raise subprocess.TimeoutExpired(command, timeout=180)
        if pi_runtime is not None:
            return pi_runtime
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    with pytest.raises(RuntimePrepareError) as caught:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.runtime_prepare["pi_runtime"]["node_version"] == "v22.19.0"
    assert caught.value.runtime_prepare["pi_runtime"]["npm_version"] == "10.8.2"
    assert caught.value.runtime_prepare["pi_runtime"]["package_lock_sha256"]


def test_service_upgrade_restart_denied_includes_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import ServiceRestartError, _restart_services_from_profile

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
                },
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Failed to restart: Access denied\n")

    with pytest.raises(ServiceRestartError) as _caught:
        _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)
    exc = _caught.value
    remediation = exc.remediation

    assert operations[-1]["returncode"] == 1
    assert "manual_restart: sudo systemctl restart options-monitor-trade-intake.service" in remediation
    assert "liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in remediation


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


def test_service_upgrade_restart_uses_explicit_restart_services_from_profile(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {
                    "command_prefix": ["sudo", "-n", "systemctl"],
                    "services": [
                        "options-monitor-trade-intake.service",
                        "options-monitor-feishu-ws.service",
                        "options-monitor-custom-worker.service",
                    ],
                },
                "services": [
                    {"name": "options-monitor-tick-us.service"},
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
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
        "options-monitor-custom-worker.service",
    ]
    assert calls == [
        ["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-feishu-ws.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-custom-worker.service"],
    ]


def test_service_upgrade_restart_supports_restart_command_string(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {
                    "restart_command": "sudo -n systemctl restart",
                    "services": ["options-monitor-trade-intake.service"],
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"]]


def test_service_upgrade_restart_no_profile_is_noop(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == []
    assert calls == []


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
        "migrate_legacy_json_once: ./om config migrate-yaml --apply --output config.yaml",
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


def test_cli_service_render_returns_json(capsys, tmp_path: Path) -> None:
    from src.interfaces.cli.main import main

    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--config-yaml",
        str(tmp_path / "runtime" / "config.yaml"),
        "--env-file",
        str(tmp_path / "options-monitor.env"),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["summary"]["service_provider"] == "systemd"
    assert payload["data"]["env_file"] == str(tmp_path / "options-monitor.env")
    profile = next(item for item in payload["data"]["files"] if item["relative_path"] == "service.profile.json")
    assert profile.get("content") is None
    assert payload["data"]["files"][0].get("content") is None


def test_cli_service_render_no_content_still_writes_files(capsys, tmp_path: Path) -> None:
    from src.interfaces.cli.main import main

    output_dir = tmp_path / "rendered"
    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--config-yaml",
        str(tmp_path / "runtime" / "config.yaml"),
        "--output-dir",
        str(output_dir),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["files"][0].get("content") is None
    assert "ExecStart=" in (output_dir / "systemd" / "options-monitor-tick-us.service").read_text(encoding="utf-8")


def test_cli_service_render_can_include_feishu_agent_credential(
    capsys,
    tmp_path: Path,
) -> None:
    from src.interfaces.cli.main import main

    output_dir = tmp_path / "rendered"
    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--config-yaml",
        str(tmp_path / "runtime" / "config.yaml"),
        "--include-feishu-agent-credential",
        "--output-dir",
        str(output_dir),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    profile = json.loads(
        (output_dir / "service.profile.json").read_text(encoding="utf-8")
    )
    helper = (
        output_dir
        / "systemd"
        / "libexec"
        / "options-monitor-materialize-feishu-agent-credential"
    )
    assert profile["feishu_agent_credential"]["enabled"] is True
    assert helper.stat().st_mode & 0o777 == 0o755
    assert (
        output_dir
        / "systemd"
        / "options-monitor-feishu-agent-credential.service"
    ).is_file()


def test_cli_service_render_can_select_runtime_file_secret_delivery(
    capsys,
    tmp_path: Path,
) -> None:
    from src.interfaces.cli.main import main

    output_dir = tmp_path / "rendered"
    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--config-yaml",
        str(tmp_path / "runtime" / "config.yaml"),
        "--include-secret-credentials",
        "--secret-credential-delivery",
        "runtime-files",
        "--output-dir",
        str(output_dir),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    profile = json.loads(
        (output_dir / "service.profile.json").read_text(encoding="utf-8")
    )
    assert profile["secret_credentials"]["delivery"] == "runtime-files"
    helper = (
        output_dir
        / "systemd"
        / "libexec"
        / "options-monitor-materialize-service-credentials"
    )
    assert helper.is_file()
    assert helper.stat().st_mode & 0o777 == 0o755


def test_cli_service_drift_reports_missing_units(monkeypatch, capsys, tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.interfaces.cli.main import main

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
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

    rc = main(["service", "drift", "--repo-root", str(repo), "--runtime-root", str(runtime)])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["missing_required_units"] == ["options-monitor-projection-verify.timer"]


def test_cli_update_check_delegates_cache_root_to_application(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli_main

    calls: list[dict[str, object]] = []

    def _fake_check(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "status": "checked"}

    monkeypatch.setattr(cli_main, "service_upgrade_check", _fake_check)

    rc = cli_main.main(
        [
            "update",
            "check",
            "--repo-root",
            str(tmp_path / "current"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--cache-root",
            str(tmp_path / "_cache"),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert calls[0]["cache_root"] == str(tmp_path / "_cache")


def test_cli_service_cleanup_delegates_to_application(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli_main

    calls: list[dict[str, object]] = []

    def _fake_cleanup(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "status": "dry_run", "changed": False}

    monkeypatch.setattr(cli_main, "service_cleanup", _fake_cleanup)

    rc = cli_main.main(
        [
            "service",
            "cleanup",
            "--repo-root",
            str(tmp_path / "current"),
            "--releases-root",
            str(tmp_path / "releases"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--keep-releases",
            "3",
            "--cleanup-downloads",
            "--cleanup-pip-cache",
            "--cleanup-output-runs",
            "--output-runs-keep-days",
            "10",
            "--output-runs-keep-count",
            "50",
            "--cleanup-runtime-logs",
            "--runtime-logs-keep-days",
            "5",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_name"] == "service.cleanup"
    assert payload["ok"] is True
    assert calls[0]["repo_root"] == str(tmp_path / "current")
    assert calls[0]["releases_root"] == str(tmp_path / "releases")
    assert calls[0]["runtime_root"] == str(tmp_path / "runtime")
    assert calls[0]["keep_releases"] == 3
    assert calls[0]["cleanup_downloads"] is True
    assert calls[0]["cleanup_pip_cache"] is True
    assert calls[0]["cleanup_output_runs"] is True
    assert calls[0]["output_runs_keep_days"] == 10
    assert calls[0]["output_runs_keep_count"] == 50
    assert calls[0]["cleanup_runtime_logs"] is True
    assert calls[0]["runtime_logs_keep_days"] == 5
    assert calls[0]["confirm"] is False


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


def test_service_cleanup_reports_delete_failure_and_only_actual_freed_bytes(monkeypatch, tmp_path: Path) -> None:
    import src.application.service_cleanup as cleanup_module

    releases = tmp_path / "releases"
    active = releases / "1.2.0"
    previous = releases / "1.1.0"
    old_ok = releases / "1.0.0"
    old_failed = releases / "0.9.0"
    for path, payload in (
        (active, b"active"),
        (previous, b"previous"),
        (old_ok, b"delete-me"),
        (old_failed, b"keep-me"),
    ):
        path.mkdir(parents=True)
        (path / "VERSION").write_text(path.name, encoding="utf-8")
        (path / "payload.bin").write_bytes(payload)
    current = tmp_path / "current"
    current.symlink_to(active, target_is_directory=True)
    original_delete = cleanup_module._delete_path

    def _delete(path: Path) -> None:
        if path == old_failed:
            raise PermissionError("denied")
        original_delete(path)

    monkeypatch.setattr(cleanup_module, "_delete_path", _delete)

    out = cleanup_module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
    )

    assert out["ok"] is False
    assert out["status"] == "partial_failure"
    assert out["failure_count"] == 1
    assert out["freed_bytes"] > 0
    assert out["freed_bytes"] < out["estimated_freed_bytes"]
    assert not old_ok.exists()
    assert old_failed.exists()


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


def test_cli_run_trade_intake_delegates_to_application(monkeypatch) -> None:
    import src.application.trades.process_supervisor as process_supervisor
    from src.interfaces.cli.main import main

    calls: list[list[str]] = []
    monkeypatch.setattr(process_supervisor, "run_trade_intake_process", lambda argv: calls.append(list(argv)) or 0)

    rc = main([
        "run",
        "trade-intake",
        "--config",
        "config.us.json",
        "--mode",
        "apply",
        "--once",
    ])

    assert rc == 0
    assert calls == [["--config", "config.us.json", "--mode", "apply", "--once"]]


def test_cli_run_trade_intake_delegates_explicit_host_port(monkeypatch) -> None:
    import src.application.trades.process_supervisor as process_supervisor
    from src.interfaces.cli.main import main

    calls: list[list[str]] = []
    monkeypatch.setattr(process_supervisor, "run_trade_intake_process", lambda argv: calls.append(list(argv)) or 0)

    rc = main([
        "run",
        "trade-intake",
        "--config",
        "config.us.json",
        "--host",
        "127.0.0.2",
        "--port",
        "22222",
        "--once",
    ])

    assert rc == 0
    assert calls == [["--config", "config.us.json", "--host", "127.0.0.2", "--port", "22222", "--once"]]


def test_cli_run_trade_intake_delegates_reconcile_state_flags(monkeypatch) -> None:
    import src.application.trades.process_supervisor as process_supervisor
    from src.interfaces.cli.main import main

    calls: list[list[str]] = []
    monkeypatch.setattr(process_supervisor, "run_trade_intake_process", lambda argv: calls.append(list(argv)) or 0)

    rc = main([
        "run",
        "trade-intake",
        "--config",
        "config.us.json",
        "--reconcile-state",
        "--account",
        "lx",
        "--deal-id",
        "deal-1",
        "--apply",
    ])

    assert rc == 0
    assert calls == [
        [
            "--config",
            "config.us.json",
            "--reconcile-state",
            "--account",
            "lx",
            "--deal-id",
            "deal-1",
            "--apply",
        ]
    ]


def test_cli_run_trade_intake_delegates_runtime_root(monkeypatch, tmp_path: Path) -> None:
    import src.application.trades.process_supervisor as process_supervisor
    from src.interfaces.cli.main import main

    runtime_root = tmp_path / "runtime"
    calls: list[list[str]] = []
    monkeypatch.setattr(process_supervisor, "run_trade_intake_process", lambda argv: calls.append(list(argv)) or 0)

    rc = main([
        "run",
        "trade-intake",
        "--config",
        "config.us.json",
        "--runtime-root",
        str(runtime_root),
        "--reconcile-state",
        "--deal-id",
        "deal-1",
        "--dry-run",
    ])

    assert rc == 0
    assert calls == [
        [
            "--config",
            "config.us.json",
            "--runtime-root",
            str(runtime_root),
            "--reconcile-state",
            "--deal-id",
            "deal-1",
            "--dry-run",
        ]
    ]


def _install_complete_systemd_bundle(bundle: dict, systemd_root: Path) -> None:
    systemd_root.mkdir(parents=True, exist_ok=True)
    live_root = Path("/etc/systemd/system")
    for item in bundle["files"]:
        kind = str(item.get("kind") or "")
        if kind in {"systemd_service", "systemd_timer"}:
            path = systemd_root / Path(item["install_path"]).name
        elif kind in {"systemd_dropin", "systemd_secret_dropin"}:
            path = systemd_root / Path(item["install_path"]).relative_to(live_root)
        elif kind == "systemd_executable":
            path = Path(item["install_path"])
        else:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")
        if item.get("mode") is not None:
            path.chmod(int(item["mode"]))


def _legacy_credential_migration_fixture(monkeypatch, tmp_path: Path) -> dict[str, object]:
    import src.application.service_deploy as service_deploy_module
    import src.application.service_drift as service_drift_module

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    libexec = tmp_path / "libexec"
    legacy_helper = libexec / "options-monitor-materialize-feishu-agent-credential"
    target_helper = libexec / "options-monitor-materialize-service-credentials"
    legacy_env = tmp_path / "run" / "options-monitor-feishu-agent.env"
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(
        service_deploy_module,
        "DEFAULT_SECRET_CREDENTIAL_HELPER",
        target_helper,
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_SECRET_CREDENTIAL_HELPER",
        target_helper,
    )
    bundle = service_deploy_module.render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_quality_monitoring=True,
        include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=legacy_helper,
        feishu_agent_credential_env_file=legacy_env,
        use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    (runtime / "service.profile.json").write_text(
        files["service.profile.json"]["content"],
        encoding="utf-8",
    )
    _install_complete_systemd_bundle(bundle, systemd_root)
    legacy_env.parent.mkdir(parents=True, exist_ok=True)
    legacy_env.write_text("OM_FEISHU_BOT_APP_SECRET=not-a-real-secret\n", encoding="utf-8")
    compat = (
        systemd_root
        / "options-monitor-quality-http.service.d"
        / service_drift_module.SECRET_BACKEND_COMPAT_DROPIN
    )
    compat.parent.mkdir(parents=True, exist_ok=True)
    compat.write_text(
        "[Service]\nLoadCredential=\nLoadCredentialEncrypted=\n"
        "Environment=OM_SECRET_BACKEND=env\n",
        encoding="utf-8",
    )
    return {
        "repo": repo,
        "runtime": runtime,
        "systemd_root": systemd_root,
        "legacy_helper": legacy_helper,
        "target_helper": target_helper,
        "legacy_env": legacy_env,
        "compat": compat,
        "store": tmp_path / "credstore.encrypted",
    }


def _credential_migration_runner(
    calls: list[list[str]],
    *,
    decrypt_ok: bool = True,
    restart_failures: list[int] | None = None,
):  # type: ignore[no-untyped-def]
    failures = restart_failures if restart_failures is not None else []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[:2] == ["/usr/bin/findmnt", "--noheadings"]:
            return subprocess.CompletedProcess(command, 0, stdout="tmpfs\n", stderr="")
        if "/usr/bin/systemd-creds" in command:
            return subprocess.CompletedProcess(
                command,
                0 if decrypt_ok else 1,
                stdout="must-not-be-captured",
                stderr="must-not-be-captured",
            )
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        if "--property=Result" in command:
            return subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
        if "restart" in command and command[-1].endswith(".service") and failures:
            failures[0] -= 1
            if failures[0] >= 0:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
            failures.clear()
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    return _run_cmd


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


def test_cli_service_credentials_migrate_passes_explicit_delivery(tmp_path: Path) -> None:
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli.service_ops import handle_service_update_command

    args = parse_args(
        [
            "service",
            "credentials-migrate",
            "--repo-root",
            str(tmp_path / "repo"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--secret-credential-delivery",
            "runtime-files",
        ]
    )
    calls: list[dict[str, object]] = []

    def _migrate(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return {"ok": True, "status": "dry_run", "changed": False}

    out = handle_service_update_command(
        args,
        migrate_service_credentials_fn=_migrate,
    )

    assert out["ok"] is True
    assert out["tool_name"] == "service.credentials-migrate"
    assert out["data"]["dry_run"] is True
    assert out["data"]["write_applied"] is False
    assert calls == [
        {
            "repo_root": str(tmp_path / "repo"),
            "runtime_root": str(tmp_path / "runtime"),
            "profile_path": None,
            "secret_credential_delivery": "runtime-files",
            "secret_credential_store_root": None,
            "confirm": False,
        }
    ]
