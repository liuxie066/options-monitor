from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.application.setup import run_setup_check
from src.application.setup.check import _credential_guidance
from src.application.secret_store.contracts import SecretBackendUnavailable, SecretStatus


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _minimal_repo(tmp_path: Path) -> Path:
    """Lay out the smallest repo root the setup checks accept."""
    (tmp_path / "src").mkdir()
    (tmp_path / "om").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    return tmp_path


def _stub_toolchain(monkeypatch, node: Path, npm: Path) -> None:
    """Pin ``node``/``npm`` lookups to the fake executables and hide ``uv``."""
    monkeypatch.setattr(
        "src.application.setup.check.shutil.which",
        lambda name: {"node": str(node), "npm": str(npm), "uv": None}.get(name),
    )


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
                    "bot": {"enabled": True, "toolsets": {}},
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
    repo_root = _minimal_repo(tmp_path)

    out = run_setup_check(repo_root=repo_root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert isinstance(out["summary"]["ok"], bool)
    assert checks["platform"]["value"]["service_target"] in {"systemd", "launchd", "manual"}
    assert out["platform_profile"]["default_env_file"]
    assert checks["install.repo"]["status"] == "ok"
    assert checks["upgrade.uv"]["status"] in {"ok", "info", "warn"}
    assert checks["config.us"]["status"] == "warn"
    assert "config init" in checks["config.us"]["hint"]
    assert any(" config init " in step for step in out["next_steps"])
    assert not (tmp_path / "config.us.json").exists()


def test_setup_check_mac_advice_uses_installed_and_runtime_paths(monkeypatch, tmp_path: Path) -> None:
    from src.application.platform_profile import current_platform_profile

    (tmp_path / "installed release").mkdir()
    repo = _minimal_repo(tmp_path / "installed release")
    runtime = tmp_path / "Library" / "Application Support" / "options-monitor"
    profile = current_platform_profile(system="Darwin", home=tmp_path)
    monkeypatch.setattr("src.application.setup.check.current_platform_profile", lambda: profile)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))

    missing = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    config_check = next(item for item in missing["checks"] if item["name"] == "config.us")
    init_command = config_check["hint"]
    assert init_command == missing["next_steps"][0]
    assert str(repo / "om") in init_command
    assert str(runtime / "config.yaml") in init_command
    assert "--no-build" in init_command
    assert f"--config-yaml '{runtime / 'config.yaml'}'" in missing["next_steps"][1]
    assert f"--output '{runtime / 'config.us.json'}'" in missing["next_steps"][1]
    assert str(runtime / "resolved" / "config.assistant.json") in missing["next_steps"][2]

    monkeypatch.delenv("OM_RUNTIME_ROOT")
    fresh_shell = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    assert fresh_shell["next_steps"][0].startswith("export OM_RUNTIME_ROOT=")
    assert str(profile.default_runtime_root) in fresh_shell["next_steps"][0]
    assert str(profile.default_runtime_root / "config.yaml") in fresh_shell["next_steps"][1]
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))

    runtime.mkdir(parents=True)
    (runtime / "config.yaml").write_text("{}\n", encoding="utf-8")
    partial = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    partial_hint = next(item for item in partial["checks"] if item["name"] == "config.us")["hint"]
    assert " config build " in partial_hint
    assert all(" config init " not in step for step in partial["next_steps"])
    assert partial_hint == partial["next_steps"][0]

    (runtime / "config.us.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "src.application.setup.check.load_runtime_config",
        lambda **_kwargs: (runtime / "config.us.json", {}),
    )
    monkeypatch.setattr(
        "src.application.setup.check.evaluate_runtime_config_readiness",
        lambda *_args, **_kwargs: {"ok": False},
    )
    invalid = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    invalid_hint = next(item for item in invalid["checks"] if item["name"] == "config.us")["hint"]
    assert f"'{repo / 'om'}' config validate" in invalid_hint
    assert f"--config-path '{runtime / 'config.us.json'}'" in invalid_hint
    monkeypatch.setattr(
        "src.application.setup.check.evaluate_runtime_config_readiness",
        lambda *_args, **_kwargs: {"ok": True},
    )
    ready = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    render_command = next(step for step in ready["next_steps"] if " service render " in step)
    assert f"--repo-root '{repo}'" in render_command
    assert f"--runtime-root '{runtime}'" in render_command
    assert f"--config-us '{runtime / 'config.us.json'}'" in render_command


def test_setup_check_prefers_runtime_assistant_snapshot_over_legacy_repo_file(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    runtime = tmp_path / "runtime"
    resolved = runtime / "resolved" / "config.assistant.json"
    resolved.parent.mkdir(parents=True)
    (repo / "config.assistant.json").write_text(json.dumps({
        "assistant": {"enabled": True, "bot": {"enabled": True},
                      "llm": {"provider": "deepseek", "model": "deepseek-chat",
                              "context_window_tokens": 24000, "max_output_tokens": 2048}},
    }), encoding="utf-8")
    resolved.write_text(json.dumps({
        "assistant": {"enabled": True, "bot": {"enabled": False},
                      "llm": {"provider": "ollama", "model": "local-test",
                              "context_window_tokens": 24000, "max_output_tokens": 2048}},
    }), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))

    out = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["value"]["config_path"] == str(resolved)
    assert checks["credential_guidance"]["value"]["credentials"] == []


def test_setup_check_skips_bot_requirements_when_disabled(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    runtime = tmp_path / "runtime"
    resolved = runtime / "resolved" / "config.assistant.json"
    resolved.parent.mkdir(parents=True)
    resolved.write_text(json.dumps({"assistant": {"enabled": False, "bot": {"enabled": False}}}), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))

    out = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["status"] == "info"
    assert checks["bot.session_path"]["status"] == "info"


def test_setup_check_does_not_hide_bot_when_assistant_snapshot_missing(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "config.yaml").write_text("assistant:\n  enabled: true\n  bot:\n    enabled: true\n", encoding="utf-8")
    (repo / "config.assistant.json").write_text(json.dumps({"assistant": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))

    out = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["bot.model_context"]["status"] == "error"
    assert checks["bot.model_context"]["value"]["config_path"] == str(runtime / "resolved" / "config.assistant.json")


def test_setup_check_uses_explicit_runtime_and_env_paths(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    runtime = tmp_path / "selected-runtime"
    env_file = tmp_path / "selected.env"
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    result = run_setup_check(
        repo_root=repo,
        markets=["us"],
        runtime_root=runtime,
        env_file=env_file,
        include_local_env_file=False,
    )
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["runtime_root"]["value"]["runtime_root"] == str(runtime)
    assert checks["settings"]["value"]["env_file"] == "<configured-env-file>"
    assert str(runtime / "config.yaml") in checks["config.us"]["hint"]
    assert any(str(env_file) in step for step in result["next_steps"])


def test_setup_check_next_steps_keep_inherited_env_file(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    env_file = tmp_path / "inherited.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))

    result = run_setup_check(repo_root=repo, markets=["us"], include_local_env_file=False)

    assert any(str(env_file) in step for step in result["next_steps"])


def test_setup_check_custom_runtime_uses_matching_default_env_path(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _minimal_repo(repo)
    runtime = tmp_path / "manual-runtime"
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)

    result = run_setup_check(repo_root=repo, markets=["us"], runtime_root=runtime, include_local_env_file=False)

    assert any(str(runtime / "options-monitor.env") in step for step in result["next_steps"])


def test_credential_guidance_deduplicates_confirmed_requirements_without_get(monkeypatch, tmp_path: Path) -> None:
    assistant = tmp_path / "config.assistant.json"
    assistant.write_text(json.dumps({
        "assistant": {
            "enabled": True,
            "bot": {"enabled": True},
            "llm": {
                "provider": "deepseek", "model": "deepseek-chat",
                "context_window_tokens": 24000, "max_output_tokens": 2048,
            },
        }
    }), encoding="utf-8")
    calls: list[str] = []

    class Store:
        backend_name = "keychain"

        def status(self, name: str) -> SecretStatus:
            calls.append(name)
            return SecretStatus(name, name != "feishu.bot.app_secret", "keychain", "test")

        def get(self, _name: str) -> str:
            raise AssertionError("setup guidance must not read secret values")

    monkeypatch.setattr("src.application.setup.check.build_secret_provisioner", lambda **_kwargs: Store())
    cfg = {
        "accounts": ["sy"],
        "account_settings": {"sy": {"type": "external_holdings"}},
        "notifications": {"provider": "feishu_app"},
    }
    value, steps = _credential_guidance(
        repo_root=tmp_path,
        market_configs={"us": cfg, "hk": cfg},
        unknown_markets=[],
        assistant_config=assistant,
        effective_env={"OM_SECRET_BACKEND": "auto"},
        platform_name="macos",
    )

    items = {item["logical_name"]: item for item in value["credentials"]}
    assert set(items) == {"feishu.holdings.app_secret", "feishu.bot.app_secret", "llm.deepseek.api_key"}
    assert calls == list(items)
    assert items["feishu.holdings.app_secret"]["features"] == ["holdings:hk", "holdings:us"]
    assert items["feishu.bot.app_secret"]["storage_status"] == "missing"
    assert all(item["consumer_verified"] is False for item in items.values())
    assert steps == ["om secrets set feishu.bot.app_secret"]


def test_credential_guidance_unknown_and_bot_disabled_skip_store(monkeypatch, tmp_path: Path) -> None:
    assistant = tmp_path / "config.assistant.json"
    assistant.write_text(json.dumps({
        "assistant": {
            "enabled": True,
            "bot": {"enabled": False},
            "llm": {
                "provider": "deepseek", "model": "deepseek-chat",
                "context_window_tokens": 24000, "max_output_tokens": 2048,
            },
        }
    }), encoding="utf-8")
    monkeypatch.setattr(
        "src.application.setup.check.build_secret_provisioner",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("zero needs must not probe storage")),
    )

    value, steps = _credential_guidance(
        repo_root=tmp_path,
        market_configs={"us": {"accounts": ["lx"]}},
        unknown_markets=["hk"],
        assistant_config=assistant,
        effective_env={"OM_SECRET_BACKEND": "auto"},
        platform_name="macos",
    )

    assert value["credentials"] == []
    assert "market:hk" in value["unknown_requirements"]
    assert steps == []


def test_credential_guidance_storage_failure_stays_unknown(monkeypatch, tmp_path: Path) -> None:
    class Store:
        backend_name = "systemd"

        def status(self, _name: str) -> SecretStatus:
            raise SecretBackendUnavailable("storage metadata unavailable")

    monkeypatch.setattr("src.application.setup.check.build_secret_provisioner", lambda **_kwargs: Store())
    value, steps = _credential_guidance(
        repo_root=tmp_path,
        market_configs={"us": {"accounts": ["lx"], "notifications": {"provider": "feishu_app"}}},
        unknown_markets=[],
        assistant_config=tmp_path / "missing.json",
        effective_env={"OM_SECRET_BACKEND": "auto"},
        platform_name="linux",
    )

    assert value["credentials"][0]["storage_status"] == "unknown"
    assert "assistant_config" in value["unknown_requirements"]
    assert steps == []


def test_credential_guidance_inbound_gate_and_local_model(monkeypatch, tmp_path: Path) -> None:
    assistant = tmp_path / "config.assistant.json"
    assistant.write_text(json.dumps({
        "assistant": {
            "enabled": True,
            "bot": {"enabled": True},
            "llm": {
                "provider": "ollama", "model": "local-test",
                "context_window_tokens": 24000, "max_output_tokens": 2048,
            },
        }
    }), encoding="utf-8")
    observed: list[str] = []

    class Store:
        backend_name = "systemd"

        def status(self, name: str) -> SecretStatus:
            observed.append(name)
            return SecretStatus(name, False, "systemd", "test")

    monkeypatch.setattr("src.application.setup.check.build_secret_provisioner", lambda **_kwargs: Store())
    value, steps = _credential_guidance(
        repo_root=tmp_path,
        market_configs={"us": {"accounts": ["lx"]}},
        unknown_markets=[],
        assistant_config=assistant,
        effective_env={
            "OM_SECRET_BACKEND": "auto",
            "OM_INBOUND_OPERATIONS_ENABLED": "1",
            "OM_INBOUND_MONITOR_RUN_ENABLED": "1",
        },
        platform_name="linux",
    )

    assert observed == ["inbound.operation_hmac_key"]
    assert value["credentials"][0]["features"] == ["inbound_operations"]
    assert steps == [f"sudo {tmp_path / 'om'} secrets set inbound.operation_hmac_key"]


def test_credential_guidance_env_compatibility_does_not_claim_storage_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.application.setup.check.build_secret_provisioner",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("env mode has no storage status")),
    )
    value, steps = _credential_guidance(
        repo_root=tmp_path,
        market_configs={"us": {"accounts": ["lx"], "notifications": {"provider": "feishu_app"}}},
        unknown_markets=[],
        assistant_config=tmp_path / "missing.json",
        effective_env={"OM_SECRET_BACKEND": "env"},
        platform_name="macos",
    )

    assert value["credentials"][0]["storage_status"] == "unknown"
    assert steps == []


def test_setup_check_guidance_uses_effective_runtime_config_and_terminal_command(monkeypatch, tmp_path: Path) -> None:
    root = _minimal_repo(tmp_path)
    (root / "config.us.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(root))
    monkeypatch.setenv("OM_SECRET_BACKEND", "auto")
    monkeypatch.setattr(
        "src.application.setup.check.load_runtime_config",
        lambda **_kwargs: (root / "config.us.json", {
            "accounts": ["lx"], "notifications": {"provider": "feishu_app"},
        }),
    )
    monkeypatch.setattr(
        "src.application.setup.check.evaluate_runtime_config_readiness",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "src.application.setup.check.diagnose_effective_settings",
        lambda **_kwargs: {"summary": {"error_count": 0, "warning_count": 0}},
    )

    class Store:
        backend_name = "keychain"

        def status(self, name: str) -> SecretStatus:
            return SecretStatus(name, False, "keychain", "test")

    monkeypatch.setattr("src.application.setup.check.build_secret_provisioner", lambda **_kwargs: Store())

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    check = {item["name"]: item for item in out["checks"]}["credential_guidance"]

    assert check["status"] == "warn"
    assert check["value"]["credentials"] == [{
        "logical_name": "feishu.bot.app_secret",
        "features": ["notifications:us"],
        "storage_status": "missing",
        "consumer_verified": False,
        "runtime_consumer_status": "unknown",
    }]
    assert "om secrets set feishu.bot.app_secret" in out["next_steps"]
    assert out["summary"]["warning_count"] >= 1


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


def test_setup_check_reports_pi_runtime_context_and_session_without_writes(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    _stub_toolchain(monkeypatch, node, npm)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert not {"install.node", "install.npm", "install.pi_packages"} & checks.keys()
    assert checks["bot.model_context"]["status"] == "ok"
    assert checks["bot.model_context"]["value"]["context_window_tokens"] == 24_000
    assert checks["bot.session_path"]["status"] == "ok"
    assert checks["bot.session_path"]["value"]["session_path"] == str(
        runtime / "output_shared" / "state" / "inbound_control.sqlite3"
    )
    assert not (runtime / "output_shared" / "state" / "inbound_control.sqlite3").exists()
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_setup_check_rejects_invalid_model_context(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path, context_window_tokens=4_096)
    config_path = root / "config.assistant.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["assistant"]["llm"]["max_output_tokens"] = 4_096
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    _stub_toolchain(monkeypatch, node, npm)

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["bot.model_context"]["status"] == "error"
    assert checks["bot.model_context"]["value"]["error"] == "invalid_assistant_config"


def test_setup_check_reports_missing_or_unwritable_pi_session_parent(monkeypatch, tmp_path: Path) -> None:
    root, node, npm = _prepare_pi_setup_root(tmp_path)
    missing_audit = tmp_path / "missing" / "state" / "inbound_control.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(missing_audit))
    _stub_toolchain(monkeypatch, node, npm)

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
    _stub_toolchain(monkeypatch, node, npm)

    out = run_setup_check(repo_root=root, markets=["us"], include_local_env_file=False)
    check = {item["name"]: item for item in out["checks"]}["bot.session_path"]

    assert check["status"] == "error"
    assert check["value"]["parent"] == str(lexical_parent)
    assert check["value"]["session_path"] == str(lexical_parent / "inbound_control.sqlite3")
    assert check["value"]["parent_is_symlink"] is True
    assert not (physical_parent / "pi_sessions.sqlite3").exists()


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


def test_cli_setup_init_requires_interactive_terminal(capsys) -> None:
    import src.interfaces.cli.main as cli

    assert cli.main(["setup", "init"]) == 2
    assert "interactive terminal" in capsys.readouterr().out
