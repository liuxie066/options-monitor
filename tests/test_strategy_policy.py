from __future__ import annotations

from src.application.strategy_policy import (
    resolve_position_strategy,
    strategy_side_config_for_resolution,
)


def test_strategy_resolution_prefers_position_snapshot() -> None:
    position = {
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
    }
    config = {"symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "short_vol"}}]}

    resolution = resolve_position_strategy(position=position, config=config)

    assert resolution.strategy_source == "position_snapshot"
    assert resolution.strategy_family == "sell_put"
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
    assert resolution.strategy_profile == "short_vol"
    assert side_cfg["strategy"] == "short_vol"
    assert side_cfg["min_open_interest"] == 50
