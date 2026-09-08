from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import phase2_opening_row
from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.wheel import (
    build_shared_coverage_facts,
    finalize_wheel_capacity,
    run_wheel_call_scan,
)
from src.application.wheel.capacity import (
    finalize_wheel_put_capacity,
    revalidate_selected_wheel_put_candidate,
)
from src.application.wheel.scanning import run_wheel_put_scan
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
                "monitoring_gate": "enabled",
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
        "min_abs_delta": 0.25,
        "max_abs_delta": 0.35,
        "min_open_interest": 0,
        "min_volume": 0,
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
                        "contracts": 1,
                        "accepted": True,
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


@pytest.mark.parametrize(
    "early_evaluations",
    [
        {},
        {"1": {"accepted": False, "contracts": 1, "multiplier": 100}},
    ],
)
def test_rejected_wheel_grant_recomputes_pool_without_regranting_later_claims(
    early_evaluations: dict,
) -> None:
    model = {
        "account": "lx",
        "batches": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": lot_id,
                "lifecycle_status": "active",
                "active_intent_reserved_shares": reserved,
                "assignment_at_ms": assignment_at,
                "batch_generation_hash": char * 64,
                "projection_hash": char.upper() * 64,
            }
            for lot_id, assignment_at, reserved, char in (
                ("early", 1, 100, "a"),
                ("late", 2, 0, "b"),
            )
        ],
    }
    wheel_scan = {
        "scope_results": [
            {
                "symbol": "NVDA",
                "stock_lot_id": lot_id,
                "status": "completed",
                "reason_code": "candidates_found",
            }
            for lot_id in ("late", "early")
        ],
        "raw_candidates": {
            "early": [
                {
                    "candidate_id": "candidate-early",
                    "symbol": "NVDA",
                    "contracts": 1,
                    "multiplier": 100,
                    "accepted": True,
                    "_grant_evaluations": early_evaluations,
                }
            ],
            "late": [
                {
                    "candidate_id": "candidate-late",
                    "symbol": "NVDA",
                    "contracts": 1,
                    "multiplier": 100,
                    "accepted": True,
                    "_grant_evaluations": {
                        "1": {"accepted": True, "contracts": 1, "multiplier": 100}
                    },
                }
            ],
        },
        "capacity_claims": [
            {
                "claim_id": f"wheel:{lot_id}",
                "strategy_family": "wheel",
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": lot_id,
                "assignment_at_ms": assignment_at,
                "requested_contracts": 1,
                "multiplier": 100,
            }
            for lot_id, assignment_at in (("late", 2), ("early", 1))
        ],
    }

    captured = finalize_wheel_capacity(
        account="lx",
        wheel_read_model=model,
        wheel_scan=wheel_scan,
        opening_call_candidates=[
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA-CC",
                "multiplier": 100,
                "max_new_contracts": 1,
            }
        ],
        coverage_facts=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "status": "available",
                "shares_eligible": 300,
                "shares_locked": 0,
                "shares_reserved": 100,
                "capacity_identity_hash": "capacity-1",
            }
        ],
    )
    allocations = {row["claim_id"]: row for row in captured["allocations"]}
    batches = {row["stock_lot_id"]: row for row in captured["batches"]}

    assert (
        allocations["wheel:early"]["granted_contracts"],
        allocations["wheel:early"]["capacity_before"],
        allocations["wheel:early"]["capacity_after"],
    ) == (0, 200, 200)
    assert allocations["wheel:early"]["allocation_reason"] == (
        "wheel_capacity_grant_candidate_rejected"
    )
    assert (
        allocations["wheel:late"]["granted_contracts"],
        allocations["wheel:late"]["capacity_before"],
        allocations["wheel:late"]["capacity_after"],
    ) == (1, 200, 100)
    assert (
        allocations["covered_call:NVDA"]["granted_contracts"],
        allocations["covered_call:NVDA"]["capacity_before"],
        allocations["covered_call:NVDA"]["capacity_after"],
    ) == (0, 100, 100)
    assert batches["early"]["granted_contracts"] == 0
    assert batches["early"]["final_candidate"] is None
    assert batches["early"]["allocation"] == allocations["wheel:early"]
    assert batches["early"]["reason_code"] == "wheel_capacity_grant_candidate_rejected"
    assert batches["late"]["granted_contracts"] == 1
    assert batches["late"]["final_candidate"]["candidate_id"] == "candidate-late"
    assert captured["allocation_hash"] == canonical_sha256(captured["allocations"])


def test_wheel_scan_disabled_keeps_batch_status_without_candidate_demand() -> None:
    model = _read_model()
    model["batches"][0]["monitoring_gate"] = "disabled"
    result = run_wheel_call_scan(
        model,
        _policy(),
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


def test_wheel_put_scan_and_account_cash_grant_are_direction_aware() -> None:
    model = {
        "account": "lx",
        "wheel_branches": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "wheel_branch_id": branch_id,
                "direction": "put",
                "lifecycle_status": "active",
                "integrity_status": "trusted",
                "phase": "ready",
                "monitoring_gate": "enabled",
                "remaining_contracts": 1,
                "multiplier": 100,
                "principal_anchor": 10_010,
                "realized_put_net_pnl_in_current_stage": 0,
                "currency": "USD",
                "branch_generation_hash": generation * 64,
                "projection_hash": projection * 64,
            }
            for branch_id, generation, projection in (
                ("branch-a", "a", "c"),
                ("branch-b", "b", "d"),
            )
        ],
    }
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-PUT-99",
            "multiplier": 100,
            "currency": "USD",
            "strike": 99,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": -0.30,
        }
    )
    converter = CurrencyConverter(
        ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
    )
    scan = run_wheel_put_scan(
        model,
        _policy(),
        {"frames": {"NVDA": pd.DataFrame([row])}},
        {
            "exchange_rate_converter": converter,
            "stock_assignment_fee_fact_fn": lambda _branch, _candidate, _shares: {
                "basis": "estimated",
                "amount": 10,
            },
        },
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )
    captured = finalize_wheel_put_capacity(
        account="lx",
        wheel_read_model=model,
        wheel_scan=scan,
        opening_put_candidates=[],
        cash_capacity_fact={
            "account": "lx",
            "status": "available",
            "cash_authority": {"status": "available", "logical_account": "lx"},
            "cash_authority_hash": "authority-1",
            "cash_by_currency": {"USD": 15_000},
            "cash_secured_by_currency": {},
            "wheel_intent_reservations": [],
            "fx_snapshot": {"rates": {}},
        },
        exchange_rate_converter=converter,
    )

    allocations = {
        row["wheel_branch_id"]: row for row in captured["allocations"]
    }
    batches = {row["wheel_branch_id"]: row for row in captured["batches"]}
    assert len(scan["capacity_claims"]) == 2
    assert allocations["branch-a"]["granted_contracts"] == 1
    assert allocations["branch-b"]["granted_contracts"] == 0
    assert batches["branch-a"]["final_candidate"]["direction"] == "put"
    assert batches["branch-a"]["final_candidate"]["cash_reservation_amount"] == 9_900
    assert batches["branch-b"]["final_candidate"] is None
    assert [(row["symbol"], row["direction"]) for row in captured["scope_results"]] == [
        ("NVDA", "put")
    ]


def test_wheel_pending_put_branch_remains_visible_without_required_data() -> None:
    model = {
        "account": "lx",
        "wheel_branches": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "wheel_branch_id": "pending-put",
                "direction": "put",
                "lifecycle_status": "pending_decision",
                "integrity_status": "trusted",
                "phase": "pending_decision",
                "monitoring_gate": "enabled",
                "remaining_contracts": 1,
                "branch_generation_hash": "a" * 64,
                "projection_hash": "b" * 64,
            }
        ],
    }

    scan = run_wheel_put_scan(
        model,
        _policy(),
        {"frames": {}},
        {},
        decision_time_ms=int(AS_OF.timestamp() * 1000),
    )

    assert scan["capacity_claims"] == []
    assert scan["scope_results"][0]["status"] == "not_applicable"
    assert scan["scope_results"][0]["reason_code"] == "wheel_pending_decision"


def test_wheel_put_revalidation_consumes_frozen_fx_rate_facts() -> None:
    allocation = revalidate_selected_wheel_put_candidate(
        cash_capacity_fact={
            "account": "lx",
            "status": "available",
            "cash_authority": {"status": "available", "logical_account": "lx"},
            "cash_authority_hash": "authority-1",
            "cash_by_currency": {"CNY": 70_000},
            "cash_secured_by_currency": {},
            "wheel_intent_reservations": [],
            "fx_snapshot": {
                "fx_rate_facts": [
                    {
                        "fact_id": "fx-usd-cny",
                        "base_currency": "USD",
                        "quote_currency": "CNY",
                        "rate": 7,
                        "effective_at_ms": 1_000,
                        "observed_at_ms": 1_001,
                        "revision": 1,
                    }
                ]
            },
        },
        final_candidate={
            "claim_id": "wheel:put:branch-a",
            "wheel_branch_id": "branch-a",
            "symbol": "NVDA",
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "granted_contracts": 1,
        },
    )

    assert allocation["allocation_status"] == "allocated"
    assert allocation["cash_reservation_amount"] == 10_000


def test_wheel_finalizers_preserve_homogeneous_scan_failure_reason() -> None:
    call_result = finalize_wheel_capacity(
        account="lx",
        wheel_read_model=_read_model(),
        wheel_scan={
            "scope_results": [
                {
                    "symbol": "NVDA",
                    "stock_lot_id": "stock-1",
                    "status": "failed",
                    "reason_code": "wheel_scan_failed",
                }
            ],
            "raw_candidates": {},
            "capacity_claims": [],
        },
        opening_call_candidates=[],
        coverage_facts=[],
    )
    put_result = finalize_wheel_put_capacity(
        account="lx",
        wheel_read_model={
            "account": "lx",
            "wheel_branches": [
                {
                    "account": "lx",
                    "symbol": "NVDA",
                    "wheel_branch_id": "put-1",
                    "direction": "put",
                    "branch_generation_hash": "a" * 64,
                    "projection_hash": "b" * 64,
                }
            ],
        },
        wheel_scan={
            "scope_results": [
                {
                    "symbol": "NVDA",
                    "wheel_branch_id": "put-1",
                    "status": "failed",
                    "reason_code": "wheel_scan_failed",
                }
            ],
            "raw_candidates": {},
            "capacity_claims": [],
        },
        opening_put_candidates=[],
        cash_capacity_fact={
            "account": "lx",
            "status": "available",
            "cash_authority": {"status": "available", "logical_account": "lx"},
            "cash_authority_hash": "authority-1",
            "cash_by_currency": {"USD": 15_000},
            "cash_secured_by_currency": {},
            "wheel_intent_reservations": [],
            "fx_snapshot": {"rates": {}},
        },
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
        ),
    )

    assert call_result["scope_results"][0]["reason_code"] == "wheel_scan_failed"
    assert put_result["scope_results"][0]["reason_code"] == "wheel_scan_failed"
