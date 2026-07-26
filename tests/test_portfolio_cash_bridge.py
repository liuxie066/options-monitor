from __future__ import annotations

from src.application.portfolio_cash_bridge import build_portfolio_cash_bridge


def _facts(
    account: str,
    *,
    period: str = "mtd",
    month: str = "2026-07",
    end_date: str = "2026-07-16",
    opening: float = 500.0,
    external: float = 100.0,
    ending: float = 550.0,
    period_change: float | None = None,
    currency: str = "CNY",
) -> dict:
    amounts = {
        "currency": currency,
        "opening_cash": opening,
        "external_cash_flow": external,
        "ending_cash": ending,
    }
    if period_change is not None:
        amounts["period_cash_change"] = period_change
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
        "amounts": amounts,
    }


def _metric(amount: float, *, status: str = "observed") -> dict:
    return {
        "by_currency": {"USD": amount / 7.2},
        "cny": amount,
        "status": status,
        "missing": ["fee:missing"] if status == "partial" else [],
        "fx_fact_ids": ["fx-1"],
    }


def _report(
    account: str,
    *,
    period: str = "mtd",
    end_date: str = "2026-07-16",
    cash: float | None = -200.0,
    status: str = "observed",
) -> dict:
    total = _metric(cash, status=status) if cash is not None else {"cny": None, "status": status}
    return {
        "period": {
            "kind": period,
            "requested_end_date": end_date,
            "reporting_timezone": "Asia/Shanghai",
        },
        "scope": {"account": account},
        "quality": {"status": "observed", "evidence_fact_ids": ["cash-event-1", "fee-1"]},
        "cash": {
            "option_trade_cash_gross": _metric(100.0),
            "option_fee_cash": _metric(-5.0),
            "stock_settlement_cash_gross": _metric(-1000.0),
            "stock_settlement_fee_cash": _metric(-2.0),
            "assigned_stock_sale_cash_gross": _metric(800.0),
            "assigned_stock_sale_fee_cash": _metric(-1.0),
            "total_cash_change_net": total,
        },
    }


def _step(row: dict, key: str) -> dict:
    return next(item for item in row["steps"] if item["key"] == key)


def test_cash_bridge_derives_period_change_from_cash_balances_without_asset_substitution() -> None:
    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx")},
        observed_at="2026-07-18T00:00:00+00:00",
    )

    account = result["accounts"][0]
    assert result["status"] == "ok"
    assert account["calculation"]["period_cash_change"] == -50.0
    assert account["calculation"]["period_cash_change_basis"] == "derived_from_opening_external_and_ending_cash"
    assert _step(account, "option_total_cash_change")["amount"] == -200.0
    assert _step(account, "portfolio_and_other_cash_change")["amount"] == 150.0
    assert _step(account, "reconciliation_residual")["amount"] == 0.0
    assert all("assets" not in item["key"] for item in account["steps"])
    assert result["source"]["portfolio"] == {
        "service": "portfolio-management",
        "transport": "loopback_http",
        "capability": "portfolio_cash_facts",
        "status": "not_onboarded",
        "endpoint": None,
    }
    assert "### MTD 现金余额桥" in result["fallback_text"]


def test_cash_bridge_preserves_all_option_and_assignment_cash_components_as_evidence() -> None:
    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx")},
    )

    components = result["accounts"][0]["option_cash_evidence"]["components"]
    assert set(components) == {
        "option_trade_cash_gross",
        "option_fee_cash",
        "stock_settlement_cash_gross",
        "stock_settlement_fee_cash",
        "assigned_stock_sale_cash_gross",
        "assigned_stock_sale_fee_cash",
    }
    assert components["stock_settlement_fee_cash"]["cny"] == -2.0
    assert components["assigned_stock_sale_fee_cash"]["cny"] == -1.0
    assert result["accounts"][0]["option_cash_evidence"]["evidence_fact_ids"] == [
        "cash-event-1",
        "fee-1",
    ]


def test_partial_option_cash_is_not_used_or_replaced_with_zero() -> None:
    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx", cash=-200.0, status="partial")},
    )

    account = result["accounts"][0]
    assert result["status"] == "partial"
    assert account["option_cash_evidence"]["amount_cny"] == -200.0
    assert account["option_cash_evidence"]["usable"] is False
    assert _step(account, "option_total_cash_change")["amount"] is None
    assert _step(account, "portfolio_and_other_cash_change")["amount"] is None
    assert "不按 0 处理" in result["fallback_text"]


def test_reported_period_cash_change_mismatch_is_explicit_residual() -> None:
    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx", period_change=-40.0)},
        option_reports_by_account={"lx": _report("lx")},
    )

    account = result["accounts"][0]
    assert account["status"] == "partial"
    assert account["calculation"]["period_cash_change_basis"] == "portfolio_cash_facts"
    assert account["reconciliation"]["status"] == "mismatch"
    assert account["reconciliation"]["difference"] == 10.0
    assert _step(account, "reconciliation_residual")["amount"] == -10.0


def test_cash_bridge_requires_aligned_option_period_and_matching_account_end_dates() -> None:
    misaligned = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": _report("lx", end_date="2026-07-15")},
    )
    combined = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx", "sy"],
        cash_facts_by_account={
            "lx": _facts("lx", end_date="2026-07-16"),
            "sy": _facts("sy", end_date="2026-07-15"),
        },
        option_reports_by_account={
            "lx": _report("lx", end_date="2026-07-16"),
            "sy": _report("sy", end_date="2026-07-15"),
        },
    )

    assert misaligned["accounts"][0]["option_cash_evidence"]["reason"] == "option_performance_period_mismatch"
    assert _step(misaligned["accounts"][0], "option_total_cash_change")["amount"] is None
    assert combined["combined"]["reason"] == "account_end_date_mismatch"


def test_cash_bridge_requires_exact_source_account_and_reporting_timezone() -> None:
    wrong_facts = _facts("sy")
    utc_facts = _facts("lx")
    utc_facts["period"]["timezone"] = "UTC"
    aggregate_report = _report("lx")
    aggregate_report["scope"]["account"] = None
    utc_report = _report("lx")
    utc_report["period"]["reporting_timezone"] = "UTC"

    wrong_facts_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": wrong_facts},
        option_reports_by_account={"lx": _report("lx")},
    )
    facts_timezone_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": utc_facts},
        option_reports_by_account={"lx": _report("lx")},
    )
    aggregate_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": aggregate_report},
    )
    timezone_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": utc_report},
    )

    assert wrong_facts_result["accounts"][0]["reason"] == "portfolio_cash_account_mismatch"
    assert facts_timezone_result["accounts"][0]["reason"] == "portfolio_cash_timezone_mismatch"
    assert aggregate_result["accounts"][0]["option_cash_evidence"]["reason"] == "option_performance_account_mismatch"
    assert timezone_result["accounts"][0]["option_cash_evidence"]["reason"] == "option_performance_timezone_mismatch"


def test_cash_bridge_does_not_use_observed_metric_from_partial_report() -> None:
    report = _report("lx")
    report["quality"] = {"status": "partial", "warnings": ["source_conflict:bad-sale"]}

    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx")},
        option_reports_by_account={"lx": report},
    )

    account = result["accounts"][0]
    assert account["option_cash_evidence"]["status"] == "observed"
    assert account["option_cash_evidence"]["usable"] is False
    assert _step(account, "option_total_cash_change")["amount"] is None


def test_missing_cash_balance_or_non_cny_facts_are_unavailable() -> None:
    missing = _facts("lx")
    del missing["amounts"]["opening_cash"]
    missing_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": missing},
        option_reports_by_account={"lx": _report("lx")},
    )
    currency_result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": _facts("lx", currency="USD")},
        option_reports_by_account={"lx": _report("lx")},
    )

    assert missing_result["status"] == "unavailable"
    assert missing_result["accounts"][0]["reason"] == "portfolio_cash_amounts_incomplete"
    assert missing_result["accounts"][0]["steps"] == []
    assert currency_result["accounts"][0]["reason"] == "portfolio_cash_currency_not_cny"


def test_invalid_reported_period_cash_change_fails_closed() -> None:
    facts = _facts("lx")
    facts["amounts"]["period_cash_change"] = "unknown"

    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-07",
        accounts=["lx"],
        cash_facts_by_account={"lx": facts},
        option_reports_by_account={"lx": _report("lx")},
    )

    assert result["status"] == "unavailable"
    assert result["accounts"][0]["reason"] == "portfolio_cash_period_change_invalid"
    assert result["accounts"][0]["steps"] == []
