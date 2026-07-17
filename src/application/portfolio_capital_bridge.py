from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "portfolio.capital_bridge.v1"
_MONEY = Decimal("0.01")
_TOLERANCE = Decimal("0.05")


def beijing_end_of_day_ms(value: str) -> int:
    day = date.fromisoformat(str(value))
    cutoff = datetime.combine(day, time(23, 59, 59, 999000), tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(cutoff.timestamp() * 1000)


def build_portfolio_capital_bridge(
    *,
    period: str,
    as_of_month: str,
    accounts: list[str],
    capital_facts_by_account: dict[str, dict[str, Any]],
    option_reports_by_end_date: dict[str, dict[str, Any]],
    option_scope_accounts: set[str],
    observed_at: str | None = None,
) -> dict[str, Any]:
    period_value = str(period).strip().lower()
    account_results = [
        _account_bridge(
            account=account,
            period=period_value,
            as_of_month=as_of_month,
            facts=capital_facts_by_account.get(account),
            report_by_end_date=option_reports_by_end_date,
            option_scope_accounts=option_scope_accounts,
        )
        for account in accounts
    ]
    combined = _combined_bridge(account_results, period=period_value, as_of_month=as_of_month)
    available_count = sum(1 for item in account_results if item.get("status") != "unavailable")
    if available_count == 0:
        status = "unavailable"
    elif all(item.get("status") == "ok" for item in account_results) and combined.get("status") == "ok":
        status = "ok"
    else:
        status = "partial"

    cutoffs = [
        {
            "end_date": end_date,
            "as_of_ms": beijing_end_of_day_ms(end_date),
            "timezone": "Asia/Shanghai",
        }
        for end_date in sorted(option_reports_by_end_date)
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "status": status,
        "period": {
            "kind": period_value,
            "as_of_month": as_of_month,
        },
        "accounts": account_results,
        "combined": combined,
        "coverage": _coverage(account_results, option_scope_accounts),
        "source": {
            "portfolio": {
                "service": "portfolio-management",
                "transport": "loopback_http",
                "endpoint": "/analysis/capital-facts",
            },
            "option_cash": {
                "service": "options-monitor",
                "source": "OM local option ledger",
                "field": "monthly_income_report.return_summary[].net_income_cny",
                "basis": "gross_before_fees_excluding_assignment_stock_principal",
                "shared_ledger_loads": 1,
            },
        },
        "freshness": {
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "portfolio": "live",
            "option_ledger": "snapshot",
            "option_cutoffs": cutoffs,
        },
    }
    result["fallback_text"] = _fallback_text(result)
    return result


def _account_bridge(
    *,
    account: str,
    period: str,
    as_of_month: str,
    facts: dict[str, Any] | None,
    report_by_end_date: dict[str, dict[str, Any]],
    option_scope_accounts: set[str],
) -> dict[str, Any]:
    if not isinstance(facts, dict) or str(facts.get("status") or "") != "ok":
        return {
            "account": account,
            "status": "unavailable",
            "reason": str((facts or {}).get("reason") or "portfolio_capital_facts_unavailable"),
            "period": dict((facts or {}).get("period") or {"kind": period, "requested_as_of_month": as_of_month}),
            "steps": [],
        }

    fact_period = dict(facts.get("period") or {})
    end_date = str(fact_period.get("end_date") or "")
    amounts = facts.get("amounts") if isinstance(facts.get("amounts"), dict) else {}
    opening = _money(amounts.get("opening_assets"))
    external = _money(amounts.get("external_cash_flow"))
    period_pnl = _money(amounts.get("period_pnl"))
    ending = _money(amounts.get("ending_assets"))
    option = _option_cash_evidence(
        account=account,
        period=period,
        as_of_month=as_of_month,
        report=report_by_end_date.get(end_date),
        option_scope_accounts=option_scope_accounts,
    )
    option_amount = _money_or_none(option.get("amount"))
    residual = _money(period_pnl - option_amount) if option_amount is not None else None
    status = "ok" if residual is not None else "partial"

    return {
        "account": account,
        "status": status,
        "currency": "CNY",
        "period": fact_period,
        "steps": _steps(
            opening=opening,
            external=external,
            option=option_amount,
            option_status=str(option.get("status") or "not_observed"),
            residual=residual,
            ending=ending,
        ),
        "calculation": {
            "period_pnl": _float(period_pnl),
            "portfolio_and_other_change": _float_or_none(residual),
            "formula": "ending_assets - opening_assets - external_cash_flow - option_cash_change",
        },
        "reconciliation": _reconciliation(opening, external, option_amount, residual, ending),
        "portfolio_reconciliation": dict(facts.get("reconciliation") or {}),
        "option_cash_evidence": option,
    }


def _option_cash_evidence(
    *,
    account: str,
    period: str,
    as_of_month: str,
    report: dict[str, Any] | None,
    option_scope_accounts: set[str],
) -> dict[str, Any]:
    if account not in option_scope_accounts:
        return {
            "amount": 0.0,
            "currency": "CNY",
            "status": "not_applicable",
            "reason": "account_outside_option_monitor_scope",
            "months": [],
        }

    months = set(_period_months(period, as_of_month))
    rows = [
        row
        for row in ((report or {}).get("return_summary") or [])
        if isinstance(row, dict)
        and str(row.get("account") or "").strip() == account
        and str(row.get("month") or "") in months
    ]
    if not rows:
        return {
            "amount": None,
            "currency": "CNY",
            "status": "not_observed",
            "reason": "option_cash_not_observed_for_period",
            "months": [],
        }
    if any(row.get("net_income_cny") is None for row in rows):
        return {
            "amount": None,
            "currency": "CNY",
            "status": "not_observed",
            "reason": "option_cash_cny_not_observed",
            "months": [str(row.get("month") or "") for row in rows],
        }
    amount = sum((_money(row.get("net_income_cny")) for row in rows), Decimal("0"))
    return {
        "amount": _float(_money(amount)),
        "currency": "CNY",
        "status": "observed",
        "reason": None,
        "months": [str(row.get("month") or "") for row in rows],
        "rows": [
            {"month": str(row.get("month") or ""), "net_income_cny": _float(_money(row.get("net_income_cny")))}
            for row in rows
        ],
    }


def _combined_bridge(account_results: list[dict[str, Any]], *, period: str, as_of_month: str) -> dict[str, Any]:
    if any(item.get("status") == "unavailable" for item in account_results):
        return {
            "status": "unavailable",
            "reason": "account_bridge_unavailable",
            "accounts": [str(item.get("account") or "") for item in account_results],
            "steps": [],
        }
    end_dates = {
        str((item.get("period") or {}).get("end_date") or "")
        for item in account_results
    }
    if len(end_dates) != 1:
        return {
            "status": "unavailable",
            "reason": "account_end_date_mismatch",
            "accounts": [str(item.get("account") or "") for item in account_results],
            "end_dates": {
                str(item.get("account") or ""): str((item.get("period") or {}).get("end_date") or "")
                for item in account_results
            },
            "steps": [],
        }

    opening = _sum_step(account_results, "opening_assets")
    external = _sum_step(account_results, "external_cash_flow")
    ending = _sum_step(account_results, "ending_assets")
    option_values = [_step_amount(item, "option_cash_change") for item in account_results]
    option = _money(sum(option_values, Decimal("0"))) if all(value is not None for value in option_values) else None
    residual = _money(ending - opening - external - option) if option is not None else None
    option_statuses = {
        str((item.get("option_cash_evidence") or {}).get("status") or "not_observed")
        for item in account_results
    }
    option_status = "not_observed" if option is None else ("not_applicable" if option_statuses == {"not_applicable"} else "observed")
    end_date = next(iter(end_dates))
    return {
        "status": "ok" if residual is not None else "partial",
        "currency": "CNY",
        "accounts": [str(item.get("account") or "") for item in account_results],
        "period": {
            "kind": period,
            "requested_as_of_month": as_of_month,
            "end_date": end_date,
            "timezone": "Asia/Shanghai",
        },
        "steps": _steps(
            opening=opening,
            external=external,
            option=option,
            option_status=option_status,
            residual=residual,
            ending=ending,
        ),
        "calculation": {
            "period_pnl": _float(_money(ending - opening - external)),
            "portfolio_and_other_change": _float_or_none(residual),
            "formula": "ending_assets - opening_assets - external_cash_flow - option_cash_change",
        },
        "reconciliation": _reconciliation(opening, external, option, residual, ending),
    }


def _steps(
    *,
    opening: Decimal,
    external: Decimal,
    option: Decimal | None,
    option_status: str,
    residual: Decimal | None,
    ending: Decimal,
) -> list[dict[str, Any]]:
    return [
        {"key": "opening_assets", "label": "Opening assets", "kind": "total", "amount": _float(opening), "status": "observed"},
        {"key": "external_cash_flow", "label": "External cash flow", "kind": "change", "amount": _float(external), "status": "observed"},
        {"key": "option_cash_change", "label": "Option cash change", "kind": "change", "amount": _float_or_none(option), "status": option_status},
        {
            "key": "portfolio_and_other_change",
            "label": "Portfolio and other change",
            "kind": "change",
            "amount": _float_or_none(residual),
            "status": "derived" if residual is not None else "not_observed",
        },
        {"key": "ending_assets", "label": "Ending assets", "kind": "total", "amount": _float(ending), "status": "observed"},
    ]


def _reconciliation(
    opening: Decimal,
    external: Decimal,
    option: Decimal | None,
    residual: Decimal | None,
    ending: Decimal,
) -> dict[str, Any]:
    if option is None or residual is None:
        return {"status": "not_observed", "difference": None, "tolerance": _float(_TOLERANCE)}
    difference = _money(opening + external + option + residual - ending)
    return {
        "status": "ok" if abs(difference) <= _TOLERANCE else "mismatch",
        "difference": _float(difference),
        "tolerance": _float(_TOLERANCE),
    }


def _coverage(account_results: list[dict[str, Any]], option_scope_accounts: set[str]) -> dict[str, Any]:
    evidence = {
        str(item.get("account") or ""): str((item.get("option_cash_evidence") or {}).get("status") or "unavailable")
        for item in account_results
    }
    requested = set(evidence)
    return {
        "requested_accounts": len(account_results),
        "portfolio_available_accounts": sum(1 for item in account_results if item.get("status") != "unavailable"),
        "option_scope_accounts": sorted(requested.intersection(option_scope_accounts)),
        "option_observed_accounts": sorted(account for account, status in evidence.items() if status == "observed"),
        "option_not_observed_accounts": sorted(account for account, status in evidence.items() if status == "not_observed"),
        "option_not_applicable_accounts": sorted(account for account, status in evidence.items() if status == "not_applicable"),
    }


def _period_months(period: str, as_of_month: str) -> list[str]:
    if period == "mtd":
        return [as_of_month]
    year_text, month_text = as_of_month.split("-", 1)
    return [f"{year_text}-{month:02d}" for month in range(1, int(month_text) + 1)]


def _sum_step(account_results: list[dict[str, Any]], key: str) -> Decimal:
    return _money(sum((_step_amount(item, key) or Decimal("0") for item in account_results), Decimal("0")))


def _step_amount(item: dict[str, Any], key: str) -> Decimal | None:
    for step in item.get("steps") or []:
        if isinstance(step, dict) and step.get("key") == key:
            return _money_or_none(step.get("amount"))
    return None


def _fallback_text(result: dict[str, Any]) -> str:
    period = str((result.get("period") or {}).get("kind") or "").upper()
    month = str((result.get("period") or {}).get("as_of_month") or "")
    lines = [f"### {period} 总资产变化桥（{month}）", "", "| 账户 | 期初总资产 | 出入金 | 期权现金变化 | 投资组合及其他变化 | 期末总资产 |", "|---|---:|---:|---:|---:|---:|"]
    for item in result.get("accounts") or []:
        steps = {str(step.get("key")): step.get("amount") for step in item.get("steps") or [] if isinstance(step, dict)}
        if item.get("status") == "unavailable":
            lines.append(f"| {item.get('account')} | — | — | — | — | — |")
            continue
        lines.append(
            "| {account} | {opening} | {external} | {option} | {residual} | {ending} |".format(
                account=item.get("account"),
                opening=_format_money(steps.get("opening_assets")),
                external=_format_money(steps.get("external_cash_flow")),
                option=_format_money(steps.get("option_cash_change")),
                residual=_format_money(steps.get("portfolio_and_other_change")),
                ending=_format_money(steps.get("ending_assets")),
            )
        )
    combined = result.get("combined") if isinstance(result.get("combined"), dict) else {}
    if combined.get("status") != "unavailable":
        steps = {str(step.get("key")): step.get("amount") for step in combined.get("steps") or [] if isinstance(step, dict)}
        lines.append(
            "| **合计** | **{opening}** | **{external}** | **{option}** | **{residual}** | **{ending}** |".format(
                opening=_format_money(steps.get("opening_assets")),
                external=_format_money(steps.get("external_cash_flow")),
                option=_format_money(steps.get("option_cash_change")),
                residual=_format_money(steps.get("portfolio_and_other_change")),
                ending=_format_money(steps.get("ending_assets")),
            )
        )
    elif combined.get("reason") == "account_end_date_mismatch":
        lines.extend(["", "> 合计不可用：账户实际期末净值日期不一致。"])
    missing = [
        str(item.get("account") or "")
        for item in result.get("accounts") or []
        if str((item.get("option_cash_evidence") or {}).get("status") or "") == "not_observed"
    ]
    if missing:
        lines.extend(["", f"> 未观察到期权现金证据：{', '.join(missing)}；未将缺失值按 0 处理。"])
    return "\n".join(lines)


def _format_money(value: Any) -> str:
    if value is None:
        return "—"
    return f"¥{float(value):,.2f}"


def _money(value: Any) -> Decimal:
    if value is None:
        raise ValueError("bridge amount is missing")
    return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _money_or_none(value: Any) -> Decimal | None:
    return None if value is None else _money(value)


def _float(value: Decimal) -> float:
    return float(value)


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else _float(value)


__all__ = ["SCHEMA_VERSION", "beijing_end_of_day_ms", "build_portfolio_capital_bridge"]
