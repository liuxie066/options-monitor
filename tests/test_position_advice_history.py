import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.close_advice import STRICT_CLOSE_POLICY_VERSION
from src.application.close_advice_report_manifest import publish_close_advice_report_manifest
from src.application.position_advice_history import find_position_reference
from src.application.position_advice_inputs import normalize_manual_positions, position_economics_sha256
from test_position_advice_inputs import manual

NOW = datetime(2026, 8, 2, 15, tzinfo=timezone.utc)
ASOF = "2026-07-31T19:30:00+00:00"


def report(tmp_path: Path, position, **overrides):
    tmp_path.mkdir(exist_ok=True, parents=True)
    row = {"account": "sample", "symbol": "NVDA", "position_lot_id": "lot-1", "broker": "富途", "position_side": "short", "option_type": "put", "expiration": "2026-08-28", "strike": 100, "contracts_open": 1, "multiplier": 100, "currency": "USD", "premium": 2,
           "quote_mode": "frozen_snapshot", "required_data_snapshot_manifest_sha256": "a" * 64, "close_advice_required_data_plan_sha256": "b" * 64,
           "assessment_as_of_utc": ASOF, "assessment_mode": "current", "current_actionable": True, "policy_version": STRICT_CLOSE_POLICY_VERSION,
           "market_data_provider": "opend", "position_economics_sha256": position_economics_sha256(position), "recommendation_state": "close", "evaluation_status": "priced", "decision_evidence_status": "complete", "dte": 28, "spot": 110, "ask": 0.1, "bid": 0.09, "capital_basis": 10000, "remaining_max_annualized_return": 0.01, "net_capture_ratio": 0.94, **overrides}
    path = tmp_path / "close_advice.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    text = tmp_path / "close_advice.txt"
    text.write_text("historical assessment")
    publish_close_advice_report_manifest(csv_path=path, text_path=text, context_path=tmp_path / "unused", context={"filters": {"account": "sample"}}, rows=[row], markets_to_run=["US"], run_id="old-run", quote_mode="frozen_snapshot", required_data_snapshot_manifest_sha256="a" * 64, close_advice_required_data_plan_sha256="b" * 64)
    return path


def position(**overrides):
    return normalize_manual_positions([manual(input_as_of_utc="2026-07-30T15:00:00Z", **overrides)], account="sample", broker="富途", now_utc=NOW)[0]


def test_closed_reference_preserves_original_prices_and_dte_without_fake_lot(tmp_path):
    pos = position()
    path = report(tmp_path, pos)
    original = path.read_bytes()
    row = find_position_reference(pos, report_paths=[path], provider="opend", now_utc=NOW)
    assert row["assessment_mode"] == "historical_reference"
    assert row["reference_recommendation_state"] == "close"
    assert row["recommendation_state"] == "not_evaluable"
    assert row["current_actionable"] is False
    assert row["dte"] == "28" and row["assessment_as_of_utc"] == ASOF
    assert row["spot"] == "110" and row["ask"] == "0.1"
    assert row["position_lot_id"] is None and row["input_id"] == "user-1"
    assert path.read_bytes() == original


@pytest.mark.parametrize("override", [{"market_data_provider": "another"}, {"assessment_mode": "unavailable"}, {"current_actionable": False}, {"policy_version": "old"}, {"position_economics_sha256": "wrong"}, {"assessment_as_of_utc": "2026-08-03T12:00:00Z"}, {"account": "other"}])
def test_incompatible_reference_unavailable(tmp_path, override):
    pos = position()
    path = report(tmp_path, pos, **override)
    assert find_position_reference(pos, report_paths=[path], provider="opend", now_utc=NOW) is None


def test_manual_created_after_snapshot_and_tampered_report_rejected(tmp_path):
    pos = position()
    path = report(tmp_path, pos)
    newer = {**pos, "input_as_of_utc": "2026-08-01T12:00:00Z"}
    assert find_position_reference(newer, report_paths=[path], provider="opend", now_utc=NOW) is None
    path.write_bytes(path.read_bytes() + b"\n")
    assert find_position_reference(pos, report_paths=[path], provider="opend", now_utc=NOW) is None


def test_latest_compatible_observation_selected_not_path_mtime(tmp_path):
    pos = position()
    older = report(tmp_path / "older", pos, assessment_as_of_utc="2026-07-30T19:30:00Z")
    newer = report(tmp_path / "newer", pos)
    row = find_position_reference(pos, report_paths=[newer, older], provider="opend", now_utc=NOW)
    assert row["assessment_as_of_utc"] == ASOF
