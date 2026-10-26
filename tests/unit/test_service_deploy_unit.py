from __future__ import annotations

import json
import os
import plistlib
import shlex
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
    _render_bundle,
)


def test_render_systemd_bundle_uses_runtime_root_and_canonical_entrypoints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])

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
    assert str(repo / "scripts/auto_close_schedule.sh") + " --market us" in files["systemd/options-monitor-auto-close-us.service"]["content"]
    auto_close = files["systemd/options-monitor-auto-close-us.service"]["content"]
    assert "--apply --yes" in auto_close
    assert "--runtime-root " + str(runtime) in auto_close
    assert "TimeoutStartSec=620" in auto_close
    assert "TimeoutStopSec=10" in auto_close
    assert "Restart=on-failure" in auto_close
    assert "RestartSec=300" in auto_close
    assert "RestartPreventExitStatus=2 78" in auto_close
    assert "StartLimitIntervalSec=14400" in auto_close
    assert "StartLimitBurst=12" in auto_close
    assert "KillMode=control-group" in auto_close
    assert 12 * (620 + 10 + 300) < 14400 < 23 * 3600
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 America/New_York" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer
    assert str(repo / "om") + " run trade-intake" in intake
    runtime_status = files["systemd/options-monitor-runtime-status.service"]["content"]
    assert str(repo / "om") + " status --profile-path " + str(runtime / "service.profile.json") in runtime_status
    assert "--journal-summary" in runtime_status
    assert "SyslogLevelPrefix=yes" in runtime_status
    assert str(repo / "om-agent") not in runtime_status
    assert "Restart=always" in intake
    assert "RestartPreventExitStatus=78" in intake
    assert "SyslogLevelPrefix=yes" in intake
    assert "OnFailure=options-monitor-trade-intake-alert.service" in intake
    alert_unit = files["systemd/options-monitor-trade-intake-alert.service"]["content"]
    assert "run service-failure-alert --unit options-monitor-trade-intake.service" in alert_unit
    assert "TimeoutStartSec=120" in alert_unit
    heartbeat_unit = files["systemd/options-monitor-trade-intake-heartbeat.service"]["content"]
    heartbeat_timer = files["systemd/options-monitor-trade-intake-heartbeat.timer"]["content"]
    assert "run trade-intake-heartbeat-check --unit options-monitor-trade-intake.service" in heartbeat_unit
    assert "OnUnitActiveSec=1min" in heartbeat_timer
    assert "RestartPreventExitStatus=" not in tick
    assert "RestartPreventExitStatus=" not in runtime_status
    assert "RestartPreventExitStatus=" not in verify
    assert "UMask=0077" in tick
    assert "UMask=0077" in intake


def test_systemd_enable_suggestions_require_install_section(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_service_account_config(repo / "config.us.json", _two_futu_service_accounts(tmp_path))
    bundle = _render_bundle(
        repo, tmp_path / "runtime", accounts=["lx", "sy"], markets=["us", "hk"],
        config_paths={"us": config, "hk": config}, include_opend=True,
        include_auto_upgrade=True, include_quality_monitoring=True,
        include_feishu_ws=True, feishu_ws_config_key="us",
        include_wechat_clawbot=True, wechat_clawbot_config_key="us",
        wechat_clawbot_allowed_senders="fixture-sender", include_feishu_agent_credential=True,
    )
    expected = {
        "options-monitor-tick-us.timer", "options-monitor-tick-hk.timer",
        "options-monitor-auto-close-us.timer", "options-monitor-auto-close-hk.timer",
        "options-monitor-projection-verify.timer", "options-monitor-runtime-status.timer",
        "options-monitor-trade-intake-heartbeat.timer", "options-monitor-upgrade.timer",
        "options-monitor-quality-refresh.timer", "options-monitor-quality-recheck.timer",
        "options-monitor-quality-day-end-us.timer", "options-monitor-quality-day-end-hk.timer",
        "options-monitor-trade-intake.service", "options-monitor-opend-lx.service",
        "options-monitor-opend-sy.service", "options-monitor-feishu-ws.service",
        "options-monitor-wechat-clawbot.service", "options-monitor-feishu-agent-credential.service",
    }
    commands = bundle["commands"]["enable"]
    assert len(commands) == len(expected)
    assert set(commands) == {f"systemctl enable --now {name}" for name in expected}
    files = {Path(item["install_path"]).name: item for item in bundle["files"]}
    for name in expected:
        assert "[Install]" in files[name]["content"].splitlines()
    assert "Type=oneshot" in files["options-monitor-feishu-agent-credential.service"]["content"]
    assert "systemctl status options-monitor-trade-intake-heartbeat.service" in bundle["commands"]["status"]

@pytest.mark.parametrize("include_legacy_credentials", [False, True])
def test_managed_systemd_names_cover_renderer_and_preserve_unknown_names(
    tmp_path: Path, include_legacy_credentials: bool,
) -> None:
    from src.application.service_deploy import managed_systemd_unit_names

    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_service_account_config(
        repo / "config.us.json", _two_futu_service_accounts(tmp_path),
    )
    bundle = _render_bundle(
        repo, tmp_path / "runtime", accounts=["lx", "sy"], markets=["us", "hk"],
        config_paths={"us": config, "hk": config}, include_opend=True,
        include_auto_upgrade=True, include_feishu_ws=True, include_wechat_clawbot=True,
        feishu_ws_config_key="us", wechat_clawbot_config_key="us",
        wechat_clawbot_allowed_senders="fixture-sender",
        include_quality_monitoring=True,
        include_feishu_agent_credential=include_legacy_credentials,
    )
    rendered = {
        Path(item["install_path"]).name for item in bundle["files"]
        if item["kind"] in {"systemd_service", "systemd_timer"}
    }
    managed = managed_systemd_unit_names(accounts=["lx", "sy"])
    assert rendered <= managed
    assert {"options-monitor-opend-lx.service", "options-monitor-opend-sy.service"} <= rendered
    assert {
        "options-monitor-position-advice-promotion.service",
        "options-monitor-position-advice-promotion.timer",
        "options-monitor-ai-evidence-collector.service",
        "options-monitor-ai-evidence-collector.timer",
        "options-monitor-quality-http.service",
    } <= managed
    assert not managed & {
        "options-monitor-my-experiment.service", "options-monitor-my-experiment.timer",
        "options-monitor-tick-us.service.bak", "options-monitor-tick-us.timer.disabled",
        "options-monitor-feishu-ws.timer", "options-monitor-tick-cn.service",
        "options-monitor-opend-other.service",
    }
    assert "options-monitor-opend-lx.service" not in managed_systemd_unit_names(accounts=[])


def test_render_systemd_bundle_omits_retired_ai_evidence_collector(
    tmp_path: Path,
) -> None:
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
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], config_yaml=config_yaml)
    paths = {item["relative_path"] for item in bundle["files"]}
    assert "systemd/options-monitor-ai-evidence-collector.service" not in paths
    assert "systemd/options-monitor-ai-evidence-collector.timer" not in paths
    assert "ai_evidence_collector" not in str(bundle)

def test_render_systemd_bundle_service_hardening() -> None:
    repo = Path("/tmp/om-svc-repo")
    runtime = Path("/tmp/om-svc-runtime")
    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us", "hk"])
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
    assert "TimeoutStartSec=900" in tick
    assert "TimeoutStopSec=30" in tick
    assert "SyslogLevelPrefix=yes" in tick
    assert "TimeoutStartSec=" not in runtime_status
    assert "TimeoutStartSec=" not in verify
    assert "TimeoutStartSec=" not in intake
    assert "--timeout 600" in tick
    assert "--timeout 600" in tick_hk
    assert "RuntimeMaxSec=" not in "\n".join(item["content"] for item in files.values())
    assert "[Install]\nWantedBy=multi-user.target" in intake
    assert "[Install]\nWantedBy=multi-user.target" not in tick
    assert "OnCalendar=*-*-* 00:07:00 America/New_York" in auto_close_us_timer
    assert "OnCalendar=*-*-* 00:05:00 Asia/Hong_Kong" in auto_close_hk_timer
    assert "Persistent=true" in auto_close_us_timer
    assert "Persistent=true" in auto_close_hk_timer
    assert "OnBootSec=2min" in auto_close_us_timer
    assert "OnBootSec=2min" in auto_close_hk_timer
    for name, item in files.items():
        if name.endswith(".timer") and "OnCalendar=" in item["content"] and "auto-close" not in name:
            assert "OnBootSec=" not in item["content"]
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

@pytest.mark.parametrize("delivery", [None, "load-credential-encrypted", "runtime-files"])
def test_secure_bundle_strips_secret_env_from_all_services(tmp_path: Path, delivery: str | None) -> None:
    from src.application.secret_store import legacy_secret_env_names
    env_file = tmp_path / "ordinary.env"
    bundle = _render_bundle(
        tmp_path, tmp_path / "runtime", accounts=["lx"], markets=["us", "hk"], env_file=env_file,
        include_feishu_ws=True, feishu_ws_config_key="us", include_quality_monitoring=True, include_auto_upgrade=True,
        include_secret_credentials=delivery is not None,
        secret_credential_delivery=delivery or "load-credential-encrypted", deploy_user="liuxie",
    )
    services = [item for item in bundle["files"] if item["kind"] == "systemd_service"]
    assert services
    assert not any("quality-http" in item["relative_path"] for item in bundle["files"])
    unset = "UnsetEnvironment=" + " ".join(sorted(legacy_secret_env_names()))
    assert "OM_QUALITY_READ_TOKEN" in unset
    for item in services:
        assert (unset in item["content"]) is (delivery is not None)
        assert f"EnvironmentFile={env_file}" in item["content"]
    profile = json.loads(next(item["content"] for item in bundle["files"] if item["relative_path"] == "service.profile.json"))
    if delivery is not None:
        bindings = profile["secret_credentials"]["service_credentials"]
        assert "options-monitor-quality-refresh.service" not in bindings
        assert "options-monitor-upgrade.service" not in bindings
        assert "options-monitor-projection-verify.service" not in bindings


def test_render_systemd_bundle_uses_per_unit_encrypted_credentials(tmp_path: Path) -> None:
    from src.application.secret_store import legacy_secret_env_names

    repo = tmp_path / "repo"
    repo.mkdir()
    store = tmp_path / "credstore.encrypted"
    bundle = _render_bundle(
        repo, tmp_path / "runtime", accounts=["lx"], markets=["us"], include_opend=True, opend_root=tmp_path / "opend", include_feishu_ws=True,
        include_quality_monitoring=True, include_secret_credentials=True, secret_credential_store_root=store,
        deploy_user="liuxie",
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    profile = json.loads(files["service.profile.json"]["content"])

    tick = files[
        "systemd/options-monitor-tick-us.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    alert_secret = files[
        "systemd/options-monitor-trade-intake-alert.service.d/zzzz-secret-credentials.conf"
    ]["content"]
    assert "om-feishu-bot-app-secret" in alert_secret
    assert f"om-feishu-holdings-app-secret:{store}/om-feishu-holdings-app-secret" in tick
    assert f"om-feishu-bot-app-secret:{store}/om-feishu-bot-app-secret" in tick
    assert "om-quality-read-token" not in tick
    assert "om-inbound-operation-hmac-key" not in tick
    assert (
        "UnsetEnvironment=" + " ".join(sorted(legacy_secret_env_names()))
    ) in tick

    assert not any("quality-http" in name for name in files)

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
    bundle = _render_bundle(
        repo, tmp_path / "runtime", accounts=["lx"], markets=["us"], include_opend=True, opend_root=tmp_path / "opend", include_feishu_ws=True,
        include_quality_monitoring=True, include_secret_credentials=True, secret_credential_delivery="runtime-files",
        secret_credential_store_root=store, deploy_user="liuxie",
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

    assert not any("quality-http" in name for name in files)

    secret_profile = profile["secret_credentials"]
    assert secret_profile["backend"] == "systemd"
    assert secret_profile["delivery"] == "runtime-files"
    assert secret_profile["helper_path"] == str(DEFAULT_SECRET_CREDENTIAL_HELPER)
    assert secret_profile["runtime_root"] == str(DEFAULT_SECRET_CREDENTIAL_RUNTIME_ROOT)
    assert secret_profile["legacy_env_materializer_enabled"] is False

def test_render_rejects_unknown_secret_credential_delivery(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="secret credential delivery"):
        _render_bundle(
            tmp_path, markets=["us"], include_secret_credentials=True, secret_credential_delivery="automatic-fallback",
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
            "liuxie\rExecStartPre=+/tmp/injected",
            "/etc/credstore.encrypted",
            "deploy user",
        ),
        (
            "liuxie\r\nExecStartPre=+/tmp/injected",
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
    with pytest.raises(ValueError, match=error):
        _render_bundle(
            tmp_path, markets=["us"], include_secret_credentials=True, secret_credential_delivery="runtime-files",
            secret_credential_store_root=store_root, deploy_user=deploy_user,
        )

def test_render_native_credentials_rejects_systemd_store_expansion(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="systemd expansion"):
        _render_bundle(
            tmp_path, markets=["us"], include_secret_credentials=True,
            secret_credential_delivery="load-credential-encrypted", secret_credential_store_root="/etc/credstore.%n",
        )

def test_render_rejects_mixed_legacy_and_per_unit_secret_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _render_bundle(tmp_path, markets=["us"], include_secret_credentials=True, include_feishu_agent_credential=True)

def test_render_launchd_bundle_rejects_feishu_agent_credential(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supported only for systemd"):
        _render_bundle(tmp_path, target="launchd", include_feishu_agent_credential=True)

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
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    bundle = _render_bundle(repo, runtime, target="launchd", accounts=["lx"], markets=["us"])
    files = {item["relative_path"]: item for item in bundle["files"]}
    content = files["launchd/com.options-monitor.runtime-status.plist"]["content"]
    assert str(repo / "om") in content
    assert "status" in content
    assert "--journal-summary" in content
    assert str(repo / "om-agent") not in content

def test_render_systemd_bundle_can_include_opend_service(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    opend = tmp_path / "futu-opend" / "current"
    repo.mkdir()
    opend.mkdir(parents=True)

    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], include_opend=True, opend_root=opend)

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

    bundle = _render_bundle(
        repo, runtime, accounts=["lx", "sy"], markets=["us"], config_paths={"us": config_path}, include_opend=True,
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
            "host": "127.0.0.1",
            "port": 11111,
        },
        {
            "account": "sy",
            "root": str(opend_sy),
            "executable": str(opend_sy / "FutuOpenD"),
            "service_name": "options-monitor-opend-sy.service",
            "host": "127.0.0.1",
            "port": 11112,
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

    bundle = _render_bundle(repo, runtime, accounts=["lx", "sy"], markets=["us"], config_yaml=config_yaml, include_opend=True)

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
            "host": "127.0.0.1",
            "port": 11111,
        },
        {
            "account": "sy",
            "root": str(opend_sy),
            "executable": str(opend_sy / "FutuOpenD"),
            "service_name": "options-monitor-opend-sy.service",
            "host": "127.0.0.1",
            "port": 11112,
        },
    ]

def test_render_systemd_bundle_rejects_invalid_opend_config_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    config_path = tmp_path / "config.us.json"
    repo.mkdir()
    runtime.mkdir()
    config_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError, match="failed to parse service config JSON"):
        _render_bundle(
            repo, runtime, accounts=["lx", "sy"], markets=["us"], config_paths={"us": config_path}, include_opend=True,
        )

def test_render_systemd_bundle_aligns_hk_tick_timer_to_calendar_boundaries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["hk"])

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-hk.service"]["content"]
    tick_timer = files["systemd/options-monitor-tick-hk.timer"]["content"]

    assert str(repo / "om") + " run tick-cron --market hk" in tick
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer

def test_render_systemd_bundle_can_include_auto_upgrade_timer(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    default_bundle = _render_bundle(repo, runtime, markets=["us"])
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-upgrade.service" not in default_files

    bundle = _render_bundle(repo, runtime, markets=["us"], include_auto_upgrade=True)

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
    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    default_bundle = _render_bundle(repo, runtime, markets=["us", "hk"])
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-quality-http.service" not in default_files

    bundle = _render_bundle(repo, runtime, markets=["us", "hk"], include_opend=True, opend_root=tmp_path / "opend", include_quality_monitoring=True)

    files = {item["relative_path"]: item for item in bundle["files"]}
    assert not any("quality-http" in name for name in files)
    refresh = files["systemd/options-monitor-quality-refresh.service"]["content"]
    refresh_timer = files["systemd/options-monitor-quality-refresh.timer"]["content"]
    recheck = files["systemd/options-monitor-quality-recheck.service"]["content"]
    recheck_timer = files["systemd/options-monitor-quality-recheck.timer"]["content"]
    day_end_us = files["systemd/options-monitor-quality-day-end-us.service"]["content"]
    day_end_us_timer = files["systemd/options-monitor-quality-day-end-us.timer"]["content"]
    day_end_hk_timer = files["systemd/options-monitor-quality-day-end-hk.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " quality refresh --config-key us --config-key hk --no-deep" in refresh
    assert "TimeoutStartSec=600" in refresh
    assert "OnUnitActiveSec=15min" in refresh_timer
    assert str(repo / "om") + " quality recheck-due --config-key us --config-key hk" in recheck
    assert "After=network-online.target options-monitor-opend.service" in recheck
    assert "TimeoutStartSec=300" in recheck
    assert "OnUnitActiveSec=1min" in recheck_timer
    assert str(repo / "om") + " quality refresh --config-key us --day-end-strict" in day_end_us
    assert "TimeoutStartSec=300" in day_end_us
    assert "OnCalendar=Mon..Fri *-*-* 16:30:00 America/New_York" in day_end_us_timer
    assert "OnCalendar=Mon..Fri *-*-* 16:30:00 Asia/Hong_Kong" in day_end_hk_timer
    assert profile["quality_monitoring"] == {
        "enabled": True,
        "artifact_path": str(runtime / "output_shared" / "state" / "quality" / "status.v1.json"),
        "regular_refresh_interval": "15min",
        "recheck_interval": "1min",
        "day_end_calendars": {
            "us": "Mon..Fri *-*-* 16:30:00 America/New_York",
            "hk": "Mon..Fri *-*-* 16:30:00 Asia/Hong_Kong",
        },
    }

def test_render_launchd_bundle_rejects_quality_monitoring(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    repo.mkdir()

    with pytest.raises(ValueError, match="supported only for systemd"):
        _render_bundle(repo, target="launchd", include_quality_monitoring=True)

def test_render_systemd_bundle_records_yaml_authoring_source(tmp_path: Path) -> None:
    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    config_yaml = runtime / "config.yaml"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, markets=["us", "hk"], config_yaml=config_yaml, include_auto_upgrade=True)

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
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")

    bundle = _render_bundle(repo, runtime, markets=["us"], config_yaml=config_yaml, include_feishu_ws=True)

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-feishu-ws.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " inbound feishu-ws" in service
    assert "--config-path " + str(repo / "config.us.json") in service
    assert "--config-key" not in service
    assert "--bot-config " + str(runtime / "resolved" / "config.bot.json") in service
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
    assert profile["bot_config_path"] == str(runtime / "resolved" / "config.bot.json")
    assert profile["feishu_ws"]["bot_config_path"] == str(runtime / "resolved" / "config.bot.json")
    assert profile["feishu_ws"]["lock_path"] == str(runtime / "locks" / "feishu-ws.lock")
    assert "systemctl enable --now options-monitor-feishu-ws.service" in bundle["commands"]["enable"]

def test_render_systemd_bundle_can_include_wechat_clawbot_service(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    config_yaml = runtime / "config.yaml"
    config_yaml.write_text("accounts: {}\nmarkets: {}\n", encoding="utf-8")

    bundle = _render_bundle(
        repo, runtime, markets=["us"], config_yaml=config_yaml, include_wechat_clawbot=True, wechat_clawbot_label="ops",
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
    assert "--bot-config " + str(runtime / "resolved" / "config.bot.json") in service
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
    assert profile["wechat_clawbot"]["bot_config_path"] == str(runtime / "resolved" / "config.bot.json")
    assert profile["wechat_clawbot"]["lock_path"] == str(runtime / "locks" / "wechat-clawbot.lock")
    assert "systemctl enable --now options-monitor-wechat-clawbot.service" in bundle["commands"]["enable"]

def test_render_wechat_clawbot_requires_explicit_allowed_senders(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="wechat_clawbot_allowed_senders"):
        _render_bundle(repo, tmp_path / "runtime", markets=["us"], include_wechat_clawbot=True)

def test_render_wechat_clawbot_can_use_yaml_inbound_allowlist(tmp_path: Path) -> None:
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

    bundle = _render_bundle(repo, runtime, markets=["us"], config_yaml=config_yaml, include_wechat_clawbot=True)

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-wechat-clawbot.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "--label ops" in service
    assert "--bot-config " + str(runtime / "resolved" / "config.bot.json") in service
    assert "--allowed-senders" not in service
    assert profile["wechat_clawbot"]["label"] == "ops"
    assert profile["wechat_clawbot"]["allowed_senders_configured"] is True
    assert profile["wechat_clawbot"]["allowed_senders_source"] == "config_yaml"
    assert "allowed_senders" not in profile["wechat_clawbot"]
    assert profile["wechat_clawbot"]["bot_config_path"] == str(runtime / "resolved" / "config.bot.json")


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("source", ["single-market", "explicit-key", "yaml"])
def test_service_drift_wechat_profile_uses_canonical_renderer(tmp_path, target, source):
    from src.application.service_drift import _expected_bundle_from_profile

    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    kwargs = dict(
        target=target, accounts=["fixture"], markets=["us"],
        include_wechat_clawbot=True, wechat_clawbot_allowed_senders="wechat:fixture",
        use_default_deploy_user=False, deploy_home=tmp_path / "home",
    )
    if source != "single-market":
        kwargs.update(markets=["us", "hk"], wechat_clawbot_config_key="hk")
    if source == "yaml":
        config_yaml = runtime / "config.yaml"
        config_yaml.write_text(
            "accounts: {}\nmarkets: {}\ninbound:\n  wechat_clawbot:\n"
            "    allowed_senders: wechat:fixture\n", encoding="utf-8",
        )
        kwargs.update(config_yaml=config_yaml, wechat_clawbot_allowed_senders=None)
    bundle = _render_bundle(repo, runtime, **kwargs)
    profile = json.loads(next(item["content"] for item in bundle["files"] if item["kind"] == "service_profile"))
    if source == "single-market":
        profile["wechat_clawbot"].pop("config_key")
    if source == "yaml":
        # YAML content, rather than historical summary flags, supplies the allowlist.
        profile["wechat_clawbot"].pop("allowed_senders_configured")
        profile["wechat_clawbot"].pop("allowed_senders_source")
    rebuilt = _expected_bundle_from_profile(profile, provider=target, repo_root=repo, runtime_root=runtime)
    assert {item["relative_path"]: item["content"] for item in rebuilt["files"]} == {
        item["relative_path"]: item["content"] for item in bundle["files"]
    }


def test_render_systemd_auto_upgrade_preserves_symlink_repo_root(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    release_dir = releases / "1.2.71"
    release_dir.mkdir(parents=True)
    current = tmp_path / "options-monitor"
    current.symlink_to(release_dir, target_is_directory=True)
    runtime = tmp_path / "runtime"

    bundle = _render_bundle(current, runtime, markets=["hk"], include_auto_upgrade=True)

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
    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = _render_bundle(
        repo, tmp_path / "runtime", accounts=["lx"], markets=["us"], deploy_user="ops", deploy_home="/srv/options-home",
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
    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = _render_bundle(repo, tmp_path / "runtime", accounts=["lx"], markets=["us"], deploy_user="ops", include_feishu_ws=True)

    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])

    assert profile["restart"]["command_prefix"] == ["sudo", "-n", "systemctl"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in profile["restart"]["sudoers"]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service" in profile["restart"]["sudoers"]

@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("prefix,suffix", [("", "after"), ("before", "after"), ("before", "")])
@pytest.mark.parametrize("field", ["argument", "environment_name", "environment_value"])
def test_systemd_line_boundary_helpers(newline, prefix, suffix, field):
    from src.application.service_deploy import _systemd_environment_assignment, _systemd_quote_arg

    value = prefix + newline + suffix
    with pytest.raises(ValueError) as error:
        if field == "argument":
            _systemd_quote_arg(value)
        elif field == "environment_name":
            _systemd_environment_assignment(value, "fixture")
        else:
            _systemd_environment_assignment("FIXTURE", value)
    assert str(error.value) == "systemd unit values must not contain CR or LF"


@pytest.mark.parametrize("value,argument,environment", [
    ("", '""', 'Environment="FIXTURE="'),
    ("/tmp/plain", "/tmp/plain", 'Environment="FIXTURE=/tmp/plain"'),
    ("/tmp/with space", '"/tmp/with space"', 'Environment="FIXTURE=/tmp/with space"'),
    ('/tmp/with"quote', '"/tmp/with\\"quote"', 'Environment="FIXTURE=/tmp/with\\"quote"'),
    ("/tmp/with'quote", '"/tmp/with\'quote"', 'Environment="FIXTURE=/tmp/with\'quote"'),
    (r"/tmp/with\slash", '"/tmp/with\\\\slash"', 'Environment="FIXTURE=/tmp/with\\\\slash"'),
    ("/tmp/with;semicolon", '"/tmp/with;semicolon"', 'Environment="FIXTURE=/tmp/with;semicolon"'),
    (Path("/tmp/路径"), "/tmp/路径", 'Environment="FIXTURE=/tmp/路径"'),
])
def test_service_render_single_line_compatibility_helpers(value, argument, environment):
    from src.application.service_deploy import _systemd_environment_assignment, _systemd_quote_arg

    assert _systemd_quote_arg(value) == argument
    assert _systemd_environment_assignment("FIXTURE", value) == environment


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("field", [
    "repo_root", "runtime_root", "env_file", "deploy_home", "deploy_user",
    "opend_root", "opend_executable",
])
def test_service_render_line_boundary_rejects_effective_values(tmp_path, newline, field):
    from src.application.service_deploy import render_service_bundle

    options = dict(target="systemd", repo_root=tmp_path / "repo", runtime_root=tmp_path / "runtime",
                   accounts=["fixture"], markets=["us"], use_default_deploy_user=False,
                   deploy_home=tmp_path / "home")
    if field.startswith("opend_"):
        options.update(include_opend=True, opend_root=tmp_path / "opend")
    options[field] = str(tmp_path / "before") + newline + "X-OM-Renderer-Probe=present"
    message_field = "deploy user" if field == "deploy_user" else "unit values"
    with pytest.raises(ValueError) as error:
        render_service_bundle(**options)
    assert str(error.value) == f"systemd {message_field} must not contain CR or LF"
    assert list(tmp_path.iterdir()) == []


def test_service_render_line_boundary_keeps_credential_user_policy_scoped(tmp_path):
    options = dict(accounts=["fixture"], markets=["us"], deploy_user="fixture@realm",
                   deploy_home=tmp_path / "home", use_default_deploy_user=False)
    bundle = _render_bundle(tmp_path / "repo", tmp_path / "runtime", **options)
    tick = next(item["content"] for item in bundle["files"]
                if item["relative_path"] == "systemd/options-monitor-tick-us.service")
    assert "User=fixture@realm\n" in tick
    with pytest.raises(ValueError, match="runtime credential deploy user is not a safe system account name"):
        _render_bundle(tmp_path / "repo", tmp_path / "runtime", **options,
                       include_secret_credentials=True, secret_credential_delivery="runtime-files",
                       secret_credential_store_root=tmp_path / "credstore")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
def test_service_render_line_boundary_preserves_existing_normalization(tmp_path, newline):
    runtime = tmp_path / "runtime"
    bundle = _render_bundle(tmp_path / "repo", newline + str(runtime) + newline,
                            accounts=["fixture"], markets=["us"], use_default_deploy_user=False)
    assert bundle["runtime_root"] == str(runtime)

    runtime_with_newline = str(runtime) + newline + "tail"
    launchd = _render_bundle(tmp_path / "repo", runtime_with_newline, target="launchd",
                            accounts=["fixture"], markets=["us"], use_default_deploy_user=False)
    assert launchd["runtime_root"] == runtime_with_newline
    tick = next(item for item in launchd["files"]
                if item["relative_path"] == "launchd/com.options-monitor.tick-us.plist")
    # Existing XML parsing normalizes CR and CRLF; systemd validation must not change launchd.
    parsed = plistlib.loads(tick["content"].encode())
    assert parsed["EnvironmentVariables"]["OM_RUNTIME_ROOT"] == str(runtime) + "\n" + "tail"


def test_render_systemd_bundle_quotes_paths_with_spaces(tmp_path: Path) -> None:
    repo = tmp_path / "repo with space"
    runtime = tmp_path / "runtime with space"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"])

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]

    assert f'WorkingDirectory="{repo}"' in tick
    assert f'Environment="OM_RUNTIME_ROOT={runtime}"' in tick
    assert f'ExecStart="{repo / "om"}" run tick-cron' in tick
    assert f'--lock-path "{runtime / "locks" / "tick-us.lock"}"' in tick

def test_render_systemd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "etc" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, accounts=["lx"], markets=["us"], env_file=env_file)

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert f"EnvironmentFile={env_file}" in tick
    assert f"EnvironmentFile={env_file}" in intake
    assert bundle["env_file"] == str(env_file)
    assert profile["env_file"] == str(env_file)

def test_render_launchd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "Library" / "Application Support" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, target="launchd", accounts=["lx"], markets=["us"], env_file=env_file)

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
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = _render_bundle(
        repo, runtime, target="launchd", markets=["us"], include_feishu_ws=True, include_wechat_clawbot=True,
        wechat_clawbot_label="ops", wechat_clawbot_allowed_senders="wechat:user_1",
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
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, target="launchd", accounts=["lx", "sy"], markets=["us", "hk"])

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
    assert "<string>com.options-monitor.auto-close-us</string>" in auto_close_us
    import plistlib
    for market, raw in (("hk", auto_close_hk), ("us", auto_close_us)):
        auto = plistlib.loads(raw.encode())
        assert auto["StartCalendarInterval"] == {}
        assert auto["RunAtLoad"] is True
        assert "KeepAlive" not in auto
        assert "TZ" not in auto["EnvironmentVariables"]
        assert "AbandonProcessGroup" not in auto
        assert auto["ProgramArguments"][:3] == [str(repo / "scripts/auto_close_schedule.sh"), "--market", market]
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
    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = _render_bundle(repo, runtime, target="launchd", markets=["us"], include_auto_upgrade=True)

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
        installed_path = Path(command[-1])
        if command[:4] == ["sudo", "-n", "install", "-d"]:
            assert installed_path == target.parent
            installed_path.mkdir(parents=True, exist_ok=True)
        elif command[:3] == ["sudo", "-n", "install"] and "/dev/stdin" in command:
            assert installed_path == target
            installed_path.write_text(str(kwargs.get("input") or ""), encoding="utf-8")
        else:
            raise AssertionError(f"unexpected install command: {command}")
        os.chown(
            installed_path,
            int(command[command.index("-o") + 1]),
            int(command[command.index("-g") + 1]),
        )
        installed_path.chmod(int(command[command.index("-m") + 1], 8))
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
    assert (target.stat().st_uid, target.stat().st_gid) == (expected_uid, expected_gid)
    assert (target.parent.stat().st_uid, target.parent.stat().st_gid) == (expected_uid, expected_gid)
    assert target.parent.stat().st_mode & 0o777 == 0o755
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


@pytest.mark.parametrize("wrong_owner", ["uid", "gid"])
def test_service_drift_privileged_executable_rejects_wrong_ownership(
    monkeypatch,
    tmp_path: Path,
    wrong_owner: str,
) -> None:
    import src.application.service_drift as service_drift_module
    from src.application.service_drift import _write_text_with_sudo_fallback

    target = tmp_path / "protected" / "credential-helper"
    target.parent.mkdir(parents=True)
    target.touch()
    initial_owner = target.stat()
    expected_uid = initial_owner.st_uid + (1 if wrong_owner == "uid" else 0)
    expected_gid = initial_owner.st_gid + (1 if wrong_owner == "gid" else 0)
    monkeypatch.setattr(service_drift_module.os, "geteuid", lambda: expected_uid + 1)

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:4] == ["sudo", "-n", "install", "-d"]:
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target.write_text(str(kwargs.get("input") or ""), encoding="utf-8")
            target.chmod(0o755)
        # Simulate a successful command that did not apply its requested owner.
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

    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o755
    observed = target.stat().st_uid if wrong_owner == "uid" else target.stat().st_gid
    assert observed != (expected_uid if wrong_owner == "uid" else expected_gid)
    assert out["ok"] is False
    assert out["sudo_fallback"] is True
    assert out["returncode"] == 1
    assert "ownership verification failed" in out["error"]


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


def test_post_upgrade_opend_login_check_is_read_only_and_repeatable(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    repo = tmp_path / "repo"
    repo.mkdir()
    service = "options-monitor-opend-lx.service"
    profile = {"service_provider": "systemd", "services": [{"name": service}],
               "opend": {"services": [{"service_name": service, "host": "127.0.0.1", "port": 11111}]}}
    probes: list[list[str]] = []
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if "src.infrastructure.opend_watchdog" in command:
            probes.append(list(command))
            assert kwargs["timeout"] > float(command[command.index("--retry-timeout-sec") + 1])
            assert "--ensure" not in command and "--retry-enabled" in command
            status = ({"ok": False, "error_code": "OPEND_LOGIN_INVALID"} if len(probes) == 1
                      else {"ok": True})
            return subprocess.CompletedProcess(command, 2 if len(probes) == 1 else 0,
                                               stdout=json.dumps(status), stderr="")
        assert command[:2] == ["systemctl", "is-active"] or command[:2] == ["systemctl", "is-enabled"]
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    first = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[],
                                         monotonic_fn=lambda: 0.0, sleep_fn=_sleep)
    assert first["ok"] is True
    for _ in range(2):
        healthy = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[],
                                               monotonic_fn=lambda: 0.0, sleep_fn=_sleep)
        assert healthy["ok"] is True
    assert len(probes) == 4
    assert len({tuple(p) for p in probes}) == 1
    assert sleeps == [5]


def test_post_upgrade_opend_login_check_requires_profile_endpoint(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    service = "options-monitor-opend.service"
    profile = {"service_provider": "systemd", "services": [{"name": service}],
               "opend": {"services": [{"service_name": service}]}}
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=tmp_path, run_cmd=_run_cmd, operations=[])
    assert out["ok"] is False
    assert out["failed_checks"][0]["reason_code"] == "OPEND_ENDPOINT_MISSING"
    assert all("src.infrastructure.opend_watchdog" not in call for call in calls)

def test_post_upgrade_service_health_reports_precise_wechat_check_failure(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    repo = tmp_path / "repo with space 中文"
    repo.mkdir()
    state_dir = tmp_path / "wechat-state"
    audit_db = tmp_path / "inbound.sqlite3"
    bot_config = tmp_path / "config.bot.json"
    config_path = tmp_path / "config.us.json"
    profile = {
        "service_provider": "systemd",
        "config_paths": {"us": str(config_path)},
        "wechat_clawbot": {
            "enabled": True,
            "label": "ops",
            "state_dir": str(state_dir),
            "config_key": "us",
            "bot_config_path": str(bot_config),
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
        + shlex.join(
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
                "--bot-config",
                str(bot_config),
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
        (path / "src/application/bot").mkdir(parents=True, exist_ok=True)
        (path / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
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
        runtime_root=tmp_path / "runtime",
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
    assert _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[]) == restarted
    assert calls[3:] == calls[:3]

@pytest.mark.parametrize(
    ("profile_extras", "expected_command", "expected_source"),
    (
        (
            {},
            ["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"],
            "non_root_sudo_fallback",
        ),
        (
            {"restart": {"requires_sudo": False}},
            ["systemctl", "restart", "options-monitor-trade-intake.service"],
            "profile.requires_sudo_false",
        ),
    ),
    ids=("legacy-profile-falls-back-to-sudo", "explicit-non-sudo-profile"),
)
def test_service_upgrade_restart_resolves_sudo_from_profile(
    monkeypatch,
    tmp_path: Path,
    profile_extras: dict,
    expected_command: list[str],
    expected_source: str,
) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                **profile_extras,
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
    assert calls == [expected_command]
    assert operations[0]["command_source"] == expected_source

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
    assert ["git", "clone", "--mirror", "--origin", "origin", "https://example.invalid/repo.git", str(cache_repo)] not in calls

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
    assert out["bot_runtime"] == {"ok": True, "runtime": "python"}
    assert [str(target / ".venv/bin/python"), "-c",
            "import src.application.bot.host; import src.application.bot.runtime"] in calls
    assert not any(command[0] in {"node", "npm"} for command in calls)
    assert operations and all("duration_seconds" in item for item in operations)

def test_service_upgrade_rejects_legacy_runtime_before_dependency_install(tmp_path: Path) -> None:
    from src.application.service_upgrade import ServiceTransitionError, _ensure_release_runtime

    target = tmp_path / "legacy"
    _write_runtime_target_with_server_deps(target)
    (target / "src/application/bot/runtime.py").unlink()
    operations: list[dict] = []
    with pytest.raises(ServiceTransitionError) as caught:
        _ensure_release_runtime(
            target_dir=target,
            run_cmd=lambda *_args, **_kwargs: pytest.fail("unsupported runtime installed dependencies"),
            operations=operations,
        )
    assert caught.value.status == "unsupported_runtime"
    assert operations == []
    assert not (target / ".venv").exists()

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


def test_service_upgrade_rebuilds_cache_with_relocated_pip_shebang(monkeypatch, tmp_path: Path) -> None:
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
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    first_out = _ensure_release_runtime(
        target_dir=first, cache_root=cache_root, run_cmd=_run_cmd, operations=[]
    )
    shared = Path(first_out["shared_venv_path"])
    pip = shared / "bin" / "pip"
    pip.write_text("#!/removed/tmp/venv/bin/python3\n", encoding="utf-8")
    pip.chmod(0o755)
    call_count = len(calls)

    second_out = _ensure_release_runtime(
        target_dir=second, cache_root=cache_root, run_cmd=_run_cmd, operations=[]
    )

    assert second_out["venv_reused"] is False
    assert any(command[:3] == [CURRENT_PYTHON, "-m", "venv"] for command in calls[call_count:])


def test_service_upgrade_relocates_generated_venv_entrypoints(tmp_path: Path) -> None:
    from src.application.service_upgrade import _relocate_venv_scripts

    built_at = tmp_path / ".hash.tmp.1"
    final = tmp_path / "hash"
    script = built_at / "bin" / "pip"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!{built_at}/bin/python3\nprint('pip')\n", encoding="utf-8")
    built_at.rename(final)

    _relocate_venv_scripts(venv_dir=final, built_at=built_at)

    assert (final / "bin" / "pip").read_text(encoding="utf-8").splitlines()[0] == (
        f"#!{final}/bin/python3"
    )

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

def test_service_upgrade_bot_prepare_retries_after_import_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    attempts = 0

    def _run_cmd(command, **_kwargs):
        nonlocal attempts
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        if command[1:] == ["-c", "import src.application.bot.host; import src.application.bot.runtime"]:
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="Bot import failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    operations: list[dict] = []
    with pytest.raises(RuntimePrepareError, match="import src.application.bot.host"):
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=operations)
    assert operations[-1]["returncode"] == 1
    assert operations[-1]["stderr"] == "Bot import failed\n"
    assert operations[-1]["command"] == [str(target / ".venv/bin/python"), "-c",
                                          "import src.application.bot.host; import src.application.bot.runtime"]
    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    assert attempts == 2
    assert out["bot_runtime"] == {"ok": True, "runtime": "python"}

def test_service_upgrade_bot_smoke_timeout_keeps_structured_prepare_evidence(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):
        if command[:3] == [CURRENT_PYTHON, "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        if command[1:] == ["-c", "import src.application.bot.host; import src.application.bot.runtime"]:
            raise subprocess.TimeoutExpired(command, timeout=30)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    with pytest.raises(RuntimePrepareError) as caught:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=operations)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert operations[-1]["timed_out"] is True
    assert operations[-1]["timeout_seconds"] == 30
    assert operations[-1]["ok"] is False
    assert caught.value.runtime_prepare["duration_seconds"] >= 0
    assert not Path(caught.value.runtime_prepare["shared_venv_path"]).exists()

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


def test_repository_logrotate_template_caps_runtime_logs_without_touching_audits() -> None:
    template = Path(__file__).resolve().parents[2] / "deploy/logrotate/options-monitor.conf.in"
    content = template.read_text(encoding="utf-8")
    assert "@RUNTIME_ROOT@/logs/*.log" in content
    assert "maxsize 16M" in content
    assert "hourly" in content
    assert "create 0600 @DEPLOY_USER@ @DEPLOY_USER@" in content
    assert "audit_events" not in content
    assert "copytruncate" not in content


def test_upgrade_reconcile_delegates_to_the_new_release_cli(tmp_path: Path) -> None:
    """The post-switch reconcile must run the release that is now current.

    The upgrade process is the *previous* release's `om`, so reconciling in
    process renders the previous release's bundle and the units the new release
    declares are never installed: the 3.7.0 -> 3.7.1 upgrade (2026-09-24) reported
    26 expected units and wrote nothing where 3.7.1's renderer expects 29. This
    pins the delegation, the flags that carry the activation policy across, and
    the shape of the result it reads back.
    """
    from src.application.service_upgrade import _reconcile_services_from_current_release

    target_dir = tmp_path / "releases" / "1.0.1"
    runtime = tmp_path / "runtime"
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "tool_name": "service.drift",
            "ok": True,
            "data": {"summary": {"status": "ok", "ok": True}, "written_units": ["options-monitor-tick-us.timer"]},
        }), stderr="")

    reconcile = _reconcile_services_from_current_release(
        repo_link=tmp_path / "current",
        target_dir=target_dir,
        runtime=runtime,
        activation_policy="preserve-existing",
        run_cmd=_run_cmd,
    )

    assert calls == [[
        str(target_dir / "om"), "service", "drift",
        "--repo-root", str(tmp_path / "current"),
        "--runtime-root", str(runtime),
        "--profile-path", str(runtime / "service.profile.json"),
        "--confirm", "--preserve-activation-state",
    ]]
    assert reconcile == {"summary": {"status": "ok", "ok": True}, "written_units": ["options-monitor-tick-us.timer"]}


def test_upgrade_reconcile_keeps_ensure_active_without_preserving(tmp_path: Path) -> None:
    from src.application.service_upgrade import _reconcile_services_from_current_release

    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "tool_name": "service.drift", "ok": True,
            "data": {"summary": {"status": "ok", "ok": True}},
        }), stderr="")

    _reconcile_services_from_current_release(
        repo_link=tmp_path / "current",
        target_dir=tmp_path / "releases" / "1.0.1",
        runtime=tmp_path / "runtime",
        activation_policy="ensure-active",
        run_cmd=_run_cmd,
    )

    assert "--preserve-activation-state" not in calls[0]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected_reason"),
    [
        (1, "", "drift: no such unit", "drift: no such unit"),
        (2, json.dumps({
            "tool_name": "om", "ok": False,
            "error": {
                "code": "DEPENDENCY_MISSING",
                "message": "could not capture timer activation state",
                "details": {"remediation": ["systemctl is-enabled options-monitor-tick-us.timer"]},
            },
        }), "target release rejected --preserve-activation-state", "systemctl is-enabled"),
        (0, json.dumps({
            "tool_name": "om", "ok": True, "data": {"summary": {"status": "ok", "ok": True}},
        }), "", "unreadable child response"),
        (2, json.dumps({
            "tool_name": "service.drift", "ok": True,
            "data": {"summary": {"status": "ok", "ok": True}},
        }), "child exited with status 2", "child exited with status 2"),
    ],
)
def test_upgrade_reconcile_fails_closed_on_bad_child_result(
    tmp_path: Path, returncode: int, stdout: str, stderr: str, expected_reason: str,
) -> None:
    """A reconcile that cannot be read is a failed upgrade, not a silent one.

    `_service_reconcile_failed` treats an empty result as success, so the helper
    has to refuse to return one: an unreadable reconcile would otherwise let the
    upgrade report success with the new release's units missing.
    """
    from src.application.service_upgrade import (
        ServiceTransitionError,
        _reconcile_services_from_current_release,
    )

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    with pytest.raises(ServiceTransitionError) as excinfo:
        _reconcile_services_from_current_release(
            repo_link=tmp_path / "current",
            target_dir=tmp_path / "releases" / "1.0.1",
            runtime=tmp_path / "runtime",
            activation_policy="ensure-active",
            run_cmd=_run_cmd,
        )

    assert excinfo.value.status == "upgraded_service_reconcile_failed"
    assert expected_reason in str(excinfo.value) or any(
        expected_reason in item for item in excinfo.value.remediation
    )
    if returncode:
        assert any(f"returncode: {returncode}" in item for item in excinfo.value.remediation)


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("timeout_seconds", [1, 600, 1800])
def test_service_timeout_profile_roundtrip(tmp_path, target, timeout_seconds):
    from src.application.service_drift import _expected_bundle_from_profile

    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    bundle = _render_bundle(repo, runtime, target=target, accounts=["lx"],
                           markets=["us", "hk"], timeout_seconds=timeout_seconds,
                           use_default_deploy_user=False)
    profile = json.loads(next(item["content"] for item in bundle["files"]
                              if item["kind"] == "service_profile"))
    assert profile["timeout_seconds"] == timeout_seconds
    rebuilt = _expected_bundle_from_profile(profile, provider=target,
                                            repo_root=repo, runtime_root=runtime)
    assert {item["relative_path"]: item["content"] for item in rebuilt["files"]} == {
        item["relative_path"]: item["content"] for item in bundle["files"]
    }


@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_service_timeout_legacy_profile_uses_default(tmp_path, target):
    from src.application.service_deploy import DEFAULT_TIMEOUT_SECONDS
    from src.application.service_drift import _expected_bundle_from_profile

    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    bundle = _render_bundle(repo, runtime, target=target, accounts=["lx"],
                           markets=["us", "hk"], use_default_deploy_user=False)
    profile = json.loads(next(item["content"] for item in bundle["files"]
                              if item["kind"] == "service_profile"))
    profile.pop("timeout_seconds", None)
    rebuilt = _expected_bundle_from_profile(profile, provider=target,
                                            repo_root=repo, runtime_root=runtime)
    assert {item["relative_path"]: item["content"] for item in rebuilt["files"]
            if item["kind"] != "service_profile"} == {
        item["relative_path"]: item["content"] for item in bundle["files"]
        if item["kind"] != "service_profile"
    }
    restored = json.loads(next(item["content"] for item in rebuilt["files"]
                               if item["kind"] == "service_profile"))
    assert restored["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("timeout_seconds", [None, True, False, "1800", 1800.5, 0, -1, [], {}])
def test_service_render_rejects_invalid_timeout(tmp_path, target, timeout_seconds):
    with pytest.raises(ValueError, match="timeout_seconds must be a positive integer"):
        _render_bundle(tmp_path / "repo", tmp_path / "runtime", target=target,
                       accounts=["lx"], markets=["us"], timeout_seconds=timeout_seconds)



def test_python_bot_runtime_import_preserves_legacy_store(tmp_path: Path) -> None:
    from src.application import service_upgrade as module

    release = tmp_path / "release"
    _write_upgrade_release_skeleton(release, "3.5.2")
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(CURRENT_PYTHON)
    legacy_db = release / "output_shared/state/pi_sessions.sqlite3"
    legacy_db.parent.mkdir(parents=True)
    legacy_db.write_bytes(b"legacy store must never be opened")
    before = legacy_db.read_bytes()
    operations: list[dict] = []
    out = module._ensure_bot_runtime(release, subprocess.run, operations)
    assert out == {"ok": True, "runtime": "python"}
    assert module._runtime_readiness(release_dir=release) == out
    assert len(operations) == 1 and operations[0]["ok"] is True
    assert operations[0]["command"] == [str(python), "-c", "import src.application.bot.host; import src.application.bot.runtime"]
    assert legacy_db.read_bytes() == before


def test_failed_compensation_never_restores_or_starts_incompatible_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    previous = tmp_path / "releases/3.5.1"
    target = tmp_path / "releases/3.5.2"
    _write_upgrade_release_skeleton(previous, "3.5.1")
    _write_upgrade_release_skeleton(target, "3.5.2")
    current = tmp_path / "current"
    current.symlink_to(target, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    (previous / "src/application/bot/runtime.py").unlink()
    monkeypatch.setattr(module, "_switch_current_symlink", lambda **_kwargs: pytest.fail("compensation restored old runtime"))
    monkeypatch.setattr(module, "service_drift", lambda **_kwargs: pytest.fail("compensation reconciled old services"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", lambda **_kwargs: pytest.fail("compensation started old service"))

    out = module._compensate_service_transition(  # noqa: SLF001 - exact failure boundary
        repo_link=current,
        previous_dir=previous,
        runtime_root=runtime,
        previous_profile={"service_provider": "systemd"},
        config_commit={},
        restart_services=True,
        activation_policy="preserve-existing",
        preserved_activation_states={},
        run_cmd=lambda *_args, **_kwargs: pytest.fail("unexpected service command"),
        operations=[],
    )

    assert out["status"] == "unsupported_runtime"
    assert out["symlink_restored"] is False
    assert out["restarted_services"] == []
    assert out["runtime_readiness"] == {}
    assert out["config_restore"]["status"] == "skipped"
    assert current.resolve() == target
