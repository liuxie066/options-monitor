from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.setup import run_setup_check


def test_setup_check_is_read_only_and_reports_missing_config(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert isinstance(out["summary"]["ok"], bool)
    assert checks["platform"]["value"]["service_target"] in {"systemd", "launchd", "manual"}
    assert out["platform_profile"]["default_env_file"]
    assert checks["install.repo"]["status"] == "ok"
    assert checks["upgrade.uv"]["status"] in {"ok", "info", "warn"}
    assert checks["config.us"]["status"] == "warn"
    assert "config init" in checks["config.us"]["hint"]
    assert any(step.startswith("./om config init") for step in out["next_steps"])
    assert not (tmp_path / "config.us.json").exists()


def test_setup_check_warns_when_uv_forced_but_missing(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setattr("src.application.setup.check.shutil.which", lambda _name: None)
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "uv")

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["upgrade.uv"]["status"] == "warn"
    assert checks["upgrade.uv"]["value"]["installer_mode"] == "uv"
    assert "Install uv" in checks["upgrade.uv"]["hint"]


def test_setup_check_reports_yfinance_as_runtime_dependency(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")

    def _find_spec(name: str):
        if name == "yfinance":
            return None
        return object()

    monkeypatch.setattr("src.application.setup.check.importlib.util.find_spec", _find_spec)

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["install.dependencies"]["status"] == "error"
    assert checks["install.dependencies"]["value"]["missing"] == ["yfinance"]
    assert checks["install.dependencies"]["value"]["checked"] == ["pandas", "futu", "yfinance"]


def test_setup_check_reports_stale_runtime_config_and_schedule_readiness(monkeypatch, tmp_path: Path) -> None:
    from src.application.config_defaults import DEFAULT_CONFIG_REF, default_config_sha256

    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    source = tmp_path / "config.yaml"
    source.write_text("accounts: {}\n", encoding="utf-8")
    runtime_config = tmp_path / "config.us.json"
    runtime_config.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                    "sources": [
                        {
                            "role": "system",
                            "loaded": True,
                            "inline": True,
                            "ref": DEFAULT_CONFIG_REF,
                            "sha256": default_config_sha256(),
                        },
                        {
                            "role": "market_user",
                            "loaded": True,
                            "inline": False,
                            "path": str(source),
                            "sha256": "stale-sha",
                        },
                    ],
                },
                "schedule": {"timezone": "America/New_York"},
                "symbols": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["config.us"]["status"] == "error"
    assert checks["config.us"]["value"]["identity"]["ok"] is True
    assert checks["config.us"]["value"]["schedule"]["ok"] is True
    assert checks["config.us"]["value"]["freshness"]["ok"] is False
    assert out["summary"]["ok"] is False


def test_cli_setup_check_outputs_json(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    def _check(**kwargs):
        return {
            "summary": {"ok": True, "error_count": 0, "warning_count": 0},
            "repo_root": str(kwargs["repo_root"]),
            "markets": kwargs["markets"],
            "checks": [],
            "next_steps": [],
        }

    monkeypatch.setattr(cli, "run_setup_check", _check)

    rc = cli.main(["setup", "check", "--market", "us", "--no-local-env-file"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "setup.check"
    assert payload["ok"] is True
    assert payload["data"]["markets"] == ["us"]


def test_cli_setup_init_subcommand_is_removed(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main([
            "setup",
            "init",
            "--market",
            "us",
            "--futu-acc-id",
            "123456",
            "--account",
            "lx",
        ])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
