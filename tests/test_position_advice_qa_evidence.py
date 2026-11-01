"""Independent offline QA: no sockets, services, or production artifacts.

Reuses the existing sealed-workspace producer rather than bypassing integrity.
Tests assert the required behavior, including outstanding review regressions.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from domain.domain.close_advice import select_close_advice_notification_rows
from src.application.close_advice_runner import run_close_advice
from src.application.position_advice_evidence import current_position_advice_evidence
from test_close_advice_required_data import _frozen_workspace


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("independent evidence QA forbids sockets and subprocesses")

    # PyCryptodome's SDK import detects the local interpreter architecture.
    # Permit exactly that read-only probe, never an application child process.
    original_popen = subprocess.Popen
    def guarded_popen(command, *args, **kwargs):
        if (isinstance(command, list) and len(command) == 3 and command[:2] == ["file", "-b"]
            and Path(command[2]).resolve() == Path(sys.executable).resolve()
            and not kwargs.get("shell")):
            return original_popen(command, *args, **kwargs)
        return forbidden()

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


def _runner_case(tmp_path, **changes):
    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    expiration = (started + timedelta(days=20)).date().isoformat()
    quote_fields = {"option_sec_status": "NORMAL", "option_suspension": False,
                    **changes.pop("quote_fields", {})}
    frozen = _frozen_workspace(
        tmp_path, run_started_at_utc=started, quote_expiration=expiration,
        quote_ask=0.1, quote_delta=-0.3, quote_fields=quote_fields, **changes,
    )
    result = run_close_advice(**frozen.run_kwargs(tmp_path))
    with (frozen.output_dir / "close_advice.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert result["snapshot_authority"] == "valid", result
    assert len(rows) == 1
    return result, rows[0], frozen


def _assert_nonactionable(result, row):
    assert row["recommendation_state"] == "not_evaluable", row
    assert row["current_actionable"] == "False", row
    assert row["assessment_mode"] == "unavailable", row
    assert result["notify_rows"] == 0
    assert result["notification_text"] == ""
    assert select_close_advice_notification_rows([row]) == []


def test_qa_positive_control_uses_real_sealed_runner(tmp_path):
    result, row, _ = _runner_case(tmp_path)
    assert row["recommendation_state"] == "close"
    assert row["current_actionable"] == "True"
    assert row["assessment_mode"] == "current"
    assert result["notify_rows"] == 1  # Selection only; this runner never sends.


def test_qa_missing_premium_resets_actionability_at_runner_boundary(tmp_path):
    result, row, _ = _runner_case(tmp_path, position_fields={"premium_open": None})
    _assert_nonactionable(result, row)
    assert row["bid"] and row["ask"] and row["spot"]
    assert row["decision_evidence_status"] == "not_evaluable"


def test_qa_unavailable_fee_resets_actionability_at_runner_boundary(tmp_path, monkeypatch):
    def unavailable_fee(*_args, **_kwargs):
        raise ValueError("isolated QA fee calculation unavailable")
    monkeypatch.setattr("src.application.close_advice_runner.calc_futu_option_fee", unavailable_fee)
    result, row, _ = _runner_case(tmp_path)
    _assert_nonactionable(result, row)
    assert row["bid"] and row["ask"] and row["spot"]
    assert row["fee_calc_status"] == "unavailable"


@pytest.mark.parametrize("field", ["underlier_right", "option_right"])
@pytest.mark.parametrize("right", ["BMP", "NO", "SF", "UNKNOWN", None])
def test_qa_unknown_or_delayed_rights_never_close(tmp_path, field, right):
    rights = {"status": "ok", "market": "US", "underlier_right": "LV1",
              "option_right": "LV2", field: right}
    result, row, _ = _runner_case(tmp_path, quote_rights=rights)
    _assert_nonactionable(result, row)
    assert float(row["net_capture_ratio"]) > 0.8
    assert float(row["estimated_pnl_if_close_net"]) > 0


@pytest.mark.parametrize("state", ["CLOSED", "REST", "PRE_MARKET_BEGIN", "AFTER_HOURS_BEGIN", "UNKNOWN"])
def test_qa_closed_or_unknown_post_snapshot_state_never_close(tmp_path, state):
    result, row, _ = _runner_case(tmp_path, market_state=state)
    _assert_nonactionable(result, row)


@pytest.mark.parametrize("field,value", [
    ("snapshot_requested_at_utc", None),
    ("spot_observed_at_utc", None),
    ("spot_observed_at_utc", "1999-01-01T15:00:00Z"),
    ("underlier_sec_status", "HALT"),
    ("underlier_suspension", True),
])
def test_qa_missing_or_invalid_underlier_time_and_state_never_close(tmp_path, field, value):
    result, row, _ = _runner_case(tmp_path, quote_fields={field: value})
    _assert_nonactionable(result, row)


@pytest.mark.parametrize("fields", [
    {"option_sec_status": "HALT", "option_suspension": True,
     "opening_contract_status": "unavailable",
     "opening_contract_reason_codes": ["option_suspended"]},
    {"option_sec_status": "NORMAL", "option_suspension": True,
     "opening_contract_status": "unavailable",
     "opening_contract_reason_codes": ["option_suspended"]},
])
def test_qa_halted_option_never_produces_current_close(tmp_path, fields):
    result, row, _ = _runner_case(tmp_path, quote_fields=fields)
    _assert_nonactionable(result, row)


@pytest.mark.parametrize("field,value", [
    ("option_sec_status", None),
    ("option_sec_status", ""),
    ("option_sec_status", "UNKNOWN"),
    ("option_sec_status", "N/A"),
    ("option_sec_status", "HALT"),
    ("option_sec_status", "DELISTED"),
    ("option_suspension", None),
    ("option_suspension", ""),
    ("option_suspension", "UNKNOWN"),
    ("option_suspension", "N/A"),
    ("option_suspension", "nan"),
    ("option_suspension", True),
])
def test_qa_unknown_option_tradeability_never_close(tmp_path, field, value):
    result, row, _ = _runner_case(tmp_path, quote_fields={field: value})
    _assert_nonactionable(result, row)
    assert float(row["net_capture_ratio"]) > 0.8
    assert float(row["estimated_pnl_if_close_net"]) > 0


def test_qa_zero_bid_with_valid_ask_preserves_current_buyback(tmp_path):
    result, row, _ = _runner_case(tmp_path, quote_fields={
        "bid": 0, "opening_contract_status": "ineligible",
        "opening_contract_reason_codes": ["option_no_current_bid"],
    })
    assert float(row["bid"]) == 0 and float(row["ask"]) > 0
    assert row["recommendation_state"] == "close"
    assert row["current_actionable"] == "True"
    assert row["assessment_mode"] == "current"
    assert result["notify_rows"] == 1


def test_qa_daily_brief_consumer_does_not_select_halted_option(tmp_path):
    from src.application.daily_decision_brief_service import _load_close_advice
    result, row, frozen = _runner_case(tmp_path, quote_fields={
        "option_sec_status": "HALT", "option_suspension": True,
        "opening_contract_status": "ineligible",
        "opening_contract_reason_codes": ["option_suspended", "option_security_not_normal"],
    })
    gaps = []
    rows, available = _load_close_advice(
        path=frozen.output_dir / "close_advice.csv", run_account_dir=frozen.output_dir,
        market="US", account="lx", run_id="run-1", source_artifacts=[], data_gaps=gaps,
    )
    assert available and not gaps
    selected = select_close_advice_notification_rows(rows)
    assert not selected, {
        "runner_notify_rows": result["notify_rows"],
        "current_actionable": row["current_actionable"],
        "assessment_mode": row["assessment_mode"],
        "recommendation_state": row["recommendation_state"],
        "brief_selected_count": len(selected),
    }


def test_qa_old_last_trade_does_not_invalidate_fresh_bid_ask_receipt(tmp_path):
    result, row, _ = _runner_case(tmp_path, quote_fields={
        "last_price_update_time": "1999-01-01 10:00:00",
        "last_price_observed_at_utc": "1999-01-01T15:00:00Z",
    })
    assert row["recommendation_state"] == "close"
    assert row["assessment_mode"] == "current"
    assert result["notify_rows"] == 1


def test_qa_underlier_received_after_option_is_not_current(tmp_path):
    result, row, _ = _runner_case(tmp_path, quote_fields={
        "spot_observed_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    _assert_nonactionable(result, row)


@pytest.mark.parametrize("market", ["US", "HK"])
def test_qa_exact_freshness_boundary_and_future_proofs(market):
    now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    def assess(age):
        instant = (now - timedelta(seconds=age)).isoformat()
        return current_position_advice_evidence(
            position={}, market=market, now_utc=now,
            quote={"snapshot_requested_at_utc": instant,
                   "snapshot_received_at_utc": instant,
                   "spot_observed_at_utc": instant,
                   "underlier_observation_status": "ready",
                   "underlier_sec_status": "NORMAL", "underlier_suspension": False,
                   "option_sec_status": "NORMAL", "option_suspension": False,
                   "market_state": "MORNING"},
            requirement={"market_state_after_snapshot": "MORNING",
                         "market_state_received_at_utc": instant,
                         "quote_rights_received_at_utc": instant,
                         "quote_rights_status": "ok", "quote_rights_market": market,
                         "quote_rights_underlier": "LV1", "quote_rights_option": "LV2"},
        )
    assert assess(300)[0]["current_actionable"] is True
    assert assess(301)[0]["current_actionable"] is False
    assert assess(-1)[0]["current_actionable"] is False


def test_qa_snapshot_tamper_invalidates_report_without_notification(tmp_path):
    _result, _row, frozen = _runner_case(tmp_path)
    target = frozen.required_root / "parsed" / "NVDA_required_data.csv"
    assert target.is_relative_to(tmp_path)
    assert target.is_file()
    target.write_bytes(target.read_bytes() + b"\n")
    result = run_close_advice(**frozen.run_kwargs(tmp_path))
    assert result["snapshot_authority"] == "invalid"
    assert result["notify_rows"] == 0


def test_qa_closed_public_facade_uses_real_sealed_manual_reference(tmp_path, monkeypatch):
    from src.application import position_advice as service
    from src.application.position_advice_inputs import normalize_manual_positions

    now = datetime.now(timezone.utc)
    manual = {
        "input_id": "qa-manual", "input_as_of_utc": (now - timedelta(minutes=1)).isoformat(),
        "symbol": "NVDA", "option_type": "put", "side": "short",
        "expiration": (now + timedelta(days=20)).date().isoformat(),
        "strike": 100, "contracts_open": 1, "multiplier": 100,
        "currency": "USD", "premium": 2,
    }
    holdings = normalize_manual_positions([manual], account="lx", broker="富途", now_utc=now)
    result, original, frozen = _runner_case(tmp_path, manual_positions=holdings)
    assert result["notify_rows"] == 1
    path = frozen.output_dir / "close_advice.csv"
    before = path.read_bytes()
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "CLOSED"})
    monkeypatch.setattr(service, "_read_holdings", lambda **_: pytest.fail("manual reference read ledger"))
    monkeypatch.setattr(service, "_run_current_assessment", lambda **_: pytest.fail("closed reference fetched quotes"))
    config = {**frozen.config, "accounts": ["lx"]}
    def assess(position):
        return service.evaluate_position_advice(
            {"account": "lx", "positions": [position]}, config=config,
            config_path=tmp_path / "nonexistent-config.us.json",
            base_dir=Path(__file__).resolve().parents[1], runtime_root=tmp_path,
            now_utc=now + timedelta(days=2),
        )
    first = assess(manual)
    second = assess(manual)
    assert first["request_id"] != second["request_id"]
    row = first["rows"][0]
    assert row["assessment_mode"] == "historical_reference"
    assert row["reference_recommendation_state"] == "close"
    assert row["recommendation_state"] == "not_evaluable"
    assert row["current_actionable"] is False
    assert row["position_lot_id"] is None and row["input_id"] == "qa-manual"
    assert float(row["dte"]) == float(original["dte"])
    assert float(row["spot"]) == float(original["spot"])
    assert row["assessment_as_of_utc"] == original["assessment_as_of_utc"]
    assert first["notify_rows"] == 0
    assert path.read_bytes() == before
    assert Path(first["report_path"]).is_relative_to(tmp_path / "output_shared" / "position_advice_requests")
    newer = assess({**manual, "input_as_of_utc": (now + timedelta(hours=1)).isoformat()})
    assert newer["rows"][0]["assessment_mode"] == "unavailable"
    assert newer["notify_rows"] == 0
