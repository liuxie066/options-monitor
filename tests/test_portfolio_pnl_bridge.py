from src.application.portfolio_pnl_bridge import build_portfolio_pnl_bridge


def _facts(account: str = "lx") -> dict:
    return {
        "status": "ok",
        "account": account,
        "period": {
            "kind": "mtd",
            "requested_as_of_month": "2026-09",
            "timezone": "Asia/Shanghai",
            "end_date": "2026-09-02",
        },
        "amounts": {
            "currency": "CNY",
            "opening_assets": 1000,
            "external_cash_flow": 100,
            "period_pnl": 50,
            "ending_assets": 1150,
        },
    }


def _step(row: dict, key: str) -> dict:
    return next(item for item in row["steps"] if item["key"] == key)


def test_pnl_bridge_keeps_portfolio_fact_but_option_pnl_is_unavailable() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-09",
        accounts=["lx"],
        capital_facts_by_account={"lx": _facts()},
        observed_at="2026-09-02T00:00:00+00:00",
    )

    row = result["accounts"][0]
    assert result["schema_version"] == "portfolio.pnl_bridge.v1"
    assert result["status"] == "partial"
    assert row["calculation"]["portfolio_period_pnl"] == 50.0
    assert row["option_pnl_evidence"] == {
        "status": "unavailable",
        "amount_cny": None,
        "usable": False,
        "source": None,
        "reason": "authoritative_option_pnl_source_unavailable",
    }
    assert _step(row, "option_period_total_net_pnl")["status"] == "unavailable"
    assert _step(row, "portfolio_and_other_pnl")["amount"] is None
    assert row["reconciliation"]["status"] == "ok"
    assert result["source"]["option_pnl"]["source"] is None
    assert "不以期权净现金流替代" in result["fallback_text"]


def test_pnl_bridge_preserves_portfolio_fact_fail_closed_behavior() -> None:
    result = build_portfolio_pnl_bridge(
        period="mtd",
        as_of_month="2026-09",
        accounts=["lx"],
        capital_facts_by_account={
            "lx": {
                "status": "unavailable",
                "reason": "portfolio_capital_facts_unavailable",
            }
        },
    )

    assert result["status"] == "unavailable"
    assert result["accounts"][0]["reason"] == "portfolio_capital_facts_unavailable"
    assert result["accounts"][0]["option_pnl_evidence"]["status"] == "unavailable"
    assert result["combined"]["status"] == "unavailable"
