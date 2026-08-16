from __future__ import annotations

from src.application.sell_put_underwriting import (
    SellPutCandidate,
    SellPutPolicy,
    evaluate_sell_put_candidate,
)


def _candidate(**overrides):
    data = {
        "symbol": "SPCX",
        "name": "SpaceX",
        "current_price": 140.0,
        "strike": 118.0,
        "expiration": "2026-09-18",
        "bid": 3.9,
        "ask": 4.1,
        "mid": 4.0,
        "delta": -0.25,
        "iv": 0.70,
        "volume": 100,
        "open_interest": 500,
        "contract_symbol": "SPCX260918P00118000",
        "option_type": "put",
    }
    data.update(overrides)
    return SellPutCandidate(**data)


def test_sell_put_underwriting_approves_tier_a_when_guardrails_pass() -> None:
    payload = evaluate_sell_put_candidate(_candidate(), portfolio_nav=150000)

    assert payload["final_decision"] in {"GO_SMALL_SIZE", "GO_REDUCED_SIZE", "GO_NORMAL_SIZE"}
    assert payload["conviction_tier"] == "tier_a"
    assert payload["effective_basis"] == 114.0
    assert payload["hard_vetoes"] == []
    assert payload["exit_plan"]["time_exit_dte"] == 14
    assert payload["underwriting_answer"].startswith("YES")


def test_sell_put_underwriting_rejects_unauthorized_single_stock_csp() -> None:
    payload = evaluate_sell_put_candidate(_candidate(symbol="NVDA"), portfolio_nav=150000)

    assert payload["final_decision"] == "NO_GO"
    assert "conviction_not_authorized_for_cash_secured_put" in payload["hard_vetoes"]


def test_sell_put_underwriting_treats_cash_capacity_as_unlimited() -> None:
    payload = evaluate_sell_put_candidate(_candidate(), portfolio_nav=30000)

    assert payload["final_decision"] in {"GO_SMALL_SIZE", "GO_REDUCED_SIZE", "GO_NORMAL_SIZE"}
    assert payload["cash_assumption"] == "unlimited_cash"
    assert payload["hard_vetoes"] == []


def test_sell_put_underwriting_does_not_require_nav_for_final_approval() -> None:
    payload = evaluate_sell_put_candidate(_candidate(), portfolio_nav=None)

    assert payload["final_decision"] in {"GO_SMALL_SIZE", "GO_REDUCED_SIZE", "GO_NORMAL_SIZE"}
    assert payload["portfolio_nav"] is None
    assert payload["cash_required"] == 11800.0


def test_sell_put_underwriting_rejects_low_iv_contracts() -> None:
    payload = evaluate_sell_put_candidate(_candidate(iv=0.25), portfolio_nav=None)

    assert payload["final_decision"] == "NO_GO"
    assert "implied_volatility_below_threshold" in payload["hard_vetoes"]
