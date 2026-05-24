from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.domain.symbol_identity import canonical_symbol, futu_underlier_code, symbol_currency
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.strategy_lab.historical_data.contracts import (
    HistoricalBar,
    HistoricalDataRequest,
    HistoricalDataSnapshot,
    build_historical_data_snapshot,
)
from src.infrastructure.futu_gateway import build_ready_futu_gateway, retry_futu_gateway_call


TIMEFRAME_TO_FUTU_KTYPE: dict[str, str] = {
    "1d": "K_DAY",
    "1m": "K_1M",
    "5m": "K_5M",
    "15m": "K_15M",
    "30m": "K_30M",
    "60m": "K_60M",
}


@dataclass(frozen=True)
class FutuHistoricalFetchOptions:
    host: str = "127.0.0.1"
    port: int = 11111
    max_count: int = 1000
    max_pages: int = 20
    max_wait_sec: float = 30.0
    window_sec: float = 30.0
    max_calls: int = 30
    retry_max_attempts: int = 4
    retry_time_budget_sec: float = 20.0
    retry_base_delay_sec: float = 0.8
    retry_max_delay_sec: float = 6.0
    no_retry: bool = False
    adjustment: str | None = None


class FutuHistoricalMarketDataProvider:
    name = "futu"

    def __init__(
        self,
        *,
        base: Path,
        options: FutuHistoricalFetchOptions | None = None,
        gateway_factory: Callable[..., Any] = build_ready_futu_gateway,
        retry_call: Callable[..., Any] = retry_futu_gateway_call,
        rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    ) -> None:
        self.base = Path(base).resolve()
        self.options = options or FutuHistoricalFetchOptions()
        self.gateway_factory = gateway_factory
        self.retry_call = retry_call
        self.rate_limited_call = rate_limited_call

    def fetch(self, request: HistoricalDataRequest) -> HistoricalDataSnapshot:
        gateway = self.gateway_factory(
            host=str(self.options.host),
            port=int(self.options.port),
            is_option_chain_cache_enabled=False,
        )
        warnings: list[str] = []
        bars: list[HistoricalBar] = []
        try:
            for symbol in request.symbols:
                code = _futu_code_for_request_symbol(symbol, asset_type=request.asset_type)
                if not code:
                    warnings.append(f"historical_data_symbol_unresolved:{symbol}")
                    continue
                bars.extend(self._fetch_symbol_bars(gateway, request, symbol=symbol, code=code, warnings=warnings))
        finally:
            try:
                gateway.close()
            except Exception:
                pass
        return build_historical_data_snapshot(
            request=request,
            bars=bars,
            source=self.name,
            warnings=tuple(warnings),
        )

    def _fetch_symbol_bars(
        self,
        gateway: Any,
        request: HistoricalDataRequest,
        *,
        symbol: str,
        code: str,
        warnings: list[str],
    ) -> list[HistoricalBar]:
        page_key = None
        pages = 0
        out: list[HistoricalBar] = []
        while True:
            pages += 1
            if pages > max(1, int(self.options.max_pages)):
                warnings.append(f"historical_data_page_limit_reached:{symbol}")
                break

            payload = self._request_history_page(gateway, request, code=code, page_req_key=page_key)
            rows = _rows(payload.get("data"))
            for row in rows:
                bar = _bar_from_futu_row(
                    row,
                    requested_symbol=symbol,
                    fallback_code=code,
                    asset_type=request.asset_type,
                )
                if bar is not None:
                    out.append(bar)
            page_key = payload.get("page_req_key")
            if not page_key:
                break
        return out

    def _request_history_page(
        self,
        gateway: Any,
        request: HistoricalDataRequest,
        *,
        code: str,
        page_req_key: Any,
    ) -> dict[str, Any]:
        def _call() -> Any:
            return self.rate_limited_call(
                base_dir=self.base,
                endpoint="history_kline",
                max_wait_sec=float(self.options.max_wait_sec),
                window_sec=float(self.options.window_sec),
                max_calls=int(self.options.max_calls),
                call=lambda: gateway.request_history_kline(
                    code=code,
                    start=request.start_date,
                    end=request.end_date,
                    ktype=TIMEFRAME_TO_FUTU_KTYPE.get(request.timeframe, "K_DAY"),
                    autype=_autype(request, adjustment=self.options.adjustment),
                    fields=_history_fields(request.fields),
                    max_count=max(1, int(self.options.max_count)),
                    page_req_key=page_req_key,
                ),
            )

        data = self.retry_call(
            f"request_history_kline({code})",
            _call,
            no_retry=bool(self.options.no_retry),
            retry_max_attempts=int(self.options.retry_max_attempts),
            retry_time_budget_sec=float(self.options.retry_time_budget_sec),
            retry_base_delay_sec=float(self.options.retry_base_delay_sec),
            retry_max_delay_sec=float(self.options.retry_max_delay_sec),
            quiet=True,
        )
        if isinstance(data, dict):
            return data
        return {"data": data, "page_req_key": None}


def normalize_historical_symbols(symbols: Any, *, asset_type: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in _list(symbols):
        raw = str(item or "").strip()
        if not raw:
            continue
        if str(asset_type or "").strip().lower() == "underlying":
            out.append(canonical_symbol(raw) or raw.upper())
        else:
            out.append(raw.upper())
    return tuple(dict.fromkeys(out))


def _futu_code_for_request_symbol(symbol: str, *, asset_type: str) -> str | None:
    if str(asset_type or "").strip().lower() == "underlying":
        return futu_underlier_code(symbol)
    text = str(symbol or "").strip().upper()
    if text.startswith(("US.", "HK.", "SH.", "SZ.")):
        return text
    return text or None


def _autype(request: HistoricalDataRequest, *, adjustment: str | None) -> str:
    raw = str(adjustment or "").strip().lower()
    if raw in {"qfq", "hfq", "none"}:
        return raw.upper() if raw != "none" else "NONE"
    return "QFQ" if request.adjusted else "NONE"


def _history_fields(fields: tuple[str, ...]) -> list[str] | None:
    if not fields:
        return None
    out: list[str] = []
    for field in fields:
        normalized = str(field or "").strip().upper()
        if not normalized:
            continue
        out.append(normalized if normalized.startswith("KL_FIELD.") else normalized)
    return out or None


def _bar_from_futu_row(
    row: Mapping[str, Any],
    *,
    requested_symbol: str,
    fallback_code: str,
    asset_type: str,
) -> HistoricalBar | None:
    timestamp = _text(_pick(row, "time_key", "datetime", "time", "date", "timestamp"))
    if not timestamp:
        return None
    code = _text(_pick(row, "code", "symbol")) or fallback_code
    symbol = _symbol_from_futu_code(code, fallback=requested_symbol, asset_type=asset_type)
    return HistoricalBar(
        symbol=symbol,
        timestamp=timestamp,
        asset_type=asset_type,
        open=_float(_pick(row, "open", "open_price")),
        high=_float(_pick(row, "high", "high_price")),
        low=_float(_pick(row, "low", "low_price")),
        close=_float(_pick(row, "close", "close_price", "last_price")),
        volume=_float(_pick(row, "volume", "vol")),
        currency=symbol_currency(symbol) if asset_type == "underlying" else None,
        source="futu",
        raw=dict(row),
    )


def _symbol_from_futu_code(code: str, *, fallback: str, asset_type: str) -> str:
    if str(asset_type or "").strip().lower() != "underlying":
        return str(code or fallback).strip().upper()
    return canonical_symbol(code) or str(fallback or code).strip().upper()


def _rows(data: Any) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        try:
            records = data.to_dict("records")
            if isinstance(records, list):
                return [dict(row) for row in records if isinstance(row, Mapping)]
        except Exception:
            pass
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, Mapping)]
    if isinstance(data, Mapping):
        rows = data.get("rows") or data.get("records") or data.get("data")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        return [dict(data)]
    return []


def _pick(row: Mapping[str, Any], *keys: str) -> Any:
    lower = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
        normalized = str(key).strip().lower()
        if normalized in lower and lower.get(normalized) not in (None, ""):
            return lower.get(normalized)
    return None


def _float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except Exception:
        return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[Any] = []
        for item in value:
            out.extend(_list(item))
        return out
    return [part.strip() for part in str(value).replace("|", ",").split(",") if part.strip()]
