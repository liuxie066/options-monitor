from __future__ import annotations

from domain.domain.close_advice import (
    CloseAdviceConfig,
    CloseAdviceInput,
    EXIT_REASON_TYPE_PROFIT_CAPTURE,
    EXIT_REASON_TYPE_RISK_EXIT,
    EXIT_REASON_TYPE_SALVAGE,
    EXIT_REASON_TYPE_TAKE_PROFIT,
    EXIT_REASON_TYPE_THESIS_EXPIRED,
    EXIT_STATE_HOLD,
    EXIT_STATE_LET_EXPIRE,
    EXIT_STATE_NOT_EVALUABLE,
    EXIT_STATE_PROFIT_CAPTURE,
    EXIT_STATE_RISK_EXIT,
    EXIT_STATE_SALVAGE,
    EXIT_STATE_TAKE_PROFIT,
    evaluate_close_advice,
    evaluate_long_call_convexity_advice,
    evaluate_short_vol_close_advice,
)
from domain.domain.short_vol_assessment import ShortVolAssessmentConfig


def _short_put_input(**overrides) -> CloseAdviceInput:
    base = {
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "expiration": "2026-06-19",
        "strike": 100,
        "contracts_open": 1,
        "premium": 1.0,
        "close_mid": 0.10,
        "bid": 0.09,
        "ask": 0.11,
        "dte": 30,
        "multiplier": 100,
        "spot": 120,
        "currency": "USD",
        "delta": -0.20,
    }
    base.update(overrides)
    return CloseAdviceInput(**base)


def _long_call_input(**overrides) -> CloseAdviceInput:
    base = {
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "side": "long",
        "expiration": "2026-06-19",
        "strike": 140,
        "contracts_open": 1,
        "premium": 1.0,
        "close_mid": 2.20,
        "bid": 2.10,
        "ask": 2.30,
        "dte": 30,
        "multiplier": 100,
        "spot": 125,
        "currency": "USD",
        "delta": 0.20,
    }
    base.update(overrides)
    return CloseAdviceInput(**base)


def test_return_first_profit_capture_contract() -> None:
    row = evaluate_close_advice(_short_put_input(), CloseAdviceConfig())

    assert row["tier"] == "strong"
    assert row["exit_state"] == EXIT_STATE_PROFIT_CAPTURE
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_PROFIT_CAPTURE


def test_short_vol_soft_risk_loss_is_not_actionable() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(close_mid=1.20, bid=1.19, ask=1.21),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": -0.20,
            "implied_volatility": 0.20,
            "realized_volatility_estimate": 0.20,
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["short_vol_thesis_status"] == "vol_edge_lost"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["realized_if_close"] < 0
    assert "risk_exit_loss_not_actionable" in row["data_quality_flags"]


def test_short_vol_event_risk_can_emit_risk_exit_even_when_not_profitable() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(close_mid=1.20, bid=1.19, ask=1.21),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False, reject_event_risk=True),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": -0.20,
            "implied_volatility": 0.20,
            "realized_volatility_estimate": 0.20,
            "event_flag": True,
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["short_vol_thesis_status"] == "event_risk"
    assert row["tier"] == "strong"
    assert row["exit_state"] == EXIT_STATE_RISK_EXIT
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_RISK_EXIT
    assert row["realized_if_close"] < 0


def test_long_call_take_profit_contract() -> None:
    row = evaluate_long_call_convexity_advice(_long_call_input(close_mid=2.20))

    assert row["exit_state"] == EXIT_STATE_TAKE_PROFIT
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_TAKE_PROFIT
    assert row["long_call_value_ratio"] == 2.2


def test_long_call_wide_spread_is_not_evaluable() -> None:
    row = evaluate_long_call_convexity_advice(
        _long_call_input(close_mid=2.20, bid=0.01, ask=4.39)
    )

    assert row["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert row["tier"] == "not_evaluable"
    assert "spread_too_wide" in row["data_quality_flags"]


def test_long_call_salvage_contract() -> None:
    row = evaluate_long_call_convexity_advice(
        _long_call_input(close_mid=0.08, bid=0.07, ask=0.09, dte=3, delta=0.03)
    )

    assert row["exit_state"] == EXIT_STATE_SALVAGE
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_SALVAGE


def test_long_call_let_expire_contract() -> None:
    row = evaluate_long_call_convexity_advice(
        _long_call_input(close_mid=0.01, bid=0.009, ask=0.011, dte=3, delta=0.02)
    )

    assert row["exit_state"] == EXIT_STATE_LET_EXPIRE
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_THESIS_EXPIRED
