from __future__ import annotations

import sys
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
