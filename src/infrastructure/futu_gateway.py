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

from src.infrastructure.opend_retcodes import OpenDRetCode, classify_opend_error


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

    @staticmethod
    def _rows(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                rows = to_dict(orient="records")
            except TypeError:
                rows = to_dict()
            if isinstance(rows, list):
                return [
                    dict(item)
                    for item in rows
                    if isinstance(item, dict)
                ]
            if isinstance(rows, dict):
                return [dict(rows)]
        if isinstance(value, list):
            return [
                dict(item)
                for item in value
                if isinstance(item, dict)
            ]
        if isinstance(value, tuple):
            return [
                dict(item)
                for item in value
                if isinstance(item, dict)
            ]
        if isinstance(value, dict):
            return [dict(value)]
        return []

    def _query_with_coverage(
        self,
        method: Any,
        *,
        paginated: bool,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Return rows plus explicit coverage evidence for settlement checks."""

        try:
            import futu
            ret_ok = futu.RET_OK
        except Exception:
            ret_ok = 0
        query = {
            key: value
            for key, value in dict(kwargs or {}).items()
            if value is not None
        }
        rows: list[dict[str, Any]] = []
        pages = 0
        page_req_key = query.pop("page_req_key", None)
        while True:
            call_kwargs = dict(query)
            if page_req_key not in (None, ""):
                call_kwargs["page_req_key"] = page_req_key
            result = method(**call_kwargs)
            pages += 1
            if isinstance(result, tuple) and len(result) >= 2:
                ret, data = result[0], result[1]
                if ret not in (ret_ok, 0, None):
                    raise RuntimeError(data)
                rows.extend(self._rows(data))
                next_key = result[2] if len(result) >= 3 else None
            else:
                rows.extend(self._rows(result))
                next_key = None
            if not paginated or next_key in (None, ""):
                break
            if pages >= 100:
                raise RuntimeError(
                    "Futu query pagination exceeded 100 pages"
                )
            page_req_key = next_key
        return {
            "retcode": 0,
            "rows": rows,
            "coverage_complete": True,
            "pagination_complete": True,
            "page_count": pages,
        }

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

    def get_order_list(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "order_list_query"):
            return self._unwrap(trade.order_list_query(**kwargs))
        raise AttributeError("order_list_query unavailable")

    def get_deal_list(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "deal_list_query"):
            return self._unwrap(trade.deal_list_query(**kwargs))
        raise AttributeError("deal_list_query unavailable")

    def get_history_orders(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "history_order_list_query"):
            return self._query_with_coverage(
                trade.history_order_list_query,
                paginated=True,
                kwargs=kwargs,
            )
        raise AttributeError("history_order_list_query unavailable")

    def get_history_deals(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "history_deal_list_query"):
            return self._query_with_coverage(
                trade.history_deal_list_query,
                paginated=True,
                kwargs=kwargs,
            )
        raise AttributeError("history_deal_list_query unavailable")

    def get_account_cash_flows(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "cash_flow_query"):
            return self._query_with_coverage(
                trade.cash_flow_query,
                paginated=False,
                kwargs=kwargs,
            )
        raise AttributeError("cash_flow_query unavailable")

    def get_trading_days(self, **kwargs: Any) -> Any:
        return self._unwrap(self._quote().request_trading_days(**kwargs))

    def get_positions_with_receipt(self, **kwargs: Any) -> Any:
        return self._query_with_coverage(
            self._trade().position_list_query,
            paginated=False,
            kwargs=kwargs,
        )

    def get_trading_days_with_receipt(self, **kwargs: Any) -> Any:
        return self._query_with_coverage(
            self._quote().request_trading_days,
            paginated=False,
            kwargs=kwargs,
        )

    def get_financials_earnings_price_history(self, **kwargs: Any) -> Any:
        quote = self._quote()
        if hasattr(quote, "get_financials_earnings_price_history"):
            return self._unwrap(quote.get_financials_earnings_price_history(**kwargs))
        raise AttributeError("get_financials_earnings_price_history unavailable; upgrade futu-api")

    def get_corporate_actions_dividends(self, **kwargs: Any) -> Any:
        quote = self._quote()
        if hasattr(quote, "get_corporate_actions_dividends"):
            return self._unwrap(quote.get_corporate_actions_dividends(**kwargs))
        raise AttributeError("get_corporate_actions_dividends unavailable; upgrade futu-api")

    def get_corporate_actions_stock_splits(self, **kwargs: Any) -> Any:
        quote = self._quote()
        if hasattr(quote, "get_corporate_actions_stock_splits"):
            return self._unwrap(quote.get_corporate_actions_stock_splits(**kwargs))
        raise AttributeError("get_corporate_actions_stock_splits unavailable; upgrade futu-api")


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


def _map_error(exc: Exception, *, action: str) -> FutuGatewayError:
    msg = str(exc or "")
    code = classify_opend_error(exc)

    if code is OpenDRetCode.NEED_2FA:
        return FutuGatewayNeed2FAError(f"{action} failed: {msg}", raw_error=exc)

    if code is OpenDRetCode.AUTH_EXPIRED:
        return FutuGatewayAuthExpiredError(f"{action} failed: {msg}", raw_error=exc)

    if code is OpenDRetCode.RATE_LIMIT:
        return FutuGatewayRateLimitError(f"{action} failed: {msg}", raw_error=exc)

    if code is OpenDRetCode.TRANSIENT:
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
        except TypeError as exc:
            if "is_force_refresh" not in str(exc):
                self._raise_mapped(exc, action="get_option_chain")
            try:
                return self.client.get_option_chain(**kwargs)
            except Exception as exc2:
                self._raise_mapped(exc2, action="get_option_chain")
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

    def get_order_list(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_order_list(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_order_list")
        raise AssertionError("unreachable")

    def get_deal_list(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_deal_list(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_deal_list")
        raise AssertionError("unreachable")

    def get_history_orders(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_history_orders(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_history_orders")
        raise AssertionError("unreachable")

    def get_history_deals(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_history_deals(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_history_deals")
        raise AssertionError("unreachable")

    def get_account_cash_flows(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_account_cash_flows(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_account_cash_flows")
        raise AssertionError("unreachable")

    def get_trading_days(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_trading_days(**kwargs)
        except Exception as exc:
            self._raise_mapped(exc, action="get_trading_days")
        raise AssertionError("unreachable")

    def get_positions_with_receipt(self, **kwargs: Any) -> Any:
        try:
            return self.client.get_positions_with_receipt(**kwargs)
        except Exception as exc:
            self._raise_mapped(
                exc,
                action="get_positions_with_receipt",
            )
        raise AssertionError("unreachable")

    def get_trading_days_with_receipt(
        self,
        **kwargs: Any,
    ) -> Any:
        try:
            return self.client.get_trading_days_with_receipt(
                **kwargs
            )
        except Exception as exc:
            self._raise_mapped(
                exc,
                action="get_trading_days_with_receipt",
            )
        raise AssertionError("unreachable")

    def request_history_kline(self, **kwargs: Any) -> dict[str, Any]:
        quote = self._quote_client()
        try:
            params = _normalize_history_kline_kwargs(kwargs)
            result = quote.request_history_kline(**params)
            if isinstance(result, tuple) and len(result) >= 2:
                ret, data = result[0], result[1]
                if ret != 0:
                    raise RuntimeError(data)
                return {
                    "data": data,
                    "page_req_key": result[2] if len(result) >= 3 else None,
                }
            return {"data": result, "page_req_key": None}
        except Exception as exc:
            self._raise_mapped(exc, action="request_history_kline")
        raise AssertionError("unreachable")

    def get_financials_earnings_price_history(self, code: str) -> Any:
        try:
            return self.client.get_financials_earnings_price_history(code=code)
        except Exception as exc:
            self._raise_mapped(exc, action="get_financials_earnings_price_history")
        raise AssertionError("unreachable")

    def get_corporate_actions_dividends(self, code: str) -> Any:
        try:
            return self.client.get_corporate_actions_dividends(code=code)
        except Exception as exc:
            self._raise_mapped(exc, action="get_corporate_actions_dividends")
        raise AssertionError("unreachable")

    def get_corporate_actions_stock_splits(
        self,
        code: str,
        *,
        next_key: str | None = None,
        num: int | None = None,
    ) -> Any:
        try:
            return self.client.get_corporate_actions_stock_splits(code=code, next_key=next_key, num=num)
        except Exception as exc:
            self._raise_mapped(exc, action="get_corporate_actions_stock_splits")
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


def _normalize_history_kline_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    params = dict(kwargs)
    params["ktype"] = _futu_enum_value("KLType", params.get("ktype"))
    params["autype"] = _futu_enum_value("AuType", params.get("autype"))
    fields = params.get("fields")
    if isinstance(fields, (list, tuple)):
        normalized_fields = [_futu_enum_value("KL_FIELD", item) for item in fields if item not in (None, "")]
        if normalized_fields:
            params["fields"] = normalized_fields
        else:
            params.pop("fields", None)
    elif fields in (None, "", ()):
        params.pop("fields", None)
    return {key: value for key, value in params.items() if value is not None}


def _futu_enum_value(namespace: str, value: Any) -> Any:
    if value in (None, ""):
        return value
    try:
        import futu

        enum_ns = getattr(futu, namespace)
        name = str(value).strip()
        if hasattr(enum_ns, name):
            return getattr(enum_ns, name)
        upper = name.upper()
        if hasattr(enum_ns, upper):
            return getattr(enum_ns, upper)
    except Exception:
        pass
    return value


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
