from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd


class EventSourceError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "source_error") -> None:
        super().__init__(message)
        self.error_code = error_code


def classify_event_source_error(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if "yfratelimiterror" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return "rate_limited"
    if "modulenotfounderror" in lowered or "no module named" in lowered:
        return "dependency_missing"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "source_error"


def to_date_str(value: Any) -> str | None:
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert(None)
    except Exception:
        pass
    return ts.date().isoformat()


def fetch_symbol_events_yfinance(symbol: str) -> list[dict[str, Any]]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    source_ok_count = 0
    source_errors: list[str] = []

    def _add(event_type: str, raw_value: Any) -> None:
        ds = to_date_str(raw_value)
        if not ds:
            return
        key = (event_type, ds)
        if key in seen:
            return
        seen.add(key)
        events.append({"type": event_type, "date": ds})

    try:
        edf = ticker.get_earnings_dates(limit=8)
        if isinstance(edf, pd.DataFrame) and not edf.empty:
            for idx in edf.index:
                _add("earnings", idx)
        source_ok_count += 1
    except Exception as exc:
        source_errors.append(f"earnings_dates:{type(exc).__name__}:{exc}")

    try:
        cal = ticker.calendar
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            for key in ("Earnings Date", "Ex-Dividend Date"):
                if key not in cal.index:
                    continue
                row = cal.loc[key]
                if isinstance(row, pd.Series):
                    for v in row.tolist():
                        _add("earnings" if key == "Earnings Date" else "ex_dividend", v)
                else:
                    _add("earnings" if key == "Earnings Date" else "ex_dividend", row)
        source_ok_count += 1
    except Exception as exc:
        source_errors.append(f"calendar:{type(exc).__name__}:{exc}")

    try:
        div = ticker.get_dividends()
        if isinstance(div, pd.Series) and not div.empty:
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=180)
            for idx in div.index:
                ds = to_date_str(idx)
                if not ds:
                    continue
                if ds >= cutoff.isoformat():
                    _add("ex_dividend", ds)
        source_ok_count += 1
    except Exception as exc:
        source_errors.append(f"dividends:{type(exc).__name__}:{exc}")

    if source_ok_count == 0 and source_errors:
        message = "; ".join(source_errors)
        raise EventSourceError(message, error_code=classify_event_source_error(message))

    events.sort(key=lambda x: (x.get("date") or "", x.get("type") or ""))
    return events
