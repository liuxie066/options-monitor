from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.models import (
    FXRateFact,
    OptionInstrumentKey,
    OptionValuationPosition,
    ValuationMarkFact,
)
from domain.domain.performance.period import normalize_period


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _instrument(*, symbol: str = "NVDA", strike: str = "100") -> OptionInstrumentKey:
    return OptionInstrumentKey(
        symbol=symbol,
        option_type="put",
        strike=Decimal(strike),
        expiration_ymd="2026-08-21",
        currency="USD",
        multiplier=Decimal("100"),
    )


def _position(
    *,
    fee: Decimal | None = Decimal("10"),
    quality: str = "actual",
    account: str = "lx",
    symbol: str = "NVDA",
    strike: str = "100",
) -> OptionValuationPosition:
    return OptionValuationPosition(
        lot_id=f"lot-{account}-{symbol}-{strike}",
        account=account,
        broker="futu",
        instrument=_instrument(symbol=symbol, strike=strike),
        position_side="short",
        contracts_open=1,
        open_price=Decimal("2"),
        open_fee_remaining=fee,
        open_fee_quality=quality,
        opened_at_ms=_ms("2026-04-01T10:00:00"),
    )


def _mark(
    fact_id: str,
    price: str,
    at_ms: int,
    *,
    symbol: str = "NVDA",
    strike: str = "100",
) -> ValuationMarkFact:
    return ValuationMarkFact(
        fact_id=fact_id,
        instrument=_instrument(symbol=symbol, strike=strike),
        price=Decimal(price),
        mark_kind="official_close",
        effective_at_ms=at_ms,
        observed_at_ms=at_ms,
        source="official_close",
        source_id=fact_id,
    )


def _fx(fact_id: str, rate: str, at_ms: int) -> FXRateFact:
    return FXRateFact(
        fact_id=fact_id,
        base_currency="USD",
        quote_currency="CNY",
        rate=Decimal(rate),
        rate_kind="official_close",
        effective_at_ms=at_ms,
        observed_at_ms=at_ms,
        source="official_close",
        source_id=fact_id,
    )


def _window():
    return normalize_period({"period": "month", "month": "2026-05"}, now_ms=NOW_MS)


def test_period_total_is_realized_plus_end_unrealized_minus_opening_unrealized() -> None:
    window = _window()
    position = _position()
    report = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        opening_positions=[position],
        ending_positions=[position],
        valuation_marks=[
            _mark("open-mark", "1.5", window.valuation_open_at_ms),
            _mark("end-mark", "1.0", window.valuation_end_at_ms),
        ],
        fx_rates=[
            _fx("open-fx", "7.0", window.valuation_open_at_ms),
            _fx("end-fx", "7.2", window.valuation_end_at_ms),
        ],
    ).to_dict()

    assert report["pnl"]["opening_unrealized_gross"]["by_currency"] == {"USD": 50.0}
    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 100.0}
    assert report["pnl"]["period_total_gross"]["by_currency"] == {"USD": 50.0}
    assert report["pnl"]["period_total_gross"]["cny"] == 370.0
    assert report["pnl"]["opening_unrealized_net"]["by_currency"] == {"USD": 40.0}
    assert report["pnl"]["ending_unrealized_net"]["by_currency"] == {"USD": 90.0}
    assert report["pnl"]["period_total_net"]["by_currency"] == {"USD": 50.0}
    assert set(report["quality"]["evidence_fact_ids"]) == {
        "open-mark",
        "end-mark",
        "open-fx",
        "end-fx",
    }


def test_missing_end_mark_preserves_opening_and_realized_but_nulls_period_total() -> None:
    window = _window()
    position = _position()
    report = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        opening_positions=[position],
        ending_positions=[position],
        valuation_marks=[_mark("open-mark", "1.5", window.valuation_open_at_ms)],
        fx_rates=[_fx("open-fx", "7", window.valuation_open_at_ms)],
    ).to_dict()

    assert report["pnl"]["opening_unrealized_gross"]["by_currency"] == {"USD": 50.0}
    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {}
    assert report["pnl"]["ending_unrealized_gross"]["status"] == "partial"
    assert report["pnl"]["period_total_gross"]["by_currency"] == {}
    assert report["pnl"]["period_total_gross"]["status"] == "partial"


def test_missing_open_fee_nulls_net_only_and_missing_fx_preserves_native() -> None:
    window = _window()
    position = _position(fee=None, quality="missing")
    report = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        opening_positions=[position],
        ending_positions=[position],
        valuation_marks=[
            _mark("open-mark", "1.5", window.valuation_open_at_ms),
            _mark("end-mark", "1", window.valuation_end_at_ms),
        ],
        fx_rates=[],
    ).to_dict()

    assert report["pnl"]["period_total_gross"]["by_currency"] == {"USD": 50.0}
    assert report["pnl"]["period_total_gross"]["cny"] is None
    assert report["pnl"]["period_total_gross"]["status"] == "partial"
    assert report["pnl"]["period_total_net"]["by_currency"] == {}
    assert report["pnl"]["period_total_net"]["status"] == "partial"
    assert report["quality"]["status"] == "partial"
    assert any(item.startswith("fx:USD:") for item in report["quality"]["missing"])


def test_valuation_only_scope_and_breakdowns_conserve_portfolio_totals() -> None:
    window = _window()
    lx = _position(account="lx", symbol="NVDA", strike="100", fee=Decimal("0"))
    sy = _position(account="sy", symbol="AMD", strike="105", fee=Decimal("0"))
    marks = [
        _mark("nvda-open", "2", window.valuation_open_at_ms, symbol="NVDA", strike="100"),
        _mark("nvda-end", "1", window.valuation_end_at_ms, symbol="NVDA", strike="100"),
        _mark("amd-open", "2", window.valuation_open_at_ms, symbol="AMD", strike="105"),
        _mark("amd-end", "1.5", window.valuation_end_at_ms, symbol="AMD", strike="105"),
    ]
    fx = [_fx("open-fx", "7", window.valuation_open_at_ms), _fx("end-fx", "7", window.valuation_end_at_ms)]

    aggregate = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        opening_positions=[lx, sy],
        ending_positions=[lx, sy],
        valuation_marks=marks,
        fx_rates=fx,
    ).to_dict()
    filtered = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        account="lx",
        opening_positions=[lx, sy],
        ending_positions=[lx, sy],
        valuation_marks=marks,
        fx_rates=fx,
    ).to_dict()

    assert aggregate["scope"]["accounts"] == ["lx", "sy"]
    assert aggregate["scope"]["symbols"] == ["AMD", "NVDA"]
    assert aggregate["pnl"]["period_total_gross"]["by_currency"] == {"USD": 150.0}
    assert sum(
        item["pnl"]["period_total_gross"]["by_currency"]["USD"]
        for item in aggregate["breakdowns"]["accounts"]
    ) == 150.0
    assert sum(
        item["pnl"]["period_total_gross"]["by_currency"]["USD"]
        for item in aggregate["breakdowns"]["symbols"]
    ) == 150.0
    assert filtered["scope"]["accounts"] == ["lx"]
    assert filtered["scope"]["symbols"] == ["NVDA"]
    assert filtered["pnl"]["period_total_gross"]["by_currency"] == {"USD": 100.0}


def test_zero_unrealized_is_observed_not_not_observed() -> None:
    window = _window()
    position = _position(fee=Decimal("0"))
    report = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        ending_positions=[position],
        valuation_marks=[_mark("flat", "2", window.valuation_end_at_ms)],
        fx_rates=[_fx("fx", "7", window.valuation_end_at_ms)],
    ).to_dict()

    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 0.0}
    assert report["pnl"]["ending_unrealized_gross"]["status"] == "observed"


def test_valuation_diagnostics_follow_account_scope_even_outside_activity_period() -> None:
    window = _window()
    report = build_period_performance(
        events=[],
        allocations=[],
        period=window,
        account="lx",
        diagnostics=[
            {
                "context": "valuation",
                "code": "valuation_position_decode_failed",
                "event_id": "lx-open",
                "account": "lx",
                "broker": "futu",
                "event_time_ms": _ms("2026-04-01T10:00:00"),
            },
            {
                "context": "valuation",
                "code": "valuation_position_decode_failed",
                "event_id": "sy-open",
                "account": "sy",
                "broker": "futu",
                "event_time_ms": _ms("2026-04-01T10:00:00"),
            },
        ],
    ).to_dict()

    assert report["quality"]["warnings"] == ["valuation_position_decode_failed:lx-open"]
    assert report["quality"]["status"] == "partial"
