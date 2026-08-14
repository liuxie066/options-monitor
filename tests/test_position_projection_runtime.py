from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tracemalloc

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.position_projection_publication import (
    read_current_position_projection,
)
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.preflight import _preflight_open_event, preflight_manual_close
from src.application.ledger.position_projection_runtime import (
    CHECKPOINT_ROTATE_EVENT_COUNT,
    compare_full_and_resumed_position_projection,
    extend_event_prefix_chain,
    initial_event_prefix_chain,
    preview_position_projection_append,
    run_position_projection_fast_if_safe,
    run_position_projection_forced_full,
    run_position_projection_in_transaction,
)
from src.application.ledger.writer import persist_trade_event_object
from src.application.ledger.projector_implementation import (
    ProjectorImplementationUnavailable,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _key(*, account: str = "lx", symbol: str = "NVDA") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-06-19",
    )


def _event(
    event_id: str,
    event_type: str,
    event_time_ms: int,
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    lot_id: str | None = None,
    target_lot_id: str | None = None,
    target_event_id: str | None = None,
    contracts: int = 1,
    price: float = 1.5,
    raw_payload: dict[str, object] | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=_key(account=account, symbol=symbol),
        contracts=contracts,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id=lot_id,
        target_lot_id=target_lot_id,
        target_event_id=target_event_id,
        raw_payload=dict(raw_payload or {}),
    )


def _repo(tmp_path: Path) -> SQLiteOptionPositionsRepository:
    return SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")


def _enable(repo: SQLiteOptionPositionsRepository) -> None:
    repo.set_position_projection_checkpoint_mode("enabled")


def _checkpoint_rows(repo: SQLiteOptionPositionsRepository) -> list[dict[str, object]]:
    return repo.list_position_projection_checkpoints()


def _legacy_s1_store(tmp_path: Path) -> Path:
    db_path = tmp_path / "legacy-s1.sqlite3"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE trade_events (
              event_id TEXT PRIMARY KEY, account TEXT, event_json TEXT NOT NULL,
              trade_time_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE position_lots (
              record_id TEXT PRIMARY KEY, account TEXT, fields_json TEXT NOT NULL,
              source_event_id TEXT, expiration INTEGER, strike REAL,
              multiplier REAL, updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE position_projection_source_state (
              singleton_id INTEGER PRIMARY KEY, source_generation INTEGER NOT NULL,
              projector_schema TEXT NOT NULL,
              projector_implementation_fingerprint TEXT,
              sqlite_schema_cookie INTEGER, checkpoint_mode TEXT NOT NULL,
              last_full_verified_source_generation INTEGER, updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE position_projection_heads (
              account TEXT PRIMARY KEY, lots_generation INTEGER NOT NULL,
              built_source_generation INTEGER, built_lots_generation INTEGER,
              projection_fingerprint TEXT, lot_count INTEGER NOT NULL,
              projector_schema TEXT NOT NULL,
              projector_implementation_fingerprint TEXT, status TEXT NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            INSERT INTO position_projection_source_state VALUES
              (1, 0, 'position_projection.v1', NULL, NULL, 'disabled', NULL, 1);
            CREATE TRIGGER trg_trade_events_source_insert
            AFTER INSERT ON trade_events BEGIN
              UPDATE position_projection_source_state
              SET source_generation=source_generation+1 WHERE singleton_id=1;
            END;
            CREATE TRIGGER trg_trade_events_source_update
            AFTER UPDATE ON trade_events BEGIN
              UPDATE position_projection_source_state
              SET source_generation=source_generation+1 WHERE singleton_id=1;
            END;
            CREATE TRIGGER trg_trade_events_source_delete
            AFTER DELETE ON trade_events BEGIN
              UPDATE position_projection_source_state
              SET source_generation=source_generation+1 WHERE singleton_id=1;
            END;
            """
        )
    return db_path


def test_event_prefix_chain_golden_vector() -> None:
    seed = hashlib.sha256(b"position_projection_event_prefix.v1").hexdigest()
    assert initial_event_prefix_chain() == seed

    first = hashlib.sha256(
        bytes.fromhex(seed) + (1).to_bytes(8, "big") + b"a"
    ).hexdigest()
    second = hashlib.sha256(
        bytes.fromhex(first) + len("期".encode()).to_bytes(8, "big") + "期".encode()
    ).hexdigest()
    assert extend_event_prefix_chain(seed, "a") == first
    assert extend_event_prefix_chain(first, "期") == second
    with pytest.raises(ValueError, match="lowercase"):
        extend_event_prefix_chain(seed.upper(), b"a")


def test_s1_trigger_bodies_upgrade_once_without_schema_cookie_churn(
    tmp_path: Path,
) -> None:
    db_path = _legacy_s1_store(tmp_path)
    repo = SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        trigger_sql = {
            str(row["name"]): str(row["sql"])
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_trade_events_source_%'"
            ).fetchall()
        }
        cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    assert trigger_sql
    assert all("position_projection_checkpoints" in sql for sql in trigger_sql.values())

    reopened = SQLiteOptionPositionsRepository(db_path)
    assert reopened.position_projection_schema_cookie() == cookie


def test_forced_full_seed_then_strict_tail_uses_no_full_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    seeded = run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a", contracts=2)],
        seed_checkpoint=True,
    )
    assert seeded.mode_used == "full"
    assert seeded.checkpoint_written is True
    _enable(repo)
    checkpoint_before = [dict(row) for row in _checkpoint_rows(repo)]

    def _unexpected_full(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("strict append must not list or project the full prefix")

    monkeypatch.setattr(repo, "list_trade_events", _unexpected_full)
    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.project_stored_trade_events_to_position_lots",
        _unexpected_full,
    )
    result = run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "partial",
                "close",
                2_000,
                target_lot_id="lot-a",
                contracts=1,
                price=0.5,
            )
        ],
    )

    assert result.mode_used == "fast_tail"
    assert result.tail_event_count == 1
    assert result.checkpoint_written is False
    assert [dict(row) for row in _checkpoint_rows(repo)] == checkpoint_before
    current = read_current_position_projection(repo, account="lx")
    assert current["status"] == "trusted"
    assert current["position_lots"][0]["fields"]["contracts_open"] == 1


def test_resumed_preview_uses_checkpoint_without_reading_full_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a", contracts=2)],
        seed_checkpoint=True,
    )
    _enable(repo)

    def _unexpected_full(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("resumed preview must not read the full event prefix")

    monkeypatch.setattr(repo, "list_trade_events", _unexpected_full)
    monkeypatch.setattr(repo, "list_position_lots", _unexpected_full)
    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.project_stored_trade_events_to_position_lots",
        _unexpected_full,
    )
    preview = preview_position_projection_append(
        repo,
        [
            _event(
                "partial",
                "close",
                2_000,
                target_lot_id="lot-a",
                contracts=1,
                price=0.5,
            )
        ],
    )

    assert preview.mode_used == "resumed_tail"
    assert preview.source_event_count == 1
    assert preview.checkpoint_id
    assert preview.projection.active_lots[0].fields["contracts_open"] == 1


def test_public_single_writer_and_fifo_use_bounded_fast_runtime_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a", contracts=2)],
        seed_checkpoint=True,
    )
    _enable(repo)

    def _unexpected_full(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("eligible public writer must not read full history")

    monkeypatch.setattr(repo, "list_trade_events", _unexpected_full)
    monkeypatch.setattr(repo, "list_position_lots", _unexpected_full)
    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.project_stored_trade_events_to_position_lots",
        _unexpected_full,
    )
    result = persist_trade_event_object(
        repo,
        _event(
            "partial",
            "close",
            2_000,
            contracts=1,
            price=0.5,
        ),
    )

    assert result.created is True
    assert result.record_id == "lot-a"
    assert result.position_lot_count == 1
    assert result.details["projection_diagnostics"] == []


def test_public_close_preflight_uses_bounded_resumed_preview_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a", contracts=2)],
        seed_checkpoint=True,
    )
    _enable(repo)
    fields = repo.get_position_lot_fields("lot-a")
    assert fields is not None

    def _unexpected_full(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("eligible preflight must not read full history")

    monkeypatch.setattr(repo, "list_trade_events", _unexpected_full)
    monkeypatch.setattr(repo, "list_position_lots", _unexpected_full)
    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.project_stored_trade_events_to_position_lots",
        _unexpected_full,
    )
    result = preflight_manual_close(
        repo,
        record_id="lot-a",
        fields=fields,
        contracts_to_close=1,
        close_price=0.5,
        close_reason="test",
        as_of_ms=2_000,
    )

    assert result.status == "ok"
    assert result.contracts_open_before == 2
    assert result.contracts_open_after == 1
    assert result.details["projection_preview_mode"] == "resumed_tail"


@pytest.mark.parametrize("checkpoint_enabled", [False, True])
def test_open_preflight_preserves_candidate_projection_error_contract(
    tmp_path: Path,
    checkpoint_enabled: bool,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    if checkpoint_enabled:
        _enable(repo)

    with pytest.raises(LedgerPreflightError) as exc_info:
        _preflight_open_event(
            repo,
            event=_event("duplicate-lot", "open", 2_000, lot_id="lot-a"),
            source="test",
            operation_label="test open",
        )

    assert exc_info.value.code == "open_projection_invalid"
    assert set(exc_info.value.details) == {"event_id", "errors"}
    assert [item["code"] for item in exc_info.value.details["errors"]] == [
        "duplicate_lot_id"
    ]


def test_read_only_shadow_reports_full_resumed_parity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [
            _event("closed-open", "open", 500, lot_id="lot-closed"),
            _event(
                "closed-close",
                "close",
                600,
                target_lot_id="lot-closed",
                price=0.5,
            ),
            _event("open", "open", 1_000, lot_id="lot-a", contracts=2),
        ],
        seed_checkpoint=True,
    )
    _enable(repo)
    run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "partial",
                "close",
                2_000,
                target_lot_id="lot-a",
                contracts=1,
                price=0.5,
            )
        ],
    )

    result = compare_full_and_resumed_position_projection(repo)

    assert result["status"] == "pass"
    assert result["mismatch_count"] == 0
    assert result["full_lot_count"] == result["stored_lot_count"] == 2
    assert result["full_active_lot_count"] == result["resumed_lot_count"] == 1


def test_fast_path_avoids_global_readiness_and_all_checkpoint_payload_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("fast path must use bounded readiness/checkpoint reads")

    monkeypatch.setattr(repo, "position_projection_normalized_columns_ready", _unexpected)
    monkeypatch.setattr(repo, "list_position_projection_checkpoints", _unexpected)
    result = run_position_projection_fast_if_safe(
        repo,
        [_event("verify", "verification", 2_000, contracts=0, price=0)],
    )
    assert result.mode_used == "fast_tail"


def test_runtime_uses_process_frozen_implementation_without_source_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)

    def _unexpected_source_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("runtime must use the process-frozen implementation id")

    monkeypatch.setattr(Path, "read_bytes", _unexpected_source_read)
    result = run_position_projection_fast_if_safe(
        repo,
        [_event("verify", "verification", 2_000, contracts=0, price=0)],
    )
    assert result.mode_used == "fast_tail"


def test_zero_lot_account_remains_fast_path_eligible(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("verify-1", "verification", 1_000, contracts=0, price=0)],
        seed_checkpoint=True,
    )
    _enable(repo)

    result = run_position_projection_fast_if_safe(
        repo,
        [_event("verify-2", "verification", 2_000, contracts=0, price=0)],
    )

    assert result.mode_used == "fast_tail"
    assert result.position_lot_count == 0


def test_backdate_and_control_invalidate_and_seed_full_recovery(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 2_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)

    backdated = run_position_projection_fast_if_safe(
        repo,
        [_event("verify-old", "verification", 1_000, contracts=0, price=0)],
    )
    assert backdated.mode_used == "full"
    assert backdated.fallback_reason == "checkpoint_missing_or_invalidated"
    assert backdated.checkpoint_written is True
    assert repo.read_position_projection_source_state()["checkpoint_mode"] == "enabled"

    control = run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "void-open",
                "void",
                3_000,
                target_event_id="open",
                contracts=0,
                price=0,
            )
        ],
    )
    assert control.mode_used == "full"
    assert control.checkpoint_written is True
    assert repo.read_position_projection_source_state()["checkpoint_mode"] == "enabled"


def test_insertion_inside_existing_tail_invalidates_only_intersected_checkpoint(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                f"verify-{index:03d}",
                "verification",
                2_000 + index,
                contracts=0,
                price=0,
            )
            for index in range(CHECKPOINT_ROTATE_EVENT_COUNT)
        ],
    )
    rows_before = _checkpoint_rows(repo)
    oldest = min(rows_before, key=lambda row: int(row["prefix_event_count"]))
    newest = max(rows_before, key=lambda row: int(row["prefix_event_count"]))

    with repo._connect() as conn:  # type: ignore[attr-defined]
        assert repo.upsert_trade_event(
            _event("verify-middle", "verification", 2_050, contracts=0, price=0),
            conn=conn,
        )
        conn.commit()
    rows = {str(row["checkpoint_id"]): row for row in _checkpoint_rows(repo)}
    assert rows[str(oldest["checkpoint_id"])]["trust_status"] == "trusted"
    assert rows[str(newest["checkpoint_id"])]["trust_status"] == "invalid"


def test_repeat_full_seed_is_idempotent_and_retrusts_same_checkpoint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = run_position_projection_forced_full(repo, seed_checkpoint=True)
    second = run_position_projection_forced_full(repo, seed_checkpoint=True)
    assert second.checkpoint_id == first.checkpoint_id
    assert len(_checkpoint_rows(repo)) == 1

    repo.invalidate_position_projection_checkpoints(reason="test")
    recovered = run_position_projection_forced_full(repo, seed_checkpoint=True)
    assert recovered.checkpoint_id == first.checkpoint_id
    assert _checkpoint_rows(repo)[0]["trust_status"] == "trusted"


@pytest.mark.parametrize("event_type", ["void", "repair"])
def test_control_event_trigger_invalidates_all_checkpoints(
    tmp_path: Path,
    event_type: str,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        assert repo.upsert_trade_event(
            _event(
                f"{event_type}-open",
                event_type,
                2_000,
                target_event_id="open",
                contracts=0,
                price=0,
            ),
            conn=conn,
        )
        conn.commit()
    rows = _checkpoint_rows(repo)
    assert rows
    assert {row["trust_status"] for row in rows} == {"invalid"}
    assert {row["invalidation_reason"] for row in rows} == {"control_event_insert"}


def test_unclassified_append_invalidates_all_checkpoints(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    payload = _event("future", "future_event", 2_000, contracts=0, price=0).to_dict()
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO trade_events (
              event_id, account, event_json, trade_time_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "future",
                "lx",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                2_000,
                2_000,
                2_000,
            ),
        )
        conn.commit()

    rows = _checkpoint_rows(repo)
    assert rows
    assert {row["trust_status"] for row in rows} == {"invalid"}
    assert {row["invalidation_reason"] for row in rows} == {
        "unclassified_event_insert"
    }


def test_corrupt_checkpoint_falls_back_and_keeps_mode_untrusted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE position_projection_checkpoints SET accumulator_json = x'7B7D'"
        )
        conn.commit()

    result = run_position_projection_fast_if_safe(
        repo,
        [_event("verify", "verification", 2_000, contracts=0, price=0)],
    )

    assert result.mode_used == "full"
    assert result.fallback_reason.startswith("checkpoint_untrusted:")
    assert result.checkpoint_written is False
    assert repo.read_position_projection_source_state()["checkpoint_mode"] == "untrusted"
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"

    verified = run_position_projection_forced_full(repo, seed_checkpoint=True)
    assert verified.checkpoint_written is True
    assert _checkpoint_rows(repo)[0]["trust_status"] == "trusted"
    assert repo.read_position_projection_source_state()["checkpoint_mode"] == "untrusted"


def test_stale_parent_metadata_is_diagnostic_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    tail = [
        _event(
            f"verify-{index:03d}",
            "verification",
            2_000 + index,
            contracts=0,
            price=0,
        )
        for index in range(CHECKPOINT_ROTATE_EVENT_COUNT)
    ]
    rotated = run_position_projection_fast_if_safe(repo, tail)
    assert rotated.checkpoint_written is True
    assert rotated.parent_checkpoint_id is not None
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "DELETE FROM position_projection_checkpoints WHERE checkpoint_id=?",
            (rotated.parent_checkpoint_id,),
        )
        conn.commit()

    resumed = run_position_projection_fast_if_safe(
        repo,
        [_event("verify-next", "verification", 3_000, contracts=0, price=0)],
    )
    assert resumed.mode_used == "fast_tail"
    assert resumed.tail_event_count == 1


def test_rotation_at_100_events_and_pruning_stays_bounded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    verification_events = [
        _event(
            f"verify-{index:03d}",
            "verification",
            2_000 + index,
            contracts=0,
            price=0,
        )
        for index in range(CHECKPOINT_ROTATE_EVENT_COUNT)
    ]
    rotated = run_position_projection_fast_if_safe(repo, verification_events)

    assert rotated.mode_used == "fast_tail"
    assert rotated.tail_event_count == CHECKPOINT_ROTATE_EVENT_COUNT
    assert rotated.checkpoint_written is True
    assert rotated.parent_checkpoint_id is not None
    assert len(_checkpoint_rows(repo)) == 2

    for cycle in range(3):
        tail = [
            _event(
                f"verify-{cycle + 1}-{index:03d}",
                "verification",
                3_000 + cycle * CHECKPOINT_ROTATE_EVENT_COUNT + index,
                contracts=0,
                price=0,
            )
            for index in range(CHECKPOINT_ROTATE_EVENT_COUNT)
        ]
        run_position_projection_fast_if_safe(repo, tail)
    rows = _checkpoint_rows(repo)
    assert len(rows) <= 3
    assert any(row["verification_kind"] == "full_oracle" for row in rows)


def test_rotation_at_one_mib_tail_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(repo, seed_checkpoint=True)
    _enable(repo)
    result = run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "large-open",
                "open",
                1_000,
                lot_id="large-lot",
                raw_payload={"opaque": "x" * 1_100_000},
            )
        ],
    )
    assert result.mode_used == "fast_tail"
    assert result.tail_event_count == 1
    assert result.tail_event_bytes >= 1_048_576
    assert result.checkpoint_written is True


def test_checkpoint_decode_peak_allocation_stays_within_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [
            _event(
                f"open-{index:04d}",
                "open",
                1_000 + index,
                lot_id=f"lot-{index:04d}",
            )
            for index in range(4_000)
        ],
        seed_checkpoint=True,
    )
    _enable(repo)
    state_bytes = int(_checkpoint_rows(repo)[0]["state_bytes"])

    tracemalloc.start()
    try:
        result = run_position_projection_fast_if_safe(repo)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.mode_used == "fast_tail"
    assert peak <= max(64 * 1_048_576, 2 * state_bytes)


def test_cross_account_head_capture_and_final_close_tail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [
            _event("open-lx", "open", 1_000, lot_id="lot-lx", contracts=2),
            _event(
                "open-sy",
                "open",
                1_100,
                account="sy",
                symbol="AAPL",
                lot_id="lot-sy",
            ),
        ],
        seed_checkpoint=True,
    )
    _enable(repo)
    result = run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "close-lx",
                "close",
                2_000,
                target_lot_id="lot-lx",
                contracts=2,
                price=0.2,
            )
        ],
    )
    assert result.mode_used == "fast_tail"
    lx = read_current_position_projection(repo, account="lx")
    sy = read_current_position_projection(repo, account="sy")
    assert lx["position_lots"][0]["fields"]["status"] == "close"
    assert sy["status"] == "trusted"
    assert sy["position_lots"][0]["record_id"] == "lot-sy"
    with repo._connect() as conn:  # type: ignore[attr-defined]
        source_generation = int(
            conn.execute(
                "SELECT source_generation FROM position_projection_source_state WHERE singleton_id=1"
            ).fetchone()[0]
        )
        generations = {
            str(row["account"]): int(row["built_source_generation"])
            for row in conn.execute(
                "SELECT account, built_source_generation FROM position_projection_heads"
            ).fetchall()
        }
    assert generations == {"lx": source_generation, "sy": source_generation}


def test_new_open_fast_path_allows_its_trigger_row_but_rejects_real_collision(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(repo, seed_checkpoint=True)
    _enable(repo)
    opened = run_position_projection_fast_if_safe(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
    )
    assert opened.mode_used == "fast_tail"
    partial = run_position_projection_fast_if_safe(
        repo,
        [
            _event(
                "partial",
                "close",
                2_000,
                target_lot_id="lot-a",
                contracts=1,
                price=0.5,
            )
        ],
    )
    assert partial.mode_used == "fast_tail"

    other = SQLiteOptionPositionsRepository(tmp_path / "collision.sqlite3")
    run_position_projection_forced_full(
        other,
        [
            _event("historical", "open", 500, lot_id="lot-a"),
            _event(
                "historical-close",
                "close",
                600,
                target_lot_id="lot-a",
                price=0.5,
            ),
        ],
        seed_checkpoint=True,
    )
    _enable(other)
    with pytest.raises(ValueError, match="duplicate_lot_id"):
        run_position_projection_fast_if_safe(
            other,
            [_event("open", "open", 1_000, lot_id="lot-a")],
        )
    assert other.count_trade_events() == 2


def test_fast_path_rejects_reopened_checkpoint_lot_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    closed = run_position_projection_fast_if_safe(
        repo,
        [_event("close", "close", 2_000, target_lot_id="lot-a", price=0.5)],
    )
    assert closed.mode_used == "fast_tail"
    closed_row = repo.get_position_lot_fields("lot-a")

    with pytest.raises(ValueError, match="duplicate_lot_id"):
        run_position_projection_fast_if_safe(
            repo,
            [_event("reopen", "open", 3_000, lot_id="lot-a")],
        )

    assert repo.count_trade_events() == 2
    assert repo.get_position_lot_fields("lot-a") == closed_row


def test_fast_path_rejects_reopened_lot_id_within_one_tail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(repo, seed_checkpoint=True)
    _enable(repo)

    with pytest.raises(ValueError, match="duplicate_lot_id"):
        run_position_projection_fast_if_safe(
            repo,
            [
                _event("open", "open", 1_000, lot_id="lot-a"),
                _event("close", "close", 2_000, target_lot_id="lot-a", price=0.5),
                _event("reopen", "open", 3_000, lot_id="lot-a"),
            ],
        )

    assert repo.count_trade_events() == 0


def test_oversized_checkpoint_is_not_written_or_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.MAX_CHECKPOINT_STATE_BYTES",
        1,
    )

    result = run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )

    assert result.mode_used == "full"
    assert result.checkpoint_written is False
    assert _checkpoint_rows(repo) == []
    assert repo.read_position_projection_source_state()["checkpoint_mode"] == "untrusted"
    assert repo.get_position_lot_fields("lot-a") is not None


@pytest.mark.parametrize(
    "stage",
    [
        "after_event_write",
        "after_tail_projection",
        "after_head_publication",
        "before_checkpoint_insert",
        "after_checkpoint_insert",
        "after_checkpoint_prune",
        "before_commit",
    ],
)
def test_failure_injection_rolls_back_event_lot_head_and_checkpoint(
    tmp_path: Path,
    stage: str,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    before_events = repo.count_trade_events()
    before_lots = repo.list_position_lots()
    before_checkpoints = [
        (row["checkpoint_id"], row["trust_status"])
        for row in _checkpoint_rows(repo)
    ]
    tail = [
        _event(
            f"verify-{index:03d}",
            "verification",
            2_000 + index,
            contracts=0,
            price=0,
        )
        for index in range(CHECKPOINT_ROTATE_EVENT_COUNT)
    ]

    def _fail(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"crash:{stage}")

    with pytest.raises(RuntimeError, match=f"crash:{stage}"):
        run_position_projection_fast_if_safe(repo, tail, failure_hook=_fail)

    assert repo.count_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert [
        (row["checkpoint_id"], row["trust_status"])
        for row in _checkpoint_rows(repo)
    ] == before_checkpoints
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"


@pytest.mark.parametrize("dml_class", ["added", "changed", "removed"])
def test_failure_after_each_lot_dml_class_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dml_class: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / f"{dml_class}.sqlite3")
    initial = []
    if dml_class in {"changed", "removed"}:
        initial = [
            _event(
                "open",
                "open",
                1_000,
                lot_id="lot-a",
                contracts=2 if dml_class == "changed" else 1,
            )
        ]
    run_position_projection_forced_full(repo, initial, seed_checkpoint=True)
    _enable(repo)
    before_events = repo.count_trade_events()
    before_lots = repo.list_position_lots()
    before_checkpoints = [dict(row) for row in _checkpoint_rows(repo)]
    before_source = repo.read_position_projection_source_state()
    original = repo.apply_position_lot_diff

    def _raise_after_real_dml(*args: object, **kwargs: object) -> object:
        diff = original(*args, **kwargs)
        assert getattr(diff, dml_class) == 1
        raise RuntimeError(f"crash-after-{dml_class}")

    monkeypatch.setattr(repo, "apply_position_lot_diff", _raise_after_real_dml)
    if dml_class == "added":
        event = _event("open", "open", 1_000, lot_id="lot-a")
    elif dml_class == "changed":
        event = _event(
            "partial",
            "close",
            2_000,
            target_lot_id="lot-a",
            contracts=1,
            price=0.5,
        )
    else:
        event = _event(
            "void",
            "void",
            2_000,
            target_event_id="open",
            contracts=0,
            price=0,
        )

    with pytest.raises(RuntimeError, match=f"crash-after-{dml_class}"):
        run_position_projection_fast_if_safe(repo, [event])

    assert repo.count_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert [dict(row) for row in _checkpoint_rows(repo)] == before_checkpoints
    assert repo.read_position_projection_source_state() == before_source


def test_metadata_update_does_not_invalidate_but_event_update_delete_do(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    checkpoint_id = str(_checkpoint_rows(repo)[0]["checkpoint_id"])
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE trade_events SET updated_at_ms = updated_at_ms + 1 WHERE event_id='open'")
        conn.commit()
    assert _checkpoint_rows(repo)[0]["trust_status"] == "trusted"

    with repo._connect() as conn:  # type: ignore[attr-defined]
        payload = json.loads(
            conn.execute("SELECT event_json FROM trade_events WHERE event_id='open'").fetchone()[0]
        )
        payload["price"] = 9
        conn.execute(
            "UPDATE trade_events SET event_json=?, updated_at_ms=updated_at_ms+1 WHERE event_id='open'",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()
    rows = {str(row["checkpoint_id"]): row for row in _checkpoint_rows(repo)}
    assert rows[checkpoint_id]["trust_status"] == "invalid"
    assert rows[checkpoint_id]["invalidation_reason"] == "event_update"

    recovered = run_position_projection_fast_if_safe(repo)
    assert recovered.mode_used == "full"
    new_id = str(recovered.checkpoint_id)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM trade_events WHERE event_id='open'")
        conn.commit()
    rows = {str(row["checkpoint_id"]): row for row in _checkpoint_rows(repo)}
    assert rows[new_id]["trust_status"] == "invalid"
    assert rows[new_id]["invalidation_reason"] == "event_delete"


def test_idempotent_retry_writes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = _event("open", "open", 1_000, lot_id="lot-a")
    run_position_projection_forced_full(repo, [event], seed_checkpoint=True)
    _enable(repo)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        source_before = int(
            conn.execute(
                "SELECT source_generation FROM position_projection_source_state WHERE singleton_id=1"
            ).fetchone()[0]
        )
        head_before = tuple(
            conn.execute(
                "SELECT built_source_generation, built_lots_generation, updated_at_ms FROM position_projection_heads WHERE account='lx'"
            ).fetchone()
        )
    checkpoint_before = [dict(row) for row in _checkpoint_rows(repo)]

    result = run_position_projection_fast_if_safe(repo, [event])

    assert result.mode_used == "unchanged"
    assert result.checkpoint_written is False
    with repo._connect() as conn:  # type: ignore[attr-defined]
        assert int(
            conn.execute(
                "SELECT source_generation FROM position_projection_source_state WHERE singleton_id=1"
            ).fetchone()[0]
        ) == source_before
        assert tuple(
            conn.execute(
                "SELECT built_source_generation, built_lots_generation, updated_at_ms FROM position_projection_heads WHERE account='lx'"
            ).fetchone()
        ) == head_before
    assert [dict(row) for row in _checkpoint_rows(repo)] == checkpoint_before


def test_idempotent_retry_does_not_skip_stale_head_recovery(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = _event("open", "open", 1_000, lot_id="lot-a")
    run_position_projection_forced_full(repo, [event], seed_checkpoint=True)
    _enable(repo)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE position_lots SET strike = strike + 1 WHERE record_id = 'lot-a'"
        )
        conn.commit()

    result = run_position_projection_fast_if_safe(repo, [event])

    assert result.mode_used == "full"
    assert result.fallback_reason == "projection_head_stale"
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"


def test_caller_owned_runtime_does_not_commit_or_rollback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conn = repo._connect()  # type: ignore[attr-defined]
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = run_position_projection_in_transaction(
            repo,
            [_event("open", "open", 1_000, lot_id="lot-a")],
            conn=conn,
            mode="forced_full",
        )
        assert result.created_flags == (True,)
        assert result.diagnostics == ()
        assert conn.in_transaction is True
        assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 1
        conn.rollback()
    finally:
        conn.close()

    assert repo.list_trade_events() == []
    assert repo.list_position_lots() == []


def test_caller_owned_runtime_preserves_mixed_batch_created_order(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    existing = _event("open-a", "open", 1_000, lot_id="lot-a")
    run_position_projection_forced_full(repo, [existing], seed_checkpoint=True)
    _enable(repo)
    conn = repo._connect()  # type: ignore[attr-defined]
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = run_position_projection_in_transaction(
            repo,
            [existing, _event("open-b", "open", 2_000, lot_id="lot-b")],
            conn=conn,
            mode="fast_if_safe",
        )
        assert result.created_flags == (False, True)
        assert result.mode_used == "fast_tail"
        assert result.diagnostics == ()
        conn.commit()
    finally:
        conn.close()

    assert [item["event_id"] for item in repo.list_trade_events()] == [
        "open-a",
        "open-b",
    ]


def test_caller_owned_runtime_error_leaves_rollback_to_caller(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conn = repo._connect()  # type: ignore[attr-defined]
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="target_lot_not_found"):
            run_position_projection_in_transaction(
                repo,
                [
                    _event(
                        "close-missing",
                        "close",
                        1_000,
                        target_lot_id="missing",
                    )
                ],
                conn=conn,
                mode="forced_full",
            )
        assert conn.in_transaction is True
        assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 1
        conn.rollback()
    finally:
        conn.close()

    assert repo.list_trade_events() == []


def test_unavailable_implementation_keeps_full_projection_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)

    def _unavailable() -> str:
        raise ProjectorImplementationUnavailable("test")

    monkeypatch.setattr(
        "src.application.ledger.position_projection_runtime.loaded_projector_implementation_fingerprint",
        _unavailable,
    )
    monkeypatch.setattr(
        "src.application.ledger.position_projection_publication.loaded_projector_implementation_fingerprint",
        _unavailable,
    )
    result = run_position_projection_fast_if_safe(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
    )

    assert result.mode_used == "full"
    assert result.fallback_reason == "projector_implementation_unavailable"
    assert result.created_flags == (True,)
    assert result.publication.heads_trusted is False
    assert result.checkpoint_written is False
    assert len(repo.list_position_lots()) == 1


@pytest.mark.parametrize("mismatch", ["implementation", "schema_cookie"])
def test_implementation_or_schema_cookie_mismatch_stays_untrusted(
    tmp_path: Path,
    mismatch: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / f"{mismatch}.sqlite3")
    run_position_projection_forced_full(
        repo,
        [_event("open", "open", 1_000, lot_id="lot-a")],
        seed_checkpoint=True,
    )
    _enable(repo)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        if mismatch == "implementation":
            conn.execute(
                "UPDATE position_projection_source_state SET projector_implementation_fingerprint=? WHERE singleton_id=1",
                ("0" * 64,),
            )
        else:
            conn.execute("CREATE TABLE schema_cookie_probe(value INTEGER)")
        conn.commit()

    result = run_position_projection_fast_if_safe(
        repo,
        [_event("verify", "verification", 2_000, contracts=0, price=0)],
    )
    assert result.mode_used == "full"
    assert result.checkpoint_written is False
    state = repo.read_position_projection_source_state()
    assert state["checkpoint_mode"] == "untrusted"
    assert result.checkpoint_id is None
