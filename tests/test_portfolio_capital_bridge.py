from __future__ import annotations

from datetime import datetime, timezone

from src.application.portfolio_capital_bridge import beijing_end_of_day_ms, build_portfolio_capital_bridge


def _facts(
    account: str,
    *,
    period: str = "mtd",
    month: str = "2026-07",
    end_date: str = "2026-07-16",
    opening: float = 1000.0,
    external: float = 100.0,
    ending: float = 1250.0,
) -> dict:
    period_pnl = ending - opening - external
    return {
        "schema_version": "portfolio.capital_facts.v1",
        "success": True,
        "status": "ok",
        "account": account,
        "period": {
            "kind": period,
            "requested_as_of_month": month,
            "calendar_start": f"{month}-01" if period == "mtd" else "2026-01-01",
            "anchor_date": "2026-06-30" if period == "mtd" else "2025-12-31",
            "end_date": end_date,
            "timezone": "Asia/Shanghai",
        },
        "amounts": {
            "currency": "CNY",
            "opening_assets": opening,
            "external_cash_flow": external,
            "period_pnl": period_pnl,
            "ending_assets": ending,
        },
        "reconciliation": {"status": "ok"},
    }


def _step(result: dict, key: str) -> dict:
    return next(item for item in result["steps"] if item["key"] == key)


def test_mtd_bridge_builds_account_and_combined_waterfall_steps() -> None:
    result = build_portfolio_capital_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx", "sy"],
        capital_facts_by_account={
            "lx": _facts("lx"),
            "sy": _facts("sy", opening=2000.0, external=-50.0, ending=2050.0),
        },
        option_reports_by_end_date={
            "2026-07-16": {
                "return_summary": [
                    {"month": "2026-07", "account": "lx", "net_income_cny": 40.0},
                    {"month": "2026-07", "account": "sy", "net_income_cny": 10.0},
                ]
            }
        },
        option_scope_accounts={"lx", "sy"},
        observed_at="2026-07-17T00:00:00+00:00",
    )

    assert result["status"] == "ok"
    lx = result["accounts"][0]
    assert _step(lx, "opening_assets")["amount"] == 1000.0
    assert _step(lx, "external_cash_flow")["amount"] == 100.0
    assert _step(lx, "option_cash_change")["amount"] == 40.0
    assert _step(lx, "portfolio_and_other_change")["amount"] == 110.0
    assert _step(lx, "ending_assets")["amount"] == 1250.0
    assert lx["reconciliation"]["status"] == "ok"

    combined = result["combined"]
    assert combined["status"] == "ok"
    assert _step(combined, "opening_assets")["amount"] == 3000.0
    assert _step(combined, "external_cash_flow")["amount"] == 50.0
    assert _step(combined, "option_cash_change")["amount"] == 50.0
    assert _step(combined, "portfolio_and_other_change")["amount"] == 200.0
    assert _step(combined, "ending_assets")["amount"] == 3300.0
    assert result["source"]["option_cash"]["shared_ledger_loads"] == 1
    assert "### MTD 总资产变化桥" in result["fallback_text"]
    assert "| **合计** |" in result["fallback_text"]


def test_ytd_bridge_selects_january_through_requested_month_only() -> None:
    result = build_portfolio_capital_bridge(
        period="ytd",
        as_of_month="2026-06",
        accounts=["lx"],
        capital_facts_by_account={
            "lx": _facts("lx", period="ytd", month="2026-06", end_date="2026-06-30")
        },
        option_reports_by_end_date={
            "2026-06-30": {
                "return_summary": [
                    {"month": "2025-12", "account": "lx", "net_income_cny": 99.0},
                    {"month": "2026-01", "account": "lx", "net_income_cny": 10.0},
                    {"month": "2026-06", "account": "lx", "net_income_cny": 20.0},
                    {"month": "2026-07", "account": "lx", "net_income_cny": 88.0},
                ]
            }
        },
        option_scope_accounts={"lx"},
    )

    account = result["accounts"][0]
    assert account["option_cash_evidence"]["months"] == ["2026-01", "2026-06"]
    assert _step(account, "option_cash_change")["amount"] == 30.0
    assert _step(account, "portfolio_and_other_change")["amount"] == 120.0


def test_missing_option_evidence_is_null_while_out_of_scope_is_zero() -> None:
    result = build_portfolio_capital_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx", "sy"],
        capital_facts_by_account={"lx": _facts("lx"), "sy": _facts("sy")},
        option_reports_by_end_date={"2026-07-16": {"return_summary": []}},
        option_scope_accounts={"lx"},
    )

    lx, sy = result["accounts"]
    assert lx["status"] == "partial"
    assert lx["option_cash_evidence"]["status"] == "not_observed"
    assert _step(lx, "option_cash_change")["amount"] is None
    assert _step(lx, "portfolio_and_other_change")["amount"] is None
    assert lx["reconciliation"]["status"] == "not_observed"

    assert sy["status"] == "ok"
    assert sy["option_cash_evidence"]["status"] == "not_applicable"
    assert _step(sy, "option_cash_change")["amount"] == 0.0
    assert _step(sy, "portfolio_and_other_change")["amount"] == 150.0
    assert result["combined"]["status"] == "partial"
    assert _step(result["combined"], "option_cash_change")["amount"] is None
    assert "未将缺失值按 0 处理" in result["fallback_text"]


def test_combined_bridge_requires_matching_actual_end_dates() -> None:
    result = build_portfolio_capital_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx", "sy"],
        capital_facts_by_account={
            "lx": _facts("lx", end_date="2026-07-16"),
            "sy": _facts("sy", end_date="2026-07-15"),
        },
        option_reports_by_end_date={
            "2026-07-15": {"return_summary": [{"month": "2026-07", "account": "sy", "net_income_cny": 10.0}]},
            "2026-07-16": {"return_summary": [{"month": "2026-07", "account": "lx", "net_income_cny": 20.0}]},
        },
        option_scope_accounts={"lx", "sy"},
    )

    assert result["status"] == "partial"
    assert result["combined"] == {
        "status": "unavailable",
        "reason": "account_end_date_mismatch",
        "accounts": ["lx", "sy"],
        "end_dates": {"lx": "2026-07-16", "sy": "2026-07-15"},
        "steps": [],
    }


def test_unavailable_portfolio_facts_do_not_create_bridge_amounts() -> None:
    result = build_portfolio_capital_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={
            "lx": {
                "success": True,
                "status": "unavailable",
                "reason": "previous_month_anchor_missing",
                "period": {"kind": "mtd", "requested_as_of_month": "2026-07"},
            }
        },
        option_reports_by_end_date={},
        option_scope_accounts={"lx"},
    )

    assert result["status"] == "unavailable"
    assert result["accounts"][0]["steps"] == []
    assert result["accounts"][0]["reason"] == "previous_month_anchor_missing"
    assert result["combined"]["reason"] == "account_bridge_unavailable"


def test_beijing_cutoff_uses_actual_nav_date_end_of_day() -> None:
    cutoff = beijing_end_of_day_ms("2026-07-16")

    assert datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc).isoformat(timespec="milliseconds") == (
        "2026-07-16T15:59:59.999+00:00"
    )
