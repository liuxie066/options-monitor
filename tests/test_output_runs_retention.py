from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.output_runs_retention import parse_run_timestamp


NOW = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("name,expected", [
    ("20261105T075006Z", datetime(2026, 11, 5, 7, 50, 6, tzinfo=timezone.utc)),
    ("20261231T235959Z-abcdef", datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
    ("20240229T000000Z", datetime(2024, 2, 29, tzinfo=timezone.utc)),
    ("20260101T000000Z", datetime(2026, 1, 1, tzinfo=timezone.utc)),
])
def test_parse_run_timestamp(name, expected) -> None:
    assert parse_run_timestamp(name) == expected


@pytest.mark.parametrize("name", [
    "latest", "20260229T000000Z", "20261301T000000Z", "20261105T240000Z",
    "20261105T075006", "20261105T075006Z.bak", "backup-20261105T075006Z",
    "../20261105T075006Z", "20261105T075006Z/child", "20261105T075006Z-unrelated",
])
def test_invalid_names_are_not_timestamps(name) -> None:
    assert parse_run_timestamp(name) is None


def _run(root: Path, days: int, *, name: str | None = None) -> Path:
    path = root / "output_runs" / (name or (NOW - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ"))
    path.mkdir(parents=True)
    (path / "payload.json").write_bytes(b"x" * 4096)
    return path


def _prune(root: Path, **kwargs) -> dict:
    from src.application.output_runs_retention import prune_output_runs

    return prune_output_runs(runtime_root=root, now=NOW, **kwargs)


def test_seven_day_boundary_crosses_year_and_ignores_mtime(tmp_path: Path) -> None:
    latest, seventh, eighth = (_run(tmp_path, age) for age in (0, 7, 8))
    os.utime(eighth, (NOW.timestamp(), NOW.timestamp()))
    os.utime(seventh, (0, 0))
    result = _prune(tmp_path, confirm=True)
    assert result["ok"] is True
    assert latest.exists() and seventh.exists()
    assert not eighth.exists()
    assert result["freed_bytes"] >= 4096
    assert any(row["run_id"] == eighth.name for row in result["deleted"])


def test_dry_run_leaves_all_artifacts_untouched(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    old = _run(tmp_path, 8)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = _prune(tmp_path)
    assert result["dry_run"] is True
    assert result["deleted"] == []
    assert result["candidates"][0]["run_id"] == old.name
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_latest_is_retained_even_if_expired(tmp_path: Path) -> None:
    latest = _run(tmp_path, 10)
    old = _run(tmp_path, 11)
    result = _prune(tmp_path, confirm=True)
    assert latest.exists() and not old.exists()
    assert any(row["run_id"] == latest.name and row["reason"] == "latest_run" for row in result["skipped"])


def test_invalid_names_and_outside_paths_are_untouched(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    invalid = _run(tmp_path, 9, name="20261331T120000Z")
    for relative in ("output_shared/state/option_positions.sqlite3", "data/ledger.sqlite3",
                     "configs/config.json", "upgrade-backups/20251220T120000Z/evidence"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must remain")
    outside = tmp_path / "upgrade-backups"
    link = tmp_path / "output_runs" / "20251220T120000Z"
    link.symlink_to(outside, target_is_directory=True)
    inside_link = old / "outside"
    inside_link.symlink_to(outside, target_is_directory=True)
    result = _prune(tmp_path, confirm=True)
    assert not old.exists()
    assert invalid.exists() and link.is_symlink()
    assert any(row["run_id"] == invalid.name and row["reason"] == "invalid_run_name" for row in result["skipped"])
    for relative in ("output_shared/state/option_positions.sqlite3", "data/ledger.sqlite3",
                     "configs/config.json", "upgrade-backups/20251220T120000Z/evidence"):
        assert (tmp_path / relative).read_bytes() == b"must remain"


def test_symlinked_runs_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tmp_path / "output_runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, RuntimeError)):
        _prune(tmp_path, confirm=True)


def test_active_writer_blocks_prune_and_releases_after_exit(tmp_path: Path) -> None:
    from src.application.output_runs_retention import run_lifecycle_lock

    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    with run_lifecycle_lock(tmp_path):
        result = _prune(tmp_path, confirm=True)
        assert old.exists()
        assert result["skipped"][0]["reason"] == "runs_locked"
    assert _prune(tmp_path, confirm=True)["deleted"][0]["run_id"] == old.name
    assert not old.exists()


def test_per_artifact_lock_skips_only_locked_run(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    locked = _run(tmp_path, 9)
    other = _run(tmp_path, 10)
    lock = locked / "snapshot.json.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        result = _prune(tmp_path, confirm=True)
        assert locked.exists() and not other.exists()
        assert any(row["run_id"] == locked.name and row["reason"] == "run_locked" for row in result["skipped"])
    assert not _prune(tmp_path, confirm=True)["errors"]
    assert not locked.exists()


@pytest.mark.parametrize("kind", ["pointer", "relative_pointer", "alias", "manifest", "root_manifest", "live_claim"])
def test_referenced_runs_are_retained(tmp_path: Path, kind: str) -> None:
    _run(tmp_path, 0)
    referenced = _run(tmp_path, 9)
    unreferenced = _run(tmp_path, 10)
    state = tmp_path / "output_shared" / "state"
    state.mkdir(parents=True)
    if kind.endswith("pointer"):
        value = referenced if kind == "pointer" else referenced.relative_to(tmp_path)
        (state / "last_run_dir.txt").write_text(str(value), encoding="utf-8")
    elif kind == "alias":
        accounts = tmp_path / "output_accounts"
        accounts.mkdir()
        (accounts / "test").symlink_to(referenced, target_is_directory=True)
    elif kind == "live_claim":
        claims = state / "idempotency" / "tick_execution"
        claims.mkdir(parents=True)
        (claims / "claim.json").write_text(json.dumps({"status": "in_progress", "pid": os.getpid(), "run_id": referenced.name}))
    else:
        payload = {"source": {"relpath": f"output_runs/{referenced.name}/payload.json"}}
        if kind == "root_manifest":
            payload = {"source_root_relpath": str(referenced), "source": {"relpath": "payload.json"}}
        (state / "live_manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    result = _prune(tmp_path, confirm=True)
    assert result["ok"]
    assert referenced.exists() and not unreferenced.exists()
    assert any(row["run_id"] == referenced.name for row in result["skipped"])


def test_transitive_run_reference_is_retained(tmp_path: Path) -> None:
    newest = _run(tmp_path, 0)
    first = _run(tmp_path, 9)
    second = _run(tmp_path, 10)
    for source, target in [(newest, first), (first, second)]:
        (source / "source_manifest.json").write_text(json.dumps({"source": {"path": str(target / "payload.json")}}))
    assert _prune(tmp_path, confirm=True)["deleted"] == []
    assert first.exists() and second.exists()


def test_provenance_and_archive_copies_do_not_pin_originals(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    state = tmp_path / "output_shared" / "state"
    state.mkdir(parents=True)
    (state / "last_run.json").write_text(json.dumps({"run_id": old.name, "history": [{"run_dir": str(old)}]}))
    (state / "audit_event_latest.current.json").write_text(json.dumps({"extra": {"path": str(old)}}))
    archive = tmp_path / "output_shared" / "research" / "remote_archive" / "prod"
    archive.mkdir(parents=True)
    (archive / "inventory.latest.json").write_text(json.dumps({"source_root_relpath": str(tmp_path),
        "runs": [{"run_id": old.name, "path": str(old)}]}))
    assert _prune(tmp_path, confirm=True)["ok"]
    assert not old.exists()
    assert (archive / "inventory.latest.json").exists()


def test_latest_account_market_scan_is_retained(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    hk = _run(tmp_path, 10)
    us = _run(tmp_path, 9)
    old = _run(tmp_path, 11)
    for path, market in [(hk, "HK"), (us, "US"), (old, "HK")]:
        state = path / "accounts" / "test" / "state"
        state.mkdir(parents=True)
        (state / "candidate_snapshot_manifest.v4.json").write_text(json.dumps({"markets": [market]}))
    assert _prune(tmp_path, confirm=True)["ok"]
    assert hk.exists() and us.exists() and not old.exists()


@pytest.mark.parametrize("preserved_by", ["lock", "candidate_set"])
def test_retained_expired_run_protects_its_dependencies(tmp_path: Path, preserved_by: str) -> None:
    _run(tmp_path, 0)
    retained = _run(tmp_path, 9)
    dependency = _run(tmp_path, 10)
    (retained / "source_manifest.json").write_text(json.dumps({"source": {"path": str(dependency / "payload.json")}}))
    lock = retained / "snapshot.lock"
    with lock.open("w") as handle:
        options = {}
        if preserved_by == "lock":
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            options["candidate_ids"] = {dependency.name}
        result = _prune(tmp_path, confirm=True, **options)
    assert result["ok"]
    assert retained.exists() and dependency.exists()
    assert result["deleted"] == []


def test_service_keep_count_retains_transitive_dependencies(tmp_path: Path) -> None:
    from src.application.service_cleanup import _output_run_cleanup_plan

    _run(tmp_path, 0)
    retained = _run(tmp_path, 9)
    dependency = _run(tmp_path, 10)
    (retained / "source_manifest.json").write_text(json.dumps({"source": {"path": str(dependency / "payload.json")}}))
    plan = _output_run_cleanup_plan(runtime_root=tmp_path, keep_days=7, keep_count=2, now=NOW)
    assert plan["delete_runs"] == []
    assert any(row["run_id"] == dependency.name and row["reason"] == "referenced_path" for row in plan["protected_runs"])


def test_reference_inventory_failure_blocks_all_deletes(tmp_path: Path) -> None:
    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    state = tmp_path / "output_shared" / "state"
    state.mkdir(parents=True)
    (state / "broken_manifest.json").write_text("{", encoding="utf-8")
    result = _prune(tmp_path, confirm=True)
    assert not result["ok"] and old.exists()
    assert result["errors"][0]["reason"] == "reference_inventory_failed"


def test_audit_intent_failure_prevents_deletion(tmp_path: Path, monkeypatch) -> None:
    from src.application import output_runs_retention as mod

    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    original = mod._audit
    def fail_intent(root, *, action, **kwargs):
        if action == "delete_started":
            raise OSError("disk full")
        return original(root, action=action, **kwargs)
    monkeypatch.setattr(mod, "_audit", fail_intent)
    result = _prune(tmp_path, confirm=True)
    assert not result["ok"] and old.exists()
    assert result["freed_bytes"] == 0


def test_tick_lifecycle_cleans_after_writers_unwind(tmp_path: Path, monkeypatch) -> None:
    from src.application import output_runs_retention as mod

    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    config = tmp_path / "config.us.json"
    config.write_text("{}")
    original = mod.prune_output_runs
    monkeypatch.setattr(mod, "prune_output_runs", lambda **kwargs: original(**{**kwargs, "now": NOW}))
    with mod.tick_run_lifecycle(runtime_root=tmp_path, config_path=config):
        assert old.exists()
        assert _prune(tmp_path)["skipped"][0]["reason"] == "runs_locked"
    assert not old.exists()


def test_reader_before_first_cleanup_blocks_delete_without_writing(tmp_path: Path) -> None:
    from src.application.output_runs_retention import run_reader_lease

    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    @run_reader_lease(lambda: tmp_path)
    def read():
        assert not (tmp_path / "locks").exists()
        receipt = _prune(tmp_path, confirm=True)
        assert receipt["skipped"][0]["reason"] == "runs_locked"
        assert old.exists()
    read()
    assert _prune(tmp_path, confirm=True)["ok"]
    assert not old.exists()


def test_cron_prunes_only_after_child_has_finished(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    from src.application import output_runs_retention as retention
    from src.application.tick_cron import run_tick_cron

    _run(tmp_path, 0)
    old = _run(tmp_path, 9)
    config = tmp_path / "config.us.json"
    config.write_text("{}")
    original = retention.prune_output_runs
    order = []
    def prune(**kwargs):
        order.append("prune")
        assert order == ["child", "prune"]
        return original(**{**kwargs, "now": NOW})
    monkeypatch.setattr(retention, "prune_output_runs", prune)
    def child(command, **kwargs):
        order.append("child")
        assert old.exists()
        assert original(runtime_root=tmp_path, now=NOW)["skipped"][0]["reason"] == "runs_locked"
        return subprocess.CompletedProcess(command, 0)
    assert run_tick_cron(market="us", config_path=str(config), runtime_root=tmp_path,
                         lock_path=str(tmp_path / "tick.lock"), preflight_config_fn=None,
                         run_cmd=child, environ={}) == 0
    assert order == ["child", "prune"]
    assert not old.exists()


def test_tick_lifecycle_preserves_original_failure_and_unlocks(tmp_path: Path, monkeypatch, capsys) -> None:
    from src.application import output_runs_retention as mod

    def failed_prune(**kwargs):
        raise OSError("maintenance failed")
    monkeypatch.setattr(mod, "prune_output_runs", failed_prune)
    with pytest.raises(RuntimeError, match="scan failed"):
        with mod.tick_run_lifecycle(runtime_root=tmp_path, config_path=tmp_path / "missing.json"):
            raise RuntimeError("scan failed")
    assert "OUTPUT_RUNS_RETENTION_FAILED" in capsys.readouterr().err
    with mod.run_lifecycle_lock(tmp_path, exclusive=True):
        pass
