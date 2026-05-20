from __future__ import annotations

import json
from pathlib import Path

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
    assert "setup init --market us" in checks["config.us"]["hint"]
    assert any(step.startswith("./om setup init --market us") for step in out["next_steps"])
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


def test_cli_setup_init_subcommand_delegates_to_runtime_init(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    def _init_runtime(**kwargs):
        return kwargs

    monkeypatch.setattr(cli, "init_runtime", _init_runtime)

    rc = cli.main([
        "setup",
        "init",
        "--market",
        "us",
        "--futu-acc-id",
        "123456",
        "--account",
        "lx",
        "--config-path",
        str(tmp_path / "config.us.json"),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "setup.init"
    assert payload["data"]["market"] == "us"
    assert payload["data"]["account_label"] == "lx"
