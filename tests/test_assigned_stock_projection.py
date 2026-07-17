from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.assigned_stock import project_assigned_stock_lifecycle


TZ = ZoneInfo("Asia/Shanghai")


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _event(
    event_id: str,
    *,
    option_type: str,
    side: str,
    position_effect: str,
    at: str,
    price: float,
    strike: float,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "assignment" if (raw_payload or {}).get("close_type") == "assignment" else position_effect,
        "trade_time_ms": _ms(at),
        "broker": "富途",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": option_type,
        "side": side,
        "position_effect": position_effect,
        "contracts": 1,
        "price": price,
        "strike": strike,
        "expiration_ymd": "2026-08-21",
        "currency": "USD",
        "multiplier": 100,
        "fees": 0,
        "raw_payload": raw_payload or {"fee_provenance": {"basis": "actual", "source": "test"}},
    }


def _base_projection(
    *,
    assigned_stock_events: list[dict[str, Any]] | None = None,
    quote: float | None = 102,
    stock_holdings: list[dict[str, Any]] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
    extra_option_lots: list[dict[str, Any]] | None = None,
    extra_allocations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    opened_at = _ms("2026-04-03T10:00:00")
    assigned_at = _ms("2026-05-01T10:00:00")
    open_put = _event(
        "open-put",
        option_type="put",
        side="sell",
        position_effect="open",
        at="2026-04-03T10:00:00",
        price=2.5,
        strike=100,
    )
    assignment = _event(
        "assign-put",
        option_type="put",
        side="buy",
        position_effect="close",
        at="2026-05-01T10:00:00",
        price=0,
        strike=100,
        raw_payload={
            "close_type": "assignment",
            "fee_provenance": {"basis": "actual", "source": "test"},
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": 100,
                "fees": 0,
                "fee_provenance": {"basis": "actual", "source": "test"},
            },
        },
    )
    trade_events = [open_put, assignment, *(extra_events or [])]
    option_lots = [
        {
            "record_id": "lot-put",
            "open_event_id": "open-put",
            "opened_at": opened_at,
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "currency": "USD",
            "contracts": 1,
            "remaining": 0,
            "price": 2.5,
            "multiplier": 100,
            "strike": 100,
            "expiration_ymd": "2026-08-21",
        },
        *(extra_option_lots or []),
    ]
    allocations = [
        {
            "event_id": "assign-put",
            "open_event_id": "open-put",
            "source_record_id": "lot-put",
            "close_type": "assignment",
            "contracts_closed": 1,
            "realized_pnl_gross": 250,
            "realized_pnl_net": 250,
            "closed_at": assigned_at,
        },
        *(extra_allocations or []),
    ]
    quotes = [] if quote is None else [{"symbol": "NVDA", "spot": quote, "quote_time_ms": _ms("2026-06-30T16:00:00")}]
    return project_assigned_stock_lifecycle(
        trade_events,
        assignment_option_rows=allocations,
        option_open_lots=option_lots,
        assigned_stock_events=assigned_stock_events,
        quote_snapshots=quotes,
        stock_holdings=stock_holdings,
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=_ms("2026-06-30T16:00:00"),
    )


def test_projection_tracks_partial_sale_principal_basis_and_missing_quote() -> None:
    sale = {
        "event_type": "sale",
        "stock_event_id": "sale-1",
        "target_stock_lot_id": "assigned-stock-assign-put",
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "side": "sell",
        "shares": 40,
        "price": 110,
        "currency": "USD",
        "fees": 2,
        "fee_provenance": {"basis": "actual", "source": "test"},
        "trade_time_ms": _ms("2026-06-15T10:00:00"),
    }

    marked = _base_projection(assigned_stock_events=[sale])
    missing = _base_projection(assigned_stock_events=[sale], quote=None)

    row = marked["assigned_stock_lots"][0]
    sale_row = marked["assigned_stock_sale_rows"][0]
    assert row["status"] == "partially_sold"
    assert row["shares_remaining"] == 60
    assert row["remaining_stock_principal_basis"] == 6000
    assert row["assigned_stock_unrealized_pnl_gross"] == 120
    assert sale_row["stock_principal_basis_sold"] == 4000
    assert sale_row["assigned_stock_realized_pnl"] == 398
    assert missing["assigned_stock_lots"][0]["assigned_stock_unrealized_pnl_gross"] is None
    assert missing["assigned_stock_review_rows"][0]["status"] == "missing_quote"


def test_projection_marks_unsupported_assignment_or_exercise_inventory_basis() -> None:
    unsupported = _event(
        "exercise-call",
        option_type="call",
        side="sell",
        position_effect="close",
        at="2026-05-01T10:00:00",
        price=0,
        strike=110,
        raw_payload={
            "close_type": "exercise",
            "stock_settlement": {"side": "sell", "shares": 100, "price": 110},
        },
    )

    report = project_assigned_stock_lifecycle(
        [unsupported],
        assignment_option_rows=[],
        option_open_lots=[],
        assigned_stock_events=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=_ms("2026-05-02T10:00:00"),
    )

    assert report["assigned_stock_lots"] == []
    assert report["unsupported_inventory_rows"][0]["status"] == "incomplete_inventory_basis"


def test_covered_call_prefers_explicit_link_and_attributes_open_unrealized_once() -> None:
    opened_at = _ms("2026-05-05T10:00:00")
    call_open = _event(
        "open-call",
        option_type="call",
        side="sell",
        position_effect="open",
        at="2026-05-05T10:00:00",
        price=2,
        strike=110,
        raw_payload={
            "fee_provenance": {"basis": "actual", "source": "test"},
            "stock_lot_id": "assigned-stock-assign-put",
        },
    )
    report = _base_projection(
        extra_events=[call_open],
        extra_option_lots=[
            {
                "record_id": "lot-call",
                "open_event_id": "open-call",
                "opened_at": opened_at,
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "call",
                "position_side": "short",
                "currency": "USD",
                "contracts": 1,
                "remaining": 1,
                "price": 2,
                "multiplier": 100,
                "strike": 110,
                "expiration_ymd": "2026-08-21",
                "unrealized_pnl_gross": 50,
                "valuation_status": "selected",
                "valuation_evidence_fact_id": "call-mark",
            }
        ],
    )

    row = report["assigned_stock_lots"][0]
    assert row["covered_call_pnl"] == 50
    assert row["covered_call_realized_pnl"] == 0
    assert row["covered_call_unrealized_pnl"] == 50
    assert row["covered_call_allocation_status"] == "explicit"
    assert row["covered_call_allocation_quality"] == "exact"
    assert row["covered_call_evidence_fact_ids"] == ["call-mark"]


def test_covered_call_fifo_downgrades_quality_and_mixed_inventory_fails_closed() -> None:
    opened_at = _ms("2026-05-05T10:00:00")
    call_open = _event(
        "open-call",
        option_type="call",
        side="sell",
        position_effect="open",
        at="2026-05-05T10:00:00",
        price=2,
        strike=110,
    )
    call_lot = {
        "record_id": "lot-call",
        "open_event_id": "open-call",
        "opened_at": opened_at,
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": "call",
        "position_side": "short",
        "currency": "USD",
        "contracts": 1,
        "remaining": 1,
        "price": 2,
        "multiplier": 100,
        "strike": 110,
        "expiration_ymd": "2026-08-21",
        "unrealized_pnl_gross": 50,
    }

    fifo = _base_projection(extra_events=[call_open], extra_option_lots=[call_lot])
    mixed = _base_projection(
        extra_events=[call_open],
        extra_option_lots=[call_lot],
        stock_holdings=[{"account": "lx", "broker": "富途", "symbol": "NVDA", "shares": 200}],
    )

    fifo_row = fifo["assigned_stock_lots"][0]
    assert fifo_row["covered_call_pnl"] == 50
    assert fifo_row["covered_call_allocation_status"] == "derived_fifo"
    assert fifo_row["covered_call_allocation_quality"] == "heuristic"
    assert fifo_row["lifecycle_quality"] == "open_marked_heuristic"
    assert mixed["assigned_stock_lots"][0]["covered_call_pnl"] == 0
    assert any(row["status"] == "covered_call_unallocated" for row in mixed["assigned_stock_review_rows"])


def test_covered_call_fails_closed_when_assigned_shares_are_sold_before_call_end() -> None:
    opened_at = _ms("2026-05-05T10:00:00")
    call_open = _event(
        "open-call",
        option_type="call",
        side="sell",
        position_effect="open",
        at="2026-05-05T10:00:00",
        price=2,
        strike=110,
    )
    sale = {
        "event_type": "sale",
        "stock_event_id": "sale-before-call-end",
        "target_stock_lot_id": "assigned-stock-assign-put",
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "side": "sell",
        "shares": 100,
        "price": 105,
        "currency": "USD",
        "fees": 0,
        "fee_provenance": {"basis": "actual", "source": "test"},
        "trade_time_ms": _ms("2026-05-20T10:00:00"),
    }
    report = _base_projection(
        assigned_stock_events=[sale],
        extra_events=[call_open],
        extra_option_lots=[
            {
                "record_id": "lot-call",
                "open_event_id": "open-call",
                "opened_at": opened_at,
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "call",
                "position_side": "short",
                "currency": "USD",
                "contracts": 1,
                "remaining": 1,
                "price": 2,
                "multiplier": 100,
                "strike": 110,
                "expiration_ymd": "2026-08-21",
                "unrealized_pnl_gross": 50,
            }
        ],
    )

    assert report["assigned_stock_lots"][0]["covered_call_pnl"] == 0
    assert any(row["status"] == "covered_call_unallocated" for row in report["assigned_stock_review_rows"])
