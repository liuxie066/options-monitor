#!/usr/bin/env python3
from __future__ import annotations

"""Small gateway for Futu OpenD integration.

Centralizes:
- futu-api OpenD client creation
- Backward-compatible host/port defaults
- Explicit fail-fast error classification (2FA/auth expired/rate limit)
"""

import ast
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
import logging
from numbers import Integral
from pathlib import Path
import random
import time
from typing import Any, Iterable

from src.infrastructure.opend_retcodes import OpenDRetCode, classify_opend_error


LOG = logging.getLogger(__name__)

FUTU_EARNINGS_CALENDAR_MIN_VERSION = "10.9.6908"
FUTU_EARNINGS_CALENDAR_CAPABILITY = "get_earnings_calendar"
FUTU_EARNINGS_CALENDAR_UNSUPPORTED_REASON = "opend_earnings_calendar_unsupported"


def _numeric_version(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for raw_part in str(value or "").strip().split("."):
        digits = ""
        for character in raw_part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_at_least(value: Any, minimum: str) -> bool:
    observed = _numeric_version(value)
    required = _numeric_version(minimum)
    if not observed or not required:
        return False
    width = max(len(observed), len(required))
    return observed + (0,) * (width - len(observed)) >= required + (0,) * (width - len(required))


def _futu_package_root() -> Path | None:
    try:
        spec = importlib_util.find_spec("futu")
    except Exception:
        return None
    locations = getattr(spec, "submodule_search_locations", None) if spec is not None else None
    if not locations:
        return None
    for raw_path in locations:
        path = Path(str(raw_path)).resolve()
        if path.is_dir():
            return path
    return None


def _open_quote_context_has_earnings_calendar(package_root: Path | None) -> bool:
    if package_root is None:
        return False
    source_path = Path(package_root) / "quote" / "open_quote_context.py"
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "OpenQuoteContext":
            continue
        return any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == FUTU_EARNINGS_CALENDAR_CAPABILITY
            for item in node.body
        )
    return False


def inspect_futu_sdk_earnings_calendar_capability(
    *,
    package_root: Path | None = None,
    installed_version: str | None = None,
) -> dict[str, Any]:
    """Inspect the installed SDK without importing it or creating Futu log files."""

    root = Path(package_root).resolve() if package_root is not None else _futu_package_root()
    version = str(installed_version or "").strip()
    if not version:
        try:
            version = importlib_metadata.version("futu-api")
        except importlib_metadata.PackageNotFoundError:
            version = ""
    installed = root is not None and bool(version)
    version_supported = installed and _version_at_least(
        version,
        FUTU_EARNINGS_CALENDAR_MIN_VERSION,
    )
    method_available = _open_quote_context_has_earnings_calendar(root)
    supported = bool(installed and version_supported and method_available)
    if not installed:
        reason_code = "futu_api_missing"
    elif not version_supported:
        reason_code = "futu_api_version_too_old"
    elif not method_available:
        reason_code = FUTU_EARNINGS_CALENDAR_UNSUPPORTED_REASON
    else:
        reason_code = None
    return {
        "supported": supported,
        "installed": installed,
        "installed_version": version or None,
        "minimum_version": FUTU_EARNINGS_CALENDAR_MIN_VERSION,
        "method_available": method_available,
        "capability": FUTU_EARNINGS_CALENDAR_CAPABILITY,
        "reason_code": reason_code,
    }


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

    def _ensure_quote_client(self) -> Any:
        if self._quote_client is None:
            _ensure_futu_api_importable()
            import futu

            self._quote_client = futu.OpenQuoteContext(host=self.host, port=self.port)
            LOG.info(
                "futu_sdk_client_created event=client_created capability=quote host=%s port=%s",
                self.host,
                self.port,
            )
        return self._quote_client

    def _ensure_trade_client(self) -> Any:
        if self._trade_client is None:
            _ensure_futu_api_importable()
            import futu

            self._trade_client = futu.OpenSecTradeContext(host=self.host, port=self.port)
            LOG.info(
                "futu_sdk_client_created event=client_created capability=broker host=%s port=%s",
                self.host,
                self.port,
            )
        return self._trade_client

    def _ensure_clients(self) -> tuple[Any, Any]:
        """Backward-compatible combined access; capability paths do not use it."""
        self._ensure_quote_client()
        self._ensure_trade_client()
        return self._quote_client, self._trade_client


class _FutuAPIClient:
    def __init__(self, backend: Any, *, is_option_chain_cache_enabled: bool) -> None:
        self.backend = backend
        self.is_option_chain_cache_enabled = bool(is_option_chain_cache_enabled)

    def _quote(self) -> Any:
        ensure = getattr(self.backend, "_ensure_quote_client", None)
        if callable(ensure):
            return ensure()
        quote, _trade = self.backend._ensure_clients()
        return quote

    def _trade(self) -> Any:
        ensure = getattr(self.backend, "_ensure_trade_client", None)
        if callable(ensure):
            return ensure()
        _quote, trade = self.backend._ensure_clients()
        return trade

    @staticmethod
    def _trade_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Adapt canonical string account ids to the Futu SDK wire type."""

        params = dict(kwargs or {})
        raw_account_id = params.get("acc_id")
        if raw_account_id is None:
            return params
        if isinstance(raw_account_id, bool):
            raise ValueError("Futu acc_id must be a lossless integer")
        if isinstance(raw_account_id, Integral):
            params["acc_id"] = int(raw_account_id)
            return params
        if isinstance(raw_account_id, str):
            stripped = raw_account_id.strip()
            try:
                parsed = int(stripped)
            except (TypeError, ValueError) as exc:
                raise ValueError("Futu acc_id must be a lossless integer") from exc
            if str(parsed) != stripped:
                raise ValueError("Futu acc_id must be a lossless integer")
            params["acc_id"] = parsed
            return params
        raise ValueError("Futu acc_id must be a lossless integer")

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
            for key, value in self._trade_kwargs(kwargs).items()
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
        return self._unwrap(
            self._trade().position_list_query(**self._trade_kwargs(kwargs))
        )

    def get_account_balance(self, **kwargs: Any) -> Any:
        return self._unwrap(
            self._trade().accinfo_query(**self._trade_kwargs(kwargs))
        )

    def get_funds(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "acctradinginfo_query"):
            return self._unwrap(
                trade.acctradinginfo_query(**self._trade_kwargs(kwargs))
            )
        return self._unwrap(
            trade.accinfo_query(**self._trade_kwargs(kwargs))
        )

    def get_order_list(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "order_list_query"):
            return self._unwrap(
                trade.order_list_query(**self._trade_kwargs(kwargs))
            )
        raise AttributeError("order_list_query unavailable")

    def get_deal_list(self, **kwargs: Any) -> Any:
        trade = self._trade()
        if hasattr(trade, "deal_list_query"):
            return self._unwrap(
                trade.deal_list_query(**self._trade_kwargs(kwargs))
            )
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

    def get_earnings_calendar(self, **kwargs: Any) -> Any:
        quote = self._quote()
        method = getattr(quote, FUTU_EARNINGS_CALENDAR_CAPABILITY, None)
        if not callable(method):
            raise FutuGatewayCapabilityUnavailableError(
                "OpenD earnings calendar is unavailable; upgrade futu-api and OpenD",
                capability=FUTU_EARNINGS_CALENDAR_CAPABILITY,
                reason_code=FUTU_EARNINGS_CALENDAR_UNSUPPORTED_REASON,
            )
        return self._unwrap(method(**kwargs))

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


class FutuGatewayCapabilityUnavailableError(FutuGatewayError):
    code = "CAPABILITY_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        capability: str,
        reason_code: str,
        raw_error: Any | None = None,
    ) -> None:
        super().__init__(message, raw_error=raw_error)
        self.capability = str(capability)
        self.reason_code = str(reason_code)


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
    _closed_client_ids: set[int] = field(default_factory=set, init=False, repr=False)

    def _raise_mapped(self, exc: Exception, *, action: str) -> None:
        mapped = _map_error(exc, action=action)
        LOG.error("[futu_gateway] %s code=%s error=%s", action, getattr(mapped, "code", "UNKNOWN"), mapped)
        raise mapped

    def close(self) -> None:
        for c in (getattr(self.backend, "_quote_client", None), getattr(self.backend, "_trade_client", None)):
            client_id = id(c)
            if c is None or client_id in self._closed_client_ids:
                continue
            self._closed_client_ids.add(client_id)
            try:
                c.close()
            except Exception:
                pass

    def _quote_client(self) -> Any:
        try:
            ensure = getattr(self.backend, "_ensure_quote_client", None)
            if callable(ensure):
                return ensure()
            quote, _ = self.backend._ensure_clients()
            return quote
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_quote_client")
        raise AssertionError("unreachable")

    def _trade_client(self) -> Any:
        try:
            ensure = getattr(self.backend, "_ensure_trade_client", None)
            if callable(ensure):
                return ensure()
            _, trade = self.backend._ensure_clients()
            return trade
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_trade_client")
        raise AssertionError("unreachable")

    def ensure_quote_ready(self) -> dict[str, Any]:
        quote = self._quote_client()
        try:
            ret, state = quote.get_global_state()
            if ret != 0:
                raise RuntimeError(f"OpenD get_global_state ret={ret}: {state}")
            if not isinstance(state, dict):
                raise RuntimeError(f"OpenD invalid global_state: {state}")
            if state.get("program_status_type") != "READY":
                raise RuntimeError(f"OpenD not READY: {state}")
            if state.get("qot_logined") is not True:
                raise RuntimeError(f"OpenD quote not logged in: {state}")
            return state
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_quote_ready")
        raise AssertionError("unreachable")

    def ensure_broker_ready(
        self,
        *,
        expected_account_ids: Iterable[str],
        trd_env: str,
    ) -> dict[str, Any]:
        trade = self._trade_client()
        required = _normalize_required_account_ids(expected_account_ids)
        environment = _normalize_trade_environment(trd_env)
        if not required:
            raise FutuGatewayError("ensure_broker_ready failed: expected_account_ids is empty")
        if not environment:
            raise FutuGatewayError("ensure_broker_ready failed: trd_env is empty")
        try:
            ret, state = trade.get_global_state()
            if ret != 0:
                raise RuntimeError(f"OpenD get_global_state ret={ret}: {state}")
            if not isinstance(state, dict):
                raise RuntimeError(f"OpenD invalid global_state: {state}")
            if state.get("program_status_type") != "READY":
                raise RuntimeError(f"OpenD not READY: {state}")
            if state.get("trd_logined") is not True:
                raise RuntimeError("OpenD trade not logged in")

            result = trade.get_acc_list()
            rows = self.client._rows(self.client._unwrap(result))
            observed: set[str] = set()
            for row in rows:
                row_env = _normalize_trade_environment(
                    row.get("trd_env") or row.get("env")
                )
                if row_env != environment:
                    continue
                account_id = _normalize_observed_account_id(row.get("acc_id"))
                if account_id:
                    observed.add(account_id)
            missing = sorted(required - observed)
            if missing:
                raise RuntimeError(
                    "OpenD required broker identities are unavailable "
                    f"for trd_env={environment}: missing={[_mask_account_id(value) for value in missing]}"
                )
            return {
                "program_status_type": state.get("program_status_type"),
                "trd_logined": True,
                "trd_env": environment,
                "required_account_id_count": len(required),
                "matched_account_id_count": len(required),
                "masked_required_account_ids": [
                    _mask_account_id(value) for value in sorted(required)
                ],
            }
        except Exception as exc:
            self._raise_mapped(exc, action="ensure_broker_ready")
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

    def get_earnings_calendar(
        self,
        *,
        market: Any,
        begin_date: str,
        end_date: str,
        sort_type: Any | None = None,
        filter_list: Any | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "market": market,
            "begin_date": str(begin_date),
            "end_date": str(end_date),
        }
        if sort_type is not None:
            kwargs["sort_type"] = sort_type
        if filter_list is not None:
            kwargs["filter_list"] = filter_list
        try:
            return self.client.get_earnings_calendar(**kwargs)
        except FutuGatewayCapabilityUnavailableError:
            raise
        except Exception as exc:
            self._raise_mapped(exc, action=FUTU_EARNINGS_CALENDAR_CAPABILITY)
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


def build_ready_futu_quote_gateway(
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    is_option_chain_cache_enabled: bool = True,
    backend_cls: Any | None = None,
    client_cls: Any | None = None,
) -> FutuGateway:
    """Build a gateway and preflight only the quote capability."""

    return build_ready_futu_gateway(
        host=host,
        port=port,
        is_option_chain_cache_enabled=is_option_chain_cache_enabled,
        backend_cls=backend_cls,
        client_cls=client_cls,
    )


def build_ready_futu_broker_gateway(
    *,
    expected_account_ids: Iterable[str],
    trd_env: str,
    host: str = "127.0.0.1",
    port: int = 11111,
    is_option_chain_cache_enabled: bool = False,
    backend_cls: Any | None = None,
    client_cls: Any | None = None,
) -> FutuGateway:
    """Build a gateway and verify only broker identity/readiness."""

    gateway = build_futu_gateway(
        host=host,
        port=port,
        is_option_chain_cache_enabled=is_option_chain_cache_enabled,
        backend_cls=backend_cls,
        client_cls=client_cls,
    )
    try:
        gateway.ensure_broker_ready(
            expected_account_ids=expected_account_ids,
            trd_env=trd_env,
        )
        return gateway
    except Exception:
        gateway.close()
        raise


def _normalize_required_account_ids(values: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for raw in values:
        value = _normalize_observed_account_id(raw)
        if value is None:
            raise FutuGatewayError(
                "ensure_broker_ready failed: account id is not losslessly comparable"
            )
        normalized.add(value)
    return normalized


def _normalize_observed_account_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return str(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped)
    except (TypeError, ValueError):
        return None
    return stripped if str(parsed) == stripped else None


def _normalize_trade_environment(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw


def _mask_account_id(value: str) -> str:
    raw = str(value or "")
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{'*' * (len(raw) - 4)}{raw[-4:]}"


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
