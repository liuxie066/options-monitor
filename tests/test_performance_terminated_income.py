"""Terminal-period option income through the shared owner and public report."""
from dataclasses import replace
from decimal import Decimal

import pytest

from domain.domain.ledger import project_trade_events
from domain.domain.performance.period import PeriodRequest, normalize_performance_period
from domain.domain.performance.weighted_reducer import reduce_option_performance
from src.application.performance.service import build_option_period_performance
from test_performance_service_v2 import _Repo
from test_performance_weighted_reducer import _event, _key, _ms, _period


def _cash(result, currency="USD"):
    return result.bundle["option_net_cashflow"]["by_currency"][currency]["total"]


@pytest.mark.parametrize("period_request,expected", [
    (PeriodRequest(period="mtd"), 1),
    (PeriodRequest(period="ytd"), 2),
    (PeriodRequest(period="month", month="2026-08"), 1),
    (PeriodRequest(period="year", year=2026), 2),
])
def test_terminal_time_selects_shares_across_month_and_year_boundaries(period_request, expected):
    events = [
        _event("open", "open", "2025-12-30T10:00:00", lot_id="lot", contracts=4, price=3),
        _event("before-year", "close", "2025-12-31T23:59:59.999", target_lot_id="lot", close_type="buy_to_close"),
        _event("before-month", "close", "2026-08-31T23:59:59.999", target_lot_id="lot", close_type="buy_to_close"),
        _event("month-start", "close", "2026-09-01T00:00:00", target_lot_id="lot", close_type="buy_to_close"),
        _event("future", "close", "2026-09-02T12:00:00.001", target_lot_id="lot", close_type="buy_to_close"),
    ]
    period = normalize_performance_period(period_request, report_now_ms=_ms("2026-09-02T12:00:00"))
    result = reduce_option_performance(project_trade_events(events), period=period, basis="terminated")
    assert len(result.facts) == expected
    assert _cash(result)["amount"] == 200 * expected
    assert result.bundle["sell_option_win_rate"]["eligible_contracts"] == expected


def test_native_accounts_symbols_and_cny_income_agree_without_current_fx():
    events = []
    for account, symbol, currency, fx in (("lx", "NVDA", "USD", 7), ("sy", "0700.HK", "HKD", .9)):
        key = _key(account=account, symbol=symbol)
        events.extend([
            _event(account + "-open", "open", "2026-08-20T10:00:00", key=key, lot_id=account,
                   price=3, currency=currency, fx_rate=fx),
            _event(account + "-close", "close", "2026-09-02T10:00:00", key=key, target_lot_id=account,
                   price=1, currency=currency, fx_rate=fx, close_type="buy_to_close"),
        ])
    repo = _Repo([event.to_dict() for event in events])
    def query(**kw):
        return build_option_period_performance(repo, period=_period(), config_key="us",
                                               configured_accounts=("lx", "sy"), include_rows=True, **kw)
    report = query()
    income = report["terminated_option_net_income"]
    assert report["basis"] == "terminated"
    assert income["by_currency"]["USD"]["amount"] == income["by_currency"]["HKD"]["amount"] == 200
    assert income["cny_total"]["amount"] == 1580
    assert sum(query(account=a)["terminated_option_net_income"]["cny_total"]["amount"] for a in ("lx", "sy")) == 1580
    assert {row["key"] for row in report["breakdowns"]["symbols"]} == {"NVDA", "0700.HK"}
    assert all(row["terminated_option_net_income"] == 200 for row in report["rows"])
    assert "option_net_cashflow" not in report and "period_option_net_cashflow" not in report
    assert all("option_net_cashflow" not in row for row in report["breakdowns"]["symbols"])
    empty = query(broker="ibkr")
    assert empty["terminated_option_net_income"]["by_currency"] == {}
    assert empty["terminated_option_net_income"]["cny_total"]["amount"] == 0
    assert empty["sell_option_win_rate"]["rate"] is None


def test_cny_partial_close_allocations_conserve_persisted_rounding_across_periods():
    events = [_event("open", "open", "2026-08-20T10:00:00", lot_id="lot", contracts=3,
                     price=.003333335, fee=.000001, fx_rate=7.12345, key=_key(expiration="2026-11-30"))]
    events.extend(_event(f"close-{n}", "close", at, target_lot_id="lot", price=0,
                         close_type="buy_to_close", key=_key(expiration="2026-11-30")) for n, at in enumerate((
                             "2026-08-31T10:00:00", "2026-09-02T10:00:00", "2026-10-02T10:00:00")))
    projection = project_trade_events(events)
    def calc(request):
        window = normalize_performance_period(request, report_now_ms=_ms("2026-10-03T12:00:00"))
        return reduce_option_performance(projection, period=window, basis="terminated").bundle["option_net_cashflow"]["cny_total"]["amount"]
    months = sum((calc(PeriodRequest(period="month", month=month)) for month in ("2026-08", "2026-09", "2026-10")), Decimal(0))
    assert months == calc(PeriodRequest(period="ytd"))


@pytest.mark.parametrize("price", [1, 0.000000000001])
def test_missing_cny_preserves_native_income_but_nonzero_pending_cash_is_not_earned(price):
    opened = _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3)
    raw = dict(opened.raw_payload)
    raw.pop("cash_conversions")
    opened = replace(opened, raw_payload=raw)
    closed = _event("close", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=1, close_type="buy_to_close")
    result = reduce_option_performance(project_trade_events([opened, closed]), period=_period(), basis="terminated")
    assert _cash(result)["amount"] == 200
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] is None
    pending = replace(closed, price=price, raw_payload={**closed.raw_payload, "close_type": "cause_pending"})
    result = reduce_option_performance(project_trade_events([opened, pending]), period=_period(), basis="terminated")
    assert _cash(result)["amount"] is None
    assert "close_reason_pending" in _cash(result)["missing"]


def test_cashflow_reconciliation_is_separate_and_has_no_income_or_cohort_pages():
    repo = _Repo([
        _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3).to_dict(),
        _event("close", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=1, close_type="buy_to_close").to_dict(),
    ])
    report = build_option_period_performance(repo, period=_period(), config_key="us",
                                             configured_accounts=("lx",), basis="cashflow")
    assert report["period_option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == -100
    assert "terminated_option_net_income" not in report and "option_net_cashflow" not in report
    assert all(not rows for rows in report["breakdowns"].values())


@pytest.mark.parametrize("event_type,side", [("assignment", "short"), ("exercise", "long"), ("expire_close", "short")])
def test_invalid_nonzero_physical_settlement_cash_is_never_proven_income(event_type, side):
    key = _key(side=side)
    result = reduce_option_performance(project_trade_events([
        _event("open", "open", "2026-08-20T10:00:00", key=key, lot_id="lot"),
        _event("end", event_type, "2026-09-02T10:00:00", key=key, target_lot_id="lot", price=100),
    ]), period=_period(), basis="terminated")
    assert _cash(result)["amount"] is None
    assert "physical_settlement_option_cash_invalid" in _cash(result)["missing"]
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] is None


def test_partial_close_cny_matches_original_event_conversion_exactly():
    opened = _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", contracts=2,
                    price=.003333335, fx_rate=7)
    events = [opened, *[_event(f"close-{n}", "close", f"2026-09-0{n + 1}T10:00:00", target_lot_id="lot",
                              price=0, close_type="buy_to_close") for n in range(2)]]
    result = reduce_option_performance(project_trade_events(events), period=_period(), basis="terminated")
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] == Decimal("4.666669")


@pytest.mark.parametrize("amount,status,missing,expected", [
    (0, "observed", [], "USD 合计 0.00"),
    (698, "observed", [], "USD 合计 698.00"),
    (None, "partial", ["actual_fee_missing"], "实际手续费尚未确认"),
    (None, "partial", ["terminal_evidence_missing"], "证据不完整"),
])
def test_default_renderer_shows_only_terminal_income(amount, status, missing, expected):
    from src.application.bot.control.renderer import render_canonical_tool_result
    from test_option_performance_agent_tool import _report

    report = _report()
    report["terminated_option_net_income"] = {
        "by_currency": {"USD": {"amount": amount, "status": status, "missing": missing}},
        "cny_total": {"amount": None, "status": "partial", "missing": ["cash_conversion_missing"]},
    }
    report["quality"].update(status="partial", missing=[*missing, "cash_conversion_missing"])
    text = render_canonical_tool_result(renderer_key="option_performance", tool_result={"ok": True}, data=report)
    assert "本月已终结期权净收益" in text and expected in text
    assert "折合 CNY 合计 -（证据不完整）" in text
    assert "期间期权净现金流" not in text and "开仓批次" not in text and "持有至到期估算" not in text
    assert "不含股票交割" in text


def test_income_command_reconciliation_and_opening_analysis_are_explicit():
    from datetime import date
    from src.application.bot.control.command_parser import parse_bot_control_command

    def parse(text):
        return parse_bot_control_command(text, now_fn=lambda: date(2026, 9, 2)).arguments
    assert parse("/income") == {"period": "mtd"}
    assert parse("/income lx 对账") == {"account": "lx", "period": "mtd", "basis": "cashflow"}
    assert parse("/income sy 2026-08 开仓批次") == {
        "account": "sy", "period": "month", "month": "2026-08", "basis": "opening_cohort",
    }


@pytest.mark.parametrize("bad", [{"view": "rows"}, {"include_rows": True}, {"group_by": "symbols"}, {"symbol": "NVDA"}])
def test_reconciliation_rejects_unimplemented_pages_before_ledger_read(bad):
    from src.application.performance.query import normalize_option_performance_request
    from src.application.performance.query_contracts import PerformanceQueryError
    from domain.domain.option_position_identity import normalize_broker

    with pytest.raises(PerformanceQueryError, match="summary only"):
        normalize_option_performance_request({"basis": "cashflow", **bad}, normalize_broker=normalize_broker)


@pytest.mark.parametrize("symbol,currency,multiplier,fx", [
    ("TEST", "USD", 100, 7), ("1234.HK", "HKD", 500, .9),
])
@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("side", ["short", "long"])
def test_pending_reason_preserves_proven_economics_across_markets(symbol, currency, multiplier, fx, option_type, side):
    key = _key(symbol=symbol, option_type=option_type, side=side)
    opened = _event("open", "open", "2026-08-20T10:00:00", key=key, lot_id="lot",
                    price=3, fee=2, currency=currency, multiplier=multiplier, fx_rate=fx)
    results = {}
    for kind, event_type in (("cause_pending", "close"), ("expired", "expire_close"),
                             ("physical", "assignment" if side == "short" else "exercise")):
        terminal = _event("end", event_type, "2026-09-02T10:00:00", key=key, target_lot_id="lot",
                          price=0, fee=1, close_type=kind if event_type == "close" else None,
                          currency=currency, multiplier=multiplier, fx_rate=fx)
        results[kind] = reduce_option_performance(project_trade_events([opened, terminal]),
                                                 period=_period(), basis="terminated")
    pending = results["cause_pending"]
    assert _cash(pending, currency) == {"amount": Decimal((3 if side == "short" else -3) * multiplier - 3), "status": "observed", "missing": ()}
    for result in results.values():
        assert _cash(result, currency) == _cash(pending, currency)
        assert result.bundle["option_return"] == pending.bundle["option_return"]
        assert result.bundle["option_net_cashflow"]["cny_total"] == pending.bundle["option_net_cashflow"]["cny_total"]
    assert pending.bundle["option_return"]["by_currency"][currency]["rate"] is not None
    assert pending.bundle["option_return"]["by_currency"][currency]["annualized_rate"] is not None
    assert pending.bundle["status"] == pending.facts[0].status == "partial"
    assert pending.facts[0].missing == pending.facts[0].win_missing == ("close_reason_pending",)
    win_key = "sell_option_win_rate" if side == "short" else "buy_option_win_rate"
    assert pending.bundle[win_key]["rate"] is None
    assert results["expired"].bundle[win_key]["rate"] == (1 if side == "short" else 0)
    assert results["physical"].bundle[win_key]["rate"] == (0 if side == "short" else None)


@pytest.mark.parametrize("fee_basis", [None, "estimated"])
@pytest.mark.parametrize("event_index", [0, 1])
def test_pending_reason_does_not_bypass_actual_fees(fee_basis, event_index):
    events = [
        _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3),
        _event("end", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=0, close_type="cause_pending"),
    ]
    raw = dict(events[event_index].raw_payload)
    raw.pop("fee_provenance")
    if fee_basis:
        raw["fee_provenance"] = {"basis": fee_basis, "amount": 0, "source": "test"}
    events[event_index] = replace(events[event_index], raw_payload=raw)
    result = reduce_option_performance(project_trade_events(events), period=_period(), basis="terminated")
    assert _cash(result)["amount"] is None
    assert any("fee" in reason for reason in _cash(result)["missing"])
    assert result.bundle["option_return"]["by_currency"]["USD"]["rate"] is None


def test_pending_partial_allocation_retains_only_ended_shares_and_independent_currency_gaps():
    key = _key(symbol="1234.HK", expiration="2026-08-31")
    events = [
        _event("hk-open", "open", "2026-08-20T10:00:00", key=key, lot_id="hk", contracts=2,
               price=3, fee=4, currency="HKD", multiplier=500, fx_rate=.9),
        _event("hk-end", "close", "2026-09-01T10:00:00", key=key, target_lot_id="hk", contracts=1,
               price=0, fee=1, close_type="cause_pending", currency="HKD", multiplier=500, fx_rate=.9),
    ]
    result = reduce_option_performance(project_trade_events(events), period=_period(), basis="terminated")
    assert {fact.state for fact in result.facts} == {"terminated", "unresolved_after_expiry"}
    ended = next(fact for fact in result.facts if fact.state == "terminated")
    assert ended.contracts == 1 and ended.option_net_cashflow == 1497
    assert _cash(result, "HKD")["amount"] is None
    assert "terminal_evidence_missing" in _cash(result, "HKD")["missing"]
    # Fully closed HK income remains visible beside an independently unresolved USD lot.
    events[0] = _event("hk-open", "open", "2026-08-20T10:00:00", key=key, lot_id="hk",
                        price=3, fee=2, currency="HKD", multiplier=500, fx_rate=.9)
    events.append(_event("us-open", "open", "2026-08-20T10:00:00", lot_id="us",
                         key=_key(symbol="TEST", expiration="2026-08-31")))
    result = reduce_option_performance(project_trade_events(events), period=_period(), basis="terminated")
    assert _cash(result, "HKD")["amount"] == 1497
    assert _cash(result)["amount"] is None
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] is None
    assert result.bundle["option_return"]["by_currency"]["HKD"]["rate"] is not None
    assert result.bundle["option_return"]["by_currency"]["USD"]["rate"] is None


def test_pending_native_income_does_not_require_fx_but_cny_does():
    opened = _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3)
    raw = dict(opened.raw_payload)
    raw.pop("cash_conversions")
    opened = replace(opened, raw_payload=raw)
    terminal = _event("end", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=0,
                      close_type="cause_pending")
    result = reduce_option_performance(project_trade_events([opened, terminal]), period=_period(), basis="terminated")
    assert _cash(result)["amount"] == 300
    assert result.bundle["option_return"]["by_currency"]["USD"]["rate"] is not None
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] is None
    assert "cash_conversion_missing" in result.bundle["option_net_cashflow"]["cny_total"]["missing"]


@pytest.mark.parametrize("event_type", ["expire_close", "assignment"])
@pytest.mark.parametrize("fee", [1, 3])
def test_late_reason_and_fee_correction_preserve_terminal_month_and_capital(event_type, fee):
    key = _key(symbol="TEST", expiration="2026-09-30")
    opened = _event("open", "open", "2026-09-20T10:00:00", key=key, lot_id="lot", price=3, fee=2)
    pending = _event("pending", "close", "2026-09-30T16:00:00", key=key, target_lot_id="lot",
                     price=0, fee=1, close_type="cause_pending")
    void = replace(_event("void", "void", "2026-10-02T10:00:00", key=key, contracts=0),
                   target_event_id="pending")
    confirmed = _event("confirmed", event_type, "2026-09-30T16:00:00", key=key,
                       target_lot_id="lot", price=0, fee=fee)
    def calc(events, month):
        period = normalize_performance_period(PeriodRequest(period="month", month=month),
                                               report_now_ms=_ms("2026-10-03T12:00:00"))
        return reduce_option_performance(project_trade_events(events), period=period, basis="terminated")
    before = calc([opened, pending], "2026-09")
    after = calc([opened, pending, void, confirmed], "2026-09")
    assert len(before.facts) == len(after.facts) == 1
    assert _cash(before)["amount"] == 297
    assert _cash(after)["amount"] == 298 - fee
    assert before.facts[0].terminal_at_ms == after.facts[0].terminal_at_ms == pending.event_time_ms
    for field in ("capital_days", "average_occupied_capital"):
        assert before.bundle["option_return"]["by_currency"]["USD"][field] == after.bundle["option_return"]["by_currency"]["USD"][field]
    assert after.bundle["option_net_cashflow"]["cny_total"]["amount"] == Decimal(298 - fee) * 7
    assert not calc([opened, pending, void, confirmed], "2026-10").facts


def test_pending_exception_requires_valid_capital_while_confirmed_cash_stays_independent():
    key = _key(symbol="TEST", side="long")
    opened = _event("open", "open", "2026-08-20T10:00:00", key=key, lot_id="lot", price=0, fee=1)
    pending = _event("end", "close", "2026-09-02T10:00:00", key=key, target_lot_id="lot",
                     price=0, close_type="cause_pending")
    before = reduce_option_performance(project_trade_events([opened, pending]), period=_period(), basis="terminated")
    assert _cash(before)["amount"] is None
    assert "capital_non_positive" in before.facts[0].capital_missing
    expired = _event("end", "expire_close", "2026-09-02T10:00:00", key=key, target_lot_id="lot", price=0)
    after = reduce_option_performance(project_trade_events([opened, expired]), period=_period(), basis="terminated")
    assert _cash(after)["amount"] == -1
    assert after.bundle["option_return"]["by_currency"]["USD"]["rate"] is None


@pytest.mark.parametrize("conflict", ["account", "quantity", "currency"])
def test_pending_exception_does_not_admit_conflicting_terminal_identity(conflict):
    from domain.domain.performance.weighted_reducer import PerformanceScopeError

    opened = _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3)
    terminal = _event("end", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=0,
                      close_type="cause_pending", contracts=2 if conflict == "quantity" else 1,
                      key=_key(account="sy" if conflict == "account" else "lx"),
                      currency="HKD" if conflict == "currency" else "USD")
    try:
        result = reduce_option_performance(project_trade_events([opened, terminal]), period=_period(), basis="terminated")
    except PerformanceScopeError:
        return
    assert _cash(result)["amount"] is None
    assert result.bundle["option_return"]["by_currency"]["USD"]["rate"] is None
