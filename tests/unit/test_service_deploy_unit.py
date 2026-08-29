from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from tests.service_deploy_test_support import (
    CURRENT_PYTHON,
    PYTHON_MINOR,
    _futu_service_account,
    _write_service_account_config,
    _two_futu_service_accounts,
    _write_upgrade_release_skeleton,
    _write_runtime_target_with_server_deps,
    _create_fake_venv_python_at,
    _fake_git_cache_materialize,
    _fake_pi_runtime_prepare,
)

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
    tick_hk = files["systemd/options-monitor-tick-hk.service"]["content"]
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
    assert "--timeout 600" in tick
    assert "--timeout 600" in tick_hk
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
