from __future__ import annotations

import multiprocessing.connection
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from src.infrastructure import futu_gateway as gateway


class _SDKContext:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = threading.Event()
        # Model SDK-owned non-daemon threads which normal interpreter exit joins.
        threading.Thread(target=self.closed.wait, daemon=False).start()

    def get_sync_conn_id(self):
        return 1

    def blocked_query(self):
        with self.lock:
            threading.Event().wait()  # Neither a response nor an SDK timeout.

    def get_market_snapshot(self, codes):
        import pandas as pd
        return 0, pd.DataFrame([{"code": code, "last_price": 100.0} for code in codes])

    def connection_error(self):
        raise ConnectionError("transport unavailable")

    def close(self):
        with self.lock:
            self.closed.set()


def _sdk_factory(_kind, _kwargs):
    return _SDKContext()


def _context():
    remote = gateway._FutuContextProcess("quote", {}, factory=_sdk_factory)
    proxy = gateway._FutuQueryContext(remote, deadline=None, cancelled=None)
    proxy._call("connect", remote.wait_ready)
    return proxy, remote


def test_sdk_lock_timeout_kills_worker_and_next_context_queries_successfully(monkeypatch):
    first, first_remote = _context()
    # Startup/import time is separate from the per-request wait.
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 0.1)
    start = time.monotonic()
    with pytest.raises(gateway.FutuGatewayTransientError, match="query timeout"):
        first.blocked_query()
    first.close()  # Must not wait for the SDK lock or its non-daemon thread.
    assert time.monotonic() - start < 0.4
    assert multiprocessing.connection.wait([first_remote._process.sentinel], timeout=3)
    second, second_remote = _context()
    # Allow the first pandas import in a fresh SDK process on slower CI runners.
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 5.0)
    try:
        ret, frame = second.get_market_snapshot(["US.MSFT"])
        assert ret == 0
        assert frame.to_dict("records") == [{"code": "US.MSFT", "last_price": 100.0}]
        with pytest.raises(ConnectionError, match="transport unavailable") as error:
            second.connection_error()
        assert gateway.classify_opend_error(error.value) is gateway.OpenDRetCode.TRANSIENT
    finally:
        second.close()
    assert multiprocessing.connection.wait([second_remote._process.sentinel], timeout=3)


def test_remote_missing_method_preserves_structured_capability_error():
    proxy, _remote = _context()
    backend = SimpleNamespace(_ensure_quote_client=lambda: proxy)
    client = gateway._FutuAPIClient(backend, is_option_chain_cache_enabled=False)
    try:
        assert not hasattr(proxy, "get_earnings_calendar")
        with pytest.raises(gateway.FutuGatewayCapabilityUnavailableError) as caught:
            client.get_earnings_calendar(market="US", begin_date="2026-11-12", end_date="2026-11-12")
        assert caught.value.code == "CAPABILITY_UNAVAILABLE"
        assert caught.value.capability == "get_earnings_calendar"
        assert caught.value.reason_code == "opend_earnings_calendar_unsupported"
    finally:
        proxy.close()


def test_existing_retry_can_replace_timed_out_context_and_succeed(monkeypatch):
    attempts = []
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 0.1)

    def query():
        proxy, _remote = _context()
        attempts.append(proxy)
        try:
            return proxy.blocked_query() if len(attempts) == 1 else proxy.get_sync_conn_id()
        finally:
            proxy.close()

    assert gateway.retry_futu_gateway_call(
        "snapshot", query, retry_max_attempts=2, retry_base_delay_sec=0,
        retry_max_delay_sec=0, quiet=True,
    ) == 1
    assert len(attempts) == 2


def test_abandoned_sdk_process_does_not_delay_normal_cli_exit():
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(repo / "tests"), str(repo)))
    env.pop("OM_TICK_HANG_TRACEBACK_PATH", None)
    script = """
from test_futu_query_process_timeout import _context
from src.infrastructure import futu_gateway as g
proxy, remote = _context()
g.FUTU_QUERY_TIMEOUT_SEC = 0.1
try:
    proxy.blocked_query()
except g.FutuGatewayTransientError:
    pass
proxy.close()
print('tick completed', flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=repo, env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "tick completed" in completed.stdout
