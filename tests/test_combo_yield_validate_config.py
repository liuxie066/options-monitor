from __future__ import annotations


import pytest


@pytest.mark.parametrize(("market", "open_interest", "volume"), [
    (None, 100, 5), ("", 100, 5), (" US ", 100, 5), (" hk ", 50, 0), ("unknown", 100, 5),
])
def test_combo_defaults_keep_market_values_and_return_independent_copies(market, open_interest, volume):
    from src.application.combo_yield_config import combo_yield_defaults_for_market
    from src.application.config_defaults import default_config

    before = default_config()
    defaults = combo_yield_defaults_for_market(market)
    assert defaults == {
        "enabled": False, "structure_mode": "same_expiry_pair", "objective": "premium_funded_long_call",
        "variant": "sp_lc", "min_combo_net_credit": None, "min_net_credit_annualized": 0.08,
        "min_net_credit_retention": 0.60, "min_open_interest": open_interest, "min_volume": volume,
        "max_spread_ratio": 0.35, "max_combo_spread_ratio": 0.5,
        "call": {"min_delta": 0.15, "max_delta": 0.35}, "put": {"min_delta": 0.15, "max_delta": 0.35},
    }
    market_key = str(market or "").strip().lower()
    market_key = market_key if market_key in {"us", "hk"} else "us"
    assert defaults == before["markets"][market_key]["symbol_defaults"]["combo_yield"]
    defaults["call"]["min_delta"] = -1
    defaults["min_volume"] = -1
    again = combo_yield_defaults_for_market(market)
    assert again["call"]["min_delta"] == 0.15 and again["min_volume"] == volume
    assert default_config() == before


def test_combo_validator_defaults_are_an_isolated_system_projection():
    from src.application.combo_yield_config import COMBO_YIELD_DEFAULTS
    from src.application.config_defaults import DEFAULT_CONFIG

    source = DEFAULT_CONFIG["markets"]["us"]["symbol_defaults"]["combo_yield"]
    assert COMBO_YIELD_DEFAULTS == source
    assert COMBO_YIELD_DEFAULTS is not source
    assert COMBO_YIELD_DEFAULTS["call"] is not source["call"]
    assert COMBO_YIELD_DEFAULTS["put"] is not source["put"]


def test_combo_authored_overrides_remain_separate_from_system_defaults():
    from src.application.combo_yield_config import apply_combo_yield_defaults, derive_combo_yield_policy
    from src.application.config_defaults import default_config

    before = default_config()
    override = {"enabled": True, "min_open_interest": 7, "min_combo_net_credit": None,
                "call": {"min_delta": 0.2}}
    applied = apply_combo_yield_defaults(override, market="hk")
    policy = derive_combo_yield_policy(override, market="hk")
    for result in (applied, policy.config):
        assert result["enabled"] and result["min_open_interest"] == 7 and result["min_volume"] == 0
        assert result["min_combo_net_credit"] is None
        assert result["call"] == {"min_delta": 0.2, "max_delta": 0.35}
    assert default_config() == before


def _config(symbols: list[dict], *, templates: dict | None = None) -> dict:
    return {"templates": {} if templates is None else templates, "symbols": symbols}


def _nvda(*, symbol: str = "NVDA", **sections) -> dict:
    return {"symbol": symbol, **sections}


def test_validate_config_accepts_minimal_sell_put_combo_yield_symbol() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={"enabled": True},
            sell_call={"enabled": False},
        )
    ])

    validate_config(cfg)


def test_validate_config_rejects_string_combo_yield_enabled() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True},
            combo_yield={"enabled": "false"},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit, match="NVDA.combo_yield.enabled must be a boolean"):
        validate_config(cfg)


def test_validate_config_rejects_removed_combo_yield_output_mode() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True},
            combo_yield={"enabled": True, "output_mode": "separate"},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit, match="output_mode has been removed") as exc_info:
        validate_config(cfg)
    assert "./om config build" in str(exc_info.value)


def test_validate_config_rejects_removed_combo_yield_funding_mode_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={
                "enabled": True,
                "funding_mode": "bad_mode",
                "call": {"min_strike": 108, "max_strike": 120},
            },
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield" in str(exc_info.value)
    assert "funding_mode" in str(exc_info.value)


def test_validate_config_rejects_removed_combo_yield_cost_ratio_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={
                "enabled": True,
                "max_call_cost_to_put_credit": 0.4,
                "call": {"min_strike": 108, "max_strike": 120},
            },
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield" in str(exc_info.value)
    assert "max_call_cost_to_put_credit" in str(exc_info.value)


def test_validate_config_rejects_removed_combo_yield_optimizer_field() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={"enabled": True, "max_downside_worsen_pct": -0.01},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield has removed optimizer fields" in str(exc_info.value)


def test_validate_config_rejects_invalid_combo_yield_objective() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={"enabled": True, "objective": "incremental_optimizer"},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield.objective" in str(exc_info.value)


def test_validate_config_rejects_removed_combo_yield_scenario_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={"enabled": True, "min_upside_lift_to_call_cost": -0.1},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield has removed scenario fields: min_upside_lift_to_call_cost" in str(exc_info.value)


def test_validate_config_rejects_invalid_combo_yield_net_credit_annualized() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 20,
                "max_dte": 60,
            },
            combo_yield={"enabled": True, "min_net_credit_annualized": -0.01},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield.min_net_credit_annualized" in str(exc_info.value)


def test_validate_config_rejects_combo_yield_net_credit_retention_outside_unit_interval() -> None:
    from src.application.config_validator import validate_config

    for value in (-0.01, 1.01):
        cfg = _config([
            _nvda(
                sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
                combo_yield={"enabled": True, "min_net_credit_retention": value},
                sell_call={"enabled": False},
            )
        ])

        with pytest.raises(SystemExit) as exc_info:
            validate_config(cfg)
        assert "NVDA.combo_yield.min_net_credit_retention" in str(exc_info.value)


def test_validate_config_rejects_invalid_template_combo_yield_call_bounds() -> None:
    from src.application.config_validator import validate_config

    cfg = _config(
        [
            _nvda(
                use=["put_base"],
                sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
                sell_call={"enabled": False},
            )
        ],
        templates={"put_base": {"combo_yield": {"call": {"min_strike": 120, "max_strike": 108}}}},
    )

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "templates.put_base.combo_yield.call.min_strike" in str(exc_info.value)


def test_validate_config_rejects_staggered_expiry_gap_fields_as_unknown_keys() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={
                "enabled": True,
                "structure_mode": "same_expiry_pair",
                "min_expiry_gap_days": 30,
                "max_expiry_gap_days": 90,
            },
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "contains unsupported keys" in str(exc_info.value)
    assert "min_expiry_gap_days" in str(exc_info.value)


def test_validate_config_rejects_absolute_call_dte_for_combo_yield() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={
                "enabled": True,
                "structure_mode": "same_expiry_pair",
                "call": {"min_dte": 60, "max_dte": 120},
            },
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "unsupported absolute DTE fields" in str(exc_info.value)


def test_validate_config_rejects_removed_template_combo_yield_call_otm_bounds() -> None:
    from src.application.config_validator import validate_config

    cfg = _config(
        [
            _nvda(
                use=["put_base"],
                sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
                sell_call={"enabled": False},
            )
        ],
        templates={"put_base": {"combo_yield": {"call": {"min_otm_pct": 0.5, "max_otm_pct": 0.2}}}},
    )

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "templates.put_base.combo_yield.call has removed OTM fields: min_otm_pct, max_otm_pct" in str(exc_info.value)


def test_validate_config_rejects_nested_sell_put_combo_yield_template_path() -> None:
    from src.application.config_validator import validate_config

    cfg = _config(
        [
            _nvda(
                use=["put_base"],
                sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
                sell_call={"enabled": False},
            )
        ],
        templates={"put_base": {"sell_put": {"combo_yield": {"enabled": True}}}},
    )

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "templates.put_base.sell_put.combo_yield has been removed" in str(exc_info.value)


def test_validate_config_rejects_nested_sell_put_combo_yield_symbol_path() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={
                "enabled": True,
                "min_dte": 20,
                "max_dte": 60,
                "combo_yield": {"enabled": True},
            },
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.sell_put.combo_yield has been removed" in str(exc_info.value)


def test_validate_config_rejects_legacy_rebound_combo_template_path() -> None:
    from src.application.config_validator import validate_config

    cfg = _config(
        [
            _nvda(
                use=["rebound_base"],
                sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
                sell_call={"enabled": False},
            )
        ],
        templates={"rebound_base": {"rebound_combo": {"enabled": False}}},
    )

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "templates.rebound_base.rebound_combo has been removed" in str(exc_info.value)


def test_validate_config_rejects_legacy_rebound_combo_symbol_path() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            sell_call={"enabled": False},
            rebound_combo={"enabled": True},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.rebound_combo has been removed" in str(exc_info.value)


def test_validate_config_rejects_removed_combo_yield_target_price_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = _config([
        _nvda(
            sell_put={"enabled": True, "min_dte": 20, "max_dte": 60},
            combo_yield={"enabled": True, "target_upside_pct": 0.15},
            sell_call={"enabled": False},
        )
    ])

    with pytest.raises(SystemExit) as exc_info:
        validate_config(cfg)
    assert "NVDA.combo_yield has removed target-price fields" in str(exc_info.value)


@pytest.mark.parametrize(
    ("side_payload", "expected_path", "typo"),
    [
        (
            {"sell_put": {"enabled": False, "min_annualized_net_retur": 0.99}},
            "NVDA.sell_put",
            "min_annualized_net_retur",
        ),
        (
            {"sell_call": {"enabled": False, "min_strike_cost_multipler": 1.1}},
            "NVDA.sell_call",
            "min_strike_cost_multipler",
        ),
        (
            {"combo_yield": {"enabled": False, "min_net_credit_retentin": 0.9}},
            "NVDA.combo_yield",
            "min_net_credit_retentin",
        ),
        (
            {"combo_yield": {"enabled": False, "call": {"max_dleta": 0.2}}},
            "NVDA.combo_yield.call",
            "max_dleta",
        ),
    ],
)
def test_validate_config_rejects_unknown_opening_strategy_keys(
    side_payload: dict,
    expected_path: str,
    typo: str,
) -> None:
    from src.application.config_validator import validate_config

    item = {"symbol": "NVDA", "sell_put": {"enabled": False}, "sell_call": {"enabled": False}, **side_payload}

    with pytest.raises(SystemExit) as exc_info:
        validate_config(_config([item]))

    message = str(exc_info.value)
    assert expected_path in message
    assert typo in message
    assert "did you mean" in message


def test_validate_config_accepts_independent_combo_yield_without_false_warning(capsys) -> None:
    from src.application.config_validator import validate_config

    validate_config(
        _config([
            _nvda(
                sell_put={"enabled": False},
                combo_yield={"enabled": True},
                sell_call={"enabled": False},
            )
        ])
    )

    assert "combo_yield is enabled but sell_put is disabled" not in capsys.readouterr().err


@pytest.mark.parametrize("leg", ["call", "put"])
@pytest.mark.parametrize("bounds", [
    {"min_delta": None}, {"max_delta": None}, {"min_delta": True},
    {"min_delta": -0.1}, {"max_delta": 1.1}, {"max_delta": float("nan")},
    {"max_delta": float("inf")}, {"min_delta": 0.4}, {"max_delta": 0.1},
    {"min_delta": 0.5, "max_delta": 0.4}, None,
])
def test_combo_long_delta_invalid_bounds_rejected(leg, bounds) -> None:
    from src.application.config_validator import validate_config
    with pytest.raises(SystemExit, match=f"combo_yield.{leg}"):
        validate_config(_config([_nvda(combo_yield={"enabled": True, leg: bounds})]))


@pytest.mark.parametrize("leg", ["call", "put"])
@pytest.mark.parametrize("bounds", [{"min_delta": 0.0}, {"max_delta": 1.0}, {"min_delta": 0.4, "max_delta": 0.5}])
def test_combo_long_delta_valid_overrides(leg, bounds) -> None:
    from src.application.config_validator import validate_config
    validate_config(_config([_nvda(combo_yield={"enabled": True, leg: bounds})]))
