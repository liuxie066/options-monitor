from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_contract_identity import derive_trade_side
from src.application.ledger.api import (
    MAX_TRADE_EVENT_PAGE_ROWS,
    TradeEventPaginationError,
    trade_event_page,
)
from src.application.ledger import repository_trade_schema
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.position_projection_migration import (
    apply_position_projection_migration,
    build_position_projection_migration_inventory,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


CURSOR_KEY = "test-only-cursor-signing-key"


class _InjectedPublishFailure(sqlite3.OperationalError):
    """The failure a test injects into the publish, so it can be told apart.

    It subclasses ``OperationalError`` because that is what a real statement
    failure is, and the handler under test branches on ``sqlite3.Error``.
    """


def test_public_api_exposes_trade_event_page_limit() -> None:
    assert MAX_TRADE_EVENT_PAGE_ROWS == 20


def test_fresh_exhausted_page_covers_full_query_but_continuation_does_not(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for index in range(20):
        repo.upsert_trade_event(
            _event(
                f"close-{index:02d}",
                event_time_ms=10_000 + index,
                event_type="close",
            )
        )

    first_ten = _page(
        repo,
        {"account": "lx", "position_effect": "close", "limit": 10},
    )
    all_twenty = _page(
        repo,
        {"account": "lx", "position_effect": "close", "limit": 20},
    )
    second_ten = _continuation(repo, first_ten["next_cursor"], limit=10)

    assert first_ten["coverage"]["complete_for"] == "requested_page"
    assert first_ten["coverage"]["included_count"] == 10
    assert first_ten["coverage"]["has_more"] is True
    assert first_ten["coverage"]["total_count"] is None
    assert all_twenty["coverage"]["complete_for"] == "full_query"
    assert all_twenty["coverage"]["included_count"] == 20
    assert all_twenty["coverage"]["total_count"] == 20
    assert all_twenty["coverage"]["omitted_count"] == 0
    assert all_twenty["snapshot_exhausted"] is True
    assert second_ten["coverage"]["complete_for"] == "requested_page"
    assert second_ten["snapshot_exhausted"] is True


def _event(
    event_id: str,
    *,
    event_time_ms: int,
    event_type: str = "open",
    account: str = "lx",
    symbol: str = "NVDA",
) -> TradeEvent:
    needs_target = event_type in {
        "close",
        "expire_close",
        "assignment",
        "exercise",
        "adjust",
    }
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account=account,
            underlying_symbol=symbol,
            option_type="put",
            strike=100,
            expiration_ymd="2026-06-19",
                ),
        contracts=1,
        price=1.5,
        currency="HKD" if symbol.endswith(".HK") else "USD",
        source="pagination_test",
        target_lot_id=f"lot_{event_id}" if needs_target else None,
        # §9.2 step 3: the contract key no longer carries the position side, so
        # the fixture's short put side travels as the trade side.
        raw_payload={"side": derive_trade_side(event_type, "short") or ""},
    )


def _page(
    repo: object,
    payload: dict[str, object],
    *,
    account: str | None = "lx",
    market: str = "US",
    authorized_accounts: tuple[str, ...] = ("lx",),
    now_epoch_s: int = 1_000,
    cursor_key: str = CURSOR_KEY,
) -> dict[str, object]:
    return trade_event_page(
        repo,
        payload=payload,
        account=account,
        market=market,
        authorized_accounts=authorized_accounts,
        cursor_key=cursor_key,
        now_epoch_s=now_epoch_s,
        as_of="2026-08-22T00:00:00Z",
    )


def _continuation(
    repo: object, cursor: object, *, limit: int, **extra: object
) -> dict[str, object]:
    """Fetch a follow-up page from a cursor; continuation pages carry no account."""
    return _page(
        repo, {"cursor": cursor, "limit": limit, **extra}, account=None, now_epoch_s=1_001
    )


def _legacy_store(path: Path, events: tuple[TradeEvent, ...]) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trade_events (
              event_id TEXT PRIMARY KEY,
              account TEXT,
              event_json TEXT NOT NULL,
              trade_time_ms INTEGER NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE position_lots (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              source_event_id TEXT,
              expiration INTEGER,
              strike REAL,
              multiplier REAL,
              updated_at_ms INTEGER NOT NULL
            );
            """
        )
        for index, event in enumerate(events):
            encoded = encode_trade_event_for_storage(event)
            conn.execute(
                "INSERT INTO trade_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.contract_key.account,
                    encoded.event_json,
                    event.event_time_ms,
                    index // 2,
                    index,
                ),
            )


def _ingest_sequences(db_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT ingest_seq FROM trade_events ORDER BY ingest_seq"
            )
        ]


def _force_zero_event_time(path: Path, event_id: str) -> None:
    """Rewrite one stored row to ``event_time_ms = 0`` in both copies of the time."""

    with sqlite3.connect(path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        payload["event_time_ms"] = 0
        conn.execute(
            "UPDATE trade_events SET event_json = ?, trade_time_ms = 0 WHERE event_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), event_id),
        )


def test_variable_page_sizes_do_not_repeat_and_freeze_late_inserts(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for index in range(40):
        repo.upsert_trade_event(
            _event(
                f"close-{index:02d}",
                event_time_ms=10_000 + index,
                event_type="close",
            )
        )

    first = _page(
        repo,
        {"account": "lx", "position_effect": "close", "limit": 10},
    )
    repo.upsert_trade_event(_event("inserted-newest", event_time_ms=99_999, event_type="close"))
    repo.upsert_trade_event(_event("inserted-late", event_time_ms=1, event_type="close"))
    second = _continuation(repo, first["next_cursor"], limit=20, include_total=True)
    third = _page(
        repo,
        {"cursor": second["next_cursor"], "limit": 10},
        account=None,
        now_epoch_s=1_002,
    )

    assert [page["returned_count"] for page in (first, second, third)] == [10, 20, 10]
    assert second["total_count"] == 40
    assert third["snapshot_exhausted"] is True
    assert third["next_cursor"] is None
    assert first["stream_id"] == second["stream_id"] == third["stream_id"]
    assert first["as_of"] == second["as_of"] == third["as_of"]
    page_ids = [{str(row["event_id"]) for row in page["rows"]} for page in (first, second, third)]
    assert all(page_ids[left].isdisjoint(page_ids[right]) for left, right in ((0, 1), (0, 2), (1, 2)))
    all_ids = set().union(*page_ids)
    assert len(all_ids) == 40
    assert {"inserted-newest", "inserted-late"}.isdisjoint(all_ids)


def test_same_time_ties_use_descending_event_id_keyset(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for event_id in ("same-a", "same-c", "same-b"):
        repo.upsert_trade_event(_event(event_id, event_time_ms=5_000))

    first = _page(repo, {"limit": 2})
    second = _continuation(repo, first["next_cursor"], limit=2)

    assert [row["event_id"] for row in first["rows"]] == ["same-c", "same-b"]
    assert [row["event_id"] for row in second["rows"]] == ["same-a"]


def test_market_effect_and_authority_are_applied_before_paging(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for event in (
        _event("lx-close", event_time_ms=10, event_type="close"),
        _event("lx-assignment", event_time_ms=11, event_type="assignment"),
        _event("lx-open", event_time_ms=12),
        _event("sy-close", event_time_ms=13, event_type="expire_close", account="sy"),
        _event("rogue-close", event_time_ms=14, event_type="close", account="rogue"),
        _event("hk-close", event_time_ms=15, event_type="exercise", symbol="0700.HK"),
    ):
        repo.upsert_trade_event(event)

    page = _page(
        repo,
        {"position_effect": "close", "limit": 20},
        account=None,
        authorized_accounts=("lx", "sy"),
    )

    assert {row["event_id"] for row in page["rows"]} == {
        "lx-close",
        "lx-assignment",
        "sy-close",
    }
    assert all(row["position_effect"] == "close" for row in page["rows"])


def test_cursor_rejects_tampering_expiry_and_scope_changes(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-1", event_time_ms=2))
    repo.upsert_trade_event(_event("event-2", event_time_ms=1))
    first = _page(repo, {"symbol": "NVDA", "limit": 1})
    cursor = str(first["next_cursor"])
    encoded, signature = cursor.split(".", 1)
    tampered = f"{encoded}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

    cases = (
        ({"cursor": tampered, "limit": 1}, ("lx",), 1_000, "invalid_cursor_signature"),
        ({"cursor": cursor, "limit": 1, "symbol": "AAPL"}, ("lx",), 1_001, "cursor_query_mismatch"),
        ({"cursor": cursor, "limit": 1}, ("lx", "sy"), 1_001, "cursor_authority_mismatch"),
        ({"cursor": cursor, "limit": 1}, ("lx",), 2_800, "cursor_expired"),
    )
    for payload, authority, now, code in cases:
        with pytest.raises(TradeEventPaginationError) as error:
            _page(
                repo,
                payload,
                account=None,
                authorized_accounts=authority,
                now_epoch_s=now,
            )
        assert error.value.code == code

    with pytest.raises(TradeEventPaginationError) as missing_key:
        _page(repo, {"limit": 1}, cursor_key="")
    assert missing_key.value.code == "cursor_key_unavailable"


def test_legacy_rows_require_controlled_deterministic_backfill(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_store(
        path,
        (
            _event("event-b", event_time_ms=20),
            _event("event-a", event_time_ms=10),
            _event("event-c", event_time_ms=30, symbol="0700.HK"),
        ),
    )
    repo = SQLiteOptionPositionsRepository(path)

    with pytest.raises(TradeEventPaginationError) as unavailable:
        _page(repo, {"limit": 1})
    assert unavailable.value.code == "pagination_unavailable"

    inventory = build_position_projection_migration_inventory(path)
    applied = apply_position_projection_migration(path, inventory)
    assert applied["trade_event_pagination_rows_backfilled"] == 3
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT event_id, ingest_seq, market, position_effect
            FROM trade_events ORDER BY ingest_seq
            """
        ).fetchall()
    assert rows == [
        ("event-a", 1, "US", "open"),
        ("event-b", 2, "US", "open"),
        ("event-c", 3, "HK", "open"),
    ]


def test_backfill_preserves_voided_legacy_event_with_non_positive_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-voided-invalid-time.sqlite3"
    target = _event("legacy-close", event_time_ms=10, event_type="close")
    void = TradeEvent(
        event_id="void-legacy-close",
        event_type="void",
        event_time_ms=20,
        contract_key=target.contract_key,
        contracts=0,
        price=0.0,
        currency=target.currency,
        source="pagination_test_repair",
        target_event_id=target.event_id,
    )
    _legacy_store(path, (target, void))
    _force_zero_event_time(path, target.event_id)

    inventory = build_position_projection_migration_inventory(path)
    applied = apply_position_projection_migration(path, inventory)

    assert applied["trade_event_pagination_rows_backfilled"] == 2
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT trade_time_ms, ingest_seq, market, position_effect
            FROM trade_events WHERE event_id = ?
            """,
            (target.event_id,),
        ).fetchone()
    assert row == (0, 1, "US", "close")


def test_backfill_rejects_unvoided_legacy_event_with_non_positive_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-active-invalid-time.sqlite3"
    target = _event("legacy-close", event_time_ms=10, event_type="close")
    _legacy_store(path, (target,))
    _force_zero_event_time(path, target.event_id)

    inventory = build_position_projection_migration_inventory(path)
    with pytest.raises(ValueError, match="event_time_must_be_positive"):
        apply_position_projection_migration(path, inventory)


def test_backfill_rejects_conflicting_existing_projection(tmp_path: Path) -> None:
    path = tmp_path / "legacy-conflict.sqlite3"
    _legacy_store(path, (_event("event-1", event_time_ms=10),))
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE trade_events
            SET ingest_seq = 1, market = 'HK', position_effect = 'open'
            WHERE event_id = 'event-1'
            """
        )

    inventory = build_position_projection_migration_inventory(path)
    with pytest.raises(ValueError, match="market projection conflicts"):
        apply_position_projection_migration(path, inventory)


def test_storage_guards_only_snapshot_membership_and_query_fields(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for index in range(3):
        repo.upsert_trade_event(_event(f"event-{index}", event_time_ms=index + 1))
    first = _page(repo, {"limit": 1})

    with repo._connect() as conn:  # type: ignore[attr-defined]
        payload = json.loads(
            conn.execute("SELECT event_json FROM trade_events WHERE event_id = 'event-0'").fetchone()[0]
        )
        payload["price"] = 9.0
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = 'event-0'",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
        )

    second = _continuation(repo, first["next_cursor"], limit=2)
    assert [row["event_id"] for row in second["rows"]] == ["event-1", "event-0"]
    assert second["rows"][1]["price"] == 9.0

    with repo._connect() as conn:  # type: ignore[attr-defined]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="query projection is immutable|market projection conflicts",
        ):
            conn.execute("UPDATE trade_events SET market = 'HK' WHERE event_id = 'event-0'")
        with pytest.raises(sqlite3.IntegrityError, match="membership is immutable"):
            conn.execute("DELETE FROM trade_events WHERE event_id = 'event-0'")
        row = conn.execute("SELECT * FROM trade_events WHERE event_id = 'event-0'").fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError, match="replacement is not allowed"):
            conn.execute(
                """
                REPLACE INTO trade_events (
                  event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms, ingest_seq, market,
                  position_effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    row[name]
                    for name in (
                        "event_id",
                        "account",
                        "event_json",
                        "trade_time_ms",
                        "created_at_ms",
                        "updated_at_ms",
                        "ingest_seq",
                        "market",
                        "position_effect",
                    )
                ),
            )


def test_storage_guard_rejects_unallocated_or_incomplete_direct_insert(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = _event("event-1", event_time_ms=10)
    encoded = encode_trade_event_for_storage(event)

    with repo._connect() as conn:  # type: ignore[attr-defined]
        with pytest.raises(sqlite3.IntegrityError, match="was not allocated"):
            conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms, ingest_seq, market,
                  position_effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event.event_id, "lx", encoded.event_json, 10, 1, 1, 1, "US", "open"),
            )

        payload = json.loads(encoded.event_json)
        del payload["contract_key"]["broker"]
        with pytest.raises(sqlite3.IntegrityError, match="query fields are incomplete"):
            conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms, ingest_seq, market,
                  position_effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    "lx",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    10,
                    1,
                    1,
                    1,
                    "US",
                    "open",
                ),
            )


def test_backfill_is_batched_and_keyset_query_uses_ordered_index(tmp_path: Path) -> None:
    path = tmp_path / "legacy-large.sqlite3"
    events = tuple(_event(f"event-{index:05d}", event_time_ms=index + 1) for index in range(2_001))
    _legacy_store(path, events)
    repo = SQLiteOptionPositionsRepository(path)
    selects: list[str] = []
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.set_trace_callback(
            lambda sql: selects.append(sql) if "SELECT event_id, account, event_json, trade_time_ms" in sql else None
        )
        assert repo.backfill_trade_event_pagination(conn=conn) == 2_001
        conn.commit()
    assert len(selects) == 4
    assert any("(created_at_ms, event_id) >" in sql for sql in selects)

    with sqlite3.connect(path) as conn:
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT event_id FROM trade_events
                WHERE ingest_seq <= ? AND market = ? AND account = ?
                  AND (trade_time_ms, event_id) < (?, ?)
                ORDER BY trade_time_ms DESC, event_id DESC LIMIT ?
                """,
                (2_001, "US", "lx", 2_001, "event-02000", 20),
            )
        )
    assert "idx_trade_events_account_market_keyset" in plan
    assert "USE TEMP B-TREE" not in plan


def test_cursor_admission_uses_signed_account_on_continuation(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-1", event_time_ms=2))
    repo.upsert_trade_event(_event("event-2", event_time_ms=1))
    first = _page(
        repo,
        {"account": "lx", "limit": 1},
        authorized_accounts=("lx", "sy"),
    )
    admitted: list[tuple[dict[str, object], dict[str, object]]] = []

    trade_event_page(
        repo,
        payload={"cursor": first["next_cursor"], "limit": 1},
        account=None,
        market="US",
        authorized_accounts=("lx", "sy"),
        cursor_key=CURSOR_KEY,
        now_epoch_s=1_001,
        admit_query=lambda query, authority: admitted.append((query, authority)),
    )

    assert admitted == [
        (
            {**first["filters"]},
            {"accounts": ["lx", "sy"], "market": "US"},
        )
    ]


def test_repository_page_requires_caller_owned_transaction(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_event("event-1", event_time_ms=1))
    conn = repo._connect()
    try:
        with pytest.raises(ValueError, match="must already own a transaction"):
            repo.list_trade_events_page(market="US", authorized_accounts=("lx",), conn=conn)
        conn.execute("BEGIN DEFERRED")
        page = repo.list_trade_events_page(market="US", authorized_accounts=("lx",), conn=conn)
        assert [row["event_id"] for row in page["rows"]] == ["event-1"]
        conn.rollback()
    finally:
        conn.close()


def test_public_page_facade_never_loads_full_event_collection() -> None:
    calls: list[dict[str, object]] = []

    class PageOnlyRepo:
        def list_position_lots(self) -> list[dict[str, object]]:
            return []

        def list_trade_events(self) -> list[dict[str, object]]:
            raise AssertionError("full event collection must not be loaded")

        def list_trade_events_page(self, **kwargs: object) -> dict[str, object]:
            calls.append(dict(kwargs))
            return {
                "rows": [],
                "snapshot_max_ingest_seq": 0,
                "has_more": False,
                "total_count": None,
                "last_trade_time_ms": None,
                "last_event_id": None,
            }

    result = _page(
        PageOnlyRepo(),
        {"limit": 10},
        account=None,
        authorized_accounts=("lx", "sy"),
    )

    assert result["rows"] == []
    assert calls == [
        {
            "limit": 10,
            "snapshot_max_ingest_seq": None,
            "last_trade_time_ms": None,
            "last_event_id": None,
            "authorized_accounts": ("lx", "sy"),
            "include_total": False,
            **result["filters"],
        }
    ]


@pytest.mark.parametrize(
    ("limit", "error_code"),
    ((21, "invalid_limit"), ("all", "needs_narrowing"), (1.5, "invalid_limit")),
)
def test_event_limit_is_bounded(
    tmp_path: Path,
    limit: object,
    error_code: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with pytest.raises(TradeEventPaginationError) as error:
        _page(repo, {"limit": limit})
    assert error.value.code == error_code


_PAGINATION_GUARDS = (
    "trg_trade_events_pagination_projection_insert_guard",
    "trg_trade_events_pagination_projection_update_guard",
)

# Must match ``repository_trade_schema._TRADE_EVENT_PAGINATION_GUARD_SCHEMA``.
_PAGINATION_GUARD_SCHEMA = "trade_event_pagination_guard.v2"


def _age_pagination_guards(db_path: Path) -> None:
    """Rewrite the pagination guards to their pre-§7.4 definition.

    Drops the schema marker and narrows the ``typeof(strike)`` whitelist back to
    ``('integer', 'real')``, which is what a store created before the §7.4
    decimal-text strike carries.
    """

    with sqlite3.connect(db_path) as conn:
        for name in _PAGINATION_GUARDS:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()
            assert row is not None, name
            stale = "\n".join(
                line
                for line in str(row[0]).splitlines()
                if _PAGINATION_GUARD_SCHEMA not in line
            ).replace("'integer', 'real', 'text'", "'integer', 'real'")
            conn.execute(f"DROP TRIGGER {name}")
            conn.execute(stale)


def _pagination_guards_carry_current_definition(db_path: Path) -> bool:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)",
            _PAGINATION_GUARDS,
        ).fetchall()
    return len(rows) == 2 and all(
        _PAGINATION_GUARD_SCHEMA in str(row[0] or "") for row in rows
    )


def _duplicate_ingest_sequence(db_path: Path) -> None:
    """Leave two rows sharing an ``ingest_seq``, with no unique index over them.

    Mirrors a store whose allocator was reseeded while the index was absent: the
    values collide, and the definitions the open path would publish are missing.
    """

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_trade_events_ingest_seq_immutable")
        conn.execute("DROP INDEX IF EXISTS idx_trade_events_ingest_seq")
        conn.execute("UPDATE trade_events SET ingest_seq = 7")


def test_open_refreshes_a_stale_pagination_guard_definition(tmp_path: Path) -> None:
    """A store built before §7.4 must not keep a guard that aborts every write.

    Readiness sees the guards by name and type and reads the stored rows, never by
    how they are *defined*, and that used to be enough on its own to skip the publish.
    The publish itself used ``CREATE TRIGGER IF NOT EXISTS`` too, a no-op once the
    name exists. Either half on its own left a store whose guard still carried the
    old ``typeof(strike)`` whitelist rather than the widened one. Once §7.1 onwards
    writes the strike as decimal *text*, that stale guard rejects every insert with
    the misleading ``trade event pagination query fields are incomplete``. Opening
    the store has to refresh the definition, without rewriting any row.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    repo.upsert_trade_event(_event("open-1", event_time_ms=1000))

    # Reproduce the upgraded-store shape: rows present, guard definition stale.
    _age_pagination_guards(db_path)
    assert not _pagination_guards_carry_current_definition(db_path)

    reopened = SQLiteOptionPositionsRepository(db_path)

    assert _pagination_guards_carry_current_definition(db_path)
    # The write that used to be aborted by the stale guard.
    reopened.upsert_trade_event(_event("open-2", event_time_ms=2000))

    # Refreshing the definition must not have disturbed the stored rows.
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT event_id FROM trade_events ORDER BY event_id"
        ).fetchall()
    assert [row[0] for row in stored] == ["open-1", "open-2"]


def test_open_keeps_a_store_whose_ingest_sequence_is_duplicated(
    tmp_path: Path,
) -> None:
    """A store that cannot carry the unique index must still open.

    ``_publish_trade_event_pagination_schema`` creates
    ``CREATE UNIQUE INDEX ... ON trade_events(ingest_seq)``, which cannot succeed
    while two rows share a value. Publishing that from the open path aborts the
    open outright, so a store which opened before the refresh never opens again —
    worse than the stale definitions the refresh set out to fix, because every
    read fails, not only pagination. The open has to leave the definitions alone
    and let the pagination entry points report the gap.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    repo.upsert_trade_event(_event("open-1", event_time_ms=1000))
    repo.upsert_trade_event(_event("open-2", event_time_ms=2000))

    _duplicate_ingest_sequence(db_path)

    # The regression: this used to raise
    # ``IntegrityError: UNIQUE constraint failed: trade_events.ingest_seq``.
    reopened = SQLiteOptionPositionsRepository(db_path)

    # The ledger itself is still readable...
    assert len(reopened.list_trade_events()) == 2
    # ...and the gap left by the unpublishable definition is reported as
    # ``TradeEventPaginationError`` rather than as a raw ``IntegrityError`` from the
    # open path.
    with pytest.raises(TradeEventPaginationError) as error:
        _page(reopened, {"limit": 10})
    assert error.value.code == "pagination_unavailable"


def test_open_reseeds_a_lost_ingest_sequence_allocator(tmp_path: Path) -> None:
    """A lost allocator row must not re-issue values the stored rows already carry.

    The open path seeded it with a literal ``0``, so a store whose row went
    missing (a partial restore, say) restarted at 1 and handed the next insert an
    ``ingest_seq`` already in use — the duplicate that makes the unique index
    impossible to create in the first place.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    for index in range(3):
        repo.upsert_trade_event(_event(f"open-{index}", event_time_ms=1000 + index))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM trade_event_ingest_sequence")

    reopened = SQLiteOptionPositionsRepository(db_path)
    reopened.upsert_trade_event(_event("open-3", event_time_ms=2000))

    sequences = _ingest_sequences(db_path)
    assert sequences == [1, 2, 3, 4]


@pytest.mark.parametrize("stored", ["'abc'", "-5"])
def test_open_seeds_the_allocator_only_from_whole_non_negative_values(
    tmp_path: Path,
    stored: str,
) -> None:
    """Seeding runs before the readiness check, so it also sees rejected rows.

    A ``TEXT`` value would be stored as ``last_value`` — ``CHECK(last_value >= 0)``
    compares text above every integer, so it passes the constraint — and the next
    insert's ``int(...)`` would then fail. A negative value fails that check
    outright. Either way the open would break on a store it used to tolerate, so
    neither may seed the allocator.

    This pins the *new* seeding's precondition rather than a behaviour change:
    the plain ``VALUES (1, 0)`` it replaced passed this trivially. It does bite
    the narrowing it guards against — dropping the ``typeof`` filter fails the
    ``'abc'`` case with ``('abc', 'text')`` as ``last_value``.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    repo.upsert_trade_event(_event("open-1", event_time_ms=1000))

    with sqlite3.connect(db_path) as conn:
        # Every guard has to go: the projection guards refuse to let ``ingest_seq``
        # become a non-integer or a negative, and a store that still had them
        # working would never carry these values. Guards that cannot be trusted is
        # the premise of the refresh path, not an extra liberty taken here.
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP INDEX IF EXISTS idx_trade_events_ingest_seq")
        conn.execute(f"UPDATE trade_events SET ingest_seq = {stored}")
        conn.execute("DELETE FROM trade_event_ingest_sequence")

    SQLiteOptionPositionsRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_value, typeof(last_value) FROM trade_event_ingest_sequence"
        ).fetchone()
    assert row == (0, "integer")


def _schema_objects(db_path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        return {
            (str(row[0]), str(row[1]))
            for row in conn.execute("SELECT type, name FROM sqlite_master")
        }


def test_open_survives_a_pagination_index_name_owned_by_a_table(
    tmp_path: Path,
) -> None:
    """A shadowed index name must not take the whole ledger down with it.

    SQLite treats ``CREATE INDEX IF NOT EXISTS`` as a no-op only when the name
    belongs to an *index*; a table owning it makes the statement raise instead.
    The publish runs from the open path, which every ``__init__`` calls, so an
    escaping failure takes every read with it — a store that opened before the
    guard-refresh branch existed would never open again. The publish is undone
    instead, and the gap is still reported by the pagination entry points.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    for index in range(2):
        repo.upsert_trade_event(_event(f"open-{index}", event_time_ms=1000 + index))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_trade_events_ingest_seq")
        conn.execute("DROP INDEX idx_trade_events_pagination_missing")
        conn.execute("CREATE TABLE idx_trade_events_ingest_seq (shadow INTEGER)")
    before = _schema_objects(db_path)

    reopened = SQLiteOptionPositionsRepository(db_path)

    assert len(reopened.list_trade_events()) == 2
    with pytest.raises(TradeEventPaginationError) as error:
        _page(reopened, {"limit": 10})
    assert error.value.code == "pagination_unavailable"
    # ``idx_trade_events_pagination_missing`` is written before the statement that
    # fails, so a publish that was not undone would leave it behind.
    assert _schema_objects(db_path) == before


def test_open_republishes_the_guards_of_a_duplicate_ingest_sequence_store(
    tmp_path: Path,
) -> None:
    """The unique index is what a duplicate store cannot take, not the guards.

    Skipping the whole publish leaves a stale guard behind, and that guard aborts
    every write with ``trade event pagination query fields are incomplete`` — a
    message about query shape for a store whose actual problem is duplicates.
    Publishing everything but the index keeps the store writable, and readiness
    still reports the gap because the index itself is never created.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    for index in range(2):
        repo.upsert_trade_event(_event(f"open-{index}", event_time_ms=1000 + index))

    _duplicate_ingest_sequence(db_path)
    _age_pagination_guards(db_path)
    assert not _pagination_guards_carry_current_definition(db_path)

    reopened = SQLiteOptionPositionsRepository(db_path)

    assert _pagination_guards_carry_current_definition(db_path)
    reopened.upsert_trade_event(_event("open-2", event_time_ms=2000))
    sequences = _ingest_sequences(db_path)
    # the duplicates survive, and the allocator resumed above them
    assert sequences == [7, 7, 8]
    with pytest.raises(TradeEventPaginationError) as error:
        _page(reopened, {"limit": 10})
    assert error.value.code == "pagination_unavailable"


def test_open_repairs_an_allocator_that_lags_behind_the_stored_rows(
    tmp_path: Path,
) -> None:
    """A stale allocator row must be raised, not merely left alone.

    ``DO NOTHING`` would keep ``last_value`` below a value the rows already carry,
    and the next insert would re-issue it — the collision the seeding exists to
    prevent. The row is only lower than the rows here, not missing, so this
    reaches the ``DO UPDATE`` arm rather than the insert.
    """

    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    for index in range(3):
        repo.upsert_trade_event(_event(f"open-{index}", event_time_ms=1000 + index))

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trade_event_ingest_sequence SET last_value = 1")

    reopened = SQLiteOptionPositionsRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT last_value FROM trade_event_ingest_sequence"
        ).fetchone() == (3,)

    reopened.upsert_trade_event(_event("open-3", event_time_ms=2000))
    sequences = _ingest_sequences(db_path)
    assert sequences == [1, 2, 3, 4]


def test_open_aborts_when_the_publish_failure_took_the_transaction_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure that destroyed the transaction leaves nothing to undo to.

    Some statements roll the caller's whole transaction back when they fail, and
    the savepoint goes with it. Reporting the gap from there is not the harmless
    outcome it looks like: the connection left behind has no transaction at all,
    so every later statement in ``_init_db`` runs in autocommit and the store
    ends up holding whatever part of the schema happened to follow the failure.
    The open then dies on the first statement referencing a rolled-back object
    and names *that* object instead of the failure that actually happened. The
    error is re-raised so the open aborts with neither a partial schema nor a
    misleading cause.

    The two assertions that carry this test are the attempt count and the error
    text: a publish that is never attempted trips the first, and one whose cause
    has been replaced by a downstream missing object trips the second. The empty
    ``sqlite_master`` reading is descriptive rather than a discriminator — this
    store is empty, so nothing can be left behind either way, and it holds under
    the swallowing handler too. The partial-commit consequence needs the shape
    ``test_open_leaves_no_partial_schema_when_the_publish_failure_takes_the_transaction``
    builds. The ``__context__`` assertion pins the branch that reports the failure
    without attempting the undo at all: with the undo attempted, the same original
    failure arrives carrying the failed undo's ``no such savepoint`` as context
    instead.
    """

    db_path = tmp_path / "ledger.sqlite3"
    attempted: list[str] = []

    def take_the_transaction_down(conn: sqlite3.Connection, **_: object) -> None:
        attempted.append("publish")
        # The state SQLite's own auto-rollback leaves behind: the savepoint is
        # gone, ``in_transaction`` is false, and the connection commits from here.
        conn.rollback()
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(
        repository_trade_schema,
        "_publish_trade_event_pagination_schema",
        take_the_transaction_down,
    )

    with pytest.raises(sqlite3.OperationalError) as error:
        SQLiteOptionPositionsRepository(db_path)

    # The publish really was attempted, so this is not a store that never got
    # that far, and the cause survives instead of a downstream missing object.
    assert attempted == ["publish"]
    assert "interrupted" in str(error.value)
    # This branch re-raises without attempting the undo, so the failure carries no
    # context. Delete the branch and the same failure arrives through the undo
    # branch instead, with ``no such savepoint`` chained onto it -- the only
    # observable difference between having the branch and not having it.
    assert error.value.__context__ is None
    # Descriptive, not a discriminator: the file exists because the open created
    # it, and it holds no objects because the abort left nothing behind.
    assert db_path.is_file()
    assert _schema_objects(db_path) == set()


def test_open_leaves_no_partial_schema_when_the_publish_failure_takes_the_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destroyed transaction must not leave the store holding a partial schema.

    Some statements roll the caller's whole transaction back when they fail, and the
    savepoint goes with it. Carrying on from there is what commits a partial schema:
    ``_init_db`` issues its remaining DDL in autocommit, so each statement it reaches
    is written and kept. For that to leave a trace, the store needs a later-stage
    index that is missing over an *empty* table — ``_create_index_if_table_empty``
    rebuilds that one, so the rebuild is a change committed on its own — together
    with a later statement that still fails, so the open does not simply succeed and
    hide it. Aborting instead leaves the store as it was found.

    Both halves matter: dropping only the pagination indexes leaves every later
    statement a no-op, which is why the abort test's empty store shows nothing.

    The comparison of the store is not left to carry this alone: with the rebuilt
    index already created when the publish runs, a swallowing handler leaves the
    store comparing equal to ``before`` even though the open took a different path.
    So the test also pins that the publish was attempted and that the failure which
    surfaced is the injected one. Neither assertion depends on which statement
    fails first.
    """

    db_path = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_trade_events_ingest_seq")
        conn.execute("DROP INDEX idx_trade_events_pagination_missing")
        conn.execute("DROP INDEX idx_trade_events_account_time")
        # A later-stage index name taken by a table, so the statements after the
        # failure are not all no-ops: the open still fails, and what it fails with is
        # the partial schema the failure already committed.
        conn.execute("DROP INDEX idx_position_lots_account_record")
        conn.execute(
            "CREATE TABLE idx_position_lots_account_record (placeholder INTEGER)"
        )
    before = _schema_objects(db_path)

    attempted: list[str] = []

    def take_the_transaction_down(conn: sqlite3.Connection, **_: object) -> None:
        attempted.append("publish")
        conn.rollback()
        raise _InjectedPublishFailure("interrupted")

    monkeypatch.setattr(
        repository_trade_schema,
        "_publish_trade_event_pagination_schema",
        take_the_transaction_down,
    )

    with pytest.raises(sqlite3.OperationalError) as error:
        SQLiteOptionPositionsRepository(db_path)

    # The store is compared first: leaving it untouched is this test's subject, and
    # whichever statement fails first is not. Swallowing the failure instead lets
    # ``_init_db`` commit the rebuilt ``idx_trade_events_account_time`` — a
    # projection index, not one of the pagination ones — in autocommit and fail
    # later on the shadowed name, which is what this comparison refuses.
    assert _schema_objects(db_path) == before
    # ...and what surfaced is the injected failure, not something the open ran into
    # on its way. This is the half the comparison above cannot carry on its own: with
    # the rebuilt index already created when the publish runs, swallowing leaves the
    # store comparing equal to ``before`` anyway, and the open still answers to the
    # shadowed name rather than to this.
    assert attempted == ["publish"]
    assert isinstance(error.value, _InjectedPublishFailure)


def test_open_reports_the_original_cause_when_the_savepoint_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The undo must not answer a failure it has nothing to roll back to.

    A savepoint can be gone while the enclosing transaction survives — releasing
    the outermost one merges it away rather than ending the transaction. Reading
    ``in_transaction`` alone cannot see that, so the undo runs against a savepoint
    that is not there and raises its own ``no such savepoint``. Letting that escape
    would replace the real cause with one that names the rollback machinery instead
    of the publish, which is exactly the misleading cause this handler exists to
    stop reporting. The original failure is what the caller sees.
    """

    db_path = tmp_path / "ledger.sqlite3"
    attempted: list[str] = []

    def release_the_savepoint_then_fail(conn: sqlite3.Connection, **_: object) -> None:
        attempted.append("publish")
        # Publish's own statements cannot do this (they contain no transaction
        # control), so it takes an edit inside them -- or this injection -- to
        # reach the state. ``RELEASE`` merges the savepoint into the surrounding
        # transaction and leaves that transaction open.
        conn.execute("RELEASE trade_event_pagination_publish")
        assert conn.in_transaction
        raise sqlite3.OperationalError("the real cause")

    monkeypatch.setattr(
        repository_trade_schema,
        "_publish_trade_event_pagination_schema",
        release_the_savepoint_then_fail,
    )

    with pytest.raises(sqlite3.OperationalError) as error:
        SQLiteOptionPositionsRepository(db_path)

    assert attempted == ["publish"]
    assert "the real cause" in str(error.value)
    # Not the undo's own complaint about the savepoint it could not find.
    assert "no such savepoint" not in str(error.value)
