from __future__ import annotations

from pathlib import Path

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel import build_wheel_read_model, end_wheel_lifecycle


def _assign_short_put(
    tmp_path: Path,
    *,
    wheel_start_enabled: bool,
    contracts: int = 1,
) -> tuple[SQLiteOptionPositionsRepository, str, str]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=contracts,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    result = record_manual_assignment(
        repo,
        record_id=put_lot_id,
        contracts_to_close=contracts,
        stock_side="buy",
        stock_qty=contracts * 100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="put-assignment-1",
        wheel_start_enabled=wheel_start_enabled,
    )
    assignment_event_id = str(result["result"]["event_id"])
    return repo, put_lot_id, f"assigned-stock-{assignment_event_id}"


def test_assignment_starts_wheel_and_manual_end_is_cas_idempotent(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]

    preview = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        as_of_ms=4_000,
    )
    applied = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        apply_changes=True,
        as_of_ms=4_000,
    )
    replay = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        apply_changes=True,
        as_of_ms=5_000,
    )

    assert preview["dry_run"] is True
    assert preview["lifecycle_status_after"] == "manual_ended"
    assert applied["write_applied"] is True
    assert replay["idempotent"] is True
    assert replay["write_applied"] is False
    assert len(repo.list_wheel_events(account="lx")) == 2
    terminal = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert terminal["lifecycle_status"] == "manual_ended"
    assert terminal["phase"] is None


def test_assignment_replay_does_not_backfill_wheel_start(tmp_path: Path) -> None:
    repo, put_lot_id, _stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=False,
    )

    replay = record_manual_assignment(
        repo,
        record_id=put_lot_id,
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="put-assignment-1",
        wheel_start_enabled=True,
    )

    assert replay["result"]["created"] is False
    assert repo.list_wheel_events(account="lx") == []


def test_partial_wheel_call_assignment_keeps_batch_active(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
        contracts=2,
    )
    call_lot_id = "wheel-call-lot-partial"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-partial",
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload={
                    "strategy": "wheel",
                    "leg_role": "wheel_call",
                    "source_stock_lot_id": stock_lot_id,
                },
            )
        ],
    )

    record_manual_assignment(
        repo,
        record_id=call_lot_id,
        contracts_to_close=1,
        stock_side="sell",
        stock_qty=100,
        stock_price=110,
        as_of_ms=4_000,
    )

    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert [item["event_type"] for item in repo.list_wheel_events(account="lx")] == [
        "wheel_started"
    ]
    assert batch["lifecycle_status"] == "active"
    assert batch["shares_remaining"] == 100
    assert batch["phase"] == "ready"


def test_wheel_call_assignment_closes_batch_in_same_transaction(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = "wheel-call-lot-1"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-1",
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload={
                    "strategy": "wheel",
                    "leg_role": "wheel_call",
                    "source_stock_lot_id": stock_lot_id,
                },
            )
        ],
    )

    record_manual_assignment(
        repo,
        record_id=call_lot_id,
        contracts_to_close=1,
        stock_side="sell",
        stock_qty=100,
        stock_price=110,
        as_of_ms=4_000,
        request_id="call-assignment-1",
    )

    events = repo.list_wheel_events(account="lx")
    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert [item["event_type"] for item in events] == [
        "wheel_started",
        "wheel_called_away",
    ]
    assert batch["lifecycle_status"] == "called_away"
    assert batch["shares_remaining"] == 0
    assert batch["integrity_status"] == "trusted"


def test_wheel_start_failure_rolls_back_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])

    def _fail(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("forced Wheel companion failure")

    monkeypatch.setattr(repo, "append_wheel_event_once", _fail)
    with pytest.raises(ValueError, match="forced Wheel companion failure"):
        record_manual_assignment(
            repo,
            record_id=put_lot_id,
            contracts_to_close=1,
            stock_side="buy",
            stock_qty=100,
            stock_price=100,
            as_of_ms=2_000,
            wheel_start_enabled=True,
        )

    assert [item["event_type"] for item in repo.list_trade_events()] == ["open"]
    assert repo.get_position_lot_fields(put_lot_id)["status"] == "open"
