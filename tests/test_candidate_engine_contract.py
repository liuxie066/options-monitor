from __future__ import annotations

import pytest

from domain.domain.engine import (
    CANDIDATE_STAGE_ORDER,
    build_candidate_reject,
    evaluate_opening_candidate_policy,
    explain_candidate_rank,
    rank_candidate_rows,
    select_best_candidate_per_symbol,
)


def _policy_row(*, mode: str = "put", **overrides):  # type: ignore[no-untyped-def]
    row = {
        "symbol": "NVDA",
        "contract_symbol": f"NVDA-{mode}-100",
        "option_type": mode,
        "expiration": "2026-09-18",
        "dte": 43,
        "spot": 110.0,
        "strike": 100.0 if mode == "put" else 120.0,
        "spread_ratio": 0.08,
        "annualized_net_return_on_cash_basis": 0.12,
        "annualized_net_premium_return": 0.12,
        "period_net_return_on_cash_basis": 0.014,
        "period_net_premium_return": 0.014,
        "net_premium_cny": 700.0,
        "iv_rv_ratio": 1.50,
        "iv_minus_rv": 0.12,
        "earnings_evidence_status": "ready",
        "earnings_has_event": False,
        "max_new_contracts": 1,
        "covered_contracts_available": 1,
    }
    row.update(overrides)
    return row


def _reasons(decision: dict) -> set[str]:
    return {str(item["reason"]) for item in decision["rejects"]}


def test_candidate_engine_stage_contract_and_reject_payload_are_stable() -> None:
    assert CANDIDATE_STAGE_ORDER == (
        "stage0_input_normalization",
        "stage1_hard_constraints",
        "stage2_return_floor",
        "stage3_risk_filter",
        "stage4_ranking",
    )
    reject = build_candidate_reject(
        stage="stage3_risk_filter",
        reason="risk_spread",
        message="wide",
        metric_value=0.5,
        threshold=0.4,
    )
    assert reject == {
        "stage": "stage3_risk_filter",
        "reason": "risk_spread",
        "message": "wide",
        "metric_value": 0.5,
        "threshold": 0.4,
    }


def test_sell_put_recall_window_uses_smaller_of_config_max_and_spot_then_80_pct() -> None:
    accepted = evaluate_opening_candidate_policy(
        _policy_row(strike=88.0),
        mode="put",
        min_strike=70.0,
        max_strike=120.0,
    )
    below = evaluate_opening_candidate_policy(
        _policy_row(strike=87.99),
        mode="put",
        min_strike=70.0,
        max_strike=120.0,
    )
    above = evaluate_opening_candidate_policy(
        _policy_row(strike=110.01),
        mode="put",
        min_strike=70.0,
        max_strike=120.0,
    )

    assert accepted["accepted"] is True
    assert "hard_strike" in _reasons(below)
    assert "hard_strike" in _reasons(above)


def test_covered_call_recall_window_starts_at_spot_and_caps_at_120_pct() -> None:
    assert evaluate_opening_candidate_policy(
        _policy_row(mode="call", strike=110.0),
        mode="call",
        min_strike=100.0,
        max_strike=150.0,
    )["accepted"] is True
    assert "hard_strike" in _reasons(
        evaluate_opening_candidate_policy(
            _policy_row(mode="call", strike=109.99),
            mode="call",
            min_strike=100.0,
            max_strike=150.0,
        )
    )
    assert "hard_strike" in _reasons(
        evaluate_opening_candidate_policy(
            _policy_row(mode="call", strike=132.01),
            mode="call",
            min_strike=100.0,
            max_strike=150.0,
        )
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"dte": 6}, "hard_dte"),
        ({"max_new_contracts": 0}, "hard_capacity_put"),
        ({"annualized_net_return_on_cash_basis": 0.09}, "return_annualized"),
        ({"net_premium_cny": 49.99}, "return_net_premium_cny"),
        ({"spread_ratio": 0.41}, "risk_spread"),
        ({"iv_rv_ratio": 1.09}, "risk_iv_rv_ratio"),
        ({"iv_minus_rv": 0.049}, "risk_iv_minus_rv"),
        ({"earnings_evidence_status": "unavailable"}, "risk_earnings_unavailable"),
        ({"earnings_has_event": True}, "risk_earnings_event"),
    ],
)
def test_sell_put_formal_gates_fail_closed(overrides: dict, reason: str) -> None:
    decision = evaluate_opening_candidate_policy(
        _policy_row(**overrides),
        mode="put",
        min_dte=7,
        max_dte=60,
    )
    assert reason in _reasons(decision)


def test_optional_oi_volume_and_delta_do_not_create_hard_gates() -> None:
    decision = evaluate_opening_candidate_policy(
        _policy_row(open_interest=None, volume=None, delta=None),
        mode="put",
    )
    assert decision["accepted"] is True


def test_covered_call_requires_physical_share_capacity() -> None:
    decision = evaluate_opening_candidate_policy(
        _policy_row(mode="call", covered_contracts_available=0, max_new_contracts=0),
        mode="call",
    )
    assert "hard_capacity_call" in _reasons(decision)


def test_rank_uses_period_return_band_then_strategy_tie_breaks() -> None:
    put_rows = [
        _policy_row(
            contract_symbol="RETURN_LEADER",
            period_net_return_on_cash_basis=0.0100,
            net_assignment_discount_pct=0.05,
        ),
        _policy_row(
            contract_symbol="NEAR_SAFER",
            period_net_return_on_cash_basis=0.0081,
            net_assignment_discount_pct=0.08,
        ),
        _policy_row(
            contract_symbol="NEXT_BAND",
            period_net_return_on_cash_basis=0.0079,
            net_assignment_discount_pct=0.20,
        ),
    ]
    call_rows = [
        _policy_row(
            mode="call",
            contract_symbol="LOW_STRIKE",
            strike=115.0,
            period_net_premium_return=0.0100,
        ),
        _policy_row(
            mode="call",
            contract_symbol="NEAR_HIGH_STRIKE",
            strike=125.0,
            period_net_premium_return=0.0081,
        ),
    ]

    assert [row["contract_symbol"] for row in rank_candidate_rows(put_rows, mode="put")] == [
        "NEAR_SAFER",
        "RETURN_LEADER",
        "NEXT_BAND",
    ]
    assert [row["contract_symbol"] for row in rank_candidate_rows(call_rows, mode="call")] == [
        "NEAR_HIGH_STRIKE",
        "LOW_STRIKE",
    ]
    assert explain_candidate_rank(call_rows[1], mode="call")["primary_drivers"] == [
        "period_net_premium_return"
    ]


def test_select_best_candidate_per_symbol_preserves_canonical_symbol_winner() -> None:
    rows = [
        _policy_row(symbol="NVDA", contract_symbol="NVDA_LOW", period_net_return_on_cash_basis=0.01),
        _policy_row(symbol="NVDA", contract_symbol="NVDA_HIGH", period_net_return_on_cash_basis=0.02),
        _policy_row(symbol="AMD", contract_symbol="AMD_ONLY", period_net_return_on_cash_basis=0.015),
    ]
    winners = select_best_candidate_per_symbol(rows, mode="put")
    assert {row["contract_symbol"] for row in winners} == {"NVDA_HIGH", "AMD_ONLY"}


def test_rank_api_has_no_score_weight_compatibility_alias() -> None:
    with pytest.raises(TypeError):
        rank_candidate_rows([_policy_row()], mode="put", score_weights={})  # type: ignore[call-arg]
