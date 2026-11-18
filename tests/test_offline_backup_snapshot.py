from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from src.application.offline_backup_snapshot import (
    OfflineBackupError,
    stage_offline_backup,
    verify_offline_backup,
)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "stopped"
    root.mkdir()
    (root / "state").mkdir()
    (root / "config.yaml").write_bytes(b"mode: isolated\n")
    with closing(sqlite3.connect(root / "state/ledger.sqlite3")) as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, amount INTEGER)")
        db.execute("INSERT INTO events VALUES (12, 31)")
        db.commit()
    return root


def _stage(source: Path, target: Path, *, budget: int = 100_000) -> str:
    return stage_offline_backup(
        source=source, destination=target, max_bytes=budget,
        files={"config.yaml": "file", "state/ledger.sqlite3": "sqlite"},
    )


def test_offline_copy_verifies_independently(source: Path, tmp_path: Path) -> None:
    package = tmp_path / "package"
    identity = _stage(source, package)
    other = tmp_path / "other"
    shutil.copytree(package, other)
    (source / "config.yaml").write_bytes(b"changed after backup")
    verify_offline_backup(root=other, manifest_sha256=identity)

    assert (other / "config.yaml").read_bytes() == b"mode: isolated\n"
    with closing(sqlite3.connect(other / "state/ledger.sqlite3")) as db:
        assert db.execute("SELECT * FROM events").fetchall() == [(12, 31)]
    assert os.stat(package).st_mode & 0o777 == 0o700
    assert os.stat(package / "state/ledger.sqlite3").st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _stage(source, package)


@pytest.mark.parametrize("failure", ["same_size", "manifest", "unexpected_file", "alias"])
def test_readback_rejects_invalid_copy(source: Path, tmp_path: Path, failure: str) -> None:
    package = tmp_path / "package"
    identity = _stage(source, package)
    if failure == "same_size":
        (package / "config.yaml").write_bytes(b"mode: incorrect")
    elif failure == "manifest":
        (package / "manifest.json").write_bytes(b"{}")
    elif failure == "unexpected_file":
        (package / "unlisted").touch()
    else:
        path = package / "config.yaml"
        path.unlink()
        path.symlink_to(source / "config.yaml")
    with pytest.raises(OfflineBackupError):
        verify_offline_backup(root=package, manifest_sha256=identity)


def test_live_wal_budget_and_overlapping_destination_are_rejected(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "package"
    with pytest.raises(OfflineBackupError, match="budget"):
        _stage(source, target, budget=2)
    with pytest.raises(OfflineBackupError, match="separate"):
        _stage(source, source / "package")
    with closing(sqlite3.connect(source / "state/ledger.sqlite3")) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("INSERT INTO events VALUES (13, 2)")
        db.commit()
        with pytest.raises(OfflineBackupError, match="checkpointed"):
            _stage(source, target)
    assert not target.exists()


def test_invalid_database_never_publishes_manifest(source: Path, tmp_path: Path) -> None:
    (source / "state/ledger.sqlite3").write_bytes(b"not a database")
    target = tmp_path / "package"
    with pytest.raises(sqlite3.DatabaseError):
        _stage(source, target)
    assert not (target / "manifest.json").exists()


def test_sqlite_cannot_bypass_checkpoint_guard_as_an_ordinary_file(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    with closing(sqlite3.connect(source / "state/ledger.sqlite3")) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("INSERT INTO events VALUES (13, 91)")
        db.commit()
        with pytest.raises(OfflineBackupError, match="declared as sqlite"):
            stage_offline_backup(
                source=source, destination=target,
                files={"state/ledger.sqlite3": "file"}, max_bytes=100_000,
            )
    assert not target.exists()
