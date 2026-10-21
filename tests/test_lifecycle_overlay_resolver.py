from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.application.ledger.lifecycle_overlay import (
    LifecycleOverlayContractError,
    advance_direct_lifecycle_anchor_resolution,
    lifecycle_case_generation_token,
    lifecycle_case_resolution,
    resolve_account_lifecycle_overlay,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)


EXPIRATION_YMD = "2026-08-21"


def _case(
    case_id: str,
    manifest: dict[str, int],
    *,
    status: str = "waiting_settlement_evidence",
    superseded_by_case_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "lifecycle_case.v2",
        "case_id": case_id,
        "case_key": f"key-{case_id}",
        "account": "lx",
        "broker": "futu",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": EXPIRATION_YMD,
        "target_contracts_by_lot": dict(manifest),
        "status": status,
        "superseded_by_case_id": superseded_by_case_id,
    }


def _lot(lot_id: str, contracts: int = 1) -> dict[str, Any]:
    return {
        "record_id": lot_id,
        "fields": {
            "account": "lx",
            "contracts": contracts,
            "original_contracts": contracts,
        },
    }


def _direct_anchor(
    *,
    case_id: str,
    evidence_id: str,
    source_suffix: str,
    manifest: dict[str, int],
    received_at_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_key = f"futu:lx:1001:{source_suffix}"
    evidence = {
        "evidence_id": evidence_id,
        "case_id": case_id,
        "source_event_id": source_key,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": sum(manifest.values()),
        "target_contracts_by_lot": dict(manifest),
        "price": "0",
        "event_time_ms": received_at_ms - 100,
        "received_at_ms": received_at_ms,
    }
    claim = build_source_consumption_claim(
        source_key=source_key,
        case_id=case_id,
        owner_evidence_id=evidence_id,
        source_role="option_anchor",
        economic_payload=evidence,
    )
    return evidence, claim


def _bridge_anchor(
    *,
    case_id: str,
    legacy_case_id: str,
    manifest: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    legacy_evidence_id = f"evidence-{legacy_case_id}"
    source_key = f"futu:lx:1001:source-{legacy_case_id}"
    legacy_evidence = {
        "evidence_id": legacy_evidence_id,
        "case_id": legacy_case_id,
        "source_event_id": f"source-{legacy_case_id}",
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "symbol": "NVDA",
        "raw": {"price": "0"},
        "_ledger_created_at_ms": 1_700_000_000_200,
    }
    bridge = {
        "schema_version": "migration_bridge_evidence.v1",
        "evidence_id": f"bridge-{case_id}",
        "case_id": case_id,
        "evidence_type": "migration_bridge",
        "account": "lx",
        "symbol": "NVDA",
        "referenced_legacy_case_id": legacy_case_id,
        "referenced_legacy_evidence_id": legacy_evidence_id,
        "allocating": False,
    }
    claim = build_source_consumption_claim(
        source_key=source_key,
        case_id=legacy_case_id,
        owner_evidence_id=legacy_evidence_id,
        source_role="option_anchor",
        economic_payload={
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": "100",
            "expiration_ymd": EXPIRATION_YMD,
            "contracts": sum(manifest.values()),
            "price": "0",
            "event_time_ms": 1_700_000_000_100,
        },
    )
    return [legacy_evidence, bridge], [claim], _case(
        legacy_case_id,
        manifest,
        status="superseded",
        superseded_by_case_id=case_id,
    )


def _resolve(
    *,
    cases: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    allocations: list[dict[str, Any]] | None = None,
    effective_void_event_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return resolve_account_lifecycle_overlay(
        account="lx",
        cases=cases,
        evidence=evidence,
        allocations=allocations or [],
        source_claims=claims,
        timing_policies=[],
        position_lots=lots,
        effective_void_event_ids=effective_void_event_ids,
    )


@pytest.mark.parametrize("capacity,quantities", [(2, (1, 1)), (3, (1, 1)), (3, (1, 1, 1))])
@pytest.mark.parametrize("incremental", [False, True])
def test_same_lot_distinct_sources_accumulate_within_capacity(capacity, quantities, incremental) -> None:
    case = _case("case-a", {"lot-1": capacity})
    pairs = [_direct_anchor(case_id="case-a", evidence_id=f"evidence-{i}", source_suffix=f"deal-{i}",
                            manifest={"lot-1": quantity}, received_at_ms=1_700_000_000_200 + i)
             for i, quantity in enumerate(quantities)]
    results = []
    for ordered in (pairs, list(reversed(pairs))):
        if incremental:
            result = lifecycle_case_resolution(_resolve(
                cases=[case], evidence=[], claims=[], lots=[_lot("lot-1", capacity)]), case_id="case-a")
            for evidence, claim in ordered:
                result = advance_direct_lifecycle_anchor_resolution(
                    lifecycle_case=case, prior_resolution=result, evidence=evidence, source_claim=claim)
        else:
            result = lifecycle_case_resolution(_resolve(
                cases=[case], evidence=[p[0] for p in ordered], claims=[p[1] for p in ordered],
                lots=[_lot("lot-1", capacity)]), case_id="case-a")
        assert result["status"] == "direct"
        assert result["reason_codes"] == []
        assert result["requested_reservations_by_lot"] == {"lot-1": sum(quantities)}
        assert result["effective_reservations_by_lot"] == {"lot-1": sum(quantities)}
        assert len(result["anchor_facts"]) == len(quantities)
        results.append(result)
    assert results[0] == results[1]
    full = lifecycle_case_resolution(_resolve(
        cases=[case], evidence=[p[0] for p in pairs], claims=[p[1] for p in pairs],
        lots=[_lot("lot-1", capacity)]), case_id="case-a")
    assert results[0] == full


@pytest.mark.parametrize("new_quantity", [1, 2])
@pytest.mark.parametrize("void_mode", ["active", "terminal_void", "allocation_void"])
def test_gross_anchor_capacity_is_independent_of_net_allocations(new_quantity, void_mode) -> None:
    case = _case("case-a", {"lot-1": 2})
    first, claim_1 = _direct_anchor(case_id="case-a", evidence_id="first", source_suffix="first",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200)
    second, claim_2 = _direct_anchor(case_id="case-a", evidence_id="second", source_suffix="second",
        manifest={"lot-1": new_quantity}, received_at_ms=1_700_000_000_300)
    allocation = {"allocation_id": "allocation-1", "case_id": "case-a", "evidence_id": "first",
        "target_lot_id": "lot-1", "contracts_allocated": 1, "canonical_terminal_event_id": "terminal-1",
        "voided": void_mode == "allocation_void"}
    inputs = {"cases": [case], "lots": [_lot("lot-1", 2)], "allocations": [allocation],
              "effective_void_event_ids": ("terminal-1",) if void_mode == "terminal_void" else ()}
    prior = lifecycle_case_resolution(_resolve(evidence=[first], claims=[claim_1], **inputs), case_id="case-a")
    assert prior["requested_reservations_by_lot"] == ({} if void_mode == "active" else {"lot-1": 1})
    advanced = advance_direct_lifecycle_anchor_resolution(
        lifecycle_case=case, prior_resolution=prior, evidence=second, source_claim=claim_2)
    full = lifecycle_case_resolution(_resolve(evidence=[first, second], claims=[claim_1, claim_2], **inputs),
                                     case_id="case-a")
    for result in (advanced, full):
        if new_quantity == 2:
            assert result["status"] == "conflict"
            assert result["reason_codes"] == ["lifecycle_anchor_target_quantity_invalid"]
            assert result["effective_reservations_by_lot"] == {}
        else:
            assert result["status"] == "direct"
            assert result["requested_reservations_by_lot"] == {"lot-1": 1 if void_mode == "active" else 2}
            assert len(result["anchor_facts"]) == 2
    assert advanced == full


def test_same_lot_anchor_replay_preserves_source_identity() -> None:
    case = _case("case-a", {"lot-1": 2})
    first, claim = _direct_anchor(case_id="case-a", evidence_id="first", source_suffix="same-source",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200)
    prior = lifecycle_case_resolution(_resolve(cases=[case], evidence=[first], claims=[claim],
        lots=[_lot("lot-1", 2)]), case_id="case-a")
    assert advance_direct_lifecycle_anchor_resolution(
        lifecycle_case=case, prior_resolution=prior, evidence=first, source_claim=claim) == prior
    changed, changed_claim = _direct_anchor(case_id="case-a", evidence_id="changed", source_suffix="same-source",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_300)
    rejected = advance_direct_lifecycle_anchor_resolution(
        lifecycle_case=case, prior_resolution=prior, evidence=changed, source_claim=changed_claim)
    assert rejected["reason_codes"] == ["direct_anchor_source_collision"]
    assert rejected["effective_reservations_by_lot"] == {}
    with pytest.raises(LifecycleOverlayContractError, match="duplicate"):
        _resolve(cases=[case], evidence=[first, changed], claims=[claim, changed_claim], lots=[_lot("lot-1", 2)])


@pytest.mark.parametrize("duplicate", ["cases", "evidence", "claims"])
def test_duplicate_raw_anchor_rows_are_not_coalesced(duplicate) -> None:
    evidence, claim = _direct_anchor(case_id="case-a", evidence_id="first", source_suffix="first",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200)
    rows = {"cases": [_case("case-a", {"lot-1": 2})], "evidence": [evidence], "claims": [claim],
            "lots": [_lot("lot-1", 2)]}
    rows[duplicate].append(deepcopy(rows[duplicate][0]))
    with pytest.raises(LifecycleOverlayContractError, match="duplicate"):
        _resolve(**rows)


@pytest.mark.parametrize("invalid,reason", [
    ("unknown_lot", "lifecycle_anchor_target_quantity_invalid"),
    ("nonpositive", "direct_anchor_evidence_invalid"),
    ("original_capacity", "lifecycle_target_quantity_invalid"),
    ("excess_allocation", "lifecycle_allocation_exceeds_anchor"),
    ("claim_hash", "direct_anchor_source_claim_invalid"),
    ("claim_account", "direct_anchor_source_claim_invalid"),
])
def test_same_lot_capacity_repair_keeps_invalid_input_guards(invalid, reason) -> None:
    case = _case("case-a", {"lot-1": 2})
    evidence, claim = _direct_anchor(case_id="case-a", evidence_id="first", source_suffix="first",
        manifest={"other-lot" if invalid == "unknown_lot" else "lot-1": 1}, received_at_ms=1_700_000_000_200)
    allocations = []
    if invalid == "nonpositive":
        evidence["target_contracts_by_lot"] = {"lot-1": 0}
    elif invalid == "excess_allocation":
        allocations = [{"allocation_id": "allocation-1", "case_id": "case-a", "target_lot_id": "lot-1",
                        "contracts_allocated": 2}]
    elif invalid == "claim_hash":
        claim["source_payload_hash"] = "invalid"
    elif invalid == "claim_account":
        case["futu_account_id"] = "different-account"
    result = lifecycle_case_resolution(_resolve(cases=[case], evidence=[evidence], claims=[claim],
        allocations=allocations, lots=[_lot("lot-1", 1 if invalid == "original_capacity" else 2)]), case_id="case-a")
    assert result["status"] == "conflict"
    assert result["reason_codes"] == [reason]
    assert result["effective_reservations_by_lot"] == {}


@pytest.mark.parametrize("mode", ["valid", "mixed", "competing_claim"])
def test_same_lot_repair_preserves_bridge_ownership(mode) -> None:
    case = _case("case-a", {"lot-1": 2})
    evidence, claims, legacy = _bridge_anchor(case_id="case-a", legacy_case_id="legacy", manifest={"lot-1": 2})
    direct, claim = _direct_anchor(case_id="case-a", evidence_id="direct", source_suffix="direct",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_300)
    if mode == "mixed":
        evidence.append(direct)
    if mode != "valid":
        claims.append(claim)
    result = lifecycle_case_resolution(_resolve(cases=[case, legacy], evidence=evidence, claims=claims,
        lots=[_lot("lot-1", 2)]), case_id="case-a")
    if mode == "valid":
        assert result["status"] == "bridged"
        assert result["effective_reservations_by_lot"] == {"lot-1": 2}
    else:
        assert result["status"] == "conflict"
        assert result["effective_reservations_by_lot"] == {}
        assert result["reason_codes"] == ["direct_and_migration_bridge_conflict" if mode == "mixed"
                                          else "migration_bridge_competing_owner_claim"]


def test_valid_same_case_partials_do_not_relax_cross_case_contention() -> None:
    pairs = [_direct_anchor(case_id=case_id, evidence_id=str(i), source_suffix=str(i),
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200 + i)
        for i, case_id in enumerate(("case-a", "case-a", "case-b"))]
    result = _resolve(cases=[_case("case-a", {"lot-1": 3}), _case("case-b", {"lot-1": 3})],
        evidence=[p[0] for p in pairs], claims=[p[1] for p in pairs], lots=[_lot("lot-1", 3)])
    for case_id in ("case-a", "case-b"):
        resolution = lifecycle_case_resolution(result, case_id=case_id)
        assert resolution["reason_codes"] == ["reservation_target_overlap"]
        assert resolution["effective_reservations_by_lot"] == {}


def test_disjoint_direct_anchors_are_canonical_and_order_stable() -> None:
    lifecycle_case = _case("case-a", {"lot-1": 1, "lot-2": 1})
    anchor_2, claim_2 = _direct_anchor(
        case_id="case-a", evidence_id="evidence-2", source_suffix="deal-2",
        manifest={"lot-2": 1}, received_at_ms=1_700_000_000_300,
    )
    anchor_1, claim_1 = _direct_anchor(
        case_id="case-a", evidence_id="evidence-1", source_suffix="deal-1",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200,
    )
    first = _resolve(
        cases=[lifecycle_case], evidence=[anchor_2, anchor_1],
        claims=[claim_2, claim_1], lots=[_lot("lot-2"), _lot("lot-1")],
    )
    second = _resolve(
        cases=[deepcopy(lifecycle_case)], evidence=[deepcopy(anchor_1), deepcopy(anchor_2)],
        claims=[deepcopy(claim_1), deepcopy(claim_2)], lots=[_lot("lot-1"), _lot("lot-2")],
    )

    assert first == second
    resolution = lifecycle_case_resolution(first, case_id="case-a")
    assert resolution is not None
    assert resolution["status"] == "direct"
    assert resolution["effective_reservations_by_lot"] == {
        "lot-1": 1,
        "lot-2": 1,
    }
    assert min(
        item["received_at_ms"] for item in resolution["anchor_facts"]
    ) == 1_700_000_000_200


def test_incremental_direct_anchor_matches_full_resolver() -> None:
    lifecycle_case = _case("case-a", {"lot-1": 1, "lot-2": 1})
    anchor_1, claim_1 = _direct_anchor(
        case_id="case-a", evidence_id="evidence-1", source_suffix="deal-1",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200,
    )
    anchor_2, claim_2 = _direct_anchor(
        case_id="case-a", evidence_id="evidence-2", source_suffix="deal-2",
        manifest={"lot-2": 1}, received_at_ms=1_700_000_000_300,
    )
    prior = lifecycle_case_resolution(
        _resolve(
            cases=[lifecycle_case], evidence=[anchor_1], claims=[claim_1], lots=[_lot("lot-1"), _lot("lot-2")]
        ),
        case_id="case-a",
    )
    full = lifecycle_case_resolution(
        _resolve(
            cases=[lifecycle_case], evidence=[anchor_1, anchor_2],
            claims=[claim_1, claim_2], lots=[_lot("lot-1"), _lot("lot-2")],
        ),
        case_id="case-a",
    )
    assert prior is not None and full is not None
    assert advance_direct_lifecycle_anchor_resolution(
        lifecycle_case=lifecycle_case,
        prior_resolution=prior,
        evidence=anchor_2,
        source_claim=claim_2,
    ) == full


def test_direct_anchor_without_claim_is_conflict_and_zero_reservation() -> None:
    anchor, _claim = _direct_anchor(
        case_id="case-a", evidence_id="evidence-1", source_suffix="deal-1",
        manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200,
    )
    resolved = _resolve(cases=[_case("case-a", {"lot-1": 1})], evidence=[anchor], claims=[], lots=[_lot("lot-1")])

    resolution = lifecycle_case_resolution(resolved, case_id="case-a")
    assert resolution is not None
    assert resolution["status"] == "conflict"
    assert resolution["effective_reservations_by_lot"] == {}
    assert resolution["reason_codes"] == [
        "direct_anchor_source_claim_not_unique"
    ]


def test_account_arbitration_conflicts_whole_component_only() -> None:
    direct, direct_claim = _direct_anchor(
        case_id="case-direct", evidence_id="evidence-direct", source_suffix="deal-direct",
        manifest={"lot-shared": 1}, received_at_ms=1_700_000_000_200,
    )
    legacy_evidence, legacy_claims, legacy_case = _bridge_anchor(
        case_id="case-bridge",
        legacy_case_id="legacy-bridge",
        manifest={"lot-shared": 1},
    )
    unrelated, unrelated_claim = _direct_anchor(
        case_id="case-unrelated", evidence_id="evidence-unrelated", source_suffix="deal-unrelated",
        manifest={"lot-other": 1}, received_at_ms=1_700_000_000_400,
    )
    resolved = _resolve(
        cases=[
            _case("case-direct", {"lot-shared": 1}),
            _case("case-bridge", {"lot-shared": 1}),
            legacy_case,
            _case("case-unrelated", {"lot-other": 1}),
        ],
        evidence=[direct, *legacy_evidence, unrelated], claims=[direct_claim, *legacy_claims, unrelated_claim],
        lots=[_lot("lot-shared"), _lot("lot-other")],
    )

    for case_id in ("case-direct", "case-bridge"):
        item = lifecycle_case_resolution(resolved, case_id=case_id)
        assert item is not None
        assert item["status"] == "conflict"
        assert item["effective_reservations_by_lot"] == {}
        assert item["reason_codes"] == ["reservation_target_overlap"]
    unrelated_resolution = lifecycle_case_resolution(resolved, case_id="case-unrelated")
    assert unrelated_resolution is not None
    assert unrelated_resolution["status"] == "direct"
    assert unrelated_resolution["effective_reservations_by_lot"] == {
        "lot-other": 1
    }


def test_generation_token_tracks_potential_competitor_not_unrelated_case() -> None:
    anchor_a, claim_a = _direct_anchor(
        case_id="case-a", evidence_id="evidence-a", source_suffix="deal-a",
        manifest={"lot-shared": 1}, received_at_ms=1_700_000_000_200,
    )
    anchor_b, claim_b = _direct_anchor(
        case_id="case-b", evidence_id="evidence-b", source_suffix="deal-b",
        manifest={"lot-shared": 1}, received_at_ms=1_700_000_000_300,
    )
    unrelated, unrelated_claim = _direct_anchor(
        case_id="case-c", evidence_id="evidence-c", source_suffix="deal-c",
        manifest={"lot-other": 1}, received_at_ms=1_700_000_000_400,
    )
    cases = [
        _case("case-a", {"lot-shared": 1}),
        _case("case-b", {"lot-shared": 1}),
        _case("case-c", {"lot-other": 1}),
    ]
    lots = [_lot("lot-shared"), _lot("lot-other")]
    before = _resolve(cases=cases, evidence=[anchor_a], claims=[claim_a], lots=lots)
    unrelated_changed = _resolve(
        cases=cases, evidence=[anchor_a, unrelated], claims=[claim_a, unrelated_claim], lots=lots
    )
    competitor_changed = _resolve(
        cases=cases, evidence=[anchor_a, anchor_b], claims=[claim_a, claim_b], lots=lots
    )

    before_token = lifecycle_case_generation_token(before, case_id="case-a")
    unrelated_token = lifecycle_case_generation_token(unrelated_changed, case_id="case-a")
    competitor_token = lifecycle_case_generation_token(competitor_changed, case_id="case-a")
    assert before_token is not None
    assert unrelated_token is not None
    assert competitor_token is not None
    assert (
        before_token["generation_token"]
        == unrelated_token["generation_token"]
    )
    assert (
        before_token["generation_token"]
        != competitor_token["generation_token"]
    )
