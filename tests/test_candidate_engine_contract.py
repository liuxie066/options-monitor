from __future__ import annotations

from datetime import date

import pytest

from domain.domain.engine import (
    CANDIDATE_STAGE_ORDER,
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
    build_candidate_reject,
    evaluate_opening_candidate_policy,
    explain_candidate_rank,
    rank_candidate_rows,
    select_best_candidate_per_symbol,
)


def _earnings_evidence(*, event_date: str | None = None) -> dict:
    event = None
    if event_date is not None:
        days_before_expiration = (
            date.fromisoformat("2026-09-18")
            - date.fromisoformat(event_date)
        ).days
        blocking = days_before_expiration <= EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
        event = {
            "earnings_date": event_date,
            "days_before_expiration": days_before_expiration,
            "classification": "blocking" if blocking else "nonblocking",
            "blocking": blocking,
        }
    events = [] if event is None else [event]
    blocking_events = [item for item in events if item["blocking"]]
    nonblocking_events = [item for item in events if not item["blocking"]]
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": "2026-08-06",
        "earnings_hard_window_start": "2026-09-12",
        "earnings_hard_window_end": "2026-09-18",
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": bool(events),
        "earnings_blocking_has_event": bool(blocking_events),
        "earnings_events": events,
        "earnings_blocking_events": blocking_events,
        "earnings_nonblocking_events": nonblocking_events,
    }


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
        "max_new_contracts": 1,
        "covered_contracts_available": 1,
    }
    row.update(_earnings_evidence())
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
        (_earnings_evidence(event_date="2026-09-12"), "risk_earnings_event"),
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


@pytest.mark.parametrize(
    ("event_date", "accepted"),
    [
        ("2026-09-18", False),
        ("2026-09-12", False),
        ("2026-09-11", True),
    ],
)
def test_earnings_window_is_inclusive_on_days_zero_and_six_only(
    event_date: str,
    accepted: bool,
) -> None:
    decision = evaluate_opening_candidate_policy(
        _policy_row(**_earnings_evidence(event_date=event_date)),
        mode="put",
    )

    assert decision["accepted"] is accepted


def test_same_market_day_earnings_remains_pending_without_timestamp_semantics() -> None:
    evidence = _earnings_evidence(event_date="2026-08-06")
    event = {
        **evidence["earnings_events"][0],
        "earnings_timestamp": 1.0,
        "pub_type": "BEFORE_MARKET",
    }
    evidence.update(
        {
            "earnings_events": [event],
            "earnings_nonblocking_events": [event],
        }
    )
    decision = evaluate_opening_candidate_policy(
        _policy_row(**evidence),
        mode="put",
    )

    assert decision["accepted"] is True


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
