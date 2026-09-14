from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _release(path: Path, version: str) -> Path:
    path.mkdir(parents=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (path / "agent-runtime").mkdir()
    return path


def _upgrade_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    releases = tmp_path / "releases"
    previous = _release(releases / "3.5.1", "3.5.1")
    target = _release(releases / "3.5.2", "3.5.2")
    current = tmp_path / "current"
    current.symlink_to(previous, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return current, previous, target, runtime


def _stub_upgrade_preparation(monkeypatch: pytest.MonkeyPatch, module, target: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        module,
        "service_upgrade_check",
        lambda **_kwargs: {"ok": True, "latest_version": "3.5.2", "release_tag": "v3.5.2"},
    )
    monkeypatch.setattr(
        module,
        "_materialize_release_from_git_cache",
        lambda **_kwargs: {"status": "reused", "target_dir": str(target)},
    )
    monkeypatch.setattr(module, "_ensure_release_runtime", lambda **_kwargs: {"status": "ready"})
    monkeypatch.setattr(module, "_run_required", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        module,
        "_prepare_runtime_configs_for_release",
        lambda **_kwargs: {"status": "prepared", "items": []},
    )
    monkeypatch.setattr(module, "_commit_prepared_runtime_configs", lambda **_kwargs: {"status": "committed"})
    monkeypatch.setattr(module, "_validate_committed_runtime_configs", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_load_service_profile", lambda _runtime: {})


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _stage_actual_target_with_old_installer(
    *,
    tmp_path: Path,
    old_release: Path,
) -> Path:
    from test_install_script import _installer_env

    source = Path(__file__).resolve().parents[1]
    installer_root = tmp_path / "installer"
    env = _installer_env(installer_root)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    _write_executable(
        fake_bin / "git",
        f"""#!{sys.executable}
import os
import shutil
import sys
from pathlib import Path

source = Path(os.environ["PI_TEST_CLONE_SOURCE"])
destination = Path(sys.argv[-1])
destination.mkdir(parents=True)
for name in ("src", "domain", "scripts", "requirements", "constraints", "agent-runtime"):
    shutil.copytree(source / name, destination / name, symlinks=True)
for name in ("om", "om-agent", "requirements.txt", "constraints.txt", "pyproject.toml"):
    os.link(source / name, destination / name)
version = destination / "VERSION"
version.write_text("3.5.2\\n", encoding="utf-8")
smoke = destination / "scripts" / "pi_runtime_smoke.sh"
smoke.unlink()
smoke.write_text("#!/usr/bin/env bash\\nset -euo pipefail\\n[[ -x \\\"$4\\\" ]]\\n", encoding="utf-8")
smoke.chmod(0o755)
""",
    )
    env["PI_TEST_CLONE_SOURCE"] = str(source)
    prefix = tmp_path / "private-upgrade-control"
    before_link = (tmp_path / "production" / "current").resolve()
    completed = subprocess.run(
        [
            "bash",
            str(old_release / "scripts" / "install.sh"),
            "--version",
            "v3.5.2",
            "--prefix",
            str(prefix),
            "--repo-url",
            "https://example.invalid/options-monitor.git",
            "--no-install-cli",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert (tmp_path / "production" / "current").resolve() == before_link
    target = prefix / "releases" / "v3.5.2"
    shutil.rmtree(target / ".venv")
    (target / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    return target


def _target_cli_env(target: Path, *, extra_python_path: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["OM_PYTHON"] = str(target / ".venv" / "bin" / "python")
    if extra_python_path is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(extra_python_path)
    return env


def _run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(completed.stderr + completed.stdout) from exc
    return completed, payload


def _isolated_update_python(tmp_path: Path) -> Path:
    """Keep target wrapper/argparse/application real; substitute external effects only."""
    bootstrap = tmp_path / "isolated-update-python"
    _write_executable(bootstrap, f'''#!{sys.executable}
import atexit
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

if sys.argv[1:2] == ["-c"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
assert sys.argv[1:3] == ["-m", "src.interfaces.cli.main"]
import src.application.service_upgrade as module

target = Path.cwd()
assert Path(module.__file__).resolve() == target / "src/application/service_upgrade.py"
calls = []
def run_service(command, **kwargs):
    command = list(command)
    calls.append(command)
    output = "enabled\\n" if "is-enabled" in command else "active\\n" if "is-active" in command else "ok\\n"
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

def materialize(**kwargs):
    destination = kwargs["target_dir"]
    if not destination.exists():
        shutil.copytree(target, destination, symlinks=True, copy_function=os.link)
    return {{"status": "materialized", "target_dir": str(destination)}}

module.service_upgrade_check = lambda **kwargs: {{"ok": True, "latest_version": "3.5.2", "release_tag": "v3.5.2"}}
module._materialize_release_from_git_cache = materialize
module._ensure_release_runtime = lambda **kwargs: {{"status": "ready"}}
module._run_required = lambda *args, **kwargs: {{}}
module._prepare_runtime_configs_for_release = lambda **kwargs: {{"status": "prepared", "items": []}}
module._commit_prepared_runtime_configs = lambda **kwargs: {{"status": "committed"}}
module._validate_committed_runtime_configs = lambda **kwargs: []
module._load_service_profile = lambda runtime: {{"service_provider": "systemd", "restart": {{"requires_sudo": False, "services": ["options-monitor-feishu-ws.service"]}}}}
module.service_drift = lambda **kwargs: {{"summary": {{"status": "ok"}}}}
module._post_upgrade_service_health = lambda **kwargs: {{"ok": True, "status": "ok"}}
module.service_upgrade.__kwdefaults__["run_cmd"] = run_service
if os.environ.get("PI_TEST_FAIL_ACTIVATION") == "1":
    def fail_activation(**kwargs):
        raise module.ServiceTransitionError("activation snapshot failed", status="service_activation_snapshot_failed")
    module.capture_preserved_timer_activation_states = fail_activation

argv = sys.argv[3:]
def record():
    Path(os.environ["PI_TEST_UPDATE_AUDIT"]).write_text(json.dumps({{
        "argv": argv, "cwd": str(target), "module": module.__file__,
        "sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
        "calls": calls,
    }}))
atexit.register(record)
sys.argv = ["src.interfaces.cli.main", *argv]
runpy.run_module("src.interfaces.cli.main", run_name="__main__")
''')
    return bootstrap


def test_pi_readiness_checks_custom_and_release_local_session_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    runtime = tmp_path / "runtime"
    old = _release(tmp_path / "releases" / "3.5.1", "3.5.1")
    target = _release(tmp_path / "releases" / "3.5.2", "3.5.2")
    custom_db = tmp_path / "custom" / "pi_sessions.sqlite3"
    old_db = old / "output_shared" / "state" / "pi_sessions.sqlite3"
    target_db = target / "output_shared" / "state" / "pi_sessions.sqlite3"
    for path in (custom_db, old_db, target_db):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(custom_db.with_name("inbound.sqlite3")))
    observed: list[tuple[Path, Path]] = []

    def _assert_ready(pi_db: Path, runtime_dir: Path) -> dict[str, object]:
        observed.append((Path(pi_db), Path(runtime_dir)))
        return {"ok": True, "status": "ready", "database": str(pi_db)}

    monkeypatch.setattr(module, "assert_pi_storage_ready", _assert_ready)

    out = module._pi_storage_readiness(  # noqa: SLF001 - transition safety contract
        runtime_root=runtime,
        repo_root=old,
        runtime_dir=target / "agent-runtime",
        release_dirs=(old, target),
    )

    assert out["ok"] is True
    assert {item[0] for item in observed} == {custom_db, old_db, target_db}
    assert {item[1] for item in observed} == {target / "agent-runtime"}


def test_pi_readiness_requires_explicit_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    monkeypatch.delenv("OM_INBOUND_AUDIT_DB", raising=False)
    monkeypatch.setattr(module, "assert_pi_storage_ready", lambda *_args: {"status": "ready"})

    with pytest.raises(module.ServiceTransitionError, match="readiness rejected"):
        module._pi_storage_readiness(  # noqa: SLF001 - malformed helper result proof
            runtime_root=tmp_path / "runtime",
            repo_root=tmp_path / "repo",
            runtime_dir=tmp_path / "target" / "agent-runtime",
            release_dirs=(),
        )


def test_upgrade_verify_reports_pi_storage_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, _target, runtime = _upgrade_fixture(tmp_path)
    (runtime / "config.us.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(module, "_load_service_profile", lambda _runtime: {})
    monkeypatch.setattr(
        module,
        "_runtime_config_verify_summary",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "_pi_storage_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(
            module.ServiceTransitionError(
                "reverse conversion required",
                status="pi_storage_not_ready",
                remediation=["keep Agent ingress stopped"],
            )
        ),
    )

    out = module.service_upgrade_verify(
        repo_root=current,
        runtime_root=runtime,
        check_latest=False,
    )

    assert out["ok"] is False
    assert out["status"] == "attention_required"
    assert out["repo_root_resolved"] == str(previous)
    assert out["pi_storage_readiness"] == {
        "ok": False,
        "status": "pi_storage_not_ready",
        "error": "reverse conversion required",
        "remediation": ["keep Agent ingress stopped"],
    }


def test_upgrade_preview_is_read_only_and_keeps_services_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    monkeypatch.setattr(module, "_pi_storage_readiness", lambda **_kwargs: {"ok": True, "stores": []})
    monkeypatch.setattr(module, "_materialize_release_from_git_cache", lambda **_kwargs: pytest.fail("preview materialized release"))
    monkeypatch.setattr(module, "_switch_current_symlink", lambda **_kwargs: pytest.fail("preview switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", lambda **_kwargs: pytest.fail("preview restarted service"))

    out = module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=target.parent,
        target_version="3.5.2",
        confirm=False,
        restart_services=False,
        preserve_activation_state=True,
    )

    assert out["status"] == "dry_run"
    assert current.resolve() == previous


def test_upgrade_reads_target_store_before_switch_and_does_not_restart_in_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    events: list[str] = []
    original_switch = module._switch_current_symlink  # noqa: SLF001

    def _readiness(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["runtime_dir"] == target / "agent-runtime"
        events.append("readiness")
        return {"ok": True, "stores": []}

    def _switch(**kwargs):  # type: ignore[no-untyped-def]
        events.append("switch")
        original_switch(**kwargs)

    monkeypatch.setattr(module, "_pi_storage_readiness", _readiness)
    monkeypatch.setattr(module, "_switch_current_symlink", _switch)
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", lambda **_kwargs: pytest.fail("maintenance restarted service"))

    out = module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=target.parent,
        target_version="3.5.2",
        confirm=True,
        restart_services=False,
        preserve_activation_state=True,
    )

    assert out["status"] == "upgraded"
    assert events == ["readiness", "switch"]
    assert current.resolve() == target


def test_rollback_rejects_incompatible_store_before_switch_or_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    current, old, new, runtime = _upgrade_fixture(tmp_path)
    current.unlink()
    current.symlink_to(new, target_is_directory=True)
    monkeypatch.setattr(
        module,
        "_prepare_runtime_configs_for_release",
        lambda **_kwargs: {"status": "prepared", "items": []},
    )
    monkeypatch.setattr(
        module,
        "_pi_storage_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(
            module.ServiceTransitionError("reverse conversion required", status="pi_storage_not_ready")
        ),
    )
    monkeypatch.setattr(module, "_switch_current_symlink", lambda **_kwargs: pytest.fail("rollback switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", lambda **_kwargs: pytest.fail("rollback restarted service"))

    out = module.service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=old.parent,
        to_version="3.5.1",
        confirm=True,
    )

    assert out["status"] == "pi_storage_not_ready"
    assert out["changed"] is False
    assert current.resolve() == new


def test_failed_compensation_never_restores_or_starts_incompatible_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    current.unlink()
    current.symlink_to(target, target_is_directory=True)

    readiness_call: dict[str, object] = {}

    def _reject_readiness(**kwargs):  # type: ignore[no-untyped-def]
        readiness_call.update(kwargs)
        raise module.ServiceTransitionError(
            "reverse conversion required",
            status="pi_storage_not_ready",
            remediation=["keep Agent ingress stopped"],
        )

    monkeypatch.setattr(
        module,
        "_pi_storage_readiness",
        _reject_readiness,
    )
    monkeypatch.setattr(module, "_switch_current_symlink", lambda **_kwargs: pytest.fail("compensation restored old runtime"))
    monkeypatch.setattr(module, "service_drift", lambda **_kwargs: pytest.fail("compensation reconciled old services"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", lambda **_kwargs: pytest.fail("compensation started old service"))

    out = module._compensate_service_transition(  # noqa: SLF001 - exact failure boundary
        repo_link=current,
        previous_dir=previous,
        transition_dir=target,
        runtime_root=runtime,
        previous_profile={"service_provider": "systemd"},
        config_commit={},
        restart_services=True,
        activation_policy="preserve-existing",
        preserved_activation_states={},
        run_cmd=lambda *_args, **_kwargs: pytest.fail("unexpected service command"),
        operations=[],
    )

    assert out["status"] == "pi_storage_not_ready"
    assert out["symlink_restored"] is False
    assert out["restarted_services"] == []
    assert set(readiness_call["release_dirs"]) == {previous, target}
    assert current.resolve() == target


def test_post_publication_failure_before_switch_does_not_resume_old_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_upgrade as module

    current, previous, target, runtime = _upgrade_fixture(tmp_path)
    _stub_upgrade_preparation(monkeypatch, module, target)
    monkeypatch.setattr(
        module,
        "_load_service_profile",
        lambda _runtime: {"service_provider": "systemd"},
    )

    def _readiness(**kwargs):  # type: ignore[no-untyped-def]
        if kwargs["runtime_dir"] == target / "agent-runtime":
            return {"ok": True, "stores": [{"receipt_phase": "published"}]}
        raise module.ServiceTransitionError(
            "reverse conversion required",
            status="pi_storage_not_ready",
            remediation=["keep Agent ingress stopped"],
        )

    monkeypatch.setattr(module, "_pi_storage_readiness", _readiness)
    monkeypatch.setattr(
        module,
        "capture_preserved_timer_activation_states",
        lambda **_kwargs: (_ for _ in ()).throw(
            module.ServiceTransitionError(
                "activation snapshot failed",
                status="service_activation_snapshot_failed",
            )
        ),
    )
    monkeypatch.setattr(
        module,
        "_switch_current_symlink",
        lambda **_kwargs: pytest.fail("failure path switched current"),
    )
    monkeypatch.setattr(
        module,
        "_restart_services_from_loaded_profile",
        lambda **_kwargs: pytest.fail("failure path started old service"),
    )

    out = module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=target.parent,
        target_version="3.5.2",
        confirm=True,
        restart_services=True,
        preserve_activation_state=True,
    )

    assert out["status"] == "service_activation_snapshot_failed"
    assert out["compensation"]["status"] == "pi_storage_not_ready"
    assert "keep Agent ingress stopped" in out["remediation"]
    assert current.resolve() == previous


def test_cleanup_keeps_receipt_runtime_outside_keep_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_cleanup as module

    releases = tmp_path / "releases"
    current_release = _release(releases / "3.5.3", "3.5.3")
    _release(releases / "3.5.2", "3.5.2")
    retained_release = _release(releases / "3.5.1", "3.5.1")
    stale_release = _release(releases / "3.5.0", "3.5.0")
    current = tmp_path / "current"
    current.symlink_to(current_release, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pi_db = runtime / "pi_sessions.sqlite3"
    monkeypatch.setattr(module, "pi_session_database_paths", lambda **_kwargs: (pi_db,))
    monkeypatch.setattr(module, "read_pi_migration_receipt", lambda _path: {"phase": "published"})
    monkeypatch.setattr(
        module,
        "retained_pi_runtime_paths",
        lambda _receipt: (retained_release / "agent-runtime",),
    )

    out = module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        runtime_root=runtime,
        keep_releases=2,
        confirm=True,
    )

    assert out["status"] == "cleaned"
    assert {item["version"] for item in out["kept_releases"]} == {"3.5.3", "3.5.2", "3.5.1"}
    assert out["pi_retained_releases"] == [{"path": str(retained_release), "version": "3.5.1"}]
    assert retained_release.exists()
    assert not stale_release.exists()


def test_cleanup_without_runtime_root_still_keeps_release_local_receipt_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.service_cleanup as module
    from src.application.bot.pi_migration import pi_migration_receipt_path

    releases = tmp_path / "releases"
    current_release = _release(releases / "3.5.3", "3.5.3")
    _release(releases / "3.5.2", "3.5.2")
    retained_release = _release(releases / "3.5.1", "3.5.1")
    stale_release = _release(releases / "3.5.0", "3.5.0")
    current = tmp_path / "current"
    current.symlink_to(current_release, target_is_directory=True)
    retained_db = retained_release / "output_shared" / "state" / "pi_sessions.sqlite3"
    retained_db.parent.mkdir(parents=True)
    pi_migration_receipt_path(retained_db).write_text("{}\n", encoding="utf-8")
    observed: list[Path] = []

    def _read_receipt(pi_db: Path):  # type: ignore[no-untyped-def]
        observed.append(pi_db)
        return {"phase": "published"} if pi_db == retained_db else None

    monkeypatch.setattr(module, "read_pi_migration_receipt", _read_receipt)
    monkeypatch.setattr(
        module,
        "retained_pi_runtime_paths",
        lambda _receipt: (retained_release / "agent-runtime",),
    )

    out = module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
    )

    assert out["status"] == "cleaned"
    assert retained_db in observed
    assert {item["version"] for item in out["kept_releases"]} == {"3.5.3", "3.5.2", "3.5.1"}
    assert retained_release.exists()
    assert not stale_release.exists()


def test_cleanup_fails_closed_when_receipt_cannot_be_validated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_cleanup as module

    releases = tmp_path / "releases"
    current_release = _release(releases / "3.5.2", "3.5.2")
    old_release = _release(releases / "3.5.1", "3.5.1")
    current = tmp_path / "current"
    current.symlink_to(current_release, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(module, "pi_session_database_paths", lambda **_kwargs: (runtime / "pi_sessions.sqlite3",))
    monkeypatch.setattr(
        module,
        "read_pi_migration_receipt",
        lambda _path: (_ for _ in ()).throw(ValueError("receipt identity mismatch")),
    )

    out = module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        runtime_root=runtime,
        keep_releases=2,
        confirm=True,
    )

    assert out["status"] == "pi_retention_unresolved"
    assert out["changed"] is False
    assert old_release.exists()


def test_python_runtime_preserves_legacy_store_without_node(tmp_path, monkeypatch):
    from src.application import service_upgrade as module
    release = tmp_path / "release"
    marker = release / "src/application/bot/runtime.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# Python Bot")
    database = tmp_path / "pi_sessions.sqlite3"
    database.write_bytes(b"legacy store is never opened")
    monkeypatch.setattr(module, "assert_pi_storage_ready", lambda *a: pytest.fail("Python Bot opened Pi store"))
    observed = []
    monkeypatch.setattr(module, "_run_required", lambda command, **kwargs: observed.append(command) or {})
    assert module._ensure_pi_runtime(release, lambda *a: None, []) == {"runtime":"python", "ok":True}
    assert not any("node" in arg or "npm" in arg for command in observed for arg in command)
    ready = module._pi_storage_readiness(runtime_root=tmp_path, repo_root=release,
                                        runtime_dir=release/"agent-runtime", release_dirs=(release,))
    assert ready["ok"] and ready["runtime"] == "python"
    assert database.read_bytes() == b"legacy store is never opened"
