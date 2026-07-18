from __future__ import annotations

from src.application.portfolio_pnl_bridge import build_portfolio_pnl_bridge


def _facts(
    account: str,
    *,
    period: str = "mtd",
    month: str = "2026-07",
    end_date: str = "2026-07-16",
    opening: float = 1000.0,
    external: float = 100.0,
    period_pnl: float = 150.0,
    ending: float = 1250.0,
    currency: str = "CNY",
) -> dict:
    return {
        "success": True,
        "status": "ok",
        "account": account,
        "period": {
            "kind": period,
            "requested_as_of_month": month,
            "end_date": end_date,
            "timezone": "Asia/Shanghai",
        },
        "amounts": {
            "currency": currency,
            "opening_assets": opening,
            "external_cash_flow": external,
            "period_pnl": period_pnl,
            "ending_assets": ending,
        },
    }


def _report(
    account: str,
    *,
    period: str = "mtd",
    end_date: str = "2026-07-16",
    pnl: float | None = 40.0,
    status: str = "observed",
) -> dict:
    return {
        "period": {"kind": period, "requested_end_date": end_date},
        "scope": {"account": account},
        "cash": {
            "stock_settlement_cash_gross": {
                "by_currency": {"USD": -10000.0},
                "cny": -72000.0,
                "status": "observed",
            }
        },
        "quality": {"status": "observed", "evidence_fact_ids": ["mark-1", "fee-1"]},
        "pnl": {
            "period_total_net": {
                "by_currency": {"USD": 5.0} if pnl is not None else {},
                "cny": pnl,
                "status": status,
                "missing": ["fee:missing"] if status == "partial" else [],
                "fx_fact_ids": ["fx-1"] if pnl is not None else [],
            }
        },
    }


def _step(row: dict, key: str) -> dict:
    return next(item for item in row["steps"] if item["key"] == key)


def test_pnl_bridge_uses_period_total_net_and_excludes_assignment_principal() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx")},
        observed_at="2026-07-18T00:00:00+00:00",
    )

    account = result["accounts"][0]
    assert result["status"] == "ok"
    assert _step(account, "option_period_total_net_pnl")["amount"] == 40.0
    assert _step(account, "portfolio_and_other_pnl")["amount"] == 110.0
    assert _step(account, "reconciliation_residual")["amount"] == 0.0
    assert account["option_pnl_evidence"]["fx_fact_ids"] == ["fx-1"]
    assert account["option_pnl_evidence"]["evidence_fact_ids"] == ["mark-1", "fee-1"]
    assert result["source"]["option_pnl"]["assignment_principal_included"] is False
    assert "stock_settlement_cash" not in account["calculation"]["bridge_formula"]
    assert "### MTD 总资产盈亏桥" in result["fallback_text"]


def test_partial_net_metric_is_not_used_even_when_it_contains_a_cny_value() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx", pnl=40.0, status="partial")},
    )

    account = result["accounts"][0]
    assert result["status"] == "partial"
    assert account["option_pnl_evidence"]["amount_cny"] == 40.0
    assert account["option_pnl_evidence"]["usable"] is False
    assert _step(account, "option_period_total_net_pnl")["amount"] is None
    assert _step(account, "portfolio_and_other_pnl")["amount"] is None
    assert "不按 0 处理" in result["fallback_text"]


def test_pnl_bridge_requires_aligned_option_period_and_account() -> None:
    wrong_period = _report("lx", end_date="2026-07-15")
    wrong_account = _report("sy")

    period_result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": wrong_period},
    )
    account_result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": wrong_account},
    )

    assert period_result["accounts"][0]["option_pnl_evidence"]["reason"] == "option_performance_period_mismatch"
    assert account_result["accounts"][0]["option_pnl_evidence"]["reason"] == "option_performance_account_mismatch"
    assert _step(period_result["accounts"][0], "option_period_total_net_pnl")["amount"] is None
    assert _step(account_result["accounts"][0], "option_period_total_net_pnl")["amount"] is None


def test_pnl_bridge_exposes_portfolio_reconciliation_residual() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx", ending=1240.0)},
        option_reports_by_account={"lx": _report("lx")},
    )

    account = result["accounts"][0]
    assert account["status"] == "partial"
    assert account["reconciliation"] == {
        "status": "mismatch",
        "difference": 10.0,
        "residual": -10.0,
        "tolerance": 0.05,
        "formula": "opening_assets + external_cash_flow + period_pnl - ending_assets",
    }
    assert _step(account, "reconciliation_residual")["amount"] == -10.0


def test_combined_pnl_bridge_requires_matching_end_dates_and_complete_accounts() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx", "sy"],
        capital_facts_by_account={
            "lx": _facts("lx", end_date="2026-07-16"),
            "sy": _facts("sy", end_date="2026-07-15"),
        },
        option_reports_by_account={
            "lx": _report("lx", end_date="2026-07-16"),
            "sy": _report("sy", end_date="2026-07-15"),
        },
    )

    assert result["status"] == "partial"
    assert result["combined"]["reason"] == "account_end_date_mismatch"
    assert result["combined"]["end_dates"] == {"lx": "2026-07-16", "sy": "2026-07-15"}


def test_unavailable_or_non_cny_capital_facts_never_create_bridge_amounts() -> None:
    unavailable = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={
            "lx": {
                "status": "unavailable",
                "reason": "previous_month_anchor_missing",
                "period": {"kind": "mtd", "requested_as_of_month": "2026-07"},
            }
        },
        option_reports_by_account={},
    )
    wrong_currency = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts("lx", currency="USD")},
        option_reports_by_account={"lx": _report("lx")},
    )

    assert unavailable["status"] == "unavailable"
    assert unavailable["accounts"][0]["reason"] == "previous_month_anchor_missing"
    assert unavailable["accounts"][0]["steps"] == []
    assert wrong_currency["accounts"][0]["reason"] == "portfolio_capital_currency_not_cny"
    assert wrong_currency["accounts"][0]["steps"] == []


def test_ytd_pnl_bridge_uses_the_requested_pm_end_date() -> None:
    result = build_portfolio_pnl_bridge(
        period="ytd",
        as_of_month="2026-06",
        accounts=["lx"],
        capital_facts_by_account={
            "lx": _facts(
                "lx",
                period="ytd",
                month="2026-06",
                end_date="2026-06-30",
            )
        },
        option_reports_by_account={
            "lx": _report("lx", period="ytd", end_date="2026-06-30")
        },
    )

    assert result["status"] == "ok"
    assert result["period"] == {"kind": "ytd", "as_of_month": "2026-06"}
    assert result["accounts"][0]["option_pnl_evidence"]["period"]["requested_end_date"] == "2026-06-30"
