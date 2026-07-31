from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.application.ledger.lifecycle_overlay import (
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
) -> dict[str, Any]:
    return resolve_account_lifecycle_overlay(
        account="lx",
        cases=cases,
        evidence=evidence,
        allocations=[],
        source_claims=claims,
        timing_policies=[],
        position_lots=lots,
    )


def test_disjoint_direct_anchors_are_canonical_and_order_stable() -> None:
    lifecycle_case = _case("case-a", {"lot-1": 1, "lot-2": 1})
    anchor_2, claim_2 = _direct_anchor(
        case_id="case-a",
        evidence_id="evidence-2",
        source_suffix="deal-2",
        manifest={"lot-2": 1},
        received_at_ms=1_700_000_000_300,
    )
    anchor_1, claim_1 = _direct_anchor(
        case_id="case-a",
        evidence_id="evidence-1",
        source_suffix="deal-1",
        manifest={"lot-1": 1},
        received_at_ms=1_700_000_000_200,
    )
    first = _resolve(
        cases=[lifecycle_case],
        evidence=[anchor_2, anchor_1],
        claims=[claim_2, claim_1],
        lots=[_lot("lot-2"), _lot("lot-1")],
    )
    second = _resolve(
        cases=[deepcopy(lifecycle_case)],
        evidence=[deepcopy(anchor_1), deepcopy(anchor_2)],
        claims=[deepcopy(claim_1), deepcopy(claim_2)],
        lots=[_lot("lot-1"), _lot("lot-2")],
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


def test_direct_anchor_without_claim_is_conflict_and_zero_reservation() -> None:
    anchor, _claim = _direct_anchor(
        case_id="case-a",
        evidence_id="evidence-1",
        source_suffix="deal-1",
        manifest={"lot-1": 1},
        received_at_ms=1_700_000_000_200,
    )
    resolved = _resolve(
        cases=[_case("case-a", {"lot-1": 1})],
        evidence=[anchor],
        claims=[],
        lots=[_lot("lot-1")],
    )

    resolution = lifecycle_case_resolution(resolved, case_id="case-a")
    assert resolution is not None
    assert resolution["status"] == "conflict"
    assert resolution["effective_reservations_by_lot"] == {}
    assert resolution["reason_codes"] == [
        "direct_anchor_source_claim_not_unique"
    ]


def test_account_arbitration_conflicts_whole_component_only() -> None:
    direct, direct_claim = _direct_anchor(
        case_id="case-direct",
        evidence_id="evidence-direct",
        source_suffix="deal-direct",
        manifest={"lot-shared": 1},
        received_at_ms=1_700_000_000_200,
    )
    legacy_evidence, legacy_claims, legacy_case = _bridge_anchor(
        case_id="case-bridge",
        legacy_case_id="legacy-bridge",
        manifest={"lot-shared": 1},
    )
    unrelated, unrelated_claim = _direct_anchor(
        case_id="case-unrelated",
        evidence_id="evidence-unrelated",
        source_suffix="deal-unrelated",
        manifest={"lot-other": 1},
        received_at_ms=1_700_000_000_400,
    )
    resolved = _resolve(
        cases=[
            _case("case-direct", {"lot-shared": 1}),
            _case("case-bridge", {"lot-shared": 1}),
            legacy_case,
            _case("case-unrelated", {"lot-other": 1}),
        ],
        evidence=[direct, *legacy_evidence, unrelated],
        claims=[direct_claim, *legacy_claims, unrelated_claim],
        lots=[_lot("lot-shared"), _lot("lot-other")],
    )

    for case_id in ("case-direct", "case-bridge"):
        item = lifecycle_case_resolution(resolved, case_id=case_id)
        assert item is not None
        assert item["status"] == "conflict"
        assert item["effective_reservations_by_lot"] == {}
        assert item["reason_codes"] == ["reservation_target_overlap"]
    unrelated_resolution = lifecycle_case_resolution(
        resolved,
        case_id="case-unrelated",
    )
    assert unrelated_resolution is not None
    assert unrelated_resolution["status"] == "direct"
    assert unrelated_resolution["effective_reservations_by_lot"] == {
        "lot-other": 1
    }


def test_generation_token_tracks_potential_competitor_not_unrelated_case() -> None:
    anchor_a, claim_a = _direct_anchor(
        case_id="case-a",
        evidence_id="evidence-a",
        source_suffix="deal-a",
        manifest={"lot-shared": 1},
        received_at_ms=1_700_000_000_200,
    )
    anchor_b, claim_b = _direct_anchor(
        case_id="case-b",
        evidence_id="evidence-b",
        source_suffix="deal-b",
        manifest={"lot-shared": 1},
        received_at_ms=1_700_000_000_300,
    )
    unrelated, unrelated_claim = _direct_anchor(
        case_id="case-c",
        evidence_id="evidence-c",
        source_suffix="deal-c",
        manifest={"lot-other": 1},
        received_at_ms=1_700_000_000_400,
    )
    cases = [
        _case("case-a", {"lot-shared": 1}),
        _case("case-b", {"lot-shared": 1}),
        _case("case-c", {"lot-other": 1}),
    ]
    lots = [_lot("lot-shared"), _lot("lot-other")]
    before = _resolve(
        cases=cases,
        evidence=[anchor_a],
        claims=[claim_a],
        lots=lots,
    )
    unrelated_changed = _resolve(
        cases=cases,
        evidence=[anchor_a, unrelated],
        claims=[claim_a, unrelated_claim],
        lots=lots,
    )
    competitor_changed = _resolve(
        cases=cases,
        evidence=[anchor_a, anchor_b],
        claims=[claim_a, claim_b],
        lots=lots,
    )

    before_token = lifecycle_case_generation_token(
        before,
        case_id="case-a",
    )
    unrelated_token = lifecycle_case_generation_token(
        unrelated_changed,
        case_id="case-a",
    )
    competitor_token = lifecycle_case_generation_token(
        competitor_changed,
        case_id="case-a",
    )
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
