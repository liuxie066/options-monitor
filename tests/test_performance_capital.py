from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.period import normalize_period


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _key(*, option_type: str = "put", side: str = "short", strike: float = 100) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd="2026-08-21",
    )


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    option_type: str = "put",
    side: str = "short",
    strike: float = 100,
    contracts: int = 1,
    price: float = 1,
    target_lot_id: str | None = None,
    fee_basis: str | None = "actual",
    raw: dict | None = None,
) -> TradeEvent:
    payload = dict(raw or {})
    if fee_basis:
        payload["fee_provenance"] = {"basis": fee_basis, "source": "test"}
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=_key(option_type=option_type, side=side, strike=strike),
        contracts=contracts,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        lot_id=f"lot-{event_id}" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        raw_payload=payload,
    )


def _report(
    events: list[TradeEvent],
    *,
    period: dict | None = None,
    ending_assigned_stock: dict | None = None,
) -> dict:
    projection = project_trade_events(events)
    window = normalize_period(
        period or {"period": "month", "month": "2026-05"},
        now_ms=NOW_MS,
    )
    return build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        diagnostics=[item.to_dict() for item in projection.diagnostics],
        ending_assigned_stock=ending_assigned_stock,
    ).to_dict()


def test_same_day_open_close_uses_fractional_day_notional() -> None:
    events = [
        _event("open", "open", "2026-05-04T10:00:00", price=2),
        _event("close", "close", "2026-05-04T16:00:00", price=1, target_lot_id="lot-open"),
    ]

    capital = _report(events)["capital"]

    assert capital["capital_basis"] == "notional_days_v1"
    assert capital["capital_days_by_currency"] == {"USD": 2500.0}
    assert capital["period_realized_net_annualized_efficiency"]["by_currency"] == {"USD": 14.6}
    assert capital["segments"][0]["overlap_ms"] == 6 * 60 * 60 * 1000


def test_intraday_partial_close_weights_each_quantity_segment() -> None:
    events = [
        _event("open", "open", "2026-05-01T00:00:00", contracts=2, price=2),
        _event(
            "half",
            "close",
            "2026-05-01T12:00:00",
            contracts=1,
            price=1,
            target_lot_id="lot-open",
        ),
        _event(
            "rest",
            "close",
            "2026-05-02T00:00:00",
            contracts=1,
            price=1,
            target_lot_id="lot-open",
        ),
    ]

    capital = _report(events)["capital"]

    assert capital["capital_days_by_currency"] == {"USD": 15000.0}
    assert [(row["quantity"], row["capital_days"]) for row in capital["segments"]] == [
        (2.0, 10000.0),
        (1.0, 5000.0),
    ]


def test_exact_midnight_is_exclusive_and_cross_period_lot_is_clipped() -> None:
    ending_at_boundary = [
        _event("old", "open", "2026-04-30T12:00:00"),
        _event("old-close", "close", "2026-05-01T00:00:00", target_lot_id="lot-old"),
    ]
    cross_period = [
        _event("cross", "open", "2026-04-15T12:00:00"),
        _event("cross-close", "close", "2026-06-15T12:00:00", target_lot_id="lot-cross"),
    ]

    boundary = _report(ending_at_boundary)["capital"]
    cross = _report(cross_period)["capital"]

    assert boundary["capital_days_by_currency"] == {}
    assert cross["capital_days_by_currency"] == {"USD": 310000.0}


def test_assignment_handoff_has_no_gap_or_double_count() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", price=2),
        _event(
            "assign",
            "assignment",
            "2026-05-02T00:00:00",
            price=0,
            target_lot_id="lot-put-open",
            raw={
                "stock_settlement": {
                    "side": "buy",
                    "shares": 100,
                    "price": 100,
                    "fees": 0,
                    "fee_provenance": {"basis": "actual", "source": "test"},
                }
            },
        ),
    ]
    stock = {
        "assigned_stock_lots": [
            {
                "stock_lot_id": "assigned-stock-assign",
                "source_assignment_event_id": "assign",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "assigned_at_ms": _ms("2026-05-02T00:00:00"),
                "shares_opened": 100,
                "stock_cost_basis_total": 10000,
            }
        ],
        "assigned_stock_sale_rows": [],
    }

    capital = _report(
        events,
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-02"},
        ending_assigned_stock=stock,
    )["capital"]

    assert capital["capital_days_by_currency"] == {"USD": 20000.0}
    assert [(row["exposure_kind"], row["start_at_ms"], row["end_at_ms"]) for row in capital["segments"]] == [
        ("short_put_strike_notional", _ms("2026-05-01T00:00:00"), _ms("2026-05-02T00:00:00")),
        ("assigned_stock_cost_basis", _ms("2026-05-02T00:00:00"), _ms("2026-05-03T00:00:00")),
    ]


def test_assigned_stock_partial_sale_reduces_basis_at_event_instant() -> None:
    stock = {
        "assigned_stock_lots": [
            {
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "assigned_at_ms": _ms("2026-05-01T00:00:00"),
                "shares_opened": 100,
                "stock_cost_basis_total": 10000,
            }
        ],
        "assigned_stock_sale_rows": [
            {
                "stock_event_id": "sale-half",
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "event_at": _ms("2026-05-01T12:00:00"),
                "shares": 50,
                "stock_cost_basis_sold": 5000,
            }
        ],
    }

    capital = _report(
        [],
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-01"},
        ending_assigned_stock=stock,
    )["capital"]

    assert capital["capital_days_by_currency"] == {"USD": 7500.0}
    assert [row["quantity"] for row in capital["segments"]] == [100.0, 50.0]


def test_covered_call_adds_zero_incremental_capital_but_naked_call_is_unavailable() -> None:
    call_events = [
        _event(
            "call-open",
            "open",
            "2026-05-02T00:00:00",
            option_type="call",
            side="short",
            strike=110,
            price=1,
        ),
        _event(
            "call-close",
            "close",
            "2026-05-03T00:00:00",
            option_type="call",
            side="short",
            strike=110,
            target_lot_id="lot-call-open",
        ),
    ]
    stock = {
        "assigned_stock_lots": [
            {
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "assigned_at_ms": _ms("2026-05-01T00:00:00"),
                "shares_opened": 100,
                "stock_cost_basis_total": 10000,
            }
        ],
        "assigned_stock_sale_rows": [],
        "covered_call_allocations": [{"open_event_id": "call-open", "allocation_status": "explicit"}],
    }

    covered = _report(
        call_events,
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        ending_assigned_stock=stock,
    )["capital"]
    naked = _report(call_events)["capital"]

    assert covered["capital_days_by_currency"] == {"USD": 30000.0}
    assert covered["coverage"]["zero_incremental_segment_count"] == 1
    assert covered["coverage"]["missing"] == []
    assert naked["capital_days_by_currency"] == {}
    assert naked["coverage"]["missing"] == ["capital_basis_unavailable:call-open"]


def test_fifo_covered_call_keeps_zero_capital_but_downgrades_coverage() -> None:
    events = [
        _event(
            "call-open",
            "open",
            "2026-05-02T00:00:00",
            option_type="call",
            side="short",
            strike=110,
        )
    ]
    stock = {
        "covered_call_allocations": [
            {"open_event_id": "call-open", "allocation_status": "derived_fifo"}
        ]
    }

    coverage = _report(events, ending_assigned_stock=stock)["capital"]["coverage"]

    assert coverage["status"] == "partial"
    assert coverage["missing"] == []
    assert coverage["warnings"] == ["covered_call_allocation_derived_fifo:call-open"]


def test_long_option_uses_opening_premium_debit_for_remaining_contracts() -> None:
    events = [
        _event("long-open", "open", "2026-05-01T00:00:00", side="long", contracts=2, price=3),
        _event(
            "long-half",
            "close",
            "2026-05-02T00:00:00",
            side="long",
            contracts=1,
            price=4,
            target_lot_id="lot-long-open",
        ),
    ]

    capital = _report(
        events,
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-02"},
    )["capital"]

    assert capital["capital_days_by_currency"] == {"USD": 900.0}


def test_zero_denominator_and_missing_net_pnl_are_explicit() -> None:
    zero_events = [
        _event("a-zero-open", "open", "2026-05-04T10:00:00", price=2),
        _event("z-zero-close", "close", "2026-05-04T10:00:00", price=1, target_lot_id="lot-a-zero-open"),
    ]
    missing_net_events = [
        _event("missing-open", "open", "2026-05-04T10:00:00", price=2, fee_basis=None),
        _event(
            "missing-close",
            "close",
            "2026-05-04T16:00:00",
            price=1,
            target_lot_id="lot-missing-open",
        ),
    ]

    zero = _report(zero_events)["capital"]["period_realized_net_annualized_efficiency"]
    missing_net = _report(missing_net_events)["capital"]["period_realized_net_annualized_efficiency"]

    assert zero == {
        "by_currency": {},
        "status": "not_applicable",
        "missing": ["zero_capital_days:USD"],
    }
    assert missing_net == {
        "by_currency": {},
        "status": "partial",
        "missing": ["pnl_unavailable:USD"],
    }


def test_zero_premium_long_option_is_known_zero_basis_not_missing() -> None:
    events = [
        _event("a-free-open", "open", "2026-05-04T10:00:00", side="long", price=0),
        _event(
            "z-free-close",
            "close",
            "2026-05-04T16:00:00",
            side="long",
            price=1,
            target_lot_id="lot-a-free-open",
        ),
    ]

    capital = _report(events)["capital"]

    assert capital["coverage"]["status"] == "observed"
    assert capital["coverage"]["missing"] == []
    assert capital["capital_days_by_currency"] == {}
    assert capital["period_realized_net_annualized_efficiency"] == {
        "by_currency": {},
        "status": "not_applicable",
        "missing": ["zero_capital_days:USD"],
    }


def test_efficiency_uses_exact_decimal_denominator_before_presentation_rounding() -> None:
    events = [
        _event("tiny-open", "open", "2026-05-04T10:00:00", side="long", price=0.01),
        _event(
            "tiny-close",
            "close",
            "2026-05-04T10:00:00.001",
            side="long",
            price=0.02,
            target_lot_id="lot-tiny-open",
        ),
    ]

    capital = _report(events)["capital"]

    assert capital["capital_days_by_currency"]["USD"] == pytest.approx(1 / 86_400_000)
    assert capital["period_realized_net_annualized_efficiency"]["by_currency"]["USD"] == 31_536_000_000.0


def test_capital_segments_conserve_contract_time_across_multiple_partial_closes() -> None:
    events = [
        _event("open", "open", "2026-05-01T00:00:00", contracts=3),
        _event("c1", "close", "2026-05-01T06:00:00", contracts=1, target_lot_id="lot-open"),
        _event("c2", "close", "2026-05-01T18:00:00", contracts=1, target_lot_id="lot-open"),
        _event("c3", "close", "2026-05-02T00:00:00", contracts=1, target_lot_id="lot-open"),
    ]

    segments = _report(events)["capital"]["segments"]

    contract_milliseconds = sum(row["quantity"] * row["overlap_ms"] for row in segments)
    assert contract_milliseconds == pytest.approx(
        3 * 6 * 60 * 60 * 1000 + 2 * 12 * 60 * 60 * 1000 + 1 * 6 * 60 * 60 * 1000
    )
