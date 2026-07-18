from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

SCHEMA_VERSION = "portfolio.cash_bridge.v1"
_MONEY = Decimal("0.01")
_TOLERANCE = Decimal("0.05")
_STEP_KEYS = (
    "opening_cash",
    "external_cash_flow",
    "option_total_cash_change",
    "portfolio_and_other_cash_change",
    "reconciliation_residual",
    "ending_cash",
)
_OPTION_CASH_COMPONENTS = (
    "option_trade_cash_gross",
    "option_fee_cash",
    "stock_settlement_cash_gross",
    "stock_settlement_fee_cash",
    "assigned_stock_sale_cash_gross",
    "assigned_stock_sale_fee_cash",
)


def build_portfolio_cash_bridge(
    *,
    period: str,
    as_of_month: str,
    accounts: list[str],
    cash_facts_by_account: dict[str, dict[str, Any]],
    option_reports_by_account: dict[str, dict[str, Any]],
    observed_at: str | None = None,
) -> dict[str, Any]:
    period_value = str(period).strip().lower()
    rows = [
        _account_bridge(
            account=account,
            period=period_value,
            as_of_month=as_of_month,
            facts=cash_facts_by_account.get(account),
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
                "endpoint": "/analysis/cash-facts",
            },
            "option_cash": {
                "service": "options-monitor",
                "source": "option_performance_report.cash.total_cash_change_net",
                "basis": "complete_signed_option_and_assignment_lifecycle_cash_after_incurred_fees",
                "components": list(_OPTION_CASH_COMPONENTS),
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
    required = _required_money(amounts, "opening_cash", "external_cash_flow", "ending_cash")
    if required is None:
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_cash_amounts_incomplete",
        )
    currency = str(amounts.get("currency") or "").strip().upper()
    if currency != "CNY":
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_cash_currency_not_cny",
        )

    opening, external, ending = required
    has_reported_period_change = "period_cash_change" in amounts
    reported_period_change = _money_or_none(amounts.get("period_cash_change"))
    if has_reported_period_change and reported_period_change is None:
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_cash_period_change_invalid",
        )
    if reported_period_change is None:
        period_change = _money(ending - opening - external)
        period_change_basis = "derived_from_opening_external_and_ending_cash"
    else:
        period_change = reported_period_change
        period_change_basis = "portfolio_cash_facts"

    option = _option_cash_evidence(
        report,
        account=account,
        period=period,
        end_date=str(fact_period["end_date"]),
    )
    option_amount = _money_or_none(option.get("amount_cny")) if option.get("usable") else None
    other_change = _money(period_change - option_amount) if option_amount is not None else None
    difference = _money(opening + external + period_change - ending)
    reconciliation_residual = _money(-difference)
    reconciliation_status = "ok" if abs(difference) <= _TOLERANCE else "mismatch"
    status = "ok" if option_amount is not None and reconciliation_status == "ok" else "partial"

    return {
        "account": account,
        "status": status,
        "currency": "CNY",
        "period": fact_period,
        "steps": [
            _step("opening_cash", "Opening cash", "total", opening, "observed"),
            _step("external_cash_flow", "External cash flow", "flow", external, "observed"),
            _step(
                "option_total_cash_change",
                "Option total cash change",
                "flow",
                option_amount,
                str(option.get("status") or "not_observed"),
            ),
            _step(
                "portfolio_and_other_cash_change",
                "Portfolio and other cash change",
                "flow",
                other_change,
                "derived" if other_change is not None else "not_observed",
            ),
            _step(
                "reconciliation_residual",
                "Reconciliation residual",
                "flow",
                reconciliation_residual,
                reconciliation_status,
            ),
            _step("ending_cash", "Ending cash", "total", ending, "observed"),
        ],
        "calculation": {
            "period_cash_change": _float(period_change),
            "period_cash_change_basis": period_change_basis,
            "decomposition_formula": (
                "period_cash_change = option_total_cash_change + portfolio_and_other_cash_change"
            ),
            "bridge_formula": (
                "ending_cash = opening_cash + external_cash_flow + option_total_cash_change + "
                "portfolio_and_other_cash_change + reconciliation_residual"
            ),
        },
        "option_cash_evidence": option,
        "reconciliation": {
            "status": reconciliation_status,
            "difference": _float(difference),
            "residual": _float(reconciliation_residual),
            "tolerance": _float(_TOLERANCE),
            "formula": "opening_cash + external_cash_flow + period_cash_change - ending_cash",
        },
    }


def _fact_contract_error(facts: Any, *, period: str, as_of_month: str) -> str | None:
    if not isinstance(facts, dict) or str(facts.get("status") or "") != "ok":
        return str((facts or {}).get("reason") or "portfolio_cash_facts_unavailable")
    fact_period = facts.get("period") if isinstance(facts.get("period"), dict) else {}
    if str(fact_period.get("kind") or "").strip().lower() != period:
        return "portfolio_cash_period_mismatch"
    requested_month = str(fact_period.get("requested_as_of_month") or "").strip()
    if requested_month and requested_month != as_of_month:
        return "portfolio_cash_as_of_month_mismatch"
    if not str(fact_period.get("end_date") or "").strip():
        return "portfolio_cash_end_date_missing"
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


def _option_cash_evidence(
    report: Any,
    *,
    account: str,
    period: str,
    end_date: str,
) -> dict[str, Any]:
    unavailable = {
        "status": "not_observed",
        "amount_cny": None,
        "usable": False,
        "reason": "option_performance_report_unavailable",
        "components": {},
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

    cash = report.get("cash") if isinstance(report.get("cash"), dict) else {}
    metric = cash.get("total_cash_change_net") if isinstance(cash.get("total_cash_change_net"), dict) else {}
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
        "components": {key: cash.get(key) for key in _OPTION_CASH_COMPONENTS},
    }
    if not usable:
        evidence["reason"] = "option_total_cash_change_unavailable"
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
            totals["opening_cash"]
            + totals["external_cash_flow"]
            + totals["option_total_cash_change"]
            + totals["portfolio_and_other_cash_change"]
            + totals["reconciliation_residual"]
            - totals["ending_cash"]
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
                "total" if key in {"opening_cash", "ending_cash"} else "flow",
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
        f"### {str(result['period']['kind']).upper()} 现金余额桥",
        "",
        "| 账户 | 期权现金变化 | 其他现金变化 | 对账残差 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in result["accounts"]:
        steps = {item["key"]: item for item in row.get("steps") or []}
        lines.append(
            f"| {row['account']} | {_display(steps.get('option_total_cash_change'))} | "
            f"{_display(steps.get('portfolio_and_other_cash_change'))} | "
            f"{_display(steps.get('reconciliation_residual'))} | {row['status']} |"
        )
    combined = result.get("combined") or {}
    if combined.get("steps"):
        steps = {item["key"]: item for item in combined["steps"]}
        lines.append(
            f"| **合计** | {_display(steps.get('option_total_cash_change'))} | "
            f"{_display(steps.get('portfolio_and_other_cash_change'))} | "
            f"{_display(steps.get('reconciliation_residual'))} | {combined.get('status')} |"
        )
    if result.get("status") != "ok":
        lines.extend(["", "> 缺失或不完整证据保持为空，不按 0 处理；不会用总资产替代现金余额。"])
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


__all__ = ["SCHEMA_VERSION", "build_portfolio_cash_bridge"]
