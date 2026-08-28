from __future__ import annotations

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
from src.application.portfolio_assignment_scenario import (
    AssignmentScenarioInputError,
    query_portfolio_assignment_scenario,
)
from src.application.portfolio_pnl_bridge import build_portfolio_pnl_bridge
from src.application.portfolio_management import (
    PORTFOLIO_MANAGEMENT_DISABLED,
    portfolio_management_failure_code,
    resolve_portfolio_management_client,
)
from src.application.performance.service import build_option_period_performance
from src.infrastructure.portfolio_management_client import (
    SERVICE_URL_ENV,
    PortfolioManagementClient,
    PortfolioManagementError,
    PortfolioManagementHTTPError,
)


_ACCOUNT_REQUIRED_VIEWS = frozenset({"holdings", "cash", "nav", "full_report"})
_FORBIDDEN_ENDPOINT_FIELDS = frozenset({"base_url", "endpoint", "service_url", "url"})
_PM_FRESHNESS_STATUSES = frozenset({"fresh", "stale", "unknown", "unavailable"})
_PM_TRUST_STATUSES = frozenset({"trusted", "partial", "untrusted", "unavailable"})


def _portfolio_client() -> PortfolioManagementClient:
    try:
        _config_path, config = load_runtime_config(config_key="us")
        client = resolve_portfolio_management_client(
            config,
            urlopen_fn=urllib.request.urlopen,
        )
        if client == PORTFOLIO_MANAGEMENT_DISABLED:
            raise AgentToolError(
                code=PORTFOLIO_MANAGEMENT_DISABLED,
                message="portfolio-management integration is disabled",
            )
        return client
    except ValueError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc


def _map_client_error(exc: PortfolioManagementError) -> AgentToolError:
    details = {"status": exc.status} if isinstance(exc, PortfolioManagementHTTPError) else {}
    return AgentToolError(
        code=portfolio_management_failure_code(exc),
        message=str(exc),
        details=details,
    )


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


def _portfolio_query(payload: dict[str, Any]):
    view = str(payload.get("view") or "health").strip()
    query, scope = _request_scope(view, payload)
    price_timeout = int(payload.get("price_timeout") or 30)
    try:
        response = _portfolio_client().read_view(
            view,
            query=query,
            timeout=float(min(max(price_timeout + 5, 10), 120)),
        )
    except PortfolioManagementError as exc:
        raise _map_client_error(exc) from exc
    data = dict(response)
    data["source"] = {
        "service": "portfolio-management",
        "transport": "loopback_http",
    }
    data["scope"] = scope
    if view == "health":
        data["freshness"] = {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "dataset_ids": ["pm.runtime"],
            "reason_codes": [],
        }
        return data, [], {}

    freshness, warning = _validate_pm_freshness(response.get("freshness"))
    data["freshness"] = freshness
    if view == "holdings" and bool(payload.get("group_by_market", False)):
        by_market = data.get("by_market")
        groups = by_market if isinstance(by_market, dict) else {}
        included_count = sum(
            len(rows)
            for rows in groups.values()
            if isinstance(rows, list)
        )
        declared_count = data.get("count")
        total_count = (
            declared_count
            if isinstance(declared_count, int) and not isinstance(declared_count, bool)
            else included_count
        )
        complete = included_count == total_count
        data["coverage"] = {
            "status": "complete" if complete else "partial",
            "complete_for": "full_query" if complete else "requested_page",
            "included_count": included_count,
            "total_count": total_count,
            "omitted_count": max(0, total_count - included_count),
            "has_more": not complete,
        }
    return data, ([warning] if warning else []), {}


def _validate_pm_freshness(value: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(value, dict):
        return _unavailable_pm_freshness(), "PM freshness evidence is missing; data is unavailable"
    status = str(value.get("status") or "")
    trust_status = str(value.get("trust_status") or "")
    dataset_ids = value.get("dataset_ids")
    reason_codes = value.get("reason_codes")
    if (
        status not in _PM_FRESHNESS_STATUSES
        or trust_status not in _PM_TRUST_STATUSES
        or not isinstance(dataset_ids, list)
        or not dataset_ids
        or not isinstance(reason_codes, list)
    ):
        return _unavailable_pm_freshness(), "PM freshness evidence is invalid; data is unavailable"
    normalized = dict(value)
    if status == "fresh" and trust_status != "trusted":
        normalized["status"] = "unknown"
        return normalized, "PM freshness evidence is not trusted; current data is unavailable"
    return normalized, None


def _unavailable_pm_freshness() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "trust_status": "unavailable",
        "observed_at_utc": None,
        "dataset_ids": [],
        "reason_codes": ["PM_FRESHNESS_EVIDENCE_MISSING"],
    }


_PORTFOLIO_COLLECTION_VIEWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "accounts": ("accounts", ("success", "accounts", "count", "retrieved_at_utc")),
    "overview": (
        "items",
        (
            "success",
            "status",
            "accounts",
            "account_count",
            "successful_count",
            "failed_count",
            "summary",
            "items",
            "retrieved_at_utc",
        ),
    ),
    "cash": ("items", ("success", "by_currency", "items", "count", "retrieved_at_utc")),
    "nav": ("history", ("success", "latest", "history", "retrieved_at_utc")),
}


def _portfolio_query_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    view = str(payload.get("view") or "health").strip()
    base: dict[str, Any] = {
        "schema_version": "portfolio_query.output.v1",
        "bounded_projection": "contract_fields",
        "freshness": "source_declared",
        "pagination": {"mode": "none"},
        "source_label": "portfolio-management loopback API",
        "freshness_fields": [
            "freshness.status",
            "freshness.trust_status",
            "freshness.observed_at_utc",
            "freshness.dataset_ids",
            "freshness.reason_codes",
        ],
    }
    collection = _PORTFOLIO_COLLECTION_VIEWS.get(view)
    if view == "holdings" and not bool(payload.get("group_by_market", False)):
        collection = (
            "holdings",
            ("success", "count", "holdings", "retrieved_at_utc"),
        )
    elif view == "holdings":
        return {
            **base,
            "evidence_type": "collection",
            "coverage": "source_declared",
            "fact_fields": ["success", "count", "by_market", "retrieved_at_utc"],
            "model_value_fields": ["success", "count", "by_market", "retrieved_at_utc"],
        }
    elif view == "distribution":
        primary_rows = "by_asset" if bool(payload.get("by_asset", False)) else "by_type"
        collection = (
            primary_rows,
            (
                "success",
                "total_value",
                "total_quantity",
                "accounts",
                primary_rows,
                "retrieved_at_utc",
            ),
        )
    if collection is not None:
        primary_rows, model_fields = collection
        return {
            **base,
            "evidence_type": "collection",
            "coverage": "primary_rows",
            "primary_rows": primary_rows,
            "fact_fields": list(model_fields),
            "model_value_fields": list(model_fields),
        }
    point_fields: tuple[str, ...] = ()
    if view == "full_report":
        point_fields = (
            "success",
            "generated_at",
            "overview",
            "nav",
            "returns",
            "top_holdings",
            "distribution",
            "retrieved_at_utc",
        )
    return {
        **base,
        "evidence_type": "point",
        "coverage": "point",
        **(
            {
                "fact_fields": list(point_fields),
                "model_value_fields": list(point_fields),
            }
            if point_fields
            else {}
        ),
    }


def _read_capital_facts(*, account: str, period: str, as_of_month: str) -> dict[str, Any]:
    try:
        return _portfolio_client().read_capital_facts(
            account=account,
            period=period,
            as_of_month=as_of_month,
        )
    except PortfolioManagementError as exc:
        raise _map_client_error(exc) from exc



def _read_cash_facts(*, account: str, period: str, as_of_month: str) -> dict[str, Any]:
    del account, period, as_of_month
    raise AgentToolError(
        code="CAPABILITY_UNAVAILABLE",
        message="portfolio cash facts are not onboarded",
        details={"capability": "portfolio_cash_facts", "endpoint": None},
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
            unavailable_reason="portfolio_cash_facts_not_onboarded",
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


def _validate_assignment_scenario_input(payload: dict[str, Any]) -> None:
    unsupported = sorted(set(payload).difference({"accounts"}))
    if unsupported:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=(
                "portfolio_assignment_scenario accepts only accounts; "
                f"unsupported fields: {', '.join(unsupported)}"
            ),
        )


def _portfolio_assignment_scenario(payload: dict[str, Any]):
    try:
        result = query_portfolio_assignment_scenario(payload.get("accounts") or [])
    except AssignmentScenarioInputError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    return result, list(result.get("warnings") or []), {}


PORTFOLIO_QUERY_TOOL = build_agent_tool(
    name="portfolio_query",
    catalog_summary="通过权威接口读取组合账户与资产数据。",
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
        "schema_version": "portfolio_query.output.v1",
        "evidence_type": "mixed",
        "bounded_projection": "contract_fields",
        "coverage": "unknown",
        "freshness": "source_declared",
        "pagination": {"mode": "none"},
        "freshness_fields": [
            "freshness.status",
            "freshness.trust_status",
            "freshness.observed_at_utc",
            "freshness.dataset_ids",
            "freshness.reason_codes",
        ],
    },
    output_contract_resolver=_portfolio_query_output_contract,
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

def _bridge_tool(*, name: str, description: str, handler, schema_version: str, evidence_field: str, catalog_summary: str) -> AgentTool:
    return build_agent_tool(
        name=name,
        catalog_summary=catalog_summary,
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
            "evidence_type": "collection",
            "bounded_projection": "contract_fields",
            "coverage": "primary_rows",
            "freshness": "source_declared",
            "pagination": {"mode": "none"},
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
    catalog_summary="读取组合期间损益桥接及期权分解。",
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
    catalog_summary="读取组合现金余额桥接及期权现金变动。",
)

PORTFOLIO_ASSIGNMENT_SCENARIO_TOOL = build_agent_tool(
    name="portfolio_assignment_scenario",
    catalog_summary="读取组合归属情景与现金影响。",
    description=(
        "Project the current CNY asset distribution and funding coverage if every open short put and "
        "short call in the selected accounts were physically assigned. MMF is cash; long options are "
        "excluded; the operation is read-only and does not write assignment events."
    ),
    requires=(
        "portfolio-management valuation evidence loopback API",
        "runtime_config",
        "sqlite_position_lots",
    ),
    capabilities=(
        "portfolio_read",
        "assignment_stress_scenario",
        "cross_product_read",
        "read_only",
    ),
    input_schema={
        "accounts": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
            },
            "minItems": 1,
            "maxItems": 20,
            "required": True,
            "description": "Required OM account labels; normalized to lowercase and de-duplicated.",
        },
    },
    handler=_portfolio_assignment_scenario,
    pure_read=True,
    safe_default_input={},
    input_validator=_validate_assignment_scenario_input,
    examples=({"input": {"accounts": ["lx", "sy"]}},),
    output_contract={
        "schema_version": "portfolio.assignment_scenario.v1",
        "evidence_type": "collection",
        "bounded_projection": "contract_fields",
        "coverage": "primary_rows",
        "freshness": "source_declared",
        "pagination": {"mode": "none"},
        "source_label": "portfolio-management valuation evidence + OM SQLite position_lots",
        "primary_rows": "assignments",
        "fact_fields": [
            "status",
            "scope",
            "snapshot",
            "summary",
            "cash_coverage",
            "fee_summary",
            "assignments",
            "position_changes",
            "expiration_ladder",
            "distribution",
            "account_breakdown",
            "fx_facts",
            "warnings",
        ],
        "missing_data_fields": [
            "cash_coverage.ending_cash_net_estimated_cny",
            "distribution.net_assets_cny",
        ],
        "notes": [
            "Business status complete|partial|unavailable is independent of the tool envelope ok flag.",
            "Long options are excluded and are neither valued nor retained in this report.",
        ],
    },
    copilot_input_fields=("accounts",),
)

TOOLS: tuple[AgentTool, ...] = (
    PORTFOLIO_QUERY_TOOL,
    PORTFOLIO_PNL_BRIDGE_TOOL,
    PORTFOLIO_CASH_BRIDGE_TOOL,
    PORTFOLIO_ASSIGNMENT_SCENARIO_TOOL,
)

__all__ = [
    "PORTFOLIO_ASSIGNMENT_SCENARIO_TOOL",
    "PORTFOLIO_CASH_BRIDGE_TOOL",
    "PORTFOLIO_PNL_BRIDGE_TOOL",
    "PORTFOLIO_QUERY_TOOL",
    "TOOLS",
]
