"""F1 producer regression through the unchanged finalizer and frozen reader."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket

import pandas as pd
import pytest

from src.application import opend_market_snapshot_fetching as snapshots
from src.application import opend_symbol_fetching as symbols
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.opend_symbol_outputs import finalize_required_data_quote_candidate
from src.application.required_data_snapshot import (
    FrozenRequiredDataUnavailable, resolve_frozen_required_data, seal_required_data_snapshot,
)
from src.application.short_vol_metrics import RealizedVolatilitySnapshot, TermMatchedRVObservation
from src.application.source_receipts import SourceReceiptError
from test_required_data_quote_receipts import NOW, _contract, _fetch_plan, _required_payload
from test_required_data_snapshot import _summary

CODE = "US.NVDA260821P100000"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network is forbidden in producer regressions")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _payload(monkeypatch, tmp_path, case):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)
    monkeypatch.setattr(symbols, "datetime", Clock)
    monkeypatch.setattr(symbols, "_utc_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr("src.application.opend_symbol_chain_fetching._utc_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr("src.application.opend_symbol_chain_fetching.datetime", Clock)
    monkeypatch.setattr(symbols, "rate_limited_opend_call", lambda **kw: kw["call"]())
    monkeypatch.setattr(symbols, "get_trading_date", lambda _market: NOW.date())
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})
    rv_meta = _required_payload()["meta"]["realized_volatility"]
    term = deepcopy(rv_meta["term_matched"]["2026-08-21"])
    term["missing_sessions"] = tuple(term["missing_sessions"])
    rv = RealizedVolatilitySnapshot(
        rv_20=0.2, rv_60=0.2, rv_120=0.2, rv_estimate=0.2, status="ok",
        term_matched={"2026-08-21": TermMatchedRVObservation(**term)},
        qfq_history_evidence=rv_meta["qfq_history"],
        trading_calendar_evidence=rv_meta["trading_calendar"],
    )
    if case == "rv_missing":
        rv = RealizedVolatilitySnapshot(status="missing", reason="synthetic_history_gap")
    monkeypatch.setattr(symbols, "fetch_realized_volatility_snapshot", lambda *_, **__: rv)
    monkeypatch.setattr("src.application.option_chain_fetching.datetime", Clock)
    row = {
        "code": CODE, "last_price": 1.1, "bid_price": 1.0, "ask_price": 1.2,
        "volume": 50, "option_open_interest": 100, "option_implied_volatility": 25,
        "option_delta": -0.2, "option_gamma": 0.01, "option_theta": -0.02,
        "option_vega": 0.1, "option_rho": -0.01, "lot_size": 100,
        "stock_owner": "US.NVDA", "stock_type": "DRVT", "price_spread": 0.01,
        "option_standard_type": "STANDARD", "suspension": False,
    }

    class Gateway:
        def __init__(self):
            self.calls = []

        def get_option_chain(self, **_):
            if case == "chain_failure":
                raise RuntimeError("synthetic chain failure")
            return pd.DataFrame([{
                "code": CODE, "strike_time": "2026-08-21", "strike_price": 100,
                "option_type": "PUT", "lot_size": 100, "stock_owner": "US.NVDA",
                "stock_type": "DRVT", "option_standard_type": "STANDARD", "suspension": False,
            }])

        def get_snapshot(self, codes):
            self.calls.append(list(codes))
            if len(self.calls) == 1 and case not in {"clean", "rv_missing", "identity"}:
                raise RuntimeError("PacketErr.Timeout synthetic main batch timeout")
            if case == "missing":
                return pd.DataFrame(columns=["code"])
            if case == "duplicate":
                return pd.DataFrame([row, row])
            if case == "unexpected":
                return pd.DataFrame([row, {**row, "code": "US.UNRELATED260821P100000"}])
            return pd.DataFrame([row])

    gateway = Gateway()
    underlier = normalize_underlier_observation(
        code="US.NVDA", market="US", now_utc=NOW,
        snapshot_row={"code": "US.NVDA", "last_price": 110, "update_time": NOW.isoformat(),
                      "sec_status": "NORMAL", "suspension": False},
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
    ).to_dict()
    payload = symbols.fetch_symbol_request(symbols.FetchSymbolRequest(
        symbol="NVDA", base_dir=tmp_path, gateway=gateway, host="127.0.0.1", port=11111,
        trading_date=NOW.date().isoformat(), option_types="put",
        explicit_expirations=["2026-08-21"], min_dte=20, max_dte=30,
        side_strike_windows={"put": {"min_strike": 100, "max_strike": 100}},
        spot_override=110, underlier_observation=underlier,
        no_retry=True, chain_cache=False, include_realized_volatility=True,
        snapshot_batch_size=1, snapshot_fallback_max_codes=1, snapshot_fallback_batch_size=1,
    ))
    if case == "identity":
        # A shared payload identity mutation must fail even with complete quote rows.
        payload["underlier_code"] = "US.UNRELATED"
    return payload, gateway


def _finalize_and_seal(tmp_path, payload):
    root = tmp_path / "output_runs" / "run-1" / "required_data"
    plan = _fetch_plan()
    finalized = finalize_required_data_quote_candidate(
        base=tmp_path, producer_root=root, producer_run_id="run-1", symbol="NVDA",
        expected_fetch_contract=_contract(plan),
        fetch_policy={"source": "opend", "host": "127.0.0.1", "port": 11111},
        mode="fresh", payload=payload, now=NOW + timedelta(seconds=1),
    )
    manifest_path = root.parent / "state" / "required_data_snapshot_manifest.json"
    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path, required_data_root=root, run_id="run-1",
        prefetch_summary=_summary("NVDA", fetch_plans={"NVDA": plan}),
        sealed_at=NOW + timedelta(seconds=1),
    )
    frozen = resolve_frozen_required_data(
        manifest_path=manifest_path, expected_run_id="run-1", symbol="NVDA",
        required_data_root=root, now=NOW + timedelta(seconds=1),
    )
    return finalized, manifest, frozen


@pytest.mark.parametrize("case", ["clean", "recovered"])
def test_complete_snapshot_recovery_is_consumable_by_real_frozen_reader(monkeypatch, tmp_path, case):
    payload, gateway = _payload(monkeypatch, tmp_path, case)
    meta = payload["meta"]
    assert meta["snapshot_complete"] is True
    assert meta["errors"] == []
    assert meta["snapshot_errors"] == []
    assert meta["snapshot_fallback_filled"] == int(case == "recovered")
    assert len(gateway.calls) == (2 if case == "recovered" else 1)
    requests = meta["research_prescreen"]["snapshots"]["batch_receipts"]
    assert [item["stage"] for item in requests] == (
        ["market_snapshot", "market_snapshot_fallback"] if case == "recovered" else ["market_snapshot"]
    )
    assert all(item["provider_call_started"] and item["requested_codes"] == [CODE] for item in requests)
    if case == "recovered":
        assert requests[0]["status"] == "failed"
        assert requests[0]["error_code"] == "RuntimeError"
        assert "Timeout" in requests[0]["message"]
        assert requests[1]["status"] == "received"
        attempt_errors = meta["research_prescreen"]["snapshots"]["attempt_errors"]
        assert len(attempt_errors) == 1
        assert attempt_errors[0]["error_code"] == "TRANSIENT"
        assert "Timeout" in attempt_errors[0]["message"]
    finalized, manifest, frozen = _finalize_and_seal(tmp_path, payload)
    assert frozen["source_outcome"] == "success_rows"
    assert manifest["symbols"]["NVDA"]["status"] == "ready"
    assert frozen["snapshot_id"] == finalized["quote_receipt"]["snapshot_id"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "unexpected", "identity", "rv_missing", "chain_failure"])
def test_unresolved_or_invalid_provider_facts_still_fail_finalization(monkeypatch, tmp_path, case):
    payload, gateway = _payload(monkeypatch, tmp_path, case)
    if case == "unexpected":
        # The current completeness boolean excludes unexpected codes. It cannot clear errors.
        assert payload["meta"]["snapshot_complete"] is True
        assert payload["meta"]["errors"]
    if case in {"missing", "duplicate", "unexpected"}:
        assert len(gateway.calls) == 2
        assert [item["stage"] for item in payload["meta"]["research_prescreen"]["snapshots"]["batch_receipts"]] == [
            "market_snapshot", "market_snapshot_fallback",
        ]
    with pytest.raises(SourceReceiptError):
        _finalize_and_seal(tmp_path, payload)
    root = tmp_path / "output_runs" / "run-1" / "required_data"
    with pytest.raises(FrozenRequiredDataUnavailable):
        resolve_frozen_required_data(
            manifest_path=root.parent / "state" / "required_data_snapshot_manifest.json",
            expected_run_id="run-1", symbol="NVDA",
            required_data_root=root, now=NOW,
        )
