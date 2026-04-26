from __future__ import annotations

import math

from domain.domain.close_advice import CloseAdviceConfig, CloseAdviceInput, evaluate_close_advice


def _inp(*, premium: float = 1.0, mid: float = 0.1, dte: int = 10, option_type: str = "put") -> CloseAdviceInput:
    return CloseAdviceInput(
        account="lx",
        symbol="NVDA",
        option_type=option_type,
        side="short",
        expiration="2026-05-15",
        strike=100.0,
        contracts_open=1,
        premium=premium,
        close_mid=mid,
        bid=max(0.01, mid - 0.01),
        ask=mid + 0.01,
        dte=dte,
        multiplier=100,
        spot=120.0,
        currency="USD",
    )


def test_close_advice_strong_uses_dynamic_dte_thresholds() -> None:
    cfg = CloseAdviceConfig()

    assert evaluate_close_advice(_inp(premium=1.0, mid=0.10, dte=10), cfg)["tier"] == "strong"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.15, dte=20), cfg)["tier"] == "strong"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.20, dte=40), cfg)["tier"] == "strong"


def test_close_advice_medium_weak_optional_and_none() -> None:
    cfg = CloseAdviceConfig()

    assert evaluate_close_advice(_inp(premium=1.0, mid=0.27, dte=25), cfg)["tier"] == "medium"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.45, dte=40), cfg)["tier"] == "weak"
    assert evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.05,
            bid=0.045,
            ask=0.055,
            dte=3,
            multiplier=100,
            currency="USD",
        ),
        cfg,
    )["tier"] == "optional"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.60, dte=20), cfg)["tier"] == "none"


def test_close_advice_metrics_for_put_and_call() -> None:
    put = evaluate_close_advice(_inp(premium=2.0, mid=0.4, dte=20), CloseAdviceConfig())
    assert round(put["capture_ratio"], 6) == 0.8
    assert round(put["remaining_premium"], 6) == 40.0
    assert round(put["realized_if_close"], 6) == 160.0
    assert round(put["remaining_annualized_return"], 6) == 0.073

    call = evaluate_close_advice(_inp(premium=2.0, mid=0.4, dte=20, option_type="call"), CloseAdviceConfig())
    assert round(call["remaining_annualized_return"], 6) == 0.060833


def test_close_advice_data_quality_blocks_notifications() -> None:
    assert evaluate_close_advice(_inp(premium=0.0), CloseAdviceConfig())["tier"] == "none"
    assert "invalid_premium" in evaluate_close_advice(_inp(premium=0.0), CloseAdviceConfig())["data_quality_flags"]

    no_quote = evaluate_close_advice(_inp(mid=0.0), CloseAdviceConfig())
    assert no_quote["tier"] == "none"
    assert "invalid_mid" in no_quote["data_quality_flags"]

    wide = evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.5,
            bid=0.1,
            ask=0.9,
            dte=30,
            multiplier=100,
            currency="USD",
        ),
        CloseAdviceConfig(max_spread_ratio=0.4),
    )
    assert wide["tier"] == "none"
    assert "spread_too_wide" in wide["data_quality_flags"]

    unsupported = evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="long",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.1,
            dte=30,
            multiplier=100,
            currency="USD",
        )
    )
    assert unsupported["tier"] == "none"
    assert "unsupported_position" in unsupported["data_quality_flags"]


def test_close_advice_mid_above_premium_is_not_profit_advice() -> None:
    row = evaluate_close_advice(_inp(premium=1.0, mid=1.1, dte=30), CloseAdviceConfig())
    assert row["tier"] == "none"
    assert "not_profitable_to_close" in row["data_quality_flags"]


def test_close_advice_nan_quote_is_treated_as_missing_data() -> None:
    row = evaluate_close_advice(_inp(mid=math.nan), CloseAdviceConfig())
    assert row["tier"] == "none"
    assert "missing_mid" in row["data_quality_flags"]
