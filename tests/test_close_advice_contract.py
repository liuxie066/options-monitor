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
    HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE,
    HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE,
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

    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["realized_if_close"] < 0
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    assert "默认可接货" in row["reason"]
    assert "IV/RV edge" in row["short_vol_reason"]
    assert "risk_exit_loss_not_actionable" not in row["data_quality_flags"]


def test_short_vol_sell_put_event_context_does_not_override_loss_hold() -> None:
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
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-01",
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["event_context_status"] == "in_window"
    assert row["event_context_types"] == "earnings"
    assert row["event_context_dates"] == "2026-06-01"
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    assert "默认可接货" in row["reason"]
    assert "到期前存在事件风险" in row["short_vol_reason"]
    assert "risk_exit_loss_not_actionable" not in row["data_quality_flags"]
    assert row["realized_if_close"] < 0


def test_short_vol_covered_call_event_context_does_not_override_loss_hold() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(option_type="call", close_mid=1.20, bid=1.19, ask=1.21, delta=0.20),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False, reject_event_risk=True),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": 0.20,
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-01",
            "event_source_status": "ok",
        },
        mode="call",
    )

    assert row["event_context_status"] == "in_window"
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
    assert "默认可被行权卖出正股" in row["reason"]
    assert "到期前存在事件风险" in row["short_vol_reason"]
    assert "risk_exit_loss_not_actionable" not in row["data_quality_flags"]
    assert row["realized_if_close"] < 0


def test_short_vol_event_context_profit_still_uses_return_capture() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(option_type="call", close_mid=0.20, bid=0.19, ask=0.21, delta=0.20),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False, reject_event_risk=True),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": 0.20,
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-01",
            "event_source_status": "ok",
        },
        mode="call",
    )

    assert row["event_context_status"] == "in_window"
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "strong"
    assert row["exit_state"] == EXIT_STATE_PROFIT_CAPTURE
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_PROFIT_CAPTURE
    assert row["realized_if_close"] > 0
    assert "到期前存在事件风险" in row["short_vol_reason"]


def test_short_vol_missing_risk_data_keeps_profit_capture_action() -> None:
    inp = _short_put_input()
    base = evaluate_close_advice(inp, CloseAdviceConfig())
    row = evaluate_short_vol_close_advice(
        inp,
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": -0.20,
            "implied_volatility": 0.30,
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["tier"] == base["tier"] == "strong"
    assert row["reason"] == base["reason"]
    assert row["exit_state"] == EXIT_STATE_PROFIT_CAPTURE
    assert row["exit_reason_type"] == EXIT_REASON_TYPE_PROFIT_CAPTURE
    assert row["short_vol_thesis_status"] == "not_evaluable"
    assert row["short_vol_reason"] == "缺少 short-vol 平仓评估数据: rv"
    assert "short_vol_risk_data_missing" in row["data_quality_flags"]


def test_short_vol_profitable_soft_risk_without_capture_is_hold_observation() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(close_mid=0.80, bid=0.79, ask=0.81, dte=30),
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

    assert row["realized_if_close"] > 0
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    assert "IV/RV edge" in row["short_vol_reason"]
    assert "不作为平仓提醒" in row["reason"]
    assert "risk_exit_loss_not_actionable" not in row["data_quality_flags"]


def test_short_vol_delta_high_without_capture_is_hold_observation_for_covered_call() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(option_type="call", close_mid=0.80, bid=0.79, ask=0.81, dte=30, delta=0.45),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": 0.45,
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_source_status": "ok",
        },
        mode="call",
    )

    assert row["realized_if_close"] > 0
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
    assert "delta 偏离承保观察区间" in row["short_vol_reason"]
    assert "不作为平仓提醒" in row["reason"]


def test_short_vol_event_without_capture_is_hold_observation() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(close_mid=0.80, bid=0.79, ask=0.81, dte=30),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False, reject_event_risk=True),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": -0.20,
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-01",
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["event_context_status"] == "in_window"
    assert row["realized_if_close"] > 0
    assert row["short_vol_thesis_status"] == "observe"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["exit_reason_type"] == EXIT_STATE_HOLD
    assert "到期前存在事件风险" in row["short_vol_reason"]
    assert "不作为平仓提醒" in row["reason"]


def test_short_vol_sell_put_valid_loss_marks_assignment_hold() -> None:
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
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_source_status": "ok",
        },
        mode="put",
    )

    assert row["short_vol_thesis_status"] == "valid"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    assert "等待归零或接货" in row["reason"]


def test_short_vol_covered_call_valid_loss_marks_called_away_hold() -> None:
    row = evaluate_short_vol_close_advice(
        _short_put_input(option_type="call", close_mid=1.20, bid=1.19, ask=1.21, delta=0.20),
        short_vol_config=ShortVolAssessmentConfig(enable_stress_check=False, reject_event_risk=True),
        close_config=CloseAdviceConfig(),
        quote_row={
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-06-19",
            "strike": 100,
            "spot": 120,
            "delta": 0.20,
            "implied_volatility": 0.30,
            "realized_volatility_estimate": 0.20,
            "event_source_status": "ok",
        },
        mode="call",
    )

    assert row["short_vol_thesis_status"] == "valid"
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
    assert "等待归零或被行权" in row["reason"]


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
