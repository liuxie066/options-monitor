from __future__ import annotations

import pytest

from src.application.config_validator import validate_config
from src.application.config_profiles import apply_profiles
from src.application.symbol_mutations import default_use_for_enabled_sides, edit_symbol_entry


def _base_cfg(symbol: dict) -> dict:
    return {
        "accounts": ["lx"],
        "account_settings": {"lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}}},
        "templates": {
            "put_base": {"sell_put": {"strategy": "insurance_underwriting"}},
            "call_base": {"sell_call": {"strategy": "insurance_underwriting"}},
        },
        "symbols": [symbol],
    }


def test_validate_config_rejects_enabled_sell_call_without_call_base_or_strategy() -> None:
    cfg = _base_cfg(
        {
            "symbol": "PDD",
            "use": "put_base",
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 45, "min_strike": 130},
        }
    )

    with pytest.raises(SystemExit) as exc:
        validate_config(cfg)

    message = str(exc.value)
    assert "PDD.sell_call enabled but no sell_call.strategy is inherited" in message
    assert "Add call_base to PDD.use" in message
    assert 'use: ["put_base", "call_base"]' in message


def test_validate_config_accepts_enabled_sell_call_with_call_base() -> None:
    cfg = _base_cfg(
        {
            "symbol": "PDD",
            "use": ["put_base", "call_base"],
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 45, "min_strike": 130},
        }
    )

    validate_config(cfg)

    effective = apply_profiles(cfg["symbols"][0], cfg["templates"])
    assert effective["sell_call"]["strategy"] == "insurance_underwriting"


@pytest.mark.parametrize(
    "use",
    (
        ["put_base", "missing_guardrail"],
        ["put_base", "put_base"],
        ["put_base", 7],
        {"profile": "put_base"},
    ),
)
def test_validate_config_rejects_invalid_template_references(use) -> None:
    cfg = _base_cfg(
        {
            "symbol": "PDD",
            "use": use,
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": False},
        }
    )

    with pytest.raises(SystemExit):
        validate_config(cfg)


def test_edit_symbol_entry_can_ensure_call_base_for_covered_call() -> None:
    cfg = _base_cfg(
        {
            "symbol": "TIGR",
            "use": "put_base",
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": False},
        }
    )

    summary = edit_symbol_entry(
        cfg,
        symbol="tigr",
        sets={"sell_call.enabled": True, "sell_call.min_strike": 6.5},
        ensure_use=["call_base"],
    )

    assert summary.changed_paths == ["sell_call.enabled", "sell_call.min_strike", "sell_call.min_dte", "sell_call.max_dte", "use"]
    assert cfg["symbols"][0]["use"] == ["put_base", "call_base"]
    assert cfg["symbols"][0]["sell_call"] == {"enabled": True, "min_strike": 6.5, "min_dte": 7, "max_dte": 45}
    validate_config(cfg)


def test_validate_config_accepts_enabled_sell_call_with_explicit_strategy() -> None:
    cfg = _base_cfg(
        {
            "symbol": "PDD",
            "use": "put_base",
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 20,
                "max_dte": 45,
                "min_strike": 130,
            },
        }
    )

    validate_config(cfg)


def test_validate_config_rejects_enabled_sell_put_without_put_base_or_strategy() -> None:
    cfg = _base_cfg(
        {
            "symbol": "PDD",
            "use": "call_base",
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45},
            "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 45, "min_strike": 130},
        }
    )

    with pytest.raises(SystemExit) as exc:
        validate_config(cfg)

    message = str(exc.value)
    assert "PDD.sell_put enabled but no sell_put.strategy is inherited" in message
    assert "Add put_base to PDD.use" in message


def test_default_symbol_use_matches_enabled_strategy_sides() -> None:
    assert default_use_for_enabled_sides(sell_put_enabled=True, sell_call_enabled=False) == "put_base"
    assert default_use_for_enabled_sides(sell_put_enabled=False, sell_call_enabled=True) == "call_base"
    assert default_use_for_enabled_sides(sell_put_enabled=True, sell_call_enabled=True) == ["put_base", "call_base"]
