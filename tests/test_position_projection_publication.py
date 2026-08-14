from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
    position_lots_fingerprint,
)
from src.application.ledger.position_projection_publication import (
    publish_full_position_projection,
    read_current_position_projection,
)
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.projector_implementation import (
    EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT,
    ProjectorImplementationUnavailable,
    compute_projector_implementation_fingerprint,
    loaded_projector_implementation_fingerprint,
    resolve_projector_source_root,
    projector_implementation_manifest,
    validate_projector_implementation_manifest,
    verify_expected_projector_implementation,
)
from src.application.ledger.repository import (
    POSITION_LOTS_COLUMN_CLASSIFICATION,
    TRADE_EVENTS_COLUMN_CLASSIFICATION,
    SQLiteOptionPositionsRepository,
    with_sqlite_repo_transaction,
)


def _event(
    event_id: str,
    *,
    account: str = "lx",
    event_time_ms: int = 1_000,
    symbol: str = "NVDA",
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account=account,
            underlying_symbol=symbol,
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=1.5,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id=f"lot-{event_id}",
    )


def _lot(record_id: str, *, account: str = "lx", contracts_open: int = 1) -> PositionLotRecord:
    return PositionLotRecord(
        record_id=record_id,
        fields={
            "account": account,
            "broker": "futu",
            "symbol": "NVDA" if account == "lx" else "AAPL",
            "option_type": "put",
            "side": "short",
            "contracts": 1,
            "contracts_open": contracts_open,
            "contracts_closed": 1 - contracts_open,
            "currency": "USD",
            "status": "open" if contracts_open else "close",
            "strike": 100,
            "multiplier": 100,
            "expiration": 1781827200000,
            "expiration_ymd": "2026-06-19",
        },
    )


def _generations(repo: SQLiteOptionPositionsRepository) -> tuple[int, dict[str, int]]:
    with repo._connect() as conn:  # type: ignore[attr-defined]
        source = conn.execute(
            "SELECT source_generation FROM position_projection_source_state WHERE singleton_id = 1"
        ).fetchone()
        heads = conn.execute(
            "SELECT account, lots_generation FROM position_projection_heads ORDER BY account"
        ).fetchall()
    assert source is not None
    return int(source["source_generation"]), {str(row["account"]): int(row["lots_generation"]) for row in heads}


def test_position_lot_fingerprint_is_canonical_streaming_and_strict() -> None:
    records = [
        {"record_id": "b", "fields": {"nested": {"z": 1, "a": None}, "items": [2, 1]}},
        {"record_id": "a", "fields": {"text": "期权", "missing_is_distinct": True}},
    ]
    ordered = [records[1], records[0]]
    canonical = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected = hashlib.sha256(canonical).hexdigest()

    assert position_lots_fingerprint(records) == expected
    assert ordered_position_lots_fingerprint(iter(ordered)) == expected
    assert position_lots_fingerprint([]) == hashlib.sha256(b"[]").hexdigest()
    assert position_lots_fingerprint([{"record_id": "x", "fields": {"value": None}}]) != position_lots_fingerprint(
        [{"record_id": "x", "fields": {}}]
    )
    with pytest.raises(ValueError, match="unique ascending"):
        ordered_position_lots_fingerprint([ordered[1], ordered[0]])
    with pytest.raises(ValueError):
        position_lots_fingerprint([{"record_id": "x", "fields": {"value": float("nan")}}])


def test_projection_column_classification_is_closed(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    contract = repo.position_projection_column_contract()

    assert set(TRADE_EVENTS_COLUMN_CLASSIFICATION) == {
        "event_id",
        "account",
        "event_json",
        "trade_time_ms",
        "created_at_ms",
        "updated_at_ms",
    }
    assert set(POSITION_LOTS_COLUMN_CLASSIFICATION) == {
        "record_id",
        "account",
        "fields_json",
        "source_event_id",
        "expiration",
        "strike",
        "multiplier",
        "updated_at_ms",
    }
    assert contract == {
        "trade_events": {"missing": (), "unclassified": ()},
        "position_lots": {"missing": (), "unclassified": ()},
    }


def test_event_trigger_matrix_idempotency_conflict_and_replace(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = _event("event-1")

    assert repo.upsert_trade_event(event) is True
    assert _generations(repo)[0] == 1
    assert repo.upsert_trade_event(event) is False
    assert _generations(repo)[0] == 1
    with pytest.raises(ValueError, match="conflict"):
        repo.upsert_trade_event(TradeEvent(**{**event.__dict__, "price": 2.0}))
    assert _generations(repo)[0] == 1

    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE trade_events SET updated_at_ms = updated_at_ms + 1 WHERE event_id = ?",
            (event.event_id,),
        )
        conn.commit()
    assert _generations(repo)[0] == 1

    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE trade_events SET trade_time_ms = trade_time_ms + 1 WHERE event_id = ?",
            (event.event_id,),
        )
        conn.commit()
    assert _generations(repo)[0] == 2

    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT * FROM trade_events WHERE event_id = ?", (event.event_id,)).fetchone()
        assert row is not None
        conn.execute(
            """
            REPLACE INTO trade_events (
              event_id, account, event_json, trade_time_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            tuple(
                row[key]
                for key in ("event_id", "account", "event_json", "trade_time_ms", "created_at_ms", "updated_at_ms")
            ),
        )
        conn.commit()
    assert _generations(repo)[0] == 4

    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM trade_events WHERE event_id = ?", (event.event_id,))
        conn.commit()
    assert _generations(repo)[0] == 5


def test_event_and_lot_account_guards_cover_mixed_version_and_conflict(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = _event("event-1")
    payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)

    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO trade_events (
              event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event.event_id, payload, event.event_time_ms, 1, 1),
        )
        with pytest.raises(sqlite3.IntegrityError, match="conflicts"):
            conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, account, event_json, trade_time_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("event-conflict", "sy", payload, 2, 2, 2),
            )
        conn.commit()

    legacy_void = {
        "event_id": "legacy-void",
        "trade_time_ms": 3,
        "position_effect": "void",
        "raw_payload": {"void_target_event_id": event.event_id},
    }
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO trade_events (
              event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-void", json.dumps(legacy_void), 3, 3, 3),
        )
        conn.commit()
    assert repo.position_projection_normalized_columns_ready() is False

    lot = _lot("lot-1")
    fields_json = json.dumps(lot.fields, ensure_ascii=False, sort_keys=True)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO position_lots (
              record_id, fields_json, expiration, strike, multiplier, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lot.record_id, fields_json, 1781827200000, 100, 100, 1),
        )
        with pytest.raises(sqlite3.IntegrityError, match="conflicts"):
            conn.execute(
                """
                INSERT INTO position_lots (
                  record_id, account, fields_json, expiration, strike, multiplier, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("lot-conflict", "sy", fields_json, 1781827200000, 100, 100, 2),
            )
        conn.commit()


def test_lot_diff_has_zero_dml_for_unchanged_rows_and_tracks_account_move(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    initial = [_lot("lot-a"), _lot("lot-b", account="sy")]
    first = repo.apply_position_lot_diff(initial)
    assert (first.added, first.changed, first.removed, first.unchanged) == (2, 0, 0, 0)
    before = _generations(repo)[1]

    with repo._connect() as conn:  # type: ignore[attr-defined]
        timestamps = {
            str(row["record_id"]): int(row["updated_at_ms"])
            for row in conn.execute("SELECT record_id, updated_at_ms FROM position_lots")
        }
    second = repo.apply_position_lot_diff(initial)
    assert (second.added, second.changed, second.removed, second.unchanged) == (0, 0, 0, 2)
    assert second.touched_accounts == ()
    assert _generations(repo)[1] == before
    with repo._connect() as conn:  # type: ignore[attr-defined]
        assert timestamps == {
            str(row["record_id"]): int(row["updated_at_ms"])
            for row in conn.execute("SELECT record_id, updated_at_ms FROM position_lots")
        }

    moved = repo.apply_position_lot_diff([_lot("lot-a", account="sy"), initial[1]])
    assert (moved.changed, moved.unchanged) == (1, 1)
    assert moved.touched_accounts == ("lx", "sy")
    after = _generations(repo)[1]
    assert after["lx"] == before["lx"] + 1
    assert after["sy"] == before["sy"] + 1


def test_lot_trigger_metadata_update_and_cross_account_replace(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.apply_position_lot_diff([_lot("lot-a")])
    before = _generations(repo)[1]
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE position_lots SET updated_at_ms = updated_at_ms + 1 WHERE record_id = 'lot-a'")
        conn.commit()
    assert _generations(repo)[1] == before

    replacement = _lot("lot-a", account="sy")
    fields_json = json.dumps(replacement.fields, ensure_ascii=False, sort_keys=True)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            REPLACE INTO position_lots (
              record_id, account, fields_json, source_event_id,
              expiration, strike, multiplier, updated_at_ms
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                replacement.record_id,
                "sy",
                fields_json,
                1781827200000,
                100,
                100,
                2,
            ),
        )
        conn.commit()
    after = _generations(repo)[1]
    assert after["lx"] == before["lx"] + 1
    assert after["sy"] == 1


def test_full_publication_trusted_read_zero_lot_and_cross_account_freshness(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx", account="lx"))
    repo.upsert_trade_event(_event("event-sy", account="sy", event_time_ms=2_000, symbol="AAPL"))
    first = publish_full_position_projection(
        repo,
        [_lot("lot-lx"), _lot("lot-sy", account="sy")],
    )
    assert first.heads_trusted is True
    assert first.position_lot_count == 2
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"
    assert read_current_position_projection(repo, account="sy")["status"] == "trusted"

    repo.upsert_trade_event(_event("event-lx-2", account="lx", event_time_ms=3_000))
    stale_b = read_current_position_projection(repo, account="sy")
    assert stale_b["status"] == "data_unavailable"
    assert stale_b["reason"] == "source_generation_mismatch"

    second = publish_full_position_projection(
        repo,
        [_lot("lot-lx", contracts_open=0), _lot("lot-sy", account="sy")],
    )
    assert second.touched_accounts == ("lx",)
    fresh_b = read_current_position_projection(repo, account="sy")
    assert fresh_b["status"] == "trusted"
    assert fresh_b["position_lots"][0]["record_id"] == "lot-sy"

    empty = publish_full_position_projection(repo, [_lot("lot-sy", account="sy")])
    assert empty.position_lot_count == 1
    empty_lx = read_current_position_projection(repo, account="lx")
    assert empty_lx["status"] == "trusted"
    assert empty_lx["lot_count"] == 0
    assert empty_lx["position_lots"] == []


def test_event_only_account_gets_a_zero_lot_head_without_history_discovery(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx"))

    publication = publish_full_position_projection(repo, [])

    assert publication.position_lot_count == 0
    current = read_current_position_projection(repo, account="lx")
    assert current["status"] == "trusted"
    assert current["lot_count"] == 0
    with repo._connect() as conn:  # type: ignore[attr-defined]
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        assert repo.list_position_projection_accounts(conn=conn) == ("lx",)
        conn.set_trace_callback(None)
    joined = "\n".join(statements).lower()
    assert "from position_projection_heads" in joined
    assert "from trade_events" not in joined
    assert "from position_lots" not in joined


def test_direct_mutation_and_schema_change_fail_closed(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx"))
    publish_full_position_projection(repo, [_lot("lot-lx")])
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"

    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE position_lots SET strike = strike + 1 WHERE record_id = 'lot-lx'")
        conn.commit()
    changed = read_current_position_projection(repo, account="lx")
    assert changed["status"] == "data_unavailable"
    assert changed["reason"] == "lots_generation_mismatch"

    publish_full_position_projection(repo, [_lot("lot-lx")])
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("ALTER TABLE position_lots ADD COLUMN future_semantic TEXT")
        conn.commit()
    schema_changed = read_current_position_projection(repo, account="lx")
    assert schema_changed["status"] == "data_unavailable"
    assert schema_changed["reason"] == "sqlite_schema_cookie_mismatch"


def test_untrusted_read_rejects_before_scanning_account_lots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx"))
    publish_full_position_projection(repo, [_lot("lot-lx")])
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE position_lots SET strike = strike + 1 WHERE record_id = 'lot-lx'")
        conn.commit()

    def _unexpected_snapshot(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stale metadata must reject before reading lot rows")

    monkeypatch.setattr(repo, "position_projection_account_snapshot", _unexpected_snapshot)
    unavailable = read_current_position_projection(repo, account="lx")
    assert unavailable["status"] == "data_unavailable"
    assert unavailable["reason"] == "lots_generation_mismatch"


def test_full_publication_repairs_non_null_scalar_drift(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx"))
    publish_full_position_projection(repo, [_lot("lot-lx")])
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE position_lots SET strike = 999 WHERE record_id = 'lot-lx'")
        conn.commit()

    repaired = publish_full_position_projection(repo, [_lot("lot-lx")])
    assert repaired.changed == 1
    assert repaired.heads_trusted is True
    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT strike FROM position_lots WHERE record_id = 'lot-lx'").fetchone()
    assert row is not None
    assert row["strike"] == 100


def test_publication_rolls_back_event_lot_and_head_together(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    before = _generations(repo)
    with pytest.raises(RuntimeError, match="rollback"):
        with repo._connect() as conn:  # type: ignore[attr-defined]
            conn.execute("BEGIN IMMEDIATE")
            try:
                repo.upsert_trade_event(_event("event-lx"), conn=conn)
                publish_full_position_projection(repo, [_lot("lot-lx")], conn=conn)
                raise RuntimeError("rollback")
            except Exception:
                conn.rollback()
                raise
    assert repo.list_trade_events() == []
    assert repo.list_position_lots() == []
    assert _generations(repo) == before


def test_account_queries_use_normalized_indexes(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with repo._connect() as conn:  # type: ignore[attr-defined]
        event_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT event_json FROM trade_events WHERE account = ? ORDER BY trade_time_ms, event_id",
            ("lx",),
        ).fetchall()
        lot_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT record_id FROM position_lots WHERE account = ? ORDER BY expiration, record_id",
            ("lx",),
        ).fetchall()
        fingerprint_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT record_id FROM position_lots WHERE account = ? ORDER BY record_id",
            ("lx",),
        ).fetchall()
    assert "idx_trade_events_account_time" in " ".join(str(row["detail"]) for row in event_plan)
    assert "idx_position_lots_account_expiration" in " ".join(str(row["detail"]) for row in lot_plan)
    fingerprint_details = " ".join(str(row["detail"]) for row in fingerprint_plan)
    assert "idx_position_lots_account_record" in fingerprint_details
    assert "TEMP B-TREE" not in fingerprint_details


def test_populated_store_adds_columns_without_backfill_or_normalized_index(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    fields = _lot("lot-legacy").fields
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE position_lots (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              source_event_id TEXT,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO position_lots VALUES (?, ?, ?, ?)",
            ("lot-legacy", json.dumps(fields), None, 123),
        )
        conn.commit()

    repo = SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT account, expiration, strike, multiplier, updated_at_ms FROM position_lots"
        ).fetchone()
        indexes = {str(item["name"]) for item in conn.execute("PRAGMA index_list(position_lots)").fetchall()}
    assert row is not None
    assert tuple(row) == (None, None, None, None, 123)
    assert "idx_position_lots_account_expiration" not in indexes
    assert "idx_position_lots_account_record" not in indexes
    assert "idx_position_lots_expiration" not in indexes

    publication = publish_full_position_projection(repo, [_lot("lot-legacy")])
    assert publication.position_lot_count == 1
    assert publication.heads_trusted is False
    assert publication.trust_reason == "normalized_indexes_missing"
    unavailable = read_current_position_projection(repo, account="lx")
    assert unavailable["status"] == "data_unavailable"
    assert unavailable["reason"] == "head_not_trusted"

    assert repo.backfill_position_projection_accounts() == {
        "trade_events_updated": 0,
        "position_lots_updated": 1,
    }
    assert repo.backfill_position_lot_contract_columns() == 1
    assert repo.build_position_projection_indexes() == (
        "idx_position_lots_account_expiration",
        "idx_position_lots_account_record",
    )
    migrated = publish_full_position_projection(repo, [_lot("lot-legacy")])
    assert migrated.heads_trusted is True
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"


def test_projector_implementation_manifest_digest_and_root_contract() -> None:
    root = resolve_projector_source_root()
    assert (root / "src/application/ledger/publisher.py").is_file()
    validate_projector_implementation_manifest(root)
    assert verify_expected_projector_implementation(root) == (EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT)
    assert loaded_projector_implementation_fingerprint() == (EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT)
    with pytest.raises(ProjectorImplementationUnavailable, match="could not be resolved"):
        resolve_projector_source_root(Path("/tmp"))


def test_loaded_projector_identity_is_frozen_before_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = loaded_projector_implementation_fingerprint()

    def _unexpected_source_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("publication must use the process-frozen implementation id")

    monkeypatch.setattr(Path, "read_bytes", _unexpected_source_read)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-lx"))
    publication = publish_full_position_projection(repo, [_lot("lot-lx")])

    assert frozen == EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT
    assert publication.heads_trusted is True


def test_publication_rejects_legacy_repo_before_lot_write() -> None:
    class LegacyRepo:
        def __init__(self) -> None:
            self.replacements = 0

        def list_position_lots(self) -> list[dict[str, object]]:
            return []

        def list_trade_events(self) -> list[dict[str, object]]:
            return []

        def upsert_trade_event(self, _event: object, *, conn: object = None) -> bool:
            return True

        def replace_position_lots(
            self,
            _records: object,
            *,
            conn: object = None,
        ) -> int:
            self.replacements += 1
            return 0

    repo = LegacyRepo()

    with pytest.raises(TypeError, match="projection publication interface"):
        publish_full_position_projection(repo, [])
    assert repo.replacements == 0


def test_projecting_transaction_rejects_legacy_repo_before_event_write() -> None:
    class LegacyRepo:
        def __init__(self) -> None:
            self.event_writes = 0

        def list_position_lots(self) -> list[dict[str, object]]:
            return []

        def list_trade_events(self) -> list[dict[str, object]]:
            return []

        def upsert_trade_event(self, _event: object, *, conn: object = None) -> bool:
            self.event_writes += 1
            return True

        def replace_position_lots(
            self,
            _records: object,
            *,
            conn: object = None,
        ) -> int:
            return 0

    repo = LegacyRepo()

    with pytest.raises(TypeError, match="projection publication interface"):
        with_sqlite_repo_transaction(
            repo,
            lambda candidate, _conn: candidate.upsert_trade_event(object()),
            require_projection_publication=True,
        )
    assert repo.event_writes == 0


def test_projector_fingerprint_frames_paths_and_raw_bytes(tmp_path: Path) -> None:
    root = resolve_projector_source_root()
    # A complete release-style source tree does not need .git.
    manifest = projector_implementation_manifest()
    for source in manifest["files"]:
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, target)
    assert resolve_projector_source_root(tmp_path) == tmp_path.resolve()
    assert compute_projector_implementation_fingerprint(tmp_path) == (EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT)

    publisher = tmp_path / "src/application/ledger/publisher.py"
    publisher.write_bytes(publisher.read_bytes() + b"\n")
    assert compute_projector_implementation_fingerprint(tmp_path) != (EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT)
    with pytest.raises(ProjectorImplementationUnavailable, match="differs"):
        verify_expected_projector_implementation(tmp_path)


def test_full_writer_sources_use_shared_publication_and_no_full_delete() -> None:
    root = resolve_projector_source_root()
    writers = (
        "src/application/ledger/writer.py",
        "src/application/ledger/manual_trades.py",
        "src/application/ledger/interventions.py",
        "src/application/ledger/combo_reconciliation.py",
        "src/application/ledger/bootstrap.py",
    )
    for path in writers:
        source = (root / path).read_text(encoding="utf-8")
        assert "replace_position_lots(" not in source
        assert "run_position_projection_in_transaction" in source
    repository_source = (root / "src/application/ledger/repository.py").read_text(encoding="utf-8")
    assert 'execute("DELETE FROM position_lots")' not in repository_source
    assert "DELETE FROM position_lots WHERE record_id = ?" in repository_source
