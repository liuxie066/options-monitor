from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
        "snapshot_received_at_utc": datetime.now(timezone.utc).isoformat(),
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


@pytest.mark.parametrize(
    ("market", "currency", "mode"),
    [
        ("US", "USD", "put"),
        ("US", "USD", "call"),
        ("HK", "HKD", "put"),
        ("HK", "HKD", "call"),
    ],
)
def test_opening_policy_matrix_accepts_us_hk_put_and_call(
    market: str,
    currency: str,
    mode: str,
) -> None:
    strike = 120.0 if mode == "call" else 100.0
    fx_to_cny = 7.2 if currency == "USD" else 0.92
    source = _opening_row(
        mode=mode,
        currency=currency,
        market=market,
        strike=strike,
        bid=2.0,
        ask=2.1,
        price_tick=0.05,
    )
    metrics = calculate_opening_candidate_metrics(
        source,
        mode=mode,
        avg_cost=90.0 if mode == "call" else None,
        cny_per_currency_unit=fx_to_cny,
    )

    decision = evaluate_opening_candidate_policy(
        {
            **source,
            **metrics,
            "earnings_evidence_status": "ready",
            "earnings_has_event": False,
        },
        mode=mode,
    )

    assert decision["accepted"] is True
    assert decision["rejects"] == []


def test_market_closed_contract_fails_closed_before_candidate_calculation() -> None:
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(
            _opening_row(
                opening_contract_status="market_closed",
                underlier_observation_status="market_closed",
                underlier_observation_reason_code="market_closed",
            ),
            mode="put",
        )

    assert exc_info.value.reason == "evidence_unavailable"


def test_ineligible_contract_is_contract_ineligible_not_input_invalid() -> None:
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(
            _opening_row(
                opening_contract_status="ineligible",
                opening_contract_reason_codes=["option_non_standard"],
            ),
            mode="put",
        )

    assert exc_info.value.reason == "contract_ineligible"


def test_no_current_bid_ineligible_contract_is_contract_ineligible() -> None:
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(
            _opening_row(
                opening_contract_status="ineligible",
                opening_contract_reason_codes=["option_no_current_bid"],
            ),
            mode="put",
        )

    assert exc_info.value.reason == "contract_ineligible"
    assert exc_info.value.metric_value["reason_codes"] == [
        "option_no_current_bid"
    ]


def test_data_unavailable_contract_is_evidence_unavailable() -> None:
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(
            _opening_row(
                opening_contract_status="data_unavailable",
                opening_contract_reason_codes=["option_snapshot_stale"],
            ),
            mode="put",
        )

    assert exc_info.value.reason == "evidence_unavailable"


def test_snapshot_stale_at_decision_moment_fails_closed() -> None:
    """A snapshot fetched >300s before the decision moment must be rejected at
    decision time even though the fetch-time observation was ready."""
    stale_received = datetime(2026, 8, 6, 14, 50, 0, tzinfo=timezone.utc)
    decision_now = datetime(2026, 8, 6, 15, 0, 1, tzinfo=timezone.utc)  # 601s later
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(
            _opening_row(
                snapshot_received_at_utc=stale_received.isoformat(),
            ),
            mode="put",
            now_utc=decision_now,
        )

    assert exc_info.value.reason == "evidence_unavailable"


def test_snapshot_missing_receipt_fails_closed_at_decision_moment() -> None:
    row = _opening_row()
    row.pop("snapshot_received_at_utc", None)
    with pytest.raises(CandidateCalculationError) as exc_info:
        calculate_opening_candidate_metrics(row, mode="put")

    assert exc_info.value.reason == "evidence_unavailable"


def test_fresh_snapshot_at_decision_moment_passes() -> None:
    received = datetime(2026, 8, 6, 14, 59, 0, tzinfo=timezone.utc)
    decision_now = datetime(2026, 8, 6, 15, 0, 0, tzinfo=timezone.utc)  # 60s later
    metrics = calculate_opening_candidate_metrics(
        _opening_row(snapshot_received_at_utc=received.isoformat()),
        mode="put",
        now_utc=decision_now,
    )

    assert metrics["raw_mid"] == pytest.approx(1.005)


def test_offline_shadow_classifies_every_approved_policy_difference() -> None:
    from src.application.shadow_replay import compare_opening_policy_shadow

    source = _opening_row(
        bid=2.0,
        ask=2.1,
        price_tick=0.05,
    )
    metrics = calculate_opening_candidate_metrics(
        source,
        mode="put",
        cny_per_currency_unit=7.2,
    )
    opening = {
        **source,
        **metrics,
        "earnings_evidence_status": "ready",
        "earnings_has_event": False,
        "earnings_source": "opend",
        "rank": 1,
    }
    legacy = {
        **opening,
        "status": "accepted",
        "max_dte": 45,
        "max_strike": 95.0,
        "sell_limit": 2.0,
        "annualized_return": 0.20,
        "cash_basis": 10_000.0,
        "realized_volatility_estimate": 0.25,
        "event_source": "yfinance",
        "max_new_contracts": 2,
        "rank": 2,
        "strategy_score": 0.8,
    }

    comparison = compare_opening_policy_shadow(
        legacy_candidate=legacy,
        opening_candidate=opening,
        mode="put",
    )

    assert comparison["opening"]["accepted"] is True
    assert comparison["promotion_ready"] is True
    assert comparison["unclassified_differences"] == []
    assert {item["category"] for item in comparison["differences"]} == {
        "recall_boundary",
        "fee_and_tick",
        "return_basis",
        "realized_volatility",
        "earnings",
        "capacity",
        "ranking",
    }

    strict = compare_opening_policy_shadow(
        legacy_candidate={**legacy, "unexpected_policy_metric": 1},
        opening_candidate=opening,
        mode="put",
    )
    assert strict["unclassified_differences"] == [
        "field:unexpected_policy_metric"
    ]
    assert strict["promotion_ready"] is False


def test_offline_shadow_blocks_unexplained_acceptance_change() -> None:
    from src.application.shadow_replay import compare_opening_policy_shadow

    source = _opening_row(bid=2.0, ask=2.1, price_tick=0.05)
    metrics = calculate_opening_candidate_metrics(source, mode="put")
    opening = {
        **source,
        **metrics,
        "earnings_evidence_status": "ready",
        "earnings_has_event": False,
        "spread_ratio": 0.50,
    }

    legacy = {
        **opening,
        "status": "accepted",
        "annualized_return": opening["period_net_return_on_cash_basis"],
        "cash_basis": opening["net_cash_basis"],
        "realized_volatility_estimate": opening["term_matched_rv"],
        "strategy_score": opening["period_net_return_on_cash_basis"],
    }
    comparison = compare_opening_policy_shadow(
        legacy_candidate=legacy,
        opening_candidate=opening,
        mode="put",
    )

    assert comparison["opening"]["accepted"] is False
    assert comparison["differences"] == []
    assert comparison["unclassified_differences"] == [
        "acceptance_change_without_classified_evidence"
    ]
    assert comparison["promotion_ready"] is False


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
