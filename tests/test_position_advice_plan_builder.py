from __future__ import annotations

from datetime import datetime, timezone

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine.candidate_engine import (
    build_candidate_decision,
)
from domain.domain.position_advice_authority import scope_for
from src.application.position_advice_plan_builder import (
    PositionAdvicePlanError,
    build_position_advice_plan,
)
from src.application.ledger.decision_snapshot import (
    POSITION_FACT_SNAPSHOT_CONTRACT,
    decision_state_snapshot_fingerprint,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
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
    opening = build_candidate_decision(
        mode="put",
        symbol="NVDA",
        contract_symbol="NVDA260821P00090000",
        accepted=True,
        rejects=[],
        normalized_input=raw,
    )
    return {
        "schema_version": "opening_candidate_decision.v1",
        "candidate_id": "candidate-1",
        "strategy_mode": "put",
        "normalized_input": raw,
        "normalized_input_hash": canonical_sha256(raw),
        "risk_policy_hash": "b" * 64,
        "quote_snapshot_id": "c" * 64,
        "opening_decision": opening,
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
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
        "position_fact_contract_version": (
            POSITION_FACT_SNAPSHOT_CONTRACT
        ),
        "normalized_account": "lx",
        "snapshot_status": "trusted",
        "actionable": True,
        "decision_state_fingerprint": FINGERPRINT,
        "account_position_lots": positions,
        "account_lifecycle_cases": [],
        "account_lifecycle_evidence": [],
        "account_lifecycle_evidence_received_at_ms_by_id": {},
        "account_lifecycle_allocations": [],
        "account_lifecycle_source_consumptions": [],
        "account_lifecycle_timing_policies": [],
        "account_lifecycle_resolution": (
            resolve_account_lifecycle_overlay(
                account="lx",
                cases=[],
                evidence=[],
                allocations=[],
                source_claims=[],
                timing_policies=[],
                position_lots=positions,
            )
        ),
        "effective_void_event_ids": [],
        "account_assigned_stock_events": [],
        "account_combo_identities": combo_identities,
        "account_combo_group_memberships": (
            [_exact_combo_membership()] if combo else []
        ),
    }
    snapshot["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(snapshot)
    )
    fingerprint = snapshot["decision_state_fingerprint"]
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
        "decision_state_fingerprint": fingerprint,
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


def _rebind_snapshot_fingerprint(
    immutable_input: dict[str, object],
) -> None:
    snapshot = immutable_input["decision_state_snapshot"]
    fingerprint = decision_state_snapshot_fingerprint(snapshot)
    snapshot["decision_state_fingerprint"] = fingerprint
    immutable_input["decision_state_fingerprint"] = fingerprint


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


def _exact_combo_membership() -> dict[str, object]:
    fact: dict[str, object] = {
        "membership_schema_version": (
            "account_combo_group_membership.v1"
        ),
        "group_id": "combo-1",
        "status": "exact",
        "current_account_member_record_ids": [
            "lot-call",
            "lot-put",
        ],
        "global_current_member_count": 2,
        "global_historical_member_count": 2,
        "external_member_count": 0,
        "external_membership_hash": canonical_sha256([]),
        "retag_event_count": 0,
        "retag_history_hash": canonical_sha256([]),
        "cross_account_member_present": False,
        "cross_symbol_member_present": False,
        "member_bindings_for_current_account": [
            {
                "record_id": "lot-call",
                "role": "participation_call",
                "open_event_id": "event-call",
                "strategy": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
            },
            {
                "record_id": "lot-put",
                "role": "funding_put",
                "open_event_id": "event-put",
                "strategy": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
            },
        ],
        "reason_codes": [],
    }
    fact["membership_hash"] = canonical_sha256(fact)
    return fact


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


def test_legacy_v2_snapshot_without_position_fact_contract_is_rejected() -> None:
    immutable_input = _input()
    immutable_input["decision_state_snapshot"].pop(
        "position_fact_contract_version"
    )

    with pytest.raises(
        PositionAdvicePlanError,
        match="position_fact_contract_version_invalid",
    ):
        build_position_advice_plan(
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


def test_position_fact_marker_must_be_bound_by_decision_fingerprint() -> None:
    immutable_input = _input()
    snapshot = immutable_input["decision_state_snapshot"]
    legacy_payload = dict(snapshot)
    legacy_payload.pop("position_fact_contract_version")
    legacy_fingerprint = decision_state_snapshot_fingerprint(
        legacy_payload
    )
    snapshot["decision_state_fingerprint"] = legacy_fingerprint
    immutable_input["decision_state_fingerprint"] = legacy_fingerprint

    with pytest.raises(
        PositionAdvicePlanError,
        match="position_fact_decision_fingerprint_mismatch",
    ):
        build_position_advice_plan(
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
                    "opening_candidates",
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
    _rebind_snapshot_fingerprint(immutable_input)
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


def test_exact_combo_membership_preserves_group_action() -> None:
    immutable_input = _input(combo=True)
    immutable_input["decision_state_snapshot"][
        "account_combo_group_memberships"
    ] = [_exact_combo_membership()]
    _rebind_snapshot_fingerprint(immutable_input)

    plan = build_position_advice_plan(
        immutable_input=immutable_input,
        candidate_decisions=[_candidate()],
        quote_rows=_quotes(combo=True),
        cash_capacity={
            "status": "available",
            "uncommitted_cash_headroom_base_cny": "0",
        },
        share_coverage={"by_symbol": {}},
        fx_payload={"rates": {"USDCNY": 7.2}},
        checked_at=NOW,
    )

    funding = next(
        row for row in plan["rows"] if row["position_id"] == "lot-put"
    )
    assert funding["group_structure_state"] == "active_combo"
    assert funding["action_scope"] == "combo_group"


def test_combo_membership_must_bind_identity_open_events() -> None:
    immutable_input = _input(combo=True)
    membership = _exact_combo_membership()
    membership["member_bindings_for_current_account"][0][
        "open_event_id"
    ] = "wrong-call-event"
    membership["membership_hash"] = canonical_sha256(
        {
            key: value
            for key, value in membership.items()
            if key != "membership_hash"
        }
    )
    immutable_input["decision_state_snapshot"][
        "account_combo_group_memberships"
    ] = [membership]
    _rebind_snapshot_fingerprint(immutable_input)

    plan = build_position_advice_plan(
        immutable_input=immutable_input,
        candidate_decisions=[_candidate()],
        quote_rows=_quotes(combo=True),
        cash_capacity={
            "status": "available",
            "uncommitted_cash_headroom_base_cny": "0",
        },
        share_coverage={"by_symbol": {}},
        fx_payload={"rates": {"USDCNY": 7.2}},
        checked_at=NOW,
    )

    funding = next(
        row for row in plan["rows"] if row["position_id"] == "lot-put"
    )
    assert funding["group_structure_state"] == "review_required"
    assert funding["recommendation"] == "review"
    assert (
        "combo_identity_membership_binding_mismatch"
        in funding["reason_codes"]
    )
    assert plan["selected_proposals"] == []


def test_missing_combo_membership_fact_blocks_group_action() -> None:
    immutable_input = _input(combo=True)
    immutable_input["decision_state_snapshot"][
        "account_combo_group_memberships"
    ] = []
    _rebind_snapshot_fingerprint(immutable_input)

    plan = build_position_advice_plan(
        immutable_input=immutable_input,
        candidate_decisions=[_candidate()],
        quote_rows=_quotes(combo=True),
        cash_capacity={
            "status": "available",
            "uncommitted_cash_headroom_base_cny": "0",
        },
        share_coverage={"by_symbol": {}},
        fx_payload={"rates": {"USDCNY": 7.2}},
        checked_at=NOW,
    )

    funding = next(
        row for row in plan["rows"] if row["position_id"] == "lot-put"
    )
    assert funding["group_structure_state"] == "review_required"
    assert funding["recommendation"] == "review"
    assert funding["model_trade_actionable"] is False
    assert "combo_group_membership_missing" in funding["reason_codes"]
    assert plan["selected_proposals"] == []
