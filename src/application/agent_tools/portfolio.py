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

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tools.materialization_impl import option_performance_report_tool
from src.application.agent_tools.runtime_helpers import normalize_broker, resolve_public_data_config_path
from src.application.ledger.api import (
    open_performance_evidence_repository,
    open_position_ledger_from_data_config as resolve_option_positions_repo,
)
from src.application.portfolio_cash_bridge import build_portfolio_cash_bridge
from src.application.portfolio_pnl_bridge import build_portfolio_pnl_bridge
from src.application.performance.service import build_option_period_performance


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


def _read_capital_facts(*, account: str, period: str, as_of_month: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"account": account, "period": period, "as_of_month": as_of_month}
    )
    return _read_json(
        f"{_loopback_service_url()}/analysis/capital-facts?{query}",
        timeout=30.0,
    )



def _read_cash_facts(*, account: str, period: str, as_of_month: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"account": account, "period": period, "as_of_month": as_of_month}
    )
    return _read_json(
        f"{_loopback_service_url()}/analysis/cash-facts?{query}",
        timeout=30.0,
    )


def _read_bridge_fact(
    reader,
    *,
    account: str,
    period: str,
    as_of_month: str,
    unavailable_reason: str,
) -> dict[str, Any]:
    try:
        return reader(account=account, period=period, as_of_month=as_of_month)
    except AgentToolError as exc:
        return {
            "success": False,
            "status": "unavailable",
            "reason": unavailable_reason,
            "error": {"code": exc.code, "message": exc.message, "details": exc.details or {}},
            "period": {"kind": period, "requested_as_of_month": as_of_month},
        }


def _read_option_performance(
    *,
    account: str,
    period: str,
    end_date: str,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return option_performance_report_tool(
        {
            "config_key": "us",
            "period": period,
            "as_of_date": end_date,
            "account": account,
            "include_rows": False,
            "refresh_quotes": False,
        },
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker,
        resolve_option_positions_repo=resolve_option_positions_repo,
        open_performance_evidence_repository=open_performance_evidence_repository,
        build_option_period_performance=build_option_period_performance,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _bridge_option_reports(
    *,
    accounts: list[str],
    period: str,
    facts_by_account: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    meta: dict[str, Any] = {}
    for account in accounts:
        facts = facts_by_account.get(account) or {}
        end_date = str((facts.get("period") or {}).get("end_date") or "")
        if str(facts.get("status") or "") != "ok" or not end_date:
            continue
        try:
            report, report_warnings, report_meta = _read_option_performance(
                account=account, period=period, end_date=end_date
            )
        except AgentToolError as exc:
            reports[account] = {
                "quality": {
                    "status": "not_observed",
                    "missing": [f"option_performance_report: {exc.message}"],
                }
            }
            warnings.append(f"{account}: {exc.message}")
            continue
        reports[account] = report
        warnings.extend(f"{account}: {item}" for item in report_warnings if str(item).strip())
        if not meta:
            meta = dict(report_meta)
    return reports, warnings, meta


def _portfolio_pnl_bridge(payload: dict[str, Any]):
    period, as_of_month, accounts = _bridge_scope(payload)
    facts = {
        account: _read_bridge_fact(
            _read_capital_facts,
            account=account,
            period=period,
            as_of_month=as_of_month,
            unavailable_reason="portfolio_capital_facts_unavailable",
        )
        for account in accounts
    }
    reports, warnings, meta = _bridge_option_reports(
        accounts=accounts, period=period, facts_by_account=facts
    )
    return (
        build_portfolio_pnl_bridge(
            period=period,
            as_of_month=as_of_month,
            accounts=accounts,
            capital_facts_by_account=facts,
            option_reports_by_account=reports,
            observed_at=datetime.now(timezone.utc).isoformat(),
        ),
        list(dict.fromkeys(warnings)),
        meta,
    )


def _portfolio_cash_bridge(payload: dict[str, Any]):
    period, as_of_month, accounts = _bridge_scope(payload)
    facts = {
        account: _read_bridge_fact(
            _read_cash_facts,
            account=account,
            period=period,
            as_of_month=as_of_month,
            unavailable_reason="portfolio_cash_facts_unavailable",
        )
        for account in accounts
    }
    reports, warnings, meta = _bridge_option_reports(
        accounts=accounts, period=period, facts_by_account=facts
    )
    return (
        build_portfolio_cash_bridge(
            period=period,
            as_of_month=as_of_month,
            accounts=accounts,
            cash_facts_by_account=facts,
            option_reports_by_account=reports,
            observed_at=datetime.now(timezone.utc).isoformat(),
        ),
        list(dict.fromkeys(warnings)),
        meta,
    )


def _bridge_scope(payload: dict[str, Any]) -> tuple[str, str, list[str]]:
    return (
        str(payload.get("period") or "").strip().lower(),
        str(payload.get("as_of_month") or "").strip(),
        list(dict.fromkeys(str(item).strip() for item in payload.get("accounts") or [] if str(item).strip())),
    )

def _validate_bridge_input(payload: dict[str, Any]) -> None:
    supplied_endpoint_fields = sorted(_FORBIDDEN_ENDPOINT_FIELDS.intersection(payload))
    if supplied_endpoint_fields:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"portfolio bridge endpoint fields are not accepted: {', '.join(supplied_endpoint_fields)}",
        )
    accounts = [str(item).strip() for item in payload.get("accounts") or []]
    if not accounts or any(not item for item in accounts):
        raise AgentToolError(code="INPUT_ERROR", message="portfolio bridge accounts must be non-empty")


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

def _bridge_tool(*, name: str, description: str, handler, schema_version: str, evidence_field: str) -> AgentTool:
    return build_agent_tool(
        name=name,
        description=description,
        requires=("portfolio-management loopback HTTP API", "runtime_config", "sqlite_data_config"),
        capabilities=("portfolio_read", name, "cross_product_read", "read_only"),
        input_schema={
            "period": {"type": "string", "enum": ["mtd", "ytd"], "required": True},
            "as_of_month": {
                "type": "string",
                "pattern": r"^\d{4}-(0[1-9]|1[0-2])$",
                "required": True,
            },
            "accounts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 20,
                "required": True,
            },
        },
        handler=handler,
        pure_read=True,
        safe_default_input={},
        input_validator=_validate_bridge_input,
        examples=({"input": {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx", "sy"]}},),
        output_contract={
            "schema_version": schema_version,
            "source_label": "portfolio-management facts + option_performance_report",
            "primary_rows": "accounts",
            "fact_fields": [
                "status",
                "period.kind",
                "accounts[].account",
                "accounts[].status",
                "accounts[].steps",
                evidence_field,
                "accounts[].reconciliation",
                "combined",
                "fallback_text",
            ],
            "missing_data_fields": ["accounts[].status", evidence_field, "combined.status"],
        },
        copilot_input_fields=("period", "as_of_month", "accounts"),
    )


PORTFOLIO_PNL_BRIDGE_TOOL = _bridge_tool(
    name="portfolio_pnl_bridge",
    description=(
        "Build an MTD or YTD total-assets PnL bridge. It decomposes portfolio period PnL into option "
        "period-total net PnL and portfolio/other PnL. Assignment principal never enters the PnL equation."
    ),
    handler=_portfolio_pnl_bridge,
    schema_version="portfolio.pnl_bridge.v1",
    evidence_field="accounts[].option_pnl_evidence",
)

PORTFOLIO_CASH_BRIDGE_TOOL = _bridge_tool(
    name="portfolio_cash_bridge",
    description=(
        "Build an MTD or YTD cash-balance bridge from portfolio-management cash facts and complete OM option "
        "cash movement. It never substitutes total assets for cash and missing cash facts remain unavailable."
    ),
    handler=_portfolio_cash_bridge,
    schema_version="portfolio.cash_bridge.v1",
    evidence_field="accounts[].option_cash_evidence",
)

TOOLS: tuple[AgentTool, ...] = (
    PORTFOLIO_QUERY_TOOL,
    PORTFOLIO_PNL_BRIDGE_TOOL,
    PORTFOLIO_CASH_BRIDGE_TOOL,
)

__all__ = [
    "PORTFOLIO_CASH_BRIDGE_TOOL",
    "PORTFOLIO_PNL_BRIDGE_TOOL",
    "PORTFOLIO_QUERY_TOOL",
    "TOOLS",
]
