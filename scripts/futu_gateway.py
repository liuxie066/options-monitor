#!/usr/bin/env python3
from __future__ import annotations

"""Small gateway for Futu OpenD integration.

Centralizes:
- futu-api OpenD client creation
- Backward-compatible host/port defaults
- Explicit fail-fast error classification (2FA/auth expired/rate limit)
"""

from dataclasses import dataclass
import logging
import random
import time
from typing import Any, Iterable


LOG = logging.getLogger(__name__)


def _ensure_futu_api_importable() -> None:
    try:
        import futu  # noqa: F401
        return
    except Exception as exc:
        raise ModuleNotFoundError("No module named 'futu'") from exc


class _FutuAPIBackend:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = str(host)
        self.port = int(port)
        self._quote_client = None
        self._trade_client = None

    def _ensure_clients(self) -> tuple[Any, Any]:
        if self._quote_client is None or self._trade_client is None:
            _ensure_futu_api_importable()
            import futu

            if self._quote_client is None:
                self._quote_client = futu.OpenQuoteContext(host=self.host, port=self.port)
            if self._trade_client is None:
                self._trade_client = futu.OpenSecTradeContext(host=self.host, port=self.port)
        return self._quote_client, self._trade_client


class _FutuAPIClient:
    def __init__(self, backend: Any, *, is_option_chain_cache_enabled: bool) -> None:
        self.backend = backend
        self.is_option_chain_cache_enabled = bool(is_option_chain_cache_enabled)

    def _quote(self) -> Any:
        quote, _trade = self.backend._ensure_clients()
        return quote

    def _trade(self) -> Any:
        _quote, trade = self.backend._ensure_clients()
        return trade

    @staticmethod
    def _unwrap(result: Any) -> Any:
        try:
            import futu
            ret_ok = futu.RET_OK
        except Exception:
            ret_ok = 0
        if isinstance(result, tuple) and len(result) >= 2:
            ret, data = result[0], result[1]
            if ret not in (ret_ok, 0, None):
                raise RuntimeError(data)
            return data
        return result

    def get_option_chain(self, **kwargs: Any) -> Any:
        return self._unwrap(self._quote().get_option_chain(**kwargs))

    def get_snapshot(self, **kwargs: Any) -> Any:
        return self._unwrap(self._quote().get_market_snapshot(**kwargs))

    def get_positions(self, **kwargs: Any) -> Any:
        return self._unwrap(self._trade().position_list_query(**kwargs))

    def get_account_balance(self, **kwargs: Any) -> Any:
        return self._unwrap(self._trade().accinfo_query(**kwargs))

    def get_funds(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "acctradinginfo_query"):
            return self._unwrap(trade.acctradinginfo_query(**kwargs))
        return self._unwrap(trade.accinfo_query(**kwargs))


class FutuGatewayError(RuntimeError):
    code = "UNKNOWN"

    def __init__(self, message: str, *, raw_error: Any | None = None) -> None:
        super().__init__(message)
        self.raw_error = raw_error


class FutuGatewayNeed2FAError(FutuGatewayError):
    code = "NEED_2FA"


class FutuGatewayAuthExpiredError(FutuGatewayError):
    code = "AUTH_EXPIRED"


class FutuGatewayRateLimitError(FutuGatewayError):
    code = "RATE_LIMIT"


class FutuGatewayTransientError(FutuGatewayError):
    code = "TRANSIENT"


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(h in text for h in hints)


def _map_error(exc: Exception, *, action: str) -> FutuGatewayError:
    msg = str(exc or "")
    low = msg.lower()

    if _contains_any(low, ("2fa", "phone verification", "verify code")) or _contains_any(msg, ("手机验证码", "短信验证", "手机验证", "验证码")):
        return FutuGatewayNeed2FAError(f"{action} failed: {msg}", raw_error=exc)

    if _contains_any(low, ("login expired", "auth expired", "token expired", "not logged", "not login")):
        return FutuGatewayAuthExpiredError(f"{action} failed: {msg}", raw_error=exc)

    if _contains_any(low, ("rate limit", "too frequent")) or _contains_any(msg, ("频率太高", "最多10次")):
        return FutuGatewayRateLimitError(f"{action} failed: {msg}", raw_error=exc)

    if _contains_any(low, ("timeout", "disconnected", "connection reset", "broken pipe", "temporarily unavailable")):
        return FutuGatewayTransientError(f"{action} failed: {msg}", raw_error=exc)

    return FutuGatewayError(f"{action} failed: {msg}", raw_error=exc)


@dataclass
class FutuGateway:
    """Thin wrapper over OpenD client with explicit error semantics."""

    client: Any
    backend: Any
    host: str
    port: int

    def _raise_mapped(self, exc: Exception, *, action: str) -> None:
        mapped = _map_error(exc, action=action)
        LOG.error("[futu_gateway] %s code=%s error=%s", action, getattr(mapped, "code", "UNKNOWN"), mapped)
        raise mapped

    def close(self) -> None:
        for c in (getattr(self.backend, "_quote_client", None), getattr(self.backend, "_trade_client", None)):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass

    def _quote_client(self) -> Any:
        try:
            quote, _ = self.backend._ensure_clients()
            return quote
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_clients")
        raise AssertionError("unreachable")

    def ensure_quote_ready(self) -> dict[str, Any]:
        quote = self._quote_client()
        try:
            ret, state = quote.get_global_state()
            if ret != 0:
                raise RuntimeError(f"OpenD get_global_state ret={ret}: {state}")
            if not isinstance(state, dict):
                raise RuntimeError(f"OpenD invalid global_state: {state}")
            if state.get("program_status_type") not in (None, "", "READY"):
                raise RuntimeError(f"OpenD not READY: {state}")
            if not state.get("qot_logined", True):
                raise RuntimeError(f"OpenD quote not logged in: {state}")
            return state
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_quote_ready")
        raise AssertionError("unreachable")

    def get_option_expiration_dates(self, code: str) -> Any:
        quote = self._quote_client()
        try:
            ret, data = quote.get_option_expiration_date(code)
            if ret != 0:
                raise RuntimeError(data)
            return data
        except Exception as exc:
            self._raise_mapped(exc, action="get_option_expiration_date")
        raise AssertionError("unreachable")

    def get_option_chain(self, *, is_force_refresh: bool = False, **kwargs: Any) -> Any:
        try:
            return self.client.get_option_chain(is_force_refresh=is_force_refresh, **kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_option_chain")
        raise AssertionError("unreachable")

    def get_snapshot(self, codes: Iterable[str]) -> Any:
        try:
            return self.client.get_snapshot(code_list=list(codes))
        except Exception as exc:
            self._raise_mapped(exc, action="get_snapshot")
        raise AssertionError("unreachable")

    def get_positions(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_positions(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_positions")
        raise AssertionError("unreachable")

    def get_account_balance(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_account_balance(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_account_balance")
        raise AssertionError("unreachable")

    def get_funds(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_funds(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_funds")
        raise AssertionError("unreachable")


def build_futu_gateway(
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    is_option_chain_cache_enabled: bool = True,
    backend_cls: Any | None = None,
    client_cls: Any | None = None,
) -> FutuGateway:
    backend_cls = backend_cls or _FutuAPIBackend
    client_cls = client_cls or _FutuAPIClient

    backend = backend_cls(host=str(host), port=int(port))
    client = client_cls(backend, is_option_chain_cache_enabled=bool(is_option_chain_cache_enabled))
    return FutuGateway(client=client, backend=backend, host=str(host), port=int(port))


def build_ready_futu_gateway(
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    is_option_chain_cache_enabled: bool = True,
    backend_cls: Any | None = None,
    client_cls: Any | None = None,
) -> FutuGateway:
    gateway = build_futu_gateway(
        host=host,
        port=port,
        is_option_chain_cache_enabled=is_option_chain_cache_enabled,
        backend_cls=backend_cls,
        client_cls=client_cls,
    )
    try:
        gateway.ensure_quote_ready()
        return gateway
    except Exception:
        gateway.close()
        raise


def retry_futu_gateway_call(
    what: str,
    fn: Any,
    *,
    no_retry: bool = False,
    retry_max_attempts: int = 4,
    retry_time_budget_sec: float = 8.0,
    retry_base_delay_sec: float = 0.8,
    retry_max_delay_sec: float = 6.0,
    quiet: bool = False,
) -> Any:
    if no_retry or int(retry_max_attempts) <= 1:
        return fn()

    t0 = time.monotonic()
    attempt = 0
    delay = float(retry_base_delay_sec or 0.5)
    max_delay = float(retry_max_delay_sec or 6.0)
    budget = float(retry_time_budget_sec or 0.0)
    last_err = None

    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            last_err = exc

        if attempt >= int(retry_max_attempts):
            raise RuntimeError(f"{what} failed after {attempt} attempts: {last_err}")

        if isinstance(last_err, FutuGatewayAuthExpiredError):
            raise RuntimeError(f"{what} failed (auth expired): {last_err}")
        if isinstance(last_err, FutuGatewayNeed2FAError):
            raise RuntimeError(f"{what} failed (non-transient): {last_err}")
        if (not isinstance(last_err, FutuGatewayTransientError)) and (not isinstance(last_err, FutuGatewayRateLimitError)):
            raise RuntimeError(f"{what} failed (non-transient): {last_err}")

        sleep_s = min(max_delay, max(0.0, delay))
        if isinstance(last_err, FutuGatewayRateLimitError):
            sleep_s = max(sleep_s, 2.0)

        if (budget > 0) and ((time.monotonic() - t0) + sleep_s > budget):
            raise RuntimeError(f"{what} failed (retry budget {budget}s exceeded): {last_err}")

        if not quiet:
            print(f"[WARN] {what} failed (attempt {attempt}/{retry_max_attempts}): {last_err}; sleep {sleep_s:.1f}s")

        time.sleep(sleep_s + random.uniform(0.0, 0.2))
        delay = min(max_delay, delay * 2.0)
