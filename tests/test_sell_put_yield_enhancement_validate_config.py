from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_validate_config_accepts_minimal_sell_put_yield_enhancement_symbol() -> None:
    from scripts.validate_config import validate_config

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
                "yield_enhancement": {
                    "enabled": True
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    validate_config(cfg)


def test_validate_config_rejects_invalid_sell_put_yield_enhancement_funding_mode() -> None:
    from scripts.validate_config import validate_config

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
                "yield_enhancement": {
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
        assert "NVDA.yield_enhancement.funding_mode" in str(exc)


def test_validate_config_rejects_invalid_template_yield_enhancement_call_bounds() -> None:
    from scripts.validate_config import validate_config

    cfg = {
        "templates": {
            "put_base": {
                "yield_enhancement": {
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
        assert "templates.put_base.yield_enhancement.call.min_strike" in str(exc)


def test_validate_config_rejects_nested_sell_put_yield_enhancement_template_path() -> None:
    from scripts.validate_config import validate_config

    cfg = {
        "templates": {
            "put_base": {
                "sell_put": {
                    "yield_enhancement": {"enabled": True},
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
        assert "templates.put_base.sell_put.yield_enhancement has been removed" in str(exc)


def test_validate_config_rejects_nested_sell_put_yield_enhancement_symbol_path() -> None:
    from scripts.validate_config import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                    "yield_enhancement": {"enabled": True},
                },
                "sell_call": {"enabled": False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError("expected config validation failure")
    except SystemExit as exc:
        assert "NVDA.sell_put.yield_enhancement has been removed" in str(exc)


def test_validate_config_rejects_legacy_rebound_combo_template_path() -> None:
    from scripts.validate_config import validate_config

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
    from scripts.validate_config import validate_config

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


def test_validate_config_rejects_removed_yield_enhancement_target_price_fields() -> None:
    from scripts.validate_config import validate_config

    cfg = {
        "templates": {},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "yield_enhancement": {
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
        assert "NVDA.yield_enhancement has removed target-price fields" in str(exc)
