from __future__ import annotations

import threading
from typing import Any


def is_gateway_connection_error(exc: Exception) -> bool:
    text = str(exc or "")
    low = text.lower()
    if "ret_error" in low:
        return True
    keys = ("disconnected", "connection", "broken pipe", "connection reset", "timeout", "temporarily unavailable")
    return any(key in low for key in keys)


class ThreadLocalFutuGatewayPool:
    """Reuse one Futu gateway per endpoint in each worker thread."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._registry: list[Any] = []

    @staticmethod
    def _key(host: str, port: int, chain_cache: bool) -> tuple[str, int, bool]:
        return (str(host), int(port), bool(chain_cache))

    def _gateways(self) -> dict[tuple[str, int, bool], Any]:
        gateways = getattr(self._local, "gateways", None)
        if not isinstance(gateways, dict):
            gateways = {}
            self._local.gateways = gateways
        return gateways

    def get_gateway(self, *, host: str, port: int, chain_cache: bool) -> Any:
        key = self._key(host, port, chain_cache)
        gateways = self._gateways()
        gateway = gateways.get(key)
        if gateway is not None:
            try:
                checker = getattr(gateway, "is_connected", None)
                if checker is None or checker():
                    return gateway
            except Exception:
                pass
            try:
                gateway.close()
            except Exception:
                pass
            gateways.pop(key, None)

        from src.infrastructure import futu_gateway

        gateway = futu_gateway.build_ready_futu_gateway(
            host=str(host),
            port=int(port),
            is_option_chain_cache_enabled=bool(chain_cache),
        )
        gateways[key] = gateway
        with self._registry_lock:
            self._registry.append(gateway)
        return gateway

    def close_current_thread(self) -> None:
        gateways = getattr(self._local, "gateways", None)
        if isinstance(gateways, dict):
            values = list(gateways.values())
            gateways.clear()
        else:
            values = []
        seen: set[int] = set()
        for gateway in values:
            if id(gateway) in seen:
                continue
            seen.add(id(gateway))
            try:
                gateway.close()
            except Exception:
                pass
        self.mark_success()

    def close_registered(self) -> None:
        with self._registry_lock:
            gateways = list(self._registry)
            self._registry.clear()
        for gateway in gateways:
            try:
                gateway.close()
            except Exception:
                pass

    def mark_failure(self, exc: Exception, *, close_after: int = 2) -> None:
        count = int(getattr(self._local, "failure_count", 0) or 0)
        if is_gateway_connection_error(exc):
            count += 1
        else:
            count = 0
        self._local.failure_count = count
        if count >= int(close_after):
            self.close_current_thread()

    def mark_success(self) -> None:
        self._local.failure_count = 0

