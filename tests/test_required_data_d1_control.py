"""The same run control bounds planning, SDK calls, waits and queued symbols."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
import socket
import subprocess
import time

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("D1 control test attempted a provider connection")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_discovery_control_reaches_gateway_retry_limiter_and_sdk(tmp_path, monkeypatch):
    from src.application import opend_symbol_chain_fetching as discovery
    from src.infrastructure import futu_gateway
    deadline = time.monotonic() + 30
    cancelled = lambda: False
    events = []
    class Gateway:
        def get_option_expiration_dates(self, code, **kwargs):
            assert kwargs == {"deadline": deadline, "cancelled": cancelled}
            assert futu_gateway._FETCH_CONTROL.get() == (deadline, cancelled)
            events.append("sdk")
            return pd.DataFrame({"strike_time": ["2026-08-28"]})
        def close(self):
            events.append("close")
    def build(**kwargs):
        assert (kwargs["deadline"], kwargs["cancelled"]) == (deadline, cancelled)
        events.append("gateway")
        return Gateway()
    def limited(**kwargs):
        assert (kwargs["deadline"], kwargs["cancelled"]) == (deadline, cancelled)
        events.append("limiter")
        return kwargs["call"]()
    monkeypatch.setattr(discovery, "build_ready_futu_quote_gateway", build)
    monkeypatch.setattr(discovery, "rate_limited_opend_call", limited)
    result = discovery.discover_option_expirations(
        "AAPL", base_dir=tmp_path, trading_date="2026-08-04", deadline=deadline, cancelled=cancelled)
    assert result.outcome == "success_rows"
    assert result.expirations == ["2026-08-28"]
    assert events == ["gateway", "limiter", "sdk", "close"]
    assert "deadline" not in result.request_identity and "cancelled" not in result.request_identity


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_chain_request_control_reaches_actual_gateway_and_type_fallback(tmp_path, controlled, fallback):
    from src.application.option_chain_fetching import OptionChainFetchRequest, fetch_option_chains
    from src.infrastructure.futu_gateway import retry_futu_gateway_call
    deadline = time.monotonic() + 30
    cancelled = lambda: False
    controls = {"deadline": deadline, "cancelled": cancelled} if controlled else {}
    calls = []
    class Gateway:
        def get_option_chain(self, **kwargs):
            calls.append(dict(kwargs))
            assert {name: kwargs[name] for name in ("deadline", "cancelled") if name in kwargs} == controls
            if fallback and "option_type" in kwargs:
                raise TypeError("unexpected keyword argument 'option_type'")
            return pd.DataFrame([{"code": "US.NVDA.2026-09-18.P100", "strike_time": "2026-09-18",
                "strike_price": 100, "option_type": "PUT", "lot_size": 100}])
    result = fetch_option_chains(gateway=Gateway(), retry_call=retry_futu_gateway_call, request=OptionChainFetchRequest(
        symbol="NVDA", underlier_code="US.NVDA", expirations=["2026-09-18"], option_types="put",
        base_dir=tmp_path, chain_cache=False, no_retry=True, max_calls=10000, window_sec=0.01,
        max_wait_sec=0.01, **controls))
    assert result.source_outcome == "success_rows" and len(result.rows) == 1
    assert len(calls) == (2 if fallback else 1)
    assert calls[0]["option_type"] == "PUT"
    if fallback:
        assert "option_type" not in calls[-1]
    assert all(call["code"] == "US.NVDA" and call["start"] == call["end"] == "2026-09-18" for call in calls)


@pytest.mark.parametrize("controlled", [False, True])
def test_earnings_actual_gateway_receives_controls_only_when_present(controlled):
    from src.application.earnings_calendar import fetch_market_earnings_calendar
    from tests.test_earnings_calendar_deadline import _request
    deadline = time.monotonic() + 30
    cancelled = lambda: False
    controls = {"deadline": deadline, "cancelled": cancelled} if controlled else {}
    calls = []
    class Gateway:
        def get_earnings_calendar(self, **kwargs):
            assert {name: kwargs[name] for name in ("deadline", "cancelled") if name in kwargs} == controls
            calls.append(kwargs)
            return []
    request = _request()
    result = fetch_market_earnings_calendar(gateway=Gateway(), **request,
        now_fn=lambda: request["scan_at_utc"], **controls)
    assert result["status"] == "ready"
    assert len(calls) == 3 and all(call["market"] == "US" for call in calls)
    assert calls[0]["begin_date"] == "2026-08-06" and calls[-1]["end_date"] == "2026-08-21"


def test_discovery_does_not_convert_run_cancellation_to_provider_failure(tmp_path):
    from src.application.opend_symbol_chain_fetching import discover_option_expirations
    stopped = [False]
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs)
        stopped[0] = True
        return ["2026-08-28"]
    with pytest.raises(TimeoutError, match="cancelled"):
        discover_option_expirations("AAPL", base_dir=tmp_path, trading_date="2026-08-04",
            list_expirations_fn=fetch, cancelled=lambda: stopped[0])
    assert len(calls) == 1


def test_wave_wait_and_warmup_do_not_start_after_cancellation(monkeypatch):
    from src.application.multi_tick import required_data_prefetch as prefetch
    def no_sleep(seconds):
        pytest.fail("cancelled rate-limit wait slept")
    monkeypatch.setattr("src.infrastructure.futu_gateway.time.sleep", no_sleep)
    with pytest.raises(TimeoutError, match="cancelled"):
        prefetch._sleep_after_rate_limit_wave(30, cancelled=lambda: True)
    with pytest.raises(TimeoutError, match="cancelled"):
        prefetch.warm_required_data_chain_cache(base=Path("unused"), base_cfg={}, cfg_path=Path("unused"),
            accounts=["lx"], markets_to_run=["US"], symbols_arg=None, next_formal_target_utc="2026-08-04T00:00:00Z",
            worker_stop_at_utc="2026-08-04T00:00:00Z", lock_release_by_utc="2026-08-04T00:00:00Z",
            cancelled=lambda: True)


@pytest.mark.parametrize("control", ["deadline", "cancelled"])
def test_started_warm_worker_obeys_run_control_and_releases_owned_lock(tmp_path, monkeypatch, control):
    from datetime import datetime, timedelta, timezone
    from src.application.multi_tick import required_data_prefetch as prefetch
    from src.infrastructure.private_storage import exclusive_private_file_lock

    context = prefetch.multiprocessing.get_context("fork")
    marker = tmp_path / "worker-entered"
    def blocked_worker(**kwargs):
        assert kwargs.get("deadline") is not None or kwargs.get("cancelled") is not None
        marker.write_text("entered", encoding="utf-8")
        while True:
            time.sleep(1)
    monkeypatch.setattr(prefetch.multiprocessing, "get_context", lambda mode: context)
    monkeypatch.setattr(prefetch, "_warm_required_data_chain_cache_inprocess", blocked_worker)
    now = datetime.now(timezone.utc)
    controls = {"cancelled": marker.exists}
    if control == "deadline":
        origin = time.monotonic()
        deadline = origin + 0.3
        # Exercise an already-running lock owner. Advance the controlled clock
        # only after the child enters that scope; scheduling before fork must
        # not decide whether the in-flight branch is reached.
        clock = SimpleNamespace(
            monotonic=lambda: deadline + 0.001 if marker.exists() else origin,
            sleep=time.sleep,
        )
        monkeypatch.setattr(prefetch, "time", clock)
        monkeypatch.setattr("src.infrastructure.fetch_control.time", clock)
        controls = {"deadline": deadline}
    started = time.monotonic()
    summary = prefetch.warm_required_data_chain_cache(
        base=tmp_path, base_cfg={"symbols": []}, cfg_path=tmp_path / "config.us.json",
        accounts=["lx"], markets_to_run=["US"], symbols_arg=None,
        next_formal_target_utc=(now + timedelta(seconds=180)).isoformat(),
        worker_stop_at_utc=(now + timedelta(seconds=30)).isoformat(),
        lock_release_by_utc=(now + timedelta(seconds=120)).isoformat(), **controls)
    assert marker.exists()
    assert summary["status"] == "deadline_reached" and summary["child_terminated"] is True
    assert ("worker_deadline_reached" if control == "deadline" else "worker_cancelled") in summary["reason_codes"]
    assert summary["duration_ms"] < 3000
    assert time.monotonic() - started < 3
    with exclusive_private_file_lock(tmp_path / "output_shared" / "state" / "required_data_prefetch.lock"):
        pass


def test_next_symbol_cannot_reacquire_parent_budget(monkeypatch):
    from src.application import pipeline_watchlist as pipeline
    from src.application.symbol_monitoring import SymbolProcessingTimeout
    clock = [10.0]
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(pipeline, "current_thread", lambda: object())
    with pipeline._enforce_symbol_timeout(symbol="AAPL", timeout_sec=120, deadline=11) as deadline:
        assert deadline == 11
        clock[0] = 10.5
    with pipeline._enforce_symbol_timeout(symbol="MSFT", timeout_sec=120, deadline=11) as deadline:
        assert deadline == 11
    clock[0] = 11
    with pytest.raises(SymbolProcessingTimeout, match="Tick deadline"):
        with pipeline._enforce_symbol_timeout(symbol="NVDA", timeout_sec=120, deadline=11):
            pytest.fail("expired queued symbol started")


def test_prefetch_subprocess_control_uses_existing_runner_and_not_plan_identity(tmp_path, monkeypatch):
    from src.application.multi_tick import required_data_prefetch as prefetch
    from src.application import required_data_planning as planning, opend_utils
    from tests import test_required_data_prefetch_inprocess as fixture
    deadline = time.monotonic() + 30
    cancelled = lambda: False
    controls = []
    fixture._patch_prefetch_sources(monkeypatch, watchlist=[{
        "symbol": "AAPL", "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 50, "max_strike": 200},
    }])
    monkeypatch.setattr(prefetch, "_prefill_spot_observation_cache", lambda **kwargs: ({}, set()))
    monkeypatch.setattr(prefetch, "prefetch_market_earnings_calendars", lambda **kwargs: {})
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(planning, "list_option_expirations", lambda *args, **kwargs: ["2026-08-28"])
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 8, 4))
    def command(cmd, **kwargs):
        assert kwargs["deadline"] == deadline and kwargs["cancelled"] is cancelled
        assert float(kwargs["env"]["OM_TICK_DEADLINE_MONOTONIC"]) == deadline
        controls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="isolated failure")
    monkeypatch.setattr(prefetch, "run_command", command)
    result = fixture._prefetch(tmp_path, execution_mode="subprocess", deadline=deadline, cancelled=cancelled)
    assert result["errors"] == 1
    assert len(controls) == 1
    plan = result["global_required_data_plan"]
    from src.application.required_data_plan_identity import required_data_plan_id
    assert plan["plan_id"] == required_data_plan_id(plan["symbols"])
    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)
    assert not ({"deadline", "cancelled"} & set(keys(plan)))


def test_earnings_prefetch_forwards_same_control_to_existing_boundaries(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from src.application import earnings_calendar as calendar
    deadline = time.monotonic() + 30
    cancelled = lambda: False
    seen = []
    class Gateway:
        def close(self):
            seen.append("close")
    def build(**kwargs):
        assert kwargs["deadline"] == deadline and kwargs["cancelled"] is cancelled
        seen.append("gateway")
        return Gateway()
    def fetch(**kwargs):
        assert kwargs["deadline"] == deadline and kwargs["cancelled"] is cancelled
        seen.append("calendar")
        return {"schema_version": calendar.EARNINGS_CALENDAR_SCHEMA_VERSION,
                "status": "ok", "absence_authoritative": True, "intervals": [], "snapshot_hash": "a" * 64}
    monkeypatch.setattr(calendar, "fetch_market_earnings_calendar", fetch)
    calendar.prefetch_market_earnings_calendars(market_requests={"US": {"scan_date": "2026-08-04"}},
        output_dir=tmp_path, scan_at_utc=datetime.now(timezone.utc), gateway_builder=build,
        deadline=deadline, cancelled=cancelled)
    assert seen == ["gateway", "calendar", "close"]
