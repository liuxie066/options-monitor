from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

import pandas as pd


TRADING_DAYS_PER_YEAR = 252
DEFAULT_RV_WINDOWS = (20, 60, 120)


@dataclass(frozen=True)
class RealizedVolatilitySnapshot:
    rv_20: float | None = None
    rv_60: float | None = None
    rv_120: float | None = None
    rv_estimate: float | None = None
    sample_count: int = 0
    status: str = "missing"
    reason: str | None = None

    def to_row_fields(self) -> dict[str, Any]:
        return {
            "realized_volatility_20": self.rv_20,
            "realized_volatility_60": self.rv_60,
            "realized_volatility_120": self.rv_120,
            "realized_volatility_estimate": self.rv_estimate,
        }

    def to_meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "sample_count": self.sample_count,
            **self.to_row_fields(),
        }


def compute_realized_volatility_snapshot(
    rows: Iterable[dict[str, Any]] | pd.DataFrame | Any,
    *,
    windows: tuple[int, ...] = DEFAULT_RV_WINDOWS,
) -> RealizedVolatilitySnapshot:
    closes = _close_prices(rows)
    if len(closes) < 2:
        return RealizedVolatilitySnapshot(sample_count=len(closes), status="missing", reason="insufficient_close_prices")

    returns: list[float] = []
    for prev, cur in zip(closes[:-1], closes[1:]):
        if prev <= 0 or cur <= 0:
            continue
        returns.append(math.log(cur / prev))
    if len(returns) < 2:
        return RealizedVolatilitySnapshot(sample_count=len(closes), status="missing", reason="insufficient_returns")

    values: dict[int, float | None] = {}
    for window in windows:
        values[int(window)] = _annualized_std(returns[-int(window) :]) if len(returns) >= int(window) else None

    rv20 = values.get(20)
    rv60 = values.get(60)
    rv120 = values.get(120)
    usable = [v for v in (rv20, rv60, rv120) if v is not None and v > 0]
    if not usable:
        return RealizedVolatilitySnapshot(
            rv_20=rv20,
            rv_60=rv60,
            rv_120=rv120,
            sample_count=len(closes),
            status="missing",
            reason="insufficient_window_returns",
        )

    # Give the most recent realized volatility more weight without making the
    # estimate depend on a single short window.
    weighted_parts: list[tuple[float, float]] = []
    if rv20 is not None:
        weighted_parts.append((rv20, 0.50))
    if rv60 is not None:
        weighted_parts.append((rv60, 0.30))
    if rv120 is not None:
        weighted_parts.append((rv120, 0.20))
    total_weight = sum(weight for _value, weight in weighted_parts)
    estimate = sum(value * weight for value, weight in weighted_parts) / total_weight if total_weight > 0 else None

    return RealizedVolatilitySnapshot(
        rv_20=_round_optional(rv20),
        rv_60=_round_optional(rv60),
        rv_120=_round_optional(rv120),
        rv_estimate=_round_optional(estimate),
        sample_count=len(closes),
        status="ok",
    )


def fetch_realized_volatility_snapshot(
    gateway: Any,
    *,
    underlier_code: str,
    trading_day: date,
    lookback_calendar_days: int = 240,
) -> RealizedVolatilitySnapshot:
    start = trading_day - timedelta(days=max(140, int(lookback_calendar_days)))
    try:
        data: list[dict[str, Any]] = []
        page_req_key = None
        for _ in range(5):
            result = gateway.request_history_kline(
                code=str(underlier_code),
                start=start.isoformat(),
                end=trading_day.isoformat(),
                ktype="K_DAY",
                autype="QFQ",
                fields=["time_key", "close"],
                max_count=300,
                page_req_key=page_req_key,
            )
            chunk = result.get("data") if isinstance(result, dict) else result
            if isinstance(chunk, list):
                data.extend(item for item in chunk if isinstance(item, dict))
            elif hasattr(chunk, "to_dict"):
                try:
                    data.extend(chunk.to_dict("records"))
                except Exception:
                    pass
            page_req_key = result.get("page_req_key") if isinstance(result, dict) else None
            if not page_req_key or len(data) >= 130:
                break
        snapshot = compute_realized_volatility_snapshot(data)
        if snapshot.status == "ok":
            return snapshot
        return RealizedVolatilitySnapshot(
            rv_20=snapshot.rv_20,
            rv_60=snapshot.rv_60,
            rv_120=snapshot.rv_120,
            rv_estimate=snapshot.rv_estimate,
            sample_count=snapshot.sample_count,
            status="missing",
            reason=snapshot.reason or "history_kline_unusable",
        )
    except Exception as exc:
        return RealizedVolatilitySnapshot(status="error", reason=f"{type(exc).__name__}: {exc}")


def _close_prices(rows: Iterable[dict[str, Any]] | pd.DataFrame | Any) -> list[float]:
    if hasattr(rows, "to_dict"):
        try:
            raw_rows = rows.to_dict("records")
        except Exception:
            raw_rows = []
    elif isinstance(rows, list):
        raw_rows = rows
    else:
        raw_rows = []

    closes: list[float] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        value = _first_float(row, "close", "close_price", "last_close", "price")
        if value is not None and value > 0:
            closes.append(float(value))
    return closes


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        try:
            value = row.get(key)
            if value in (None, ""):
                continue
            parsed = float(value)
        except Exception:
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _annualized_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance)) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _round_optional(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)
