from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.application.setup import run_setup_check


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _prepare_pi_setup_root(tmp_path: Path, *, context_window_tokens: int = 24_000) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "agent-runtime" / "node_modules").mkdir(parents=True)
    (root / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (root / "config.assistant.json").write_text(
        json.dumps(
            {
                "assistant": {
                    "enabled": True,
                    "copilot": {"enabled": True, "toolsets": {}},
                    "llm": {
                        "provider": "ollama",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "local-test",
                        "context_window_tokens": context_window_tokens,
                        "max_output_tokens": 2048,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    state = runtime / "output_shared" / "state"
    state.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    node = _write_executable(
        fake_bin / "node",
        "#!/usr/bin/env bash\nif [[ \"${1:-}\" == \"--version\" ]]; then echo v22.19.0; fi\n",
    )
    npm = _write_executable(fake_bin / "npm", "#!/usr/bin/env bash\nexit 0\n")
    return root, node, npm


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


def test_setup_check_no_longer_requires_yfinance(monkeypatch, tmp_path: Path) -> None:
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

    assert checks["install.dependencies"]["status"] == "ok"
    assert checks["install.dependencies"]["value"].get("missing", []) == []
    assert checks["install.dependencies"]["value"]["checked"] == ["pandas", "futu"]


def test_setup_check_reports_earnings_calendar_sdk_capability(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.application.setup.check.inspect_futu_sdk_earnings_calendar_capability",
        lambda: {
            "supported": False,
            "installed": True,
            "installed_version": "10.8.6808",
            "minimum_version": "10.9.6908",
            "method_available": False,
            "reason_code": "futu_api_version_too_old",
        },
    )

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    capability = checks["install.futu_earnings_calendar"]
    assert capability["status"] == "error"
    assert capability["value"]["reason_code"] == "futu_api_version_too_old"
    assert "10.9.6908" in capability["hint"]


def test_setup_check_reports_pi_runtime_context_and_session_without_writes(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(
        "src.application.setup.check.shutil.which",
        lambda name: {"node": str(node), "npm": str(npm), "uv": None}.get(name),
    )
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["install.node"]["status"] == "ok"
    assert checks["install.node"]["value"]["version"] == "v22.19.0"
    assert checks["install.npm"]["status"] == "ok"
    assert checks["install.pi_packages"]["status"] == "ok"
    assert checks["copilot.model_context"]["status"] == "ok"
    assert checks["copilot.model_context"]["value"]["context_window_tokens"] == 24_000
    assert checks["copilot.pi_session_path"]["status"] == "ok"
    assert checks["copilot.pi_session_path"]["value"]["pi_session_path"] == str(
        runtime / "output_shared" / "state" / "pi_sessions.sqlite3"
    )
    assert not (runtime / "output_shared" / "state" / "pi_sessions.sqlite3").exists()
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_setup_check_rejects_invalid_model_context(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path, context_window_tokens=4_096)
    config_path = root / "config.assistant.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["assistant"]["llm"]["max_output_tokens"] = 4_096
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "src.application.setup.check.shutil.which",
        lambda name: {"node": str(node), "npm": str(npm), "uv": None}.get(name),
    )

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["copilot.model_context"]["status"] == "error"
    assert checks["copilot.model_context"]["value"]["error"] == "invalid_assistant_config"


def test_setup_check_reports_missing_or_unwritable_pi_session_parent(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path)
    missing_audit = tmp_path / "missing" / "state" / "inbound_control.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(missing_audit))
    monkeypatch.setattr(
        "src.application.setup.check.shutil.which",
        lambda name: {"node": str(node), "npm": str(npm), "uv": None}.get(name),
    )

    missing = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    missing_check = {item["name"]: item for item in missing["checks"]}["copilot.pi_session_path"]
    assert missing_check["status"] == "error"
    assert missing_check["value"]["parent_exists"] is False
    assert not missing_audit.parent.exists()

    existing_parent = tmp_path / "existing" / "state"
    existing_parent.mkdir(parents=True)
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(existing_parent / "inbound_control.sqlite3"))
    real_access = os.access
    monkeypatch.setattr(
        "src.application.setup.check.os.access",
        lambda path, mode: False if Path(path) == existing_parent else real_access(path, mode),
    )
    unwritable = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    unwritable_check = {item["name"]: item for item in unwritable["checks"]}["copilot.pi_session_path"]
    assert unwritable_check["status"] == "error"
    assert unwritable_check["value"]["parent_exists"] is True
    assert not (existing_parent / "pi_sessions.sqlite3").exists()


def test_setup_check_rejects_symlinked_pi_session_parent_without_resolving_or_writing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path)
    physical_parent = tmp_path / "physical-state"
    physical_parent.mkdir()
    lexical_parent = tmp_path / "linked-state"
    lexical_parent.symlink_to(physical_parent, target_is_directory=True)
    audit_db = lexical_parent / "inbound_control.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(audit_db))
    monkeypatch.setattr(
        "src.application.setup.check.shutil.which",
        lambda name: {"node": str(node), "npm": str(npm), "uv": None}.get(name),
    )

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    check = {item["name"]: item for item in out["checks"]}["copilot.pi_session_path"]

    assert check["status"] == "error"
    assert check["value"]["parent"] == str(lexical_parent)
    assert check["value"]["pi_session_path"] == str(lexical_parent / "pi_sessions.sqlite3")
    assert check["value"]["parent_is_symlink"] is True
    assert not (physical_parent / "pi_sessions.sqlite3").exists()


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
