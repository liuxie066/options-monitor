"""Retired runtime entry points stay absent; historical data stays untouched."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.application import service_cleanup as cleanup
from src.application import service_upgrade as upgrade


ROOT = Path(__file__).resolve().parents[1]


def _release(root: Path, version: str, *, python: bool = True) -> Path:
    path = root / version
    path.mkdir(parents=True)
    (path / "VERSION").write_text(version + "\n")
    if python:
        runtime = path / "src/application/bot/runtime.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("# isolated Python release fixture\n")
    return path


def _no_effect(*_args, **_kwargs):
    pytest.fail("unsupported runtime reached installation, config preparation or service execution")


def test_legacy_runtime_implementation_and_packages_are_absent():
    for name in (
        "agent-runtime", "src/application/bot/pi_migration.py",
        "src/infrastructure/pi_agent_process.py", "tests/manual/pi_migration.py",
        "tests/bot_pi_test_support.py",
    ):
        assert not (ROOT / name).exists(), name
    retired = {"src.application.bot.pi_migration", "src.infrastructure.pi_agent_process"}
    for directory in (ROOT / "src", ROOT / "domain", ROOT / "scripts"):
        for path in directory.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    assert node.module not in retired, path
                elif isinstance(node, ast.Import):
                    assert not retired.intersection(alias.name for alias in node.names), path


def test_runtime_preparation_rejects_unsupported_release_before_install(tmp_path):
    target = _release(tmp_path / "releases", "1.0.0", python=False)
    with pytest.raises(upgrade.ServiceTransitionError) as error:
        upgrade._ensure_release_runtime(target_dir=target, cache_root=tmp_path / "cache",
                                        run_cmd=_no_effect, operations=[])
    assert error.value.status == "unsupported_runtime"
    assert not (tmp_path / "cache").exists()
    assert not (target / ".venv").exists()


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
def test_unsupported_target_rejected_before_config_or_symlink_change(tmp_path, monkeypatch, confirm, operation):
    releases = tmp_path / "releases"
    current = _release(releases, "1.0.1")
    version = "1.0.2" if operation == "upgrade" else "1.0.0"
    target = _release(releases, version, python=False)
    link = tmp_path / "current"
    link.symlink_to(current, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = runtime / "config.yaml"
    config.write_bytes(b"keep-authoring-unchanged\n")
    historical = runtime / "output_shared/state/pi_sessions.sqlite3"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b"unopened historical store")
    receipt = historical.with_suffix(".sqlite3.om-pi-migration.json")
    receipt.write_bytes(b"opaque historical receipt, not parsed")
    monkeypatch.setattr(upgrade, "_prepare_runtime_configs_for_release", _no_effect)
    monkeypatch.setattr(upgrade, "_run_required", _no_effect)
    monkeypatch.setattr(upgrade, "_load_service_profile", lambda _root: {})
    if operation == "upgrade":
        monkeypatch.setattr(upgrade, "service_upgrade_check", lambda **_kw: {
            "ok": True, "latest_version": version, "release_tag": "v" + version,
            "upgrade_available": True,
        })
        monkeypatch.setattr(upgrade, "_materialize_release_from_git_cache", lambda **_kw: {
            "status": "reused", "target_dir": str(target),
        })
        out = upgrade.service_upgrade(repo_root=link, runtime_root=runtime, releases_root=releases,
                                      target_version=version, confirm=confirm, restart_services=False,
                                      run_cmd=_no_effect)
    else:
        out = upgrade.service_rollback(repo_root=link, runtime_root=runtime, releases_root=releases,
                                       to_version=version, confirm=confirm, restart_services=False,
                                       run_cmd=_no_effect)
    assert out["ok"] is False
    assert out["status"] == "unsupported_runtime"
    assert out["changed"] is False
    assert link.resolve() == current
    assert config.read_bytes() == b"keep-authoring-unchanged\n"
    assert historical.read_bytes() == b"unopened historical store"
    assert receipt.read_bytes() == b"opaque historical receipt, not parsed"
    assert not (runtime / "config_backups").exists()
    assert not (target / ".venv").exists()


def test_python_readiness_does_not_open_historical_storage(tmp_path):
    release = _release(tmp_path / "releases", "1.0.0")
    old = release / "output_shared/state/pi_sessions.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"not a SQLite database; must not be probed")
    assert upgrade._runtime_readiness(release_dir=release) == {"ok": True, "runtime": "python"}
    assert old.read_bytes() == b"not a SQLite database; must not be probed"


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("historical_python", [False, True])
def test_cleanup_preserves_legacy_releases_and_opaque_history(tmp_path, confirm, historical_python):
    releases = tmp_path / "releases"
    legacy = _release(releases, "1.0.0", python=historical_python)
    expired = _release(releases, "2.0.0")
    previous = _release(releases, "3.0.0")
    active = _release(releases, "4.0.0")
    old = legacy / "output_shared/state/pi_sessions.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"historical database")
    receipt = old.with_suffix(".sqlite3.om-pi-migration.json")
    receipt.write_bytes(b"malformed historical receipt must never be parsed")
    link = tmp_path / "current"
    link.symlink_to(active, target_is_directory=True)
    out = cleanup.service_cleanup(repo_root=link, releases_root=releases,
                                  runtime_root=tmp_path / "runtime", confirm=confirm)
    assert out["ok"] is True
    assert [r["path"] for r in out["retained_legacy_releases"]] == [str(legacy)]
    assert {r["path"] for r in out["delete_releases"]} == {str(expired)}
    assert legacy.is_dir() and previous.is_dir() and active.is_dir()
    assert old.read_bytes() == b"historical database"
    assert receipt.read_bytes() == b"malformed historical receipt must never be parsed"
    assert expired.exists() is not confirm
    assert link.resolve() == active
