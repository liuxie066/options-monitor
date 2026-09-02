from src.application.portfolio_cash_bridge import build_portfolio_cash_bridge


def test_cash_bridge_keeps_public_route_but_is_explicitly_unavailable() -> None:
    result = build_portfolio_cash_bridge(
        period="mtd",
        as_of_month="2026-09",
        accounts=["lx", "sy"],
        observed_at="2026-09-02T00:00:00+00:00",
    )

    assert result["schema_version"] == "portfolio.cash_bridge.v1"
    assert result["status"] == "unavailable"
    assert result["reason"] == "portfolio_cash_facts_not_onboarded"
    assert [row["account"] for row in result["accounts"]] == ["lx", "sy"]
    assert all(row["status"] == "unavailable" for row in result["accounts"])
    assert result["source"]["option_cash"] == {
        "status": "unavailable",
        "source": None,
        "reason": "combined_option_assignment_cash_source_required",
    }
    assert result["combined"]["steps"] == []
    assert "不能替代" in result["fallback_text"]


def test_cash_bridge_never_accepts_option_performance_as_cash_evidence() -> None:
    result = build_portfolio_cash_bridge(
        period="ytd",
        as_of_month="2026-09",
        accounts=["lx"],
    )

    evidence = result["accounts"][0]["option_cash_evidence"]
    assert evidence["status"] == "unavailable"
    assert evidence["source"] is None
    assert evidence["reason"] == "combined_option_assignment_cash_source_required"
