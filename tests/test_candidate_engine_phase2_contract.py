from __future__ import annotations

from domain.domain.engine import (
    CandidateCalculationError,
    calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy,
    explain_candidate_rank,
    rank_candidate_rows,
)


def _opening_row(*, mode: str = "put", currency: str = "USD", **overrides):  # type: ignore[no-untyped-def]
    market = "HK" if currency == "HKD" else "US"
    owner = "HK.00700" if market == "HK" else "US.NVDA"
    row = {
        "symbol": "0700.HK" if market == "HK" else "NVDA",
        "market": market,
        "option_type": mode,
        "expiration": "2026-09-18",
        "dte": 43,
        "contract_symbol": f"{owner}260918{mode[0].upper()}00100000",
        "currency": currency,
        "strike": 100.0,
        "spot": 110.0,
        "bid": 1.00,
        "ask": 1.01,
        "price_tick": 0.05,
        "implied_volatility": 0.30,
        "term_matched_rv": 0.20,
        "term_matched_rv_status": "ready",
        "term_matched_rv_reason": None,
        "underlier_observation_status": "ready",
        "underlier_observation_reason_code": None,
        "option_standard_type": "STANDARD",
        "stock_owner": owner,
        "stock_type": "DRVT",
        "chain_multiplier": 100,
        "snapshot_multiplier": 100,
        "multiplier": 100,
        "opening_contract_status": "ready",
        "opening_contract_reason_codes": "",
        "max_new_contracts": 1,
        "covered_contracts_available": 1,
    }
    row.update(overrides)
    return row


def test_sell_put_uses_tick_rounded_wait_price_and_net_cash_period_return() -> None:
    metrics = calculate_opening_candidate_metrics(
        _opening_row(),
        mode="put",
        cny_per_currency_unit=7.2,
    )

    assert metrics["raw_mid"] == 1.005
    assert metrics["sell_limit"] == 1.05
    assert metrics["gross_premium"] == 105.0
    assert metrics["fee_schedule_version"] == "futu_option_sell_fee.v1"
    assert metrics["fee_basis"] == "futu_us_candidate_upper_bound_2026-08-06"
    assert metrics["assignment_notional"] == 10000.0
    assert metrics["net_cash_basis"] == metrics["assignment_notional"] - metrics["net_premium"]
    assert metrics["period_net_return_on_cash_basis"] == round(
        metrics["net_premium"] / metrics["net_cash_basis"],
        10,
    )
    assert metrics["net_premium_cny"] == round(metrics["net_premium"] * 7.2, 6)


def test_covered_call_uses_current_market_value_and_same_hk_formula_contract() -> None:
    metrics = calculate_opening_candidate_metrics(
        _opening_row(
            mode="call",
            currency="HKD",
            symbol="0700.HK",
            strike=120.0,
            spot=110.0,
        ),
        mode="call",
        avg_cost=90.0,
    )

    assert metrics["current_market_value"] == 11000.0
    assert metrics["period_net_premium_return"] == round(
        metrics["net_premium"] / 11000.0,
        10,
    )
    assert metrics["fee_basis"].startswith("futu_hk_")
    assert metrics["if_exercised_total_return"] > 0


def test_candidate_calculation_never_defaults_multiplier_or_legacy_rv() -> None:
    row = _opening_row(multiplier=None, term_matched_rv=None, realized_volatility_estimate=0.20)
    try:
        calculate_opening_candidate_metrics(row, mode="put")
    except CandidateCalculationError as exc:
        assert exc.reason == "multiplier_missing_or_invalid"
    else:
        raise AssertionError("missing multiplier must fail closed")


def test_candidate_calculation_fails_only_the_contract_when_fee_schedule_is_unavailable() -> None:
    try:
        calculate_opening_candidate_metrics(
            _opening_row(currency="CNY"),
            mode="put",
        )
    except CandidateCalculationError as exc:
        assert exc.reason == "option_fee_estimate_unavailable"
        assert exc.metric_value == "CNY"
    else:
        raise AssertionError("unsupported candidate fee schedule must fail closed")


def test_scan_adapters_use_the_same_canonical_calculation() -> None:
    import pandas as pd

    from src.application.scan_sell_call import compute_metrics as compute_call_metrics
    from src.application.scan_sell_put import compute_metrics as compute_put_metrics

    put_row = _opening_row(bid=1.0, ask=1.01, price_tick=0.05)
    put_domain = calculate_opening_candidate_metrics(put_row, mode="put")
    put_scan = compute_put_metrics(pd.Series(put_row))
    assert put_scan is not None

    call_row = _opening_row(
        mode="call",
        strike=120.0,
        bid=1.0,
        ask=1.01,
        price_tick=0.05,
    )
    call_domain = calculate_opening_candidate_metrics(
        call_row,
        mode="call",
        avg_cost=90.0,
    )
    call_scan = compute_call_metrics(pd.Series(call_row), avg_cost=90.0)
    assert call_scan is not None

    canonical_fields = {
        "raw_mid",
        "raw_spread",
        "sell_limit",
        "estimated_full_sell_fees",
        "net_premium",
        "term_matched_rv",
        "iv_rv_ratio",
        "iv_minus_rv",
    }
    assert {key: put_scan[key] for key in canonical_fields} == {
        key: put_domain[key] for key in canonical_fields
    }
    assert {key: call_scan[key] for key in canonical_fields} == {
        key: call_domain[key] for key in canonical_fields
    }
    assert call_scan["period_net_premium_return"] == call_domain[
        "period_net_premium_return"
    ]


def test_common_policy_uses_cny_iv_rv_spread_and_opend_earnings_only() -> None:
    metrics = calculate_opening_candidate_metrics(
        _opening_row(bid=2.0, ask=2.2, price_tick=0.01),
        mode="put",
        cny_per_currency_unit=7.2,
    )
    row = {
        **_opening_row(bid=2.0, ask=2.2, price_tick=0.01),
        **metrics,
        "earnings_evidence_status": "ready",
        "earnings_has_event": False,
    }
    assert evaluate_opening_candidate_policy(row, mode="put")["accepted"] is True

    rejected = evaluate_opening_candidate_policy(
        {**row, "earnings_has_event": True, "earnings_event_dates": "2026-09-01"},
        mode="put",
    )
    assert rejected["rejects"][0]["reason"] == "risk_earnings_event"


def test_covered_call_rank_uses_anchored_period_band_then_higher_strike() -> None:
    rows = [
        {
            "symbol": "NVDA",
            "contract_symbol": "HIGH_RETURN_LOW_STRIKE",
            "period_net_premium_return": 0.0100,
            "strike": 110,
            "spread_ratio": 0.03,
            "open_interest": 100,
            "net_premium": 100,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "NEAR_RETURN_HIGH_STRIKE",
            "period_net_premium_return": 0.0081,
            "strike": 120,
            "spread_ratio": 0.04,
            "open_interest": 0,
            "net_premium": 90,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "NEXT_BAND",
            "period_net_premium_return": 0.0079,
            "strike": 130,
            "spread_ratio": 0.01,
            "open_interest": 1000,
            "net_premium": 80,
        },
    ]

    ranked = rank_candidate_rows(rows, mode="call")
    assert [row["contract_symbol"] for row in ranked] == [
        "NEAR_RETURN_HIGH_STRIKE",
        "HIGH_RETURN_LOW_STRIKE",
        "NEXT_BAND",
    ]
    explanation = explain_candidate_rank(ranked[0], mode="call")
    assert explanation["primary_drivers"] == ["period_net_premium_return"]
    assert "持有期净权利金收益分带" in explanation["rank_reason"]
