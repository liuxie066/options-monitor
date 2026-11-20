"""Single loopback HTTP adapter for portfolio-management."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from src.application.portfolio_management_contract import (
    API_VERSION,
    DEFAULT_MAX_RESPONSE_BYTES,
    VALUATION_MAX_RESPONSE_BYTES,
    _VIEW_PATHS,
    CAPITAL_FACTS_PATH,
    VALUATION_EVIDENCE_PATH,
    HOLDINGS_REFRESH_PATH,
    PortfolioManagementConfigError,
    PortfolioManagementTransportError,
    PortfolioManagementProtocolError,
    PortfolioManagementHTTPError,
    resolve_portfolio_service_origin,
    _validate_view_account_scope,
    _response_header,
    _response_status,
    _decode_public_error,
    _decode_optional_object,
    _decode_object,
    _normalized_accounts,
    _validate_valuation_evidence_response,
    validate_holdings_refresh_response,
)


class PortfolioManagementClient:
    """Version-aware endpoint client with no implicit retries."""

    def __init__(
        self,
        *,
        service_url: str | None = None,
        urlopen_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.origin = resolve_portfolio_service_origin(service_url)
        self._urlopen = urlopen_fn or urllib.request.urlopen

    def read_view(
        self,
        view: str,
        *,
        query: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        try:
            path = _VIEW_PATHS[view]
        except KeyError as exc:
            raise PortfolioManagementConfigError(f"unsupported portfolio view: {view}") from exc
        response = self._request("GET", path, query=query, timeout=timeout)
        _validate_view_account_scope(response, query or {})
        return response

    def read_capital_facts(
        self,
        *,
        account: str,
        period: str,
        as_of_month: str,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            CAPITAL_FACTS_PATH,
            query={"account": account, "period": period, "as_of_month": as_of_month},
            timeout=30.0,
        )

    def read_valuation_evidence(
        self,
        *,
        accounts: list[str],
        supplemental_codes: list[str],
        price_timeout: int,
        holdings_scope: str | None = None,
    ) -> dict[str, Any]:
        normalized_accounts = _normalized_accounts(accounts)
        payload = {
            "accounts": normalized_accounts,
            "supplemental_codes": supplemental_codes,
            "price_timeout": int(price_timeout),
        }
        if holdings_scope is not None:
            payload["holdings_scope"] = holdings_scope
        result = self._request(
            "POST",
            VALUATION_EVIDENCE_PATH,
            payload=payload,
            timeout=float(min(max(int(price_timeout) + 10, 15), 180)),
            max_response_bytes=VALUATION_MAX_RESPONSE_BYTES,
        )
        return _validate_valuation_evidence_response(
            result,
            requested_accounts=normalized_accounts,
            holdings_scope=holdings_scope,
        )

    def request_holdings_refresh(
        self,
        *,
        account: str,
        request_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            HOLDINGS_REFRESH_PATH,
            payload={"account": account, "request_id": request_id},
            timeout=timeout,
            expected_status=202,
        )
        return validate_holdings_refresh_response(
            result,
            requested_account=account,
            requested_request_id=request_id,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(
            {key: value for key, value in (query or {}).items() if value is not None}
        )
        url = f"{self.origin}{path}" + (f"?{query_string}" if query_string else "")
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=float(timeout)) as response:
                response_body = response.read(max_response_bytes + 1)
                version = _response_header(response, "X-PM-API-Version")
                response_status = _response_status(response)
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read(max_response_bytes + 1)
            except Exception:
                error_body = b""
            if len(error_body) > max_response_bytes:
                raise PortfolioManagementProtocolError(
                    f"portfolio-management response exceeds {max_response_bytes // (1024 * 1024)} MiB"
                ) from exc
            if path.startswith("/api/v1/"):
                version = _response_header(exc, "X-PM-API-Version")
                if version != API_VERSION:
                    raise PortfolioManagementProtocolError(
                        f"portfolio-management API version mismatch: {version or 'missing'}"
                    ) from exc
                decoded = _decode_public_error(error_body)
            else:
                decoded = _decode_optional_object(error_body)
            raise PortfolioManagementHTTPError(
                str(
                    decoded.get("message")
                    or decoded.get("error")
                    or decoded.get("detail")
                    or f"portfolio-management HTTP {exc.code}"
                ),
                status=exc.code,
                error_code=decoded.get("error_code"),
                details=decoded.get("details") if isinstance(decoded.get("details"), dict) else None,
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise PortfolioManagementTransportError(
                f"portfolio-management request failed: {exc}"
            ) from exc
        if len(response_body) > max_response_bytes:
            raise PortfolioManagementProtocolError(
                f"portfolio-management response exceeds {max_response_bytes // (1024 * 1024)} MiB"
            )
        if response_status != int(expected_status):
            raise PortfolioManagementProtocolError(
                "portfolio-management success status mismatch: "
                f"expected {expected_status}, got {response_status}"
            )
        if path.startswith("/api/v1/") and version != API_VERSION:
            raise PortfolioManagementProtocolError(
                f"portfolio-management API version mismatch: {version or 'missing'}"
            )
        decoded = _decode_object(response_body)
        if path.startswith("/api/v1/") and decoded.get("success") is not True:
            raise PortfolioManagementProtocolError(
                "portfolio-management success response did not confirm success=true"
            )
        if decoded.get("success") is False:
            raise PortfolioManagementHTTPError(
                str(decoded.get("message") or decoded.get("error") or "portfolio-management request failed"),
                status=503,
                error_code=decoded.get("error_code"),
                details=decoded.get("details") if isinstance(decoded.get("details"), dict) else None,
            )
        return decoded
