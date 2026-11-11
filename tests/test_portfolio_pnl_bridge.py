import pytest

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


@pytest.mark.parametrize("period", ["mtd", "ytd"])
@pytest.mark.parametrize("included", [["lx", "sy"], ["lx"], []])
def test_combined_declares_account_coverage(period, included) -> None:
    accounts = ["lx", "sy"]
    facts = {}
    for account in accounts:
        fact = _facts(account)
        fact["period"]["kind"] = period
        if account not in included:
            fact = {"status": "unavailable", "reason": "synthetic capital facts unavailable"}
        facts[account] = fact
    result = build_portfolio_pnl_bridge(
        period=period, as_of_month="2026-09", accounts=accounts,
        capital_facts_by_account=facts, observed_at="2026-09-02T00:00:00+00:00",
    )
    combined = result["combined"]
    assert combined["status"] == ("partial" if included else "unavailable")
    if included:
        assert _step(combined, "ending_assets")["amount"] == 1150 * len(included)
        assert _step(combined, "ending_assets")["status"] == "observed"
        assert combined["option_pnl_evidence"] == {
            "status": "unavailable", "source": None, "reason": "authoritative_option_pnl_source_unavailable",
        }
    else:
        assert combined["steps"] == [] and combined["reason"] == "account_bridge_unavailable"
    assert [row["account"] for row in result["accounts"] if row["status"] != "unavailable"] == included
    assert combined["account_coverage"] == {
        "status": "complete" if len(included) == len(accounts) else "partial" if included else "unavailable",
        "included_accounts": included,
        "missing_accounts": [account for account in accounts if account not in included],
    }


@pytest.mark.parametrize("period", ["mtd", "ytd"])
def test_combined_coverage_does_not_override_end_date_guard(period) -> None:
    facts = {account: _facts(account) for account in ("lx", "sy")}
    for fact in facts.values():
        fact["period"]["kind"] = period
    facts["sy"]["period"]["end_date"] = "2026-09-01"
    combined = build_portfolio_pnl_bridge(
        period=period, as_of_month="2026-09", accounts=["lx", "sy"], capital_facts_by_account=facts,
    )["combined"]
    assert combined["status"] == "unavailable" and combined["reason"] == "account_end_date_mismatch"
    assert combined["steps"] == []
    assert combined["account_coverage"] == {
        "status": "complete", "included_accounts": ["lx", "sy"], "missing_accounts": [],
    }


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
