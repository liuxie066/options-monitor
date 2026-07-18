from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.period import normalize_period


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _key(*, account: str = "lx", side: str = "short", symbol: str = "NVDA") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type="put",
        position_side=side,
        strike=100,
        expiration_ymd="2026-08-21",
    )


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    account: str = "lx",
    side: str = "short",
    symbol: str = "NVDA",
    contracts: int = 1,
    price: float = 1,
    fees: float = 0,
    fee_basis: str | None = "actual",
    target_lot_id: str | None = None,
    raw: dict | None = None,
    currency: str = "USD",
    multiplier: float = 100,
) -> TradeEvent:
    payload = dict(raw or {})
    if fee_basis:
        payload["fee_provenance"] = {"basis": fee_basis, "source": "test"}
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=_key(account=account, side=side, symbol=symbol),
        contracts=contracts,
        price=price,
        currency=currency,
        source="test",
        multiplier=multiplier,
        fees=fees,
        lot_id=f"lot-{event_id}" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        raw_payload=payload,
    )


def _report(events: list[TradeEvent], *, period: dict | None = None, account: str | None = None):
    projection = project_trade_events(events)
    window = normalize_period(period or {"period": "month", "month": "2026-05"}, now_ms=NOW_MS)
    return build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        account=account,
        diagnostics=[item.to_dict() for item in projection.diagnostics],
    ).to_dict()


def test_realized_pnl_belongs_to_close_period_and_open_premium_is_not_pnl() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2, fees=0),
        _event("close", "close", "2026-05-04T10:00:00", price=1, fees=0, target_lot_id="lot-open"),
    ]

    april = _report(events, period={"period": "month", "month": "2026-04"})
    may = _report(events)

    assert april["activity"]["premium_collected_gross"]["by_currency"] == {"USD": 200.0}
    assert april["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 200.0}
    assert april["pnl"]["realized_gross"]["by_currency"] == {}
    assert may["activity"]["premium_collected_gross"]["by_currency"] == {}
    assert may["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": -100.0}
    assert may["pnl"]["realized_gross"]["by_currency"] == {"USD": 100.0}
    assert may["pnl"]["realized_net"]["by_currency"] == {"USD": 100.0}


def test_assignment_stock_principal_is_cash_movement_not_option_loss() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2.5),
        _event(
            "assign",
            "assignment",
            "2026-05-01T10:00:00",
            price=0,
            target_lot_id="lot-open",
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

    report = _report(events)

    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 0.0}
    assert report["cash"]["stock_settlement_cash_gross"]["by_currency"] == {"USD": -10000.0}
    assert report["cash"]["total_cash_change_net"]["by_currency"] == {"USD": -10000.0}
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 250.0}
    assert report["pnl"]["realized_net"]["by_currency"] == {"USD": 250.0}


def test_missing_open_fee_preserves_gross_but_nulls_only_realized_net() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2, fee_basis=None),
        _event("close", "close", "2026-05-04T10:00:00", price=1, target_lot_id="lot-open"),
    ]

    report = _report(events)

    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 100.0}
    assert report["pnl"]["realized_net"]["by_currency"] == {}
    assert report["pnl"]["realized_net"]["status"] == "partial"
    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": -100.0}
    assert report["cash"]["total_cash_change_net"]["by_currency"] == {"USD": -100.0}


def test_long_option_cash_signs_and_realized_pnl_are_opposite_short() -> None:
    events = [
        _event("open", "open", "2026-05-03T10:00:00", side="long", price=1),
        _event(
            "close",
            "close",
            "2026-05-04T10:00:00",
            side="long",
            price=2.5,
            target_lot_id="lot-open",
        ),
    ]

    report = _report(events)

    assert report["activity"]["premium_paid_gross"]["by_currency"] == {"USD": 100.0}
    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 150.0}
    assert report["cash"]["total_cash_change_net"]["by_currency"] == {"USD": 150.0}
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 150.0}


def test_breakdowns_and_scope_keep_month_account_and_symbol_dimensions() -> None:
    events = [
        _event("lx-open", "open", "2026-04-03T10:00:00", account="lx", symbol="NVDA", price=2),
        _event("sy-open", "open", "2026-05-03T10:00:00", account="sy", symbol="AAPL", price=1),
    ]

    report = _report(
        events,
        period={"period": "range", "start_date": "2026-04-01", "end_date": "2026-05-31"},
    )

    assert report["scope"]["accounts"] == ["lx", "sy"]
    assert report["scope"]["symbols"] == ["AAPL", "NVDA"]
    assert [item["month"] for item in report["breakdowns"]["monthly"]] == ["2026-04", "2026-05"]
    assert [item["account"] for item in report["breakdowns"]["accounts"]] == ["lx", "sy"]
    assert [item["symbol"] for item in report["breakdowns"]["symbols"]] == ["AAPL", "NVDA"]


def test_effective_close_without_allocation_is_explicitly_partial_not_silently_dropped() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2),
        _event(
            "close",
            "close",
            "2026-05-04T10:00:00",
            price=1,
            currency="HKD",
            target_lot_id="lot-open",
        ),
    ]

    report = _report(events)

    assert report["activity"]["contracts_closed"] == 1
    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"HKD": -100.0}
    assert report["pnl"]["realized_gross"]["by_currency"] == {}
    assert report["pnl"]["realized_gross"]["status"] == "partial"
    assert report["quality"]["status"] == "partial"


def test_voided_open_does_not_contribute_activity_or_cash() -> None:
    open_event = _event("open", "open", "2026-05-03T10:00:00", price=2)
    void = TradeEvent(
        event_id="void-open",
        event_type="void",
        event_time_ms=_ms("2026-05-04T10:00:00"),
        contract_key=open_event.contract_key,
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_event_id="open",
    )

    report = _report([open_event, void])

    assert report["activity"]["contracts_opened"] == 0
    assert report["cash"]["option_trade_cash_gross"]["status"] == "not_observed"
    assert report["rows"] == []


def test_diagnostics_are_filtered_by_period_and_scope_but_selected_decode_errors_remain_partial() -> None:
    events = [_event("lx-open", "open", "2026-05-03T10:00:00", account="lx", price=2)]
    projection = project_trade_events(events)
    window = normalize_period({"period": "month", "month": "2026-05"}, now_ms=NOW_MS)
    diagnostics = [
        {
            "event_id": "sy-bad",
            "event_time_ms": _ms("2026-05-03T11:00:00"),
            "account": "sy",
            "broker": "futu",
            "code": "performance_event_decode_failed",
        },
        {
            "event_id": "lx-old-bad",
            "event_time_ms": _ms("2026-04-03T11:00:00"),
            "account": "lx",
            "broker": "futu",
            "code": "performance_event_decode_failed",
        },
        {
            "event_id": "lx-selected-bad",
            "event_time_ms": _ms("2026-05-04T11:00:00"),
            "account": "lx",
            "broker": "futu",
            "code": "performance_event_decode_failed",
        },
    ]

    report = build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        account="lx",
        diagnostics=diagnostics,
    ).to_dict()

    assert report["quality"]["status"] == "partial"
    assert report["quality"]["warnings"] == ["performance_event_decode_failed:lx-selected-bad"]


def test_fractional_assignment_shares_fail_closed_without_losing_option_realized_pnl() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2.5),
        _event(
            "assign",
            "assignment",
            "2026-05-01T10:00:00",
            price=0,
            target_lot_id="lot-open",
            raw={"stock_settlement": {"side": "buy", "shares": 100.5, "price": 100, "fees": 0}},
        ),
    ]

    report = _report(events)

    assert report["cash"]["stock_settlement_cash_gross"]["by_currency"] == {}
    assert report["cash"]["stock_settlement_cash_gross"]["status"] == "partial"
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 250.0}


def test_invalid_event_and_allocation_currency_fail_closed_without_erasing_known_currency() -> None:
    events = [
        _event("bad-open", "open", "2026-04-03T10:00:00", price=2, currency="?"),
        _event(
            "bad-close",
            "close",
            "2026-05-04T10:00:00",
            price=1,
            currency="?",
            target_lot_id="lot-bad-open",
        ),
        _event("good-open", "open", "2026-05-05T10:00:00", price=1, currency="USD"),
    ]

    report = _report(events)

    assert report["activity"]["contracts_opened"] == 1
    assert report["activity"]["contracts_closed"] == 1
    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 100.0}
    assert report["cash"]["option_trade_cash_gross"]["status"] == "partial"
    assert report["pnl"]["realized_gross"]["by_currency"] == {}
    assert report["pnl"]["realized_gross"]["status"] == "partial"
    assert report["quality"]["status"] == "partial"
    bad_rows = [row for row in report["rows"] if row["source_event_id"] == "bad-close"]
    assert bad_rows
    assert all(row["currency"] is None for row in bad_rows)


def test_negative_assignment_shares_fail_closed_without_losing_option_realized_pnl() -> None:
    events = [
        _event("open", "open", "2026-04-03T10:00:00", price=2.5),
        _event(
            "assign",
            "assignment",
            "2026-05-01T10:00:00",
            price=0,
            target_lot_id="lot-open",
            raw={"stock_settlement": {"side": "buy", "shares": -100, "price": 100, "fees": 0}},
        ),
    ]

    report = _report(events)

    assert report["cash"]["stock_settlement_cash_gross"]["by_currency"] == {}
    assert report["cash"]["stock_settlement_cash_gross"]["status"] == "partial"
    assert report["cash"]["total_cash_change_net"]["by_currency"] == {}
    assert report["cash"]["total_cash_change_net"]["status"] == "partial"
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 250.0}
