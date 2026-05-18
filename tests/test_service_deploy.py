from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert 'Environment="OM_RUNTIME_ROOT=' + str(runtime) + '"' in tick
    assert str(repo / "om") + " run tick-cron --market us" in tick
    assert "--lock-path " + str(runtime / "locks" / "tick-us.lock") in tick
    assert str(repo / "om") + " run trade-intake" in intake
    assert "Restart=always" in intake
    assert profile["service_provider"] == "systemd"
    assert profile["runtime_root"] == str(runtime)
    assert {"name": "options-monitor-tick-us.service"} in profile["services"]
    assert {"name": "options-monitor-tick-us.timer"} in profile["services"]


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


def test_render_service_bundle_rejects_env_file_for_launchd(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    try:
        render_service_bundle(
            target="launchd",
            repo_root=repo,
            env_file="/etc/options-monitor/options-monitor.env",
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "--env-file is only supported for systemd" in str(exc)


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
        markets=["hk"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["launchd/com.options-monitor.tick-hk.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<key>Label</key>" in tick
    assert "<string>com.options-monitor.tick-hk</string>" in tick
    assert str(runtime / "logs" / "com.options-monitor.tick-hk.out.log") in tick
    assert "--market" in tick
    assert "hk" in tick
    assert profile["service_provider"] == "launchd"
    assert {"name": "com.options-monitor.tick-hk"} in profile["services"]


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


def test_runtime_status_loads_service_profile_paths(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool

    cfg_path = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_config)},
                "notifications": {"provider": "openclaw", "target": "route"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "accounts": ["lx"],
                "markets": ["us"],
                "config_paths": {"us": str(cfg_path)},
                "paths": {
                    "report_dir": str(tmp_path / "output" / "reports"),
                    "state_dir": str(tmp_path / "output" / "state"),
                    "shared_state_dir": str(tmp_path / "output_shared" / "state"),
                    "accounts_root": str(tmp_path / "output_accounts"),
                    "runs_root": str(tmp_path / "output_runs"),
                },
                "services": [{"name": "options-monitor-trade-intake.service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = execute_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["lx"]
    assert out["data"]["service_profile"]["loaded"] is True
    assert out["data"]["service_profile"]["provider"] == "systemd"
    assert out["data"]["service_profile"]["service_count"] == 1


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
        "--env-file",
        str(tmp_path / "options-monitor.env"),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["summary"]["service_provider"] == "systemd"
    assert payload["data"]["env_file"] == str(tmp_path / "options-monitor.env")
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
        "--output-dir",
        str(output_dir),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["files"][0].get("content") is None
    assert "ExecStart=" in (output_dir / "systemd" / "options-monitor-tick-us.service").read_text(encoding="utf-8")


def test_cli_run_trade_intake_delegates_to_application(monkeypatch) -> None:
    import src.application.trades.auto_intake as auto_intake
    from src.interfaces.cli.main import main

    calls: list[list[str]] = []
    monkeypatch.setattr(auto_intake, "main", lambda argv: calls.append(list(argv)) or 0)

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
    assert calls == [["--config", "config.us.json", "--mode", "apply", "--host", "127.0.0.1", "--port", "11111", "--once"]]
