"""Real readiness/fetch owners with isolated transports and durable source receipts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import socket
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application import opend_market_snapshot_fetching as snapshots
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.research import watchlist_series as group
from src.infrastructure.futu_gateway import FutuGateway
from src.infrastructure import futu_gateway as futu
from test_watchlist_research_scope import inputs  # noqa: F401
from test_watchlist_research_series import planned  # noqa: F401


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("regression attempted real network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def immediate(**kwargs):
    return kwargs["call"]()


@pytest.mark.parametrize("mode", ["raw", "wrapped", "default"])
def test_market_admission_failure_has_no_physical_receipt(tmp_path, monkeypatch, mode):
    from src.application import plugin_runtime

    now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    transport, admitted, closed, window_calls = [], [], [], []
    unavailable = plugin_runtime.PluginUnavailableError("market-opend unavailable")

    def require(plugin_id):
        admitted.append(plugin_id)
        raise unavailable

    monkeypatch.setattr(plugin_runtime, "require_plugin", require)
    raw = FutuGateway(
        client=SimpleNamespace(get_snapshot=lambda **kw: transport.append(kw)),
        backend=SimpleNamespace(_quote_client=SimpleNamespace(close=lambda: closed.append(True))),
        host="fixture.invalid", port=11111,
    )
    gateway = raw if mode != "wrapped" else series._WindowGateway(
        raw, start=now, end=now + timedelta(minutes=1), clock=lambda: now, calls=window_calls,
    )
    plan = {"schema_version": quote.PLAN_SCHEMA, "scope_id": "admission-regression",
            "observation_at_utc": now.isoformat(), "option_codes": ["US.ACME261120P90000"]}
    out = tmp_path / "capture"
    manifest = quote.collect_option_quotes(
        plan, output_dir=out, host="fixture.invalid", port=11111, confirm=True,
        gateway_factory=None if mode == "default" else lambda **_: gateway,
        now_utc=now, rate_limited_call=immediate,
    )
    data = json.loads((out / "quotes.json").read_bytes())
    assert not transport
    assert admitted == ["market-opend"]  # Admission failure must not enter fallback.
    assert manifest["execution_error_type"] == "PluginUnavailableError"
    assert manifest["complete"] is False and manifest["source_snapshot_id"] is None
    assert not (out / "receipt.json").exists()
    assert data["provider_requested_at_utc"] is data["provider_received_at_utc"] is None
    assert all(not receipt["provider_call_started"] for receipt in data["batch_receipts"])
    assert len(closed) == int(mode == "raw")
    assert not window_calls


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("phase", ["initialization_failure", "window_cross", "ready", "fallback", "retry", "cancel"])
def test_native_snapshot_hook_tracks_only_transport(tmp_path, monkeypatch, wrapped, phase):
    now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    end = now + timedelta(seconds=10)
    state = SimpleNamespace(now=now)
    events, window_calls, closed = [], [], []
    cancellation = KeyboardInterrupt("native snapshot cancellation")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now if tz else state.now.replace(tzinfo=None)

    class Context:
        def get_market_snapshot(self, code_list):
            events.append(("snapshot", state.now))
            if phase == "cancel":
                raise cancellation
            if phase == "retry" and sum(event == "snapshot" for event, _ in events) == 1:
                raise ConnectionResetError("isolated connection reset")
            if phase == "fallback" and sum(event == "snapshot" for event, _ in events) == 1:
                return 0, pd.DataFrame([])
            return 0, pd.DataFrame([{"code": code, "bid_price": 1, "ask_price": 1.1} for code in code_list])

        def close(self):
            closed.append(True)

    class Backend:
        _quote_client = None

        def _ensure_quote_client(self):
            if self._quote_client is None:
                events.append(("initialize", state.now))
                if phase == "initialization_failure":
                    raise RuntimeError("isolated SDK initialization failure")
                self._quote_client = Context()
                state.now = end if phase == "window_cross" else now + timedelta(seconds=1)
            return self._quote_client

    monkeypatch.setattr(snapshots, "datetime", Clock)
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=0))
    backend = Backend()
    raw = FutuGateway(
        client=futu._FutuAPIClient(backend, is_option_chain_cache_enabled=False),
        backend=backend, host="fixture.invalid", port=11111,
    )
    gateway = series._WindowGateway(raw, start=now, end=end, clock=lambda: state.now, calls=window_calls) if wrapped else raw
    kwargs = dict(
        option_codes=["US.ACME261120P90000"], gateway=gateway, base_dir=tmp_path,
        snapshot_limit=quote.SNAPSHOT_LIMIT, rate_limited_call=immediate, no_retry=phase != "retry",
        retry_max_attempts=2, retry_base_delay_sec=0, retry_max_delay_sec=0,
    )
    try:
        if phase == "cancel":
            with pytest.raises(KeyboardInterrupt) as raised:
                snapshots.fetch_option_snapshots(**kwargs)
            assert raised.value is cancellation
            result = raised.value.partial_result
        else:
            result = snapshots.fetch_option_snapshots(**kwargs)
    finally:
        raw.close()
    physical = [at for event, at in events if event == "snapshot"]
    assert result.opend_call_count == len(physical)
    started = [receipt for receipt in result.batch_receipts if receipt["provider_call_started"]]
    assert len(started) == len(physical)
    assert [receipt["requested_at_utc"] for receipt in started] == [at.isoformat() for at in physical]
    assert len(closed) == int(phase != "initialization_failure")
    if phase == "initialization_failure" or (wrapped and phase == "window_cross"):
        assert not physical
        assert result.requested_at_utc is result.received_at_utc is None
    else:
        assert len(physical) == (2 if phase in {"fallback", "retry"} else 1)
    if wrapped:
        assert [call["requested_at_utc"] for call in window_calls] == [
            series._stamp(at) for at in physical
        ]
        assert all(call["requested_at_utc"] and call["received_at_utc"] for call in window_calls)


@pytest.mark.parametrize("wrapped", [False, True])
def test_fallback_admission_failure_propagates_original_with_partial_receipt(tmp_path, monkeypatch, wrapped):
    from src.application import plugin_runtime

    now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    admitted, transport = [], []
    unavailable = plugin_runtime.PluginUnavailableError("isolated fallback admission failure")

    def require(plugin_id):
        admitted.append(plugin_id)
        if len(admitted) > 1:
            raise unavailable

    monkeypatch.setattr(plugin_runtime, "require_plugin", require)
    raw = FutuGateway(
        client=SimpleNamespace(get_snapshot=lambda **kw: transport.append(kw) or pd.DataFrame([])),
        backend=SimpleNamespace(), host="fixture.invalid", port=11111,
    )
    window_calls = []
    gateway = series._WindowGateway(
        raw, start=now, end=now + timedelta(minutes=1), clock=lambda: now, calls=window_calls,
    ) if wrapped else raw
    with pytest.raises(plugin_runtime.PluginUnavailableError) as raised:
        snapshots.fetch_option_snapshots(
            option_codes=["US.ACME261120P90000"], gateway=gateway, base_dir=tmp_path,
            snapshot_limit=quote.SNAPSHOT_LIMIT, rate_limited_call=immediate,
        )
    assert raised.value is unavailable
    result = raised.value.partial_result
    assert len(admitted) == 2 and len(transport) == result.opend_call_count == 1
    assert [receipt["provider_call_started"] for receipt in result.batch_receipts] == [True, False]
    assert result.batch_receipts[-1]["error_code"] == "PluginUnavailableError"
    assert result.batch_receipts[-1]["requested_at_utc"] is result.batch_receipts[-1]["received_at_utc"] is None
    if wrapped:
        assert len(window_calls) == 1
        assert window_calls[0]["requested_at_utc"] and window_calls[0]["received_at_utc"]


@pytest.mark.parametrize("failure", ["missing_market", "initialization_failure"])
def test_series_zero_physical_calls_persist_failed_status(planned, monkeypatch, failure):
    from src.application import plugin_runtime

    _, _, plan, args, state = planned
    closed = []

    def require(_plugin_id):
        raise plugin_runtime.PluginUnavailableError("isolated market admission failure")

    class Backend:
        _quote_client = None

        def _ensure_quote_client(self):
            raise RuntimeError("isolated SDK initialization failure")

    def factory(**kwargs):
        backend = Backend()
        gateway = FutuGateway(
            client=futu._FutuAPIClient(backend, is_option_chain_cache_enabled=False),
            backend=backend, **kwargs,
        )
        gateway.close = lambda: closed.append(True)
        return gateway

    if failure == "missing_market":
        monkeypatch.setattr(plugin_runtime, "require_plugin", require)
    result = group.collect_watchlist_series(**{**args, "gateway_factory": factory}, confirm=True)
    attempts = sorted(args["archive_root"].rglob("attempt.json"))
    assert len(attempts) == len(result["batches"]) == len(plan["batches"])
    assert len(closed) == 1
    assert not state.calls
    for path in attempts:
        attempt = json.loads(path.read_bytes())
        assert attempt["status"] == "failed"
        assert attempt["provider_calls"] == []
        manifest = json.loads((path.parent / "quotes/manifest.json").read_bytes())
        assert manifest["source_snapshot_id"] is None
        assert not (path.parent / "quotes/receipt.json").exists()


@pytest.mark.parametrize("failure", ["missing_market", "sdk_import_failure"])
def test_default_series_readiness_rejects_before_physical_attempt(planned, monkeypatch, failure):
    from src.application import plugin_runtime

    root, _, plan, group_args, state = planned
    child = plan["batches"][0]
    events, closed = [], []
    raw = futu.build_futu_gateway(host="fixture.invalid", port=11111)
    original_close = raw.close

    def close():
        closed.append(True)
        original_close()

    def build(**_kwargs):
        events.append("build")
        return raw

    def require(_plugin_id):
        raise plugin_runtime.PluginUnavailableError("isolated default market admission failure")

    def sdk_import():
        events.append("sdk_import")
        raise ModuleNotFoundError("isolated missing futu SDK")

    def forbidden(*_args, **_kwargs):
        events.append("forbidden_transport")
        raise AssertionError("readiness attempted transport before SDK admission")

    raw.close = close
    monkeypatch.setattr(series, "build_futu_gateway", build)
    monkeypatch.setattr(futu, "_ensure_futu_api_importable", sdk_import)
    monkeypatch.setattr(futu, "port_open", forbidden)
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(OpenQuoteContext=forbidden))
    if failure == "missing_market":
        monkeypatch.setattr(plugin_runtime, "require_plugin", require)
    safe_file = root / "default-admission-safe.json"
    safe_file.write_bytes(series._bytes({
        "schema_version": "research.safe-window.v1",
        "series_plan_sha256": child["series_plan_sha256"],
        "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
    }))
    archive = root / "default-admission-archive"
    args = dict(
        series_dir=root / "plan/batches" / child["batch_id"],
        expected_plan_sha256=child["series_plan_sha256"],
        safe_window_file=safe_file,
        expected_safe_window_sha256=quote._sha256(safe_file.read_bytes()),
        archive_root=archive, runtime_root=group_args["runtime_root"], prune_roots=group_args["prune_roots"],
        host="fixture.invalid", port=11111, clock=lambda: state.now,
        gateway_factory=None, rate_limited_call=immediate, confirm=True,
    )
    attempt = series.collect_series(**args)["observations"][0]
    assert events == (["build"] if failure == "missing_market" else ["build", "sdk_import"])
    assert closed == [True] and not state.calls
    assert attempt["status"] == "failed" and attempt["provider_calls"] == []
    persisted = list(archive.rglob("attempt.json"))
    assert len(persisted) == 1 and json.loads(persisted[0].read_bytes()) == attempt
    assert not list(archive.rglob("receipt.json"))
    before = list(events)
    assert series.collect_series(**args)["observations"][0]["status"] == "already_claimed"
    assert events == before and closed == [True]


@pytest.mark.parametrize("mode", ["ready", "wait_outside_window", "not_ready"])
def test_default_series_uses_real_readiness_contract(planned, monkeypatch, mode):
    root, _, plan, group_args, state = planned
    child = plan["batches"][0]
    preflights, connected, closed = [], [], []

    class QuoteClient:
        def get_global_state(self):
            preflights.append(state.now)
            return 0, {"program_status_type": "READY", "qot_logined": mode != "not_ready"}

        def close(self):
            closed.append(True)

    class Backend:
        _quote_client = None

        def _ensure_quote_client(self):
            connected.append(state.now)
            self._quote_client = QuoteClient()
            if mode == "wait_outside_window":
                state.now += timedelta(minutes=1)
            return self._quote_client

    backend = Backend()
    client = SimpleNamespace(
        get_snapshot=lambda code_list: (
            state.calls.append(list(code_list)) or pd.DataFrame(
                [{"code": code, "last_price": 100, "bid_price": 1, "ask_price": 1.1} for code in code_list]
            )
        )
    )
    gateway = FutuGateway(client=client, backend=backend, host="fixture.invalid", port=11111)
    monkeypatch.setattr(series, "build_futu_gateway", lambda **_: gateway)
    safe_file = root / "child-safe.json"
    safe_file.write_bytes(series._bytes({
        "schema_version": "research.safe-window.v1",
        "series_plan_sha256": child["series_plan_sha256"],
        "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
    }))
    args = dict(
        series_dir=root / "plan/batches" / child["batch_id"],
        expected_plan_sha256=child["series_plan_sha256"],
        safe_window_file=safe_file,
        expected_safe_window_sha256=quote._sha256(safe_file.read_bytes()),
        archive_root=root / "child-archive",
        runtime_root=group_args["runtime_root"],
        prune_roots=group_args["prune_roots"],
        host="fixture.invalid", port=11111, clock=lambda: state.now,
        rate_limited_call=immediate, confirm=True,
    )
    attempt = series.collect_series(**args)["observations"][0]
    assert "TypeError" not in attempt["reasons"]
    assert len(connected) == len(closed) == 1
    if mode == "ready":
        assert len(preflights) == 1 and len(state.calls) == 2
        assert attempt["quote_package_status"] == "complete"
        assert all(call["status"] == "received" for call in attempt["provider_calls"])
    elif mode == "wait_outside_window":
        assert not preflights and not state.calls
        assert "ValueError" in attempt["reasons"]
    else:
        assert len(preflights) == 1 and not state.calls
        assert any(reason.startswith("FutuGateway") for reason in attempt["reasons"])
    physical_calls = list(state.calls)
    assert series.collect_series(**args)["observations"][0]["status"] == "already_claimed"
    assert state.calls == physical_calls and len(connected) == 1


def test_readiness_checks_window_before_connect_and_global_state():
    events = []
    quote_client = SimpleNamespace(get_global_state=lambda: events.append("global") or (
        0, {"program_status_type": "READY", "qot_logined": True}
    ))
    backend = SimpleNamespace(_ensure_quote_client=lambda: events.append("connect") or quote_client)
    gateway = FutuGateway(client=None, backend=backend, host="fixture.invalid", port=11111)
    gateway.ensure_quote_ready(before_request=lambda: events.append("check"))
    assert events == ["check", "connect", "check", "global"]

    def outside():
        raise ValueError("provider_request_outside_window")

    events.clear()
    with pytest.raises(ValueError, match="outside_window"):
        gateway.ensure_quote_ready(before_request=outside)
    assert not events


@pytest.mark.parametrize("cancel_type", [KeyboardInterrupt, asyncio.CancelledError, BaseException])
@pytest.mark.parametrize("phase", ["first", "later", "fallback", "before_request", "before_connect"])
@pytest.mark.parametrize("close_error", [None, RuntimeError, KeyboardInterrupt])
def test_cancelled_capture_preserves_physical_calls(tmp_path, monkeypatch, cancel_type, phase, close_error):
    now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)
    codes = ["US.ACME261120P90000", "US.ACME261120P91000"] if phase == "later" else ["US.ACME261120P90000"]
    calls, closed = [], []
    cancellation = cancel_type("isolated cancellation")

    class Gateway:
        def get_snapshot(self, batch):
            calls.append(list(batch))
            if len(calls) == 1 and phase in {"later", "fallback"}:
                return pd.DataFrame([] if phase == "fallback" else [
                    {"code": batch[0], "bid_price": 1, "ask_price": 1.1}
                ])
            raise cancellation

        def close(self):
            closed.append(True)
            if close_error is not None:
                raise close_error("isolated close failure")

    def factory(**_kwargs):
        if phase == "before_connect":
            raise cancellation
        return Gateway()

    def rate(**kwargs):
        if phase == "before_request":
            raise cancellation
        return kwargs["call"]()

    def fetch(**kwargs):
        return snapshots.fetch_option_snapshots(**kwargs, snapshot_batch_size=1)

    plan = {"schema_version": quote.PLAN_SCHEMA, "scope_id": "cancel-regression",
            "observation_at_utc": "2026-10-30T19:55:00Z", "option_codes": codes}
    out = tmp_path / "capture"
    with pytest.raises(cancel_type) as raised:
        quote.collect_option_quotes(
            plan, output_dir=out, host="fixture.invalid", port=11111, confirm=True,
            gateway_factory=factory, fetch_snapshots_fn=fetch if phase in {"later", "fallback"} else None,
            now_utc=now, rate_limited_call=rate,
        )
    assert raised.value is cancellation
    assert len(closed) == (0 if phase == "before_connect" else 1)
    data = json.loads((out / "quotes.json").read_bytes())
    manifest = json.loads((out / "manifest.json").read_bytes())
    assert manifest["complete"] is data["complete"] is False
    assert manifest["execution_error_type"] == cancel_type.__name__
    assert manifest["files"]["quotes.json"] == quote._sha256((out / "quotes.json").read_bytes())
    receipts = data["batch_receipts"]
    started = [receipt for receipt in receipts if receipt["provider_call_started"]]
    assert len(started) == len(calls)
    assert [receipt["requested_codes"] for receipt in started] == calls
    assert data["returned_codes"] == (codes[:1] if phase == "later" else [])
    if calls:
        assert started[-1]["status"] == "failed"
        assert started[-1]["error_code"] == cancel_type.__name__
        assert all(receipt["requested_at_utc"] and receipt["received_at_utc"] for receipt in started)
        assert data["provider_requested_at_utc"] and data["provider_received_at_utc"]
        receipt = json.loads((out / "receipt.json").read_bytes())
        assert receipt["payload_sha256"] == manifest["files"]["quotes.json"]
        assert receipt["snapshot_id"] == manifest["source_snapshot_id"]
        partial = raised.value.partial_result
        assert partial.opend_call_count == len(calls) and partial.complete is False
        assert list(partial.batch_receipts) == receipts
    else:
        assert not (out / "receipt.json").exists()
        assert data["provider_requested_at_utc"] is data["provider_received_at_utc"] is None


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("no_retry", [False, True])
@pytest.mark.parametrize("rate_limited_call", [immediate, snapshots.rate_limited_opend_call], ids=["isolated_limiter", "real_limiter"])
def test_inflight_cancellation_is_not_replaced_by_control_check(tmp_path, fallback, no_retry, rate_limited_call):
    cancellation = KeyboardInterrupt("original cancellation")
    calls = []
    stopped = False

    class Gateway:
        def get_snapshot(self, codes, **_controls):
            nonlocal stopped
            calls.append(list(codes))
            if fallback and len(calls) == 1:
                return pd.DataFrame([])
            stopped = True
            raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        snapshots.fetch_option_snapshots(
            option_codes=["US.ACME261120P90000"], gateway=Gateway(), base_dir=tmp_path,
            snapshot_limit=quote.SNAPSHOT_LIMIT, rate_limited_call=rate_limited_call,
            cancelled=lambda: stopped, no_retry=no_retry,
        )
    assert raised.value is cancellation
    partial = raised.value.partial_result
    assert partial.opend_call_count == len(calls) == 1 + int(fallback)
    assert partial.batch_receipts[-1]["error_code"] == "KeyboardInterrupt"


def test_readiness_rechecks_window_after_port_probe(monkeypatch):
    start = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
    state = SimpleNamespace(now=start + timedelta(seconds=59.9))
    connections = []

    def probe(*_args):
        state.now += timedelta(seconds=0.2)
        return True

    monkeypatch.setattr(futu, "_ensure_futu_api_importable", lambda: None)
    monkeypatch.setattr(futu, "port_open", probe)
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
        OpenQuoteContext=lambda **kwargs: connections.append(kwargs)
    ))
    gateway = futu.build_futu_gateway(host="fixture.invalid", port=11111)
    wrapper = series._WindowGateway(
        gateway, start=start, end=start + timedelta(minutes=1), clock=lambda: state.now, calls=[],
    )
    with pytest.raises(futu.FutuGatewayError) as raised:
        wrapper.ensure_quote_ready()
    assert isinstance(raised.value.raw_error, ValueError)
    assert str(raised.value.raw_error) == "provider_request_outside_window"
    assert not connections


def test_window_rejected_snapshot_does_not_claim_provider_call(tmp_path, monkeypatch):
    start = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
    state = SimpleNamespace(now=start + timedelta(seconds=1))
    calls = []
    gateway = SimpleNamespace(get_snapshot=lambda codes: calls.append(list(codes)))
    wrapper = series._WindowGateway(
        gateway, start=start, end=start + timedelta(minutes=1), clock=lambda: state.now, calls=[],
    )

    def wait_outside(**kwargs):
        state.now = start + timedelta(minutes=1)
        return kwargs["call"]()

    plan = {"schema_version": quote.PLAN_SCHEMA, "scope_id": "window-regression",
            "observation_at_utc": start.isoformat(), "option_codes": ["US.ACME261120P90000"]}
    out = tmp_path / "capture"
    manifest = quote.collect_option_quotes(
        plan, output_dir=out, host="fixture.invalid", port=11111, confirm=True,
        gateway_factory=lambda **_: wrapper, now_utc=state.now, rate_limited_call=wait_outside,
    )
    data = json.loads((out / "quotes.json").read_bytes())
    assert not calls
    assert manifest["complete"] is False and manifest["source_snapshot_id"] is None
    assert not (out / "receipt.json").exists()
    assert all(receipt["provider_call_started"] is False for receipt in data["batch_receipts"])
    assert data["provider_requested_at_utc"] is data["provider_received_at_utc"] is None


@pytest.mark.parametrize("mode", [
    "ready", "expired", "port_cross", "port_closed", "handshake_cross", "not_ready",
    "cancel_port", "cancel_handshake", "cancel_readiness", "cancel_snapshot",
])
def test_multishard_default_readiness_stays_inside_window(planned, monkeypatch, mode):
    root, _, plan, args, state = planned
    assert len(plan["batches"]) > 1
    args = {key: value for key, value in args.items() if key != "gateway_factory"}
    start = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    if mode == "expired":
        state.now = end
    elif mode in {"port_cross", "handshake_cross"}:
        state.now = end - timedelta(seconds=0.1)
    events, contexts = [], []
    cancellation = KeyboardInterrupt("isolated default entry cancellation")

    def probe(*_args):
        events.append(("probe", state.now))
        if mode == "cancel_port":
            raise cancellation
        if mode == "port_cross":
            state.now += timedelta(seconds=0.2)
        return mode != "port_closed"

    class Context:
        def __init__(self, **_kwargs):
            events.append(("connect", state.now))
            self.close_count = 0
            contexts.append(self)

        def set_sync_query_connect_timeout(self, _timeout):
            pass

        def get_sync_conn_id(self):
            if mode == "cancel_handshake":
                raise cancellation
            if mode == "handshake_cross":
                state.now += timedelta(seconds=0.2)
            return 1

        def get_global_state(self):
            events.append(("global_state", state.now))
            if mode == "cancel_readiness":
                raise cancellation
            return 0, {"program_status_type": "READY", "qot_logined": mode != "not_ready"}

        def get_market_snapshot(self, code_list):
            events.append(("snapshot", state.now))
            state.calls.append(list(code_list))
            if mode == "cancel_snapshot":
                raise cancellation
            return 0, pd.DataFrame([
                {"code": code, "last_price": 100, "bid_price": 1, "ask_price": 1.1}
                for code in code_list
            ])

        def close(self):
            self.close_count += 1

    monkeypatch.setattr(futu, "_ensure_futu_api_importable", lambda: None)
    monkeypatch.setattr(futu, "port_open", probe)
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=0, OpenQuoteContext=Context))
    monkeypatch.setattr(
        futu, "_start_futu_query_context",
        lambda kind, kwargs, *, deadline=None, cancelled=None: futu._FutuQueryContext(
            futu._create_futu_sdk_context(kind, kwargs),
            deadline=deadline, cancelled=cancelled,
        ),
    )
    if mode.startswith("cancel_"):
        with pytest.raises(KeyboardInterrupt) as raised:
            group.collect_watchlist_series(**args, confirm=True)
        assert raised.value is cancellation
    else:
        result = group.collect_watchlist_series(**args, confirm=True)
        assert len(result["batches"]) == len(plan["batches"])

    attempts = sorted(args["archive_root"].rglob("attempt.json"))
    data = [json.loads(path.read_bytes()) for path in attempts]
    assert len(data) == (1 if mode.startswith("cancel_") else len(plan["batches"]))
    assert all(start <= at < end for _, at in events), events
    if mode != "expired":
        assert data[0]["provider_calls"][0]["endpoint"] == "quote_preflight"
        assert all(call["requested_at_utc"] and call["received_at_utc"] for attempt in data for call in attempt["provider_calls"])
    assert all(context.close_count == 1 for context in contexts)
    if mode in {"expired", "port_cross", "port_closed", "cancel_port"}:
        assert not contexts
    if mode in {"expired", "port_cross", "port_closed", "cancel_port", "handshake_cross", "not_ready", "cancel_handshake", "cancel_readiness"}:
        assert not state.calls
    if mode in {"port_closed", "cancel_port", "cancel_handshake", "cancel_readiness", "not_ready"}:
        assert all(attempt["status"] == "partial" and attempt["provider_calls"] for attempt in data)
    if mode == "ready":
        assert len(contexts) == 1
        assert len(state.calls) == 2 * len(plan["batches"])
        assert all(attempt["quote_package_status"] == "complete" for attempt in data)
        calls = [call for attempt in data for call in attempt["provider_calls"]]
        assert sum(call["endpoint"] == "quote_preflight" for call in calls) == 1
        assert sum(call["endpoint"] == "market_snapshot" for call in calls) == 2 * len(plan["batches"])
        assert len(calls) == 1 + 2 * len(plan["batches"])
    if mode == "cancel_snapshot":
        quotes_path = attempts[0].parent / "quotes/quotes.json"
        captured = json.loads(quotes_path.read_bytes())
        assert captured["batch_receipts"][0]["provider_call_started"] is True
        assert captured["batch_receipts"][0]["error_code"] == "KeyboardInterrupt"

    frozen = {path: path.read_bytes() for path in attempts}
    events_before = list(events)
    state.now = end if mode.startswith("cancel_") else state.now
    group.collect_watchlist_series(**args, confirm=True)
    assert events == events_before
    assert all(path.read_bytes() == raw for path, raw in frozen.items())
    claims = list(args["archive_root"].rglob("claim.json"))
    assert len(claims) == len(list(args["archive_root"].rglob("attempt.json"))) == len(plan["batches"])


def test_multishard_explicit_factory_keeps_its_readiness_contract(planned):
    _, _, plan, args, state = planned
    factory = args["gateway_factory"]
    closed = []

    def injected(**kwargs):
        gateway = factory(**kwargs)

        def forbidden_readiness(**_kwargs):
            raise AssertionError("explicit factory owns its readiness")

        gateway.ensure_quote_ready = forbidden_readiness
        gateway.close = lambda: closed.append(True)
        return gateway

    result = group.collect_watchlist_series(**{**args, "gateway_factory": injected}, confirm=True)
    assert len(closed) == 1
    assert len(state.calls) == 2 * len(plan["batches"])
    assert all(
        call["endpoint"] == "market_snapshot"
        for batch in result["batches"] for attempt in batch["result"]["observations"]
        for call in attempt["provider_calls"]
    )
