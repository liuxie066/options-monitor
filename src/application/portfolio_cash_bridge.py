from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "portfolio.cash_bridge.v1"
_UNAVAILABLE_REASON = "portfolio_cash_facts_not_onboarded"
_OPTION_CASH_REASON = "combined_option_assignment_cash_source_required"


def build_portfolio_cash_bridge(
    *,
    period: str,
    as_of_month: str,
    accounts: list[str],
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Keep the public route explicit while its authoritative inputs are unavailable."""

    period_value = str(period).strip().lower()
    period_scope = {"kind": period_value, "as_of_month": as_of_month}
    rows = [
        {
            "account": account,
            "status": "unavailable",
            "reason": _UNAVAILABLE_REASON,
            "period": dict(period_scope),
            "steps": [],
            "option_cash_evidence": {
                "status": "unavailable",
                "source": None,
                "reason": _OPTION_CASH_REASON,
            },
        }
        for account in accounts
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "status": "unavailable",
        "reason": _UNAVAILABLE_REASON,
        "period": period_scope,
        "accounts": rows,
        "combined": {
            "status": "unavailable",
            "reason": _UNAVAILABLE_REASON,
            "period": dict(period_scope),
            "steps": [],
        },
        "source": {
            "portfolio_cash": {
                "service": "portfolio-management",
                "status": "not_onboarded",
                "endpoint": None,
            },
            "option_cash": {
                "status": "unavailable",
                "source": None,
                "reason": _OPTION_CASH_REASON,
            },
        },
        "freshness": {
            "status": "unavailable",
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        },
    }
    result["fallback_text"] = (
        f"{period_value.upper()} 现金桥暂不可用：组合现金事实尚未接入，"
        "且期权净现金流不能替代包含指派生命周期的 CNY 现金事实。"
    )
    return result


__all__ = ["SCHEMA_VERSION", "build_portfolio_cash_bridge"]
