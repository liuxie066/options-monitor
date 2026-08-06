from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

import pandas as pd


TRADING_DAYS_PER_YEAR = 252
DEFAULT_RV_WINDOWS = (20, 60, 120)
RV_DTE_WEIGHTS: tuple[tuple[int, tuple[tuple[int, float], ...]], ...] = (
    (30, ((20, 0.70), (60, 0.30))),
    (60, ((20, 0.30), (60, 0.50), (120, 0.20))),
    (90, ((20, 0.20), (60, 0.40), (120, 0.40))),
)


@dataclass(frozen=True)
class RealizedVolatilitySnapshot:
    rv_20: float | None = None
    rv_60: float | None = None
    rv_120: float | None = None
    rv_estimate: float | None = None
    sample_count: int = 0
    status: str = "missing"
    reason: str | None = None

    def to_row_fields(self, *, dte: int | None = None) -> dict[str, Any]:
        estimate = (
            realized_volatility_estimate_for_dte(
                dte=dte,
                rv_20=self.rv_20,
                rv_60=self.rv_60,
                rv_120=self.rv_120,
            )
            if dte is not None
            else self.rv_estimate
        )
        return {
            "realized_volatility_20": self.rv_20,
            "realized_volatility_60": self.rv_60,
            "realized_volatility_120": self.rv_120,
            "realized_volatility_estimate": estimate,
        }

    def to_meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "sample_count": self.sample_count,
            **self.to_row_fields(),
            "estimation_policy": "dte_matched_v1",
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

    return RealizedVolatilitySnapshot(
        rv_20=_round_optional(rv20),
        rv_60=_round_optional(rv60),
        rv_120=_round_optional(rv120),
        rv_estimate=None,
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


def realized_volatility_weights_for_dte(dte: int | float) -> tuple[tuple[int, float], ...]:
    try:
        dte_value = int(dte)
    except (TypeError, ValueError) as exc:
        raise ValueError("dte must be an integer in [1, 90]") from exc
    if dte_value <= 0 or dte_value > 90:
        raise ValueError("dte must be in [1, 90]")
    for upper_bound, weights in RV_DTE_WEIGHTS:
        if dte_value <= upper_bound:
            return weights
    raise ValueError("dte must be in [1, 90]")


def realized_volatility_estimate_for_dte(
    *,
    dte: int | float,
    rv_20: float | None,
    rv_60: float | None,
    rv_120: float | None,
) -> float | None:
    values = {20: rv_20, 60: rv_60, 120: rv_120}
    try:
        weights = realized_volatility_weights_for_dte(dte)
    except ValueError:
        return None
    weighted = 0.0
    for window, weight in weights:
        value = values.get(window)
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            return None
        weighted += float(value) * float(weight)
    return _round_optional(weighted)
