from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import json
from pathlib import Path

import pytest

from om_strategy_core import (
    __version__, CallCapacityInputs, CurrencyConverter, ExchangeRates, InsuranceUnderwritingConfig,
    PutCapacityInputs, ShortVolPortfolioContext, calculate_constraints, calculate_metrics,
    evaluate_candidates, resolve_strategy, runtime_identity,
)
from domain.domain.engine.candidate_engine import CandidateCalculationError
from domain.domain.currency_conversion import enrich_short_vol_contract_cny_fields

BASELINE = json.loads((Path(__file__).parent / "fixtures/strategy_core_baseline.json").read_text())
VALID = [case for case in BASELINE["cases"] if "metrics" in case]
RATES = ExchangeRates(0.14, 0.920000046)
CONVERTER = CurrencyConverter(RATES)
UNKNOWN = ShortVolPortfolioContext(None, {}, {}, None)


@pytest.mark.parametrize("case", BASELINE["cases"], ids=lambda case: case["name"])
def test_independent_old_source_baseline(case):
    before = deepcopy(case)
    kwargs = dict(mode=case["mode"], avg_cost=case["avg_cost"], now_utc=datetime.fromisoformat(case["now_utc"]),
                  exchange_rates=ExchangeRates(**case["rates"]))
    if "error" in case:
        with pytest.raises(CandidateCalculationError) as exc:
            calculate_metrics(case["row"], **kwargs)
        assert exc.value.to_payload() == case["error"]
    else:
        assert calculate_metrics(case["row"], **kwargs) == {**case["metrics"], **case["cny_fields"]}
        strategy = resolve_strategy(mode=case["mode"], config=InsuranceUnderwritingConfig())
        result = evaluate_candidates([case["policy_row"]], strategy=strategy)
        assert result["decisions"] == [case["decision"]]
        assert len(result["accepted"]) == int(case["decision"]["accepted"])
    assert case == before


@pytest.mark.parametrize("case", BASELINE["fx"])
def test_exact_fx_arithmetic_and_compatibility(case):
    from src.infrastructure.exchange_rates import CurrencyConverter as OldConverter, ExchangeRates as OldRates
    assert OldConverter is CurrencyConverter
    assert OldRates is ExchangeRates
    assert CONVERTER.convert(case["amount"], from_ccy=case["from"], to_ccy=case["to"]).hex() == float(case["result"]).hex()


@pytest.mark.parametrize("value", [None, float("nan"), 0.0, 123.4567890123])
def test_supplement_keeps_independent_missing_rules(value):
    row = {**VALID[2]["policy_row"], "net_income_cny": value, "option_contract_point_value_cny": value,
           "covered_notional_cny": value, "avg_cost": 100.0}
    fields = enrich_short_vol_contract_cny_fields(row, exchange_rate_converter=CONVERTER)
    missing = value is None or value != value
    assert fields == (VALID[2]["cny_fields"] if missing else {})
    result = calculate_constraints(row, mode="call", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER)
    # setdefault must not fill present None/NaN, unlike the income/point fields.
    assert result["covered_notional_cny"] == (None if missing else value)
    assert row["covered_notional_cny"] is value
    assert result["effective_min_strike"] == 110


def test_cc_cost_and_spot_floor_and_missing_fx():
    row = {**VALID[2]["row"], "avg_cost": 120}
    result = calculate_constraints(row, mode="call", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER)
    assert result["effective_min_strike"] == 120 * 1.02
    assert result["covered_notional_cny"] == 110 * 100 / 0.14
    result = calculate_constraints(row, mode="call", risk_ctx=UNKNOWN, exchange_rate_converter=CurrencyConverter(ExchangeRates()))
    assert result["covered_notional_cny"] is None
    assert not result["concentration_evaluable"]
    c = VALID[0]
    assert calculate_metrics(c["row"], mode="put", now_utc=datetime.fromisoformat(c["now_utc"])) == c["metrics"]


@pytest.mark.parametrize("now", [None, "2026-08-06T15:00:00Z", datetime(2026, 8, 6)])
def test_clock_is_explicit_and_aware(now):
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_metrics(VALID[0]["row"], mode="put", now_utc=now)


@pytest.mark.parametrize("cost", [None, 0, -1, float("nan"), True])
def test_cc_requires_resolved_cost(cost):
    with pytest.raises(ValueError, match="avg_cost"):
        calculate_metrics(VALID[2]["row"], mode="call", avg_cost=cost, now_utc=datetime.fromisoformat(VALID[2]["now_utc"]))


@pytest.mark.parametrize("changes", [
    {"min_net_income": float("nan")}, {"min_iv_rv_ratio": float("inf")}, {"strategy": "legacy"},
    {"min_dte": True}, {"min_dte": 1.5}, {"min_dte": 60, "max_dte": 7}, {"max_spread_ratio": -1},
    {"min_strike": 100, "max_strike": 90}, {"min_annualized_return": 2}, {"min_net_income": None},
])
def test_invalid_policy_fails_before_hashing(changes):
    with pytest.raises(ValueError):
        resolve_strategy(mode="put", config=replace(InsuranceUnderwritingConfig(), **changes))


def test_policy_identity_and_immutability():
    strategy = resolve_strategy(mode="put", config=InsuranceUnderwritingConfig(min_dte=7, max_dte=60), account="lx", market="us")
    assert strategy.identity["policy_kind"] == "baseline"
    assert strategy.identity["template_version"] == __version__
    assert strategy.identity["scope"] == {"account": "lx", "market": "us", "mode": "put"}
    with pytest.raises(FrozenInstanceError):
        strategy.config.min_net_income = 0
    with pytest.raises(FrozenInstanceError):
        strategy.account = "sy"
    other = replace(strategy, config=replace(strategy.config, min_net_income=51))
    assert other.identity["policy_kind"] == "custom"
    assert other.identity["effective_policy_hash"] != strategy.identity["effective_policy_hash"]
    with pytest.raises(ValueError):
        replace(strategy, mode="call", ranking_profile="concentration_first")
    with pytest.raises(ValueError):
        replace(strategy, ranking_profile="without_concentration")
    with pytest.raises(ValueError, match="account"):
        evaluate_candidates([{**VALID[0]["policy_row"], "account": "sy"}], strategy=strategy)
    assert runtime_identity() == {"mode": "source", "core_version": __version__, "build_digest": None}


def test_complete_passes_and_representatives_and_experimental_order():
    base = VALID[0]["policy_row"]
    rows = [
        {**base, "symbol": symbol, "contract_symbol": f"{symbol}-{n}", "symbol_concentration_after": concentration,
         "period_net_return_on_cash_basis": ret, "net_assignment_discount_pct": discount}
        for symbol, concentration, ret in (("NVDA", 0.8, 0.03), ("AMD", 0.1, 0.02))
        for n, discount in ((1, 0.10), (2, 0.09))
    ]
    rows.append({**base, "contract_symbol": "rejected", "net_income_cny": 0})
    original = deepcopy(rows)
    strategy = resolve_strategy(mode="put", config=InsuranceUnderwritingConfig())
    default = evaluate_candidates(rows, strategy=strategy)
    experiment = evaluate_candidates(rows, strategy=replace(strategy, ranking_profile="concentration_first"))
    assert len(default["decisions"]) == 5
    assert len(default["accepted"]) == 4
    assert [r["contract_symbol"] for r in default["representatives"]] == ["NVDA-1", "AMD-1"]
    assert [r["contract_symbol"] for r in experiment["representatives"]] == ["AMD-1", "NVDA-1"]
    assert rows == original
    assert evaluate_candidates(rows, strategy=strategy) == default


@pytest.mark.parametrize("mode", ["put", "call"])
def test_missing_earnings_stays_a_decision(mode):
    row = deepcopy(next(c["policy_row"] for c in VALID if c["mode"] == mode))
    row["earnings_evidence_status"] = "unavailable"
    result = evaluate_candidates([row], strategy=resolve_strategy(mode=mode, config=InsuranceUnderwritingConfig()))
    assert not result["accepted"]
    assert not result["decisions"][0]["accepted"]


def test_real_capacity_owners_and_unavailable_facts():
    row = VALID[0]["row"]
    facts = PutCapacityInputs((("USD", 12000), ("CNY", 10000)), (("USD", 1000),))
    result = calculate_constraints(row, mode="put", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER, capacity_inputs=facts)
    assert result["put_cash_free"] == 11000 + 10000 * 0.14
    assert result["max_new_contracts"] == 1
    blocked = calculate_constraints(row, mode="put", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER,
                                    capacity_inputs=replace(facts, unavailable_reason="collateral_unavailable"))
    assert blocked["put_cash_free"] is None
    assert blocked["max_new_contracts"] == 0
    missing = calculate_constraints(row, mode="put", risk_ctx=UNKNOWN, exchange_rate_converter=CurrencyConverter(ExchangeRates()),
                                    capacity_inputs=PutCapacityInputs((("HKD", 100000),), fx_status="stale"))
    assert not missing["put_cash_capacity_available"]
    assert "fx_stale" in missing["put_cash_capacity_reason"]
    call = calculate_constraints(VALID[2]["row"], mode="call", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER,
                                 capacity_inputs=CallCapacityInputs(200, 200, 100))
    assert call["covered_contracts_available"] == 1
    assert call["shares_available_for_cover"] == 100


def test_concentration_delegation_with_frozen_portfolio_and_no_mutation():
    from test_portfolio_concentration_fields import candidate, context
    ctx = context()
    before = deepcopy(ctx)
    result = calculate_constraints(candidate(), mode="put", risk_ctx=ctx, exchange_rate_converter=CONVERTER)
    assert result["symbol_concentration_current"] == 0.1
    assert result["symbol_concentration_after"] == round(2000 / 10250, 6)
    assert ctx == before


def test_capacity_preserves_scan_floor_division_at_float_boundary():
    result = calculate_constraints(
        {"strike": 0.001, "multiplier": 100, "currency": "USD", "symbol": "NVDA"},
        mode="put", risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER,
        capacity_inputs=PutCapacityInputs((("USD", 1.0),)),
    )
    assert result["max_new_contracts"] == 9  # frozen old scan: int(1.0 // 0.1)


@pytest.mark.parametrize("update,remove,reason", [
    ({"currency": "EUR"}, None, "option_fee_estimate_unavailable"),
    ({}, "snapshot_received_at_utc", "evidence_unavailable"),
])
def test_metrics_reject_missing_fees_or_evidence(update, remove, reason):
    row = {**VALID[0]["row"], **update}
    if remove:
        row.pop(remove)
    with pytest.raises(CandidateCalculationError) as exc:
        calculate_metrics(row, mode="put", now_utc=datetime.fromisoformat(VALID[0]["now_utc"]))
    assert exc.value.to_payload()["rule"] == reason

def test_zero_dte_and_per_symbol_willingness_are_compatible():
    config = InsuranceUnderwritingConfig(min_dte=0, max_dte=60)
    assert resolve_strategy(mode="put", config=config).config.min_dte == 0
    baseline = resolve_strategy(mode="put", config=replace(config, min_dte=7))
    bounded = replace(baseline, config=replace(baseline.config, max_strike=100))
    assert bounded.identity["policy_kind"] == "baseline"
    assert bounded.identity["effective_policy_hash"] == baseline.identity["effective_policy_hash"]
    assert bounded.identity["strike_inputs"] == {"min_strike": None, "max_strike": 100}
    experiment = replace(baseline, ranking_profile="concentration_first")
    assert experiment.identity["policy_kind"] == "custom"
    assert experiment.identity["effective_policy_hash"] != baseline.identity["effective_policy_hash"]
