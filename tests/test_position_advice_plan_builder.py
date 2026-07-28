from __future__ import annotations

from datetime import datetime, timezone

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine.candidate_engine import (
    attach_opening_decision_provenance,
    build_candidate_decision,
    build_replacement_candidate_decision,
    evaluate_candidate_invariants,
)
from domain.domain.position_advice_authority import scope_for
from src.application.position_advice_plan_builder import (
    build_position_advice_plan,
)
from src.application.position_advice_promotion_checks import (
    evaluate_position_advice_plan_safety,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
FINGERPRINT = "d" * 64
IDENTITY = "a" * 64
CAPACITY_AUTHORITY = "b" * 64


def _candidate() -> dict[str, object]:
    raw = {
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "contract_symbol": "NVDA260821P00090000",
        "currency": "USD",
        "dte": 25,
        "strike": 90,
        "spot": 110,
        "bid": 3,
        "ask": 3.2,
        "mid": 3.1,
        "open_interest": 500,
        "volume": 50,
        "multiplier": 100,
        "net_income": 295,
        "annualized_net_return_on_cash_basis": 0.45,
        "spread_ratio": 0.0645,
    }
    invariant = evaluate_candidate_invariants(
        raw,
        mode="put",
        risk_policy_version="candidate_policy.v2",
        quote_snapshot_id="c" * 64,
        min_dte=14,
        max_dte=60,
        min_strike=None,
        max_strike=None,
        min_annualized_return=0.08,
        min_net_income=50,
        annualized_return=0.45,
        net_income=295,
        min_open_interest=100,
        min_volume=10,
        max_spread_ratio=0.3,
        event_flag=False,
        event_mode="reject",
        open_interest=500,
        volume=50,
        spread_ratio=0.0645,
    )
    opening = attach_opening_decision_provenance(
        build_candidate_decision(
            mode="put",
            symbol="NVDA",
            contract_symbol="NVDA260821P00090000",
            accepted=True,
            rejects=[],
            rank_key={"allocation_rank": 1},
            normalized_input=dict(invariant["normalized_input"]),
        ),
        risk_policy_version="candidate_policy.v2",
        risk_policy_hash=str(invariant["risk_policy_hash"]),
        quote_snapshot_id="c" * 64,
        normalized_input=dict(invariant["normalized_input"]),
    )
    replacement = build_replacement_candidate_decision(
        candidate_id="candidate-1",
        opening_decision=opening,
        invariant_decision=invariant,
    )
    return {
        "schema_version": "candidate_all_decisions.v1",
        "candidate_id": "candidate-1",
        "strategy_mode": "put",
        "normalized_input": invariant["normalized_input"],
        "normalized_input_hash": invariant["normalized_input_hash"],
        "risk_policy_version": invariant["risk_policy_version"],
        "risk_policy_hash": invariant["risk_policy_hash"],
        "quote_snapshot_id": invariant["quote_snapshot_id"],
        "opening_decision": opening,
        "invariant_decision": invariant,
        "replacement_candidate_decision": replacement,
    }


def _input(*, combo: bool = False) -> dict[str, object]:
    fields = {
        "broker": "futu",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "status": "open",
        "contracts": 1,
        "contracts_open": 1,
        "contracts_closed": 0,
        "currency": "USD",
        "strike": 100,
        "expiration_ymd": "2026-08-06",
        "multiplier": 100,
        "premium": 2,
        "cash_secured_amount": 10000,
    }
    combo_identities: list[dict[str, object]] = []
    positions = [{"record_id": "lot-put", "fields": fields}]
    if combo:
        fields.update(
            {
                "strategy": "combo_yield",
                "strategy_group_id": "combo-1",
                "leg_role": "funding_put",
            }
        )
        call_fields = {
            "broker": "futu",
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "side": "long",
            "status": "open",
            "contracts": 1,
            "contracts_open": 1,
            "contracts_closed": 0,
            "currency": "USD",
            "strike": 120,
            "expiration_ymd": "2026-12-18",
            "multiplier": 100,
            "premium": 4,
            "strategy": "combo_yield",
            "strategy_group_id": "combo-1",
            "leg_role": "participation_call",
        }
        positions.append({"record_id": "lot-call", "fields": call_fields})
        identity_payload = {
            "schema_version": "combo_identity.v2",
            "group_id": "combo-1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": "lot-put",
            "funding_put_open_event_id": "event-put",
            "funding_put_contract_key": {
                "option_type": "put",
                "strike": "100",
                "expiration": "2026-08-06",
            },
            "participation_call_record_id": "lot-call",
            "participation_call_open_event_id": "event-call",
            "participation_call_contract_key": {
                "option_type": "call",
                "strike": "120",
                "expiration": "2026-12-18",
            },
            "original_contracts": 1,
        }
        identity_payload["identity_hash"] = canonical_sha256(
            identity_payload
        )
        combo_identities.append(identity_payload)
    snapshot = {
        "schema_version": "decision_state_snapshot.v2",
        "snapshot_status": "trusted",
        "actionable": True,
        "decision_state_fingerprint": FINGERPRINT,
        "account_position_lots": positions,
        "account_lifecycle_cases": [],
        "account_lifecycle_evidence": [],
        "account_lifecycle_allocations": [],
        "account_assigned_stock_events": [],
        "account_combo_identities": combo_identities,
    }
    return {
        "schema_version": "position_advice_input.v2",
        "freshness_policy": "position_advice_freshness.v2",
        "account_run_id": "run-1",
        "account": "lx",
        "normalized_account": "lx",
        "broker": "futu",
        "included_markets": ["US"],
        "portfolio_scope_id": scope_for("lx"),
        "normalized_portfolio_source": "futu",
        "portfolio_account_identity_hash": IDENTITY,
        "capacity_pool_authority_id": CAPACITY_AUTHORITY,
        "authority_mode": "v2",
        "authority_generation": 2,
        "authority_policy_hash": "e" * 64,
        "authority_resolution_status": "resolved",
        "authority_covered_strategy_families": ["short_put"],
        "decision_state_fingerprint": FINGERPRINT,
        "decision_state_snapshot": snapshot,
        "input_hash": "f" * 64,
        "input_snapshot_ids": ["1" * 64],
        "source_manifest_hash": "2" * 64,
        "source_receipt_hashes": ["3" * 64],
        "source_manifest": [
            {
                "source_kind": "quotes",
                "snapshot_id": "c" * 64,
                "expires_at": "2026-07-27T10:30:00Z",
            }
        ],
    }


def _quotes(*, combo: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-08-06",
            "contract_symbol": "NVDA260806P00100000",
            "currency": "USD",
            "dte": 10,
            "strike": 100,
            "spot": 110,
            "bid": 0.9,
            "ask": 1,
            "mid": 0.95,
            "multiplier": 100,
        },
        dict(_candidate()["normalized_input"]),
    ]
    if combo:
        rows.append(
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-12-18",
                "currency": "USD",
                "dte": 144,
                "strike": 120,
                "spot": 110,
                "bid": 4,
                "ask": 4.2,
                "mid": 4.1,
                "multiplier": 100,
            }
        )
    return rows


def _build(*, combo: bool = False) -> dict[str, object]:
    return build_position_advice_plan(
        immutable_input=_input(combo=combo),
        candidate_decisions=[_candidate()],
        quote_rows=_quotes(combo=combo),
        cash_capacity={
            "status": "available",
            "uncommitted_cash_headroom_base_cny": "0",
        },
        share_coverage={
            "by_symbol": {
                "NVDA": {
                    "status": "available",
                    "uncommitted_covered_shares": 0,
                    "avg_cost": 100,
                    "currency": "USD",
                }
            }
        },
        fx_payload={"rates": {"USDCNY": 7.2}},
        checked_at=NOW,
    )


def test_production_built_shadow_plans_pass_automatic_promotion_safety() -> None:
    for combo in (False, True):
        immutable_input = _input(combo=combo)
        immutable_input["authority_mode"] = "v2_shadow"
        immutable_input["source_manifest"] = [
            {
                "source_kind": source_kind,
                "snapshot_id": f"{index + 1:x}" * 64,
                "receipt_hash": f"{index + 8:x}" * 64,
                "payload_sha256": f"{index + 1:x}" * 64,
                "expires_at": "2026-07-27T10:30:00Z",
            }
            for index, source_kind in enumerate(
                (
                    "quotes",
                    "candidate_decisions",
                    "portfolio",
                    "ledger_decision_state",
                    "cash_capacity",
                    "share_coverage",
                    "fx",
                )
            )
        ]
        plan = build_position_advice_plan(
            immutable_input=immutable_input,
            candidate_decisions=[_candidate()],
            quote_rows=_quotes(combo=combo),
            cash_capacity={
                "status": "available",
                "uncommitted_cash_headroom_base_cny": "0",
            },
            share_coverage={
                "by_symbol": {
                    "NVDA": {
                        "status": "available",
                        "uncommitted_covered_shares": 0,
                        "avg_cost": 100,
                        "currency": "USD",
                    }
                }
            },
            fx_payload={"rates": {"USDCNY": 7.2}},
            checked_at=NOW,
        )
        plan["artifact_hash"] = canonical_sha256(plan)

        report = evaluate_position_advice_plan_safety(
            [(plan, immutable_input)]
        )

        assert report["safety"] == {
            "false_assignment_confirmation": 0,
            "invalid_combo_continuation": 0,
            "stale_or_incomplete_actionable_exposure": 0,
            "allocator_invariant_violation": 0,
            "authority_mixed_exposure": 0,
            "lifecycle_or_identity_conflict_actionable": 0,
        }


def test_standalone_put_roll_uses_released_collateral_and_becomes_actionable() -> None:
    plan = _build()
    row = plan["rows"][0]

    assert row["recommendation"] == "roll"
    assert row["model_trade_actionable"] is True
    assert row["human_review_required"] is False
    assert row["actionable"] is True
    assert row["resource_deltas"] == [
        {
            "resource_kind": "cash_base_cny",
            "pool_key": (
                f"cash:{scope_for('lx')}:{CAPACITY_AUTHORITY}"
            ),
            "unit": "CNY",
            "released": "72000",
            "required": "64800",
            "net_after": "7200",
        }
    ]
    assert plan["resource_pools_before"][
        f"cash:{scope_for('lx')}:{CAPACITY_AUTHORITY}"
    ]["available"] == "0"


def test_lifecycle_review_is_human_actionable_but_never_trade_actionable() -> None:
    immutable_input = _input()
    immutable_input["decision_state_snapshot"][
        "account_position_lots"
    ][0]["fields"]["expiration_ymd"] = "2026-07-01"
    plan = build_position_advice_plan(
        immutable_input=immutable_input,
        candidate_decisions=[_candidate()],
        quote_rows=_quotes(),
        cash_capacity={
            "status": "available",
            "uncommitted_cash_headroom_base_cny": "0",
        },
        share_coverage={"by_symbol": {}},
        fx_payload={"rates": {"USDCNY": 7.2}},
        checked_at=NOW,
    )
    row = plan["rows"][0]

    assert row["lifecycle_state"] == "needs_review"
    assert row["recommendation"] == "review"
    assert row["model_trade_actionable"] is False
    assert row["model_actionable"] is False
    assert row["human_review_required"] is True
    assert row["actionable"] is False
    assert row["action_scope"] == "lifecycle_fact_review"


def test_active_combo_funding_put_action_decomposes_group_and_new_put_is_independent() -> None:
    plan = _build(combo=True)
    funding = next(
        row for row in plan["rows"] if row["position_id"] == "lot-put"
    )
    long_call = next(
        row for row in plan["rows"] if row["position_id"] == "lot-call"
    )

    assert funding["group_structure_state"] == "active_combo"
    assert funding["recommendation"] == "replace"
    assert funding["action_scope"] == "combo_group"
    assert funding["leg_plan"]["decomposes_group"] is True
    open_operation = funding["leg_plan"]["operations"][1]
    assert open_operation["strategy_after"] == "sell_put"
    assert open_operation["strategy_group_after"] is None
    assert long_call["recommendation"] == "not_evaluable"
    assert long_call["actionable"] is False
    assert long_call["reason_codes"] == [
        "long_call_forward_model_not_approved"
    ]
