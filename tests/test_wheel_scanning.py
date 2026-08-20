from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from conftest import phase2_opening_row
from src.application.wheel import run_wheel_call_scan
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
        int(AS_OF.timestamp() * 1000),
    )

    assert result["scope_results"][0]["reason_code"] == "candidates_found"
    assert len(result["raw_candidates"]["stock-1"]) == 1
    assert result["capacity_claims"][0]["requested_shares"] == 100


def test_wheel_scan_disabled_keeps_batch_status_without_candidate_demand() -> None:
    result = run_wheel_call_scan(
        _read_model(),
        {**_policy(), "enabled_for_new_lifecycle": False},
        {"frames": {}},
        {},
        {},
        int(AS_OF.timestamp() * 1000),
    )

    assert result["scope_results"][0]["reason_code"] == "wheel_disabled"
    assert result["raw_candidates"] == {}
    assert result["capacity_claims"] == []
