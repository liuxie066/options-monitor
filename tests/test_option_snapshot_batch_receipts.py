"""Synthetic batch receipts through real owners; no provider or persistence I/O."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from domain.domain.engine.candidate_engine import (
    CandidateCalculationError,
    calculate_opening_candidate_metrics,
)
from domain.domain.performance.models import OptionInstrumentKey, OptionValuationPosition
from src.application import opend_market_snapshot_fetching as snapshots
from src.application import opend_symbol_fetching as symbols
from src.application import opend_symbol_outputs as outputs
from src.application import opening_quote_evidence as quotes
from src.application.candidate_models import CandidateContractInput
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.performance import evidence_collection as performance
from src.application.short_vol_metrics import RealizedVolatilitySnapshot, TermMatchedRVObservation

T0 = datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
EXPIRY = "2026-11-06"
REQUEST = "snapshot_requested_at_utc"
RECEIVE = "snapshot_received_at_utc"
LIMIT = OpenDEndpointRateLimit(window_sec=30, max_calls=60, max_wait_sec=0)


def _iso(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _retry(_name, call, **_kwargs):
    return call()


def _rate(**kwargs):
    return kwargs["call"]()


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(seconds=0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = T0 + timedelta(seconds=state.seconds)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)
    return state


class Gateway:
    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = iter(responses)
        self.calls = []
        self.frames = []
        self.closed = 0

    def get_snapshot(self, codes):
        self.calls.append(list(codes))
        delay, response = next(self.responses)
        self.clock.seconds += delay
        if isinstance(response, BaseException):
            raise response
        frame = response if isinstance(response, pd.DataFrame) else pd.DataFrame(response)
        self.frames.append((frame, frame.copy(deep=True)))
        return frame

    def close(self):
        self.closed += 1


def _rows(*codes):
    return [{"code": code, "bid_price": 4.0, "ask_price": 4.1,
             "last_price": 4.05, "price_spread": .01,
             "option_contract_size": 100, "sec_status": "NORMAL", "suspension": False,
             "option_implied_volatility": 45., "option_open_interest": 800, "volume": 70,
             "option_delta": -.25} for code in codes]


def _fetch(tmp_path, gateway, codes=("A", "B"), **kwargs):
    return snapshots.fetch_option_snapshots(
        option_codes=list(codes), gateway=gateway, base_dir=tmp_path,
        snapshot_limit=LIMIT, snapshot_batch_size=kwargs.pop("batch", 1),
        snapshot_fallback_max_codes=kwargs.pop("fallback", 0),
        snapshot_fallback_batch_size=1, retry_call=kwargs.pop("retry_call", _retry),
        rate_limited_call=_rate, **kwargs,
    )


@pytest.mark.parametrize("batch", [1, 2, 3])
def test_batch_receipts_are_shared_only_by_returned_batch(monkeypatch, tmp_path, clock, batch):
    codes = ["A", "B", "C"]
    responses = []
    for start in range(0, len(codes), batch):
        rows = _rows(*codes[start:start + batch])
        for row in rows:
            row.update({REQUEST: "provider-request", RECEIVE: "provider-receive"})
        responses.append((2, rows))
    gateway = Gateway(clock, responses)
    real_keep = snapshots.keep_snapshot_record_columns

    def delayed_conversion(*args):
        clock.seconds += 10
        return real_keep(*args)

    monkeypatch.setattr(snapshots, "keep_snapshot_record_columns", delayed_conversion)
    result = _fetch(tmp_path, gateway, codes, batch=batch)
    assert result.complete and result.opend_call_count == len(responses)
    for i, code in enumerate(codes):
        group = i // batch
        assert result.snap_map[code][REQUEST] == _iso(12 * group)
        assert result.snap_map[code][RECEIVE] == _iso(12 * group + 2)
    assert result.requested_at_utc == _iso(0)
    assert result.received_at_utc == _iso(12 * (len(responses) - 1) + 2)
    for frame, original in gateway.frames:
        pd.testing.assert_frame_equal(frame, original)
        assert set(frame[RECEIVE]) == {"provider-receive"}


def test_actual_batch_receipts_distinguish_retry_from_successful_quote(tmp_path, clock):
    gateway = Gateway(clock, [(2, TimeoutError("synthetic")), (3, _rows("A"))])

    def retry(_name, call, **_kwargs):
        clock.seconds += 4
        with pytest.raises(TimeoutError):
            call()
        clock.seconds += 5
        return call()

    result = _fetch(tmp_path, gateway, ["A"], retry_call=retry)
    assert result.complete and result.opend_call_count == 2
    assert result.snap_map["A"][REQUEST] == _iso(11)
    assert result.snap_map["A"][RECEIVE] == _iso(14)
    assert result.requested_at_utc == _iso(4)
    assert [(r["requested_at_utc"], r["received_at_utc"], r["status"]) for r in result.batch_receipts] == [
        (_iso(4), _iso(6), "failed"), (_iso(11), _iso(14), "received"),
    ]
    assert result.batch_receipts[0]["error_code"] == "TimeoutError"


@pytest.mark.parametrize("fallback_count", [1, 2])
def test_partial_then_fallback_preserves_first_receipt(monkeypatch, tmp_path, clock, fallback_count):
    codes = ["A", "B", "C"][:fallback_count + 1]
    gateway = Gateway(clock, [(1, _rows("A")), *[(2, _rows(c)) for c in codes[1:]]])
    real_keep = snapshots.keep_snapshot_record_columns

    def conversion(*args):
        clock.seconds += 10
        return real_keep(*args)

    monkeypatch.setattr(snapshots, "keep_snapshot_record_columns", conversion)
    result = _fetch(tmp_path, gateway, codes, batch=3, fallback=3)
    assert result.complete and result.fallback_filled == fallback_count
    assert result.fallback_failed == 0
    assert gateway.calls == [codes, *[[c] for c in codes[1:]]]
    assert result.snap_map["A"][RECEIVE] == _iso(1)
    for i, code in enumerate(codes[1:]):
        assert result.snap_map[code][REQUEST] == _iso(11 + 12 * i)
        assert result.snap_map[code][RECEIVE] == _iso(13 + 12 * i)
    assert result.received_at_utc == _iso(13 + 12 * (fallback_count - 1))
    assert [r["stage"] for r in result.batch_receipts] == ["market_snapshot", *["market_snapshot_fallback"] * fallback_count]


@pytest.mark.parametrize("failure", [[], [{"last_price": 3}], TimeoutError("synthetic")])
def test_failed_fallback_changes_only_summary(tmp_path, clock, failure):
    gateway = Gateway(clock, [(1, _rows("A")), (20, failure)])
    result = _fetch(tmp_path, gateway, batch=2, fallback=2)
    assert not result.complete and result.missing_codes == {"B"}
    assert result.fallback_failed == 1 and result.fallback_filled == 0
    assert set(result.snap_map) == {"A"}
    assert result.snap_map["A"][REQUEST] == _iso(0)
    assert result.snap_map["A"][RECEIVE] == _iso(1)
    assert result.received_at_utc == _iso(21)


def test_fallback_budget_and_unexpected_identity_do_not_create_receipts(tmp_path, clock):
    gateway = Gateway(clock, [(1, _rows("A", "UNEXPECTED")), (2, _rows("B"))])
    result = _fetch(tmp_path, gateway, ["A", "B", "C"], batch=3, fallback=1)
    assert set(result.snap_map) == {"A", "B"}
    assert result.missing_codes == {"C"} and result.unexpected_codes == {"UNEXPECTED"}
    assert not result.complete and result.fallback_filled == result.fallback_failed == 1
    assert result.snap_map["A"][RECEIVE] == _iso(1)
    assert result.snap_map["B"][RECEIVE] == _iso(3)
    assert any(e["error_code"] == "FALLBACK_BUDGET_EXCEEDED" for e in result.errors)


def test_cross_batch_requested_code_membership_remains_global(tmp_path, clock):
    gateway = Gateway(clock, [(1, _rows("B")), (2, _rows("A"))])
    result = _fetch(tmp_path, gateway)
    assert result.complete
    assert result.snap_map["B"][RECEIVE] == _iso(1)
    assert result.snap_map["A"][RECEIVE] == _iso(3)


def test_empty_request_has_no_provider_or_receipt(tmp_path, clock):
    gateway = Gateway(clock, [])
    result = _fetch(tmp_path, gateway, [])
    assert result.complete and not result.snap_map and not gateway.calls
    assert result.opend_call_count == 0
    assert result.requested_at_utc is result.received_at_utc is None


@pytest.mark.parametrize("exception", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("fallback", [False, True])
def test_cancellation_propagates_without_returning_partial_success(tmp_path, clock, exception, fallback):
    responses = [(1, [])] if fallback else []
    responses.append((2, exception("synthetic")))
    gateway = Gateway(clock, responses)
    with pytest.raises(exception):
        _fetch(tmp_path, gateway, ["A"], fallback=1 if fallback else 0)
    assert len(gateway.calls) == 1 + int(fallback)


def test_underlier_shared_merge_does_not_gain_option_receipts(tmp_path):
    class UnderlierGateway:
        def get_snapshot(self, codes):
            return pd.DataFrame([{"code": codes[0], "last_price": 160,
                                  "update_time": T0.isoformat(), "sec_status": "NORMAL",
                                  "suspension": False}])

        def get_market_state(self, codes):
            return pd.DataFrame([{"code": codes[0], "market_state": "MORNING"}])

    observation = snapshots.get_underlier_observation_opend(
        UnderlierGateway(), "US.NVDA", market="US", base_dir=tmp_path,
        rate_limited_call=_rate, now_utc=T0,
    )
    assert observation.status == "ready"
    assert REQUEST not in observation.to_dict() and RECEIVE not in observation.to_dict()


def _symbol_payload(monkeypatch, tmp_path, clock, mode, *, failure=None, missing=()):
    codes = [f"US.NVDA261106{mode[0].upper()}{strike}000" for strike in (150, 145)]
    response = failure if failure is not None else _rows(codes[1])
    gateway = Gateway(clock, [(1, _rows(codes[0])), (2, response)])
    chain = pd.DataFrame([
        {"code": code, "strike_time": EXPIRY, "strike_price": strike,
         "option_type": mode.upper(), "lot_size": 100, "stock_type": "DRVT",
         "stock_owner": "US.NVDA", "option_standard_type": "STANDARD", "suspension": False}
        for code, strike in zip(codes, (150, 145), strict=True)
    ])
    rv = RealizedVolatilitySnapshot(status="ok", term_matched={EXPIRY: TermMatchedRVObservation(
        schema_version="term_matched_rv.v1", expiration=EXPIRY, status="ok", reason=None,
        term_matched_rv=.2, remaining_sessions=15, lookback_sessions=30,
        input_start="2026-09-01", input_end="2026-10-15", input_close_session_count=31,
        input_return_count=30, input_hash="9" * 64)})
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})
    monkeypatch.setattr(symbols, "fetch_symbol_option_chain", lambda **_: SimpleNamespace(
        frame=chain, rows=[], fetch_meta={"status": "ok", "source_outcome": "success_rows",
        "source_observed_at": _iso(0), "completed_at_utc": _iso(0),
        "expiration_statuses": {EXPIRY: "ok"}}))
    monkeypatch.setattr(symbols, "fetch_realized_volatility_snapshot", lambda *a, **k: rv)
    monkeypatch.setattr(symbols, "retry_futu_gateway_call", _retry)
    monkeypatch.setattr(symbols, "rate_limited_opend_call", _rate)
    monkeypatch.setattr(symbols, "_utc_now_iso", lambda: _iso(clock.seconds))
    real_now = quotes._utc_now
    monkeypatch.setattr(quotes, "_utc_now", lambda value: real_now(
        value or T0 + timedelta(seconds=clock.seconds)))
    owner = []

    def fetch(**kwargs):
        result = snapshots.fetch_option_snapshots(**kwargs)
        for row in result.snap_map.values():
            for field in missing:
                row.pop(field, None)
        owner.append(result)
        return result

    monkeypatch.setattr(symbols, "fetch_option_snapshots", fetch)
    # A supplied synthetic observation avoids unrelated underlier acquisition.
    underlier = {"schema_version": "opening_underlier_observation.v1", "code": "US.NVDA",
                 "market": "US", "last_price": 160, "update_time": _iso(0),
                 "observed_at_utc": _iso(0), "age_seconds": 0, "market_state": "MORNING",
                 "sec_status": "NORMAL", "suspension": False, "status": "ready", "reason_code": None}
    payload = symbols.fetch_symbol_request(symbols.FetchSymbolRequest(
        symbol="NVDA", base_dir=tmp_path, gateway=gateway, option_types=mode,
        trading_date=T0.date().isoformat(), explicit_expirations=[EXPIRY],
        spot_override=160, underlier_observation=underlier,
        no_retry=True, chain_cache=False, include_realized_volatility=True,
        snapshot_batch_size=2 if failure is not None else 1,
        snapshot_fallback_max_codes=1 if failure is not None else 0,
        snapshot_fallback_batch_size=1,
    ))
    assert gateway.closed == 0
    return payload, owner[0]


def _metrics(row, mode, decision):
    csv_row = outputs._csv_roundtrip_frame([row]).iloc[0]
    payload = CandidateContractInput.from_row(csv_row, mode=mode).to_gate_payload()
    return calculate_opening_candidate_metrics(payload, mode=mode,
        avg_cost=140 if mode == "call" else None, now_utc=decision)


@pytest.mark.parametrize("mode", ["put", "call"])
def test_real_symbol_csv_dto_metrics_preserve_batch_age(monkeypatch, tmp_path, clock, mode):
    payload, owner = _symbol_payload(monkeypatch, tmp_path, clock, mode)
    assert payload["meta"]["status"] == "ok" and owner.complete
    rows = payload["rows"]
    assert [r[REQUEST] for r in rows] == [_iso(0), _iso(1)]
    assert [r[RECEIVE] for r in rows] == [_iso(1), _iso(3)]
    assert [r["snapshot_age_seconds"] for r in rows] == [2, 0]
    assert payload["meta"][REQUEST] == _iso(0)
    assert payload["meta"][RECEIVE] == _iso(3)
    for row in rows:
        value = _metrics(row, mode, T0 + timedelta(seconds=3))
        equivalent = {**row, REQUEST: _iso(1), RECEIVE: _iso(3), "snapshot_age_seconds": 0}
        assert value == _metrics(equivalent, mode, T0 + timedelta(seconds=3))
    # Fresh spot is an isolated option-age control, not the ordinary ordered flow.
    fresh_spot = {**rows[0], "spot_observed_at_utc": _iso(290)}
    _metrics(fresh_spot, mode, T0 + timedelta(seconds=301))  # option age exactly 300
    with pytest.raises(CandidateCalculationError) as exc:
        _metrics(fresh_spot, mode, T0 + timedelta(seconds=302))
    assert exc.value.reason == "evidence_unavailable"
    with pytest.raises(CandidateCalculationError) as exc:
        _metrics(rows[1], mode, T0 + timedelta(seconds=302))
    assert "spot" in str(exc.value).lower() or "underlier" in str(exc.value).lower()


@pytest.mark.parametrize("missing", [(REQUEST,), (RECEIVE,), (REQUEST, RECEIVE)])
def test_symbol_missing_row_fields_never_inherit_summary(monkeypatch, tmp_path, clock, missing):
    payload, owner = _symbol_payload(monkeypatch, tmp_path, clock, "put", missing=missing)
    assert owner.requested_at_utc and owner.received_at_utc
    row = payload["rows"][0]
    for field in missing:
        assert row[field] is None
    if RECEIVE in missing:
        assert row["opening_contract_status"] == "data_unavailable"
    else:
        assert row["opening_contract_status"] == "ready"
        _metrics(row, "put", T0 + timedelta(seconds=3))


@pytest.mark.parametrize("failure", [[], TimeoutError("synthetic")])
def test_incomplete_symbol_payload_is_rejected_before_publication(monkeypatch, tmp_path, clock, failure):
    payload, owner = _symbol_payload(monkeypatch, tmp_path, clock, "put", failure=failure)
    assert not owner.complete and payload["meta"]["status"] == "error"
    assert payload["rows"][0][RECEIVE] == _iso(1)
    with pytest.raises(outputs.SourceReceiptError, match="^provider_incomplete:"):
        outputs.validate_required_data_payload_candidate(payload=payload, expected_fetch_contract={})


def _position(code, strike):
    return OptionValuationPosition(
        lot_id="synthetic-" + code, account="synthetic", broker="synthetic",
        instrument=OptionInstrumentKey(symbol="NVDA", option_type="put", strike=Decimal(strike),
            expiration_ymd=EXPIRY, currency="USD", multiplier=Decimal(100)),
        position_side="short", contracts_open=1, open_price=Decimal(4),
        open_fee_remaining=Decimal(0), open_fee_quality="estimated", opened_at_ms=1,
        market_code=code,
    )


@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("missing", [(), (REQUEST,), (RECEIVE,), (REQUEST, RECEIVE)])
def test_real_performance_adapter_and_marks_use_matching_row(monkeypatch, tmp_path, clock, batch, missing):
    positions = [_position("A", "150"), _position("B", "145")]
    responses = [(1, _rows("A")), (2, _rows("B"))] if batch == 1 else [(3, _rows("A", "B"))]
    gateway = Gateway(clock, responses)
    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_quote_gateway", lambda **_: gateway)
    real_fetch = snapshots.fetch_option_snapshots

    def fetch(**kwargs):
        result = real_fetch(**kwargs, retry_call=_retry, rate_limited_call=_rate)
        for row in result.snap_map.values():
            for field in missing:
                row.pop(field, None)
        return result

    monkeypatch.setattr(snapshots, "fetch_option_snapshots", fetch)
    rows = performance._default_option_snapshot_rows(positions,
        cfg={"runtime": {"opend_batch": {"market_snapshot": batch}}}, base_dir=tmp_path)
    assert gateway.closed == 1
    for i, (position, row) in enumerate(zip(positions, rows, strict=True)):
        request = _iso(i) if batch == 1 else _iso(0)
        receive = _iso(1 if i == 0 else 3) if batch == 1 else _iso(3)
        assert row["_" + REQUEST] == (None if REQUEST in missing else request)
        assert row["_" + RECEIVE] == (None if RECEIVE in missing else receive)
        fallback_ms = int((T0 + timedelta(seconds=20)).timestamp() * 1000)
        mark = performance.build_option_valuation_mark_fact(position, rows, fallback_ms=fallback_ms)
        if len(missing) == 2:
            assert mark.effective_at_ms == mark.observed_at_ms == fallback_ms
            assert mark.quality["timestamp_fallback"] is True
        else:
            expected_request = receive if REQUEST in missing else request
            expected_receive = request if RECEIVE in missing else receive
            assert mark.effective_at_ms == int(datetime.fromisoformat(expected_request).timestamp() * 1000)
            assert mark.observed_at_ms == int(datetime.fromisoformat(expected_receive).timestamp() * 1000)
            assert not mark.quality.get("timestamp_fallback")
        assert mark.price == Decimal("4.05") and mark.mark_kind == "midpoint"
        assert mark.source_id == f"{position.market_code}:{mark.observed_at_ms}"
