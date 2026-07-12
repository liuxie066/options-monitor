from __future__ import annotations

import pytest

from src.application import close_advice_runner as runner


@pytest.mark.parametrize(
    ("scenario", "row", "expected_mode", "expected_action"),
    [
        (
            "sell_put_return_first",
            {
                "option_type": "put",
                "side": "short",
                "strategy_family": "sell_put",
                "strategy_profile": "return_first",
                "exit_state": "profit_capture",
                "tier": "strong",
            },
            "standard_short_option",
            "close",
        ),
        (
            "sell_put_short_vol",
            {
                "option_type": "put",
                "side": "short",
                "strategy_family": "sell_put",
                "strategy_profile": "short_vol",
                "exit_state": "profit_capture",
                "tier": "medium",
            },
            "standard_short_option",
            "close",
        ),
        (
            "covered_call_return_first",
            {
                "option_type": "call",
                "side": "short",
                "strategy_family": "sell_call",
                "strategy_profile": "return_first",
                "exit_state": "hold",
                "tier": "none",
            },
            "standard_short_option",
            "hold",
        ),
        (
            "covered_call_legacy_risk_exit_read_only",
            {
                "option_type": "call",
                "side": "short",
                "strategy_family": "sell_call",
                "strategy_profile": "short_vol",
                "exit_state": "risk_exit",
                "tier": "strong",
            },
            "standard_short_option",
            "hold",
        ),
        (
            "yield_enhancement_put_income_upside",
            {
                "option_type": "put",
                "side": "short",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "income_upside_enhancement",
                "exit_state": "profit_capture",
                "tier": "strong",
            },
            "yield_enhancement_put_leg",
            "close_put_keep_call",
        ),
        (
            "yield_enhancement_put_vol_convexity",
            {
                "option_type": "put",
                "side": "short",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_2",
                "yield_enhancement_mode": "vol_convexity_enhancement",
                "exit_state": "profit_capture",
                "tier": "medium",
            },
            "yield_enhancement_put_leg",
            "close_put_keep_call",
        ),
        (
            "yield_enhancement_put_legacy_risk_exit_read_only",
            {
                "option_type": "put",
                "side": "short",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_legacy",
                "yield_enhancement_mode": "vol_convexity_enhancement",
                "exit_state": "risk_exit",
                "tier": "medium",
            },
            "yield_enhancement_put_leg",
            "hold_put_keep_call",
        ),
        (
            "yield_enhancement_long_call_income_upside",
            {
                "option_type": "call",
                "side": "long",
                "strategy": "yield_enhancement",
                "leg_role": "enhancement_call",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "income_upside_enhancement",
                "exit_state": "hold",
                "tier": "none",
            },
            "yield_enhancement_long_call_leg",
            "hold_call",
        ),
        (
            "yield_enhancement_long_call_vol_convexity",
            {
                "option_type": "call",
                "side": "long",
                "strategy": "yield_enhancement",
                "leg_role": "enhancement_call",
                "strategy_group_id": "ye_nvda_2",
                "yield_enhancement_mode": "vol_convexity_enhancement",
                "exit_state": "hold",
                "tier": "none",
            },
            "yield_enhancement_long_call_leg",
            "hold_call_as_convexity",
        ),
    ],
)
def test_close_action_policy_scenario_matrix(
    scenario: str,
    row: dict[str, object],
    expected_mode: str,
    expected_action: str,
) -> None:
    out = runner._apply_close_action_semantics(dict(row))

    assert out["strategy_exit_mode"] == expected_mode, scenario
    assert out["close_action"] == expected_action, scenario


def test_close_action_policy_registry_matches_declared_modes() -> None:
    expected_modes = {
        runner.CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION,
        runner.CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG,
        runner.CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG,
    }

    assert set(runner._CLOSE_ACTION_POLICY_REGISTRY) == expected_modes
    for mode, policy in runner._CLOSE_ACTION_POLICY_REGISTRY.items():
        assert policy.exit_mode == mode
