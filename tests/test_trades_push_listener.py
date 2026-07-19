from __future__ import annotations

import sys
from types import SimpleNamespace

from src.application.trades.push_listener import OpenDTradePushListener


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

    try:
        listener.check_health()
    except TradeIntakeAuthRequired as exc:
        assert exc.error_code == "OPEND_NEEDS_PHONE_VERIFY"
        assert "需要手机验证码" in exc.detail
    else:
        raise AssertionError("expected TradeIntakeAuthRequired")


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

    try:
        listener.check_health()
    except RuntimeError as exc:
        assert "OPEND_API_ERROR" in str(exc)
    else:
        raise AssertionError("expected retryable RuntimeError")
