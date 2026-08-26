from __future__ import annotations

import pytest

from src.application.strategy_policy import (
    INSURANCE_UNDERWRITING_PROFILE,
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    SHORT_VOL_PROFILE,
    RETURN_FIRST_PROFILE,
    resolve_position_strategy,
    resolve_position_strategy_semantics,
    resolve_combo_yield_position_role,
    assert_strategy_config_resolved,
    strategy_semantics_for_profile,
    strategy_semantics_for_side_config,
    strategy_side_config_for_resolution,
)


def test_strategy_resolution_prefers_position_snapshot() -> None:
    position = {
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
    }
    config = {"symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "insurance_underwriting"}}]}

    resolution = resolve_position_strategy(position=position, config=config)

    assert resolution.strategy_source == "position_snapshot"
    assert resolution.strategy_family == "sell_put"
    assert resolution.strategy_profile == "return_first"
    assert resolution.risk_model == "return_first_legacy"


def test_strategy_resolution_uses_combo_yield_mode_for_short_put() -> None:
    position = {
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "strategy": "yield_enhancement",
        "leg_role": "sell_put",
        "strategy_group_id": "ye_nvda_1",
        "yield_enhancement_mode": "vol_convexity_enhancement",
    }
    config = {"symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "return_first"}}]}

    resolution = resolve_position_strategy(position=position, config=config)

    assert resolution.strategy_source == "position_legacy_combo_yield_mode"
    assert resolution.strategy_family == "sell_put"
    assert resolution.strategy_profile == "short_vol"
    assert resolution.risk_model == "short_vol"


def test_strategy_resolution_defaults_legacy_combo_yield_to_return_first() -> None:
    position = {
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "strategy": "yield_enhancement",
        "leg_role": "sell_put",
        "strategy_group_id": "ye_nvda_1",
    }
    config = {"symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "insurance_underwriting"}}]}

    resolution = resolve_position_strategy(position=position, config=config)

    assert resolution.strategy_source == "position_combo_yield_identity"
    assert resolution.strategy_profile == "return_first"
    assert resolution.risk_model == "return_first_legacy"


def test_strategy_resolution_uses_template_defaults_for_symbol_config() -> None:
    position = {"symbol": "NVDA", "option_type": "put", "side": "short"}
    config = {
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_open_interest": 50,
                }
            }
        },
        "symbols": [{"symbol": "NVDA", "use": ["put_base"], "sell_put": {"enabled": True}}],
    }

    resolution = resolve_position_strategy(position=position, config=config)
    side_cfg = strategy_side_config_for_resolution(resolution=resolution, position=position, config=config)

    assert resolution.strategy_source == "current_config"
    assert resolution.strategy_profile == "insurance_underwriting"
    assert side_cfg["strategy"] == "insurance_underwriting"
    assert side_cfg["min_open_interest"] == 50


def test_sell_put_strategy_semantics_matrix() -> None:
    return_first = strategy_semantics_for_profile(family=SELL_PUT_FAMILY, profile=RETURN_FIRST_PROFILE)
    underwriting = strategy_semantics_for_profile(family=SELL_PUT_FAMILY, profile=INSURANCE_UNDERWRITING_PROFILE)
    short_vol = strategy_semantics_for_profile(family=SELL_PUT_FAMILY, profile=SHORT_VOL_PROFILE)

    assert return_first.strategy_profile == RETURN_FIRST_PROFILE
    assert return_first.risk_model == "return_first_legacy"
    assert return_first.scan_requires_rv is False
    assert return_first.scan_uses_short_vol_gate is False
    assert return_first.close_advice_profile == "sell_put_return_first"
    assert return_first.close_requires_rv is False

    assert underwriting.strategy_profile == INSURANCE_UNDERWRITING_PROFILE
    assert underwriting.risk_model == "short_vol"
    assert underwriting.scan_strategy_profile == INSURANCE_UNDERWRITING_PROFILE
    assert underwriting.scan_requires_rv is True
    assert underwriting.scan_uses_underwriting_gate is True
    assert underwriting.scan_uses_short_vol_gate is False
    assert underwriting.scan_uses_path_risk is False
    assert underwriting.close_advice_profile == "sell_put_short_vol"
    assert underwriting.close_requires_rv is True
    assert underwriting.close_uses_short_vol_thesis is True

    assert short_vol.strategy_profile == SHORT_VOL_PROFILE
    assert short_vol.scan_strategy_profile == SHORT_VOL_PROFILE
    assert short_vol.scan_requires_rv is False
    assert short_vol.scan_uses_underwriting_gate is False
    assert short_vol.close_advice_profile == "sell_put_short_vol"
    assert short_vol.close_uses_short_vol_thesis is True


def test_sell_call_strategy_semantics_matrix() -> None:
    return_first = strategy_semantics_for_profile(family=SELL_CALL_FAMILY, profile=RETURN_FIRST_PROFILE)
    underwriting = strategy_semantics_for_profile(family=SELL_CALL_FAMILY, profile=INSURANCE_UNDERWRITING_PROFILE)
    short_vol = strategy_semantics_for_profile(family=SELL_CALL_FAMILY, profile=SHORT_VOL_PROFILE)

    assert return_first.scan_requires_rv is False
    assert return_first.close_advice_profile == "covered_call_return_first"
    assert return_first.close_requires_rv is False

    assert underwriting.scan_strategy_profile == INSURANCE_UNDERWRITING_PROFILE
    assert underwriting.scan_requires_rv is True
    assert underwriting.scan_uses_underwriting_gate is True
    assert underwriting.close_advice_profile == "covered_call_short_vol"
    assert underwriting.close_requires_rv is True

    assert short_vol.scan_strategy_profile == SHORT_VOL_PROFILE
    assert short_vol.scan_requires_rv is False
    assert short_vol.scan_uses_underwriting_gate is False
    assert short_vol.close_advice_profile == "covered_call_short_vol"
    assert short_vol.close_requires_rv is True


def test_strategy_semantics_for_side_config_rejects_legacy_opening_profile() -> None:
    with pytest.raises(ValueError) as _caught:
        strategy_semantics_for_side_config(
            family=SELL_PUT_FAMILY,
            side_cfg={"strategy": "yield_first"},
        )
    exc = _caught.value
    assert "only supports insurance_underwriting" in str(exc)


def test_strategy_semantics_for_side_config_defaults_to_underwriting() -> None:
    semantics = strategy_semantics_for_side_config(
        family=SELL_PUT_FAMILY,
        side_cfg={},
    )

    assert semantics.strategy_profile == INSURANCE_UNDERWRITING_PROFILE
    assert semantics.scan_uses_underwriting_gate is True
    assert semantics.close_requires_rv is True


def test_position_strategy_semantics_reads_retired_combo_yield_mode() -> None:
    position = {
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "strategy": "yield_enhancement",
        "leg_role": "sell_put",
        "yield_enhancement_mode": "vol_convexity_enhancement",
    }
    config = {"symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "return_first"}}]}

    resolution, semantics = resolve_position_strategy_semantics(position=position, config=config)

    assert resolution.strategy_source == "position_legacy_combo_yield_mode"
    assert semantics.strategy_profile == SHORT_VOL_PROFILE
    assert semantics.close_requires_rv is True
    assert "yield_enhancement_mode" not in semantics.to_fields()


def test_strategy_semantics_does_not_emit_retired_combo_yield_fields() -> None:
    fields = strategy_semantics_for_profile(
        family=SELL_PUT_FAMILY,
        profile=INSURANCE_UNDERWRITING_PROFILE,
    ).to_fields()

    assert not any("yield_enhancement" in key for key in fields)


def test_strategy_config_resolution_guard_rejects_unexpanded_template_symbol() -> None:
    with pytest.raises(ValueError) as _caught:
        assert_strategy_config_resolved(
            {
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True},
            }
        )
    exc = _caught.value
    assert "apply templates/profiles" in str(exc)
    assert "sell_put" in str(exc)


def test_strategy_config_resolution_guard_accepts_expanded_template_symbol() -> None:
    assert_strategy_config_resolved(
        {
            "symbol": "NVDA",
            "use": ["put_base"],
            "sell_put": {"enabled": True, "strategy": "insurance_underwriting"},
        }
    )


def test_unknown_strategy_family_does_not_enable_underwriting_scan() -> None:
    semantics = strategy_semantics_for_profile(family="combo_yield", profile=INSURANCE_UNDERWRITING_PROFILE)

    assert semantics.strategy_family == "combo_yield"
    assert semantics.scan_strategy_profile == INSURANCE_UNDERWRITING_PROFILE
    assert semantics.scan_uses_underwriting_gate is False
    assert semantics.scan_requires_rv is False
    assert semantics.scan_uses_path_risk is False


def test_combo_yield_position_role_does_not_treat_any_grouped_put_as_combo_yield() -> None:
    role = resolve_combo_yield_position_role(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strategy_group_id": "other_combo",
        }
    )

    assert role.is_combo_yield_short_put is False


def test_combo_yield_position_role_identifies_grouped_sell_put_leg() -> None:
    role = resolve_combo_yield_position_role(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "leg_role": "sell_put",
            "strategy_group_id": "ye_nvda_1",
        }
    )

    assert role.is_combo_yield_short_put is True


def test_combo_yield_position_role_recognizes_canonical_funding_put() -> None:
    role = resolve_combo_yield_position_role(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "leg_role": "funding_put",
        }
    )

    assert role.is_combo_yield_short_put is True


def test_combo_yield_position_role_recognizes_canonical_participation_call() -> None:
    role = resolve_combo_yield_position_role(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "position_side": "long",
            "leg_role": "participation_call",
        }
    )

    assert role.is_combo_yield_long_call is True


def test_combo_yield_position_role_keeps_legacy_aliases_readable() -> None:
    put_aliases = ("sell_put", "enhancement_put", "yield_enhancement_put")
    call_aliases = ("enhancement_call", "long_call", "upside_call", "convexity_call")

    for leg_role in put_aliases:
        role = resolve_combo_yield_position_role(
            {
                "option_type": "put",
                "position_side": "short",
                "leg_role": leg_role,
                "strategy_group_id": "combo_yield:legacy",
            }
        )
        assert role.is_combo_yield_short_put is True

    for leg_role in call_aliases:
        role = resolve_combo_yield_position_role(
            {
                "option_type": "call",
                "position_side": "long",
                "leg_role": leg_role,
            }
        )
        assert role.is_combo_yield_long_call is True
