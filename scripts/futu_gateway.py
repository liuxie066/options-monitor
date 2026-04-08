#!/usr/bin/env python3
from __future__ import annotations

"""Small gateway for futu-core OpenD integration.

Centralizes:
- OpenDBackend + FutuCoreClient creation
- Backward-compatible host/port defaults
- Explicit fail-fast error classification (2FA/auth expired/rate limit)
"""

from dataclasses import dataclass
import logging
from pathlib import Path
import sys
from typing import Any, Iterable


LOG = logging.getLogger(__name__)


def _ensure_futu_core_importable() -> None:
    """Support local sibling `../futu-core/src` during dev without install."""
    try:
        import futu_core  # noqa: F401
        return
    except Exception:
        pass

    repo_base = Path(__file__).resolve().parents[1]
    local_src = (repo_base.parent / "futu-core" / "src").resolve()
    if local_src.exists():
        p = str(local_src)
        if p not in sys.path:
            sys.path.insert(0, p)


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

    try:
        _ensure_futu_core_importable()
        from futu_core.errors import FutuCoreError, ErrorCode

        if isinstance(exc, FutuCoreError):
            if exc.code == ErrorCode.NEED_2FA:
                return FutuGatewayNeed2FAError(f"{action} failed: {msg}", raw_error=exc)
            if exc.code == ErrorCode.AUTH_EXPIRED:
                return FutuGatewayAuthExpiredError(f"{action} failed: {msg}", raw_error=exc)
            if exc.code == ErrorCode.RATE_LIMIT:
                return FutuGatewayRateLimitError(f"{action} failed: {msg}", raw_error=exc)
            if exc.code == ErrorCode.TRANSIENT:
                return FutuGatewayTransientError(f"{action} failed: {msg}", raw_error=exc)
            return FutuGatewayError(f"{action} failed: {msg}", raw_error=exc)
    except Exception:
        pass

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
    """Thin wrapper over futu-core client with explicit error semantics."""

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
    _ensure_futu_core_importable()
    if backend_cls is None or client_cls is None:
        from futu_core.backends import OpenDBackend
        from futu_core.client import FutuCoreClient

        backend_cls = backend_cls or OpenDBackend
        client_cls = client_cls or FutuCoreClient

    backend = backend_cls(host=str(host), port=int(port))
    client = client_cls(backend, is_option_chain_cache_enabled=bool(is_option_chain_cache_enabled))
    return FutuGateway(client=client, backend=backend, host=str(host), port=int(port))
