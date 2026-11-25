from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.adapters import (
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
)


TZ = ZoneInfo("Asia/Shanghai")


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _contract_key(option_type: str = "put", strike: float = 100) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        strike=strike,
        expiration_ymd="2026-08-21",
        )


def _trade(**overrides: object) -> TradeEvent:
    """The file's ``open`` event; ``overrides`` replace whole fields."""
    base: dict = {
        "event_id": "open-put",
        "event_type": "open",
        "event_time_ms": 1_000,
        "contract_key": _contract_key(),
        "contracts": 1,
        "price": 2,
        "currency": "USD",
        "source": "test",
        "multiplier": 100,
        "lot_id": "lot-put",
        # §9.2 step 3: the short put side travels as the trade side.
        "raw_payload": {"side": "sell"},
    }
    base.update(overrides)
    return TradeEvent(**base)


def _assign_put(**overrides: object) -> TradeEvent:
    """The ``assignment`` event: ``_trade``'s shape plus the assignment fields."""
    base: dict = {
        "event_id": "assign-put",
        "event_type": "assignment",
        "event_time_ms": 2_000,
        "price": 0,
        "target_lot_id": "lot-put",
        "lot_id": None,
        "raw_payload": {
            # §9.2 step 3: closing the assigned short put is a buy.
            "side": "buy",
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": 100,
                "fees": 0,
                "fee_provenance": {"basis": "actual", "source": "test"},
            },
        },
    }
    base.update(overrides)
    return _trade(**base)


def _repo_with_assignment(tmp_path) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(tmp_path / "assignment-performance.sqlite3")
    repo.upsert_trade_event(_trade(event_time_ms=_ms("2026-04-03T10:00:00"), price=2.5))
    repo.upsert_trade_event(_assign_put(event_time_ms=_ms("2026-05-01T10:00:00")))
    return repo


def test_assigned_stock_boundary_projection_restates_later_valid_void(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    repo.upsert_trade_event(
        _trade(
            event_id="void-assignment",
            event_type="void",
            event_time_ms=_ms("2026-07-10T10:00:00"),
            contracts=0,
            price=0,
            target_event_id="assign-put",
            lot_id=None,
            raw_payload={},
        )
    )

    boundary = load_assigned_stock_projection(
        load_ledger_performance_inputs(repo),
        as_of_ms=_ms("2026-06-01T00:00:00"),
        account="lx",
    )

    assert boundary["assigned_stock_lots"] == []
    assert boundary["assignment_lifecycle_rows"] == []


@pytest.mark.parametrize("include_void", [False, True])
def test_assignment_boundary_metadata_shares_validated_late_void_selection(include_void):
    from src.application.ledger import api

    opening = _trade(raw_payload={"side": "sell", "strategy": "sell_put"})
    duplicate = _trade(event_id="replaced-open", raw_payload={"side": "sell", "strategy": "wheel"})
    events = [opening, duplicate, _assign_put()]
    if include_void:
        events.append(_trade(event_id="late-void", event_type="void", event_time_ms=5000,
            contracts=0, price=0, target_event_id="replaced-open", lot_id=None, raw_payload={}))
    # A future strategy adjustment must stay outside the selected boundary,
    # even though a valid later void is deliberately retained by canonical replay.
    events.append(_trade(event_id="future-adjust", event_type="adjust", event_time_ms=4000,
        contracts=0, price=0, target_lot_id="lot-put", lot_id=None,
        raw_payload={"patch": {"strategy": "wheel"}}))
    rows = {"trade_events": [api.trade_event_application_payload(e.to_dict()) for e in events],
            "account_assigned_stock_events": []}
    if not include_void:
        with pytest.raises(ValueError, match="projection is untrusted: duplicate_lot_id"):
            api.project_assigned_stock_lifecycle_from_rows(rows, as_of_ms=3000)
        return
    result = api.project_assigned_stock_lifecycle_from_rows(rows, as_of_ms=3000)
    assert len(result["assigned_stock_lots"]) == 1
    lot = result["assigned_stock_lots"][0]
    assert lot["strategy"] == "sell_put"
    assert lot["shares_remaining"] == 100


def test_assignment_boundary_does_not_revive_later_voided_strategy_adjustment():
    from src.application.ledger import api

    events = [
        _trade(raw_payload={"side": "sell", "strategy": "sell_put"}),
        _trade(event_id="replaced-adjust", event_type="adjust", event_time_ms=1500,
            contracts=0, price=0, target_lot_id="lot-put", lot_id=None,
            raw_payload={"patch": {"strategy": "wheel"}}),
        _assign_put(),
        _trade(event_id="late-void", event_type="void", event_time_ms=5000,
            contracts=0, price=0, target_event_id="replaced-adjust", lot_id=None, raw_payload={}),
    ]
    rows = {"trade_events": [api.trade_event_application_payload(e.to_dict()) for e in events],
            "account_assigned_stock_events": []}
    result = api.project_assigned_stock_lifecycle_from_rows(rows, as_of_ms=3000)
    assert result["assigned_stock_lots"][0]["strategy"] == "sell_put"


def test_assigned_stock_projection_uses_adjusted_covered_call_identity(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "adjusted-covered-call.sqlite3")
    call_key = _contract_key(option_type="call", strike=110)
    repo.upsert_trade_event(
        _trade(raw_payload={"side": "sell", "strategy_group_id": "group-a"})
    )
    repo.upsert_trade_event(_assign_put())
    repo.upsert_trade_event(
        _trade(
            event_id="open-call",
            event_type="open",
            event_time_ms=3_000,
            contract_key=call_key,
            lot_id="lot-call",
            # §9.2 step 3: the covered call is short, so it opens with a sell.
            raw_payload={"side": "sell"},
        )
    )
    repo.upsert_trade_event(
        _trade(
            event_id="adjust-call-group",
            event_type="adjust",
            event_time_ms=4_000,
            contract_key=call_key,
            contracts=0,
            price=0,
            lot_id=None,
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
