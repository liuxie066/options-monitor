from __future__ import annotations

from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool, is_gateway_connection_error


class _Gateway:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.close_calls = 0

    def is_connected(self) -> bool:
        return bool(self.connected)

    def close(self) -> None:
        self.close_calls += 1


def test_gateway_pool_reuses_per_endpoint_and_closes(monkeypatch) -> None:
    built: list[_Gateway] = []

    def _build(**_kwargs):
        gateway = _Gateway()
        built.append(gateway)
        return gateway

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", _build)
    pool = ThreadLocalFutuGatewayPool()

    first = pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)
    second = pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)
    other = pool.get_gateway(host="127.0.0.1", port=22222, chain_cache=True)

    assert first is second
    assert other is not first
    assert len(built) == 2

    pool.close_current_thread()
    assert all(gateway.close_calls >= 1 for gateway in built)


def test_gateway_pool_replaces_disconnected_gateway(monkeypatch) -> None:
    built = [_Gateway(connected=False), _Gateway(connected=True)]

    def _build(**_kwargs):
        return built.pop(0)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", _build)
    pool = ThreadLocalFutuGatewayPool()

    first = pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)
    second = pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)

    assert first is not second
    assert first.close_calls == 1
    assert second.is_connected()


def test_gateway_pool_closes_after_repeated_connection_failures(monkeypatch) -> None:
    built: list[_Gateway] = []

    def _build(**_kwargs):
        gateway = _Gateway()
        built.append(gateway)
        return gateway

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", _build)
    pool = ThreadLocalFutuGatewayPool()
    pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)

    pool.mark_failure(RuntimeError("connection reset by peer"))
    assert built[0].close_calls == 0
    pool.mark_failure(RuntimeError("connection reset by peer"))
    assert built[0].close_calls == 1
    assert is_gateway_connection_error(RuntimeError("ret_error: timeout"))
