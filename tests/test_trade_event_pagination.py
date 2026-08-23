from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import (
    MAX_TRADE_EVENT_PAGE_ROWS,
    TradeEventPaginationError,
    trade_event_page,
)
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.position_projection_migration import (
    apply_position_projection_migration,
    build_position_projection_migration_inventory,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


CURSOR_KEY = "test-only-cursor-signing-key"


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
    second_ten = _page(
        repo,
        {"cursor": first_ten["next_cursor"], "limit": 10},
        account=None,
        now_epoch_s=1_001,
    )

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
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=1.5,
        currency="HKD" if symbol.endswith(".HK") else "USD",
        source="pagination_test",
        target_lot_id=f"lot_{event_id}" if needs_target else None,
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
    second = _page(
        repo,
        {"cursor": first["next_cursor"], "limit": 20, "include_total": True},
        account=None,
        now_epoch_s=1_001,
    )
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
    second = _page(
        repo,
        {"cursor": first["next_cursor"], "limit": 2},
        account=None,
        now_epoch_s=1_001,
    )

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
    with sqlite3.connect(path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = ?",
                (target.event_id,),
            ).fetchone()[0]
        )
        payload["event_time_ms"] = 0
        conn.execute(
            "UPDATE trade_events SET event_json = ?, trade_time_ms = 0 WHERE event_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), target.event_id),
        )

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
    with sqlite3.connect(path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = ?",
                (target.event_id,),
            ).fetchone()[0]
        )
        payload["event_time_ms"] = 0
        conn.execute(
            "UPDATE trade_events SET event_json = ?, trade_time_ms = 0 WHERE event_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), target.event_id),
        )

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

    second = _page(
        repo,
        {"cursor": first["next_cursor"], "limit": 2},
        account=None,
        now_epoch_s=1_001,
    )
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
