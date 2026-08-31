from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.service_deploy_test_support import (
    _write_systemd_units_from_bundle,
)

def test_service_render_cli_omits_retired_strategy_lab_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.interfaces.cli.main import parse_args

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["service", "render", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    retired_flags = (
        "--include-strategy-lab-" + "recorder",
        "--strategy-lab-" + "recorder-account",
        "--include-strategy-lab-" + "top1",
        "--strategy-lab-" + "top1-account",
    )
    assert all(flag not in help_text for flag in retired_flags)

    for flag in retired_flags:
        with pytest.raises(SystemExit):
            parse_args([
                "service",
                "render",
                "--target",
                "systemd",
                "--config-yaml",
                "/tmp/config.yaml",
                flag,
            ])
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
