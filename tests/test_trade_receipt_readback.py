from __future__ import annotations

from tests.ledger_sqlite_test_support import connect_ledger_fixture

import sqlite3
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import open_trade_reconciliation_evidence_repo
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _published_shape(lots: list[dict]) -> list[dict]:
    """The three keys the receipt evidence publishes, out of a repo lot record.

    ``list_position_lots`` carries more than the receipt surface does: A3 gave it
    the five derived columns and the ``rowid`` for the comparison face, while the
    evidence surface's shape is a pinned published contract (asserted below).
    Comparing the shared facts is the invariant these tests are about.
    """
    return [{key: lot[key] for key in ("record_id", "lot_id", "fields")} for lot in lots]


def _event(event_id: str) -> TradeEvent:
    return TradeEvent(
        event_id=event_id, event_type="open", event_time_ms=1_000, contracts=1, price=2.5, currency="USD",
        source="test", multiplier=100, lot_id=f"lot-{event_id}",
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
                                             option_type="put", strike=100, expiration_ymd="2026-09-18"),
        # §9.2 step 3: the short put side travels as the trade side.
        raw_payload={"side": "sell", "broker_deal_id": event_id},
    )


def _minimal_database(path: Path) -> None:
    with connect_ledger_fixture(path) as conn:
        conn.execute("CREATE TABLE trade_events (event_id TEXT, event_json TEXT, trade_time_ms INTEGER)")
        conn.execute(
            "CREATE TABLE position_lots (lot_id TEXT, account TEXT, fields_json TEXT, "
            "source_event_id TEXT, strike REAL, multiplier REAL, updated_at_ms INTEGER)"
        )


def test_receipt_readback_absence_requires_initialized_ledger(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(database)
    evidence = open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()
    assert evidence == {"trade_events": [], "position_lots": []}


def test_receipt_readback_returns_application_events_and_published_lots(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(database)
    run_position_projection_forced_full(repo, [_event("deal-1")])

    evidence = open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()

    event = evidence["trade_events"][0]
    assert event["event_id"] == "deal-1"
    assert event["account"] == "lx"
    assert event["symbol"] == "NVDA"
    assert event["position_effect"] == "open"
    assert event["raw_payload"]["broker_deal_id"] == "deal-1"
    assert evidence["position_lots"] == _published_shape(repo.list_position_lots())
    assert len(evidence["position_lots"]) == 1
    lot = evidence["position_lots"][0]
    assert set(lot) == {"record_id", "lot_id", "fields"}
    assert lot["fields"]["open_event_id"] == "deal-1"


def test_receipt_readback_agrees_with_the_published_lot_identity(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(database)
    run_position_projection_forced_full(repo, [_event("deal-1")])

    evidence = open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()

    assert evidence["position_lots"] == _published_shape(repo.list_position_lots())
    assert [lot["record_id"] for lot in evidence["position_lots"]] == ["lot-deal-1"]
    assert [lot["lot_id"] for lot in evidence["position_lots"]] == ["lot-deal-1"]


def test_receipt_readback_uses_one_query_only_snapshot_during_concurrent_commit(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "ledger.sqlite3"
    writer = SQLiteOptionPositionsRepository(database)
    run_position_projection_forced_full(writer, [_event("before")])
    reader = open_trade_reconciliation_evidence_repo(database)
    original_read_lots = reader._read_position_lots
    commits = []

    def commit_between_reads(conn, **kwargs):
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly|no such function: om_trade_attribution_writer_v1"):
            conn.execute("DELETE FROM trade_events")
        if not commits:
            run_position_projection_forced_full(writer, [_event("after")])
            commits.append(True)
        return original_read_lots(conn, **kwargs)

    monkeypatch.setattr(reader, "_read_position_lots", commit_between_reads)
    evidence = reader.read_trade_receipt_evidence()
    assert [row["event_id"] for row in evidence["trade_events"]] == ["before"]
    assert [row["fields"]["open_event_id"] for row in evidence["position_lots"]] == ["before"]
    later = reader.read_trade_receipt_evidence()
    assert {row["event_id"] for row in later["trade_events"]} == {"before", "after"}
    assert {row["fields"]["open_event_id"] for row in later["position_lots"]} == {"before", "after"}


@pytest.mark.parametrize("missing", ["trade_events", "position_lots"])
def test_receipt_readback_missing_required_table_fails_closed(tmp_path: Path, missing: str) -> None:
    database = tmp_path / "ledger.sqlite3"
    _minimal_database(database)
    with connect_ledger_fixture(database) as conn:
        conn.execute(f"DROP TABLE {missing}")
    before = database.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="requires trade_events and position_lots"):
        open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()
    assert database.read_bytes() == before


@pytest.mark.parametrize("table", ["trade_events", "position_lots"])
@pytest.mark.parametrize("payload", ["{", "[]", "null", '"text"', "", None])
def test_receipt_readback_invalid_json_fails_closed(tmp_path: Path, table: str, payload: str | None) -> None:
    database = tmp_path / "ledger.sqlite3"
    _minimal_database(database)
    with connect_ledger_fixture(database) as conn:
        if table == "trade_events":
            conn.execute("INSERT INTO trade_events VALUES (?, ?, ?)", ("row-1", payload, 1))
        else:
            conn.execute(
                "INSERT INTO position_lots VALUES (?, NULL, ?, NULL, NULL, NULL, ?)",
                ("row-1", payload, 1),
            )
    with pytest.raises(ValueError):
        open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()


def test_receipt_readback_invalid_canonical_event_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    _minimal_database(database)
    with connect_ledger_fixture(database) as conn:
        conn.execute("INSERT INTO trade_events VALUES ('broken', '{}', 1)")
    with pytest.raises(ValueError, match="invalid canonical trade event"):
        open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()


def test_receipt_readback_missing_column_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    with connect_ledger_fixture(database) as conn:
        conn.execute("CREATE TABLE trade_events (event_id TEXT)")
        conn.execute("CREATE TABLE position_lots (record_id TEXT)")
    with pytest.raises(sqlite3.DatabaseError, match="no such column"):
        open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence()


def test_receipt_readback_does_not_create_or_initialize_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "ledger.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        open_trade_reconciliation_evidence_repo(missing).read_trade_receipt_evidence()
    assert not missing.parent.exists()

    empty = tmp_path / "empty.sqlite3"
    connect_ledger_fixture(empty).close()
    with pytest.raises(sqlite3.DatabaseError):
        open_trade_reconciliation_evidence_repo(empty).read_trade_receipt_evidence()
    assert empty.read_bytes() == b""


def test_receipt_readback_uses_readonly_uri_without_file_or_permission_mutation(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "ledger.sqlite3"
    _minimal_database(database)
    database.chmod(0o640)
    tmp_path.chmod(0o750)
    before = {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in tmp_path.iterdir()}
    real_connect = sqlite3.connect
    connections = []

    def readonly_connect(database_uri, **kwargs):
        assert database_uri == f"{database.resolve().as_uri()}?mode=ro"
        assert kwargs["uri"] is True
        connection = real_connect(database_uri, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    assert open_trade_reconciliation_evidence_repo(database).read_trade_receipt_evidence() == {
        "trade_events": [], "position_lots": [],
    }
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in tmp_path.iterdir()} == before
    assert tmp_path.stat().st_mode & 0o777 == 0o750
