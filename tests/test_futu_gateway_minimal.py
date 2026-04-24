"""Minimal tests for futu_gateway adapter (no futu/OpenD dependency)."""

from __future__ import annotations

from pathlib import Path


def test_build_gateway_with_mock_backend_and_snapshot_call() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.futu_gateway import build_futu_gateway

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_snapshot(self, **kwargs):
            return {"backend_host": self.backend.host, "codes": kwargs.get("code_list") or []}

    gw = build_futu_gateway(
        host="127.0.0.9",
        port=11119,
        is_option_chain_cache_enabled=True,
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    data = gw.get_snapshot(["US.NVDA", "US.TSLA"])

    assert gw.host == "127.0.0.9"
    assert gw.port == 11119
    assert data["backend_host"] == "127.0.0.9"
    assert data["codes"] == ["US.NVDA", "US.TSLA"]


def test_gateway_error_mapping_need_2fa() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.futu_gateway import build_futu_gateway, FutuGatewayNeed2FAError

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_snapshot(self, **kwargs):
            raise RuntimeError("phone verification required")

    gw = build_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    try:
        _ = gw.get_snapshot(["US.AAPL"])
    except FutuGatewayNeed2FAError:
        pass
    else:
        raise AssertionError("expected FutuGatewayNeed2FAError")

def test_build_ready_gateway_ensures_quote_ready() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.futu_gateway import build_ready_futu_gateway

    class FakeQuote:
        def __init__(self) -> None:
            self.ready_calls = 0

        def get_global_state(self):
            self.ready_calls += 1
            return 0, {"program_status_type": "READY", "qot_logined": True}

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.quote = FakeQuote()

        def _ensure_clients(self):
            return self.quote, None

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

    gw = build_ready_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    assert gw.host == "127.0.0.1"
    assert gw.port == 11111
    assert gw.backend.quote.ready_calls == 1


def test_retry_futu_gateway_call_retries_transient_once(monkeypatch) -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.futu_gateway import FutuGatewayTransientError, retry_futu_gateway_call

    calls = {"count": 0}
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("random.uniform", lambda _a, _b: 0.0)

    def _fn():
        calls["count"] += 1
        if calls["count"] == 1:
            raise FutuGatewayTransientError("temporary")
        return "ok"

    out = retry_futu_gateway_call("test_call", _fn, retry_max_attempts=2)

    assert out == "ok"
    assert calls["count"] == 2
