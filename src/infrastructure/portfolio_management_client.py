"""Compatibility exports and lazy constructor for the optional PM capability."""

from __future__ import annotations

from typing import Any, Callable

from src.application.portfolio_management_contract import (
    DEFAULT_SERVICE_URL,
    SERVICE_URL_ENV,
    API_VERSION,
    DEFAULT_MAX_RESPONSE_BYTES,
    VALUATION_MAX_RESPONSE_BYTES,
    VALUATION_EVIDENCE_SCHEMA,
    _FRESHNESS_STATUSES,
    _TRUST_STATUSES,
    _VIEW_PATHS,
    CAPITAL_FACTS_PATH,
    VALUATION_EVIDENCE_PATH,
    HOLDINGS_REFRESH_PATH,
    CONTRACT_OPERATIONS,
    PortfolioManagementError,
    PortfolioManagementUnavailableError,
    PortfolioManagementConfigError,
    PortfolioManagementTransportError,
    PortfolioManagementProtocolError,
    PortfolioManagementHTTPError,
    resolve_portfolio_service_origin,
    _validate_view_account_scope,
    _response_header,
    _response_status,
    _decode_object,
    _decode_optional_object,
    _decode_public_error,
    _normalized_accounts,
    _validate_valuation_evidence_response,
    _broker_scope,
    _validate_non_futu_inventory,
    _validate_freshness,
    validate_holdings_refresh_response,
    _require_timestamp,
)


class PortfolioManagementClient:
    """Construct the installed PM client without loading it during core imports."""

    def __new__(
        cls,
        *,
        service_url: str | None = None,
        urlopen_fn: Callable[..., Any] | None = None,
    ) -> Any:
        from src.application.portfolio_management import load_portfolio_management_client_class

        return load_portfolio_management_client_class()(
            service_url=service_url, urlopen_fn=urlopen_fn,
        )
