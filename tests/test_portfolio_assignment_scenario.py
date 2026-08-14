from __future__ import annotations

from domain.domain.portfolio_assignment_scenario import project_assignment_scenario


def _evidence(
    *,
    holdings=None,
    quotes=None,
    status="complete",
    warnings=None,
    freshness_status="fresh",
    trust_status="trusted",
):
    return {
        "schema_version": "portfolio.valuation_evidence.v1",
        "success": True,
        "status": status,
        "freshness": {
            "status": freshness_status,
            "trust_status": trust_status,
            "observed_at_utc": "2026-07-24T01:00:00+00:00",
            "dataset_ids": [
                "pm.holdings_quantity",
                "pm.prices",
                "pm.fx",
            ],
            "reason_codes": [],
        },
        "retrieved_at_utc": "2026-07-24T01:00:01+00:00",
        "scope": {"accounts": ["lx"], "reporting_currency": "CNY"},
        "snapshot": {
            "snapshot_id": "valuation-test",
            "observed_at": "2026-07-24T01:00:00+00:00",
        },
        "holdings": list(holdings or []),
        "quotes": list(quotes or []),
        "account_status": [{"account": "lx", "status": status}],
        "warnings": list(warnings or []),
    }


def _quote(
    code,
    *,
    currency="HKD",
    price=400,
    cny_price=368,
    fx=0.92,
    source="futu",
):
    return {
        "code": code,
        "status": "observed",
        "currency": currency,
        "price_native": price,
        "price_cny": cny_price,
        "exchange_rate_to_cny": fx,
        "source": source,
        "source_chain": [source],
        "observed_at": "2026-07-24T01:00:00+00:00",
        "is_stale": False,
    }


def _option(
    record_id,
    *,
    option_type,
    contracts=1,
    symbol="0700.HK",
    strike=350,
    currency="HKD",
    expiration="2026-08-28",
    side="short",
    status="open",
):
    return {
        "record_id": record_id,
        "account": "lx",
        "broker": "富途证券(香港)",
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "status": status,
        "contracts_open": contracts,
        "multiplier": 100,
        "strike": strike,
        "currency": currency,
        "expiration_ymd": expiration,
    }


def _snapshot():
    return {
        "snapshot_id": "assignment-test",
        "observed_at": "2026-07-24T01:00:02+00:00",
        "portfolio_observed_at": "2026-07-24T01:00:00+00:00",
        "options_observed_at": "2026-07-24T01:00:02+00:00",
        "max_source_skew_seconds": "2.000000",
    }


def test_projects_put_and_call_with_cash_mmf_and_existing_holding():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "CNY-CASH",
                    "name": "人民币现金",
                    "asset_type": "cash",
                    "normalized_type": "cash",
                    "quantity": 100000,
                    "currency": "CNY",
                    "market_value_cny": 100000,
                },
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "CNY-MMF",
                    "name": "货币基金",
                    "asset_type": "mmf",
                    "normalized_type": "cash",
                    "quantity": 20000,
                    "currency": "CNY",
                    "market_value_cny": 20000,
                },
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "0700.HK",
                    "name": "腾讯控股",
                    "asset_type": "hk_stock",
                    "normalized_type": "stock",
                    "quantity": 100,
                    "currency": "HKD",
                    "market_value_cny": 36800,
                },
            ],
            quotes=[
                _quote("0700.HK"),
                _quote("CNY-CASH", currency="CNY", price=1, cny_price=1, fx=1, source="fixed"),
                _quote("CNY-MMF", currency="CNY", price=1, cny_price=1, fx=1, source="fixed"),
            ],
        ),
        option_positions=[
            _option("put-1", option_type="put", strike=350),
            _option("call-1", option_type="call", contracts=2, strike=450),
            _option("long-put", option_type="put", side="long"),
        ],
        snapshot=_snapshot(),
    )

    assert result["schema_version"] == "portfolio.assignment_scenario.v1"
    assert result["status"] == "partial"
    assert result["scope"]["include_long_options"] is False
    assert result["summary"] == {
        "assignment_count": 2,
        "short_put_count": 1,
        "short_call_count": 1,
        "position_change_count": 1,
        "warning_count": 0,
    }
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] == "120000.00"
    assert result["cash_coverage"]["gross_put_requirement_cny"] == "32200.00"
    assert result["cash_coverage"]["call_assignment_inflow_cny"] == "82800.00"
    assert result["cash_coverage"]["total_fees_cny"] is None
    assert result["fee_summary"]["status"] == "partial"
    assert result["position_changes"][0]["opening_shares"] == "100"
    assert result["position_changes"][0]["put_assigned_shares"] == "100"
    assert result["position_changes"][0]["call_assigned_shares"] == "200"
    assert result["position_changes"][0]["ending_shares"] == "0"
    assert result["position_changes"][0]["liability_kind"] is None
    assert {row["record_id"] for row in result["assignments"]} == {"put-1", "call-1"}
    assert len(result["expiration_ladder"]) == 1
    assert result["distribution"]["status"] == "partial"
    assert result["distribution"]["gross_assets_cny"] is None


def test_uncovered_call_becomes_short_stock_liability_not_error():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "CNY-CASH",
                    "name": "现金",
                    "asset_type": "cash",
                    "normalized_type": "cash",
                    "quantity": 0,
                    "currency": "CNY",
                    "market_value_cny": 0,
                }
            ],
            quotes=[_quote("0700.HK")],
        ),
        option_positions=[
            _option("call-1", option_type="call", strike=450),
        ],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    change = result["position_changes"][0]
    assert change["ending_shares"] == "-100"
    assert change["ending_market_value_cny"] == "-36800.00"
    assert change["liability_kind"] == "short_stock"
    liabilities = result["distribution"]["liabilities"]
    assert any(row["code"] == "0700.HK" and row["liability_cny"] == "36800.00" for row in liabilities)
    assert result["cash_coverage"]["terminal_funding_gap_cny"] is None


def test_us_assignment_uses_unified_stock_calculator_but_fails_closed_on_missing_rule():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "USD-CASH",
                    "name": "美元现金",
                    "asset_type": "cash",
                    "normalized_type": "cash",
                    "quantity": 20000,
                    "currency": "USD",
                    "market_value_cny": 144000,
                }
            ],
            quotes=[
                _quote(
                    "NVDA",
                    currency="USD",
                    price=120,
                    cny_price=864,
                    fx=7.2,
                    source="finnhub",
                )
            ],
        ),
        option_positions=[
            _option(
                "put-us",
                option_type="put",
                symbol="NVDA",
                strike=100,
                currency="USD",
            )
        ],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    fee = result["fee_summary"]["items"][0]
    assert fee["calculator"] == "domain.domain.fee_calc.calc_futu_stock_fee"
    assert fee["estimated_stock_fee_native"] is not None
    assert fee["status"] == "missing"
    assert fee["reason"] == "us_assignment_fee_rule_not_explicit"
    assert result["cash_coverage"]["ending_cash_gross_cny"] == "72000.00"
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] is None
    assert result["distribution"]["net_assets_cny"] is None


def test_hk_assignment_stock_fee_estimate_fails_closed_without_account_plan():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "CNY-CASH",
                    "name": "现金",
                    "asset_type": "cash",
                    "normalized_type": "cash",
                    "quantity": 100000,
                    "currency": "CNY",
                    "market_value_cny": 100000,
                }
            ],
            quotes=[_quote("0700.HK")],
        ),
        option_positions=[_option("put-hk", option_type="put", strike=350)],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    fee = result["fee_summary"]["items"][0]
    assert fee["calculator"] == "domain.domain.fee_calc.calc_futu_hk_terminal_fee"
    assert fee["estimated_stock_fee_native"] is not None
    assert fee["estimated_stock_fee_cny"] is not None
    assert fee["status"] == "missing"
    assert fee["fee_native"] is None
    assert fee["fee_cny"] is None
    assert fee["reason"] == "hk_account_fee_plan_missing"
    assert result["fee_summary"]["status"] == "partial"
    assert result["fee_summary"]["total_fees_cny"] is None
    assert result["cash_coverage"]["total_fees_cny"] is None
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] is None
    assert result["account_breakdown"][0]["ending_cash_net_estimated_cny"] is None


def test_hk_assignment_rejects_unvalidated_account_fee_plan_payload():
    option = _option("put-hk", option_type="put", strike=350)
    option["account_fee_plan"] = {
        "commission_free": False,
        "platform_fee": 15.0,
        "fee_plan_ref": "futu_hk_standard_fixed",
    }
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[],
            quotes=[_quote("0700.HK")],
        ),
        option_positions=[option],
        snapshot=_snapshot(),
    )

    fee = result["fee_summary"]["items"][0]
    assert fee["calculator"] == "domain.domain.fee_calc.calc_futu_hk_terminal_fee"
    assert fee["status"] == "missing"
    assert fee["fee_native"] is None
    assert fee["fee_plan_ref"] is None
    assert fee["estimated_stock_fee_native"] == "64.945000"
    assert fee["estimated_basis"] == "standard_fixed_non_commission_free"
    assert result["status"] == "partial"
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] is None


def test_missing_explicit_fx_never_infers_from_native_and_cny_prices():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[],
            quotes=[
                {
                    "code": "NVDA",
                    "currency": "USD",
                    "price_native": 120,
                    "price_cny": 864,
                    "exchange_rate_to_cny": None,
                    "source": "test",
                }
            ],
        ),
        option_positions=[
            _option(
                "put-us",
                option_type="put",
                symbol="NVDA",
                strike=100,
                currency="USD",
            )
        ],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    assert result["assignments"][0]["fx_to_cny"] is None
    assert result["assignments"][0]["principal_cny"] is None
    assert result["cash_coverage"]["gross_put_requirement_cny"] is None
    assert result["distribution"]["net_assets_cny"] is None
    assert any("fx_evidence_missing" in warning for warning in result["warnings"])


def test_missing_spot_keeps_strike_cash_projection_when_fx_is_explicit():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[],
            quotes=[
                {
                    "code": "0700.HK",
                    "currency": "HKD",
                    "price_native": None,
                    "price_cny": None,
                    "exchange_rate_to_cny": 0.92,
                    "source": "test",
                }
            ],
        ),
        option_positions=[_option("put-hk", option_type="put", strike=350)],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    assert result["cash_coverage"]["gross_put_requirement_cny"] == "32200.00"
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] is None
    assert result["distribution"]["status"] == "partial"
    assert result["distribution"]["net_assets_cny"] is None


def test_long_options_are_not_read_into_the_projection():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[
                {
                    "account": "lx",
                    "broker": "富途",
                    "code": "CNY-CASH",
                    "name": "现金",
                    "asset_type": "cash",
                    "normalized_type": "cash",
                    "quantity": 1000,
                    "currency": "CNY",
                    "market_value_cny": 1000,
                }
            ],
            quotes=[],
        ),
        option_positions=[
            _option("long-put", option_type="put", side="long"),
            _option("closed-call", option_type="call", status="closed"),
        ],
        snapshot=_snapshot(),
    )

    assert result["status"] == "complete"
    assert result["summary"]["assignment_count"] == 0
    assert result["assignments"] == []
    assert result["position_changes"] == []
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] == "1000.00"
    assert result["distribution"]["net_assets_cny"] == "1000.00"


def test_expired_open_short_is_projected_but_marks_result_partial():
    option = _option("expired-put", option_type="put")
    option["state_warning"] = "expired_position_marked_open"

    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(
            holdings=[],
            quotes=[_quote("0700.HK")],
        ),
        option_positions=[option],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    assert result["summary"]["assignment_count"] == 1
    assert any("expired_position_marked_open" in warning for warning in result["warnings"])


def test_missing_freshness_fails_closed_instead_of_projecting_zero_portfolio():
    evidence = _evidence()
    evidence.pop("freshness")

    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=evidence,
        option_positions=[],
        snapshot=_snapshot(),
    )

    assert result["status"] == "unavailable"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] is None
    assert result["distribution"]["net_assets_cny"] is None
    assert result["portfolio_evidence"]["freshness_status"] == "unavailable"


def test_stale_evidence_is_propagated_as_partial():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(freshness_status="stale"),
        option_positions=[],
        snapshot=_snapshot(),
    )

    assert result["status"] == "partial"
    assert result["portfolio_evidence"]["freshness_status"] == "stale"
    assert any("stale" in warning for warning in result["warnings"])


def test_untrusted_evidence_is_unavailable():
    result = project_assignment_scenario(
        accounts=["lx"],
        portfolio_evidence=_evidence(trust_status="untrusted"),
        option_positions=[],
        snapshot=_snapshot(),
    )

    assert result["status"] == "unavailable"
    assert result["portfolio_evidence"]["trust_status"] == "untrusted"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] is None
