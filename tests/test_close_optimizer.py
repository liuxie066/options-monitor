from __future__ import annotations

import math

from domain.domain.close_advice import (
    OPTIMIZER_DEFER_NO_DELTA_REASON,
    CloseAdviceInput,
    CloseOptimizerConfig,
    calc_effective_annualized_return,
    calc_risk_adjusted_return,
    calc_switch_value_ratio,
    calc_tail_risk_score,
    decide_optimizer_tier,
    evaluate_close_optimizer,
)


def _inp(
    *,
    premium: float = 1.0,
    mid: float = 0.1,
    dte: int = 30,
    strike: float = 100.0,
    delta: float = 0.15,
    option_type: str = "put",
) -> CloseAdviceInput:
    return CloseAdviceInput(
        account="lx",
        symbol="NVDA",
        option_type=option_type,
        side="short",
        expiration="2026-06-15",
        strike=strike,
        contracts_open=1,
        premium=premium,
        close_mid=mid,
        bid=max(0.01, mid - 0.01),
        ask=mid + 0.01,
        dte=dte,
        multiplier=100,
        spot=120.0,
        currency="USD",
        delta=delta,
        otm_pct=0.15,
    )


def _cfg(**overrides: float) -> CloseOptimizerConfig:
    kwargs: dict[str, float] = {
        "min_capture_for_optimizer": 0.70,
        "min_tail_risk_for_action": 0.03,
        "min_switch_value_ratio": 0.30,
        "max_effective_annualized_to_close": 0.06,
        "min_risk_adjusted_to_hold": 0.20,
    }
    kwargs.update(overrides)
    return CloseOptimizerConfig(**kwargs)


def _default_cfg() -> CloseOptimizerConfig:
    return _cfg()


def test_tail_risk_zero_when_fully_captured() -> None:
    assert calc_tail_risk_score(capture_ratio=1.0, delta=0.5, dte=30) == 0.0


def test_tail_risk_zero_when_dte_zero() -> None:
    assert calc_tail_risk_score(capture_ratio=0.8, delta=0.5, dte=0) == 0.0


def test_tail_risk_proportional_to_delta_and_capture_gap() -> None:
    low = calc_tail_risk_score(capture_ratio=0.9, delta=0.05, dte=30)
    high = calc_tail_risk_score(capture_ratio=0.7, delta=0.40, dte=45)
    assert low < high
    assert low < 0.01
    assert high > 0.03


def test_tail_risk_clamped_to_one() -> None:
    assert calc_tail_risk_score(capture_ratio=0.0, delta=1.0, dte=365) == 1.0


def test_effective_annualized_uses_capital_tied_up() -> None:
    result = calc_effective_annualized_return(
        remaining_premium=40.0,
        capital_tied_up=10000.0,
        dte=30,
    )
    expected = (40.0 / 10000.0) * (365.0 / 30.0)
    assert result is not None and math.isclose(result, expected)


def test_effective_annualized_none_when_capital_zero() -> None:
    assert calc_effective_annualized_return(40.0, 0.0, 30) is None


def test_effective_annualized_none_when_dte_zero() -> None:
    assert calc_effective_annualized_return(40.0, 10000.0, 0) is None


def test_risk_adjusted_penalizes_high_delta() -> None:
    low_delta = calc_risk_adjusted_return(0.10, delta=0.05)
    high_delta = calc_risk_adjusted_return(0.10, delta=0.50)
    assert low_delta is not None and high_delta is not None
    assert low_delta > high_delta
    assert math.isclose(low_delta, 0.10 / 0.05)
    assert math.isclose(high_delta, 0.10 / 0.50)


def test_risk_adjusted_floor_delta_at_002() -> None:
    result = calc_risk_adjusted_return(0.10, delta=0.01)
    assert result is not None and math.isclose(result, 0.10 / 0.02)


def test_risk_adjusted_none_when_annualized_none() -> None:
    assert calc_risk_adjusted_return(None, delta=0.1) is None


def test_switch_value_ratio_positive() -> None:
    result = calc_switch_value_ratio(
        alternative_annualized=0.18, effective_annualized=0.06
    )
    assert result is not None and math.isclose(result, 2.0)


def test_switch_value_ratio_none_when_effective_none() -> None:
    assert calc_switch_value_ratio(0.18, None) is None


def test_switch_value_ratio_none_when_alternative_none() -> None:
    assert calc_switch_value_ratio(None, 0.06) is None


def test_switch_value_ratio_none_when_effective_zero() -> None:
    assert calc_switch_value_ratio(0.18, 0.0) is None


def test_decide_defer_when_capture_too_low() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.60,
        delta=0.15,
        tail_risk_score=0.04,
        effective_annualized=0.05,
        risk_adjusted=0.25,
        switch_value=0.50,
        config=_default_cfg(),
    )
    assert tier == "defer"


def test_decide_hold_deep_otm() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.85,
        delta=0.03,
        tail_risk_score=0.01,
        effective_annualized=0.04,
        risk_adjusted=2.0,
        switch_value=None,
        config=_default_cfg(),
    )
    assert tier == "optimizer_hold"


def test_decide_hold_risk_adjusted_high() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.85,
        delta=0.10,
        tail_risk_score=0.01,
        effective_annualized=0.10,
        risk_adjusted=0.50,
        switch_value=None,
        config=_default_cfg(),
    )
    assert tier == "optimizer_hold"


def test_decide_switch_when_good_alternative_exists() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.85,
        delta=0.15,
        tail_risk_score=0.04,
        effective_annualized=0.04,
        risk_adjusted=0.15,
        switch_value=0.50,
        config=_default_cfg(),
    )
    assert tier == "optimizer_switch"


def test_decide_close_when_high_tail_risk_no_alternative() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.80,
        delta=0.50,
        tail_risk_score=0.05,
        effective_annualized=0.03,
        risk_adjusted=0.06,
        switch_value=None,
        config=_default_cfg(),
    )
    assert tier == "optimizer_close"


def test_decide_close_not_triggered_if_annualized_ok() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.80,
        delta=0.50,
        tail_risk_score=0.05,
        effective_annualized=0.08,
        risk_adjusted=0.16,
        switch_value=None,
        config=_default_cfg(),
    )
    assert tier == "defer"


def test_decide_defer_when_no_condition_met() -> None:
    tier, _ = decide_optimizer_tier(
        capture_ratio=0.75,
        delta=0.10,
        tail_risk_score=0.02,
        effective_annualized=0.06,
        risk_adjusted=0.19,
        switch_value=0.15,
        config=_default_cfg(),
    )
    assert tier == "defer"


def test_evaluate_optimizer_defer_when_not_profitable() -> None:
    result = evaluate_close_optimizer(
        _inp(premium=1.0, mid=1.0), _default_cfg()
    )
    assert result["optimizer_tier"] == "defer"


def test_evaluate_optimizer_defer_when_not_short() -> None:
    inp = CloseAdviceInput(
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="long",
        expiration="2026-06-15",
        strike=100.0,
        contracts_open=1,
        premium=1.0,
        close_mid=0.1,
        dte=30,
        multiplier=100,
        currency="USD",
    )
    result = evaluate_close_optimizer(inp, _default_cfg())
    assert result["optimizer_tier"] == "defer"


def test_evaluate_optimizer_defer_when_delta_missing() -> None:
    inp = CloseAdviceInput(
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        expiration="2026-06-15",
        strike=100.0,
        contracts_open=1,
        premium=1.0,
        close_mid=0.1,
        dte=30,
        multiplier=100,
        currency="USD",
    )
    result = evaluate_close_optimizer(inp, _default_cfg())
    assert result["optimizer_tier"] == "defer"
    assert result["optimizer_reason"] == OPTIMIZER_DEFER_NO_DELTA_REASON


def test_evaluate_optimizer_hold_deep_otm() -> None:
    result = evaluate_close_optimizer(
        _inp(premium=1.0, mid=0.15, delta=0.03),
        _default_cfg(),
    )
    assert result["optimizer_tier"] == "optimizer_hold"


def test_evaluate_optimizer_switch_with_alternative() -> None:
    result = evaluate_close_optimizer(
        _inp(premium=1.0, mid=0.20, dte=45, delta=0.20),
        _default_cfg(),
        alternative_annualized_return=0.15,
    )
    assert result["optimizer_tier"] in ("optimizer_switch", "optimizer_close", "defer")
    assert result["effective_annualized_return"] is not None
    assert result["tail_risk_score"] is not None
    assert result["delta"] is not None


def test_evaluate_optimizer_passes_through_alternative() -> None:
    result = evaluate_close_optimizer(
        _inp(premium=1.0, mid=0.10, delta=0.15),
        _default_cfg(),
        alternative_annualized_return=0.12,
    )
    assert result["alternative_annualized_return"] == 0.12


def test_optimizer_config_from_mapping_defaults() -> None:
    cfg = CloseOptimizerConfig.from_mapping(None)
    assert cfg.min_capture_for_optimizer == 0.70
    assert cfg.min_tail_risk_for_action == 0.03


def test_optimizer_config_from_mapping_partial() -> None:
    cfg = CloseOptimizerConfig.from_mapping({"min_capture_for_optimizer": 0.80})
    assert cfg.min_capture_for_optimizer == 0.80
    assert cfg.min_tail_risk_for_action == 0.03
