from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application import short_vol_metrics as rv


DAY = date(2026, 12, 7)
NEAR = "2026-12-18"
FAR = "2027-02-19"


class Gateway:
    """Provider-shaped synthetic evidence; never constructs an OpenD client."""

    def __init__(self, *, code="US.NVDA"):
        self.code = code
        self.calendar_calls = []
        self.history_calls = []
        self.calendar_changes = {}
        self.calendar_failure = None
        self.history_failure = None
        self.page_size = 300
        self.bad_page = None
        self.bad_row = None
        self.repeat_page = False
        self.prices = {}
        price = 100.0
        for offset in range(-300, 0):
            day = DAY + timedelta(days=offset)
            if day.weekday() < 5:
                price *= 1.01 if offset % 2 else 0.994
                self.prices[day] = price

    def get_trading_days(self, **kwargs):
        self.calendar_calls.append(kwargs)
        if self.calendar_failure is not None:
            raise self.calendar_failure
        start = date.fromisoformat(kwargs["start"])
        end = min(date.fromisoformat(kwargs["end"]), date(DAY.year, 12, 31))
        rows = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                rows.append({"time": cursor.isoformat(), "trade_date_type": "WHOLE"})
            cursor += timedelta(days=1)
        for row in rows:
            if row["time"] in self.calendar_changes:
                row["trade_date_type"] = self.calendar_changes[row["time"]]
        return rows

    def request_history_kline(self, **kwargs):
        self.history_calls.append(kwargs)
        page = int(kwargs["page_req_key"] or 0)
        if self.history_failure is not None and page == (self.bad_page or 0):
            raise self.history_failure
        start = date.fromisoformat(kwargs["start"])
        end = date.fromisoformat(kwargs["end"])
        rows = [
            {"code": self.code, "time_key": day.isoformat(), "close": close}
            for day, close in self.prices.items() if start <= day <= end
        ]
        chunk = rows[page * self.page_size:(page + 1) * self.page_size]
        if self.bad_row is not None and page == (self.bad_page or 0):
            chunk = [*chunk, self.bad_row]
        next_key = str(page + 1) if (page + 1) * self.page_size < len(rows) else None
        if self.repeat_page and page:
            next_key = kwargs["page_req_key"]
        return {"data": chunk, "page_req_key": next_key}


def fetch(gateway, base_dir, *, expirations=(NEAR,), market="US", code="US.NVDA"):
    return rv.fetch_realized_volatility_snapshot(
        gateway, underlier_code=code, market=market, trading_day=DAY,
        expirations=expirations, base_dir=base_dir,
    )


def cache_path(base_dir):
    return rv.qfq_history_cache_path(base_dir, market="US", underlier_code="US.NVDA")


def test_cross_year_calendar_is_unavailable_only_for_far_expiry(tmp_path):
    control = fetch(Gateway(), tmp_path / "control")
    gateway = Gateway()
    snapshot = fetch(gateway, tmp_path / "mixed", expirations=(NEAR, FAR))
    assert snapshot.status == "partial"
    assert snapshot.term_matched[NEAR] == control.term_matched[NEAR]
    far = snapshot.term_matched[FAR]
    assert far.status == "data_unavailable"
    assert far.reason == "calendar_coverage_incomplete"
    assert far.term_matched_rv is None and far.input_hash is None
    assert far.remaining_sessions is None and far.lookback_sessions is None
    assert gateway.calendar_calls[0]["end"] == "2026-12-31"
    assert snapshot.trading_calendar_evidence["requested_end"] == FAR
    assert snapshot.trading_calendar_evidence["coverage_end"] == "2026-12-31"
    assert snapshot.trading_calendar_evidence["status"] == "ok"


def test_all_far_expiries_do_not_fetch_or_publish_history(tmp_path):
    gateway = Gateway()
    snapshot = fetch(gateway, tmp_path, expirations=(FAR, "2027-03-19"))
    assert snapshot.status == "error"
    assert snapshot.reason == "calendar_coverage_incomplete"
    assert not gateway.history_calls
    assert not cache_path(tmp_path).exists()
    assert all(item.status == "data_unavailable" for item in snapshot.term_matched.values())


@pytest.mark.parametrize("expiration", ["2026-12-19", "2026-12-25", "2026-12-31"])
def test_supported_holiday_or_weekend_does_not_require_session_on_expiry(tmp_path, expiration):
    gateway = Gateway()
    gateway.calendar_changes[expiration] = "NON_TRADING"
    snapshot = fetch(gateway, tmp_path, expirations=(expiration,))
    assert snapshot.status == "ok"
    assert snapshot.term_matched[expiration].status == "ok"
    assert snapshot.trading_calendar_evidence["coverage_end"] == expiration


@pytest.mark.parametrize("session", ["2026-12-01", "2026-12-08"])
@pytest.mark.parametrize("kind", ["N/A", "UNSUPPORTED", 17, 0, False])
def test_unknown_calendar_type_prevents_history_acquisition(tmp_path, session, kind):
    gateway = Gateway()
    gateway.calendar_changes[session] = kind
    snapshot = fetch(gateway, tmp_path)
    assert snapshot.status == "error"
    assert snapshot.reason == "trading_calendar_error:ValueError"
    assert snapshot.term_matched[NEAR].term_matched_rv is None
    assert not gateway.history_calls
    assert not cache_path(tmp_path).exists()


def test_compatible_calendar_types_and_half_day_deduplication():
    rows = [
        {"time": "2026-12-07", "trade_date_type": "WHOLE"},
        {"time": "2026-12-08", "trade_date_type": "MORNING"},
        {"time": "2026-12-08", "trade_date_type": "AFTERNOON"},
        {"time": "2026-12-09", "trade_date_type": "TRADING"},
        {"time": "2026-12-10", "trade_date_type": ""},
        {"time": "2026-12-11"},
        {"time": "2026-12-12", "trade_date_type": "NON_TRADING"},
    ]
    assert rv._trading_calendar_dates(rows) == [date(2026, 12, day) for day in range(7, 12)]


def test_no_future_sessions_remains_unavailable(tmp_path):
    gateway = Gateway()
    gateway.calendar_changes[DAY.isoformat()] = "NON_TRADING"
    snapshot = fetch(gateway, tmp_path, expirations=(DAY.isoformat(),))
    observation = snapshot.term_matched[DAY.isoformat()]
    assert observation.status == "data_unavailable"
    assert observation.reason == "future_trading_sessions_missing"


def test_calendar_failure_preserves_existing_cache(tmp_path):
    fetch(Gateway(), tmp_path)
    path = cache_path(tmp_path)
    before = path.read_bytes()
    gateway = Gateway()
    gateway.calendar_failure = TimeoutError("synthetic calendar timeout")
    snapshot = fetch(gateway, tmp_path)
    assert snapshot.reason == "trading_calendar_error:TimeoutError"
    assert not gateway.history_calls
    assert path.read_bytes() == before


@pytest.mark.parametrize("market,code", [("US", "US.NVDA"), ("HK", "HK.00700")])
def test_matching_identity_retains_supported_market_results(tmp_path, market, code):
    snapshot = fetch(Gateway(code=code), tmp_path, market=market, code=code)
    assert snapshot.status == "ok"
    assert snapshot.qfq_history_evidence["underlier_code"] == code
    assert snapshot.qfq_history_evidence["cache_identity"] == f"{market}:{code}:QFQ"
    assert snapshot.term_matched[NEAR].term_matched_rv > 0


@pytest.mark.parametrize("code", ["US.MSFT", "US.nvda", "", " ", None, 123])
def test_wrong_or_missing_history_identity_is_not_published(tmp_path, code):
    gateway = Gateway(code=code)
    snapshot = fetch(gateway, tmp_path)
    assert snapshot.status == "error"
    assert snapshot.reason == "qfq_history_error:ValueError"
    assert snapshot.qfq_history_evidence["status"] == "error"
    assert snapshot.term_matched[NEAR].term_matched_rv is None
    assert not cache_path(tmp_path).exists()


@pytest.mark.parametrize("bad_row", [
    {"time_key": "2026-12-01", "close": 100},
    {"code": "US.MSFT", "time_key": "2026-12-01", "close": 100},
    "not a row", 12,
])
@pytest.mark.parametrize("bad_page", [0, 1])
def test_every_page_row_is_validated_before_normalization(tmp_path, bad_row, bad_page):
    gateway = Gateway()
    gateway.page_size = 100
    gateway.bad_page = bad_page
    gateway.bad_row = bad_row
    snapshot = fetch(gateway, tmp_path)
    assert snapshot.status == "error"
    assert snapshot.reason == "qfq_history_error:ValueError"
    assert len(gateway.history_calls) == bad_page + 1
    assert not cache_path(tmp_path).exists()


def test_valid_dataframe_and_multiple_pages_preserve_values(tmp_path):
    control = fetch(Gateway(), tmp_path / "control")
    gateway = Gateway()
    gateway.page_size = 100
    request = gateway.request_history_kline

    def dataframe_page(**kwargs):
        result = request(**kwargs)
        return {**result, "data": pd.DataFrame(result["data"])}

    gateway.request_history_kline = dataframe_page
    snapshot = fetch(gateway, tmp_path / "pages")
    assert len(gateway.history_calls) == 2
    assert snapshot.term_matched == control.term_matched


@pytest.mark.parametrize("failure", [TimeoutError("synthetic timeout"), asyncio.CancelledError()])
def test_failed_refresh_preserves_cache_and_retry_uses_fresh_evidence(tmp_path, failure):
    first = fetch(Gateway(), tmp_path)
    path = cache_path(tmp_path)
    before = path.read_bytes()
    gateway = Gateway()
    gateway.history_failure = failure
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            fetch(gateway, tmp_path)
    else:
        failed = fetch(gateway, tmp_path)
        assert failed.status == "error"
        assert failed.term_matched[NEAR].term_matched_rv is None
    assert path.read_bytes() == before
    retry = fetch(Gateway(), tmp_path)
    assert retry.term_matched == first.term_matched
    assert retry.qfq_history_evidence["cache_status"] == "refreshed"


@pytest.mark.parametrize("failure_kind", ["identity", "timeout", "pagination"])
def test_later_page_failure_preserves_prior_cache(tmp_path, failure_kind):
    fetch(Gateway(), tmp_path)
    path = cache_path(tmp_path)
    before = path.read_bytes()
    gateway = Gateway()
    gateway.page_size = 2
    gateway.bad_page = 1
    if failure_kind == "identity":
        # Out-of-range rows still require identity validation before normalization.
        gateway.bad_row = {"code": "US.MSFT", "time_key": "1900-01-01", "close": 100}
    elif failure_kind == "timeout":
        gateway.history_failure = TimeoutError("second page")
    else:
        gateway.repeat_page = True
    failed = fetch(gateway, tmp_path)
    assert failed.status == "error"
    assert failed.qfq_history_evidence["status"] == "error"
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", [False, True])
def test_unverified_v1_cache_requires_full_validated_history(tmp_path, failure):
    control = fetch(Gateway(), tmp_path)
    path = cache_path(tmp_path)
    old = json.loads(path.read_text())
    old["schema_version"] = "opend_qfq_history_cache.v1"
    old["rows"][0]["close"] *= 2
    old["rows_hash"] = rv._hash_values(old["rows"])
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    gateway = Gateway(code="US.MSFT" if failure else "US.NVDA")
    snapshot = fetch(gateway, tmp_path)
    assert gateway.history_calls[0]["start"] == gateway.calendar_calls[0]["start"]
    if failure:
        assert snapshot.status == "error"
        assert path.read_bytes() == before
    else:
        assert snapshot.term_matched == control.term_matched
        assert snapshot.qfq_history_evidence["cache_status"] == "created"
        cache = json.loads(path.read_text())
        assert cache["schema_version"] == "opend_qfq_history_cache.v2"
        assert cache["rows"][0] != old["rows"][0]


def _public_fetch(monkeypatch, tmp_path, gateway, expirations):
    from src.application import opend_symbol_fetching as symbols
    from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult

    stamp = "2026-12-07T15:00:00+00:00"
    codes = [f"US.NVDA.{expiry}.P100" for expiry in expirations]
    chain = pd.DataFrame([
        {"code": code, "strike_time": expiry, "strike_price": 100,
         "option_type": "PUT", "lot_size": 100, "stock_type": "DRVT",
         "stock_owner": "US.NVDA", "option_standard_type": "STANDARD", "suspension": False}
        for code, expiry in zip(codes, expirations, strict=True)
    ])
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})
    monkeypatch.setattr(symbols, "fetch_symbol_option_chain", lambda **_: SimpleNamespace(
        frame=chain, fetch_meta={"status": "ok", "source_outcome": "success_rows",
        "expiration_statuses": {expiry: "fetched" for expiry in expirations}},
    ))
    monkeypatch.setattr(symbols, "fetch_option_snapshots", lambda **_: MarketSnapshotFetchResult(
        snap_map={code: {"code": code, "last_price": 1.0, "bid_price": 1,
                       "ask_price": 1.05, "price_spread": .01,
                       "option_implied_volatility": .30, "option_contract_multiplier": 100,
                       "stock_owner": "US.NVDA", "stock_type": "DRVT", "sec_status": "NORMAL",
                       "suspension": False, "update_time": stamp,
                       "snapshot_requested_at_utc": stamp, "snapshot_received_at_utc": stamp}
                  for code in codes},
        errors=[], requested_codes=frozenset(codes), returned_codes=frozenset(codes),
        missing_codes=frozenset(), unexpected_codes=frozenset(), complete=True,
    ))
    monkeypatch.setattr(symbols, "normalize_option_observation", _fixed_option_clock(stamp))
    return symbols.fetch_symbol_request(symbols.FetchSymbolRequest(
        symbol="NVDA", base_dir=tmp_path, gateway=gateway, spot_override=100,
        trading_date=DAY.isoformat(), explicit_expirations=list(expirations),
        option_types="put", include_realized_volatility=True, no_retry=True,
        underlier_observation={"schema_version": "opening_underlier_observation.v1",
            "code": "US.NVDA", "market": "US", "last_price": 100,
            "update_time": stamp, "observed_at_utc": stamp, "age_seconds": 0,
            "market_state": "MORNING", "sec_status": "NORMAL", "suspension": False,
            "status": "ready", "reason_code": None},
    ))


def _fixed_option_clock(stamp):
    from datetime import datetime
    from src.application.opening_quote_evidence import normalize_option_observation

    def normalize(**kwargs):
        return normalize_option_observation(**kwargs, now_utc=datetime.fromisoformat(stamp))

    return normalize


def test_public_symbol_entry_keeps_near_rv_and_rejects_far_binding(monkeypatch, tmp_path):
    from datetime import datetime
    from domain.domain.engine.candidate_engine import (
        CandidateCalculationError, calculate_opening_candidate_metrics,
    )
    from src.application.opend_symbol_fetching import _required_realized_volatility_error

    payload = _public_fetch(monkeypatch, tmp_path, Gateway(), (NEAR, FAR))
    assert payload["meta"]["status"] == "ok"
    rows = {row["expiration"]: row for row in payload["rows"]}
    assert rows[NEAR]["term_matched_rv_status"] == "ok"
    assert rows[NEAR]["term_matched_rv"] > 0
    assert rows[FAR]["term_matched_rv_status"] == "data_unavailable"
    assert rows[FAR]["term_matched_rv"] is None
    assert rows[FAR]["term_matched_rv_reason"] == "calendar_coverage_incomplete"
    decision = datetime.fromisoformat("2026-12-07T15:00:00+00:00")
    near_metrics = calculate_opening_candidate_metrics(rows[NEAR], mode="put", now_utc=decision)
    assert near_metrics["term_matched_rv"] == rows[NEAR]["term_matched_rv"]
    with pytest.raises(CandidateCalculationError) as exc:
        calculate_opening_candidate_metrics(rows[FAR], mode="put", now_utc=decision)
    assert exc.value.reason == "term_matched_rv_unavailable"
    # The existing partial-fetch contract preserves valid near rows.
    snapshot = fetch(Gateway(), None, expirations=(NEAR, FAR))
    assert _required_realized_volatility_error(snapshot, expirations=[NEAR, FAR]) is None


def test_mixed_expiry_rv_survives_created_refreshed_request_merge(monkeypatch, tmp_path):
    from datetime import datetime
    from domain.domain.engine.candidate_engine import (
        CandidateCalculationError, calculate_opening_candidate_metrics,
    )
    from src.application.required_data_fetching import (
        bind_merged_payload_evidence, merge_required_data_payloads,
    )

    payloads = [
        _public_fetch(monkeypatch, tmp_path, Gateway(), (NEAR, FAR))
        for _ in range(2)
    ]
    rv_items = [payload["meta"]["realized_volatility"] for payload in payloads]
    assert [item["qfq_history"]["cache_status"] for item in rv_items] == ["created", "refreshed"]
    assert rv_items[0] != rv_items[1]
    merged = merge_required_data_payloads(symbol="NVDA", payloads=payloads)
    bind_merged_payload_evidence(merged_payload=merged, payloads=payloads)

    assert merged["meta"]["status"] == "ok"
    assert merged["meta"]["realized_volatility"]["status"] == "partial"
    calendar = merged["meta"]["realized_volatility"]["trading_calendar"]
    assert calendar["status"] == "ok" and calendar["request_count"] == 2
    assert all(item["requested_end"] == FAR and item["coverage_end"] == "2026-12-31"
               for item in calendar["requests"])
    rows = {row["expiration"]: row for row in merged["rows"]}
    assert rows[NEAR]["term_matched_rv_status"] == "ok"
    assert rows[FAR]["term_matched_rv_status"] == "data_unavailable"
    assert rows[FAR]["term_matched_rv_reason"] == "calendar_coverage_incomplete"
    assert rows[FAR]["term_matched_rv"] is None
    decision = datetime.fromisoformat("2026-12-07T15:00:00+00:00")
    near_metrics = calculate_opening_candidate_metrics(rows[NEAR], mode="put", now_utc=decision)
    assert near_metrics["term_matched_rv"] == rv_items[0]["term_matched"][NEAR]["term_matched_rv"]
    with pytest.raises(CandidateCalculationError) as exc:
        calculate_opening_candidate_metrics(rows[FAR], mode="put", now_utc=decision)
    assert exc.value.reason == "term_matched_rv_unavailable"


@pytest.mark.parametrize("failure_kind", ["all_far", "calendar", "identity"])
def test_public_symbol_entry_emits_typed_unavailable(monkeypatch, tmp_path, failure_kind):
    gateway = Gateway()
    expirations = (NEAR,)
    if failure_kind == "all_far":
        expirations = (FAR,)
    elif failure_kind == "calendar":
        gateway.calendar_changes["2026-12-08"] = "N/A"
    else:
        gateway.code = "US.MSFT"
    payload = _public_fetch(monkeypatch, tmp_path, gateway, expirations)
    assert payload["meta"]["status"] == "error"
    assert payload["meta"]["error_code"] == "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
    assert all(row["term_matched_rv"] is None for row in payload["rows"])
    assert payload["meta"]["realized_volatility"]["status"] == "error"
    assert not cache_path(tmp_path).exists()
