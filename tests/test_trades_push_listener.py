from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace

import pytest

from src.application.trades.push_listener import OpenDTradePushListener


@pytest.fixture(autouse=True)
def _open_port(monkeypatch):
    """Tests mock the futu SDK; keep the port pre-check passing."""

    from src.application.trades import push_listener as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: True)
    yield


def test_trade_push_listener_isolates_callback_exception(monkeypatch) -> None:
    class _FakeData:
        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return [{"deal_id": "bad"}, {"deal_id": "good"}]

    class _FakeHandlerBase:
        def on_recv_rsp(self, _rsp_pb):
            return 0, _FakeData()

    class _FakeContext:
        def __init__(self, **_kwargs):
            self.handler = None

        def set_handler(self, handler):
            self.handler = handler

        def start(self):
            return None

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    seen: list[str] = []

    def _callback(row: dict) -> None:
        seen.append(str(row["deal_id"]))
        if row["deal_id"] == "bad":
            raise RuntimeError("boom")

    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=_callback)
    _ctx, handler = listener._build_default_context()

    ret, _data = handler.on_recv_rsp(None)

    assert ret == 0
    assert seen == ["bad", "good"]


def test_trade_push_listener_health_uses_existing_trade_context(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FakeContext:
        instances = 0

        def __init__(self, **_kwargs):
            type(self).instances += 1

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            return 0, {"program_status_type": "READY", "trd_logined": True, "qot_logined": False}

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    listener.start()
    listener.check_health()

    assert _FakeContext.instances == 1


def test_trade_push_listener_health_raises_terminal_phone_verification(monkeypatch) -> None:
    from src.application.trades.push_listener import TradeIntakeAuthRequired

    class _FakeHandlerBase:
        pass

    class _FakeContext:
        def __init__(self, **_kwargs):
            return None

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            return -1, "需要手机验证码"

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)
    listener.start()

    with pytest.raises(TradeIntakeAuthRequired) as _caught:
        listener.check_health()
    exc = _caught.value
    assert exc.error_code == "OPEND_NEEDS_PHONE_VERIFY"
    assert "需要手机验证码" in exc.detail


def test_trade_push_listener_health_keeps_disconnect_retryable(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FakeContext:
        def __init__(self, **_kwargs):
            return None

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            raise ConnectionResetError("connection reset")

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)
    listener.start()

    with pytest.raises(RuntimeError) as _caught:
        listener.check_health()
    exc = _caught.value
    assert "OPEND_API_ERROR" in str(exc)


def test_trade_push_listener_detects_auth_while_constructor_blocks(monkeypatch) -> None:
    from src.application.trades.push_listener import TradeIntakeAuthRequired

    release_constructor = threading.Event()

    class _FakeHandlerBase:
        pass

    class _BlockingContext:
        def __init__(self, **_kwargs):
            logging.getLogger("FTConsoleLog").warning(
                "[open_context_base.py:407] _init_connect_sync: init connect fail: "
                "msg=需要手机验证码 context=<futu.trade.open_trade_context.OpenSecTradeContext object>"
            )
            release_constructor.wait(5)

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_BlockingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    try:
        with pytest.raises(TradeIntakeAuthRequired) as caught:
            listener.start()
        assert caught.value.error_code == "OPEND_NEEDS_PHONE_VERIFY"
    finally:
        release_constructor.set()

    assert list(sdk_logger.handlers) == handlers_before


def test_trade_push_listener_cancels_blocked_constructor_and_removes_handler(monkeypatch) -> None:
    from src.application.trades.push_listener import TradeIntakeStartCancelled

    release_constructor = threading.Event()

    class _FakeHandlerBase:
        pass

    class _BlockingContext:
        def __init__(self, **_kwargs):
            release_constructor.wait(5)

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_BlockingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    cancel_event = threading.Event()
    cancel_event.set()
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    try:
        listener.start(cancel_event=cancel_event)
    except TradeIntakeStartCancelled:
        pass
    else:
        raise AssertionError("expected cancelled construction")
    finally:
        release_constructor.set()

    assert list(sdk_logger.handlers) == handlers_before


def test_trade_push_listener_constructor_error_removes_handler(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FailingContext:
        def __init__(self, **_kwargs):
            raise ConnectionRefusedError("refused")

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FailingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    with pytest.raises(RuntimeError) as _caught:
        listener.start()
    exc = _caught.value
    assert "failed to initialize" in str(exc)

    assert list(sdk_logger.handlers) == handlers_before


def test_listener_raises_typed_unreachable_when_port_closed(monkeypatch) -> None:
    from src.application.trades import push_listener as mod
    from src.infrastructure.futu_gateway import FutuGatewayUnreachableError

    monkeypatch.setattr(mod, "port_open", lambda host, port: False)
    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=object, TradeDealHandlerBase=object),
    )

    listener = OpenDTradePushListener(host="127.0.0.9", port=11119, on_deal=lambda payload: None)
    with pytest.raises(FutuGatewayUnreachableError):
        listener._build_default_context()
