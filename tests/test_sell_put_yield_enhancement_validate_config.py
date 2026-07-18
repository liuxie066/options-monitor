from __future__ import annotations

import json
import sys
from pathlib import Path

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


def test_validate_config_rejects_invalid_sell_put_combo_yield_funding_mode() -> None:
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
        assert "NVDA.combo_yield.funding_mode" in str(exc)


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


def test_validate_config_rejects_diagonal_combo_without_explicit_call_dte_window() -> None:
    from src.application.config_validator import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {
                    "enabled": True,
                    "expiry_structure": "diagonal",
                    "call": {"min_delta": 0.10, "max_delta": 0.20},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield.expiry_structure=diagonal requires call.min_dte and call.max_dte" in str(exc)


def test_validate_config_rejects_invalid_combo_expiry_structure_and_gap() -> None:
    from src.application.config_validator import validate_config

    base = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {"enabled": True},
                "sell_call": {"enabled": False},
            }
        ],
    }

    invalid_structure = json.loads(json.dumps(base))
    invalid_structure["symbols"][0]["combo_yield"]["expiry_structure"] = "calendar_guess"
    try:
        validate_config(invalid_structure)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield.expiry_structure must be one of" in str(exc)

    invalid_gap = json.loads(json.dumps(base))
    invalid_gap["symbols"][0]["combo_yield"]["min_expiry_gap_days"] = 0
    try:
        validate_config(invalid_gap)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.combo_yield.min_expiry_gap_days must be > 0" in str(exc)


def test_validate_config_rejects_zero_diagonal_call_dte_bounds() -> None:
    from src.application.config_validator import validate_config

    base = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "combo_yield": {
                    "enabled": True,
                    "expiry_structure": "diagonal",
                    "call": {"min_dte": 61, "max_dte": 90},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    for field in ("min_dte", "max_dte"):
        cfg = json.loads(json.dumps(base))
        cfg["symbols"][0]["combo_yield"]["call"][field] = 0
        try:
            validate_config(cfg)
            raise AssertionError("expected config validation failure")
        except SystemExit as exc:
            assert f"NVDA.combo_yield.call.{field} must be > 0" in str(exc)
