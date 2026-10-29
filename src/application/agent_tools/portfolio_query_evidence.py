"""Evidence qualification for PM read views, without financial recalculation."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def portfolio_query_evidence(
    view: str, data: dict[str, Any], scope: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Qualify source-reported facts only; PM cash is never trading capacity.

    Freshness policy belongs to PM's datasets. Do not apply the broker's short
    snapshot TTL to daily NAV or manually recorded assets, or replace source time
    with retrieval time. A fresh label without a valid observation time is unusable.
    """
    freshness = data["freshness"]
    reasons: list[str] = []
    if freshness.get("status") != "fresh" or freshness.get("trust_status") != "trusted":
        reasons.append("PM_FRESHNESS_NOT_TRUSTED")
    observed = _timestamp(freshness.get("observed_at_utc"))
    retrieved = _timestamp(data.get("retrieved_at_utc"))
    if observed is None or retrieved is None or observed > retrieved:
        reasons.append("PM_EVIDENCE_TIME_INVALID")
    if data.get("success") is not True:
        reasons.append("PM_SUCCESS_NOT_CONFIRMED")

    coverage = _coverage(view, data, payload)
    source_coverage = data.get("coverage")
    if data.get("status") not in (None, "ok", "empty", "complete") or (
        isinstance(source_coverage, dict) and source_coverage.get("status") != "complete"
    ):
        coverage = {**coverage, "status": "partial", "complete_for": "returned_rows"}
    if coverage["status"] != "complete":
        reasons.append("PM_QUERY_INCOMPLETE")
    if view not in {"accounts", "overview", "distribution"} and not scope.get("account"):
        reasons.append("ACCOUNT_SCOPE_MISSING")

    assessment: dict[str, Any] = {
        "status": "unable" if reasons else "reported_facts_only",
        "reason_codes": reasons,
        # Some PM v1 views have no account echo. Never claim that copying the
        # requested account into scope independently verified the returned identity.
        "scope_binding": "request" if scope.get("account") or scope.get("accounts") else "service_selected",
        "supported_claims": [] if reasons else [f"pm_reported_{view}_as_of"],
        "unsupported_claims": [
            "current_broker_balance", "available_to_trade", "additional_short_contracts",
            "unreported_broker_or_currency", "cause_of_data_difference",
        ],
        "guidance": (
            "账户事实必须引用本次工具结果的 scope、source、freshness 和 coverage。"
            "缺失、过期、局部失败或冲突时无法判断当前账户事实，不能用旧回答或自行推算补齐。"
            "币种、券商和分类沿用返回字段；未返回就写未观测，不能按持仓市场、名称或金额猜测。"
            "不同来源或时点的差额不证明原因；记录数相等不证明经济上没有重复。"
            "开仓能力结论固定为：无法判断账户能否加仓；本次仅查询 PM 记录。"
            "差异原因未观测时写原因未知，不列猜测的原因。"
        ),
    }
    if view == "cash":
        assessment["value_semantics"] = (
            "PM cash includes cash and MMF records. amount and by_currency retain PM's raw "
            "holding quantities and declared currencies; they are not broker buying power, "
            "withdrawable cash, or verified MMF redemption value. No implicit FX conversion."
        )
    return coverage, assessment


def _coverage(view: str, data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    rows: Any = None
    declared: Any = None
    structure_valid = True
    if view in {"accounts", "cash", "holdings"}:
        key = {"accounts": "accounts", "cash": "items", "holdings": "holdings"}[view]
        rows = data.get(key)
        if view == "holdings" and payload.get("group_by_market", False):
            groups = data.get("by_market")
            structure_valid = isinstance(groups, dict) and all(isinstance(v, list) for v in groups.values())
            rows = [row for group in groups.values() for row in group] if structure_valid else None
        declared = data.get("count")
        if view == "cash":
            totals = data.get("by_currency")
            structure_valid = isinstance(totals, dict) and all(
                isinstance(currency, str) and bool(currency.strip())
                and type(amount) in {int, float} and math.isfinite(amount)
                for currency, amount in totals.items()
            )
    elif view == "overview":
        rows, declared = data.get("items"), data.get("account_count")
        accounts = data.get("accounts")
        expected = payload.get("accounts")
        structure_valid = (
            isinstance(accounts, list) and all(isinstance(a, str) for a in accounts)
            and len(set(accounts)) == len(accounts)
            and (not expected or set(accounts) == set(expected))
            and type(data.get("failed_count")) is int and data["failed_count"] == 0
            and type(data.get("successful_count")) is int
            and data["successful_count"] == declared == len(accounts)
            and isinstance(data.get("summary"), dict)
        )
    elif view == "nav":
        rows = data.get("history")
        declared = len(rows) if isinstance(rows, list) else None
        structure_valid = isinstance(data.get("latest"), dict)
    elif view == "distribution":
        rows = data.get("by_asset" if payload.get("by_asset", False) else "by_type")
        declared = len(rows) if isinstance(rows, list) else None
        # PM's endpoint returns an unpaged distribution, not all raw holdings.
        expected = payload.get("accounts") or ([payload["account"]] if payload.get("account") else [])
        accounts = data.get("accounts")
        structure_valid = isinstance(accounts, list) and all(isinstance(a, str) for a in accounts)
        structure_valid = structure_valid and (not expected or set(accounts) == set(expected))
    elif view == "full_report":
        structure_valid = all(isinstance(data.get(k), dict) for k in ("overview", "returns"))
        structure_valid = structure_valid and isinstance(data.get("distribution"), list)
        structure_valid = structure_valid and isinstance(data.get("top_holdings"), list) and bool(data.get("generated_at"))
        return {"status": "complete" if structure_valid else "unknown", "complete_for": "report_summary"}

    if not isinstance(rows, list) or type(declared) is not int or declared < 0:
        return {"status": "unknown", "complete_for": "none", "included_count": len(rows) if isinstance(rows, list) else None,
                "total_count": declared if type(declared) is int and declared >= 0 else None}
    valid_rows = all(isinstance(row, str) if view == "accounts" else isinstance(row, dict) for row in rows)
    if view == "cash" and valid_rows:
        valid_rows = all(
            isinstance(row.get("currency"), str) and bool(row["currency"].strip())
            and type(row.get("amount")) in {int, float} and math.isfinite(row["amount"])
            and isinstance(row.get("type"), str) and row["type"] in {"cash", "mmf"}
            for row in rows
        )
        if valid_rows and structure_valid:
            structure_valid = set(data["by_currency"]) == {row["currency"] for row in rows}
    if view == "overview":
        labels = [row.get("account") for row in rows if isinstance(row, dict)]
        valid_rows = valid_rows and all(isinstance(a, str) for a in labels)
        valid_rows = valid_rows and len(set(labels)) == len(rows) and set(labels) == set(data.get("accounts") or [])
        valid_rows = valid_rows and all(row.get("success") is True for row in rows)
    complete = structure_valid and valid_rows and len(rows) == declared
    return {
        "status": "complete" if complete else "partial",
        "complete_for": "full_query" if complete else "returned_rows",
        "included_count": len(rows), "total_count": declared,
        "omitted_count": max(0, declared - len(rows)), "has_more": len(rows) < declared,
    }
