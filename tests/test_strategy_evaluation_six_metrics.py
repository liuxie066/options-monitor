"""OM financial v2 six-metric contract on synthetic offline facts."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from om_strategy_core.evaluation import compare_evaluations_v2, summarize_evaluation_v2

TZ = "Asia/Shanghai"
ZERO = Decimal(0)


def _ms(day: str, hour: int = 4) -> int:
    """04:00 UTC is noon in Shanghai, so the local date equals ``day``."""
    parsed = date.fromisoformat(day)
    return int(datetime(parsed.year, parsed.month, parsed.day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def _days(first: str, last: str) -> list[str]:
    cursor, stop, out = date.fromisoformat(first), date.fromisoformat(last), []
    while cursor <= stop:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _nav_point(point_id: str, day: str, nav, **overrides) -> dict:
    value = Decimal(str(nav))
    point = {
        "day": day,
        "point_id": point_id,
        "at_ms": _ms(day),
        "base_currency": "CNY",
        "status": "complete",
        "gaps": [],
        "nav": value,
        "components": {"cash": value, "stock": ZERO, "option": ZERO, "receivable": ZERO},
        "scenario": {"status": "complete", "gaps": []},
    }
    point.update(overrides)
    return point


def _link(fact_id: str, day: str, amount, *, gaps=()) -> dict:
    return {
        "fact_id": fact_id,
        "effective_at_ms": _ms(day),
        "base_amount": None if amount is None else Decimal(str(amount)),
        "gaps": list(gaps),
    }


_DEFAULT = object()


def _cashflow(*, links=(), amount=_DEFAULT, status: str = "estimated", gaps=(), base: str = "CNY") -> dict:
    total, parsed = ZERO, []
    for link in links:
        if link["base_amount"] is not None:
            total += link["base_amount"]
        parsed.append(
            {
                "fact_id": link["fact_id"],
                "effective_at_ms": link["effective_at_ms"],
                "currency": base,
                "amount": link["base_amount"],
                "base_amount": link["base_amount"],
                "gaps": list(link["gaps"]),
            }
        )
    return {
        "status": status,
        "amount": total if amount is _DEFAULT else amount,
        "base_currency": base,
        "gaps": list(gaps),
        "links": parsed,
        "by_currency": {},
        "model": "test",
    }


def _market(at_ms: int, stocks, fx=()) -> dict:
    return {
        "stocks": [
            {
                "symbol": symbol,
                "currency": currency,
                "price": Decimal(str(price)),
                "source": "synthetic",
                "effective_at_ms": at_ms,
                "available_at_ms": at_ms,
                "valid_until_ms": at_ms + 86_400_000,
            }
            for symbol, currency, price in stocks
        ],
        "options": [],
        "fx": list(fx),
    }


def _fx(currency: str, rate, at_ms: int) -> dict:
    return {
        "fact_id": f"fx:{currency}:{at_ms}",
        "base_currency": currency,
        "quote_currency": "CNY",
        "rate": Decimal(str(rate)),
        "rate_kind": "spot",
        "effective_at_ms": at_ms,
        "observed_at_ms": at_ms,
        "source": "tencent_quote",
        "source_id": "synthetic",
        "quality": {"provider_source": "tencent_quote", "source_timestamp_verified": True},
    }


def _assignment(
    batch_id: str,
    day: str,
    *,
    symbol: str = "AAA",
    currency: str = "CNY",
    strike="10",
    multiplier: int = 100,
    contracts: int = 1,
    stock_fee="0",
    event_id: str | None = None,
    fill_id: str | None = None,
) -> dict:
    return {
        "batch_id": batch_id,
        "event_id": event_id or f"event-{batch_id}",
        "fill_id": fill_id or f"fill-{batch_id}",
        "at_ms": _ms(day),
        "symbol": symbol,
        "currency": currency,
        "strike": strike,
        "multiplier": multiplier,
        "contracts": contracts,
        "stock_fee": stock_fee,
    }


def _summarize(**overrides) -> dict:
    params = {
        "initial": _nav_point("initial", "2026-01-04", "100"),
        "daily": [_nav_point("d1", "2026-01-05", "110")],
        "expected_days": ["2026-01-05"],
        "option_cashflow": _cashflow(),
        "reconciliation": {"status": "matched"},
        "evaluation_gaps": (),
        "timezone": TZ,
        "assignments": [],
        "markets": {},
        "assignment_adjustments": None,
    }
    params.update(overrides)
    return summarize_evaluation_v2(**params)


def test_positive_option_cashflow_with_negative_nav_return_and_natural_dates():
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[
            _nav_point("d1", "2026-01-05", "900"),
            _nav_point("d2", "2026-01-06", "800"),
        ],
        expected_days=["2026-01-05", "2026-01-06"],
        option_cashflow=_cashflow(links=[_link("cash-1", "2026-01-05", "50")]),
    )
    assert result["schema_version"] == "om.strategy-evaluation.v2"
    assert result["status"] == "complete"
    assert result["gaps"] == []
    assert result["base_currency"] == "CNY"
    assert result["initial_nav"] == Decimal("1000")

    total = result["six_metrics"]["total_return"]
    assert total["status"] == "complete"
    assert total["values"]["net_profit"] == Decimal("-200")
    assert total["values"]["cumulative_return"] == Decimal("-0.2")
    assert total["values"]["ending_nav"] == Decimal("800")

    drawdown = result["six_metrics"]["max_drawdown"]
    assert drawdown["values"]["amount"] == Decimal("0.2")
    assert drawdown["peak_day"] == "2026-01-04"
    assert drawdown["trough_day"] == "2026-01-06"

    duration = result["six_metrics"]["drawdown_duration"]
    assert duration["values"]["days"] == 2
    assert duration["peak_day"] == "2026-01-04"
    assert duration["recovery_day"] is None
    assert duration["unrecovered"] is True

    cashflow = result["six_metrics"]["option_cashflow"]
    assert cashflow["status"] == "complete"
    assert cashflow["values"]["amount"] == Decimal("50")
    assert cashflow["values"]["full_months"] == 0
    assert cashflow["values"]["positive_full_months"] == 0
    assert cashflow["values"]["positive_month_ratio"] is None
    assert cashflow["values"]["worst_full_month"] is None
    assert cashflow["values"]["longest_nonpositive_months"] == 0
    assert cashflow["monthly_status"] == "not_applicable"


def test_hypothetical_scenario_gap_does_not_invalidate_actual_nav():
    point = _nav_point("d1", "2026-01-05", "950")
    point.update(
        {
            "status": "incomplete",
            "nav": None,
            "gaps": ["assignment_fee:put"],
            "scenario": {"status": "incomplete", "gaps": ["assignment_fee:put"]},
        }
    )
    result = _summarize(initial=_nav_point("initial", "2026-01-04", "1000"), daily=[point])
    assert result["status"] == "complete"
    assert result["gaps"] == []
    assert result["six_metrics"]["total_return"]["status"] == "complete"
    assert result["six_metrics"]["total_return"]["values"]["net_profit"] == Decimal("-50")


def test_equal_high_recovery_resets_drawdown_duration():
    result = _summarize(
        initial=_nav_point("initial", "2026-01-01", "100"),
        daily=[
            _nav_point("d2", "2026-01-02", "120"),
            _nav_point("d3", "2026-01-03", "90"),
            _nav_point("d4", "2026-01-04", "120"),
            _nav_point("d5", "2026-01-05", "110"),
            _nav_point("d6", "2026-01-06", "120"),
        ],
        expected_days=_days("2026-01-02", "2026-01-06"),
    )
    assert result["six_metrics"]["max_drawdown"]["values"]["amount"] == Decimal("0.25")
    assert result["six_metrics"]["max_drawdown"]["peak_day"] == "2026-01-02"
    assert result["six_metrics"]["max_drawdown"]["trough_day"] == "2026-01-03"
    duration = result["six_metrics"]["drawdown_duration"]
    assert duration["values"]["days"] == 2
    assert duration["peak_day"] == "2026-01-02"
    assert duration["recovery_day"] == "2026-01-04"
    assert duration["unrecovered"] is False


def test_drawdown_duration_reports_unrecovered_episode():
    result = _summarize(
        initial=_nav_point("initial", "2026-01-01", "100"),
        daily=[
            _nav_point("d2", "2026-01-02", "90"),
            _nav_point("d3", "2026-01-03", "100"),
            _nav_point("d4", "2026-01-04", "80"),
            _nav_point("d5", "2026-01-05", "70"),
            _nav_point("d6", "2026-01-06", "60"),
        ],
        expected_days=_days("2026-01-02", "2026-01-06"),
    )
    duration = result["six_metrics"]["drawdown_duration"]
    assert duration["values"]["days"] == 3
    assert duration["peak_day"] == "2026-01-03"
    assert duration["recovery_day"] is None
    assert duration["unrecovered"] is True


def test_tail_loss_uses_natural_days_and_explicit_full_months():
    days = _days("2026-01-01", "2026-03-15")
    navs = {day: Decimal("100") for day in _days("2026-01-01", "2026-01-19")}
    navs["2026-01-20"] = Decimal("95")
    navs.update({day: Decimal("100") for day in _days("2026-01-21", "2026-01-30")})
    navs["2026-01-31"] = Decimal("110")
    for index in range(1, 29):
        navs[f"2026-02-{index:02d}"] = Decimal("110") - Decimal(11 * index) / 28
    navs.update({day: Decimal("99") for day in _days("2026-03-01", "2026-03-15")})
    result = _summarize(
        initial=_nav_point("initial", "2025-12-31", "100"),
        daily=[_nav_point(day, day, navs[day]) for day in days],
        expected_days=days,
    )
    tail = result["six_metrics"]["tail_loss"]
    assert tail["status"] == "complete"
    assert tail["values"]["worst_daily_return"] == Decimal("-0.05")
    assert tail["daily_day"] == "2026-01-20"
    assert tail["values"]["worst_monthly_return"] == Decimal("-0.1")
    assert tail["monthly_month"] == "2026-02"
    assert tail["monthly_status"] == "complete"

    months = {month["month"]: month for month in result["six_metrics"]["option_cashflow"]["months"]}
    assert months["2025-12"]["full"] is False
    assert months["2025-12"]["status"] == "partial"
    assert months["2026-01"]["full"] is True
    assert months["2026-01"]["status"] == "complete"
    assert months["2026-01"]["amount"] == ZERO
    assert months["2026-03"]["full"] is False
    assert months["2026-03"]["status"] == "partial"
    cashflow = result["six_metrics"]["option_cashflow"]["values"]
    assert cashflow["full_months"] == 2
    assert cashflow["positive_full_months"] == 0
    assert cashflow["positive_month_ratio"] == ZERO
    assert cashflow["worst_full_month"] == ZERO
    assert cashflow["longest_nonpositive_months"] == 2


def test_option_cashflow_monthly_amounts_and_nonpositive_run():
    days = _days("2026-01-01", "2026-02-28")
    result = _summarize(
        initial=_nav_point("initial", "2025-12-31", "100"),
        daily=[_nav_point(day, day, "100") for day in days],
        expected_days=days,
        option_cashflow=_cashflow(
            links=[
                _link("jan-in", "2026-01-05", "100"),
                _link("jan-out", "2026-01-20", "-30"),
                _link("feb-out", "2026-02-10", "-20"),
            ]
        ),
    )
    cashflow = result["six_metrics"]["option_cashflow"]
    assert cashflow["status"] == "complete"
    assert cashflow["values"]["amount"] == Decimal("50")
    assert cashflow["values"]["full_months"] == 2
    assert cashflow["values"]["positive_full_months"] == 1
    assert cashflow["values"]["positive_month_ratio"] == Decimal("0.5")
    assert cashflow["values"]["worst_full_month"] == Decimal("-20")
    assert cashflow["values"]["longest_nonpositive_months"] == 1
    months = {month["month"]: month for month in cashflow["months"]}
    assert months["2025-12"]["status"] == "partial"
    assert months["2026-01"]["full"] is True
    assert months["2026-01"]["amount"] == Decimal("70")
    assert months["2026-02"]["full"] is True
    assert months["2026-02"]["amount"] == Decimal("-20")


def test_no_full_months_is_not_a_monthly_failure():
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "100"),
        daily=[_nav_point(day, day, "100") for day in _days("2026-01-05", "2026-01-10")],
        expected_days=_days("2026-01-05", "2026-01-10"),
        option_cashflow=_cashflow(links=[_link("only", "2026-01-06", "10")]),
    )
    cashflow = result["six_metrics"]["option_cashflow"]
    assert cashflow["status"] == "complete"
    assert cashflow["values"]["amount"] == Decimal("10")
    assert cashflow["values"]["full_months"] == 0
    assert cashflow["values"]["positive_full_months"] == 0
    assert cashflow["values"]["positive_month_ratio"] is None
    assert cashflow["values"]["worst_full_month"] is None
    assert cashflow["values"]["longest_nonpositive_months"] == 0
    assert cashflow["monthly_status"] == "not_applicable"


def test_missing_stock_mark_invalidates_nav_but_keeps_option_cashflow():
    point = _nav_point("d1", "2026-01-05", "1000")
    point["components"]["stock"] = None
    point.update({"status": "incomplete", "nav": None, "gaps": ["stock_mark:AAA"]})
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[point],
        option_cashflow=_cashflow(links=[_link("cash", "2026-01-05", "50")]),
    )
    assert result["status"] == "incomplete"
    total = result["six_metrics"]["total_return"]
    assert total["status"] == "incomplete"
    assert total["values"] == {"net_profit": None, "cumulative_return": None, "ending_nav": None}
    assert "actual_component:stock:2026-01-05" in total["gaps"]
    cashflow = result["six_metrics"]["option_cashflow"]
    assert cashflow["status"] == "complete"
    assert cashflow["values"]["amount"] == Decimal("50")


def test_invalid_duplicate_and_nonfinite_inputs_are_rejected():
    with pytest.raises(ValueError, match="consecutive"):
        _summarize(expected_days=["2026-01-05", "2026-01-07"])
    with pytest.raises(ValueError, match="IANA"):
        _summarize(timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="duplicate daily day"):
        _summarize(
            daily=[_nav_point("d1", "2026-01-05", "100"), _nav_point("d2", "2026-01-05", "101")],
            expected_days=["2026-01-05", "2026-01-06"],
        )
    with pytest.raises(ValueError, match="duplicate cashflow fact_id"):
        _summarize(
            option_cashflow=_cashflow(links=[_link("same", "2026-01-05", "1"), _link("same", "2026-01-05", "2")])
        )
    with pytest.raises(ValueError, match="finite decimal"):
        _summarize(
            assignments=[_assignment("b1", "2026-01-05", stock_fee="NaN")],
            markets={"d1": _market(_ms("2026-01-05"), [("AAA", "CNY", "9")])},
        )
    with pytest.raises(ValueError, match="required"):
        _summarize(
            assignments=[_assignment("b1", "2026-01-05", strike=True)],
            markets={"d1": _market(_ms("2026-01-05"), [("AAA", "CNY", "9")])},
        )
    with pytest.raises(ValueError, match="duplicate assignment batch_id"):
        _summarize(
            assignments=[
                _assignment("b1", "2026-01-05"),
                _assignment("b1", "2026-01-05", fill_id="other"),
            ]
        )


def test_assignment_peak_sums_same_day_native_loss_and_charges_fee_once():
    days = _days("2026-01-05", "2026-01-07")
    prices = {
        "2026-01-05": {"AAA": "9.05", "BBB": "20.50"},
        "2026-01-06": {"AAA": "10.10", "BBB": "17.00"},
        "2026-01-07": {"AAA": "10.10", "BBB": "17.00"},
    }
    markets = {
        day: _market(
            _ms(day),
            [("AAA", "CNY", prices[day]["AAA"]), ("BBB", "CNY", prices[day]["BBB"])],
        )
        for day in days
    }
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        assignments=[
            _assignment("aaa", "2026-01-05", symbol="AAA", strike="10", stock_fee="5"),
            _assignment("bbb", "2026-01-05", symbol="BBB", strike="20", stock_fee="0"),
        ],
        markets=markets,
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-07"),
            "events": [],
        },
    )
    exposure = result["six_metrics"]["assignment_exposure"]
    assert exposure["status"] == "complete"
    assert exposure["values"]["peak_same_day_loss"] == Decimal("300")
    assert exposure["values"]["ending_unreleased_cost_ratio"] == Decimal("3.005")
    assert exposure["values"]["peak_unreleased_cost_ratio"] == Decimal("3.005")

    batches = {batch["batch_id"]: batch for batch in exposure["batches"]}
    assert batches["aaa"]["cost"] == Decimal("1005")
    assert batches["aaa"]["native_max_unrealized_loss"] == Decimal("100")
    assert batches["aaa"]["native_max_unrealized_loss_ratio"] == Decimal("100") / Decimal("1005")
    assert batches["aaa"]["held_natural_days"] == 2
    assert batches["aaa"]["unended"] is True
    assert batches["bbb"]["native_max_unrealized_loss"] == Decimal("300")


def test_assignment_foreign_stock_native_loss_is_separated_from_fx():
    days = _days("2026-01-05", "2026-01-07")
    prices = {"2026-01-05": "9.0", "2026-01-06": "9.5", "2026-01-07": "10.0"}
    rates = {"2026-01-05": "0.5", "2026-01-06": "0.6", "2026-01-07": "0.9"}
    markets = {
        day: _market(
            _ms(day),
            [("AAA", "HKD", prices[day])],
            fx=[_fx("HKD", rates[day], _ms(day))],
        )
        for day in days
    }
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        assignments=[_assignment("hkd", "2026-01-05", currency="HKD", strike="10", stock_fee="0")],
        markets=markets,
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-07"),
            "events": [],
        },
    )
    exposure = result["six_metrics"]["assignment_exposure"]
    assert exposure["status"] == "complete"
    assert exposure["values"]["peak_same_day_loss"] == Decimal("50")
    batch = exposure["batches"][0]
    assert batch["native_max_unrealized_loss"] == Decimal("100")
    assert batch["native_max_unrealized_loss_ratio"] == Decimal("0.1")
    assert exposure["values"]["peak_unreleased_cost_ratio"] == Decimal("0.9")
    assert exposure["values"]["ending_unreleased_cost_ratio"] == Decimal("0.9")


def _adjustment_markets(days):
    return {day: _market(_ms(day), [("AAA", "CNY", "9")]) for day in days}


def test_assignment_adjustment_evidence_is_required_only_when_assignments_exist():
    days = _days("2026-01-05", "2026-01-06")
    no_assignment = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        assignments=[],
        assignment_adjustments=None,
    )["six_metrics"]["assignment_exposure"]
    assert no_assignment["status"] == "not_applicable"
    assert no_assignment["values"] == {
        "peak_same_day_loss": ZERO,
        "ending_unreleased_cost_ratio": ZERO,
        "peak_unreleased_cost_ratio": ZERO,
    }
    assert no_assignment["gaps"] == []

    common = {
        "initial": _nav_point("initial", "2026-01-04", "1000"),
        "daily": [_nav_point(day, day, "1000") for day in days],
        "expected_days": days,
        "assignments": [_assignment("b1", "2026-01-05", strike="10")],
        "markets": _adjustment_markets(days),
    }
    unknown = _summarize(**common)["six_metrics"]["assignment_exposure"]
    assert unknown["status"] == "incomplete"
    assert unknown["gaps"] == ["assignment_adjustments_unknown"]
    assert unknown["batches"][0]["native_max_unrealized_loss"] is None

    unsupported = _summarize(
        **common,
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-06"),
            "events": [{"kind": "sell"}],
        },
    )["six_metrics"]["assignment_exposure"]
    assert unsupported["status"] == "incomplete"
    assert "assignment_adjustments_unsupported" in unsupported["gaps"]
    assert unsupported["batches"][0]["native_max_unrealized_loss"] is None
    assert unsupported["batches"][0]["native_max_unrealized_loss_ratio"] is None

    narrow = _summarize(
        **common,
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05") + 1,
            "end_at_ms": _ms("2026-01-06"),
            "events": [],
        },
    )["six_metrics"]["assignment_exposure"]
    assert narrow["status"] == "incomplete"
    assert "assignment_adjustments_coverage" in narrow["gaps"]


def test_evaluation_gaps_invalidate_nav_and_assignment_but_not_option_cashflow():
    days = _days("2026-01-05", "2026-01-06")
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        option_cashflow=_cashflow(links=[_link("cash", "2026-01-05", "5")]),
        evaluation_gaps=["stopped_execution:test"],
        assignments=[_assignment("b1", "2026-01-05", strike="10")],
        markets=_adjustment_markets(days),
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-06"),
            "events": [],
        },
    )
    assert result["status"] == "incomplete"
    total = result["six_metrics"]["total_return"]
    assert total["status"] == "incomplete"
    assert "stopped_execution:test" in total["gaps"]
    cashflow = result["six_metrics"]["option_cashflow"]
    assert cashflow["status"] == "complete"
    assert cashflow["values"]["amount"] == Decimal("5")
    exposure = result["six_metrics"]["assignment_exposure"]
    assert exposure["status"] == "incomplete"
    assert "stopped_execution:test" in exposure["gaps"]


def test_assignment_missing_fx_keeps_native_batch_fields():
    days = _days("2026-01-05", "2026-01-06")
    markets = {day: _market(_ms(day), [("AAA", "HKD", "9")]) for day in days}
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        assignments=[_assignment("hkd", "2026-01-05", currency="HKD", strike="10", stock_fee="0")],
        markets=markets,
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-06"),
            "events": [],
        },
    )
    exposure = result["six_metrics"]["assignment_exposure"]
    assert exposure["status"] == "incomplete"
    assert exposure["values"]["peak_same_day_loss"] is None
    assert any(gap.startswith("assignment_fx:HKD:") for gap in exposure["gaps"])
    assert exposure["batches"][0]["native_max_unrealized_loss"] == Decimal("100")


def test_assignment_missing_fee_keeps_known_batch_terms():
    days = _days("2026-01-05", "2026-01-06")
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "1000"),
        daily=[_nav_point(day, day, "1000") for day in days],
        expected_days=days,
        assignments=[_assignment("b1", "2026-01-05", stock_fee=None)],
        markets=_adjustment_markets(days),
        assignment_adjustments={
            "start_at_ms": _ms("2026-01-05"),
            "end_at_ms": _ms("2026-01-06"),
            "events": [],
        },
    )
    exposure = result["six_metrics"]["assignment_exposure"]
    assert exposure["status"] == "incomplete"
    assert "assignment_stock_fee_missing:b1" in exposure["gaps"]
    batch = exposure["batches"][0]
    assert batch["cost"] is None
    assert batch["native_max_unrealized_loss"] is None
    assert batch["held_natural_days"] == 1
    assert batch["unended"] is True


def test_compare_v2_isolates_metric_differences_and_ignores_decimal_strings():
    common = {
        "initial": _nav_point("initial", "2026-01-04", "100"),
        "expected_days": ["2026-01-05"],
    }
    left = _summarize(daily=[_nav_point("d1", "2026-01-05", "110")], **common)
    right = _summarize(daily=[_nav_point("d1", "2026-01-05", "120")], **common)
    result = compare_evaluations_v2(evaluations={"left": left, "right": right}, reference="left")
    assert result["status"] == "complete"
    assert result["comparability"] == "complete"
    assert result["gaps"] == []
    assert result["delta"]["right"]["total_return"]["net_profit"] == Decimal("10")
    assert result["delta"]["right"]["total_return"]["cumulative_return"] == Decimal("0.1")
    assert result["delta"]["right"]["total_return"]["ending_nav"] == Decimal("10")
    assert result["delta"]["right"]["max_drawdown"]["amount"] == ZERO
    assert result["delta"]["right"]["assignment_exposure"]["peak_same_day_loss"] == ZERO
    assert result["delta"]["left"]["total_return"]["net_profit"] == ZERO

    broken = deepcopy(right)
    broken["six_metrics"]["tail_loss"]["status"] = "incomplete"
    broken["six_metrics"]["tail_loss"]["values"] = {
        "worst_daily_return": None,
        "worst_monthly_return": None,
    }
    broken["six_metrics"]["total_return"]["values"]["cumulative_return"] = "0.1"
    result = compare_evaluations_v2(evaluations={"left": left, "broken": broken}, reference="left")
    assert result["status"] == "incomplete"
    assert result["comparability"] == "incomplete"
    assert result["delta"]["broken"]["total_return"]["net_profit"] == Decimal("10")
    assert result["delta"]["broken"]["total_return"]["cumulative_return"] is None
    assert result["delta"]["broken"]["tail_loss"]["worst_daily_return"] is None
    assert "metric_incomparable:broken:tail_loss" in result["gaps"]

    mismatched = deepcopy(right)
    mismatched["coverage"]["last_day"] = "2026-01-06"
    result = compare_evaluations_v2(evaluations={"left": left, "mismatched": mismatched}, reference="left")
    assert result["comparability"] == "incomplete"
    assert result["status"] == "incomplete"
    assert "coverage_mismatch:mismatched" in result["gaps"]
    assert all(value is None for metric in result["delta"]["mismatched"].values() for value in metric.values())


def test_assignment_uses_initial_nav_and_accepts_partial_assignments_of_one_fill():
    days = _days("2026-01-05", "2026-01-06")
    result = _summarize(
        initial=_nav_point("initial", "2026-01-04", "2000"),
        daily=[_nav_point(days[0], days[0], "1000"), _nav_point(days[1], days[1], "500")],
        expected_days=days,
        assignments=[_assignment("a", days[0], fill_id="same"), _assignment("b", days[1], fill_id="same")],
        markets=_adjustment_markets(days),
        assignment_adjustments={"start_at_ms": _ms(days[0]), "end_at_ms": _ms(days[1]), "events": []},
    )
    metric = result["six_metrics"]["assignment_exposure"]
    assert metric["status"] == "complete"
    assert metric["values"]["peak_unreleased_cost_ratio"] == Decimal("1")
    assert metric["values"]["ending_unreleased_cost_ratio"] == Decimal("1")
    assert len(metric["batches"]) == 2


def test_initial_on_month_first_is_partial_not_full():
    result = _summarize(
        initial=_nav_point("initial", "2026-01-01", "100"),
        daily=[_nav_point(day, day, "100") for day in _days("2026-01-02", "2026-01-31")],
        expected_days=_days("2026-01-02", "2026-01-31"),
    )
    assert result["six_metrics"]["tail_loss"]["monthly_status"] == "not_applicable"
    cash = result["six_metrics"]["option_cashflow"]
    assert cash["monthly_status"] == "not_applicable"
    assert cash["months"][0]["full"] is False


def test_unmatched_cash_reconciliation_does_not_certify_cashflow():
    result = _summarize(reconciliation={"status": "mismatch"})
    metric = result["six_metrics"]["option_cashflow"]
    assert metric["status"] == "incomplete"
    assert "cash_reconciliation" in metric["gaps"]


def test_equal_high_resets_drawdown_peak_date():
    result = _summarize(
        daily=[_nav_point("a", "2026-01-05", "100"), _nav_point("b", "2026-01-06", "90")],
        expected_days=["2026-01-05", "2026-01-06"],
    )
    assert result["six_metrics"]["max_drawdown"]["peak_day"] == "2026-01-05"


def test_assignment_and_no_assignment_have_comparable_zero_baseline():
    base = _summarize()
    assigned = deepcopy(base)
    metric = assigned["six_metrics"]["assignment_exposure"]
    metric["status"] = "complete"
    metric["values"]["peak_same_day_loss"] = Decimal("20")
    compared = compare_evaluations_v2(evaluations={"base": base, "assigned": assigned}, reference="base")
    assert compared["comparability"] == "complete"
    assert compared["delta"]["assigned"]["assignment_exposure"]["peak_same_day_loss"] == Decimal("20")


def test_cash_links_outside_exact_period_and_unexplained_valuation_are_incomplete():
    result = _summarize(option_cashflow=_cashflow(links=[_link("old", "2026-01-04", "2")]))
    assert "cashflow_link_outside_coverage:old" in result["gaps"]
    result = _summarize(daily=[_nav_point("d1", "2026-01-05", "110", status="incomplete")])
    assert "valuation_status_unverified:2026-01-05" in result["gaps"]
    result = _summarize(daily=[_nav_point("d1", "2026-01-05", "110", at_ms=_ms("2026-01-06"))])
    assert "valuation_date_mismatch:2026-01-05" in result["gaps"]


def test_stopped_execution_does_not_turn_partial_cash_into_full_period_income():
    result = _summarize(evaluation_gaps=["cash_path_stopped:execution_fee_missing:fill"])
    metric = result["six_metrics"]["option_cashflow"]
    assert metric["status"] == "incomplete"
    assert "cash_path_stopped:execution_fee_missing:fill" in metric["gaps"]


@pytest.mark.parametrize("point_kind", ["initial", "daily"])
@pytest.mark.parametrize("reason", ["cash_path_stopped", "cash_path_stopped:execution_fee_missing:fill"])
def test_point_cash_stop_invalidates_cashflow(point_kind, reason):
    point = _nav_point("initial", "2026-01-04", "100", status="incomplete", gaps=[reason])
    overrides = {"initial": point}
    if point_kind == "daily":
        point.update(day="2026-01-05", point_id="d1", at_ms=_ms("2026-01-05"))
        overrides = {"daily": [point]}
    metric = _summarize(**overrides)["six_metrics"]["option_cashflow"]
    assert metric["status"] == "incomplete"
    assert reason in metric["gaps"]
    assert all(value is None for value in metric["values"].values())


def test_outside_cash_link_cannot_certify_full_month_amount():
    days = _days("2026-01-01", "2026-01-31")
    late = _link("late", days[-1], "100")
    late["effective_at_ms"] = _ms(days[-1], hour=12)
    result = _summarize(
        initial=_nav_point("initial", "2025-12-31", "100"),
        daily=[_nav_point(day, day, "100") for day in days],
        expected_days=days,
        option_cashflow=_cashflow(links=[_link("valid", days[0], "5"), late]),
    )
    metric = result["six_metrics"]["option_cashflow"]
    assert metric["status"] == "incomplete"
    assert metric["monthly_status"] == "incomplete"
    january = next(month for month in metric["months"] if month["month"] == "2026-01")
    assert january["full"] is True
    assert january["status"] == "incomplete"
    assert january["amount"] is None
    assert "cashflow_link_outside_coverage:late" in january["gaps"]


@pytest.mark.parametrize(
    "overrides,gap",
    [
        ({"evaluation_gaps": ["cash_path_stopped"]}, "cash_path_stopped"),
        ({"reconciliation": {"status": "mismatch"}}, "cash_reconciliation"),
    ],
)
def test_unknown_cash_coverage_cannot_certify_month_detail(overrides, gap):
    days = _days("2026-01-01", "2026-01-31")
    metric = _summarize(
        initial=_nav_point("initial", "2025-12-31", "100"),
        daily=[_nav_point(day, day, "100") for day in days],
        expected_days=days,
        **overrides,
    )["six_metrics"]["option_cashflow"]
    assert metric["status"] == "incomplete"
    assert metric["monthly_status"] == "incomplete"
    january = next(month for month in metric["months"] if month["month"] == "2026-01")
    assert january["status"] == "incomplete"
    assert january["amount"] is None
    assert gap in january["gaps"]
