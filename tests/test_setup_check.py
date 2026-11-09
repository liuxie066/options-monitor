from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.application.setup import run_setup_check




def _minimal_repo(tmp_path: Path) -> Path:
    """Lay out the smallest repo root the setup checks accept."""
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    return tmp_path


def _stub_toolchain(monkeypatch) -> None:
    """No optional installer or retired Node toolchain is available."""
    monkeypatch.setattr("src.application.setup.check.shutil.which", lambda _name: None)

def _prepare_python_setup_root(tmp_path: Path, *, context_window_tokens: int = 24_000) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/application/bot").mkdir(parents=True)
    (root / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    (root / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "resolved").mkdir(parents=True)
    (runtime / "resolved" / "config.bot.json").write_text(
        json.dumps(
            {
                "bot": {'enabled': True, 'llm': {'provider': 'ollama', 'base_url': 'http://127.0.0.1:11434/v1', 'model': 'local-test', 'context_window_tokens': context_window_tokens, 'max_output_tokens': 2048}}
            }
        ),
        encoding="utf-8",
    )
    state = runtime / "output_shared" / "state"
    state.mkdir(parents=True)
    return root


def test_setup_check_is_read_only_and_reports_missing_config(tmp_path: Path) -> None:
    repo_root = _minimal_repo(tmp_path)

    out = run_setup_check(repo_root=repo_root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert isinstance(out["summary"]["ok"], bool)
    assert checks["platform"]["value"]["service_target"] in {"systemd", "launchd", "manual"}
    assert out["platform_profile"]["default_env_file"]
    assert checks["install.repo"]["status"] == "ok"
    assert checks["upgrade.uv"]["status"] in {"ok", "info", "warn"}
    assert checks["config.us"]["status"] == "error"
    assert "setup init" in checks["config.us"]["hint"]
    assert "om setup init" in out["next_steps"]
    assert not (tmp_path / "config.us.json").exists()


def test_setup_check_separates_starter_placeholder_from_optional_bot(monkeypatch, tmp_path: Path) -> None:
    from src.application.config_yaml_init import init_yaml_config

    repo = Path(__file__).resolve().parents[1]
    init_yaml_config(
        account_label="lx", repo_root=repo,
        output_config_yaml_path=tmp_path / "config.yaml",
        runtime_output_dir=tmp_path,
        bot_output_config_path=tmp_path / "resolved" / "config.bot.json",
        markets=["us"],
        us_symbols=["AAPL"],
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))

    out = run_setup_check(repo_root=repo, include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert out["markets"] == ["us"]
    assert "config.hk" not in checks
    assert out["summary"]["ok"] is False
    assert out["summary"]["bot_ready"] is False
    assert checks["config.us"]["status"] == "error"
    assert "REPLACE_WITH_FUTU_ACCOUNT_ID" in checks["config.us"]["message"]
    assert "om setup init" not in out["next_steps"]
    assert any("om accounts edit --market us" in step for step in out["next_steps"])
    assert checks["bot.model_context"]["status"] == "info"
    assert checks["bot.session_path"]["status"] == "info"


def test_first_run_account_repair_and_symbol_add_keep_one_market_ready(monkeypatch, tmp_path: Path) -> None:
    from src.application.config_yaml_init import init_yaml_config
    from src.interfaces.cli.main import main as cli_main
    from src.interfaces.cli.symbols import main as symbols_main

    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "config.yaml"
    init_yaml_config(
        account_label="lx", repo_root=repo, output_config_yaml_path=source, runtime_output_dir=tmp_path,
        bot_output_config_path=tmp_path / "resolved" / "config.bot.json",
        markets=["us"], us_symbols=["AAPL"],
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))

    assert cli_main([
        "accounts", "edit", "--market", "us", "--account-label", "lx",
        "--futu-acc-id", "123456", "--config-yaml", str(source), "--apply", "--confirm",
    ]) == 0
    assert symbols_main([
        "add", "TSLA", "--strategy", "csp", "--csp-max-strike", "100",
        "--market", "us", "--config-yaml", str(source), "--apply",
    ]) == 0

    checked = run_setup_check(repo_root=repo, include_local_env_file=False)
    configs = {item["name"]: item for item in checked["checks"] if item["name"].startswith("config.")}
    assert checked["markets"] == ["us"]
    assert configs["config.us"]["status"] == "ok"
    assert "config.hk" not in configs
    assert "om setup init" not in checked["next_steps"]


def test_setup_check_rebuilds_missing_snapshot_from_existing_yaml(monkeypatch, tmp_path: Path) -> None:
    from src.application.config_yaml_init import init_yaml_config

    repo = Path(__file__).resolve().parents[1]
    init_yaml_config(
        account_label="lx", repo_root=repo, output_config_yaml_path=tmp_path / "config.yaml",
        runtime_output_dir=tmp_path, markets=["us"], us_symbols=["AAPL"],
    )
    (tmp_path / "config.us.json").unlink()
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))

    checked = run_setup_check(repo_root=repo, include_local_env_file=False)
    config = next(item for item in checked["checks"] if item["name"] == "config.us")
    assert config["hint"].startswith("om config build --source yaml --market us")
    assert config["hint"].endswith("--dry-run")
    assert "om setup init" not in checked["next_steps"]


def test_setup_check_warns_when_uv_forced_but_missing(monkeypatch, tmp_path: Path) -> None:
    _minimal_repo(tmp_path)
    monkeypatch.setattr("src.application.setup.check.shutil.which", lambda _name: None)
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "uv")

    out = run_setup_check(repo_root=tmp_path, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["upgrade.uv"]["status"] == "warn"
    assert checks["upgrade.uv"]["value"]["installer_mode"] == "uv"
    assert "Install uv" in checks["upgrade.uv"]["hint"]


def test_setup_check_no_longer_requires_yfinance(monkeypatch, tmp_path: Path) -> None:
    _minimal_repo(tmp_path)

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
    _minimal_repo(tmp_path)
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


@pytest.mark.parametrize("kind,repo_parent,runtime_parent", [
    pytest.param("default", True, True, id="default-both-parents"),
    pytest.param("default", False, True, id="default-runtime-parent-only"),
    pytest.param("default", True, False, id="default-repo-parent-only"),
    pytest.param("absolute", True, True, id="explicit-absolute"),
    pytest.param("relative", True, True, id="explicit-relative"),
])
def test_setup_cli_session_path_matches_actual_bot_store(
    monkeypatch, tmp_path: Path, capsys, kind, repo_parent, runtime_parent,
) -> None:
    import socket

    from src.application.bot.control import audit, runtime as bot_runtime
    import src.interfaces.cli.main as cli

    root = _prepare_python_setup_root(tmp_path).resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_db = runtime_root / "output_shared/state/inbound_control.sqlite3"
    if not runtime_parent:
        runtime_db.parent.rmdir()
    if kind == "absolute":
        raw = str(tmp_path / "explicit/inbound.sqlite3")
        expected = Path(raw)
    elif kind == "relative":
        raw = "relative/inbound.sqlite3"
        expected = root / raw
    else:
        raw = None
        expected = root / "output_shared/state/inbound_control.sqlite3"
    if repo_parent:
        expected.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_INBOUND_AUDIT_DB", raising=False)
    if raw is not None:
        monkeypatch.setenv("OM_INBOUND_AUDIT_DB", raw)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(cli, "repo_base", lambda: root)
    monkeypatch.setattr(audit, "repo_base", lambda: root)
    _stub_toolchain(monkeypatch)
    selected_paths, forbidden_calls = [], []
    real_store = audit.InboundAuditStore

    class StoreCaptured(BaseException):
        pass

    def capture_store(path=None, **kwargs):
        store = real_store(path, **kwargs)
        selected_paths.append(store.path)
        raise StoreCaptured

    def forbidden_effect(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("diagnostic path comparison must not execute business or persist")

    monkeypatch.setattr(bot_runtime, "InboundAuditStore", capture_store)
    monkeypatch.setattr(bot_runtime, "handle_bot_request", forbidden_effect)
    monkeypatch.setattr(audit, "connect_private_sqlite", forbidden_effect)
    monkeypatch.setattr(socket.socket, "connect", forbidden_effect)
    before = {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}
    cli.main(["setup", "check", "--market", "us", "--no-local-env-file"])
    response = json.loads(capsys.readouterr().out)
    assert response["tool_name"] == "setup.check"
    with pytest.raises(StoreCaptured):
        cli.main([
            "bot", "handle", "--text", "/help", "--sender", "fixture", "--config-key", "us",
            "--bot-config", str(runtime_root / "resolved/config.bot.json"),
        ])
    assert selected_paths == [expected]
    assert forbidden_calls == []
    assert {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before
    assert not expected.exists() and not runtime_db.exists()
    report = response["data"]
    checks = {item["name"]: item for item in report["checks"]}
    session = checks["bot.session_path"]
    assert session["value"]["session_path"] == str(selected_paths[0])
    assert session["value"]["host_audit_db"] == str(selected_paths[0])
    assert session["value"]["parent_exists"] is repo_parent
    assert session["status"] == ("ok" if repo_parent else "error")
    assert checks["bot.model_context"]["status"] == "ok"
    assert report["summary"]["bot_ready"] is repo_parent


@pytest.mark.parametrize("repo_env_exists", [False, True], ids=["repo-env-missing", "repo-env-conflicts"])
def test_default_bot_stores_preserve_relative_env_file(monkeypatch, tmp_path: Path, repo_env_exists) -> None:
    from src.application.bot.control import audit, operation_store

    repo = tmp_path / "repo"
    instance = tmp_path / "instance"
    repo.mkdir()
    instance.mkdir()
    expected = instance / "chosen.sqlite3"
    (instance / "options-monitor.env").write_text(f"OM_INBOUND_AUDIT_DB={expected}\n", encoding="utf-8")
    if repo_env_exists:
        (repo / "options-monitor.env").write_text(
            f"OM_INBOUND_AUDIT_DB={repo / 'other.sqlite3'}\n", encoding="utf-8",
        )
    monkeypatch.chdir(instance)
    monkeypatch.setenv("OM_ENV_FILE", "options-monitor.env")
    monkeypatch.delenv("OM_INBOUND_AUDIT_DB", raising=False)
    monkeypatch.setattr(audit, "repo_base", lambda: repo)
    forbidden_calls = []

    def forbidden_connect(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("selecting an audit path must not connect SQLite")

    monkeypatch.setattr(audit, "connect_private_sqlite", forbidden_connect)
    monkeypatch.setattr(operation_store, "connect_inbound_sqlite", forbidden_connect)
    before = {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}
    selected = [audit.InboundAuditStore().path, operation_store.InboundOperationStore().path]

    assert forbidden_calls == []
    assert {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before
    assert selected == [expected, expected]


@pytest.mark.parametrize("kind", ["absolute", "relative"])
def test_setup_check_session_path_preserves_effective_env_file(monkeypatch, tmp_path: Path, kind) -> None:
    root = _prepare_python_setup_root(tmp_path).resolve()
    raw = str(tmp_path / "file-selected/inbound.sqlite3") if kind == "absolute" else "file-selected/inbound.sqlite3"
    expected = Path(raw) if kind == "absolute" else root / raw
    expected.parent.mkdir(parents=True)
    env_file = tmp_path / "audit.env"
    env_file.write_text(f"OM_INBOUND_AUDIT_DB={raw}\n", encoding="utf-8")
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(tmp_path / "process-selected/inbound.sqlite3"))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    _stub_toolchain(monkeypatch)
    before = {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}
    before_env = dict(os.environ)

    out = run_setup_check(repo_root=root, markets=["us"], env_file=env_file, include_local_env_file=False)
    session = {item["name"]: item for item in out["checks"]}["bot.session_path"]

    assert session["value"]["session_path"] == str(expected)
    assert session["value"]["host_audit_db"] == str(expected)
    assert session["status"] == "ok"
    assert dict(os.environ) == before_env
    assert {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before


def test_setup_check_reports_python_runtime_context_and_session_without_writes(monkeypatch, tmp_path: Path) -> None:
    root = _prepare_python_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    (root / "output_shared" / "state").mkdir(parents=True)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    _stub_toolchain(monkeypatch)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert not {"install.node", "install.npm", "install.pi_packages"} & checks.keys()
    assert checks["bot.model_context"]["status"] == "ok"
    assert checks["bot.model_context"]["value"]["context_window_tokens"] == 24_000
    assert checks["bot.session_path"]["status"] == "ok"
    assert checks["bot.session_path"]["value"]["session_path"] == str(
        root / "output_shared" / "state" / "inbound_control.sqlite3"
    )
    assert not (root / "output_shared" / "state" / "inbound_control.sqlite3").exists()
    assert not (runtime / "output_shared" / "state" / "inbound_control.sqlite3").exists()
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_setup_check_rejects_invalid_model_context(monkeypatch, tmp_path: Path) -> None:
    root = _prepare_python_setup_root(tmp_path, context_window_tokens=4_096)
    config_path = tmp_path / "runtime" / "resolved" / "config.bot.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["bot"]["llm"]["max_output_tokens"] = 4_096
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    _stub_toolchain(monkeypatch)

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["bot.model_context"]["status"] == "error"
    assert checks["bot.model_context"]["value"]["error"] == "invalid_bot_config"


def test_setup_check_reports_missing_or_unwritable_bot_session_parent(monkeypatch, tmp_path: Path) -> None:
    root = _prepare_python_setup_root(tmp_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    missing_audit = tmp_path / "missing" / "state" / "inbound_control.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(missing_audit))
    _stub_toolchain(monkeypatch)

    missing = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    missing_check = {item["name"]: item for item in missing["checks"]}["bot.session_path"]
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
    unwritable_check = {item["name"]: item for item in unwritable["checks"]}["bot.session_path"]
    assert unwritable_check["status"] == "error"
    assert unwritable_check["value"]["parent_exists"] is True
    assert not (existing_parent / "inbound_control.sqlite3").exists()


def test_setup_check_rejects_symlinked_bot_session_parent_without_resolving_or_writing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _prepare_python_setup_root(tmp_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    physical_parent = tmp_path / "physical-state"
    physical_parent.mkdir()
    lexical_parent = tmp_path / "linked-state"
    lexical_parent.symlink_to(physical_parent, target_is_directory=True)
    audit_db = lexical_parent / "inbound_control.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(audit_db))
    _stub_toolchain(monkeypatch)

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    check = {item["name"]: item for item in out["checks"]}["bot.session_path"]

    assert check["status"] == "error"
    assert check["value"]["parent"] == str(lexical_parent)
    assert check["value"]["session_path"] == str(lexical_parent / "inbound_control.sqlite3")
    assert check["value"]["parent_is_symlink"] is True
    assert not (physical_parent / "inbound_control.sqlite3").exists()


def test_setup_check_reports_stale_runtime_config_and_schedule_readiness(monkeypatch, tmp_path: Path) -> None:
    from src.application.config_defaults import DEFAULT_CONFIG_REF, default_config_sha256

    _minimal_repo(tmp_path)
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


def test_cli_setup_init_requires_confirmation_when_noninteractive(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    rc = cli.main(["setup", "init", "--market", "us", "--futu-acc-id", "123456"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("bot", [{"enabled": False, "llm": {"context_window_tokens": "broken"}}])
def test_disabled_bot_skips_model_and_session_checks(monkeypatch, tmp_path, bot):
    root = _prepare_python_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    config_path = runtime / "resolved/config.bot.json"
    if bot is None:
        config_path.unlink()
    else:
        config_path.write_text(json.dumps({"bot": bot}))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(tmp_path / "absent/state/audit.db"))
    monkeypatch.setattr("src.application.setup.check.load_bot_llm_config",
                        lambda **kw: pytest.fail("disabled Bot must not validate a model"))
    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["status"] == "info"
    assert checks["bot.session_path"]["status"] == "info"
    assert "hint" not in checks["bot.model_context"] and "hint" not in checks["bot.session_path"]
    assert not out["summary"]["bot_ready"]
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("content", ['{"bot":{"enabled":true}}', '{broken'])
def test_setup_check_keeps_broken_bot_as_error(monkeypatch, tmp_path, content):
    root = _prepare_python_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    (runtime / "resolved/config.bot.json").write_text(content)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["status"] == "error"
    assert out["summary"]["ok"] is False and not out["summary"]["bot_ready"]


def test_setup_check_reports_corrupt_authoring_without_hiding_it(monkeypatch, tmp_path):
    root = _minimal_repo(tmp_path)
    (root / "config.yaml").write_text("markets: [")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(root))
    out = run_setup_check(repo_root=root, include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["config.authoring"]["status"] == "error"
    assert out["summary"]["ok"] is False


@pytest.mark.parametrize("case", [
    "missing", "corrupt_snapshot", "disabled_snapshot", "stale_source", "conflicting_model",
    "corrupt_authoring", "corrupt_metadata", "wrong_source_identity",
    "disabled_missing", "disabled_corrupt_snapshot", "valid",
])
def test_setup_facade_honors_authoring_bot_intent(monkeypatch, tmp_path, capsys, case):
    import socket
    import yaml

    import src.interfaces.cli.main as cli
    from src.application.config_yaml import build_yaml_bot_config_file
    from src.application.config_yaml_init import init_yaml_config

    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "config.yaml"
    snapshot = tmp_path / "resolved/config.bot.json"
    init_yaml_config(
        repo_root=repo, output_config_yaml_path=source, runtime_output_dir=tmp_path,
        bot_output_config_path=snapshot, markets=["us"], us_symbols=["AAPL"],
        account_label="review", futu_acc_id="123456",
    )
    document = yaml.safe_load(source.read_text())
    disabled = case.startswith("disabled_") and case != "disabled_snapshot"
    document["bot"] = {"enabled": not disabled, "llm": {
        "provider": "ollama", "model": "local-test", "base_url": "http://127.0.0.1:11434/v1",
        "context_window_tokens": 24000, "max_output_tokens": 2048,
    }}
    source.write_text(yaml.safe_dump(document))
    build_yaml_bot_config_file(repo_root=repo, config_path=source, output_config_path=snapshot)
    if case in {"missing", "disabled_missing"}:
        snapshot.unlink()
    elif case in {"corrupt_snapshot", "disabled_corrupt_snapshot"}:
        snapshot.write_text("{broken")
    elif case == "corrupt_authoring":
        source.write_text("markets: [")
        snapshot.write_text('{"bot":{"enabled":false}}')
    elif case in {"disabled_snapshot", "conflicting_model", "corrupt_metadata", "wrong_source_identity"}:
        payload = json.loads(snapshot.read_text())
        if case == "disabled_snapshot":
            payload["bot"]["enabled"] = False
        elif case == "conflicting_model":
            payload["bot"]["llm"]["model"] = "other-model"
        elif case == "corrupt_metadata":
            payload["_generated"] = None
        else:
            payload["_resolved"]["config_yaml_path"] = "/another-instance/config.yaml"
        snapshot.write_text(json.dumps(payload))
    elif case == "stale_source":
        source.write_text(source.read_text() + "\n# source changed after publication\n")
    (tmp_path / "output_shared/state").mkdir(parents=True, exist_ok=True)
    setup_root = _prepare_python_setup_root(tmp_path)
    (setup_root / "output_shared/state").mkdir(parents=True)
    monkeypatch.setattr(cli, "repo_base", lambda: setup_root)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("setup must stay offline"))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    rc = cli.main(["setup", "check", "--market", "us", "--no-local-env-file"])
    response = json.loads(capsys.readouterr().out)
    checks = {item["name"]: item for item in response["data"]["checks"]}
    if disabled:
        assert rc == 0 and response["ok"] is True
        assert checks["bot.model_context"]["status"] == "info"
        assert checks["bot.session_path"]["status"] == "info"
        assert "hint" not in checks["bot.model_context"]
    elif case == "valid":
        assert rc == 0 and response["ok"] is True
        assert checks["bot.model_context"]["status"] == "ok"
        assert checks["bot.session_path"]["status"] == "ok"
        assert response["data"]["summary"]["bot_ready"] is True
    else:
        assert rc != 0 and response["ok"] is False
        assert checks["bot.model_context"]["status"] == "error"
        assert checks["bot.model_context"]["value"]["error"]
        assert "build-bot" in checks["bot.model_context"]["hint"]
        assert response["data"]["summary"]["bot_ready"] is False
        assert "disabled" not in checks["bot.session_path"]["message"]
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_setup_check_missing_runtime_only_bot_is_not_explicitly_disabled(monkeypatch, tmp_path):
    root = _prepare_python_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    (runtime / "resolved/config.bot.json").unlink()
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["status"] == "error"
    assert not out["summary"]["ok"]


@pytest.mark.parametrize("damage", [None, "missing_python", "corrupt_manifest", "missing_manifest", "checksum", "platform"])
def test_setup_facade_declared_bundle_has_no_source_fallback(monkeypatch, tmp_path, capsys, damage):
    import src.interfaces.cli.main as cli
    from src.application import runtime_bundle as bundle
    from test_runtime_bundle import miniature

    root = tmp_path / "bundle"
    manifest = miniature(root, platform_name=bundle.current_platform())
    (root / "src").mkdir()
    if damage == "missing_python":
        (root / bundle.PYTHON).unlink()
    elif damage == "corrupt_manifest":
        (root / bundle.MANIFEST).write_text("{broken")
    elif damage == "missing_manifest":
        (root / bundle.MANIFEST).unlink()
    elif damage == "checksum":
        (root / "om").write_text("changed")
    elif damage == "platform":
        manifest["platform"] = "linux-x86_64" if bundle.current_platform() != "linux-x86_64" else "macos-arm64"
        (root / bundle.MANIFEST).write_text(json.dumps(manifest))
    # Even an explicit interpreter override cannot repair a broken declared artifact.
    monkeypatch.setenv("OM_PYTHON", os.sys.executable)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "instance"))
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "uv")
    monkeypatch.setattr(cli, "repo_base", lambda: root)
    _stub_toolchain(monkeypatch)

    rc = cli.main(["setup", "check", "--market", "us", "--no-local-env-file"])
    response = json.loads(capsys.readouterr().out)
    checks = {item["name"]: item for item in response["data"]["checks"]}
    assert checks["install.runtime_bundle"]["status"] == ("error" if damage else "ok")
    assert "install.venv" not in checks and "upgrade.uv" not in checks
    assert checks["upgrade.runtime_bundle"]["status"] == "info"
    install_text = json.dumps([
        {key: c[key] for key in ("message", "hint") if key in c}
        for c in checks.values() if c["name"].startswith(("install.", "upgrade."))
    ])
    for source_hint in ("system python", ".venv", "pip install", "Install uv", "pip fallback"):
        assert source_hint not in install_text
    if damage:
        assert rc != 0 and response["ok"] is False


@pytest.mark.parametrize("repo_default", [False, True])
def test_setup_check_retains_runtime_only_instance_ownership(monkeypatch, tmp_path, repo_default):
    root = _prepare_python_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    (root / "config.bot.json").write_text('{"bot":{"enabled":false}}')
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    if repo_default:
        monkeypatch.setattr("src.application.setup.check.resolve_runtime_root", lambda **kw: type(
            "Resolution", (), {"runtime_root": root, "source": "repo_default"},
        )())
    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    check = next(c for c in out["checks"] if c["name"] == "bot.model_context")
    assert check["status"] == ("info" if repo_default else "ok")
    assert check["value"]["config_path"] == str(root / "config.bot.json" if repo_default else runtime / "resolved/config.bot.json")


def test_setup_check_source_venv_and_override_diagnostics_remain_compatible(monkeypatch, tmp_path):
    root = _minimal_repo(tmp_path)
    venv = root / ".venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("source interpreter fixture")
    monkeypatch.setenv("OM_PYTHON", os.sys.executable)
    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["install.venv"]["status"] == "ok"
    assert checks["install.venv"]["value"]["python"] == os.sys.executable
    assert "install.runtime_bundle" not in checks
    assert "upgrade.uv" in checks


def test_real_bundled_setup_entrypoint_uses_private_runtime_without_source_tools(tmp_path):
    """Opt-in offline reuse of an existing native archive, without reinstalling it."""
    import subprocess

    from src.application import runtime_bundle as bundle
    from src.application.config_yaml_init import init_yaml_config

    archive_env = os.environ.get("OM_TEST_RUNTIME_BUNDLE")
    if not archive_env:
        pytest.skip("requires an existing native archive; no build, download or install")
    archive = Path(archive_env).resolve()
    original_manifest = json.loads(archive.with_name(archive.name + ".manifest.json").read_text())
    root = tmp_path / "中文 运行包"
    manifest = bundle.materialize_bundle(archive, root, expected_tag=original_manifest["tag"])
    # Exercise this repair with the existing private runtime, not the archive's older setup module.
    module = "src/application/setup/check.py"
    (root / module).write_bytes((Path(__file__).resolve().parents[1] / module).read_bytes())
    manifest["files"][module] = bundle.sha256(root / module)
    (root / bundle.MANIFEST).write_text(json.dumps(manifest))
    instance = tmp_path / "instance"
    init_yaml_config(
        repo_root=root, output_config_yaml_path=instance / "config.yaml", runtime_output_dir=instance,
        bot_output_config_path=instance / "resolved/config.bot.json", markets=["us"],
        us_symbols=["AAPL"], account_label="review", futu_acc_id="123456",
    )
    before = {p.relative_to(instance): p.read_bytes() for p in instance.rglob("*") if p.is_file()}
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "OM_RUNTIME_ROOT": str(instance),
           "OM_SECRET_BACKEND": "env", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    result = subprocess.run(
        [str(root / "om"), "setup", "check", "--market", "us", "--no-local-env-file"],
        cwd=root, env=env, text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    checks = {item["name"]: item for item in response["data"]["checks"]}
    assert response["ok"] is True
    assert checks["install.runtime_bundle"]["status"] == "ok"
    assert checks["install.runtime_bundle"]["value"]["python"] == str(root / bundle.PYTHON)
    assert "install.venv" not in checks and "upgrade.uv" not in checks
    assert checks["bot.model_context"]["status"] == "info"
    assert {p.relative_to(instance): p.read_bytes() for p in instance.rglob("*") if p.is_file()} == before
