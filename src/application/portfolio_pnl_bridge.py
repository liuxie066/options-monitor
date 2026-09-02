from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


SCHEMA_VERSION = "portfolio.pnl_bridge.v1"
_MONEY = Decimal("0.01")
_TOLERANCE = Decimal("0.05")
_REPORTING_TIMEZONE = "Asia/Shanghai"
_OPTION_PNL_REASON = "authoritative_option_pnl_source_unavailable"
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
    observed_at: str | None = None,
) -> dict[str, Any]:
    period_value = str(period).strip().lower()
    rows = [
        _account_bridge(
            account=account,
            period=period_value,
            as_of_month=as_of_month,
            facts=capital_facts_by_account.get(account),
        )
        for account in accounts
    ]
    combined = _combined(rows, period=period_value, as_of_month=as_of_month)
    available = [row for row in rows if row["status"] != "unavailable"]
    result = {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "status": "partial" if available else "unavailable",
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
                "status": "unavailable",
                "source": None,
                "reason": _OPTION_PNL_REASON,
            },
        },
        "freshness": {
            "status": "historical",
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        },
    }
    result["fallback_text"] = _fallback_text(result)
    return result


def _account_bridge(
    *,
    account: str,
    period: str,
    as_of_month: str,
    facts: Any,
) -> dict[str, Any]:
    fact_error = _fact_contract_error(
        facts,
        account=account,
        period=period,
        as_of_month=as_of_month,
    )
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
    required = _required_money(
        amounts,
        "opening_assets",
        "external_cash_flow",
        "period_pnl",
        "ending_assets",
    )
    if required is None:
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_capital_amounts_incomplete",
        )
    if str(amounts.get("currency") or "").strip().upper() != "CNY":
        return _unavailable_account(
            account,
            facts=facts,
            period=period,
            as_of_month=as_of_month,
            reason="portfolio_capital_currency_not_cny",
        )

    opening, external, period_pnl, ending = required
    difference = _money(opening + external + period_pnl - ending)
    residual = _money(-difference)
    reconciliation_status = "ok" if abs(difference) <= _TOLERANCE else "mismatch"
    option = {
        "status": "unavailable",
        "amount_cny": None,
        "usable": False,
        "source": None,
        "reason": _OPTION_PNL_REASON,
    }
    return {
        "account": account,
        "status": "partial",
        "currency": "CNY",
        "period": fact_period,
        "steps": [
            _step("opening_assets", "Opening assets", "total", opening, "observed"),
            _step("external_cash_flow", "External cash flow", "flow", external, "observed"),
            _step(
                "option_period_total_net_pnl",
                "Option period total net PnL",
                "flow",
                None,
                "unavailable",
            ),
            _step(
                "portfolio_and_other_pnl",
                "Portfolio and other PnL",
                "flow",
                None,
                "unavailable",
            ),
            _step(
                "reconciliation_residual",
                "Reconciliation residual",
                "flow",
                residual,
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
                "portfolio_period_pnl + reconciliation_residual"
            ),
        },
        "option_pnl_evidence": option,
        "reconciliation": {
            "status": reconciliation_status,
            "difference": _float(difference),
            "residual": _float(residual),
            "tolerance": _float(_TOLERANCE),
            "formula": "opening_assets + external_cash_flow + period_pnl - ending_assets",
        },
    }


def _fact_contract_error(
    facts: Any,
    *,
    account: str,
    period: str,
    as_of_month: str,
) -> str | None:
    if not isinstance(facts, dict) or str(facts.get("status") or "") != "ok":
        return str((facts or {}).get("reason") or "portfolio_capital_facts_unavailable")
    if str(facts.get("account") or "").strip() != account:
        return "portfolio_capital_account_mismatch"
    fact_period = facts.get("period") if isinstance(facts.get("period"), dict) else {}
    if str(fact_period.get("kind") or "").strip().lower() != period:
        return "portfolio_capital_period_mismatch"
    requested_month = str(fact_period.get("requested_as_of_month") or "").strip()
    if requested_month and requested_month != as_of_month:
        return "portfolio_capital_as_of_month_mismatch"
    if str(fact_period.get("timezone") or "").strip() != _REPORTING_TIMEZONE:
        return "portfolio_capital_timezone_mismatch"
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
        "period": dict(
            (facts or {}).get("period")
            or {"kind": period, "requested_as_of_month": as_of_month}
        ),
        "steps": [],
        "option_pnl_evidence": {
            "status": "unavailable",
            "amount_cny": None,
            "usable": False,
            "source": None,
            "reason": _OPTION_PNL_REASON,
        },
    }


def _combined(
    rows: list[dict[str, Any]],
    *,
    period: str,
    as_of_month: str,
) -> dict[str, Any]:
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
            "period": {"kind": period, "as_of_month": as_of_month},
            "steps": [],
        }
    totals: dict[str, Decimal | None] = {}
    for key in _STEP_KEYS:
        values = [_step_amount(row, key) for row in available]
        totals[key] = (
            None
            if any(value is None for value in values)
            else _money(
                sum((value for value in values if value is not None), Decimal("0"))
            )
        )
    return {
        "status": "partial",
        "currency": "CNY",
        "period": {
            "kind": period,
            "as_of_month": as_of_month,
            "end_date": next(iter(end_dates.values())),
        },
        "steps": [
            _step(
                key,
                key.replace("_", " ").title(),
                "total" if key in {"opening_assets", "ending_assets"} else "flow",
                totals[key],
                "observed" if totals[key] is not None else "unavailable",
            )
            for key in _STEP_KEYS
        ],
        "option_pnl_evidence": {
            "status": "unavailable",
            "source": None,
            "reason": _OPTION_PNL_REASON,
        },
    }


def _fallback_text(result: dict[str, Any]) -> str:
    lines = [
        f"### {str(result['period']['kind']).upper()} 总资产盈亏桥",
        "",
        "| 账户 | 组合期间盈亏 | 期权盈亏分解 | 对账残差 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in result["accounts"]:
        steps = {item["key"]: item for item in row.get("steps") or []}
        period_pnl = (row.get("calculation") or {}).get("portfolio_period_pnl")
        period_pnl_text = "-" if period_pnl is None else f"{period_pnl:,.2f}"
        lines.append(
            f"| {row['account']} | {period_pnl_text} | "
            f"{_display(steps.get('option_period_total_net_pnl'))} | "
            f"{_display(steps.get('reconciliation_residual'))} | {row['status']} |"
        )
    lines.extend(
        ["", "> 期权 PnL 没有独立权威数据源，保持不可用，不以期权净现金流替代。"]
    )
    return "\n".join(lines)


def _required_money(source: dict[str, Any], *keys: str) -> tuple[Decimal, ...] | None:
    values = tuple(_money_or_none(source.get(key)) for key in keys)
    return None if any(value is None for value in values) else tuple(
        value for value in values if value is not None
    )


def _step(
    key: str,
    label: str,
    kind: str,
    amount: Decimal | None,
    status: str,
) -> dict[str, Any]:
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
