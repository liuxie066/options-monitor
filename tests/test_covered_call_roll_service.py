"""Offline CC roll service: existing inventory coverage, current quotes and reports."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import json

import pytest

from src.application import short_put_roll as service
from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot
from test_short_put_roll_service import NOW, OLD_EXPIRY, NEW_EXPIRY, setup_service  # noqa: F401
from test_short_put_roll_service import quote


def portfolio(*, shares=100, account="sample", currency="USD", age=0):
    ref = {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
        "account_label": account, "broker_account_id": "futu:REAL:123"}
    raw = [] if not shares else [{"code": "US.NVDA", "stock_type": "STOCK", "qty": shares,
        "can_sell_qty": 0, "currency": currency, "position_side": "long"}]
    snapshot = build_futu_position_snapshot(rows=raw, broker_account_ref=ref, markets=["US"],
        asset_types=["stock"], observed_at_utc=(NOW - timedelta(seconds=age)).isoformat(), completeness="complete")
    return build_futu_portfolio_context(balance_rows=[], position_rows=raw, account=account,
        futu_account_id="123", broker_account_identifiers=["123"], trd_env="REAL", capacity_market="us",
        position_snapshot_input=snapshot)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def covered_service(setup_service, monkeypatch):
    monkeypatch.setattr("src.application.futu_portfolio_context.datetime", FrozenDatetime)
    monkeypatch.setattr("src.application.portfolio_context_service.datetime", FrozenDatetime)
    provider, rows, cfg = setup_service
    rows[0]["fields"]["contract_key"]["option_type"] = "call"
    provider.spot = 102
    provider.rows = [call_quote(100, OLD_EXPIRY, bid=4.8, ask=5), call_quote(105, NEW_EXPIRY, bid=6, ask=6.1)]
    stock_context, calls = portfolio(), []
    def load(**kwargs):
        calls.append(kwargs)
        return deepcopy(stock_context)
    monkeypatch.setattr(service, "load_account_portfolio_context", load)
    return provider, rows, cfg, stock_context, calls


def call_quote(strike, expiry, **kwargs):
    row = quote(strike, expiry, **kwargs)
    return {**row, "option_type": "call", "spot": 102,
        "contract_symbol": row["contract_symbol"].replace("P", "C")}


def run(tmp_path, covered_service, payload=None, **kwargs):
    _provider, _rows, cfg, _context, _calls = covered_service
    return service.covered_call_roll_candidates({"account": "sample", "lot_id": "old",
        "max_new_expiration": NEW_EXPIRY, **(payload or {})}, config=cfg,
        config_path=tmp_path / "config.us.json", base_dir=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path, now_utc=kwargs.pop("now_utc", NOW), **kwargs)


def test_credit_roll_found_with_fees_cost_stock_basis_and_saved_report(tmp_path, covered_service):
    provider, _rows, _cfg, _context, calls = covered_service
    result = run(tmp_path, covered_service)
    assert result["schema_version"] == "covered_call_roll.output.v1"
    assert (result["status"], result["outcome"]) == ("success", "found")
    assert result["stock_coverage"]["status"] == "covered"
    assert result["stock_coverage"]["required_shares"] == result["stock_coverage"]["available_shares"] == 100
    assert result["coverage"]["status"] == "complete"
    candidate = result["candidates"][0]
    assert candidate["recommendation"] == "credit_roll"
    assert candidate["cash_flow_type"] == "credit"
    assert candidate["net_credit"] == pytest.approx(
        100 - candidate["old_buy_fee"]["amount"] - candidate["new_sell_fee"]["amount"])
    assert candidate["net_credit"] > 0
    assert candidate["net_debit"] == 0
    assert candidate["strike_increase"] == 5
    assert candidate["assignment_proceeds_uplift"] == 500
    assert candidate["assignment_net_improvement"] == pytest.approx(500 + candidate["net_credit"])
    assert candidate["old_capital_basis"] == candidate["new_capital_basis"] == 10200
    assert candidate["old_buy_fee"]["amount"] > 0 and candidate["new_sell_fee"]["amount"] > 0
    assert candidate["incremental_annualized_return"] == pytest.approx(candidate["net_credit"] / 10200 * 365 / 40)
    assert result["assignment_semantics"] == "wheel_stock_sale"
    assert calls[0]["write_cache"] is False
    assert calls[0]["required_position_asset_types"] == ("stock",)
    assert "now_utc" not in calls[0]  # The loader qualifies direct fetches at response time.
    assert provider.requests[0].option_types == "call"
    replacement = provider.requests[0].query_filters[1]
    assert replacement["min_strike"] == 100 and replacement["min_dte"] == 10
    report = Path(result["report_path"])
    assert "output_requests/covered_call_roll" in str(report)
    assert json.loads(report.read_text()) == result
    assert provider.closed == 1


@pytest.mark.parametrize("contracts", [1, 3])
def test_covered_roll_publishes_one_contract_cash_and_stock_basis(tmp_path, covered_service, monkeypatch, contracts):
    _provider, rows, _cfg, _context, _calls = covered_service
    rows[0]["fields"].update(contracts_open=contracts, contracts_opened=contracts)
    context = portfolio(shares=100 * contracts)
    monkeypatch.setattr(service, "load_account_portfolio_context", lambda **_kwargs: context)

    result = run(tmp_path, covered_service)

    assert result["outcome"] == "found"
    target, = result["candidates"]
    per_contract = target["per_contract"]
    buy_fee = service.estimate_futu_option_buy_fee("USD", 5, contracts=1, multiplier=100)
    sell_fee = service.estimate_futu_option_sell_fee("USD", 6, contracts=1, multiplier=100)
    credit = 100 - buy_fee.amount - sell_fee.amount
    assert target["contracts"] == contracts
    assert per_contract["net_credit"] == pytest.approx(credit)
    assert per_contract["old_buy_fee"] == buy_fee.to_dict()
    assert per_contract["new_sell_fee"] == sell_fee.to_dict()
    assert per_contract["incremental_period_return"] == pytest.approx(credit / 10200)
    assert per_contract["incremental_annualized_return"] == pytest.approx(credit / 10200 * 365 / 40)
    if contracts > 1:
        assert per_contract["net_credit"] != pytest.approx(target["net_credit"] / contracts)


@pytest.mark.parametrize("shares", [0, 99])
def test_no_or_insufficient_stock_is_not_applicable_before_quote_search(tmp_path, covered_service, shares):
    provider, _rows, _cfg, context, _calls = covered_service
    context.update(portfolio(shares=shares))
    result = run(tmp_path, covered_service)
    assert (result["status"], result["outcome"]) == ("success", "not_applicable")
    assert result["reason_codes"] == ["insufficient_stock_coverage"]
    assert result["coverage"] is None and result["candidates"] == []
    assert provider.open_calls == 0 and provider.requests == []


@pytest.mark.parametrize("changes", [{"shares": 100, "currency": "HKD"}, {"shares": 100, "account": "other"}, {"shares": 100, "age": 301}])
def test_wrong_currency_account_or_stale_stock_never_proves_coverage(tmp_path, covered_service, changes):
    provider, _rows, _cfg, context, _calls = covered_service
    context.update(portfolio(**changes))
    result = run(tmp_path, covered_service)
    assert result["outcome"] != "found"
    assert result["coverage"] is None
    assert provider.requests == []
    if changes.get("currency"):
        assert result["outcome"] == "not_applicable"
    else:
        assert result["outcome"] == "unable"


def test_all_calls_share_the_same_stock_pool_before_selecting_a_lot(tmp_path, covered_service):
    _provider, rows, _cfg, context, _calls = covered_service
    other = deepcopy(rows[0])
    other["record_id"] = "other-call"
    other["fields"]["contract_key"]["strike"] = "101"
    rows.append(other)
    first = run(tmp_path, covered_service)
    second = run(tmp_path, covered_service, {"lot_id": "other-call"})
    assert first["outcome"] == second["outcome"] == "not_applicable"
    assert first["stock_coverage"]["required_shares"] == second["stock_coverage"]["required_shares"] == 200
    context.update(portfolio(shares=200))
    assert run(tmp_path, covered_service)["outcome"] == "found"


def test_call_at_another_broker_does_not_consume_this_accounts_stock(tmp_path, covered_service):
    _provider, rows, _cfg, _context, _calls = covered_service
    other = deepcopy(rows[0])
    other["record_id"] = "another-broker"
    other["fields"]["contract_key"]["broker"] = "长桥"
    rows.append(other)
    result = run(tmp_path, covered_service)
    assert result["outcome"] == "found"
    assert result["stock_coverage"]["required_shares"] == 100


@pytest.mark.parametrize("strike,expiry", [(95, NEW_EXPIRY), (100, NEW_EXPIRY), (105, "2026-11-13")])
def test_wrong_direction_equal_strike_and_earlier_expiry_not_candidates(tmp_path, covered_service, strike, expiry):
    provider, *_ = covered_service
    provider.rows = [provider.rows[0], call_quote(strike, expiry, bid=10, ask=10.1)]
    result = run(tmp_path, covered_service)
    assert (result["status"], result["outcome"]) == ("success", "none")
    assert result["coverage"]["status"] == "complete" and result["candidates"] == []


def test_same_expiry_with_higher_strike_and_credit_is_found(tmp_path, covered_service):
    provider, *_ = covered_service
    provider.rows[1] = call_quote(105, OLD_EXPIRY, bid=6, ask=6.1)
    result = run(tmp_path, covered_service, {"max_new_expiration": OLD_EXPIRY})
    assert (result["status"], result["outcome"]) == ("success", "found")
    candidate = result["candidates"][0]
    assert candidate["recommendation"] == "credit_roll"
    assert candidate["expiration"] == OLD_EXPIRY
    assert candidate["old_dte"] == candidate["new_dte"] == 10
    assert candidate["extension_days"] == 0
    assert candidate["assignment_improvement_annualized_return"] == pytest.approx(
        candidate["assignment_net_improvement"] / 10200 * 365 / 10)


@pytest.mark.parametrize("strike,bid", [(105, 4), (101, 1)])
def test_net_debit_is_known_economic_exclusion_regardless_of_uplift(tmp_path, covered_service, strike, bid):
    provider, *_ = covered_service
    provider.rows[1] = call_quote(strike, NEW_EXPIRY, bid=bid, ask=bid + 0.1)
    result = run(tmp_path, covered_service)
    assert (result["status"], result["outcome"]) == ("success", "none")
    assert result["coverage"]["status"] == "complete"
    assert result["candidates"] == []
    assert {"code": "non_positive_net_credit", "count": 1} in result["exclusion_summary"]


@pytest.mark.parametrize("fee_extra,expected", [(-0.01, "credit_roll"), (0, None), (0.01, None)])
def test_service_applies_both_leg_fees_at_neutral_and_debit_boundary(
    tmp_path, covered_service, monkeypatch, fee_extra, expected,
):
    provider, *_ = covered_service
    provider.rows[1] = call_quote(105, NEW_EXPIRY, bid=5.05, ask=5.15)
    buy_estimate, sell_estimate = service.estimate_futu_option_buy_fee, service.estimate_futu_option_sell_fee
    monkeypatch.setattr(service, "estimate_futu_option_buy_fee",
        lambda *args, **kwargs: replace(buy_estimate(*args, **kwargs), amount=2))
    monkeypatch.setattr(service, "estimate_futu_option_sell_fee",
        lambda *args, **kwargs: replace(sell_estimate(*args, **kwargs), amount=3 + fee_extra))
    result = run(tmp_path, covered_service)
    assert result["status"] == "success" and result["coverage"]["status"] == "complete"
    if expected:
        candidate = result["candidates"][0]
        assert result["outcome"] == "found"
        assert candidate["recommendation"] == expected
        assert candidate["cash_flow_type"] == "credit"
        assert candidate["net_credit"] == pytest.approx(0.01) and candidate["net_debit"] == 0
        assert candidate["old_buy_fee"]["amount"] == 2 and candidate["new_sell_fee"]["amount"] == pytest.approx(2.99)
        assert candidate["old_buy_fee"]["fee_basis"] and candidate["new_sell_fee"]["fee_basis"]
    else:
        assert result["outcome"] == "none" and result["candidates"] == []
        assert {"code": "non_positive_net_credit", "count": 1} in result["exclusion_summary"]


def test_active_query_ranks_cash_return_above_larger_conditional_uplift(tmp_path, covered_service):
    provider, *_ = covered_service
    provider.rows = [provider.rows[0], call_quote(110, NEW_EXPIRY, bid=5.1, ask=5.2),
        call_quote(105, NEW_EXPIRY, bid=6, ask=6.1)]
    result = run(tmp_path, covered_service)
    assert (result["status"], result["outcome"]) == ("success", "found")
    first, second = result["candidates"]
    assert first["strike"] == 105 and second["strike"] == 110
    assert first["incremental_annualized_return"] > second["incremental_annualized_return"] > 0
    assert second["assignment_improvement_annualized_return"] > first["assignment_improvement_annualized_return"]


def test_partial_query_keeps_a_valid_credit_and_missing_scope(tmp_path, covered_service):
    provider, *_ = covered_service
    provider.rows.append(call_quote(110, NEW_EXPIRY, bid=None, ask=2))
    result = run(tmp_path, covered_service)
    assert (result["status"], result["outcome"]) == ("partial", "found")
    assert result["candidates"][0]["recommendation"] == "credit_roll"
    assert result["candidates"][0]["net_credit"] > 0
    assert result["coverage"]["unassessed"] == 1


@pytest.mark.parametrize("status", ["disabled", "unavailable"])
def test_plugin_boundary_precedes_stock_and_quote_reads(tmp_path, covered_service, monkeypatch, status):
    provider, _rows, _cfg, _context, calls = covered_service
    monkeypatch.setattr(service, "position_advice_plugin_status", lambda _: status)
    result = run(tmp_path, covered_service)
    assert result["status"] == status and result["outcome"] == "unable"
    assert calls == [] and provider.requests == []


def test_stock_snapshot_is_requalified_after_the_option_fetch(tmp_path, covered_service, monkeypatch):
    provider, _rows, _cfg, context, _calls = covered_service
    context.update(portfolio(age=299))
    class MovingDatetime(datetime):
        value = NOW
        @classmethod
        def now(cls, tz=None):
            return cls.value
    def advance():
        MovingDatetime.value = NOW + timedelta(seconds=2)
    provider.before_fetch = advance
    monkeypatch.setattr(service, "datetime", MovingDatetime)
    result = run(tmp_path, covered_service, now_utc=None)
    assert result["outcome"] == "unable"
    assert result["stock_coverage"]["status"] == "unavailable"
    assert "snapshot_observed_at_utc_stale_or_future" in result["reason_codes"]
    assert result["candidates"] == [] and result["coverage"] is None


def test_other_open_call_missing_multiplier_does_not_become_a_smaller_pool(tmp_path, covered_service):
    provider, rows, *_ = covered_service
    other = deepcopy(rows[0])
    other["record_id"] = "unknown-unit"
    other["fields"]["multiplier"] = None
    rows.append(other)
    result = run(tmp_path, covered_service)
    assert result["outcome"] == "unable"
    assert result["stock_coverage"]["status"] == "unavailable"
    assert provider.requests == []


def test_optional_maximum_strike_and_outside_old_scope(tmp_path, covered_service):
    provider, rows, *_ = covered_service
    assert run(tmp_path, covered_service, {"max_new_strike": 104})["outcome"] == "none"
    with pytest.raises(ValueError, match="above the old strike"):
        run(tmp_path, covered_service, {"max_new_strike": 100})
    rows[0]["fields"]["contract_key"]["strike"] = "110"
    provider.requests.clear()
    result = run(tmp_path, covered_service)
    assert result["outcome"] == "not_applicable"
    assert result["reason_codes"] == ["old_position_outside_roll_scope"]
    assert provider.requests == []
