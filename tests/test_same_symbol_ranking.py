"""Ordinary price tiers use frozen group evidence, retaining the complete old order."""
from copy import deepcopy
from dataclasses import replace

import pytest

from domain.domain.engine.candidate_engine import rank_candidate_rows
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from domain.domain.strategy_core import explain_rank, explain_ranked_candidates, rank_candidates, resolve_strategy
from domain.domain.strategy_identity import implementation_policy_hash, validate_strategy_identity


def _rows(mode, *, market="us", prices=None):
    symbol, currency = ("NVDA", "USD") if market == "us" else ("0700.HK", "HKD")
    strategy = resolve_strategy(mode=mode, account="lx", market=market, config=InsuranceUnderwritingConfig())
    context = {"status": "available", "reason": None, "account": "lx", "market": market,
               "symbol": symbol, "mode": mode, "currency": currency, "prices": [
                   {"lot_id": str(i), "net_price": price} for i, price in enumerate(prices or ([100] if mode == "put" else [110]))]}
    values = [98, 97, 99] if mode == "put" else [112.2, 114.4, 112]
    rows = []
    for name, value, income in zip("ABC", values, [.024, .021, .04]):
        rows.append({"symbol": symbol, "currency": currency, "option_type": mode,
                     "contract_symbol": name, "strike": 100 if mode == "put" else 110,
                     "breakeven" if mode == "put" else "net_sale_price": value,
                     "period_net_return": income, "strategy_core": strategy.identity,
                     "same_symbol_ranking_context": context})
    return rows


def _ids(rows):
    return [row["contract_symbol"] for row in rows]


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("market", ["us", "hk"])
def test_tiers_preserve_original_order_not_improvement_magnitude(mode, market):
    rows = _rows(mode, market=market)
    before = deepcopy(rows)
    assert _ids(rank_candidates(rows, mode=mode)) == ["A", "B", "C"]
    assert rows == before
    explanations = explain_ranked_candidates(rows, mode=mode)
    assert [explanations[key]["net_delivery_tier"]["priority"] for key in "ABC"] == [True, True, False]
    assert explain_rank(rows[0], mode=mode)["net_delivery_tier"]["priority"] is None


@pytest.mark.parametrize("mode,edge,outside,far", [("put", 98, 98.000001, 80), ("call", 112.2, 112.199999, 130)])
def test_inclusive_threshold_no_upper_bound_and_single_strike_distinct_prices(mode, edge, outside, far):
    rows = _rows(mode)
    field = "breakeven" if mode == "put" else "net_sale_price"
    for row, price in zip(rows, [edge, outside, far]):
        row[field] = price
    result = explain_ranked_candidates(rows, mode=mode)
    assert [result[k]["net_delivery_tier"]["qualifies"] for k in "ABC"] == [True, False, True]
    assert _ids(rank_candidates(rows, mode=mode)) == ["C", "A", "B"]


@pytest.mark.parametrize("mode,prices,baseline", [("put", [110, 100], 100), ("call", [100, 110], 110)])
def test_extreme_lot_is_baseline_not_first_or_average(mode, prices, baseline):
    rows = _rows(mode, prices=prices)
    assert explain_ranked_candidates(rows, mode=mode)["A"]["net_delivery_tier"]["baseline_net_price"] == baseline


@pytest.mark.parametrize("mode", ["put", "call"])
def test_pr1_missing_other_candidate_changes_actual_priority_not_target_facts(mode):
    rows = [_rows(mode)[i] for i in (0, 2)]
    target = deepcopy(rows[0])
    assert _ids(rank_candidates(rows, mode=mode)) == ["A", "C"]
    rows[1].pop("breakeven" if mode == "put" else "net_sale_price")
    assert _ids(rank_candidates(rows, mode=mode)) == ["C", "A"]
    tier = explain_ranked_candidates(rows, mode=mode)["A"]["net_delivery_tier"]
    assert tier["qualifies"] is True and tier["priority"] is False and tier["applied"] is False
    assert tier["reason"] == "candidate_net_price_missing"
    assert "新档位优先" not in tier["message"]
    assert rows[0] == target


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("state", ["empty", "missing_fee", "missing_price", "no_tier", "old_context", "legacy"])
def test_fallbacks_preserve_old_order_and_distinguish_absence(mode, state):
    rows = _rows(mode)
    old = [{k: v for k, v in row.items() if k not in ("strategy_core", "same_symbol_ranking_context")} for row in rows]
    if state == "empty":
        rows[0]["same_symbol_ranking_context"]["prices"] = []
    elif state == "missing_fee":
        rows[0]["same_symbol_ranking_context"].update(status="unavailable", reason="actual_opening_fee_unavailable")
    elif state == "missing_price":
        rows[0]["same_symbol_ranking_context"]["prices"].append({"lot_id": "bad", "net_price": None})
    elif state == "no_tier":
        for row in rows:
            row["breakeven" if mode == "put" else "net_sale_price"] = 100 if mode == "put" else 110
    elif state in ("old_context", "legacy"):
        for row in rows:
            row.pop("same_symbol_ranking_context")
            if state == "legacy":
                row.pop("strategy_core")
    assert _ids(rank_candidates(rows, mode=mode)) == _ids(rank_candidate_rows(old, mode=mode))
    result = explain_ranked_candidates(rows, mode=mode)["A"]
    if state == "legacy":
        assert "net_delivery_tier" not in result
    else:
        assert result["net_delivery_tier"]["priority"] is False
        assert result["net_delivery_tier"]["reason"] == {
            "empty": "no_positions", "missing_fee": "positions_unavailable", "missing_price": "position_net_price_missing",
            "no_tier": "no_new_tier", "old_context": "positions_unavailable",
        }[state]


def test_no_rebanding_inside_priority_group():
    rows = _rows("put")
    for row, income, discount in zip(rows, [.039, .038, .04], [.1, .2, .05]):
        row.update(period_net_return=income, net_assignment_discount_pct=discount)
    # All were in C's original band: B beats A. Rebanding is not a new rule.
    assert _ids(rank_candidates(rows, mode="put")) == ["B", "A", "C"]


@pytest.mark.parametrize("key,value", [("account", "sy"), ("mode", "call"), ("symbol", "MSFT"), ("currency", "HKD")])
def test_context_scope_conflicts_cannot_be_consumed(key, value):
    rows = _rows("put")
    rows[0]["same_symbol_ranking_context"][key] = value
    for call in (rank_candidates, explain_ranked_candidates):
        with pytest.raises(ValueError, match="conflicts"):
            call(rows, mode="put")


def test_mixed_context_and_policy_rejected():
    rows = deepcopy(_rows("put"))
    rows[1]["same_symbol_ranking_context"] = {**rows[1]["same_symbol_ranking_context"], "prices": []}
    with pytest.raises(ValueError, match="mixed"):
        rank_candidates(rows, mode="put")
    rows = _rows("put")
    rows[1]["strategy_core"]["effective_parameters"]["min_net_delivery_step_pct"] = .03
    rows[1]["strategy_core"]["effective_policy_hash"] = implementation_policy_hash(rows[1]["strategy_core"])
    with pytest.raises(ValueError, match="mixed"):
        rank_candidates(rows, mode="put")


@pytest.mark.parametrize("value", [0, 1, -1, True, None, "0.02", float("nan"), float("inf")])
def test_policy_rejects_invalid_step(value):
    with pytest.raises(ValueError):
        resolve_strategy(mode="put", config=replace(InsuranceUnderwritingConfig(), min_net_delivery_step_pct=value))


def test_new_identity_cannot_omit_parameter_or_existing_delta_gate():
    for field in ("min_net_delivery_step_pct", "max_abs_delta"):
        row = _rows("put")[0]
        row["strategy_core"]["effective_parameters"].pop(field)
        row["strategy_core"]["effective_policy_hash"] = implementation_policy_hash(row["strategy_core"])
        with pytest.raises(ValueError):
            validate_strategy_identity(row, mode="put")
