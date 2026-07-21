from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.application.events.source_yfinance import EventSourceError, to_date_str
from src.application.opend_utils import normalize_underlier
from src.infrastructure.futu_gateway import FutuGateway, build_ready_futu_gateway


MIN_FUTU_EVENT_API_VERSION = "10.6.6608"


def fetch_symbol_events_futu(
    symbol: str,
    *,
    gateway: FutuGateway | Any | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    close_gateway: bool | None = None,
    split_pages: int = 3,
    split_page_size: int = 50,
) -> list[dict[str, Any]]:
    """Fetch event-risk dates through Futu OpenD F10 corporate-action APIs."""

    return list(
        fetch_symbol_event_evidence_futu(
            symbol,
            gateway=gateway,
            host=host,
            port=port,
            close_gateway=close_gateway,
            split_pages=split_pages,
            split_page_size=split_page_size,
        )["events"]
    )


def fetch_symbol_event_evidence_futu(
    symbol: str,
    *,
    gateway: FutuGateway | Any | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    close_gateway: bool | None = None,
    split_pages: int = 3,
    split_page_size: int = 50,
) -> dict[str, Any]:
    """Fetch events plus per-category coverage for authoritative absence checks."""

    underlier = normalize_underlier(symbol)
    code = underlier.code
    owned_gateway = gateway is None
    gw = gateway or build_ready_futu_gateway(host=str(host), port=int(port), is_option_chain_cache_enabled=False)
    should_close = bool(owned_gateway if close_gateway is None else close_gateway)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    source_ok_count = 0
    source_errors: list[str] = []
    coverage: dict[str, dict[str, str]] = {}

    def add_event(event_type: str, value: Any, *, raw: dict[str, Any] | None = None) -> None:
        ds = futu_date_str(value)
        if not ds:
            return
        key = (event_type, ds)
        if key in seen:
            return
        seen.add(key)
        event: dict[str, Any] = {"type": event_type, "date": ds, "source": "futu", "futu_code": code}
        if raw:
            event["raw"] = _compact_raw(raw)
        events.append(event)

    try:
        rows = _rows_from_table(gw.get_financials_earnings_price_history(code))
        for row in rows:
            add_event(
                "earnings",
                _first_value(row, "pub_trading_day_str", "pub_trading_day", "pub_time_str", "pub_time"),
                raw=row,
            )
        source_ok_count += 1
        coverage["earnings"] = {"status": "complete", "error": ""}
    except Exception as exc:
        error = f"earnings_price_history:{type(exc).__name__}:{exc}"
        source_errors.append(error)
        coverage["earnings"] = {"status": "partial", "error": error}

    try:
        payload = gw.get_corporate_actions_dividends(code)
        for row in _rows_from_payload(payload, "dividend_list"):
            add_event("ex_dividend", _first_value(row, "ex_date", "ex_date_str"), raw=row)
        source_ok_count += 1
        coverage["ex_dividend"] = {"status": "complete", "error": ""}
    except Exception as exc:
        error = f"dividends:{type(exc).__name__}:{exc}"
        source_errors.append(error)
        coverage["ex_dividend"] = {"status": "partial", "error": error}

    try:
        for row in _fetch_split_rows(gw, code=code, max_pages=split_pages, page_size=split_page_size):
            add_event(
                "split",
                _first_value(row, "ex_date", "ex_date_str", "temp_trade_begin_date", "temp_trade_begin_date_str"),
                raw=row,
            )
        source_ok_count += 1
        coverage["split"] = {"status": "complete", "error": ""}
    except Exception as exc:
        error = f"stock_splits:{type(exc).__name__}:{exc}"
        source_errors.append(error)
        coverage["split"] = {"status": "partial", "error": error}
    finally:
        if should_close:
            try:
                gw.close()
            except Exception:
                pass

    if source_ok_count == 0 and source_errors:
        message = "; ".join(source_errors)
        raise EventSourceError(message, error_code=classify_futu_event_error(message))

    events.sort(key=lambda x: (x.get("date") or "", x.get("type") or ""))
    return {"events": events, "coverage": coverage}


def classify_futu_event_error(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if (
        "unavailable; upgrade futu-api" in lowered
        or "has no attribute" in lowered
        or "attributeerror" in lowered
        or "protoid" in lowered
    ):
        return "capability_missing"
    if "no module named" in lowered or "modulenotfounderror" in lowered:
        return "dependency_missing"
    if "rate limit" in lowered or "too many requests" in lowered or "频率" in lowered:
        return "rate_limited"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "need_2fa" in lowered or "phone verification" in lowered or "verification required" in lowered:
        return "auth_required"
    if "auth_expired" in lowered or "not login" in lowered or "not logged" in lowered:
        return "auth_expired"
    return "source_error"


def futu_date_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = int(value)
        except Exception:
            numeric = 0
        text = str(numeric)
        if len(text) == 8:
            return _parse_yyyymmdd(text)
        if len(text) == 13:
            return datetime.fromtimestamp(numeric / 1000, tz=timezone.utc).date().isoformat()
        if len(text) == 10:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    compact = text.replace("-", "").replace("/", "")
    if compact.isdigit() and len(compact) == 8:
        return _parse_yyyymmdd(compact)
    return to_date_str(text)


def _parse_yyyymmdd(text: str) -> str | None:
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except Exception:
        return None


def _rows_from_table(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, pd.DataFrame):
        return [dict(row) for row in value.to_dict("records")]
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _rows_from_payload(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        raw = value.get(key)
    else:
        raw = value
    if isinstance(raw, pd.DataFrame):
        return [dict(row) for row in raw.to_dict("records")]
    if isinstance(raw, list):
        return [dict(row) for row in raw if isinstance(row, dict)]
    return []


def _fetch_split_rows(gateway: Any, *, code: str, max_pages: int, page_size: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_key: str | None = None
    for _ in range(max(1, int(max_pages))):
        payload = gateway.get_corporate_actions_stock_splits(
            code,
            next_key=next_key,
            num=max(1, int(page_size)),
        )
        rows.extend(_rows_from_payload(payload, "split_list"))
        if not isinstance(payload, dict):
            break
        raw_next = str(payload.get("next_key") or "").strip()
        if not raw_next or raw_next == "-1" or raw_next == next_key:
            break
        next_key = raw_next
    return rows


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _compact_raw(row: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "fiscal_year",
        "financial_type",
        "period_text",
        "is_current",
        "pub_type",
        "process",
        "statement",
        "record_date",
        "dividend_payable_date",
        "event_status",
        "rate",
        "reform_type",
    }
    return {key: value for key, value in row.items() if key in keep and value not in (None, "")}
