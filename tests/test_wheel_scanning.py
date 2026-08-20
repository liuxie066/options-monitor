from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import phase2_opening_row
from src.application.wheel import (
    build_shared_coverage_facts,
    finalize_wheel_capacity,
    run_wheel_call_scan,
)
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


AS_OF = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)


def _read_model() -> dict:
    return {
        "account": "lx",
        "batches": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": "stock-1",
                "lifecycle_status": "active",
                "integrity_status": "trusted",
                "phase": "ready",
                "shares_remaining": 100,
                "batch_generation_hash": "a" * 64,
                "projection_hash": "b" * 64,
            }
        ],
        "assigned_stock_projection": {
            "_all_assigned_stock_lots": [
                {
                    "stock_lot_id": "stock-1",
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "currency": "USD",
                    "assigned_at_ms": 1_000,
                    "remaining_stock_cost_basis": 10_010,
                    "option_premium_attribution": 250,
                    "covered_call_realized_pnl": 100,
                    "assigned_stock_realized_pnl": 0,
                    "fee_evidence": [
                        {"component": "put_open_option_fee", "basis": "actual", "amount": 10},
                        {"component": "put_assignment_stock_fee", "basis": "estimated", "amount": 10},
                        {"component": "covered_call_open_option_fee", "basis": "actual", "amount": 5},
                    ],
                }
            ]
        },
    }


def _policy() -> dict:
    return {
        "enabled_for_new_lifecycle": True,
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": 0.30,
        "min_annualized_net_premium_return": 0.10,
        "min_net_premium_cny": 50,
        "max_spread_ratio": 0.40,
        "min_iv_rv_ratio": 1.10,
        "min_iv_minus_rv": 0.05,
    }


def test_wheel_scan_reuses_frozen_call_universe_and_builds_one_claim() -> None:
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-CALL-110",
            "multiplier": 100,
            "currency": "USD",
            "strike": 110,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": 0.35,
        }
    )
    result = run_wheel_call_scan(
        _read_model(),
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([row])}},
        {},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            )
        },
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )

    assert result["scope_results"][0]["reason_code"] == "candidates_found"
    assert len(result["raw_candidates"]["stock-1"]) == 1
    assert result["capacity_claims"][0]["requested_shares"] == 100


def test_wheel_scan_uses_decision_time_and_ignores_ineligible_sibling() -> None:
    model = _read_model()
    model["as_of_ms"] = int((AS_OF - timedelta(minutes=5)).timestamp() * 1000)
    valid = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-CALL-110",
            "multiplier": 100,
            "currency": "USD",
            "strike": 110,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": 0.35,
            "snapshot_received_at_utc": "2026-04-01T15:00:10Z",
        }
    )
    ineligible = {
        **valid,
        "contract_symbol": "NVDA-CALL-115",
        "bid": 0.0,
        "opening_contract_status": "ineligible",
        "opening_contract_reason_codes": ["option_no_current_bid"],
    }

    result = run_wheel_call_scan(
        model,
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([valid, ineligible])}},
        {},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            )
        },
        decision_time_ms=int((AS_OF + timedelta(minutes=1)).timestamp() * 1000),
    )

    decision = result["calculation_decisions"][0]
    reject = decision["opening_decision"]["rejects"][0]
    assert reject["reason"] == "contract_ineligible"
    assert "option_no_current_bid" in str(
        decision["normalized_input"]["opening_contract_reason_codes"]
    )
    assert result["scope_results"][0]["reason_code"] == "candidates_found"
    assert len(result["raw_candidates"]["stock-1"]) == 1


def test_shared_coverage_and_finalization_prioritize_wheel_over_ordinary_cc() -> None:
    model = {
        "account": "lx",
        "batches": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": "stock-1",
                "lifecycle_status": "active",
                "active_intent_reserved_shares": 0,
                "batch_generation_hash": "a" * 64,
                "projection_hash": "b" * 64,
            }
        ],
    }
    facts = build_shared_coverage_facts(
        account="lx",
        portfolio_context={
            "source_observed_at": "2026-04-01T00:00:00+00:00",
            "stocks_by_symbol": {
                "NVDA": {"shares": 200, "can_sell_qty": 200}
            },
        },
        option_context={
            "locked_shares_status": "available",
            "locked_shares_by_symbol": {"NVDA": 100},
            "locked_shares_unavailable_by_symbol": {},
            "prepared_authority": {"ledger_generation_sha256": "c" * 64},
        },
        wheel_read_model=model,
    )
    ordinary = [
        {
            "symbol": "NVDA",
            "contract_symbol": "NVDA-CC",
            "multiplier": 100,
            "max_new_contracts": 1,
        }
    ]
    captured = finalize_wheel_capacity(
        account="lx",
        wheel_read_model=model,
        wheel_scan={
            "scope_results": [
                {
                    "symbol": "NVDA",
                    "stock_lot_id": "stock-1",
                    "status": "completed",
                    "reason_code": "partial_data",
                }
            ],
            "raw_candidates": {
                "stock-1": [
                    {
                        "candidate_id": "wheel-candidate",
                        "symbol": "NVDA",
                        "multiplier": 100,
                    }
                ]
            },
            "capacity_claims": [
                {
                    "claim_id": "wheel:stock-1",
                    "strategy_family": "wheel",
                    "account": "lx",
                    "symbol": "NVDA",
                    "stock_lot_id": "stock-1",
                    "assignment_at_ms": 1,
                    "requested_contracts": 1,
                    "multiplier": 100,
                }
            ],
        },
        opening_call_candidates=ordinary,
        coverage_facts=facts,
    )

    assert captured["batches"][0]["granted_contracts"] == 1
    ordinary_allocation = next(
        row for row in captured["allocations"] if row["claim_id"] == "covered_call:NVDA"
    )
    assert ordinary_allocation["granted_contracts"] == 0
    assert captured["scope_results"][0]["candidate_count"] == 1
    assert captured["scope_results"][0]["reason_code"] == "partial_data"


def test_wheel_scan_disabled_keeps_batch_status_without_candidate_demand() -> None:
    result = run_wheel_call_scan(
        _read_model(),
        {**_policy(), "enabled_for_new_lifecycle": False},
        {"frames": {}},
        {},
        {},
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )

    assert result["scope_results"][0]["reason_code"] == "wheel_disabled"
    assert result["raw_candidates"] == {}
    assert result["capacity_claims"] == []


@pytest.mark.parametrize(
    "missing_field",
    ["term_matched_rv", "implied_volatility", "multiplier", "bid", "ask"],
)
def test_wheel_scan_marks_missing_candidate_evidence_unavailable(
    missing_field: str,
) -> None:
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-CALL-110",
            "multiplier": 100,
            "currency": "USD",
            "strike": 110,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": 0.35,
        }
    )
    row[missing_field] = None
    result = run_wheel_call_scan(
        _read_model(),
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([row])}},
        {},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            )
        },
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )

    assert result["scope_results"][0]["status"] == "unavailable"
    assert result["scope_results"][0]["reason_code"] == "data_unavailable"


def test_wheel_scan_preserves_partial_data_when_another_candidate_is_valid() -> None:
    valid = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-CALL-110",
            "multiplier": 100,
            "currency": "USD",
            "strike": 110,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": 0.35,
        }
    )
    unavailable = {**valid, "contract_symbol": "NVDA-CALL-115", "bid": None}
    result = run_wheel_call_scan(
        _read_model(),
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([valid, unavailable])}},
        {},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            )
        },
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )

    assert len(result["raw_candidates"]["stock-1"]) == 1
    assert result["scope_results"][0]["status"] == "completed"
    assert result["scope_results"][0]["reason_code"] == "partial_data"


def test_partial_capacity_grant_recomputes_final_candidate_economics() -> None:
    model = _read_model()
    model["batches"][0]["shares_remaining"] = 200
    model["assigned_stock_projection"]["_all_assigned_stock_lots"][0][
        "remaining_stock_cost_basis"
    ] = 20_020
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-CALL-110",
            "multiplier": 100,
            "currency": "USD",
            "strike": 110,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": 0.35,
        }
    )
    scan = run_wheel_call_scan(
        model,
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([row])}},
        {},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            ),
            "stock_exit_fee_fact_fn": lambda _stock, _candidate, shares: {
                "basis": "estimated",
                "amount": 10 if shares == 100 else 100,
            },
        },
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )
    captured = finalize_wheel_capacity(
        account="lx",
        wheel_read_model=model,
        wheel_scan=scan,
        opening_call_candidates=[],
        coverage_facts=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "status": "available",
                "shares_eligible": 100,
                "shares_locked": 0,
                "shares_reserved": 0,
                "capacity_identity_hash": "capacity-1",
            }
        ],
    )

    raw = captured["batches"][0]["raw_candidates"][0]
    final = captured["batches"][0]["final_candidate"]
    assert raw["contracts"] == 2
    assert final["granted_contracts"] == 1
    assert final["candidate_covered_shares"] == 100
    assert final["estimated_stock_exit_fees"] == 10
    assert final["candidate_call_net_premium"] * 2 == raw["candidate_call_net_premium"]
