from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.repository_schema import PositionLotRecord


def test_final_wal_copy_without_sidecars_reopens(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    SQLiteOptionPositionsRepository(source)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    copied = tmp_path / "copied.sqlite3"
    shutil.copyfile(source, copied)
    reopened = SQLiteOptionPositionsRepository(copied)

    assert reopened.count_position_lots() == 0


def test_active_wal_schema_is_not_hidden_by_the_settled_copy_read_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.sqlite3"
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE position_lots "
            "(record_id TEXT PRIMARY KEY, fields_json TEXT NOT NULL)"
        )
        writer.commit()
        assert Path(f"{path}-wal").exists()
        with sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True) as stale:
            tables = {
                str(row[0])
                for row in stale.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "position_lots" not in tables

        with pytest.raises(RuntimeError, match="legacy schema"):
            SQLiteOptionPositionsRepository(path, initialize=False)


@pytest.mark.parametrize(
    "position_lots_ddl",
    [
        "CREATE TABLE position_lots (record_id TEXT PRIMARY KEY, fields_json TEXT NOT NULL)",
        "CREATE TABLE position_lots (lot_id TEXT PRIMARY KEY, fields_json TEXT NOT NULL)",
    ],
)
def test_legacy_or_partial_store_is_rejected_without_mutation(
    tmp_path: Path,
    position_lots_ddl: str,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(position_lots_ddl)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="legacy|unsupported or partial") as error:
        SQLiteOptionPositionsRepository(path)

    message = str(error.value)
    assert "lot-identity-migration inventory" in message
    assert "`verify`" in message
    assert "separately authorized historical migration code" in message
    assert "apply" not in message
    assert path.read_bytes() == before
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal", ".writer.lock"))


def test_fresh_store_reopens_and_round_trips_a_lot(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(path)
    repo = SQLiteOptionPositionsRepository(path)
    record = PositionLotRecord(
        lot_id="stock-lot-1",
        fields={"asset_type": "stock", "contract_key": {"account": "lx"}},
    )

    repo.replace_position_lots([record])

    assert repo.get_position_lot_fields(record.lot_id) == record.fields
