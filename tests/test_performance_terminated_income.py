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


def test_missing_cny_does_not_hide_proven_native_income_and_pending_close_is_not_earned():
    opened = _event("open", "open", "2026-08-20T10:00:00", lot_id="lot", price=3)
    raw = dict(opened.raw_payload)
    raw.pop("cash_conversions")
    opened = replace(opened, raw_payload=raw)
    closed = _event("close", "close", "2026-09-02T10:00:00", target_lot_id="lot", price=1, close_type="buy_to_close")
    result = reduce_option_performance(project_trade_events([opened, closed]), period=_period(), basis="terminated")
    assert _cash(result)["amount"] == 200
    assert result.bundle["option_net_cashflow"]["cny_total"]["amount"] is None
    pending = replace(closed, raw_payload={**closed.raw_payload, "close_type": "cause_pending"})
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
