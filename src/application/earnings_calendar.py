from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import resolve_symbol_identity
from src.infrastructure.futu_gateway import (
    FutuGatewayCapabilityUnavailableError,
    build_ready_futu_quote_gateway,
)
from src.infrastructure.io_utils import atomic_write_json


EARNINGS_CALENDAR_SCHEMA_VERSION = "opend_earnings_calendar.v1"
EARNINGS_CALENDAR_MAX_INTERVAL_DAYS = 7
_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}


def earnings_calendar_intervals(
    start: date,
    end: date,
    *,
    max_days: int = EARNINGS_CALENDAR_MAX_INTERVAL_DAYS,
) -> list[tuple[date, date]]:
    """Split an inclusive range into non-overlapping bounded intervals."""

    if end < start:
        return []
    if max_days < 1:
        raise ValueError("earnings calendar interval size must be positive")
    intervals: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        interval_end = min(end, cursor + timedelta(days=max_days - 1))
        intervals.append((cursor, interval_end))
        cursor = interval_end + timedelta(days=1)
    return intervals


def earnings_calendar_scan_date(market: str, scan_at_utc: datetime) -> date:
    market_norm = str(market or "").strip().upper()
    timezone_info = _MARKET_TIMEZONES.get(market_norm)
    if timezone_info is None:
        raise ValueError(f"unsupported earnings calendar market: {market}")
    return _aware_utc(scan_at_utc).astimezone(timezone_info).date()


def fetch_market_earnings_calendar(
    *,
    gateway: Any,
    market: str,
    scan_date: date,
    scan_at_utc: datetime,
    expirations_by_underlier: Mapping[str, Iterable[str]],
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Fetch one market calendar and project its coverage to exact expiries."""

    market_norm = str(market or "").strip().upper()
    if market_norm not in _MARKET_TIMEZONES:
        raise ValueError(f"unsupported earnings calendar market: {market}")
    scan_at = _aware_utc(scan_at_utc)
    normalized_expirations = _normalize_expirations(
        market=market_norm,
        expirations_by_underlier=expirations_by_underlier,
    )
    all_expirations = [
        expiration
        for expirations in normalized_expirations.values()
        for expiration in expirations
    ]
    if not all_expirations:
        raise ValueError("earnings calendar requires at least one exact expiration")
    max_expiration = max(all_expirations)
    if max_expiration < scan_date:
        raise ValueError("earnings calendar expiration precedes scan date")

    observed_now = now_fn or (lambda: datetime.now(timezone.utc))
    intervals: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    unsupported_reason: str | None = None
    for interval_start, interval_end in earnings_calendar_intervals(
        scan_date,
        max_expiration,
    ):
        observed_at = _iso_utc(observed_now())
        if unsupported_reason is not None:
            intervals.append(
                _unavailable_interval(
                    interval_start,
                    interval_end,
                    observed_at=observed_at,
                    reason_code=unsupported_reason,
                    error="OpenD earnings calendar capability is unavailable",
                )
            )
            continue
        try:
            raw = gateway.get_earnings_calendar(
                market=market_norm,
                begin_date=interval_start.isoformat(),
                end_date=interval_end.isoformat(),
            )
            normalized_rows = _normalize_provider_rows(
                raw,
                market=market_norm,
                interval_start=interval_start,
                interval_end=interval_end,
            )
        except FutuGatewayCapabilityUnavailableError as exc:
            unsupported_reason = str(
                exc.reason_code or "opend_earnings_calendar_unsupported"
            )
            intervals.append(
                _unavailable_interval(
                    interval_start,
                    interval_end,
                    observed_at=observed_at,
                    reason_code=unsupported_reason,
                    error=str(exc),
                )
            )
            continue
        except Exception as exc:
            intervals.append(
                _unavailable_interval(
                    interval_start,
                    interval_end,
                    observed_at=observed_at,
                    reason_code="opend_earnings_calendar_interval_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        result_hash = _payload_hash(normalized_rows)
        intervals.append(
            {
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "status": "ok",
                "reason_code": None,
                "error": None,
                "observed_at_utc": observed_at,
                "row_count": len(normalized_rows),
                "result_hash": result_hash,
            }
        )
        events.extend(normalized_rows)

    events = _deduplicate_events(events)
    snapshot: dict[str, Any] = {
        "schema_version": EARNINGS_CALENDAR_SCHEMA_VERSION,
        "source": "opend",
        "market": market_norm,
        "scan_date": scan_date.isoformat(),
        "scan_at_utc": _iso_utc(scan_at),
        "coverage_start": scan_date.isoformat(),
        "coverage_end": max_expiration.isoformat(),
        "status": (
            "ready"
            if all(item["status"] == "ok" for item in intervals)
            else "data_unavailable"
        ),
        "absence_authoritative": all(
            item["status"] == "ok" for item in intervals
        ),
        "intervals": intervals,
        "events": events,
        "expirations_by_underlier": {
            code: [item.isoformat() for item in expirations]
            for code, expirations in normalized_expirations.items()
        },
    }
    snapshot["evidence_by_underlier"] = {
        code: {
            expiration.isoformat(): project_earnings_for_expiry(
                snapshot,
                underlier_code=code,
                expiration=expiration,
            )
            for expiration in expirations
        }
        for code, expirations in normalized_expirations.items()
    }
    snapshot["snapshot_hash"] = _payload_hash(snapshot)
    return snapshot


def project_earnings_for_expiry(
    snapshot: Mapping[str, Any],
    *,
    underlier_code: str,
    expiration: str | date,
) -> dict[str, Any]:
    """Project market interval evidence to one underlier and expiry."""

    scan_date = _strict_date(snapshot.get("scan_date"), field="scan_date")
    scan_at = _strict_datetime(snapshot.get("scan_at_utc"), field="scan_at_utc")
    expiration_date = _strict_date(expiration, field="expiration")
    code = _normalize_underlier_code(
        underlier_code,
        expected_market=str(snapshot.get("market") or ""),
    )
    if expiration_date < scan_date:
        raise ValueError("earnings projection expiration precedes scan date")

    matching_events: list[dict[str, Any]] = []
    scan_day_timestamp_unavailable = False
    for raw_event in snapshot.get("events", []):
        if not isinstance(raw_event, Mapping):
            continue
        if raw_event.get("security") != code:
            continue
        event_date = _strict_date(
            raw_event.get("earnings_date"),
            field="earnings_date",
        )
        if event_date < scan_date or event_date > expiration_date:
            continue
        if event_date == scan_date:
            raw_timestamp = raw_event.get("earnings_timestamp")
            if raw_timestamp is None:
                scan_day_timestamp_unavailable = True
                continue
            event_at = datetime.fromtimestamp(float(raw_timestamp), tz=timezone.utc)
            if event_at <= scan_at:
                continue
        matching_events.append(dict(raw_event))

    failed_intervals = [
        dict(item)
        for item in snapshot.get("intervals", [])
        if isinstance(item, Mapping)
        and item.get("status") != "ok"
        and _strict_date(item.get("start"), field="interval.start")
        <= expiration_date
        and _strict_date(item.get("end"), field="interval.end") >= scan_date
    ]
    if matching_events:
        return {
            "status": "ready",
            "reason_code": None,
            "absence_authoritative": not failed_intervals,
            "has_earnings_event": True,
            "events": matching_events,
            "failed_intervals": failed_intervals,
        }
    if scan_day_timestamp_unavailable:
        return {
            "status": "data_unavailable",
            "reason_code": "scan_day_earnings_timestamp_unavailable",
            "absence_authoritative": False,
            "events": [],
            "failed_intervals": failed_intervals,
        }
    if failed_intervals:
        return {
            "status": "data_unavailable",
            "reason_code": str(
                failed_intervals[0].get("reason_code")
                or "earnings_calendar_coverage_incomplete"
            ),
            "absence_authoritative": False,
            "events": [],
            "failed_intervals": failed_intervals,
        }

    return {
        "status": "ready",
        "reason_code": None,
        "absence_authoritative": True,
        "has_earnings_event": bool(matching_events),
        "events": matching_events,
        "failed_intervals": [],
    }


def prefetch_market_earnings_calendars(
    *,
    market_requests: Mapping[str, Mapping[str, Any]],
    output_dir: Path,
    scan_at_utc: datetime,
    gateway_builder: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch and publish one shared earnings snapshot per market and run."""

    builder = gateway_builder or build_ready_futu_quote_gateway
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    market_results: dict[str, Any] = {}
    for market in sorted(market_requests):
        request = market_requests[market]
        host = str(request.get("host") or "127.0.0.1")
        port = int(request.get("port") or 11111)
        gateway: Any | None = None
        try:
            gateway = builder(
                host=host,
                port=port,
                is_option_chain_cache_enabled=False,
            )
        except Exception as exc:
            gateway = _UnavailableEarningsGateway(exc)
        try:
            snapshot = fetch_market_earnings_calendar(
                gateway=gateway,
                market=market,
                scan_date=_strict_date(
                    request.get("scan_date"),
                    field="scan_date",
                ),
                scan_at_utc=scan_at_utc,
                expirations_by_underlier=(
                    request.get("expirations_by_underlier")
                    if isinstance(
                        request.get("expirations_by_underlier"), Mapping
                    )
                    else {}
                ),
            )
        finally:
            try:
                gateway.close()
            except Exception:
                pass
        path = root / f"{market.upper()}.json"
        atomic_write_json(path, snapshot, sort_keys=True)
        market_results[market.upper()] = {
            "status": snapshot["status"],
            "absence_authoritative": snapshot["absence_authoritative"],
            "interval_count": len(snapshot["intervals"]),
            "failed_interval_count": sum(
                item["status"] != "ok" for item in snapshot["intervals"]
            ),
            "artifact_path": path.relative_to(root.parent).as_posix(),
            "snapshot_hash": snapshot["snapshot_hash"],
        }
    return {
        "schema_version": EARNINGS_CALENDAR_SCHEMA_VERSION,
        "source": "opend",
        "market_count": len(market_results),
        "markets": market_results,
    }


class _UnavailableEarningsGateway:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_earnings_calendar(self, **_kwargs: Any) -> Any:
        raise self.error

    def close(self) -> None:
        return None


def _normalize_expirations(
    *,
    market: str,
    expirations_by_underlier: Mapping[str, Iterable[str]],
) -> dict[str, list[date]]:
    normalized: dict[str, list[date]] = {}
    for raw_code, raw_expirations in expirations_by_underlier.items():
        code = _normalize_underlier_code(raw_code, expected_market=market)
        expirations = sorted(
            {
                _strict_date(item, field="expiration")
                for item in raw_expirations
            }
        )
        if expirations:
            normalized.setdefault(code, [])
            normalized[code] = sorted(set(normalized[code]) | set(expirations))
    return normalized


def _normalize_provider_rows(
    payload: Any,
    *,
    market: str,
    interval_start: date,
    interval_end: date,
) -> list[dict[str, Any]]:
    rows = _rows(payload)
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        security_raw = _first_value(raw, "security", "code")
        event_date_raw = _first_value(raw, "earnings_date", "earning_date")
        if security_raw is None or event_date_raw is None:
            raise ValueError("earnings calendar row lacks security or earnings_date")
        security = _normalize_underlier_code(
            _security_value(security_raw),
            expected_market=market,
        )
        event_date = _strict_date(event_date_raw, field="earnings_date")
        if not interval_start <= event_date <= interval_end:
            raise ValueError("earnings calendar row falls outside requested interval")
        timestamp = _normalize_timestamp(
            _first_value(
                raw,
                "earnings_timestamp",
                "earning_timestamp",
                "earning_time",
            ),
            market=market,
            event_date=event_date,
        )
        normalized.append(
            {
                "security": security,
                "earnings_date": event_date.isoformat(),
                "earnings_timestamp": timestamp,
                "pub_type": _clean_optional_text(raw.get("pub_type")),
            }
        )
    return _deduplicate_events(normalized)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        for key in ("data", "rows", "earnings_calendar"):
            candidate = payload.get(key)
            if candidate is not None:
                return _rows(candidate)
        return [dict(payload)]
    if isinstance(payload, list):
        if not all(isinstance(item, Mapping) for item in payload):
            raise ValueError("earnings calendar result contains a non-object row")
        return [dict(item) for item in payload]
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        records = to_dict(orient="records")
        return _rows(records)
    raise ValueError("unsupported earnings calendar result type")


def _normalize_timestamp(value: Any, *, market: str, event_date: date) -> float | None:
    if _is_missing(value):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    event_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if event_at.astimezone(_MARKET_TIMEZONES[market]).date() != event_date:
        return None
    return timestamp


def _normalize_underlier_code(value: Any, *, expected_market: str) -> str:
    identity = resolve_symbol_identity(value)
    market = str(expected_market or "").strip().upper()
    if identity is None or identity.market != market:
        raise ValueError(f"invalid {market} earnings calendar security: {value}")
    return identity.futu_code


def _security_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _first_value(value, "code", "security")
    code = getattr(value, "code", None)
    return code if code not in (None, "") else value


def _first_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if not _is_missing(value):
            return value
    return None


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    try:
        return bool(math.isnan(float(value)))
    except (TypeError, ValueError):
        return False


def _clean_optional_text(value: Any) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _strict_date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str) and value == value.strip():
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid {field}") from exc
    else:
        raise ValueError(f"invalid {field}")
    return parsed


def _strict_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware_utc(value)
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("earnings calendar scan timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _deduplicate_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in events:
        event = dict(raw)
        key = (
            event.get("security"),
            event.get("earnings_date"),
            event.get("earnings_timestamp"),
            event.get("pub_type"),
        )
        by_key[key] = event
    return sorted(
        by_key.values(),
        key=lambda item: (
            str(item.get("earnings_date") or ""),
            str(item.get("security") or ""),
            float(item.get("earnings_timestamp") or 0.0),
            str(item.get("pub_type") or ""),
        ),
    )


def _unavailable_interval(
    start: date,
    end: date,
    *,
    observed_at: str,
    reason_code: str,
    error: str,
) -> dict[str, Any]:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": "data_unavailable",
        "reason_code": reason_code,
        "error": error,
        "observed_at_utc": observed_at,
        "row_count": None,
        "result_hash": None,
    }


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
