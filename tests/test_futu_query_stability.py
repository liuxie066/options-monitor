from __future__ import annotations

import sys
import io
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from src.infrastructure import futu_gateway as gateway
from src.infrastructure.opend_retcodes import OpenDRetCode, classify_opend_error


@pytest.fixture
def sdk(monkeypatch):
    contexts = []

    class Context:
        def __init__(self, *, host, port, is_async_connect=False):
            self.host, self.port = host, port
            self.closed = False
            self.connected = False
            self.timeout = None
            self.connect_attempts = 0
            contexts.append(self)
            if is_async_connect:
                self._wait_reconnect(0)
            else:
                assert self._init_connect_sync() == 0

        def _init_connect_sync(self):
            self.connect_attempts += 1
            self.connected = True
            return 0

        def _wait_reconnect(self, delay):
            # Complete the SDK's scheduled reconnect when readiness is polled.
            self.pending_connect = True

        def get_sync_conn_id(self):
            if getattr(self, "pending_connect", False):
                self.pending_connect = False
                self._init_connect_sync()
            return 1 if self.connected else 0

        def set_sync_query_connect_timeout(self, timeout):
            self.timeout = timeout

        def get_global_state(self):
            return 0, {"program_status_type": "READY", "qot_logined": True, "trd_logined": True}

        def get_acc_list(self):
            return 0, [{"acc_id": "123", "trd_env": "REAL"}]

        def close(self):
            self.closed = True

    class Trade(Context):
        # The real SDK trade constructor does not accept is_async_connect.
        def __init__(self, *, host, port):
            super().__init__(host=host, port=port)

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
        RET_OK=0, OpenQuoteContext=Context, OpenSecTradeContext=Trade,
        TradeDealHandlerBase=type("DealBase", (), {}),
    ))
    monkeypatch.setattr(gateway, "port_open", lambda *_args: True)

    def start(kind, kwargs, *, deadline=None, cancelled=None):
        return gateway._FutuQueryContext(
            gateway._create_futu_sdk_context(kind, kwargs), deadline=deadline, cancelled=cancelled,
        )

    monkeypatch.setattr(gateway, "_start_futu_query_context", start)
    return contexts, Context


def _build(capability):
    if capability == "trade_push":
        from src.infrastructure.futu_trade_push import OpenDTradePushListener

        listener = OpenDTradePushListener(host="test-opend", port=11119, on_deal=lambda _row: None)
        context, _handler = listener._build_default_context()
        return context
    if capability == "quote":
        return gateway.build_ready_futu_quote_gateway(host="test-opend", port=11119)
    return gateway.build_ready_futu_broker_gateway(
        host="test-opend", port=11119, expected_account_ids=["123"], trd_env="REAL",
    )


@pytest.mark.parametrize("capability", ["quote", "broker", "trade_push"])
def test_agent_json_keeps_sdk_connection_and_late_logs_on_stderr(sdk, monkeypatch, capsys, capability):
    from src.interfaces.agent import cli

    # Futu binds its console handler at import, before the tool starts.
    logger = logging.getLogger("FTConsoleLog")
    handler = logging.StreamHandler(sys.stdout)
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", False)
    _, context_type = sdk
    original = context_type._init_connect_sync

    def connect(self):
        logger.info("SDK connection ready")
        return original(self)

    monkeypatch.setattr(context_type, "_init_connect_sync", connect)
    expected = {"ok": True, "data": {"outcome": "unable", "reason_codes": ["market_closed"]}}

    def execute(*args):
        context = _build(capability)
        context.close()
        logger.info("SDK connection closed")
        return expected

    monkeypatch.setattr(cli, "execute_tool", execute)
    assert cli.main(["run", "--tool", "option_candidates"]) == 0
    # A timed-out SDK worker can finish after the JSON has already been emitted.
    worker = threading.Thread(target=logger.info, args=("SDK late disconnect",))
    worker.start()
    worker.join()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert all(text in captured.err for text in ("connection ready", "connection closed", "late disconnect"))


def test_sdk_console_routing_preserves_file_and_custom_handlers(sdk, monkeypatch, tmp_path, capsys):
    logger = logging.getLogger("FTConsoleLog")
    custom = io.StringIO()
    console = logging.StreamHandler(sys.stdout)
    custom_handler = logging.StreamHandler(custom)
    file_handler = logging.FileHandler(tmp_path / "sdk.log")
    handlers = [console, custom_handler, file_handler]
    monkeypatch.setattr(logger, "handlers", handlers)
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", False)
    try:
        context = _build("quote")
        context.close()
        logger.info("diagnostic retained")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "diagnostic retained" in captured.err
        assert custom.getvalue() == "diagnostic retained\n"
        assert (tmp_path / "sdk.log").read_text() == "diagnostic retained\n"
        assert logger.handlers == handlers
    finally:
        file_handler.close()


@pytest.mark.parametrize("capability", ["quote", "broker", "trade_push"])
def test_query_context_waits_for_sdk_connect_and_bounds_reconnect(sdk, capability):
    contexts, _ = sdk
    result = _build(capability)
    assert len(contexts) == 1
    context = contexts[0]
    assert context.timeout == (2 if capability == "trade_push" else gateway.FUTU_QUERY_CONNECT_TIMEOUT_SEC)
    assert context.connect_attempts == 1
    assert context.get_sync_conn_id() == 1
    # A later SDK reconnect must use the normal handshake, not reschedule itself.
    assert context._init_connect_sync() == 0
    assert context.connect_attempts == 2
    result.close()
    assert context.closed


@pytest.mark.parametrize("capability", ["quote", "broker", "trade_push"])
def test_query_context_closes_when_tcp_is_open_but_handshake_never_finishes(sdk, monkeypatch, capability):
    contexts, context_cls = sdk
    clock = [0.0]
    monkeypatch.setattr(context_cls, "_init_connect_sync", lambda _self: pytest.fail("synchronous constructor"))
    monkeypatch.setattr(context_cls, "get_sync_conn_id", lambda _self: 0)
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gateway.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(gateway.FutuGatewayTransientError, match="connect timeout"):
        _build(capability)
    assert clock[0] < gateway.FUTU_QUERY_CONNECT_TIMEOUT_SEC + 0.1
    assert len(contexts) == 1
    assert contexts[0].closed


@pytest.mark.parametrize("capability", ["quote", "broker", "trade_push"])
def test_query_context_closes_on_cancellation(sdk, monkeypatch, capability):
    contexts, context_cls = sdk
    monkeypatch.setattr(context_cls, "get_sync_conn_id", lambda _self: 0)

    def cancel(_delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(gateway.time, "sleep", cancel)
    with pytest.raises(KeyboardInterrupt):
        _build(capability)
    assert contexts[0].closed


@pytest.mark.parametrize("error", [
    TimeoutError("slow transport"), ConnectionResetError("peer vanished"),
    RuntimeError("Disconnect"), RuntimeError("Connection closed"),
    RuntimeError("operation timed out"),
])
def test_sdk_transport_failures_are_retryable(error):
    assert classify_opend_error(error) is OpenDRetCode.TRANSIENT


@pytest.mark.parametrize("error_cls", [gateway.FutuGatewayTransientError, gateway.FutuGatewayRateLimitError])
def test_retry_exhaustion_preserves_provider_error_identity(monkeypatch, error_cls):
    error = error_cls("provider unavailable")
    attempts = []
    monkeypatch.setattr(gateway.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(gateway.random, "uniform", lambda *_args: 0.0)

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(error_cls) as caught:
        gateway.retry_futu_gateway_call("snapshot", call, retry_max_attempts=2, quiet=True)
    assert caught.value is error
    assert len(attempts) == 2


def test_retry_budget_includes_jitter(monkeypatch):
    error = gateway.FutuGatewayTransientError("temporary")
    attempts = []
    monkeypatch.setattr(gateway.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(gateway.random, "uniform", lambda *_args: 0.2)
    monkeypatch.setattr(gateway.time, "sleep", lambda _delay: pytest.fail("would exceed retry budget"))

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(gateway.FutuGatewayTransientError) as caught:
        gateway.retry_futu_gateway_call("snapshot", call, retry_time_budget_sec=1.0,
                                        retry_base_delay_sec=0.9, quiet=True)
    assert caught.value is error
    assert len(attempts) == 1


def test_retry_does_not_start_after_scheduler_oversleep(monkeypatch):
    error = gateway.FutuGatewayTransientError("temporary")
    clock = [0.0]
    attempts = []
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gateway.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(gateway.time, "sleep", lambda _delay: clock.__setitem__(0, 1.1))

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(gateway.FutuGatewayTransientError):
        gateway.retry_futu_gateway_call("snapshot", call, retry_time_budget_sec=1.0, quiet=True)
    assert len(attempts) == 1


def test_retry_recovers_transport_error_and_accepts_valid_empty_result(monkeypatch):
    attempts = []
    monkeypatch.setattr(gateway.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(gateway.random, "uniform", lambda *_args: 0.0)

    def call():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("slow transport")
        return []

    assert gateway.retry_futu_gateway_call("positions", call, quiet=True) == []
    assert len(attempts) == 2


@pytest.mark.parametrize("capability,method", [("quote", "get_market_snapshot"), ("broker", "position_list_query")])
def test_blocked_query_discards_context_without_waiting_for_close(sdk, monkeypatch, capability, method):
    import time

    contexts, context_cls = sdk
    release = threading.Event()
    closing = threading.Event()
    calls = []

    def query(_self, symbol):
        calls.append(symbol)
        if symbol == "US.AAPL":
            release.wait(timeout=2.0)  # No SDK response, including no internal timeout.
        return 0, [{"code": symbol}]

    def close(self):
        closing.set()
        release.wait(timeout=2.0)  # Model SDK close waiting on the query's context lock.
        self.closed = True

    monkeypatch.setattr(context_cls, method, query, raising=False)
    monkeypatch.setattr(context_cls, "close", close)
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 0.05)
    first = _build(capability)
    first_context = first.backend._quote_client if capability == "quote" else first.backend._trade_client
    started = time.monotonic()
    try:
        with pytest.raises(gateway.FutuGatewayTransientError, match="query timeout"):
            gateway.retry_futu_gateway_call(
                method, lambda: getattr(first_context, method)("US.AAPL"),
                retry_time_budget_sec=0.01, quiet=True,
            )
        first.close()
        assert closing.wait(0.5)
        assert time.monotonic() - started < 1.0
        with pytest.raises(gateway.FutuGatewayTransientError, match="context discarded"):
            getattr(first_context, method)("US.AAPL")
        assert calls == ["US.AAPL"]

        # A separate symbol's context can complete while the first is still stuck.
        second = _build(capability)
        second_context = second.backend._quote_client if capability == "quote" else second.backend._trade_client
        assert getattr(second_context, method)("US.MSFT") == (0, [{"code": "US.MSFT"}])
        assert calls == ["US.AAPL", "US.MSFT"]
    finally:
        release.set()
        first.close()
        if len(contexts) > 1:
            second.close()


def test_inflight_query_obeys_earlier_fetch_deadline_and_cleanup_still_runs(sdk, monkeypatch):
    import time

    contexts, context_cls = sdk
    release = threading.Event()
    closed = threading.Event()

    def close(self):
        self.closed = True
        closed.set()

    monkeypatch.setattr(context_cls, "close", close)
    monkeypatch.setattr(context_cls, "get_market_snapshot", lambda _self: release.wait(timeout=2.0), raising=False)
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 15.0)
    context = gateway.build_futu_quote_context(
        host="test-opend", port=11119, deadline=time.monotonic() + 0.1,
    )
    try:
        with pytest.raises(TimeoutError, match="deadline reached"):
            context.get_market_snapshot()
        context.close()
        # Cleanup is independent of the expired deadline.
        assert closed.wait(0.5)
        assert contexts[0].closed
    finally:
        release.set()
        context.close()


@pytest.mark.parametrize("control", ["deadline", "cancelled"])
def test_warm_gateway_uses_current_readiness_controls(sdk, monkeypatch, control):
    import time

    _, context_cls = sdk
    ready = _build("quote")  # Created without a tool deadline or cancellation.
    release = threading.Event()
    cancelled = threading.Event()
    timer = threading.Timer(0.05, cancelled.set)
    monkeypatch.setattr(context_cls, "get_global_state", lambda _self: release.wait(timeout=2.0))
    kwargs = {"deadline": time.monotonic() + 0.05} if control == "deadline" else {"cancelled": cancelled.is_set}
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises((TimeoutError, gateway.FutuGatewayTransientError), match="deadline|cancelled"):
            ready.ensure_quote_ready(**kwargs)
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        timer.cancel()
        ready.close()


def test_retry_controls_bound_a_query_on_a_reused_context(sdk, monkeypatch):
    import time

    _, context_cls = sdk
    ready = _build("quote")
    release = threading.Event()
    monkeypatch.setattr(context_cls, "get_market_snapshot", lambda _self: release.wait(timeout=2.0), raising=False)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline reached"):
            gateway.retry_futu_gateway_call(
                "snapshot", ready.backend._quote_client.get_market_snapshot,
                deadline=time.monotonic() + 0.05, quiet=True,
            )
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        ready.close()


def test_hung_close_is_bounded_and_is_not_started_twice(sdk, monkeypatch):
    contexts, context_cls = sdk
    release = threading.Event()
    calls = []

    def close(_self):
        calls.append(True)
        release.wait(timeout=2.0)

    monkeypatch.setattr(context_cls, "close", close)
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 0.05)
    context = _build("quote")
    try:
        context.close()
        context.close()
        assert calls == [True]
    finally:
        release.set()


def test_missing_timeout_sdk_method_is_not_a_transport_failure():
    assert classify_opend_error(AttributeError("no set_sync_query_connect_timeout")) is OpenDRetCode.UNKNOWN
    assert classify_opend_error(TypeError("invalid timeout argument")) is OpenDRetCode.UNKNOWN
    assert classify_opend_error(ValueError("invalid timeout value")) is OpenDRetCode.UNKNOWN


@pytest.mark.parametrize("capability", ["quote", "broker"])
def test_ready_builder_closes_when_readiness_query_is_cancelled(sdk, monkeypatch, capability):
    contexts, context_cls = sdk

    def cancel(_self):
        raise KeyboardInterrupt

    monkeypatch.setattr(context_cls, "get_global_state", cancel)
    with pytest.raises(KeyboardInterrupt):
        _build(capability)
    assert contexts[0].closed


def test_gateway_pool_uses_transport_classification_after_retry_exhaustion(sdk):
    from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool, is_gateway_connection_error

    contexts, _ = sdk
    pool = ThreadLocalFutuGatewayPool()
    pool.get_gateway(host="test-opend", port=11119, chain_cache=True)
    error = gateway.FutuGatewayTransientError("provider unavailable")
    pool.mark_failure(error)
    assert not contexts[0].closed
    pool.mark_failure(error)
    assert contexts[0].closed
    pool.close_registered()
    assert not is_gateway_connection_error(gateway.FutuGatewayRateLimitError("connection timeout"))
    assert not is_gateway_connection_error(gateway.FutuGatewayAuthExpiredError("disconnected login"))


def test_watchdog_keeps_port_closed_reason_and_bounded_retry(sdk, monkeypatch):
    from src.infrastructure import opend_watchdog

    contexts, _ = sdk
    monkeypatch.setattr(gateway, "port_open", lambda *_args: False)
    monkeypatch.setattr(gateway.time, "sleep", lambda _delay: None)
    state, error, action = opend_watchdog.get_global_state("test-opend", 11119)
    assert state is None
    assert opend_watchdog.classify_watchdog_result(state, error)[0] == "OPEND_PORT_CLOSED"
    assert action == "retry_once_failed"
    assert contexts == []
