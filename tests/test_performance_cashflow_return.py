from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.performance.engine import (
    PerformanceFact,
    _CashflowCapitalIssue,
    _cashflow_return_report,
    build_period_performance,
)
from domain.domain.performance.period import normalize_period
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.reconciliation import assess_report_coverage
from src.application.performance.service import build_option_period_performance


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _key(
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 100,
) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd="2026-12-18",
    )


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 100,
    contracts: int = 1,
    price: float = 1,
    fees: float = 0,
    fee_basis: str | None = "actual",
    target_lot_id: str | None = None,
    raw: dict | None = None,
    currency: str = "USD",
) -> TradeEvent:
    payload = dict(raw or {})
    if fee_basis:
        payload["fee_provenance"] = {"basis": fee_basis, "source": "test"}
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=_key(
            account=account,
            symbol=symbol,
            option_type=option_type,
            side=side,
            strike=strike,
        ),
        contracts=contracts,
        price=price,
        currency=currency,
        source="test",
        multiplier=100,
        fees=fees,
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
        period or {"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        now_ms=NOW_MS,
    )
    return build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        diagnostics=[item.to_dict() for item in projection.diagnostics],
        ending_assigned_stock=ending_assigned_stock,
    ).to_dict()


def test_net_cash_uses_all_four_option_actions_and_actual_fees_once() -> None:
    events = [
        _event("short-open", "open", "2026-05-01T00:00:00", price=2, fees=1),
        _event(
            "short-close",
            "close",
            "2026-05-02T00:00:00",
            price=0.5,
            fees=2,
            target_lot_id="lot-short-open",
        ),
        _event("long-open", "open", "2026-05-01T12:00:00", side="long", price=1.5, fees=3),
        _event(
            "long-close",
            "close",
            "2026-05-03T00:00:00",
            side="long",
            price=2.5,
            fees=4,
            target_lot_id="lot-long-open",
        ),
    ]

    report = _report(events)

    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 250.0}
    assert report["cash"]["option_fee_cash"]["by_currency"] == {"USD": -10.0}
    assert report["cash"]["option_net_cashflow"]["by_currency"] == {"USD": 240.0}
    assert report["cashflow_return"]["capital_days_by_currency"] == {"USD": 10225.0}
    assert report["cashflow_return"]["period_return"]["by_currency"]["USD"] == pytest.approx(
        float(Decimal(240) * Decimal(3) / Decimal(10225))
    )
    assert report["cashflow_return"]["annualized_return"]["by_currency"]["USD"] == pytest.approx(
        float(Decimal(240) * Decimal(365) / Decimal(10225))
    )


def test_empty_capital_only_and_incomplete_capital_states_are_distinct() -> None:
    empty = _report([])
    capital_only = _report(
        [
            _event("put-open", "open", "2026-04-01T00:00:00"),
            _event(
                "put-close",
                "close",
                "2026-06-01T00:00:00",
                target_lot_id="lot-put-open",
            ),
        ]
    )
    incomplete = _report(
        [
            _event(
                "call-open",
                "open",
                "2026-04-01T00:00:00",
                option_type="call",
                side="short",
            )
        ]
    )

    assert empty["cash"]["option_net_cashflow"]["status"] == "not_applicable"
    assert empty["cashflow_return"]["period_return"]["status"] == "not_applicable"
    assert capital_only["cash"]["option_net_cashflow"]["by_currency"] == {"USD": 0.0}
    assert capital_only["cashflow_return"]["period_return"] == {
        "by_currency": {"USD": 0.0},
        "status": "observed",
        "missing": [],
    }
    assert assess_report_coverage(capital_only)["status"] == "pass"
    assert incomplete["cash"]["option_net_cashflow"]["by_currency"] == {"USD": 0.0}
    assert incomplete["cashflow_return"]["period_return"]["by_currency"] == {"USD": 0.0}
    assert incomplete["cashflow_return"]["coverage"]["missing_by_currency"] == {}


def test_covered_call_uses_strike_notional_for_explicit_share_allocation() -> None:
    events = [
        _event(
            "call-open",
            "open",
            "2026-05-02T00:00:00",
            option_type="call",
            side="short",
            contracts=2,
        ),
        _event(
            "call-half",
            "close",
            "2026-05-03T00:00:00",
            option_type="call",
            side="short",
            contracts=1,
            target_lot_id="lot-call-open",
        ),
        _event(
            "call-rest",
            "close",
            "2026-05-10T00:00:00",
            option_type="call",
            side="short",
            contracts=1,
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
                "shares_opened": 300,
                "stock_cost_basis_total": 36000,
            }
        ],
        "covered_call_allocations": [
            {
                "open_event_id": "call-open",
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "shares": 200,
                "start_at_ms": _ms("2026-05-02T00:00:00"),
                "end_at_ms": _ms("2026-05-04T00:00:00"),
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
            }
        ],
    }

    report = _report(
        events,
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        ending_assigned_stock=stock,
    )

    assert report["cashflow_return"]["capital_days_by_currency"] == {"USD": 30000.0}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"] == {}


@pytest.mark.parametrize(
    "allocations",
    [
        [
            {"stock_lot_id": "stock-1", "shares": 100},
            {"stock_lot_id": "stock-1", "shares": 100},
        ],
        [{"stock_lot_id": "stock-1", "shares": 101}],
        [{"stock_lot_id": "stock-1", "shares": 100}],
    ],
)
def test_covered_call_rejects_duplicate_or_overallocated_stock_lots(allocations: list[dict]) -> None:
    start_ms = _ms("2026-05-02T00:00:00")
    end_ms = _ms("2026-05-04T00:00:00") - 1
    stock = {
        "assigned_stock_lots": [],
        "covered_call_allocations": [
            {
                "open_event_id": "call-open",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "start_at_ms": start_ms,
                "end_at_ms": end_ms,
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
                **allocation,
            }
            for allocation in allocations
        ],
    }
    report = _report(
        [
            _event(
                "call-open",
                "open",
                "2026-05-02T00:00:00",
                option_type="call",
                side="short",
            )
        ],
        ending_assigned_stock=stock,
    )

    assert report["cashflow_return"]["coverage"]["status"] == "partial"
    assert report["cashflow_return"]["capital_days_by_currency"] == {}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"]["USD"] == [
        "capital_basis_unavailable:call-open"
    ]


def test_covered_call_rejects_overlapping_allocations_above_stock_lot_capacity() -> None:
    start_ms = _ms("2026-05-02T00:00:00")
    end_ms = _ms("2026-05-04T00:00:00") - 1
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
            }
        ],
        "covered_call_allocations": [
            {
                "open_event_id": open_event_id,
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "shares": 100,
                "start_at_ms": start_ms,
                "end_at_ms": end_ms,
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
            }
            for open_event_id in ("call-open-1", "call-open-2")
        ],
    }
    report = _report(
        [
            _event(
                "call-open-1",
                "open",
                "2026-05-02T00:00:00",
                option_type="call",
                side="short",
            ),
            _event(
                "call-open-2",
                "open",
                "2026-05-02T00:00:00",
                option_type="call",
                side="short",
                strike=110,
            ),
        ],
        ending_assigned_stock=stock,
    )

    assert report["cashflow_return"]["capital_days_by_currency"] == {}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"]["USD"] == [
        "capital_basis_unavailable:call-open-1",
        "capital_basis_unavailable:call-open-2",
    ]


def test_covered_call_allows_adjacent_wheel_allocations_on_the_same_stock_lot() -> None:
    pivot_ms = _ms("2026-05-03T00:00:00")
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
            }
        ],
        "covered_call_allocations": [
            {
                "open_event_id": "call-open-1",
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "shares": 100,
                "start_at_ms": _ms("2026-05-02T00:00:00"),
                "end_at_ms": pivot_ms,
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
            },
            {
                "open_event_id": "call-open-2",
                "stock_lot_id": "stock-1",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "shares": 100,
                "start_at_ms": pivot_ms,
                "end_at_ms": _ms("2026-05-04T00:00:00"),
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
            },
        ],
    }
    report = _report(
        [
            _event(
                "call-open-1",
                "open",
                "2026-05-02T00:00:00",
                option_type="call",
                side="short",
            ),
            _event(
                "call-close-1",
                "close",
                "2026-05-03T00:00:00",
                option_type="call",
                side="short",
                target_lot_id="lot-call-open-1",
            ),
            _event(
                "call-open-2",
                "open",
                "2026-05-03T00:00:00",
                option_type="call",
                side="short",
                strike=110,
            ),
        ],
        ending_assigned_stock=stock,
    )

    assert report["cashflow_return"]["capital_days_by_currency"] == {"USD": 21000.0}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"] == {}


def test_covered_call_rejects_unresolved_explicit_stock_lot() -> None:
    report = _report(
        [
            _event(
                "call-open",
                "open",
                "2026-05-02T00:00:00",
                option_type="call",
                side="short",
                raw={"stock_lot_id": "missing-stock-lot"},
            )
        ],
        ending_assigned_stock={
            "assigned_stock_lots": [],
            "covered_call_allocations": [],
        },
    )

    assert report["cashflow_return"]["period_return"]["status"] == "partial"
    assert report["cashflow_return"]["capital_days_by_currency"] == {}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"]["USD"] == [
        "capital_basis_unavailable:call-open"
    ]


def test_service_accepts_real_as_of_allocation_for_open_covered_call(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "open-covered-call.sqlite3")
    events = [
        _event("put-open", "open", "2026-04-01T00:00:00"),
        _event(
            "assign-put",
            "assignment",
            "2026-05-01T00:00:00",
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
        _event(
            "call-open",
            "open",
            "2026-05-02T00:00:00",
            option_type="call",
            side="short",
            price=2,
            raw={"stock_lot_id": "assigned-stock-assign-put"},
        ),
    ]
    for event in events:
        repo.upsert_trade_event(event)

    report = build_option_period_performance(
        repo,
        period={"period": "range", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        account="lx",
        now_ms=NOW_MS,
        include_rows=False,
    )

    assert report["cashflow_return"]["capital_days_by_currency"] == {"USD": 20000.0}
    assert report["cashflow_return"]["period_return"]["by_currency"] == {"USD": 0.03}
    assert report["cashflow_return"]["coverage"]["missing_by_currency"] == {}


def test_ytd_sums_month_cash_and_capital_before_dividing() -> None:
    events = [
        _event("open", "open", "2026-01-15T00:00:00", price=2),
        _event(
            "close",
            "close",
            "2026-03-15T00:00:00",
            price=1,
            target_lot_id="lot-open",
        ),
    ]

    report = _report(
        events,
        period={"period": "ytd", "as_of_date": "2026-03-31"},
    )
    monthly = report["breakdowns"]["monthly"]

    assert [row["month"] for row in monthly] == ["2026-01", "2026-02", "2026-03"]
    assert monthly[1]["cash"]["option_net_cashflow"]["by_currency"] == {"USD": 0.0}
    assert monthly[1]["cashflow_return"]["period_return"]["by_currency"] == {"USD": 0.0}
    assert sum(row["cash"]["option_net_cashflow"]["by_currency"]["USD"] for row in monthly) == (
        report["cash"]["option_net_cashflow"]["by_currency"]["USD"]
    )
    assert sum(row["cashflow_return"]["capital_days_by_currency"]["USD"] for row in monthly) == (
        report["cashflow_return"]["capital_days_by_currency"]["USD"]
    )


def test_current_mtd_uses_exact_elapsed_period_duration() -> None:
    report = _report(
        [_event("open", "open", "2026-07-01T00:00:00")],
        period={"period": "mtd"},
    )
    window = normalize_period({"period": "mtd"}, now_ms=NOW_MS)
    expected_days = Decimal(window.effective_end_exclusive_at_ms - window.effective_start_at_ms) / Decimal(
        86_400_000
    )

    assert report["cashflow_return"]["period_duration_days"] == pytest.approx(float(expected_days))
    assert report["breakdowns"]["monthly"][0]["cashflow_return"]["period_duration_days"] == pytest.approx(
        float(expected_days)
    )


def test_currency_scoped_capital_issue_keeps_complete_currency_rate() -> None:
    events = [
        _event("usd-open", "open", "2026-05-01T00:00:00", price=2),
        _event(
            "usd-close",
            "close",
            "2026-05-02T00:00:00",
            price=1,
            target_lot_id="lot-usd-open",
        ),
        _event(
            "hkd-call",
            "open",
            "2026-05-01T00:00:00",
            account="sy",
            symbol="0700.HK",
            option_type="call",
            side="short",
            currency="HKD",
        ),
    ]

    cashflow = _report(events)["cashflow_return"]

    assert set(cashflow["period_return"]["by_currency"]) == {"HKD", "USD"}
    assert cashflow["period_return"]["by_currency"]["HKD"] == 0.01
    assert cashflow["coverage"]["missing_by_currency"] == {}
    assert cashflow["coverage"]["status"] == "observed"


def test_covered_call_strike_notional_combines_with_put_capital_in_same_currency() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00"),
        _event(
            "call-open",
            "open",
            "2026-05-01T00:00:00",
            symbol="AAPL",
            option_type="call",
            side="short",
        ),
    ]

    cashflow = _report(events)["cashflow_return"]

    assert cashflow["capital_days_by_currency"]["USD"] > 0
    assert cashflow["period_return"]["by_currency"] == {"USD": 0.01}
    assert cashflow["coverage"]["missing_by_currency"] == {}


def test_missing_actual_fee_keeps_gross_cash_but_suppresses_net_cash_and_rate() -> None:
    report = _report(
        [_event("open", "open", "2026-05-01T00:00:00", price=2, fee_basis=None)]
    )

    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 200.0}
    assert report["cash"]["option_net_cashflow"]["by_currency"] == {}
    assert report["cash"]["option_net_cashflow"]["status"] == "partial"
    assert report["cashflow_return"]["period_return"]["by_currency"] == {}
    assert report["cashflow_return"]["period_return"]["status"] == "partial"


def test_global_capital_issue_suppresses_all_rates_but_preserves_known_cash() -> None:
    fact = PerformanceFact(
        fact_kind="option_trade_cash_gross",
        effective_at_ms=_ms("2026-05-01T12:00:00"),
        account="lx",
        broker="futu",
        symbol="NVDA",
        currency="USD",
        amount=Decimal(100),
        source_event_id="open",
    )
    issue = _CashflowCapitalIssue(
        reason="capital_currency_unknown:unknown-call",
        source_id="unknown-call",
        account="lx",
        currency=None,
        start_at_ms=_ms("2026-05-01T00:00:00"),
        end_at_ms=_ms("2026-05-02T00:00:00"),
    )

    net_cash, cashflow = _cashflow_return_report(
        [fact],
        segments=(),
        issues=(issue,),
        start_at_ms=_ms("2026-05-01T00:00:00"),
        end_exclusive_at_ms=_ms("2026-05-02T00:00:00"),
    )

    assert net_cash["by_currency"] == {"USD": 100.0}
    assert cashflow["period_return"]["by_currency"] == {}
    assert cashflow["coverage"]["global_missing"] == ["capital_currency_unknown:unknown-call"]
