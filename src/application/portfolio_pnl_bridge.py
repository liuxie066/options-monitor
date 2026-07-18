from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

SCHEMA_VERSION = "portfolio.pnl_bridge.v1"
_MONEY = Decimal("0.01")
_TOLERANCE = Decimal("0.05")
_STEP_KEYS = (
    "opening_assets",
    "external_cash_flow",
    "option_period_total_net_pnl",
    "portfolio_and_other_pnl",
    "reconciliation_residual",
    "ending_assets",
)


def build_portfolio_pnl_bridge(
    *,
    period: str,
    as_of_month: str,
    accounts: list[str],
    capital_facts_by_account: dict[str, dict[str, Any]],
    option_reports_by_account: dict[str, dict[str, Any]],
    observed_at: str | None = None,
) -> dict[str, Any]:
    period_value = str(period).strip().lower()
    rows = [
        _account_bridge(
            account=account,
            period=period_value,
            as_of_month=as_of_month,
            facts=capital_facts_by_account.get(account),
            report=option_reports_by_account.get(account),
        )
        for account in accounts
    ]
    combined = _combined(rows, period=period_value, as_of_month=as_of_month)
    available = [row for row in rows if row["status"] != "unavailable"]
    if not available:
        status = "unavailable"
    elif all(row["status"] == "ok" for row in rows) and combined["status"] == "ok":
        status = "ok"
    else:
        status = "partial"
    result = {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "status": status,
        "period": {"kind": period_value, "as_of_month": as_of_month},
        "accounts": rows,
        "combined": combined,
        "source": {
            "portfolio": {
                "service": "portfolio-management",
                "transport": "loopback_http",
                "endpoint": "/analysis/capital-facts",
                "field": "amounts.period_pnl",
            },
            "option_pnl": {
                "service": "options-monitor",
                "source": "option_performance_report.pnl.period_total_net",
                "basis": "net_after_incurred_fees_with_opening_and_ending_valuation",
                "assignment_principal_included": False,
            },
        },
        "freshness": {"observed_at": observed_at or datetime.now(timezone.utc).isoformat()},
    }
    result["fallback_text"] = _fallback_text(result)
    return result


def _account_bridge(*, account: str, period: str, as_of_month: str, facts: Any, report: Any) -> dict[str, Any]:
    fact_error = _fact_contract_error(facts, period=period, as_of_month=as_of_month)
    if fact_error is not None:
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason=fact_error,
        )

    fact_period = dict(facts.get("period") or {})
    amounts = facts.get("amounts") if isinstance(facts.get("amounts"), dict) else {}
    required = _required_money(amounts, "opening_assets", "external_cash_flow", "period_pnl", "ending_assets")
    if required is None:
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_capital_amounts_incomplete",
        )
    currency = str(amounts.get("currency") or "").strip().upper()
    if currency != "CNY":
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_capital_currency_not_cny",
        )

    opening, external, period_pnl, ending = required
    option = _metric_evidence(
        report,
        namespace="pnl",
        field="period_total_net",
        account=account,
        period=period,
        end_date=str(fact_period["end_date"]),
    )
    option_amount = _money_or_none(option.get("amount_cny")) if option.get("usable") else None
    other_pnl = _money(period_pnl - option_amount) if option_amount is not None else None
    difference = _money(opening + external + period_pnl - ending)
    reconciliation_residual = _money(-difference)
    reconciliation_status = "ok" if abs(difference) <= _TOLERANCE else "mismatch"
    status = "ok" if option_amount is not None and reconciliation_status == "ok" else "partial"

    return {
        "account": account,
        "status": status,
        "currency": "CNY",
        "period": fact_period,
        "steps": [
            _step("opening_assets", "Opening assets", "total", opening, "observed"),
            _step("external_cash_flow", "External cash flow", "flow", external, "observed"),
            _step(
                "option_period_total_net_pnl",
                "Option period total net PnL",
                "flow",
                option_amount,
                str(option.get("status") or "not_observed"),
            ),
            _step(
                "portfolio_and_other_pnl",
                "Portfolio and other PnL",
                "flow",
                other_pnl,
                "derived" if other_pnl is not None else "not_observed",
            ),
            _step(
                "reconciliation_residual",
                "Reconciliation residual",
                "flow",
                reconciliation_residual,
                reconciliation_status,
            ),
            _step("ending_assets", "Ending assets", "total", ending, "observed"),
        ],
        "calculation": {
            "portfolio_period_pnl": _float(period_pnl),
            "decomposition_formula": (
                "portfolio_period_pnl = option_period_total_net_pnl + portfolio_and_other_pnl"
            ),
            "bridge_formula": (
                "ending_assets = opening_assets + external_cash_flow + "
                "option_period_total_net_pnl + portfolio_and_other_pnl + reconciliation_residual"
            ),
        },
        "option_pnl_evidence": option,
        "reconciliation": {
            "status": reconciliation_status,
            "difference": _float(difference),
            "residual": _float(reconciliation_residual),
            "tolerance": _float(_TOLERANCE),
            "formula": "opening_assets + external_cash_flow + period_pnl - ending_assets",
        },
    }


def _fact_contract_error(facts: Any, *, period: str, as_of_month: str) -> str | None:
    if not isinstance(facts, dict) or str(facts.get("status") or "") != "ok":
        return str((facts or {}).get("reason") or "portfolio_capital_facts_unavailable")
    fact_period = facts.get("period") if isinstance(facts.get("period"), dict) else {}
    if str(fact_period.get("kind") or "").strip().lower() != period:
        return "portfolio_capital_period_mismatch"
    requested_month = str(fact_period.get("requested_as_of_month") or "").strip()
    if requested_month and requested_month != as_of_month:
        return "portfolio_capital_as_of_month_mismatch"
    if not str(fact_period.get("end_date") or "").strip():
        return "portfolio_capital_end_date_missing"
    return None


def _unavailable_account(
    account: str,
    *,
    facts: Any,
    period: str,
    as_of_month: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "account": account,
        "status": "unavailable",
        "reason": reason,
        "period": dict((facts or {}).get("period") or {"kind": period, "requested_as_of_month": as_of_month}),
        "steps": [],
    }


def _metric_evidence(
    report: Any,
    *,
    namespace: str,
    field: str,
    account: str,
    period: str,
    end_date: str,
) -> dict[str, Any]:
    unavailable = {
        "status": "not_observed",
        "amount_cny": None,
        "usable": False,
        "reason": "option_performance_report_unavailable",
    }
    if not isinstance(report, dict):
        return unavailable
    report_period = report.get("period") if isinstance(report.get("period"), dict) else {}
    if (
        str(report_period.get("kind") or "").strip().lower() != period
        or str(report_period.get("requested_end_date") or "").strip() != end_date
    ):
        return {**unavailable, "reason": "option_performance_period_mismatch", "period": dict(report_period)}
    scope = report.get("scope") if isinstance(report.get("scope"), dict) else {}
    scoped_account = str(scope.get("account") or "").strip()
    if scoped_account and scoped_account != account:
        return {**unavailable, "reason": "option_performance_account_mismatch", "scope": dict(scope)}

    section = report.get(namespace) if isinstance(report.get(namespace), dict) else {}
    metric = section.get(field) if isinstance(section.get(field), dict) else {}
    quality = metric.get("quality") if isinstance(metric.get("quality"), dict) else {}
    report_quality = report.get("quality") if isinstance(report.get("quality"), dict) else {}
    status = str(metric.get("status") or quality.get("status") or "not_observed")
    raw_amount = _money_or_none(metric.get("cny"))
    usable = status == "observed" and raw_amount is not None
    evidence = {
        "status": status,
        "amount_cny": _float(raw_amount) if raw_amount is not None else None,
        "usable": usable,
        "missing": list(metric.get("missing") or quality.get("missing") or []),
        "warnings": list(metric.get("warnings") or quality.get("warnings") or []),
        "fx_fact_ids": list(metric.get("fx_fact_ids") or []),
        "evidence_fact_ids": list(
            dict.fromkeys(
                [
                    *list(report_quality.get("evidence_fact_ids") or []),
                    *list(quality.get("evidence_fact_ids") or []),
                ]
            )
        ),
        "report_quality": dict(report_quality),
        "period": dict(report_period),
        "scope": dict(scope),
    }
    if not usable:
        evidence["reason"] = "option_period_total_net_pnl_unavailable"
    return evidence


def _combined(rows: list[dict[str, Any]], *, period: str, as_of_month: str) -> dict[str, Any]:
    available = [row for row in rows if row.get("status") != "unavailable"]
    if not available:
        return {
            "status": "unavailable",
            "reason": "account_bridge_unavailable",
            "period": {"kind": period, "as_of_month": as_of_month},
            "steps": [],
        }
    end_dates = {
        str(row.get("account")): str((row.get("period") or {}).get("end_date") or "")
        for row in available
    }
    if len(set(end_dates.values())) != 1:
        return {
            "status": "unavailable",
            "reason": "account_end_date_mismatch",
            "accounts": [str(row.get("account")) for row in available],
            "end_dates": end_dates,
            "period": {"kind": period, "as_of_month": as_of_month},
            "steps": [],
        }

    totals: dict[str, Decimal | None] = {}
    for key in _STEP_KEYS:
        values = [_step_amount(row, key) for row in available]
        totals[key] = (
            None
            if any(value is None for value in values)
            else _money(sum((value for value in values if value is not None), Decimal("0")))
        )
    complete = all(totals[key] is not None for key in _STEP_KEYS)
    difference = None
    if complete:
        difference = _money(
            totals["opening_assets"]
            + totals["external_cash_flow"]
            + totals["option_period_total_net_pnl"]
            + totals["portfolio_and_other_pnl"]
            + totals["reconciliation_residual"]
            - totals["ending_assets"]
        )
    reconciliation_status = (
        "ok" if difference is not None and abs(difference) <= _TOLERANCE else "not_observed"
    )
    status = (
        "ok"
        if reconciliation_status == "ok"
        and len(available) == len(rows)
        and all(row.get("status") == "ok" for row in rows)
        else "partial"
    )
    end_date = next(iter(end_dates.values()))
    return {
        "status": status,
        "currency": "CNY",
        "period": {"kind": period, "as_of_month": as_of_month, "end_date": end_date},
        "steps": [
            _step(
                key,
                key.replace("_", " ").title(),
                "total" if key in {"opening_assets", "ending_assets"} else "flow",
                totals[key],
                "observed" if totals[key] is not None else "not_observed",
            )
            for key in _STEP_KEYS
        ],
        "reconciliation": {
            "status": reconciliation_status,
            "difference": _float(difference) if difference is not None else None,
            "tolerance": _float(_TOLERANCE),
        },
    }


def _fallback_text(result: dict[str, Any]) -> str:
    lines = [
        f"### {str(result['period']['kind']).upper()} 总资产盈亏桥",
        "",
        "| 账户 | 期权期间净盈亏 | 其他资产盈亏 | 对账残差 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in result["accounts"]:
        steps = {item["key"]: item for item in row.get("steps") or []}
        lines.append(
            f"| {row['account']} | {_display(steps.get('option_period_total_net_pnl'))} | "
            f"{_display(steps.get('portfolio_and_other_pnl'))} | "
            f"{_display(steps.get('reconciliation_residual'))} | {row['status']} |"
        )
    combined = result.get("combined") or {}
    if combined.get("steps"):
        steps = {item["key"]: item for item in combined["steps"]}
        lines.append(
            f"| **合计** | {_display(steps.get('option_period_total_net_pnl'))} | "
            f"{_display(steps.get('portfolio_and_other_pnl'))} | "
            f"{_display(steps.get('reconciliation_residual'))} | {combined.get('status')} |"
        )
    if result.get("status") != "ok":
        lines.extend(["", "> 缺失或不完整证据保持为空，不按 0 处理。"])
    return "\n".join(lines)


def _required_money(source: dict[str, Any], *keys: str) -> tuple[Decimal, ...] | None:
    values = tuple(_money_or_none(source.get(key)) for key in keys)
    return None if any(value is None for value in values) else tuple(value for value in values if value is not None)


def _step(key: str, label: str, kind: str, amount: Decimal | None, status: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "amount": _float(amount) if amount is not None else None,
        "status": status,
    }


def _step_amount(row: dict[str, Any], key: str) -> Decimal | None:
    for item in row.get("steps") or []:
        if item.get("key") == key:
            return _money_or_none(item.get("amount"))
    return None


def _display(step: Any) -> str:
    amount = (step or {}).get("amount") if isinstance(step, dict) else None
    return "-" if amount is None else f"{amount:,.2f}"


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid money amount: {value!r}") from exc


def _money_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return _money(value)
    except ValueError:
        return None


def _float(value: Decimal) -> float:
    return float(value)


__all__ = ["SCHEMA_VERSION", "build_portfolio_pnl_bridge"]
