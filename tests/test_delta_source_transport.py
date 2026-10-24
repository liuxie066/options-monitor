from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from src.application.candidate_models import CandidateContractInput
from src.application.candidate_scanning import _load_required_data_rows
from src.application.opening_quote_evidence import normalize_option_observation
from src.application.opend_symbol_outputs import _csv_roundtrip_frame, save_outputs
from test_opening_quote_evidence import _chain, _snapshot, _ready_underlier, _RECEIPT, _US_NOW


def observation(snapshot=None, chain=None):
    return normalize_option_observation(
        expected_owner="US.NVDA", market="US", currency="USD",
        chain_row=chain or _chain(option_type="PUT"),
        snapshot_row=_snapshot() if snapshot is None else snapshot,
        underlier_observation=_ready_underlier(), now_utc=_US_NOW, **_RECEIPT,
    )


@pytest.mark.parametrize("value", [-0.40, math.nextafter(-0.40, -math.inf), 0.0, -0.0])
def test_canonical_delta_preserves_provider_precision(value):
    result = observation(_snapshot(option_delta=value))
    assert result.delta == value
    assert result.delta_source == "opend_market_snapshot.option_delta"
    assert result.delta_unit == "fraction"
    assert result.delta_reason_code is None
    assert result.snapshot_received_at_utc == _RECEIPT["snapshot_received_at_utc"]
    assert result.status == "ready"


@pytest.mark.parametrize(("value", "reason", "retained"), [
    (None, "delta_missing", None),
    (True, "delta_invalid_type", None),
    ("0.35", "delta_invalid_type", None),
    (float("nan"), "delta_non_finite", None),
    (float("inf"), "delta_non_finite", None),
    (-float("inf"), "delta_non_finite", None),
    (-35.0, "delta_out_of_range", -35.0),
    (0.2, "delta_sign_invalid", 0.2),
])
def test_canonical_invalid_never_falls_back_or_changes_general_readiness(value, reason, retained):
    result = observation(_snapshot(option_delta=value, delta=-0.1))
    assert result.delta == retained
    assert result.delta_reason_code == reason
    assert result.status == "ready"
    json.dumps(result.to_dict(), allow_nan=False)


def test_alias_only_keeps_display_but_cannot_certify_source():
    snapshot = _snapshot(delta=-0.3)
    del snapshot["option_delta"]
    result = observation(snapshot)
    assert result.delta == -0.3
    assert result.delta_source is None
    assert result.delta_unit is None
    assert result.delta_reason_code == "delta_source_unverified"


@pytest.mark.parametrize("snapshot,chain", [
    (_snapshot(code="OTHER"), _chain()),
    (_snapshot(), _chain(stock_owner="US.OTHER")),
])
def test_mismatched_identity_cannot_certify_delta(snapshot, chain):
    result = observation(snapshot, chain)
    assert result.delta_source is None
    assert result.delta_reason_code == "delta_source_unverified"


@pytest.mark.parametrize("mode,delta", [
    ("put", -0.4), ("put", math.nextafter(-0.4, -math.inf)),
    ("call", 0.4), ("call", math.nextafter(0.4, math.inf)),
])
def test_actual_required_data_writer_reader_retains_double_and_provenance(tmp_path, mode, delta):
    result = observation(_snapshot(option_delta=delta), _chain(option_type=mode.upper()))
    row = {
        **result.to_dict(), "symbol": "NVDA", "option_type": mode,
        "expiration": "2026-08-21", "dte": 15, "strike": 170,
        "market": "US", "delta": delta,
    }
    save_outputs(tmp_path, "NVDA", {"rows": [row], "meta": {}},
                 output_root=tmp_path, publish_cache_metadata=False)
    loaded = _load_required_data_rows(input_root=tmp_path, symbol="NVDA", mode=mode)
    assert len(loaded) == 1
    payload = CandidateContractInput.from_row(loaded.iloc[0], mode=mode).to_gate_payload()
    assert payload["delta"] == delta
    assert (abs(payload["delta"]) > 0.4) == (abs(delta) > 0.4)
    assert payload["delta_source"] == result.delta_source
    assert payload["delta_unit"] == "fraction"
    assert payload["delta_reason_code"] is None
    assert payload["snapshot_received_at_utc"] == result.snapshot_received_at_utc
    assert _csv_roundtrip_frame([row]).iloc[0]["delta"] == delta


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0.2"])
def test_typed_candidate_never_launders_invalid_delta(value):
    payload = CandidateContractInput.from_row(pd.Series({"delta": value}), mode="put").to_gate_payload()
    assert payload["delta"] is None
    assert payload["delta_reason_code"] is not None
    json.dumps(payload, allow_nan=False)


def test_unverifiable_delta_is_unresolved_instead_of_success_empty():
    from src.application.candidate_scanning import evidence_summary_from_decisions
    from src.application.sell_call_steps import _evidence_scan_status as call_status
    from src.application.sell_put_steps import _evidence_scan_status as put_status

    summary = evidence_summary_from_decisions(decisions=[{"opening_decision": {
        "accepted": False, "rejects": [{"reason": "risk_delta_unavailable",
            "metric_value": {"reason_code": "delta_source_unverified"}}],
    }}], accepted_count=0)
    assert summary["eligibility_unresolved_count"] == 1
    assert summary["policy_rejected_count"] == 0
    assert summary["unavailable_by_reason"] == {"delta_source_unverified": 1}
    assert put_status(evidence=summary, candidate_count=0) == ("unavailable", "data_unavailable")
    assert call_status(evidence=summary, candidate_count=0) == ("unavailable", "data_unavailable")
