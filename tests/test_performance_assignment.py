from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.adapters import (
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
)


TZ = ZoneInfo("Asia/Shanghai")


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _put_key() -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )


def _repo_with_assignment(tmp_path) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(tmp_path / "assignment-performance.sqlite3")
    key = _put_key()
    repo.upsert_trade_event(
        TradeEvent(
            event_id="open-put",
            event_type="open",
            event_time_ms=_ms("2026-04-03T10:00:00"),
            contract_key=key,
            contracts=1,
            price=2.5,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id="lot-put",
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="assign-put",
            event_type="assignment",
            event_time_ms=_ms("2026-05-01T10:00:00"),
            contract_key=key,
            contracts=1,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            target_lot_id="lot-put",
            raw_payload={
                "stock_settlement": {
                    "side": "buy",
                    "shares": 100,
                    "price": 100,
                    "fees": 0,
                    "fee_provenance": {"basis": "actual", "source": "test"},
                }
            },
        )
    )
    return repo


def test_assigned_stock_boundary_projection_restates_later_valid_void(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    repo.upsert_trade_event(
        TradeEvent(
            event_id="void-assignment",
            event_type="void",
            event_time_ms=_ms("2026-07-10T10:00:00"),
            contract_key=_put_key(),
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            target_event_id="assign-put",
        )
    )

    boundary = load_assigned_stock_projection(
        load_ledger_performance_inputs(repo),
        as_of_ms=_ms("2026-06-01T00:00:00"),
        account="lx",
    )

    assert boundary["assigned_stock_lots"] == []
    assert boundary["assignment_lifecycle_rows"] == []


def test_assigned_stock_projection_uses_adjusted_covered_call_identity(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "adjusted-covered-call.sqlite3")
    put_key = _put_key()
    call_key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        position_side="short",
        strike=110,
        expiration_ymd="2026-08-21",
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="open-put",
            event_type="open",
            event_time_ms=1_000,
            contract_key=put_key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id="lot-put",
            raw_payload={"strategy_group_id": "group-a"},
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="assign-put",
            event_type="assignment",
            event_time_ms=2_000,
            contract_key=put_key,
            contracts=1,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            target_lot_id="lot-put",
            raw_payload={
                "stock_settlement": {
                    "side": "buy",
                    "shares": 100,
                    "price": 100,
                    "fees": 0,
                    "fee_provenance": {"basis": "actual", "source": "test"},
                }
            },
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="open-call",
            event_type="open",
            event_time_ms=3_000,
            contract_key=call_key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id="lot-call",
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="adjust-call-group",
            event_type="adjust",
            event_time_ms=4_000,
            contract_key=call_key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            target_lot_id="lot-call",
            raw_payload={
                "patch": {
                    "strategy_group_id": "group-a",
                    "last_action_at": 4_000,
                }
            },
        )
    )

    report = load_assigned_stock_projection(
        load_ledger_performance_inputs(repo),
        as_of_ms=5_000,
        account="lx",
    )

    assert len(report["covered_call_allocations"]) == 1
    assert not any(
        row["status"] == "covered_call_unallocated"
        for row in report["assigned_stock_review_rows"]
    )
