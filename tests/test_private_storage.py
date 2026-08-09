from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from domain.storage.repositories import state_repo
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.inbox import enqueue_trade_payload
from src.infrastructure.private_storage import connect_private_sqlite, ensure_private_file


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sensitive_sqlite_and_audit_artifacts_ignore_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "private" / "inbound.sqlite3"
        with connect_private_sqlite(database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE secret_payload (value TEXT NOT NULL)")
            connection.execute("INSERT INTO secret_payload(value) VALUES ('private-marker')")
            connection.commit()
            assert _mode(database) == 0o600
            assert _mode(database.parent) == 0o700
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                if sidecar.exists():
                    assert _mode(sidecar) == 0o600

        audit_path = state_repo.append_shared_audit_jsonl(
            tmp_path,
            "audit_events.jsonl",
            {"event_type": "private", "action": "tested"},
        )
        assert _mode(audit_path) == 0o600
        assert _mode(audit_path.parent) == 0o700
    finally:
        os.umask(previous_umask)


def test_sensitive_file_helper_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    sensitive_dir = tmp_path / "private"
    sensitive_dir.mkdir()
    link = sensitive_dir / "audit.sqlite3"
    link.symlink_to(outside)

    with pytest.raises(OSError, match="must not be a symlink"):
        ensure_private_file(link)

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_option_ledger_ignores_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "ledger" / "option_positions.sqlite3"
        repo = SQLiteOptionPositionsRepository(database)
        assert repo.count_trade_events() == 0

        assert _mode(database.parent) == 0o700
        assert _mode(database) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                assert _mode(sidecar) == 0o600
    finally:
        os.umask(previous_umask)


def test_public_cli_entrypoints_set_private_umask() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for entrypoint in (repo_root / "om", repo_root / "om-agent"):
        lines = entrypoint.read_text(encoding="utf-8").splitlines()
        assert "umask 077" in lines[:5]


def test_trade_inbox_ignores_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "inbox" / "trade_inbox.sqlite3"
        enqueue_trade_payload(
            database,
            payload={"deal_id": "synthetic-deal", "account": "test"},
            source="test",
            broker_deal_key="futu:test:999000000000000001:synthetic-deal",
        )

        assert _mode(database.parent) == 0o700
        assert _mode(database) == 0o600
    finally:
        os.umask(previous_umask)
