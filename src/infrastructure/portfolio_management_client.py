"""Single loopback HTTP adapter for portfolio-management."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

DEFAULT_SERVICE_URL = "http://127.0.0.1:8765"
SERVICE_URL_ENV = "PORTFOLIO_SERVICE_URL"
API_VERSION = "portfolio.api.v1"
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
VALUATION_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_VIEW_PATHS = {
    "health": "/health",
    "accounts": "/api/v1/accounts",
    "overview": "/api/v1/accounts/overview",
    "holdings": "/api/v1/holdings",
    "cash": "/api/v1/cash",
    "nav": "/api/v1/nav",
    "distribution": "/api/v1/distribution",
    "full_report": "/api/v1/report/full",
}
CAPITAL_FACTS_PATH = "/api/v1/analysis/capital-facts"
VALUATION_EVIDENCE_PATH = "/api/v1/analysis/valuation-evidence"
HOLDINGS_SYNC_PATH = "/api/v1/futu/holdings/sync"
CONTRACT_OPERATIONS = frozenset(
    {
        *{("GET", path) for path in _VIEW_PATHS.values() if path.startswith("/api/v1/")},
        ("GET", CAPITAL_FACTS_PATH),
        ("POST", VALUATION_EVIDENCE_PATH),
        ("POST", HOLDINGS_SYNC_PATH),
    }
)


class PortfolioManagementError(RuntimeError):
    """Base class for PM adapter failures."""


class PortfolioManagementConfigError(PortfolioManagementError):
    """The PM endpoint violates the local-only configuration contract."""


class PortfolioManagementTransportError(PortfolioManagementError):
    """The loopback transport failed before a valid HTTP response."""


class PortfolioManagementProtocolError(PortfolioManagementError):
    """PM returned an invalid or incompatible response."""


class PortfolioManagementHTTPError(PortfolioManagementError):
    """PM returned an HTTP or application error."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        error_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.error_code = str(error_code or "").strip().upper() or None
        self.details = dict(details or {})


def resolve_portfolio_service_origin(value: str | None = None) -> str:
    raw = str(value or os.environ.get(SERVICE_URL_ENV) or DEFAULT_SERVICE_URL).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise PortfolioManagementConfigError(f"invalid {SERVICE_URL_ENV}: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PortfolioManagementConfigError(
            f"{SERVICE_URL_ENV} must be an http(s) loopback URL"
        )
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise PortfolioManagementConfigError(
            f"{SERVICE_URL_ENV} must contain only a loopback origin"
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname != "localhost":
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise PortfolioManagementConfigError(
                f"{SERVICE_URL_ENV} must use localhost or a loopback IP address"
            )
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, authority, "", "", ""))


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
        return self._request("GET", path, query=query, timeout=timeout)

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
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            VALUATION_EVIDENCE_PATH,
            payload={
                "accounts": accounts,
                "supplemental_codes": supplemental_codes,
                "price_timeout": int(price_timeout),
            },
            timeout=float(min(max(int(price_timeout) + 10, 15), 180)),
            max_response_bytes=VALUATION_MAX_RESPONSE_BYTES,
        )

    def sync_holdings(
        self,
        *,
        account: str,
        timeout: float,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            HOLDINGS_SYNC_PATH,
            payload={
                "account": account,
                "dry_run": False,
                "confirm": True,
                "allow_empty_stock_snapshot": False,
            },
            timeout=timeout,
        )
        if result.get("success") is not True:
            raise PortfolioManagementProtocolError(
                "portfolio holdings sync did not confirm success=true"
            )
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
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
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read(max_response_bytes + 1)
            except Exception:
                error_body = b""
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
        if path.startswith("/api/v1/") and version != API_VERSION:
            raise PortfolioManagementProtocolError(
                f"portfolio-management API version mismatch: {version or 'missing'}"
            )
        decoded = _decode_object(response_body)
        if decoded.get("success") is False:
            raise PortfolioManagementHTTPError(
                str(decoded.get("message") or decoded.get("error") or "portfolio-management request failed"),
                status=503,
                error_code=decoded.get("error_code"),
                details=decoded.get("details") if isinstance(decoded.get("details"), dict) else None,
            )
        return decoded


def _response_header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        return str(headers.get(name) or "")
    getter = getattr(response, "getheader", None)
    return str(getter(name) or "") if callable(getter) else ""


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioManagementProtocolError(
            "portfolio-management returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PortfolioManagementProtocolError(
            "portfolio-management JSON response must be an object"
        )
    return value


def _decode_optional_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
