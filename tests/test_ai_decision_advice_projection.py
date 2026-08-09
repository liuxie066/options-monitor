from __future__ import annotations

import pytest

from src.application.ai_decision_advice.projection import (
    project_all_candidates,
    project_one_contract,
)


def _portfolio() -> dict:
    return {
        "status": "ready",
        "asset_weights": {
            "NVDA": 0.3,
            "AAPL": 0.4,
            "0700.HK": 0.2,
            "CNY-STOCK": 0.05,
        },
        "currency_weights": {"USD": 0.7, "HKD": 0.2, "CNY": 0.1},
        "cash_and_mmf_weight": 0.1,
        "gaps": [],
    }


def _option_view() -> dict:
    return {
        "status": "ready",
        "verified_structures": [
            {
                "label": "SP+LC",
                "symbol": "NVDA",
                "funding_contracts": 2,
                "expression_contracts": 1,
                "expression_to_funding_ratio": 0.5,
            }
        ],
        "gaps": [],
    }


def _positions() -> list[dict]:
    return [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "strike": 95,
            "expiry": "2026-09-18",
            "multiplier": 100,
            "contracts": 2,
        },
        {
            "symbol": "NVDA",
            "option_type": "call",
            "side": "long",
            "strike": 120,
            "expiry": "2026-09-18",
            "multiplier": 100,
            "contracts": 3,
        },
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "long",
            "strike": 85,
            "expiry": "2026-09-24",
            "multiplier": 100,
            "contracts": 4,
        },
        {
            "symbol": "AAPL",
            "option_type": "call",
            "side": "short",
            "strike": 210,
            "expiry": "2026-09-18",
            "multiplier": 100,
            "contracts": 5,
        },
    ]


def _project(
    candidate: dict,
    *,
    mode: str = "put",
    portfolio: dict | None = None,
    total: float | None = 1_000_000,
    shares: dict | None = None,
    rates: dict | None = None,
    option_view: dict | None = None,
    positions: list[dict] | None = None,
) -> dict:
    return project_one_contract(
        candidate=candidate,
        strategy_mode=mode,
        portfolio=portfolio if portfolio is not None else _portfolio(),
        option_positions=option_view or _option_view(),
        position_rows=_positions() if positions is None else positions,
        portfolio_total_cny=total,
        shares_by_symbol=shares or {"NVDA": 300, "AAPL": 200},
        cny_per_currency=rates or {"CNY": 1.0, "USD": 7.2, "HKD": 0.92},
    )


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (
            {
                "candidate_id": "us",
                "symbol": "NVDA",
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "expiry": "2026-09-18",
            },
            0.072,
        ),
        (
            {
                "candidate_id": "hk",
                "symbol": "0700.HK",
                "currency": "HKD",
                "strike": 400,
                "multiplier": 500,
                "expiry": "2026-09-18",
            },
            0.184,
        ),
        (
            {
                "candidate_id": "cn",
                "symbol": "CNY-STOCK",
                "currency": "CNY",
                "strike": 10,
                "multiplier": 100,
                "expiry": "2026-09-18",
            },
            0.001,
        ),
    ],
)
def test_sell_put_projection_uses_cny_per_currency(
    candidate: dict,
    expected: float,
) -> None:
    out = _project(candidate)

    assert out["assignment_exposure_ratio"] == expected
    assert out["call_away_fraction"] is None
    assert out["calculation_complete"] is True
    assert "assignment_notional" not in out
    assert "after_one_contract_weight" not in str(out)


def test_covered_call_projection_uses_current_pm_shares_only_as_denominator() -> None:
    out = _project(
        {
            "candidate_id": "cc1",
            "symbol": "AAPL",
            "currency": "USD",
            "strike": 200,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        mode="call",
    )

    assert out["call_away_fraction"] == 0.5
    assert out["assignment_exposure_ratio"] is None
    assert out["same_obligation_current_contracts"] == 5
    assert out["same_obligation_after_add_contracts"] == 6
    assert "call_away_shares" not in out
    assert "current_shares" not in out


def test_complete_portfolio_treats_absent_exposure_as_zero() -> None:
    out = _project(
        {
            "candidate_id": "new-symbol",
            "symbol": "MSFT",
            "currency": "EUR",
            "strike": 500,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        rates={"CNY": 1.0, "EUR": 8.0},
    )

    assert out["current_symbol_weight"] == 0.0
    assert out["current_currency_weight"] == 0.0


def test_complete_portfolio_preserves_negative_weights() -> None:
    portfolio = _portfolio()
    portfolio["asset_weights"]["NVDA"] = -0.12
    portfolio["currency_weights"]["USD"] = -0.08
    portfolio["cash_and_mmf_weight"] = -0.03

    out = _project(
        {
            "candidate_id": "sp1",
            "symbol": "NVDA",
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        portfolio=portfolio,
    )

    assert out["current_symbol_weight"] == -0.12
    assert out["current_currency_weight"] == -0.08
    assert out["cash_and_mmf_weight"] == -0.03


def test_unavailable_portfolio_keeps_exposure_unknown() -> None:
    out = _project(
        {
            "candidate_id": "sp1",
            "symbol": "NVDA",
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        portfolio={
            "status": "unavailable",
            "asset_weights": {},
            "currency_weights": {},
            "cash_and_mmf_weight": None,
            "gaps": ["portfolio_unavailable:source_failed"],
        },
    )

    assert out["current_symbol_weight"] is None
    assert out["current_currency_weight"] is None
    assert out["cash_and_mmf_weight"] is None


def test_contract_counts_use_actual_contracts_and_separate_exact_from_near() -> None:
    out = _project(
        {
            "candidate_id": "sp1",
            "symbol": "NVDA",
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "expiry": "2026-09-18",
        }
    )

    assert out["same_obligation_current_contracts"] == 2
    assert out["same_obligation_after_add_contracts"] == 3
    assert out["long_call_current_contracts"] == 3
    assert out["long_put_current_contracts"] == 4
    assert out["exact_expiry_current_contracts"] == 10
    assert out["exact_expiry_after_add_contracts"] == 11
    assert out["near_expiry_7d_current_contracts"] == 4
    assert out["near_expiry_7d_after_add_contracts"] == 4
    assert out["verified_structures"][0]["label"] == "SP+LC"


@pytest.mark.parametrize(
    ("candidate_patch", "total", "shares", "rates", "reason"),
    [
        ({"multiplier": None}, 1_000_000, None, None, "candidate_multiplier_missing"),
        ({"strike": None}, 1_000_000, None, None, "candidate_strike_missing"),
        ({}, None, None, None, "portfolio_total_cny_missing"),
        ({}, 1_000_000, None, {"CNY": 1.0}, "fx_rate_missing:USD"),
    ],
)
def test_sell_put_missing_calculation_inputs_create_explicit_gap(
    candidate_patch: dict,
    total: float | None,
    shares: dict | None,
    rates: dict | None,
    reason: str,
) -> None:
    candidate = {
        "candidate_id": "sp1",
        "symbol": "NVDA",
        "currency": "USD",
        "strike": 100,
        "multiplier": 100,
        "expiry": "2026-09-18",
        **candidate_patch,
    }
    out = _project(
        candidate,
        total=total,
        shares=shares,
        rates=rates,
    )

    assert out["assignment_exposure_ratio"] is None
    assert reason in out["gaps"]
    assert out["scope_ceiling"] == "needs_review"


def test_covered_call_missing_shares_is_a_gap_not_zero() -> None:
    out = _project(
        {
            "candidate_id": "cc1",
            "symbol": "MSFT",
            "currency": "USD",
            "strike": 500,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        mode="call",
        shares={"AAPL": 200},
    )

    assert out["call_away_fraction"] is None
    assert "covered_call_shares_missing" in out["gaps"]


def test_unavailable_options_do_not_become_zero_positions() -> None:
    out = _project(
        {
            "candidate_id": "sp1",
            "symbol": "NVDA",
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "expiry": "2026-09-18",
        },
        option_view={
            "status": "unavailable",
            "verified_structures": [],
            "gaps": ["option_positions_unavailable:ledger_failed"],
        },
        positions=[],
    )

    assert out["same_obligation_current_contracts"] is None
    assert "option_positions_unavailable" in out["gaps"]
    assert "option_positions_unavailable:ledger_failed" in out["gaps"]


def test_project_all_candidates_projects_every_identity_once() -> None:
    candidates = {
        "sell_put": [
            {
                "candidate_id": "sp1",
                "symbol": "NVDA",
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "expiry": "2026-09-18",
            }
        ],
        "covered_call": [
            {
                "candidate_id": "cc1",
                "symbol": "AAPL",
                "currency": "USD",
                "strike": 200,
                "multiplier": 100,
                "expiry": "2026-09-18",
            }
        ],
    }

    out = project_all_candidates(
        candidates=candidates,
        portfolio=_portfolio(),
        option_positions=_option_view(),
        position_rows=_positions(),
        portfolio_total_cny=1_000_000,
        shares_by_symbol={"NVDA": 300, "AAPL": 200},
        cny_per_currency={"CNY": 1.0, "USD": 7.2, "HKD": 0.92},
    )

    assert set(out) == {"sp1", "cc1"}
    assert out["sp1"]["strategy_mode"] == "put"
    assert out["cc1"]["strategy_mode"] == "call"
