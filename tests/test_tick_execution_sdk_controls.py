"""Tick controls reach the existing SDK process while its query is blocked."""
from __future__ import annotations

import json
import multiprocessing.connection
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from src.application import plugin_runtime
from src.infrastructure import futu_gateway as gateway


class _BlockedSDK:
    def __init__(self, proof):
        self.proof = proof
        self.lock = threading.Lock()
        self.closed = threading.Event()
        threading.Thread(target=self.closed.wait, daemon=False).start()

    def get_sync_conn_id(self):
        return 1

    def _block(self, operation, params):
        assert "deadline" not in params and "cancelled" not in params
        Path(self.proof).write_text(json.dumps({"operation": operation, "params": params}))
        with self.lock:
            threading.Event().wait()

    def get_option_expiration_date(self, code):
        return self._block("expirations", {"code": code})

    def get_option_chain(self, **kwargs):
        return self._block("chain", kwargs)

    def get_market_snapshot(self, **kwargs):
        return self._block("snapshot", kwargs)

    def get_earnings_calendar(self, **kwargs):
        return self._block("earnings", kwargs)

    def get_market_state(self, **kwargs):
        return self._block("state", kwargs)

    def get_user_info(self):
        return self._block("rights", {})

    def request_trading_days(self, **kwargs):
        return self._block("calendar", kwargs)

    def get_global_state(self):
        return self._block("readiness", {})

    def position_list_query(self, **kwargs):
        return self._block("positions", kwargs)

    def accinfo_query(self, **kwargs):
        return self._block("balance", kwargs)

    def close(self):
        with self.lock:
            self.closed.set()


def _blocked_sdk_factory(_kind, kwargs):
    return _BlockedSDK(kwargs["proof"])


def _blocked_handshake_factory(_kind, _kwargs):
    threading.Event().wait()


_OPERATIONS = [
    ("get_option_expiration_dates", {"code": "US.TEST"}, "quote", "expirations"),
    ("get_option_chain", {"code": "US.TEST", "start": "2026-12-18", "end": "2026-12-18"}, "quote", "chain"),
    ("get_snapshot", {"codes": ["US.TEST"]}, "quote", "snapshot"),
    ("get_snapshot_with_request_hook", {"codes": ["US.TEST"]}, "quote", "snapshot"),
    ("get_earnings_calendar", {"market": "US", "begin_date": "2026-11-13", "end_date": "2026-11-13"}, "quote", "earnings"),
    ("get_market_state", {"codes": ["US.TEST"]}, "quote", "state"),
    ("get_quote_rights", {"market": "HK"}, "quote", "rights"),
    ("get_trading_days", {"market": "HK", "start": "2026-11-13", "end": "2026-11-13"}, "quote", "calendar"),
    ("get_trading_days_with_receipt", {"market": "HK", "start": "2026-11-13", "end": "2026-11-13"}, "quote", "calendar"),
    ("ensure_broker_ready", {"expected_account_ids": ["1"], "trd_env": "REAL"}, "trade", "readiness"),
    ("get_positions", {"acc_id": "1"}, "trade", "positions"),
    ("get_positions_with_receipt", {"acc_id": "1"}, "trade", "positions"),
    ("get_account_balance", {"acc_id": "1"}, "trade", "balance"),
]


_QUERY_CASES = [(*case, "explicit") for case in _OPERATIONS] + [
    (*case, source)
    for case in _OPERATIONS if case[0] in {"get_snapshot", "get_snapshot_with_request_hook"}
    for source in ("initialized", "scoped")
]


@pytest.mark.parametrize("method,params,kind,operation,control_source", _QUERY_CASES)
@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancelled"])
def test_gateway_controls_stop_real_blocked_sdk_query(monkeypatch, tmp_path, method, params, kind, operation, control_source, cancel):
    monkeypatch.setattr(plugin_runtime, "require_plugin", lambda _name: SimpleNamespace())
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 10)
    proof = tmp_path / "sdk-call.json"
    remote = gateway._FutuContextProcess(kind, {"proof": str(proof)}, factory=_blocked_sdk_factory)
    proxy = gateway._FutuQueryContext(remote, deadline=None, cancelled=None)
    proxy._call("connect", remote.wait_ready)
    backend = gateway._FutuAPIBackend(host="offline.invalid", port=1)
    setattr(backend, f"_{kind}_client", proxy)
    client = gateway._FutuAPIClient(backend, is_option_chain_cache_enabled=False)
    facade = gateway.FutuGateway(client=client, backend=backend, host="offline.invalid", port=1)
    stop = threading.Event()
    timer = threading.Timer(.15, stop.set) if cancel else None
    if timer:
        timer.start()
    started = time.monotonic()
    controls = {"cancelled": stop.is_set} if cancel else {"deadline": started + .15}
    token = None
    method_controls = controls
    if control_source == "initialized":
        proxy = gateway._FutuQueryContext(
            remote, deadline=controls.get("deadline"), cancelled=controls.get("cancelled"),
        )
        setattr(backend, f"_{kind}_client", proxy)
        method_controls = {}
    elif control_source == "scoped":
        token = gateway._FETCH_CONTROL.set((controls.get("deadline"), controls.get("cancelled")))
        method_controls = {}
    requests = []
    if method == "get_snapshot_with_request_hook":
        params = {**params, "before_request": lambda: requests.append("started")}
    try:
        with pytest.raises(TimeoutError, match="cancelled" if cancel else "deadline"):
            getattr(facade, method)(**params, **method_controls)
        assert time.monotonic() - started < 2, "caller waited for the independent SDK timeout"
        recorded = json.loads(proof.read_text())
        assert recorded["operation"] == operation
        assert "deadline" not in recorded["params"] and "cancelled" not in recorded["params"]
        if method == "get_snapshot_with_request_hook":
            assert requests == ["started"]
    finally:
        if token is not None:
            gateway._FETCH_CONTROL.reset(token)
        if timer:
            timer.cancel()
            timer.join(timeout=1)
        proxy.close()
        remote.abort()
        assert multiprocessing.connection.wait([remote._process.sentinel], timeout=3), "abandoned SDK worker remained alive"


def test_provider_timeout_keeps_its_existing_error_mapping(monkeypatch):
    monkeypatch.setattr(plugin_runtime, "require_plugin", lambda _name: SimpleNamespace())

    def snapshot(**_kwargs):
        raise TimeoutError("provider wait failed")

    facade = gateway.FutuGateway(client=SimpleNamespace(get_snapshot=snapshot),
                                 backend=SimpleNamespace(), host="offline.invalid", port=1)
    with pytest.raises(gateway.FutuGatewayTransientError, match="provider wait failed"):
        facade.get_snapshot(["US.TEST"])


@pytest.mark.parametrize("kind", ["quote", "trade"])
def test_native_client_initialization_consumes_the_same_deadline(monkeypatch, kind):
    monkeypatch.setattr(plugin_runtime, "require_plugin", lambda _name: SimpleNamespace())
    monkeypatch.setattr(gateway, "_ensure_futu_api_importable", lambda: None)
    monkeypatch.setattr(gateway, "port_open", lambda *args, **kwargs: True)
    monkeypatch.setattr(gateway, "_create_futu_sdk_context", _blocked_handshake_factory)
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 10)
    remotes = []
    original = gateway._FutuContextProcess

    class ObservedProcess(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            remotes.append(self)

    monkeypatch.setattr(gateway, "_FutuContextProcess", ObservedProcess)
    backend = gateway._FutuAPIBackend(host="offline.invalid", port=1)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            getattr(backend, f"_ensure_{kind}_client")(deadline=started + .25)
        assert time.monotonic() - started < 2
        assert len(remotes) == 1
    finally:
        for remote in remotes:
            remote.abort()
            assert multiprocessing.connection.wait([remote._process.sentinel], timeout=3)
