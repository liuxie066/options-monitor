"""Installation and enablement boundary for the optional Position Advice policy."""

from __future__ import annotations

from importlib.util import find_spec
from typing import Any

from domain.domain.close_advice import (
    PositionAdviceUnavailableError,
    require_position_advice_plugin,
)


def position_advice_plugin_status(config: dict[str, Any] | None) -> str:
    """Inspect availability without importing the policy or enabling it."""
    advice = config.get("close_advice") if isinstance(config, dict) else None
    if not isinstance(advice, dict) or not advice.get("enabled", False):
        return "disabled"
    try:
        return "available" if find_spec("om_position_advice_plugin") is not None else "unavailable"
    except (ImportError, ValueError):
        return "unavailable"


__all__ = [
    "PositionAdviceUnavailableError",
    "position_advice_plugin_status",
    "require_position_advice_plugin",
]
