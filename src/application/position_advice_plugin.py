"""Installation and enablement boundary for the optional Position Advice policy."""

from __future__ import annotations

from typing import Any

from domain.domain.close_advice import (
    PositionAdviceUnavailableError,
    require_position_advice_plugin as _require_position_advice_plugin,
)
from src.application.plugin_runtime import plugin_installed


def position_advice_plugin_status(config: dict[str, Any] | None) -> str:
    """Inspect availability without importing the policy or enabling it."""
    advice = config.get("close_advice") if isinstance(config, dict) else None
    if not isinstance(advice, dict) or not advice.get("enabled", False):
        return "disabled"
    return "available" if plugin_installed("position-advice") else "unavailable"


def require_position_advice_plugin() -> Any:
    """Apply host admission even when an offline caller already cached the policy."""
    if not plugin_installed("position-advice"):
        raise PositionAdviceUnavailableError(
            "POSITION_ADVICE_UNAVAILABLE: optional package om-position-advice-plugin "
            "is not installed or is incompatible; use om plugins list"
        )
    return _require_position_advice_plugin()


def evaluate_close_advice(inp: Any) -> dict[str, Any]:
    return require_position_advice_plugin().evaluate_close_advice(inp)


__all__ = [
    "PositionAdviceUnavailableError",
    "position_advice_plugin_status",
    "require_position_advice_plugin",
    "evaluate_close_advice",
]
