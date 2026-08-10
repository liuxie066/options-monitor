from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.close_advice_quote_cache import quote_cache_metadata_path
from src.application.opend_symbol_outputs import (
    finalize_required_data_quote_candidate,
    publish_required_data_quote_snapshot,
    save_outputs,
    validate_required_data_payload_candidate,
)
from src.application.source_receipts import (
    SourceReceiptError,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_request_sha256,
)
from src.application.required_data_fetching import (
    bind_merged_payload_evidence,
    merge_required_data_payloads,
)


NOW = datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc)
COMPLETED_AT = NOW + timedelta(seconds=1)
HOST = "127.0.0.1"
PORT = 11111
CONTRACT_CODE = "NVDA260821P00100000"
CALL_CONTRACT_CODE = "NVDA260821C00120000"


def _term_matched_rv_fixture(
    *,
    expiration: str = "2026-08-21",
    value: float = 0.183,
    remaining_sessions: int = 17,
) -> dict[str, object]:
    lookback_sessions = max(20, remaining_sessions)
    return {
        "schema_version": "term_matched_rv.v1",
        "expiration": expiration,
        "status": "ok",
        "reason": None,
        "term_matched_rv": value,
        "remaining_sessions": remaining_sessions,
        "lookback_sessions": lookback_sessions,
        "input_start": "2026-07-06",
        "input_end": "2026-08-03",
        "input_close_session_count": lookback_sessions + 1,
        "input_return_count": lookback_sessions,
        "input_hash": hashlib.sha256(
            f"fixture:{expiration}:{value}".encode()
        ).hexdigest(),
        "missing_sessions": [],
        "legacy_weighted_rv": value,
        "shadow_difference": 0.0,
    }


def _term_matched_rv_row_fields(
    term: dict[str, object],
) -> dict[str, object]:
    return {
        "term_matched_rv": term["term_matched_rv"],
        "term_matched_rv_status": term["status"],
        "term_matched_rv_reason": term["reason"],
        "term_matched_rv_remaining_sessions": term["remaining_sessions"],
        "term_matched_rv_lookback_sessions": term["lookback_sessions"],
        "term_matched_rv_input_start": term["input_start"],
        "term_matched_rv_input_end": term["input_end"],
        "term_matched_rv_input_session_count": term[
            "input_close_session_count"
        ],
        "term_matched_rv_input_hash": term["input_hash"],
        "term_matched_rv_legacy_shadow": term["legacy_weighted_rv"],
        "term_matched_rv_shadow_difference": term["shadow_difference"],
    }


def _realized_volatility_meta_fixture() -> dict[str, object]:
    term = _term_matched_rv_fixture()
    return {
        "status": "ok",
        "reason": None,
        "sample_count": 120,
        "realized_volatility_20": 0.18,
        "realized_volatility_60": 0.19,
        "realized_volatility_120": 0.2,
        "realized_volatility_estimate": 0.19,
        "estimation_policy": "term_matched_sessions_v1",
        "term_matched": {"2026-08-21": term},
        "qfq_history": {
            "status": "ok",
            "market": "US",
            "underlier_code": "US.NVDA",
            "autype": "QFQ",
            "cache_identity": "US:US.NVDA:QFQ",
            "completed_before": "2026-08-04",
            "session_count": 120,
            "input_hash": hashlib.sha256(b"fixture:qfq").hexdigest(),
            "cache_status": "fixture",
            "revision_detected": False,
        },
        "trading_calendar": {
            "status": "ok",
            "market": "US",
            "start": "2026-07-01",
            "end": "2026-08-21",
            "session_count": 37,
        },
    }


def _side_plan() -> dict[str, object]:
    return {
        "option_type": "put",
        "min_dte": 17,
        "max_dte": 17,
        "explicit_expirations": ["2026-08-21"],
        "strike_window": {
            "min_strike": 100.0,
            "max_strike": 100.0,
            "source": "fixture",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": 100.0,
            "base_max_strike": 100.0,
        },
        "planning_reason": "fixture",
        "source_fields": ["sell_put"],
        "spot_reference": 110.0,
        "min_strike": 100.0,
        "max_strike": 100.0,
        "expiration_count": 1,
        "required_exact_strikes_by_expiration": {
            "2026-08-21": [100.0],
        },
    }


def _request() -> dict[str, object]:
    side_plan = _side_plan()
    return {
        "symbol": "NVDA",
        "limit_expirations": 8,
        "host": HOST,
        "port": PORT,
        "trading_date": "2026-08-04",
        "option_types": ["put"],
        "explicit_expirations": ["2026-08-21"],
        "min_dte": 17,
        "max_dte": 17,
        "side_strike_windows": {
            "put": {"min_strike": 100.0, "max_strike": 100.0}
        },
        "include_realized_volatility": True,
        "side_plans": [side_plan],
        "planning_reason": "fixture",
    }


def _call_side_plan() -> dict[str, object]:
    side_plan = _side_plan()
    side_plan.update(
        {
            "option_type": "call",
            "strike_window": {
                **side_plan["strike_window"],
                "min_strike": 120.0,
                "max_strike": 120.0,
                "base_min_strike": 120.0,
                "base_max_strike": 120.0,
            },
            "source_fields": ["covered_call"],
            "min_strike": 120.0,
            "max_strike": 120.0,
            "required_exact_strikes_by_expiration": {
                "2026-08-21": [120.0],
            },
        }
    )
    return side_plan


def _call_request() -> dict[str, object]:
    return {
        **_request(),
        "option_types": ["call"],
        "side_strike_windows": {
            "call": {"min_strike": 120.0, "max_strike": 120.0}
        },
        "side_plans": [_call_side_plan()],
    }


def _fetch_plan(*, request_count: int = 1) -> dict[str, object]:
    side_plan = _side_plan()
    side_plans = [side_plan]
    requests = [_request()]
    if request_count == 2:
        side_plans.append(_call_side_plan())
        requests.append(_call_request())
    return {
        "symbol": "NVDA",
        "spot_reference": 110.0,
        "side_plans": side_plans,
        "merged_requests": requests,
        "require_realized_volatility": True,
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_rows",
            "reason_code": None,
            "expirations": ["2026-08-21"],
            "observed_at_utc": NOW.isoformat(),
            "completed_at_utc": COMPLETED_AT.isoformat(),
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": "2026-08-04",
            },
            "error": None,
        },
        "projection_outcome": "success_rows",
        "projected_expirations": ["2026-08-21"],
    }


def _contract(*, request_count: int = 1) -> dict[str, object]:
    return build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=_fetch_plan(request_count=request_count),
        source="opend",
        host=HOST,
        port=PORT,
    )


def _payload(*, multiplier: object = 100.0) -> dict[str, object]:
    term = _term_matched_rv_fixture()
    return {
        "symbol": "NVDA",
        "underlier_code": "US.NVDA",
        "spot": 110.0,
        "expiration_count": 1,
        "expirations": ["2026-08-21"],
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "dte": 17,
                "contract_symbol": CONTRACT_CODE,
                "strike": 100.0,
                "spot": 110.0,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "volume": 10,
                "open_interest": 20,
                "implied_volatility": 0.25,
                "realized_volatility_20": 0.18,
                "realized_volatility_60": 0.19,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.183,
                **_term_matched_rv_row_fields(term),
                "in_the_money": False,
                "currency": "USD",
                "otm_pct": 0.0909090909,
                "delta": -0.3,
                "multiplier": multiplier,
            }
        ],
        "meta": {
            "status": "ok",
            "source": "opend",
            "host": HOST,
            "port": PORT,
            "trading_date": "2026-08-04",
            "source_outcome": "success_rows",
            "reason_code": None,
            "source_observed_at": NOW.isoformat(),
            "completed_at_utc": COMPLETED_AT.isoformat(),
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [CONTRACT_CODE],
            "snapshot_returned_code_set": [CONTRACT_CODE],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "realized_volatility": _realized_volatility_meta_fixture(),
        },
    }


def _filtered_empty_candidate(
    *,
    require_realized_volatility: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    plan = _fetch_plan()
    plan["require_realized_volatility"] = require_realized_volatility
    side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(side_plans, list)
    assert isinstance(requests, list)
    for side_plan in side_plans:
        assert isinstance(side_plan, dict)
        side_plan["required_exact_strikes_by_expiration"] = {}
    for request in requests:
        assert isinstance(request, dict)
        request["include_realized_volatility"] = require_realized_volatility
        request_side_plans = request["side_plans"]
        assert isinstance(request_side_plans, list)
        for side_plan in request_side_plans:
            assert isinstance(side_plan, dict)
            side_plan["required_exact_strikes_by_expiration"] = {}
    payload = _payload()
    payload["rows"] = []
    meta = payload["meta"]
    assert isinstance(meta, dict)
    meta.update(
        {
            "source_outcome": "success_empty",
            "reason_code": "no_contract_rows",
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "realized_volatility": (
                {
                    "status": "not_applicable_no_contracts",
                    "reason": "not_applicable_no_contracts",
                }
                if require_realized_volatility
                else {"status": "skipped", "reason": "not_requested"}
            ),
            "option_chain_scope_coverage": {
                "schema_version": "option_chain_scope_coverage.v1",
                "scopes": [
                    {
                        "option_type": "put",
                        "expiration": "2026-08-21",
                        "chain_status": "fetched",
                        "filtered_contract_codes": [],
                        "filtered_contract_count": 0,
                    }
                ],
            },
        }
    )
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan,
        source="opend",
        host=HOST,
        port=PORT,
    )
    return payload, contract


def _no_expirations_candidate() -> tuple[dict[str, object], dict[str, object]]:
    plan = _fetch_plan()
    plan.update(
        {
            "side_plans": [],
            "merged_requests": [],
            "require_realized_volatility": False,
            "projection_outcome": "success_empty",
            "projected_expirations": [],
        }
    )
    discovery = plan["expiration_discovery"]
    assert isinstance(discovery, dict)
    discovery.update(
        {
            "outcome": "success_empty",
            "reason_code": "no_expirations",
            "expirations": [],
        }
    )
    payload = _payload()
    payload.update({"expiration_count": 0, "expirations": [], "rows": []})
    meta = payload["meta"]
    assert isinstance(meta, dict)
    meta.update(
        {
            "source_outcome": "success_empty",
            "reason_code": "no_expirations",
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
            },
        }
    )
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan,
        source="opend",
        host=HOST,
        port=PORT,
    )
    return payload, contract


def _multi_request_payload() -> dict[str, object]:
    payload = _payload()
    call_row = deepcopy(payload["rows"][0])
    call_row.update(
        {
            "option_type": "call",
            "contract_symbol": CALL_CONTRACT_CODE,
            "strike": 120.0,
            "otm_pct": 0.0909090909,
            "delta": 0.3,
        }
    )
    payload["rows"].append(call_row)
    payload["meta"].update(
        {
            "snapshot_requested_codes": 2,
            "snapshot_returned_codes": 2,
            "snapshot_requested_code_set": [CONTRACT_CODE, CALL_CONTRACT_CODE],
            "snapshot_returned_code_set": [CONTRACT_CODE, CALL_CONTRACT_CODE],
        }
    )
    return payload


def _policy() -> dict[str, object]:
    return {"source": "opend", "host": HOST, "port": PORT}


def _publish(
    *,
    root: Path,
    raw_path: Path,
    csv_path: Path,
) -> None:
    plan = _fetch_plan()
    publish_required_data_quote_snapshot(
        producer_root=root,
        producer_run_id="run-output-integrity",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=plan,
        fetch_policy=_policy(),
        expected_fetch_contract=_contract(),
        source_observed_at=NOW,
        completed_at=COMPLETED_AT,
        now=COMPLETED_AT,
    )


def _receipt_paths(root: Path) -> list[Path]:
    return list(root.glob("source_receipts/quotes/*/*/*/receipt.json"))


def _valid_multi_child_candidate() -> tuple[dict[str, object], dict[str, object]]:
    payload = _multi_request_payload()
    plan = _fetch_plan(request_count=2)
    requests = plan["merged_requests"]
    assert isinstance(requests, list)
    meta = payload["meta"]
    assert isinstance(meta, dict)
    realized_volatility = meta["realized_volatility"]
    assert isinstance(realized_volatility, dict)
    payload["meta"]["request_count"] = 2
    payload["meta"]["requests"] = [
        {
            "request_index": 0,
            "planned_request_sha256": required_data_request_sha256(requests[0]),
            "request_symbol": "NVDA",
            "request_underlier_code": "US.NVDA",
            "source": "opend",
            "host": HOST,
            "port": PORT,
            "trading_date": "2026-08-04",
            "status": "ok",
            "source_outcome": "success_rows",
            "reason_code": None,
            "source_observed_at": NOW.isoformat(),
            "completed_at_utc": (NOW + timedelta(milliseconds=500)).isoformat(),
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [CONTRACT_CODE],
            "snapshot_returned_code_set": [CONTRACT_CODE],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "option_chain_scope_coverage": {
                "schema_version": "option_chain_scope_coverage.v1",
                "scopes": [
                    {
                        "option_type": "put",
                        "expiration": "2026-08-21",
                        "chain_status": "fetched",
                        "filtered_contract_codes": [CONTRACT_CODE],
                        "filtered_contract_count": 1,
                    }
                ],
            },
            "realized_volatility": deepcopy(realized_volatility),
        },
        {
            "request_index": 1,
            "planned_request_sha256": required_data_request_sha256(requests[1]),
            "request_symbol": "NVDA",
            "request_underlier_code": "US.NVDA",
            "source": "opend",
            "host": HOST,
            "port": PORT,
            "trading_date": "2026-08-04",
            "status": "ok",
            "source_outcome": "success_rows",
            "reason_code": None,
            "source_observed_at": (NOW + timedelta(milliseconds=250)).isoformat(),
            "completed_at_utc": COMPLETED_AT.isoformat(),
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [CALL_CONTRACT_CODE],
            "snapshot_returned_code_set": [CALL_CONTRACT_CODE],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "option_chain_scope_coverage": {
                "schema_version": "option_chain_scope_coverage.v1",
                "scopes": [
                    {
                        "option_type": "call",
                        "expiration": "2026-08-21",
                        "chain_status": "cache",
                        "filtered_contract_codes": [CALL_CONTRACT_CODE],
                        "filtered_contract_count": 1,
                    }
                ],
            },
            "realized_volatility": deepcopy(realized_volatility),
        },
    ]
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan,
        source="opend",
        host=HOST,
        port=PORT,
    )
    return payload, contract


def test_validator_accepts_typed_expiry_scoped_rv_unavailability() -> None:
    payload = _payload()
    rows = payload["rows"]
    meta = payload["meta"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    terms = rv_meta["term_matched"]
    assert isinstance(terms, dict)
    term = terms["2026-08-21"]
    assert isinstance(term, dict)
    term.update(
        {
            "status": "data_unavailable",
            "reason": "qfq_history_session_gap",
            "term_matched_rv": None,
            "input_start": None,
            "input_end": None,
            "input_close_session_count": 0,
            "input_return_count": 0,
            "input_hash": None,
            "missing_sessions": ["2026-07-17"],
            "shadow_difference": None,
        }
    )
    rv_meta.update(
        {
            "status": "partial",
            "reason": "term_matched_rv_incomplete",
        }
    )
    rows[0].update(
        {
            "term_matched_rv": None,
            "term_matched_rv_status": "data_unavailable",
            "term_matched_rv_reason": "qfq_history_session_gap",
            "term_matched_rv_input_start": None,
            "term_matched_rv_input_end": None,
            "term_matched_rv_input_session_count": 0,
            "term_matched_rv_input_hash": None,
            "term_matched_rv_shadow_difference": None,
        }
    )

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=_contract(),
    )


@pytest.mark.parametrize("require_realized_volatility", [False, True])
def test_filtered_empty_payload_honors_realized_volatility_authority(
    require_realized_volatility: bool,
) -> None:
    payload, contract = _filtered_empty_candidate(
        require_realized_volatility=require_realized_volatility,
    )

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


def test_no_expirations_without_rv_accepts_no_contract_rv_evidence() -> None:
    payload, contract = _no_expirations_candidate()

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


def test_merge_accepts_proven_empty_child_and_uses_nonempty_child_rv() -> None:
    nonempty = _payload()
    empty = deepcopy(_payload())
    empty["rows"] = []
    empty_meta = empty["meta"]
    assert isinstance(empty_meta, dict)
    empty_meta.update(
        {
            "source_outcome": "success_rows",
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "option_chain_scope_coverage": {
                "schema_version": "option_chain_scope_coverage.v1",
                "scopes": [
                    {
                        "option_type": "call",
                        "expiration": "2026-08-21",
                        "chain_status": "fetched",
                        "filtered_contract_codes": [],
                        "filtered_contract_count": 0,
                    }
                ],
            },
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
            },
        }
    )
    merged = merge_required_data_payloads(
        symbol="NVDA", payloads=[nonempty, empty]
    )

    bind_merged_payload_evidence(
        merged_payload=merged, payloads=[nonempty, empty]
    )

    merged_meta = merged["meta"]
    assert isinstance(merged_meta, dict)
    assert merged_meta["status"] == "ok"
    assert merged_meta["source_outcome"] == "success_rows"
    assert merged_meta["realized_volatility"] == nonempty["meta"][
        "realized_volatility"
    ]


def test_multi_request_payload_accepts_proven_empty_child() -> None:
    payload, _contract_unused = _valid_multi_child_candidate()
    rows = payload["rows"]
    meta = payload["meta"]
    assert isinstance(rows, list) and isinstance(meta, dict)
    rows.pop()
    meta.update(
        {
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_requested_code_set": [CONTRACT_CODE],
            "snapshot_returned_code_set": [CONTRACT_CODE],
        }
    )
    children = meta["requests"]
    assert isinstance(children, list)
    empty_child = children[1]
    assert isinstance(empty_child, dict)
    empty_child.update(
        {
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "option_chain_scope_coverage": {
                "schema_version": "option_chain_scope_coverage.v1",
                "scopes": [
                    {
                        "option_type": "call",
                        "expiration": "2026-08-21",
                        "chain_status": "fetched",
                        "filtered_contract_codes": [],
                        "filtered_contract_count": 0,
                    }
                ],
            },
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
            },
        }
    )
    plan = _fetch_plan(request_count=2)
    top_side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(top_side_plans, list) and isinstance(requests, list)
    call_request = requests[1]
    assert isinstance(call_request, dict)
    call_side_plans = call_request["side_plans"]
    assert isinstance(call_side_plans, list)
    for side_plan in (top_side_plans[1], call_side_plans[0]):
        assert isinstance(side_plan, dict)
        side_plan["required_exact_strikes_by_expiration"] = {}
    empty_child["planned_request_sha256"] = required_data_request_sha256(
        call_request
    )
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan,
        source="opend",
        host=HOST,
        port=PORT,
    )

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


def _assert_fresh_finalizer_rejects_without_artifacts(
    *,
    tmp_path: Path,
    payload: dict[str, object],
    expected_fetch_contract: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(SourceReceiptError, match=match):
        finalize_required_data_quote_candidate(
            base=tmp_path,
            producer_root=tmp_path,
            producer_run_id="run-rejected-candidate",
            symbol="NVDA",
            expected_fetch_contract=expected_fetch_contract,
            fetch_policy=_policy(),
            mode="fresh",
            payload=payload,
            now=COMPLETED_AT,
        )

    assert _receipt_paths(tmp_path) == []
    assert not (tmp_path / "raw" / "NVDA_required_data.json").exists()
    assert not (tmp_path / "parsed" / "NVDA_required_data.csv").exists()


@pytest.mark.parametrize("trading_date", [None, "2026-08-05"])
def test_finalizer_rejects_missing_or_wrong_aggregate_trading_date_without_artifacts(
    tmp_path: Path,
    trading_date: str | None,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    if trading_date is None:
        meta.pop("trading_date")
    else:
        meta["trading_date"] = trading_date

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="trading date mismatch",
    )


@pytest.mark.parametrize("trading_date", [None, "2026-08-05"])
def test_finalizer_rejects_missing_or_differing_child_trading_date_without_artifacts(
    tmp_path: Path,
    trading_date: str | None,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    children = meta["requests"]
    assert isinstance(children, list)
    child = children[1]
    assert isinstance(child, dict)
    if trading_date is None:
        child.pop("trading_date")
    else:
        child["trading_date"] = trading_date

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=contract,
        match="trading date mismatch",
    )


def test_finalizer_rejects_boolean_required_rv_without_artifacts(
    tmp_path: Path,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    rv_meta["realized_volatility_estimate"] = True

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="lacks required realized volatility",
    )


def test_finalizer_rejects_meta_and_row_rv_mismatch_without_artifacts(
    tmp_path: Path,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    rv_meta["realized_volatility_20"] = 0.2

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="contradict canonical realized volatility",
    )


@pytest.mark.parametrize(
    "field",
    [
        "realized_volatility_20",
        "realized_volatility_60",
        "realized_volatility_120",
    ],
)
def test_finalizer_rejects_meta_and_row_rv_window_mismatch_without_artifacts(
    tmp_path: Path,
    field: str,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    rv_meta[field] = 9.99

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="contradict canonical realized volatility",
    )


@pytest.mark.parametrize(
    ("target", "field", "replacement"),
    [
        ("meta", "realized_volatility_20", float("inf")),
        ("row", "realized_volatility_60", True),
        ("row", "realized_volatility_120", -0.1),
    ],
)
def test_finalizer_rejects_invalid_rv_window_evidence_without_artifacts(
    tmp_path: Path,
    target: str,
    field: str,
    replacement: object,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    row = payload["rows"][0]
    assert isinstance(row, dict)
    evidence = rv_meta if target == "meta" else row
    evidence[field] = replacement

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="required realized volatility",
    )


def test_finalizer_rejects_missing_explicit_rv_window_without_artifacts(
    tmp_path: Path,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    rv_meta = meta["realized_volatility"]
    assert isinstance(rv_meta, dict)
    rv_meta.pop("realized_volatility_120")

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="lacks required realized volatility",
    )


@pytest.mark.parametrize("field", ["source_observed_at", "completed_at_utc"])
def test_finalizer_rejects_timezone_naive_timestamps_without_artifacts(
    tmp_path: Path,
    field: str,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    meta[field] = datetime(2026, 8, 4, 2, 0).isoformat()

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="stable observation timestamps",
    )


@pytest.mark.parametrize("port", [11111.0, True])
def test_finalizer_rejects_non_integer_raw_fetch_port_without_artifacts(
    tmp_path: Path,
    port: object,
) -> None:
    payload = _payload()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    meta["port"] = port

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=_contract(),
        match="payload port is invalid",
    )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("mid", 9.9),
        ("bid", 9.8),
        ("spot", 999.0),
        ("implied_volatility", 0.99),
        ("realized_volatility_estimate", 0.88),
        ("dte", 99),
    ],
)
def test_receipt_rejects_consumer_csv_value_drift(
    tmp_path: Path,
    column: str,
    replacement: float,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _payload(),
        output_root=tmp_path,
    )
    frame = pd.read_csv(csv_path)
    frame.loc[0, column] = replacement
    frame.to_csv(csv_path, index=False)

    with pytest.raises(
        SourceReceiptError,
        match="canonical projections differ",
    ):
        _publish(root=tmp_path, raw_path=raw_path, csv_path=csv_path)

    assert _receipt_paths(tmp_path) == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_receipt_rejects_duplicate_requested_contract_rows(
    tmp_path: Path,
    conflicting: bool,
) -> None:
    payload = _payload()
    duplicate = deepcopy(payload["rows"][0])
    if conflicting:
        duplicate["mid"] = 9.9
    payload["rows"].append(duplicate)
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload,
        output_root=tmp_path,
    )

    with pytest.raises(SourceReceiptError, match=r"^invalid_row_identity:"):
        _publish(root=tmp_path, raw_path=raw_path, csv_path=csv_path)

    assert _receipt_paths(tmp_path) == []


def test_direct_publisher_cannot_claim_unattested_multiplier_enrichment(
    tmp_path: Path,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _payload(multiplier=None),
        output_root=tmp_path,
    )
    frame = pd.read_csv(csv_path)
    frame.loc[0, "multiplier"] = 100.0
    frame.to_csv(csv_path, index=False)

    with pytest.raises(
        SourceReceiptError,
        match="metadata does not bind canonical bytes",
    ):
        _publish(root=tmp_path, raw_path=raw_path, csv_path=csv_path)

    assert _receipt_paths(tmp_path) == []


def test_finalizer_attests_multiplier_and_final_csv_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import multiplier_cache

    monkeypatch.setattr(
        multiplier_cache,
        "resolve_multiplier",
        lambda **_kwargs: 100.0,
    )
    payload = _payload(multiplier=None)
    result = finalize_required_data_quote_candidate(
        base=tmp_path,
        producer_root=tmp_path,
        producer_run_id="run-finalized",
        symbol="NVDA",
        expected_fetch_contract=_contract(),
        fetch_policy=_policy(),
        mode="fresh",
        payload=payload,
        now=COMPLETED_AT,
    )

    csv_path = result["csv_path"]
    frame = pd.read_csv(csv_path)
    metadata = json.loads(
        quote_cache_metadata_path(csv_path).read_text(encoding="utf-8")
    )
    assert frame.loc[0, "multiplier"] == 100.0
    assert metadata["csv_sha256"] == hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()
    assert datetime.fromisoformat(
        metadata["source_observed_at"].replace("Z", "+00:00")
    ) == NOW
    assert result["quote_receipt_path"].is_file()


def test_save_outputs_metadata_uses_raw_observation_time(tmp_path: Path) -> None:
    _raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _payload(),
        output_root=tmp_path,
    )
    metadata = json.loads(
        quote_cache_metadata_path(csv_path).read_text(encoding="utf-8")
    )

    assert datetime.fromisoformat(
        metadata["source_observed_at"].replace("Z", "+00:00")
    ) == NOW
    assert metadata["csv_sha256"] == hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()


def test_cached_finalizer_preserves_source_observation_and_run_id(
    tmp_path: Path,
) -> None:
    save_outputs(
        tmp_path,
        "NVDA",
        _payload(),
        output_root=tmp_path,
    )
    csv_path = tmp_path / "parsed" / "NVDA_required_data.csv"
    metadata_path = quote_cache_metadata_path(csv_path)
    before = json.loads(metadata_path.read_text(encoding="utf-8"))

    finalize_required_data_quote_candidate(
        base=tmp_path,
        producer_root=tmp_path,
        producer_run_id="later-consumer-run",
        symbol="NVDA",
        expected_fetch_contract=_contract(),
        fetch_policy=_policy(),
        mode="cached",
        now=COMPLETED_AT + timedelta(minutes=5),
    )

    after = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert after["source_observed_at"] == before["source_observed_at"]
    assert after["source_run_id"] == before["source_run_id"]
    assert after["csv_sha256"] == hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("child_count", [0, 1, 3])
def test_multi_request_payload_requires_exact_child_timestamp_evidence(
    child_count: int,
) -> None:
    payload = _multi_request_payload()
    child = {
        "source_observed_at": NOW.isoformat(),
        "completed_at_utc": COMPLETED_AT.isoformat(),
    }
    payload["meta"]["requests"] = [deepcopy(child) for _ in range(child_count)]

    with pytest.raises(
        SourceReceiptError,
        match="child",
    ):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=_contract(request_count=2),
        )


def test_multi_request_payload_accepts_exact_stable_child_timestamps_and_rv() -> None:
    payload, contract = _valid_multi_child_candidate()

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong_schema", "wrong_status", "wrong_code", "wrong_count"],
)
def test_multi_request_payload_rejects_scope_coverage_tampering(case: str) -> None:
    payload, contract = _valid_multi_child_candidate()
    children = payload["meta"]["requests"]
    assert isinstance(children, list)
    scope_evidence = children[1]["option_chain_scope_coverage"]
    assert isinstance(scope_evidence, dict)
    scopes = scope_evidence["scopes"]
    assert isinstance(scopes, list)
    scope = scopes[0]
    assert isinstance(scope, dict)
    if case == "missing":
        children[1].pop("option_chain_scope_coverage")
    elif case == "wrong_schema":
        scope_evidence["schema_version"] = "option_chain_scope_coverage.v0"
    elif case == "wrong_status":
        scope["chain_status"] = "stale_cache"
    elif case == "wrong_code":
        scope["filtered_contract_codes"] = [CONTRACT_CODE]
    else:
        scope["filtered_contract_count"] = 2

    with pytest.raises(SourceReceiptError, match="does not cover"):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


@pytest.mark.parametrize("case", ["missing", "status_error"])
def test_finalizer_rejects_invalid_child_required_rv_without_artifacts(
    tmp_path: Path,
    case: str,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    children = meta["requests"]
    assert isinstance(children, list)
    child = children[1]
    assert isinstance(child, dict)
    if case == "missing":
        child.pop("realized_volatility")
    else:
        rv_meta = child["realized_volatility"]
        assert isinstance(rv_meta, dict)
        rv_meta["status"] = "error"

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=contract,
        match="required realized volatility",
    )


@pytest.mark.parametrize(
    "field",
    [
        "realized_volatility_20",
        "realized_volatility_60",
        "realized_volatility_120",
        "realized_volatility_estimate",
    ],
)
def test_finalizer_rejects_child_required_rv_drift_without_artifacts(
    tmp_path: Path,
    field: str,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    children = meta["requests"]
    assert isinstance(children, list)
    child = children[1]
    assert isinstance(child, dict)
    rv_meta = child["realized_volatility"]
    assert isinstance(rv_meta, dict)
    rv_meta[field] = 9.99

    _assert_fresh_finalizer_rejects_without_artifacts(
        tmp_path=tmp_path,
        payload=payload,
        expected_fetch_contract=contract,
        match="child request realized volatility mismatch",
    )


def test_finalizer_accepts_matching_child_required_rv_with_none_window(
    tmp_path: Path,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    rows = payload["rows"]
    assert isinstance(meta, dict)
    assert isinstance(rows, list)
    aggregate_rv = meta["realized_volatility"]
    children = meta["requests"]
    assert isinstance(aggregate_rv, dict)
    assert isinstance(children, list)
    aggregate_rv["realized_volatility_120"] = None
    for row in rows:
        assert isinstance(row, dict)
        row["realized_volatility_120"] = None
    for child in children:
        assert isinstance(child, dict)
        child_rv = child["realized_volatility"]
        assert isinstance(child_rv, dict)
        child_rv["realized_volatility_120"] = None

    result = finalize_required_data_quote_candidate(
        base=tmp_path,
        producer_root=tmp_path,
        producer_run_id="run-matching-child-rv",
        symbol="NVDA",
        expected_fetch_contract=contract,
        fetch_policy=_policy(),
        mode="fresh",
        payload=payload,
        now=COMPLETED_AT,
    )

    assert result["quote_receipt_path"].is_file()
    assert len(_receipt_paths(tmp_path)) == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong_hash",
        "duplicate_hash",
        "duplicate_index",
        "wrong_binding",
        "wrong_symbol",
        "wrong_underlier",
        "failed_child",
    ],
)
def test_multi_request_payload_rejects_child_identity_or_outcome_drift(
    case: str,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    children = payload["meta"]["requests"]
    assert isinstance(children, list)
    if case == "wrong_hash":
        children[0]["planned_request_sha256"] = "0" * 64
    elif case == "duplicate_hash":
        children[1]["planned_request_sha256"] = children[0][
            "planned_request_sha256"
        ]
    elif case == "duplicate_index":
        children[1]["request_index"] = children[0]["request_index"]
    elif case == "wrong_binding":
        children[1]["port"] = PORT + 1
    elif case == "wrong_symbol":
        children[1]["request_symbol"] = "AAPL"
    elif case == "wrong_underlier":
        children[1]["request_underlier_code"] = "US.AAPL"
    else:
        children[1]["status"] = "error"

    with pytest.raises(SourceReceiptError, match="child request"):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


def _apply_child_coverage_drift(
    payload: dict[str, object],
    *,
    case: str,
) -> None:
    meta = payload["meta"]
    rows = payload["rows"]
    assert isinstance(meta, dict)
    assert isinstance(rows, list)
    children = meta["requests"]
    assert isinstance(children, list)
    first = children[0]
    second = children[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    if case == "empty":
        first.update(
            {
                "snapshot_requested_codes": 0,
                "snapshot_returned_codes": 0,
                "snapshot_requested_code_set": [],
                "snapshot_returned_code_set": [],
            }
        )
    elif case == "swapped":
        first["snapshot_requested_code_set"] = [CALL_CONTRACT_CODE]
        first["snapshot_returned_code_set"] = [CALL_CONTRACT_CODE]
        second["snapshot_requested_code_set"] = [CONTRACT_CODE]
        second["snapshot_returned_code_set"] = [CONTRACT_CODE]
    elif case == "foreign":
        second["snapshot_requested_code_set"] = ["US.NVDA.FOREIGN"]
        second["snapshot_returned_code_set"] = ["US.NVDA.FOREIGN"]
    elif case == "wrong_side":
        rows[1]["option_type"] = "put"
    elif case == "expiry":
        rows[1]["expiration"] = "2026-09-18"
        rows[1]["dte"] = 45
    elif case == "window":
        rows[1]["strike"] = 130.0
    elif case == "missing":
        second.update(
            {
                "snapshot_returned_codes": 0,
                "snapshot_missing_codes": 1,
                "snapshot_returned_code_set": [],
                "snapshot_missing_code_set": [CALL_CONTRACT_CODE],
                "snapshot_complete": False,
            }
        )
    elif case == "unexpected":
        second.update(
            {
                "snapshot_returned_codes": 2,
                "snapshot_unexpected_codes": 1,
                "snapshot_returned_code_set": [
                    CALL_CONTRACT_CODE,
                    "US.NVDA.FOREIGN",
                ],
                "snapshot_unexpected_code_set": ["US.NVDA.FOREIGN"],
            }
        )
    else:
        raise AssertionError(f"unknown child drift case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "swapped",
        "foreign",
        "wrong_side",
        "expiry",
        "window",
        "missing",
    ],
)
def test_multi_request_payload_rejects_child_coverage_drift(case: str) -> None:
    payload, contract = _valid_multi_child_candidate()
    _apply_child_coverage_drift(payload, case=case)

    with pytest.raises(SourceReceiptError, match="child request"):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


def test_multi_request_payload_accepts_quarantined_unexpected_code() -> None:
    payload, contract = _valid_multi_child_candidate()
    _apply_child_coverage_drift(payload, case="unexpected")

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


def test_multi_request_payload_accepts_child_list_reordering() -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    children = meta["requests"]
    assert isinstance(children, list)
    children.reverse()

    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=contract,
    )


@pytest.mark.parametrize("case", ["wrong_side", "expiry"])
def test_child_scope_drift_has_scope_identity_reason(case: str) -> None:
    payload, contract = _valid_multi_child_candidate()
    _apply_child_coverage_drift(payload, case=case)

    with pytest.raises(
        SourceReceiptError,
        match=r"^scope_identity_mismatch:",
    ):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


def test_child_hash_drift_has_scope_identity_reason() -> None:
    payload, contract = _valid_multi_child_candidate()
    children = payload["meta"]["requests"]
    assert isinstance(children, list)
    children[0]["planned_request_sha256"] = "0" * 64

    with pytest.raises(
        SourceReceiptError,
        match=r"^scope_identity_mismatch:",
    ):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


def test_child_index_swap_has_internal_contract_reason() -> None:
    payload, contract = _valid_multi_child_candidate()
    children = payload["meta"]["requests"]
    assert isinstance(children, list)
    children[0]["request_index"] = 1
    children[1]["request_index"] = 0

    with pytest.raises(
        SourceReceiptError,
        match=r"^internal_contract_error:",
    ):
        validate_required_data_payload_candidate(
            payload=payload,
            expected_fetch_contract=contract,
        )


def test_direct_publisher_rejects_child_coverage_drift_without_receipt(
    tmp_path: Path,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    _apply_child_coverage_drift(payload, case="swapped")
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload,
        output_root=tmp_path,
    )
    plan = _fetch_plan(request_count=2)

    with pytest.raises(SourceReceiptError, match="child request"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-child-drift",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=plan,
            fetch_policy=_policy(),
            expected_fetch_contract=contract,
            source_observed_at=NOW,
            completed_at=COMPLETED_AT,
            now=COMPLETED_AT,
        )

    assert _receipt_paths(tmp_path) == []


def test_direct_publisher_rejects_child_rv_drift_without_receipt(
    tmp_path: Path,
) -> None:
    payload, contract = _valid_multi_child_candidate()
    meta = payload["meta"]
    assert isinstance(meta, dict)
    children = meta["requests"]
    assert isinstance(children, list)
    child = children[1]
    assert isinstance(child, dict)
    child_rv = child["realized_volatility"]
    assert isinstance(child_rv, dict)
    child_rv["realized_volatility_estimate"] = 0.88
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload,
        output_root=tmp_path,
    )

    with pytest.raises(
        SourceReceiptError,
        match="child request realized volatility mismatch",
    ):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-child-rv-drift",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=_fetch_plan(request_count=2),
            fetch_policy=_policy(),
            expected_fetch_contract=contract,
            source_observed_at=NOW,
            completed_at=COMPLETED_AT,
            now=COMPLETED_AT,
        )

    assert _receipt_paths(tmp_path) == []
