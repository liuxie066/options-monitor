from __future__ import annotations

from copy import deepcopy

import pytest

from src.application.ledger.lifecycle_settlement_semantics import (
    SettlementSemanticUnavailable,
    attach_settlement_semantics,
    settlement_semantic_from_evidence,
)


def _receipt(rows: list[dict], **overrides: object) -> dict:
    return {
        "status": "complete",
        "provider_code": None,
        "error_class": None,
        "query_input": {"start": "2026-08-20", "end": "2026-09-04"},
        "coverage_complete": True,
        "pagination_complete": True,
        "stale": False,
        "fallback_cache": False,
        "observed_at_ms": 1_800_000_000_000,
        "rows": rows,
        **overrides,
    }


def _observation() -> dict:
    anchor_key = "futu:lx:1001:option-close-1"
    return {
        "schema_version": "broker_settlement_observation.v2",
        "case_id": "case-1",
        "account": "lx",
        "futu_account_id": "1001",
        "market": "US",
        "contract_identity": {
            "symbol": "NVDA",
            "option_contract_code": "US.NVDA260821P100000",
            "option_type": "put",
            "position_side": "short",
            "strike": "100.00",
            "expiration_ymd": "2026-08-21",
            "multiplier": 100,
        },
        "target_contracts_by_lot": {"lot-1": 1},
        "frozen_preterminal_remaining_by_lot": {"lot-1": 1},
        "anchor_option_deal_key": anchor_key,
        "anchor_execution_time_ms": 1_800_000_000_000,
        "observation_start_ms": 1_799_000_000_000,
        "observed_at_ms": 1_800_100_000_000,
        "settlement_deadline_ms": 1_800_000_000_001,
        "query_window": {
            "start": "2026-08-20",
            "end": "2026-09-04",
        },
        "calendar_hash": "calendar-v1",
        "required_sources": [
            "anchor_option_close",
            "history_deals",
            "history_orders",
            "fresh_positions",
            "trading_calendar",
            "contract_metadata",
        ],
        "source_receipts": {
            "anchor_option_close": _receipt(
                [
                    {
                        "evidence_id": "anchor-1",
                        "source_event_id": anchor_key,
                        "account": "lx",
                        "futu_account_id": "1001",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "position_side": "short",
                        "strike": 100,
                        "expiration_ymd": "2026-08-21",
                        "contracts": 1,
                        "price": 0,
                        "event_time_ms": 1_800_000_000_000,
                        "received_at_ms": 1_800_000_000_100,
                        "order_id": "order-1",
                    }
                ]
            ),
            "history_deals": _receipt(
                [
                    {
                        "deal_id": "option-close-1",
                        "acc_id": "1001",
                        "code": "US.NVDA260821P100000",
                        "qty": "1.0",
                        "price": "0",
                        "order_id": "order-1",
                    },
                    {
                        "deal_id": "unrelated-account-deal",
                        "acc_id": "1001",
                        "code": "US.AAPL",
                        "qty": 100,
                        "price": 200,
                    },
                ]
            ),
            "history_orders": _receipt(
                [
                    {
                        "order_id": "order-1",
                        "is_broker_auto": True,
                        "order_origin": "broker",
                    },
                    {"order_id": "unrelated-order"},
                ]
            ),
            "fresh_positions": _receipt([]),
            "trading_calendar": _receipt(
                [
                    {"date": "2026-08-24", "type": "TRADING"},
                    {"date": "2026-08-25", "type": "TRADING"},
                ]
            ),
            "contract_metadata": _receipt(
                [
                    {
                        "settlement_style": "physical",
                        "underlying_security_type": "equity",
                        "last_trade_cutoff_ms": 1_800_000_000_000,
                        "last_trade_cutoff_source": "registry",
                        "calendar_hash": "calendar-v1",
                    }
                ]
            ),
        },
        "stock_settlement_candidates": [],
        "broker_option_position_absent": True,
        "projection_matches_frozen_remaining": True,
        "reservation_exclusive": True,
        "competing_effective_consumption": False,
        "stock_settlement_present": False,
        "normal_order_present": False,
        "complete": True,
        "incomplete_reason_codes": [],
    }


def _fingerprint(observation: dict) -> str:
    return str(
        attach_settlement_semantics(observation)[
            "semantic_fingerprint"
        ]
    )


def _current_evidence(observation: dict) -> dict:
    current = attach_settlement_semantics(observation)
    return {
        "evidence_type": current["semantic_projection"]["evidence_kind"],
        "observation": current,
        "semantic_schema": current["semantic_schema"],
        "semantic_fingerprint": current["semantic_fingerprint"],
        "semantic_projection": current["semantic_projection"],
    }


def test_current_semantic_envelope_round_trips_canonical_projection() -> None:
    evidence = _current_evidence(_observation())
    evidence["semantic_projection"] = dict(
        reversed(list(evidence["semantic_projection"].items()))
    )

    semantic, fingerprint = settlement_semantic_from_evidence(evidence)

    assert semantic == evidence["semantic_projection"]
    assert fingerprint == evidence["semantic_fingerprint"]


@pytest.mark.parametrize("location", ["evidence", "observation"])
def test_current_semantic_projection_mismatch_fails_closed(
    location: str,
) -> None:
    evidence = _current_evidence(_observation())
    target = evidence if location == "evidence" else evidence["observation"]
    target["semantic_projection"] = deepcopy(
        target["semantic_projection"]
    )
    target["semantic_projection"]["complete"] = False

    with pytest.raises(
        SettlementSemanticUnavailable,
        match=f"projection mismatch in {location}",
    ):
        settlement_semantic_from_evidence(evidence)


def test_partial_current_semantic_metadata_fails_closed() -> None:
    evidence = _current_evidence(_observation())
    evidence.pop("semantic_projection")

    with pytest.raises(
        SettlementSemanticUnavailable,
        match="projection is unavailable in evidence",
    ):
        settlement_semantic_from_evidence(evidence)


def test_legacy_evidence_without_semantic_metadata_remains_projectable() -> None:
    evidence = {
        "evidence_type": "expire_close",
        "observation": _observation(),
    }

    semantic, fingerprint = settlement_semantic_from_evidence(evidence)

    assert semantic["case_id"] == "case-1"
    assert fingerprint == _fingerprint(_observation())


def test_attempt_only_metadata_does_not_change_semantics() -> None:
    first = _observation()
    second = deepcopy(first)
    second["observed_at_ms"] += 30_000
    second["request_id"] = "request-2"
    second["lifecycle_generation_token"] = "generation-2"
    second["case_status"] = "needs_review"
    for receipt in second["source_receipts"].values():
        receipt["observed_at_ms"] += 30_000
        receipt["error"] = "different raw diagnostic text"
        receipt["latency_ms"] = 9_999

    assert _fingerprint(first) == _fingerprint(second)


def test_row_order_unknown_columns_and_unrelated_rows_are_ignored() -> None:
    first = _observation()
    second = deepcopy(first)
    deals = second["source_receipts"]["history_deals"]["rows"]
    deals.reverse()
    deals[1]["new_sdk_column"] = {"volatile": True}
    deals.append(
        {
            "deal_id": "another-unrelated-deal",
            "acc_id": "1001",
            "code": "US.TSLA",
            "qty": 1,
        }
    )

    assert _fingerprint(first) == _fingerprint(second)


def test_position_fallback_filters_rows_by_case_contract() -> None:
    baseline = _observation()
    baseline["contract_identity"]["option_contract_code"] = ""
    baseline["complete"] = False
    baseline["incomplete_reason_codes"] = [
        "option_contract_code_missing"
    ]

    unrelated = deepcopy(baseline)
    unrelated["source_receipts"]["fresh_positions"]["rows"].append(
        {
            "code": "US.AAPL260821P200000",
            "qty": 1,
            "option_type": "put",
            "expiration_ymd": "2026-08-21",
            "strike": "200.00",
        }
    )
    matching = deepcopy(baseline)
    matching["source_receipts"]["fresh_positions"]["rows"].append(
        {
            "code": "US.NVDA260821P100000",
            "qty": 1,
            "option_type": "PUT",
            "expiration": "2026-08-21",
            "strike": 100,
        }
    )

    baseline_fingerprint = _fingerprint(baseline)
    assert _fingerprint(unrelated) == baseline_fingerprint
    assert _fingerprint(matching) != baseline_fingerprint


def test_relevant_duplicate_row_multiplicity_changes_semantics() -> None:
    first = _observation()
    second = deepcopy(first)
    relevant = deepcopy(
        second["source_receipts"]["history_deals"]["rows"][0]
    )
    second["source_receipts"]["history_deals"]["rows"].append(
        relevant
    )

    assert _fingerprint(first) != _fingerprint(second)


def test_query_coverage_and_business_facts_change_semantics() -> None:
    baseline = _observation()
    changed_window = deepcopy(baseline)
    changed_window["source_receipts"]["history_deals"][
        "query_input"
    ]["end"] = "2026-09-05"
    changed_coverage = deepcopy(baseline)
    changed_coverage["source_receipts"]["history_deals"][
        "coverage_complete"
    ] = False
    changed_business_fact = deepcopy(baseline)
    changed_business_fact["normal_order_present"] = True

    baseline_fingerprint = _fingerprint(baseline)
    assert _fingerprint(changed_window) != baseline_fingerprint
    assert _fingerprint(changed_coverage) != baseline_fingerprint
    assert _fingerprint(changed_business_fact) != baseline_fingerprint


def test_crossing_settlement_deadline_changes_semantics() -> None:
    before = _observation()
    before["observed_at_ms"] = before["settlement_deadline_ms"] - 1
    after = deepcopy(before)
    after["observed_at_ms"] = after["settlement_deadline_ms"]

    assert _fingerprint(before) != _fingerprint(after)
