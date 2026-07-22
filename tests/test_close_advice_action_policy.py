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
    assert out["policy_version"] == "p0_current.v1", scenario
    assert out["recommendation_state"] == (
        "close" if expected_action in {"close", "close_put_keep_call"} else "hold"
    ), scenario
    assert out["decision_basis"], scenario
    assert out["decision_evidence_status"] == "complete", scenario


def test_close_action_policy_consumes_recommendation_state_as_authority() -> None:
    out = runner._apply_close_action_semantics(
        {
            "option_type": "put",
            "side": "short",
            "strategy_family": "sell_put",
            "exit_state": "profit_capture",
            "tier": "medium",
            "policy_version": "shadow_test",
            "recommendation_state": "hold",
            "decision_basis": "underwriting_edge_remains",
            "decision_evidence_status": "complete",
        }
    )

    assert out["close_action"] == "hold"
    assert out["policy_version"] == "shadow_test"
    assert out["decision_basis"] == "underwriting_edge_remains"


def test_close_advice_read_projects_old_artifacts_without_mutating_action() -> None:
    from src.application.agent_tools.close_advice_read_impl import _public_row, _summary

    old_close = _public_row(
        {
            "account": "lx",
            "symbol": "NVDA",
            "tier": "medium",
            "exit_state": "profit_capture",
            "close_action": "close",
        }
    )
    old_hold = _public_row(
        {
            "account": "lx",
            "symbol": "AAPL",
            "tier": "none",
            "exit_state": "hold",
            "close_action": "hold",
        }
    )

    assert old_close["policy_version"] == "legacy_p0"
    assert old_close["recommendation_state"] == "close"
    assert old_close["decision_basis"] == "legacy_close_action"
    assert old_close["close_action"] == "close"
    assert old_hold["recommendation_state"] == "hold"
    assert _summary([old_close, old_hold])["recommendation_counts"] == {"close": 1, "hold": 1}


def test_close_action_policy_registry_matches_declared_modes() -> None:
    expected_modes = {
        runner.CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION,
        runner.CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG,
        runner.CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG,
    }

    assert set(runner._CLOSE_ACTION_POLICY_REGISTRY) == expected_modes
    for mode, policy in runner._CLOSE_ACTION_POLICY_REGISTRY.items():
        assert policy.exit_mode == mode
