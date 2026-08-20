from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
from domain.domain.option_position_lots import OpenPositionCommand
from domain.domain.wheel import (
    build_wheel_call_rank_key,
    build_wheel_event,
    evaluate_wheel_call_candidate,
    project_wheel_lifecycles,
)
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.wheel import build_wheel_read_model


def _started_event(*, source_trade_event_id: str = "assign-put") -> dict:
    return build_wheel_event(
        event_id="wheel-start-1",
        account="lx",
        stock_lot_id="assigned-stock-assign-put",
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        source_trade_event_id=source_trade_event_id,
        payload={"request_id": "assignment:assign-put"},
    )


def _assignment_trade() -> dict:
    return {
        "event_id": "assign-put",
        "event_type": "assignment",
        "event_time_ms": 2_000,
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "multiplier": 100,
    }


def _assigned_stock(*, remaining: int = 100) -> dict:
    return {
        "_all_assigned_stock_lots": [
            {
                "stock_lot_id": "assigned-stock-assign-put",
                "source_assignment_event_id": "assign-put",
                "account": "lx",
                "symbol": "NVDA",
                "shares_opened": 100,
                "shares_remaining": remaining,
                "shares_sold": 100 - remaining,
                "assignment_price": 100,
                "assignment_fees": 0,
                "stock_cost_basis_total": 10_000,
                "sale_event_ids": [],
            }
        ],
        "assigned_stock_review_rows": [],
    }


def test_wheel_projection_is_order_independent_and_tracks_linked_call() -> None:
    call_lot = {
        "record_id": "call-lot-1",
        "fields": {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "open",
            "contracts_open": 1,
            "multiplier": 100,
            "strategy": "wheel",
            "leg_role": "wheel_call",
            "source_stock_lot_id": "assigned-stock-assign-put",
            "source_event_id": "open-call-1",
        },
    }
    call_trade = {
        "event_id": "open-call-1",
        "event_type": "open",
        "event_time_ms": 2_500,
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "position_side": "short",
        "multiplier": 100,
    }
    first = project_wheel_lifecycles(
        [_started_event()],
        [_assignment_trade(), call_trade],
        [call_lot],
        _assigned_stock(),
        3_000,
    )[0]
    replay = project_wheel_lifecycles(
        [_started_event(), _started_event()],
        [call_trade, _assignment_trade()],
        [call_lot],
        _assigned_stock(),
        3_000,
    )[0]

    assert first == replay
    assert first["lifecycle_status"] == "active"
    assert first["phase"] == "call_open"
    assert first["integrity_status"] == "trusted"
    assert first["active_call_lot_ids"] == ["call-lot-1"]


def test_wheel_projection_fails_closed_when_called_away_event_is_missing() -> None:
    closed_call = {
        "record_id": "call-lot-1",
        "fields": {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "close",
            "contracts_open": 0,
            "multiplier": 100,
            "strategy": "wheel",
            "leg_role": "wheel_call",
            "source_stock_lot_id": "assigned-stock-assign-put",
            "source_event_id": "open-call-1",
        },
    }
    call_assignment = {
        "event_id": "assign-call",
        "event_type": "assignment",
        "event_time_ms": 3_000,
        "target_lot_id": "call-lot-1",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "position_side": "short",
        "multiplier": 100,
    }

    batch = project_wheel_lifecycles(
        [_started_event()],
        [_assignment_trade(), call_assignment],
        [closed_call],
        _assigned_stock(remaining=0),
        4_000,
    )[0]

    assert batch["integrity_status"] == "conflict"
    assert batch["phase"] is None
    assert "called_away_event_missing" in batch["reason_codes"]


def test_repository_appends_wheel_event_once_and_reads_it_in_same_snapshot(
    tmp_path: Path,
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
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
    )
    assignment = next(
        item for item in repo.list_trade_events() if item["event_type"] == "assignment"
    )
    stock_lot_id = f"assigned-stock-{assignment['event_id']}"
    event = build_wheel_event(
        event_id=f"wheel-start-{assignment['event_id']}",
        account="lx",
        stock_lot_id=stock_lot_id,
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        source_trade_event_id=assignment["event_id"],
        payload={"request_id": f"assignment:{assignment['event_id']}"},
    )
    with repo._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert repo.append_wheel_event_once(event, conn=conn) is True
        assert repo.append_wheel_event_once(event, conn=conn) is False
        conn.commit()

    rows = repo.read_decision_state_rows_many(accounts=["lx"])["lx"]
    model = build_wheel_read_model(repo, "lx", 3_000)

    assert rows["account_wheel_events"] == [event]
    assert model["batches"][0]["stock_lot_id"] == stock_lot_id
    assert model["batches"][0]["phase"] == "ready"
    with repo._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE wheel_events SET occurred_at_ms = 9 WHERE event_id = ?",
            (event["event_id"],),
        )


def test_position_lot_patch_accepts_first_class_stock_lot_link() -> None:
    from domain.domain.ledger.position_fields import build_open_adjustment_patch

    patch = build_open_adjustment_patch(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "open",
            "contracts": 1,
            "contracts_closed": 0,
            "strike": 110,
            "multiplier": 100,
            "expiration_ymd": "2026-08-21",
        },
        strategy="wheel",
        leg_role="wheel_call",
        source_stock_lot_id="assigned-stock-assign-put",
        as_of_ms=3_000,
    )

    assert patch["source_stock_lot_id"] == "assigned-stock-assign-put"


def test_wheel_candidate_uses_batch_cost_floor_and_lifecycle_pnl() -> None:
    batch = {
        "shares_remaining": 100,
        "remaining_stock_cost_basis": 10_010,
        "realized_sell_put_net_pnl": 240,
        "realized_prior_call_net_pnl": 100,
        "realized_prior_stock_sale_net_pnl": 0,
    }
    candidate = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA-CALL-102",
        "strike": 102,
        "spot": 100,
        "delta": 0.31,
        "multiplier": 100,
        "net_premium": 190,
        "net_premium_cny": 1_350,
        "period_net_premium_return": 0.019,
        "annualized_net_premium_return": 0.16,
        "spread_ratio": 0.1,
        "open_interest": 500,
    }

    accepted = evaluate_wheel_call_candidate(
        batch,
        candidate,
        {"min_delta": 0.30},
        {"basis": "estimated", "amount": 15},
        1,
    )
    below_cost = evaluate_wheel_call_candidate(
        batch,
        {**candidate, "strike": 100},
        {"min_delta": 0.30},
        {"basis": "estimated", "amount": 15},
        1,
    )

    assert accepted["accepted"] is True
    assert accepted["projected_lifecycle_net_pnl_if_called"] == 705
    assert accepted["projected_lifecycle_pnl_scope"] == "final_total_if_called"
    assert below_cost["wheel_candidate_status"] == "rejected"
    assert "wheel_call_strike_below_cost_floor" in below_cost["reason_codes"]


def test_wheel_candidate_rank_uses_lifecycle_pnl_before_covered_call_ties() -> None:
    higher_lifecycle = build_wheel_call_rank_key(
        {
            "projected_lifecycle_net_pnl_if_called": 500,
            "period_net_premium_return": 0.01,
            "strike": 105,
            "contract_symbol": "LOW-PREMIUM",
        }
    )
    lower_lifecycle = build_wheel_call_rank_key(
        {
            "projected_lifecycle_net_pnl_if_called": 400,
            "period_net_premium_return": 0.03,
            "strike": 120,
            "contract_symbol": "HIGH-PREMIUM",
        }
    )

    assert higher_lifecycle["sort_tuple"] < lower_lifecycle["sort_tuple"]
