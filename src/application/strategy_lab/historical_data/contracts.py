from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol


HISTORICAL_DATA_SCHEMA_VERSION = "strategy_lab_historical_data.v1"
SUPPORTED_ASSET_TYPES = frozenset({"underlying", "option"})
SUPPORTED_TIMEFRAMES = frozenset({"1d", "1m", "5m", "15m", "30m", "60m"})


class HistoricalMarketDataProvider(Protocol):
    name: str

    def fetch(self, request: "HistoricalDataRequest") -> "HistoricalDataSnapshot":
        ...


@dataclass(frozen=True)
class HistoricalDataRequest:
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    asset_type: str = "underlying"
    timeframe: str = "1d"
    provider: str | None = None
    adjusted: bool = False
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        symbols = tuple(_normalize_symbol(item) for item in self.symbols if _normalize_symbol(item))
        if not symbols:
            raise ValueError("historical data request requires at least one symbol")
        asset_type = str(self.asset_type or "").strip().lower()
        if asset_type not in SUPPORTED_ASSET_TYPES:
            raise ValueError(f"unsupported historical asset_type: {self.asset_type}")
        timeframe = str(self.timeframe or "").strip().lower()
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported historical timeframe: {self.timeframe}")
        start_date = str(self.start_date or "").strip()
        end_date = str(self.end_date or "").strip()
        if not start_date or not end_date:
            raise ValueError("historical data request requires start_date and end_date")
        if start_date > end_date:
            raise ValueError("historical data request start_date must be <= end_date")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "asset_type", asset_type)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "start_date", start_date)
        object.__setattr__(self, "end_date", end_date)
        object.__setattr__(self, "provider", _text(self.provider, lower=True))
        object.__setattr__(self, "fields", tuple(str(item).strip().lower() for item in self.fields if str(item).strip()))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HistoricalDataRequest":
        symbols_raw = payload.get("symbols") or payload.get("symbol")
        symbols = _list(symbols_raw)
        return cls(
            symbols=tuple(str(item) for item in symbols),
            start_date=str(payload.get("start_date") or payload.get("start") or ""),
            end_date=str(payload.get("end_date") or payload.get("end") or ""),
            asset_type=str(payload.get("asset_type") or "underlying"),
            timeframe=str(payload.get("timeframe") or "1d"),
            provider=_text(payload.get("provider"), lower=True),
            adjusted=_truthy(payload.get("adjusted")),
            fields=tuple(str(item) for item in _list(payload.get("fields"))),
        )

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "asset_type": self.asset_type,
            "timeframe": self.timeframe,
            "provider": self.provider,
            "adjusted": bool(self.adjusted),
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class HistoricalBar:
    symbol: str
    timestamp: str
    asset_type: str = "underlying"
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    currency: str | None = None
    contract_symbol: str | None = None
    option_type: str | None = None
    strike: float | None = None
    expiry: str | None = None
    iv: float | None = None
    delta: float | None = None
    source: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = _normalize_symbol(self.symbol)
        if not symbol:
            raise ValueError("historical bar requires symbol")
        timestamp = str(self.timestamp or "").strip()
        if not timestamp:
            raise ValueError("historical bar requires timestamp")
        asset_type = str(self.asset_type or "underlying").strip().lower()
        if asset_type not in SUPPORTED_ASSET_TYPES:
            raise ValueError(f"unsupported historical bar asset_type: {self.asset_type}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "asset_type", asset_type)
        object.__setattr__(self, "currency", _text(self.currency, upper=True))
        object.__setattr__(self, "contract_symbol", _text(self.contract_symbol))
        object.__setattr__(self, "option_type", _text(self.option_type, lower=True))
        object.__setattr__(self, "expiry", _text(self.expiry))
        object.__setattr__(self, "source", _text(self.source, lower=True))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw or {})))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HistoricalBar":
        return cls(
            symbol=str(payload.get("symbol") or payload.get("underlying") or ""),
            timestamp=str(payload.get("timestamp") or payload.get("date") or payload.get("time") or ""),
            asset_type=str(payload.get("asset_type") or "underlying"),
            open=_float(payload.get("open")),
            high=_float(payload.get("high")),
            low=_float(payload.get("low")),
            close=_float(payload.get("close") or payload.get("last")),
            volume=_float(payload.get("volume")),
            bid=_float(payload.get("bid")),
            ask=_float(payload.get("ask")),
            mid=_float(payload.get("mid")),
            currency=_text(payload.get("currency"), upper=True),
            contract_symbol=_text(payload.get("contract_symbol") or payload.get("option_symbol")),
            option_type=_text(payload.get("option_type"), lower=True),
            strike=_float(payload.get("strike")),
            expiry=_text(payload.get("expiry") or payload.get("expiration")),
            iv=_float(payload.get("iv") or payload.get("implied_volatility")),
            delta=_float(payload.get("delta")),
            source=_text(payload.get("source"), lower=True),
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "asset_type": self.asset_type,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "currency": self.currency,
            "contract_symbol": self.contract_symbol,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "iv": self.iv,
            "delta": self.delta,
            "source": self.source,
        }


@dataclass(frozen=True)
class HistoricalDataSnapshot:
    request: HistoricalDataRequest
    bars: tuple[HistoricalBar, ...]
    source: str
    generated_at: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        source = str(self.source or "").strip().lower() or "unknown"
        generated_at = str(self.generated_at or "").strip()
        if not generated_at:
            raise ValueError("historical data snapshot requires generated_at")
        bars = tuple(sorted(self.bars, key=lambda row: (row.symbol, row.timestamp, row.contract_symbol or "")))
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings if str(item).strip()))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HistoricalDataSnapshot":
        schema = str(payload.get("schema_version") or "")
        if schema != HISTORICAL_DATA_SCHEMA_VERSION:
            raise ValueError(f"unsupported historical data snapshot schema_version: {schema}")
        request_raw = payload.get("request")
        bars_raw = payload.get("bars")
        if not isinstance(request_raw, Mapping):
            raise ValueError("historical data snapshot requires request object")
        if not isinstance(bars_raw, list):
            raise ValueError("historical data snapshot requires bars list")
        return cls(
            request=HistoricalDataRequest.from_dict(request_raw),
            bars=tuple(HistoricalBar.from_dict(row) for row in bars_raw if isinstance(row, Mapping)),
            source=str(payload.get("source") or "unknown"),
            generated_at=str(payload.get("generated_at") or ""),
            warnings=tuple(str(item) for item in payload.get("warnings") or []),
        )

    @property
    def snapshot_id(self) -> str:
        return f"{self.source}-{self.request.fingerprint}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HISTORICAL_DATA_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "generated_at": self.generated_at,
            "request": self.request.to_dict(),
            "summary": historical_snapshot_summary(self),
            "bars": [bar.to_dict() for bar in self.bars],
            "warnings": list(self.warnings),
        }


def build_historical_data_snapshot(
    *,
    request: HistoricalDataRequest,
    bars: tuple[HistoricalBar, ...] | list[HistoricalBar],
    source: str,
    generated_at: str | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> HistoricalDataSnapshot:
    generated = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    normalized_bars = tuple(bar for bar in bars if bar.symbol in request.symbols)
    missing_symbols = tuple(symbol for symbol in request.symbols if symbol not in {bar.symbol for bar in normalized_bars})
    out_warnings = list(warnings)
    for symbol in missing_symbols:
        out_warnings.append(f"historical_data_missing_symbol:{symbol}")
    if not normalized_bars:
        out_warnings.append("historical_data_empty")
    return HistoricalDataSnapshot(
        request=request,
        bars=normalized_bars,
        source=source,
        generated_at=generated,
        warnings=tuple(out_warnings),
    )


def historical_snapshot_summary(snapshot: HistoricalDataSnapshot) -> dict[str, Any]:
    counts: dict[str, int] = {}
    timestamps = [bar.timestamp for bar in snapshot.bars]
    for bar in snapshot.bars:
        counts[bar.symbol] = counts.get(bar.symbol, 0) + 1
    return {
        "snapshot_id": snapshot.snapshot_id,
        "source": snapshot.source,
        "asset_type": snapshot.request.asset_type,
        "timeframe": snapshot.request.timeframe,
        "symbol_count": len(snapshot.request.symbols),
        "bar_count": len(snapshot.bars),
        "symbols": list(snapshot.request.symbols),
        "bar_counts_by_symbol": counts,
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
        "warnings": list(snapshot.warnings),
    }


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _text(value: Any, *, lower: bool = False, upper: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    if raw.endswith("%"):
        raw = raw[:-1].strip()
        try:
            return float(raw) / 100.0
        except Exception:
            return None
    try:
        return float(raw)
    except Exception:
        return None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [part.strip() for part in str(value).replace("|", ",").split(",") if part.strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
