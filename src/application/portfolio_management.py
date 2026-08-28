"""Shared options-monitor gate for optional portfolio-management calls."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable


PORTFOLIO_MANAGEMENT_DISABLED = "PORTFOLIO_MANAGEMENT_DISABLED"
PORTFOLIO_MANAGEMENT_UNAVAILABLE = "PORTFOLIO_MANAGEMENT_UNAVAILABLE"
PORTFOLIO_MANAGEMENT_INCOMPATIBLE = "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"

_LEGACY_HOLDINGS_SYNC_KEYS = {
    "enabled",
    "debounce_sec",
    "request_timeout_sec",
    "max_attempts",
    "retry_backoff_sec",
    "queue_capacity",
    "recent_deal_limit",
    "state_dir",
}


def normalize_portfolio_management_config(
    config: dict[str, Any] | None,
    *,
    warning_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Return canonical config while accepting the one-release legacy alias."""

    data = deepcopy(config) if isinstance(config, dict) else {}
    canonical_present = "portfolio_management" in data
    canonical = data.get("portfolio_management")
    if canonical_present:
        if not isinstance(canonical, dict):
            raise ValueError("portfolio_management must be an object")
        unknown = sorted(set(canonical) - {"enabled"})
        if unknown:
            raise ValueError(
                "portfolio_management contains unsupported keys: "
                + ", ".join(unknown)
            )
        enabled = canonical.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("portfolio_management.enabled must be a boolean")
    else:
        enabled = False

    trade_intake = data.get("trade_intake")
    trade_intake = deepcopy(trade_intake) if isinstance(trade_intake, dict) else None
    legacy_present = bool(
        isinstance(trade_intake, dict) and "holdings_sync" in trade_intake
    )
    if canonical_present and legacy_present:
        raise ValueError(
            "portfolio_management and trade_intake.holdings_sync cannot both be set"
        )
    if legacy_present and isinstance(trade_intake, dict):
        legacy = trade_intake.get("holdings_sync")
        if not isinstance(legacy, dict):
            raise ValueError("trade_intake.holdings_sync must be an object")
        unknown = sorted(set(legacy) - _LEGACY_HOLDINGS_SYNC_KEYS)
        if unknown:
            raise ValueError(
                "trade_intake.holdings_sync contains unsupported keys: "
                + ", ".join(unknown)
            )
        enabled = legacy.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(
                "trade_intake.holdings_sync.enabled must be a boolean"
            )
        ignored = sorted(set(legacy) - {"enabled"})
        if warning_fn is not None:
            suffix = f"; ignored keys: {', '.join(ignored)}" if ignored else ""
            warning_fn(
                "TRADE_INTAKE_HOLDINGS_SYNC_DEPRECATED: use "
                f"portfolio_management.enabled{suffix}"
            )
        trade_intake.pop("holdings_sync", None)
        data["trade_intake"] = trade_intake

    data["portfolio_management"] = {"enabled": bool(enabled)}
    return data


def portfolio_management_enabled(config: dict[str, Any] | None) -> bool:
    normalized = normalize_portfolio_management_config(config)
    return bool(normalized["portfolio_management"]["enabled"])


def resolve_portfolio_management_client(
    config: dict[str, Any] | None,
    *,
    client: Any | None = None,
    urlopen_fn: Callable[..., Any] | None = None,
) -> Any:
    """Return the injected/real client, or the stable disabled token."""

    if client is not None:
        return client
    if not portfolio_management_enabled(config):
        return PORTFOLIO_MANAGEMENT_DISABLED
    from src.infrastructure.portfolio_management_client import (
        PortfolioManagementClient,
    )

    return PortfolioManagementClient(urlopen_fn=urlopen_fn)


def portfolio_management_failure_code(exc: Exception) -> str:
    from src.infrastructure.portfolio_management_client import (
        PortfolioManagementConfigError,
        PortfolioManagementHTTPError,
        PortfolioManagementProtocolError,
        PortfolioManagementTransportError,
    )

    if isinstance(exc, PortfolioManagementConfigError):
        return "CONFIG_ERROR"
    if isinstance(exc, PortfolioManagementProtocolError):
        return PORTFOLIO_MANAGEMENT_INCOMPATIBLE
    if isinstance(exc, PortfolioManagementHTTPError) and exc.status in {404, 405, 422}:
        return PORTFOLIO_MANAGEMENT_INCOMPATIBLE
    if isinstance(exc, (PortfolioManagementTransportError, PortfolioManagementHTTPError)):
        return PORTFOLIO_MANAGEMENT_UNAVAILABLE
    return PORTFOLIO_MANAGEMENT_UNAVAILABLE


__all__ = [
    "PORTFOLIO_MANAGEMENT_DISABLED",
    "PORTFOLIO_MANAGEMENT_INCOMPATIBLE",
    "PORTFOLIO_MANAGEMENT_UNAVAILABLE",
    "normalize_portfolio_management_config",
    "portfolio_management_enabled",
    "portfolio_management_failure_code",
    "resolve_portfolio_management_client",
]
