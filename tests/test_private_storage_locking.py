"""Standalone Linux regression: python -m unittest discover -s tests -p test_private_storage_locking.py."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.infrastructure import private_storage


_LOCK_PROBE = """
import errno, fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, int(sys.argv[2]), os.SEEK_SET)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        print('held')
    else:
        print('free')
finally:
    os.close(fd)
"""


@unittest.skipUnless(sys.platform == "linux", "Linux POSIX SQLite lock regression")
class SQLiteLockPreservationTests(unittest.TestCase):
    def assert_lock(self, path: Path, offset: int, expected: str = "held") -> None:
        result = subprocess.run(
            [sys.executable, "-c", _LOCK_PROBE, str(path), str(offset)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), expected)

    def test_existing_database_metadata_and_second_connection_preserve_locks(self) -> None:
        for journal_mode in ("WAL", "DELETE"):
            with self.subTest(journal_mode=journal_mode), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "database.sqlite3"
                first = private_storage.connect_private_sqlite(database)
                second = None
                try:
                    self.assertEqual(first.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0], journal_mode.lower())
                    first.execute("CREATE TABLE entries (value TEXT)")
                    first.commit()
                    first.execute("BEGIN IMMEDIATE")
                    first.execute("INSERT INTO entries VALUES ('durable')")
                    lock_path = Path(f"{database}-shm") if journal_mode == "WAL" else database
                    offset = 120 if journal_mode == "WAL" else 0x40000001
                    self.assert_lock(lock_path, offset)
                    for candidate in (database, *database.parent.glob("database.sqlite3-*")):
                        candidate.chmod(0o666)
                    private_storage.secure_sqlite_artifacts(database)
                    self.assert_lock(lock_path, offset)
                    second = private_storage.connect_private_sqlite(database)
                    self.assertEqual(second.execute("SELECT count(*) FROM entries").fetchone(), (0,))
                    self.assert_lock(lock_path, offset)
                    second.close()
                    second = None
                    self.assert_lock(lock_path, offset)
                    first.commit()
                    self.assert_lock(lock_path, offset, "free")
                    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as reader:
                        self.assertEqual(reader.execute("SELECT value FROM entries").fetchall(), [("durable",)])
                    for candidate in (database, *database.parent.glob("database.sqlite3-*")):
                        self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
                finally:
                    if second is not None:
                        second.close()
                    first.close()

    def test_connection_immediately_after_publication_retains_its_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "database.sqlite3"
            real_link = os.link
            writer = None

            def publish_then_connect(source, target, **kwargs):
                nonlocal writer
                real_link(source, target, **kwargs)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as reader:
                    self.assertEqual(reader.execute("SELECT count(*) FROM sqlite_master").fetchone(), (0,))
                writer = sqlite3.connect(target)
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE entries (value TEXT)")
                writer.commit()
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO entries VALUES ('winner')")
                self.assert_lock(Path(f"{target}-shm"), 120)

            try:
                with patch.object(private_storage.os, "link", publish_then_connect):
                    connection = private_storage.connect_private_sqlite(database)
                try:
                    self.assert_lock(Path(f"{database}-shm"), 120)
                    self.assertEqual(connection.execute("SELECT count(*) FROM entries").fetchone(), (0,))
                    writer.commit()
                    self.assertEqual(connection.execute("SELECT value FROM entries").fetchone(), ("winner",))
                    self.assertFalse(list(database.parent.glob(".*.tmp")))
                finally:
                    connection.close()
            finally:
                if writer is not None:
                    writer.close()


if __name__ == "__main__":
    unittest.main()
