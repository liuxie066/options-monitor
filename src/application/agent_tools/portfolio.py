from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, build_agent_tool


DEFAULT_SERVICE_URL = "http://127.0.0.1:8765"
SERVICE_URL_ENV = "PORTFOLIO_SERVICE_URL"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_VIEW_PATHS = {
    "health": "/health",
    "accounts": "/accounts",
    "overview": "/accounts/overview",
    "holdings": "/holdings",
    "cash": "/cash",
    "nav": "/nav",
    "distribution": "/distribution",
    "full_report": "/report/full",
}
_ACCOUNT_REQUIRED_VIEWS = frozenset({"holdings", "cash", "nav", "full_report"})
_FORBIDDEN_ENDPOINT_FIELDS = frozenset({"base_url", "endpoint", "service_url", "url"})


def _loopback_service_url() -> str:
    raw = str(os.environ.get(SERVICE_URL_ENV) or DEFAULT_SERVICE_URL).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"invalid {SERVICE_URL_ENV}: {exc}") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{SERVICE_URL_ENV} must be an http(s) loopback URL",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{SERVICE_URL_ENV} must contain only a loopback origin",
        )

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname != "localhost":
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{SERVICE_URL_ENV} must use localhost or a loopback IP address",
            )

    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, authority, "", "", ""))


def _bool_query(payload: dict[str, Any], name: str, query: dict[str, str]) -> None:
    if name in payload:
        query[name] = "true" if bool(payload[name]) else "false"


def _request_scope(view: str, payload: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    query: dict[str, str] = {}
    scope: dict[str, Any] = {"view": view}

    if view == "accounts":
        _bool_query(payload, "include_default", query)
    elif view == "overview":
        accounts = [str(item).strip() for item in payload.get("accounts") or [] if str(item).strip()]
        if accounts:
            query["accounts"] = ",".join(accounts)
            scope["accounts"] = accounts
        if "price_timeout" in payload:
            query["price_timeout"] = str(payload["price_timeout"])
        _bool_query(payload, "include_details", query)
    elif view == "holdings":
        query["account"] = str(payload["account"]).strip()
        scope["account"] = query["account"]
        for name in ("include_cash", "group_by_market", "include_price"):
            _bool_query(payload, name, query)
    elif view == "cash":
        query["account"] = str(payload["account"]).strip()
        scope["account"] = query["account"]
    elif view == "nav":
        query["account"] = str(payload["account"]).strip()
        scope["account"] = query["account"]
        if "days" in payload:
            query["days"] = str(payload["days"])
    elif view == "distribution":
        account = str(payload.get("account") or "").strip()
        accounts = [str(item).strip() for item in payload.get("accounts") or [] if str(item).strip()]
        if accounts:
            query["accounts"] = ",".join(accounts)
            scope["accounts"] = accounts
        elif account:
            query["account"] = account
            scope["account"] = account
        for name in ("by_asset", "include_value", "group_cash"):
            _bool_query(payload, name, query)
    elif view == "full_report":
        query["account"] = str(payload["account"]).strip()
        scope["account"] = query["account"]
        if "price_timeout" in payload:
            query["price_timeout"] = str(payload["price_timeout"])

    return query, scope


def _validate_input(payload: dict[str, Any]) -> None:
    supplied_endpoint_fields = sorted(_FORBIDDEN_ENDPOINT_FIELDS.intersection(payload))
    if supplied_endpoint_fields:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"portfolio_query endpoint fields are not accepted: {', '.join(supplied_endpoint_fields)}",
        )
    view = str(payload.get("view") or "health").strip()
    if view in _ACCOUNT_REQUIRED_VIEWS and not str(payload.get("account") or "").strip():
        raise AgentToolError(code="INPUT_ERROR", message=f"portfolio_query account is required for view={view}")


def _read_json(url: str, *, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AgentToolError(
            code="READ_ERROR",
            message=f"portfolio-management HTTP {exc.code}",
            details={"status": exc.code},
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise AgentToolError(code="READ_ERROR", message=f"portfolio-management request failed: {exc}") from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise AgentToolError(code="READ_ERROR", message="portfolio-management response exceeds 8 MiB")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentToolError(code="READ_ERROR", message="portfolio-management returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise AgentToolError(code="READ_ERROR", message="portfolio-management JSON response must be an object")
    if decoded.get("success") is False:
        message = str(decoded.get("error") or decoded.get("message") or "portfolio-management read failed")
        raise AgentToolError(code="READ_ERROR", message=message)
    return decoded


def _portfolio_query(payload: dict[str, Any]):
    view = str(payload.get("view") or "health").strip()
    base_url = _loopback_service_url()
    query, scope = _request_scope(view, payload)
    url = f"{base_url}{_VIEW_PATHS[view]}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    price_timeout = int(payload.get("price_timeout") or 30)
    response = _read_json(url, timeout=float(min(max(price_timeout + 5, 10), 120)))
    data = dict(response)
    data["source"] = {
        "service": "portfolio-management",
        "transport": "loopback_http",
    }
    data["scope"] = scope
    data["freshness"] = {
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    return data, [], {}


PORTFOLIO_QUERY_TOOL = build_agent_tool(
    name="portfolio_query",
    description="Read portfolio-management account, holdings, cash, NAV, distribution, or report data through its same-host loopback HTTP API.",
    requires=("portfolio-management loopback HTTP API",),
    capabilities=("portfolio_read", "cross_product_read", "read_only"),
    input_schema={
        "view": "optional health|accounts|overview|holdings|cash|nav|distribution|full_report",
        "account": "optional portfolio account; required for holdings, cash, nav, and full_report",
        "accounts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": "Optional portfolio accounts for overview or distribution",
        },
        "include_default": "optional bool for accounts",
        "price_timeout": {"type": "integer", "minimum": 1, "maximum": 300},
        "include_details": "optional bool for overview",
        "include_cash": "optional bool for holdings",
        "group_by_market": "optional bool for holdings",
        "include_price": "optional bool for holdings",
        "days": {"type": "integer", "minimum": 1, "maximum": 10000},
        "by_asset": "optional bool for distribution",
        "include_value": "optional bool for distribution",
        "group_cash": "optional bool for distribution",
    },
    handler=_portfolio_query,
    pure_read=True,
    safe_default_input={"view": "health"},
    input_validator=_validate_input,
    examples=(
        {"input": {"view": "health"}},
        {"input": {"view": "overview", "accounts": ["lx", "sy"]}},
        {"input": {"view": "holdings", "account": "lx", "include_price": True}},
    ),
    output_contract={
        "fact_fields": ["success", "source", "scope", "freshness"],
        "freshness_fields": ["freshness.status", "freshness.observed_at"],
        "notes": ["Portfolio payload fields vary by selected view and are preserved at the top level."],
    },
    copilot_input_fields=(
        "view",
        "account",
        "accounts",
        "include_default",
        "price_timeout",
        "include_details",
        "include_cash",
        "group_by_market",
        "include_price",
        "days",
        "by_asset",
        "include_value",
        "group_cash",
    ),
)

TOOLS: tuple[AgentTool, ...] = (PORTFOLIO_QUERY_TOOL,)

__all__ = ["PORTFOLIO_QUERY_TOOL", "TOOLS"]
