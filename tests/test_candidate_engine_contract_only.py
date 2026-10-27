from __future__ import annotations

import json

import pytest

from domain.domain.engine import (
    calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy,
    rank_candidate_rows,
)
from test_candidate_engine_contract import _policy_row
from test_candidate_engine_phase2_contract import _opening_row


def _query(row: dict, *, mode: str = "put", **kwargs) -> dict:
    return evaluate_opening_candidate_policy(
        row,
        mode=mode,
        check_capacity=False,
        require_earnings_evidence=False,
        reject_known_earnings=False,
        **kwargs,
    )


def _conditions(decision: dict) -> dict:
    return {item["name"]: item for item in decision["conditions"]}


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("capacity", [None, 0, 1])
def test_capacity_is_opt_out_and_does_not_change_other_gates(mode, capacity):
    row = _policy_row(
        mode=mode,
        max_new_contracts=capacity,
        covered_contracts_available=capacity,
        spread_ratio=0.41,
    )
    legacy = evaluate_opening_candidate_policy(row, mode=mode)
    assert legacy == evaluate_opening_candidate_policy(row, mode=mode, check_capacity=True)
    assert "conditions" not in legacy
    assert (f"hard_capacity_{mode}" in {r["reason"] for r in legacy["rejects"]}) == (capacity != 1)
    query = _query(row, mode=mode)
    conditions = query.pop("conditions")
    legacy["rejects"] = [r for r in legacy["rejects"] if not r["reason"].startswith("hard_capacity_")]
    assert query == legacy  # Spread still fails; every other decision field is unchanged.
    assert {item["name"] for item in conditions}.isdisjoint({"capacity", "earnings", "concentration"})


@pytest.mark.parametrize("mode", ["put", "call"])
def test_accepted_query_exposes_effective_numeric_gates_without_account_evidence(mode):
    row = _policy_row(mode=mode)
    row.pop("max_new_contracts")
    row.pop("covered_contracts_available")
    row["earnings_evidence_status"] = "unavailable"
    decision = _query(row, mode=mode, min_dte=7, max_dte=60)
    assert decision["accepted"] is True
    conditions = _conditions(decision)
    assert len(conditions) == 7
    assert all(item["status"] == "pass" and item["reason_code"] is None for item in conditions.values())
    expected = {
        "dte": (43, "between", [7, 60]),
        "strike": (row["strike"], "between", [88, 110] if mode == "put" else [110, 132]),
        "annualized_net_return": (0.12, "gte", 0.10),
        "net_premium_cny": (700, "gte", 50),
        "spread_ratio": (0.08, "lte", 0.40),
        "iv_rv_ratio": (1.50, "gte", 1.10),
        "iv_minus_rv": (0.12, "gte", 0.05),
    }
    for name, (actual, operator, value) in expected.items():
        assert conditions[name]["actual"] == actual
        assert conditions[name]["required"] == {"operator": operator, "value": value}
    json.dumps(decision, allow_nan=False)


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize(
    "field,name,threshold,failing,reason",
    [
        ("dte", "dte", 7, 6, "hard_dte"),
        ("annualized", "annualized_net_return", 0.10, 0.0999, "return_annualized"),
        ("net_income_cny", "net_premium_cny", 50, 49.99, "return_net_premium_cny"),
        ("spread_ratio", "spread_ratio", 0.40, 0.4001, "risk_spread"),
        ("iv_rv_ratio", "iv_rv_ratio", 1.10, 1.0999, "risk_iv_rv_ratio"),
        ("iv_minus_rv", "iv_minus_rv", 0.05, 0.0499, "risk_iv_minus_rv"),
    ],
)
def test_conditions_follow_inclusive_gates_and_distinguish_missing(mode, field, name, threshold, failing, reason):
    if field == "annualized":
        field = "annualized_net_return_on_cash_basis" if mode == "put" else "annualized_net_premium_return"
    for value, status in [(threshold, "pass"), (failing, "fail"), (None, "unknown"), (float("nan"), "unknown")]:
        decision = _query(_policy_row(mode=mode, **{field: value}), mode=mode, min_dte=7, max_dte=60)
        condition = _conditions(decision)[name]
        assert condition["status"] == status
        assert decision["accepted"] is (status == "pass")
        if status == "pass":
            assert condition["reason_code"] is None
        elif status == "unknown" and name == "net_premium_cny":
            assert condition["reason_code"] == "net_premium_cny_unavailable"
            assert decision["rejects"][0]["reason"] == "input_missing"
        else:
            assert condition["reason_code"] == reason
            assert reason in {r["reason"] for r in decision["rejects"]}
        assert condition["actual"] == (None if status == "unknown" else value)
        json.dumps(decision["conditions"], allow_nan=False)


@pytest.mark.parametrize("mode,strike,bounds", [("put", 120, [120, 110]), ("call", 100, [110, 100])])
def test_exact_strike_outside_spot_rules_remains_rejected_with_actual_bounds(mode, strike, bounds):
    decision = _query(_policy_row(mode=mode, strike=strike), mode=mode, min_strike=strike, max_strike=strike)
    assert decision["accepted"] is False
    assert _conditions(decision)["strike"] == {
        "name": "strike", "status": "fail", "actual": strike,
        "required": {"operator": "between", "value": bounds}, "reason_code": "hard_strike",
    }


def test_conditions_resolve_row_policy_explicit_overrides_and_one_sided_bounds():
    row = _policy_row(
        mode="call", policy_min_dte=8, policy_max_dte=50,
        policy_min_annualized_return=0.11, policy_max_spread_ratio=0.2,
    )
    decision = _query(row, mode="call", min_dte=14, min_annualized_return=0.12, apply_default_call_strike_cap=False)
    conditions = _conditions(decision)
    assert decision["accepted"] is True
    assert conditions["dte"]["required"] == {"operator": "between", "value": [14, 50]}
    assert conditions["annualized_net_return"]["required"] == {"operator": "gte", "value": 0.12}
    assert conditions["spread_ratio"]["required"] == {"operator": "lte", "value": 0.2}
    assert conditions["strike"]["required"] == {"operator": "gte", "value": 110}
    one_sided = _conditions(_query(_policy_row(), max_dte=60))
    assert one_sided["dte"]["required"] == {"operator": "lte", "value": 60}
    assert "dte" not in _conditions(_query(_policy_row()))


def test_capacity_switch_does_not_disable_earnings_gates():
    row = _policy_row(earnings_evidence_status="unavailable")
    decision = evaluate_opening_candidate_policy(row, mode="put", check_capacity=False)
    assert decision["accepted"] is False
    assert decision["rejects"][0]["reason"] == "risk_earnings_unavailable"
    assert all(item["status"] == "pass" for item in decision["conditions"])


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("currency,multiplier", [("USD", 100), ("HKD", 500)])
def test_query_uses_existing_obligation_metrics_returns_and_ranking(mode, currency, multiplier):
    row = _opening_row(
        mode=mode, currency=currency, strike=100 if mode == "put" else 120,
        bid=3, ask=3.10, multiplier=multiplier, chain_multiplier=multiplier,
        snapshot_multiplier=multiplier, max_new_contracts=0, covered_contracts_available=0,
    )
    metrics = calculate_opening_candidate_metrics(row, mode=mode, cny_per_currency_unit=7.2 if currency == "USD" else 0.9)
    assert "obligation" not in metrics
    net_premium = metrics["net_income"]
    if mode == "put":
        assert metrics["breakeven"] == pytest.approx(row["strike"] - net_premium / multiplier)
        denominator = row["strike"] * multiplier - net_premium
    else:
        assert metrics["strike_above_spot_pct"] == pytest.approx((row["strike"] - row["spot"]) / row["spot"])
        denominator = row["spot"] * multiplier
    assert metrics["period_net_return"] == pytest.approx(net_premium / denominator)
    candidate = {**row, **metrics}
    decision = _query(candidate, mode=mode, min_dte=7, max_dte=60)
    assert decision["accepted"] is True
    lower = {**candidate, "contract_symbol": "LOWER", "period_net_return_on_cash_basis": 0.0001, "period_net_premium_return": 0.0001}
    assert rank_candidate_rows([lower, candidate], mode=mode)[0] == candidate
