"""Compatibility facade for the consolidated portfolio-management client."""

from __future__ import annotations

import urllib.request
from typing import Any, Callable

from src.infrastructure.portfolio_management_client import (
    PortfolioManagementClient,
    PortfolioManagementError,
    PortfolioManagementTransportError,
    resolve_portfolio_service_origin as _resolve_origin,
)


class PortfolioHoldingsSyncError(RuntimeError):
    """Raised when the local portfolio holdings sync cannot be confirmed."""


class PortfolioHoldingsSyncUnknownError(PortfolioHoldingsSyncError):
    """The request may have committed, but no valid response was received."""


def resolve_portfolio_service_origin(value: str | None = None) -> str:
    try:
        return _resolve_origin(value)
    except PortfolioManagementError as exc:
        raise PortfolioHoldingsSyncError(str(exc)) from exc


def sync_portfolio_holdings(
    account: str,
    *,
    timeout_sec: float = 120.0,
    service_url: str | None = None,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    account_label = str(account or "").strip().lower()
    if not account_label:
        raise PortfolioHoldingsSyncError("account is required")
    try:
        return PortfolioManagementClient(
            service_url=service_url,
            urlopen_fn=urlopen_fn,
        ).sync_holdings(account=account_label, timeout=float(timeout_sec))
    except PortfolioManagementTransportError as exc:
        raise PortfolioHoldingsSyncUnknownError(str(exc)) from exc
    except PortfolioManagementError as exc:
        raise PortfolioHoldingsSyncError(str(exc)) from exc
