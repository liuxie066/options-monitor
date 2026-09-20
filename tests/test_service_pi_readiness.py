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


def _fail_lambda(message: str):
    """A seam stub that fails the test if the seam is reached."""

    def _fail(**_kwargs):
        pytest.fail(message)

    return _fail


def _upgrade(
    module,
    *,
    current: Path,
    runtime: Path,
    target: Path,
    confirm: bool,
    restart_services: bool,
):  # type: ignore[no-untyped-def]
    """Run the upgrade entrypoint with the fixture's fixed release/runtime arguments."""
    return module.service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=target.parent,
        target_version="3.5.2",
        confirm=confirm,
        restart_services=restart_services,
        preserve_activation_state=True,
    )


def _cleanup_fixture(tmp_path: Path, versions: list[str]) -> tuple[Path, list[Path], Path]:
    """Create one release dir per version (newest first) and point `current` at the newest."""
    releases = tmp_path / "releases"
    created = [_release(releases / version, version) for version in versions]
    current = tmp_path / "current"
    current.symlink_to(created[0], target_is_directory=True)
    return releases, created, current


def _cleanup(module, *, current: Path, releases: Path, **kwargs):  # type: ignore[no-untyped-def]
    """Run the cleanup entrypoint with the fixture's fixed keep/confirm arguments."""
    return module.service_cleanup(
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
        **kwargs,
    )


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
    monkeypatch.setattr(module, "_runtime_config_verify_summary", lambda **_kwargs: {"ok": True})

    def _reject_readiness(**_kwargs):
        raise module.ServiceTransitionError(
            "reverse conversion required",
            status="pi_storage_not_ready",
            remediation=["keep Agent ingress stopped"],
        )

    monkeypatch.setattr(module, "_pi_storage_readiness", _reject_readiness)

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
    monkeypatch.setattr(module, "_materialize_release_from_git_cache", _fail_lambda("preview materialized release"))
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_lambda("preview switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", _fail_lambda("preview restarted service"))

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=False, restart_services=False)

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
    monkeypatch.setattr(
        module, "_restart_services_from_loaded_profile", _fail_lambda("maintenance restarted service")
    )

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=True, restart_services=False)

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
    def _reject_readiness(**_kwargs):
        raise module.ServiceTransitionError("reverse conversion required", status="pi_storage_not_ready")

    monkeypatch.setattr(module, "_pi_storage_readiness", _reject_readiness)
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_lambda("rollback switched current"))
    monkeypatch.setattr(module, "_restart_services_from_loaded_profile", _fail_lambda("rollback restarted service"))

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

    def _fail_activation(**_kwargs):
        raise module.ServiceTransitionError("activation snapshot failed", status="service_activation_snapshot_failed")

    monkeypatch.setattr(module, "_pi_storage_readiness", _readiness)
    monkeypatch.setattr(module, "capture_preserved_timer_activation_states", _fail_activation)
    monkeypatch.setattr(module, "_switch_current_symlink", _fail_lambda("failure path switched current"))
    monkeypatch.setattr(
        module, "_restart_services_from_loaded_profile", _fail_lambda("failure path started old service")
    )

    out = _upgrade(module, current=current, runtime=runtime, target=target, confirm=True, restart_services=True)

    assert out["status"] == "service_activation_snapshot_failed"
    assert out["compensation"]["status"] == "pi_storage_not_ready"
    assert "keep Agent ingress stopped" in out["remediation"]
    assert current.resolve() == previous


def test_cleanup_keeps_receipt_runtime_outside_keep_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_cleanup as module

    releases, created, current = _cleanup_fixture(tmp_path, ["3.5.3", "3.5.2", "3.5.1", "3.5.0"])
    current_release, _, retained_release, stale_release = created
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

    out = _cleanup(module, current=current, releases=releases, runtime_root=runtime)

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

    releases, created, current = _cleanup_fixture(tmp_path, ["3.5.3", "3.5.2", "3.5.1", "3.5.0"])
    current_release, _, retained_release, stale_release = created
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

    out = _cleanup(module, current=current, releases=releases)

    assert out["status"] == "cleaned"
    assert retained_db in observed
    assert {item["version"] for item in out["kept_releases"]} == {"3.5.3", "3.5.2", "3.5.1"}
    assert retained_release.exists()
    assert not stale_release.exists()


def test_cleanup_fails_closed_when_receipt_cannot_be_validated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_cleanup as module

    releases, created, current = _cleanup_fixture(tmp_path, ["3.5.2", "3.5.1"])
    current_release, old_release = created
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(module, "pi_session_database_paths", lambda **_kwargs: (runtime / "pi_sessions.sqlite3",))

    def _reject_receipt(_path):
        raise ValueError("receipt identity mismatch")

    monkeypatch.setattr(module, "read_pi_migration_receipt", _reject_receipt)

    out = _cleanup(module, current=current, releases=releases, runtime_root=runtime)

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
