from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_validate_config_accepts_minimal_sell_put_combo_yield_symbol() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    validate_config(cfg)


def test_validate_config_rejects_removed_combo_yield_funding_mode_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "funding_mode": "bad_mode",
                    "call": {"min_strike": 108, "max_strike": 120},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield" in str(exc)
        assert "funding_mode" in str(exc)


def test_validate_config_rejects_removed_combo_yield_cost_ratio_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "max_call_cost_to_put_credit": 0.4,
                    "call": {"min_strike": 108, "max_strike": 120},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield" in str(exc)
        assert "max_call_cost_to_put_credit" in str(exc)


def test_validate_config_rejects_removed_combo_yield_optimizer_field() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "max_downside_worsen_pct": -0.01,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield has removed optimizer fields" in str(exc)


def test_validate_config_rejects_invalid_combo_yield_objective() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "objective": "incremental_optimizer",
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield.objective" in str(exc)


def test_validate_config_rejects_removed_combo_yield_scenario_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "min_upside_lift_to_call_cost": -0.1,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield has removed scenario fields: min_upside_lift_to_call_cost" in str(exc)


def test_validate_config_rejects_invalid_combo_yield_net_credit_annualized() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "strategy": "insurance_underwriting",
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {
                    "enabled": True,
                    "min_net_credit_annualized": -0.01,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield.min_net_credit_annualized" in str(exc)


def test_validate_config_rejects_combo_yield_net_credit_retention_outside_unit_interval() -> None:
    from src.application.config_validator import validate_config

    for value in (-0.01, 1.01):
        cfg = {
            "templates": {},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                    "combo_yield": {"enabled": True, "min_net_credit_retention": value},
                    "sell_call": {"enabled": False},
                }
            ],
        }

        try:
            validate_config(cfg)
            raise AssertionError("expected config validation failure")
        except SystemExit as exc:
            assert "NVDA.combo_yield.min_net_credit_retention" in str(exc)


def test_validate_config_rejects_invalid_template_combo_yield_call_bounds() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {
            "put_base": {
                "combo_yield": {
                    "call": {"min_strike": 120, "max_strike": 108},
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "templates.put_base.combo_yield.call.min_strike" in str(exc)


def test_validate_config_accepts_staggered_expiry_gap_bounds() -> None:
    from src.application.config_validator import validate_config

    validate_config(
        {
            "templates": {},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                    "combo_yield": {
                        "enabled": True,
                        "structure_mode": "staggered_expiry_pair",
                        "min_expiry_gap_days": 30,
                        "max_expiry_gap_days": 90,
                    },
                    "sell_call": {"enabled": False},
                }
            ],
        }
    )


def test_validate_config_rejects_absolute_call_dte_for_combo_yield() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {
                    "enabled": True,
                    "structure_mode": "staggered_expiry_pair",
                    "call": {"min_dte": 60, "max_dte": 120},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "unsupported absolute DTE fields" in str(exc)


def test_validate_config_rejects_invalid_staggered_expiry_gap_bounds() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {
                    "enabled": True,
                    "structure_mode": "staggered_expiry_pair",
                    "min_expiry_gap_days": 91,
                    "max_expiry_gap_days": 90,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "min_expiry_gap_days >" in str(exc)


def test_validate_config_rejects_removed_template_combo_yield_call_otm_bounds() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {
            "put_base": {
                "combo_yield": {
                    "call": {"min_otm_pct": 0.5, "max_otm_pct": 0.2},
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "templates.put_base.combo_yield.call has removed OTM fields: min_otm_pct, max_otm_pct" in str(exc)


def test_validate_config_rejects_nested_sell_put_combo_yield_template_path() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {
            "put_base": {
                "sell_put": {
                    "combo_yield": {"enabled": True},
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "templates.put_base.sell_put.combo_yield has been removed" in str(exc)


def test_validate_config_rejects_nested_sell_put_combo_yield_symbol_path() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                    "combo_yield": {"enabled": True},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.sell_put.combo_yield has been removed" in str(exc)


def test_validate_config_rejects_legacy_rebound_combo_template_path() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {
            "rebound_base": {
                "rebound_combo": {"enabled": False},
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "use": ["rebound_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "templates.rebound_base.rebound_combo has been removed" in str(exc)


def test_validate_config_rejects_legacy_rebound_combo_symbol_path() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "sell_call": {"enabled": False},
                "rebound_combo": {"enabled": True},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.rebound_combo has been removed" in str(exc)


def test_validate_config_rejects_removed_combo_yield_target_price_fields() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {
                    "enabled": True,
                    "target_upside_pct": 0.15,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield has removed target-price fields" in str(exc)


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

    item = {
        "symbol": "NVDA",
        "sell_put": {"enabled": False},
        "sell_call": {"enabled": False},
        **side_payload,
    }

    with pytest.raises(SystemExit) as exc_info:
        validate_config({"templates": {}, "symbols": [item]})

    message = str(exc_info.value)
    assert expected_path in message
    assert typo in message
    assert "did you mean" in message


def test_validate_config_accepts_independent_combo_yield_without_false_warning(capsys) -> None:
    from src.application.config_validator import validate_config

    validate_config(
        {
            "templates": {},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "sell_put": {"enabled": False},
                    "combo_yield": {"enabled": True},
                    "sell_call": {"enabled": False},
                }
            ],
        }
    )

    assert "combo_yield is enabled but sell_put is disabled" not in capsys.readouterr().err
