from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
    classify_pending_earnings_events,
)
from domain.domain.symbol_identity import resolve_symbol_identity
from src.infrastructure.futu_gateway import (
    FutuGatewayCapabilityUnavailableError,
    build_ready_futu_quote_gateway,
)
from src.infrastructure.io_utils import atomic_write_json


EARNINGS_CALENDAR_SCHEMA_VERSION = "opend_earnings_calendar.v2"
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
        "policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
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
    _validate_earnings_calendar_source(snapshot)
    snapshot["evidence_by_underlier"] = {
        code: {
            expiration.isoformat(): _project_earnings_for_expiry(
                snapshot,
                underlier_code=code,
                expiration=expiration,
            )
            for expiration in expirations
        }
        for code, expirations in normalized_expirations.items()
    }
    snapshot["snapshot_hash"] = _payload_hash(snapshot)
    validate_earnings_calendar_snapshot(snapshot, expected_market=market_norm)
    return snapshot


def project_earnings_for_expiry(
    snapshot: Mapping[str, Any],
    *,
    underlier_code: str,
    expiration: str | date,
) -> dict[str, Any]:
    """Reproject validated source facts to one underlier and expiry."""

    validate_earnings_calendar_snapshot(snapshot)
    projected = _project_earnings_for_expiry(
        snapshot,
        underlier_code=underlier_code,
        expiration=expiration,
    )
    code = _normalize_underlier_code(
        underlier_code,
        expected_market=str(snapshot.get("market") or ""),
    )
    stored = (
        snapshot.get("evidence_by_underlier", {})
        .get(code, {})
        .get(_strict_date(expiration, field="expiration").isoformat())
    )
    if not isinstance(stored, Mapping) or dict(stored) != projected:
        raise ValueError("earnings calendar stored projection mismatch")
    return projected


def validate_earnings_calendar_snapshot(
    snapshot: Mapping[str, Any],
    *,
    expected_market: str | None = None,
) -> None:
    """Validate source partition, identity, and every stored v2 projection."""

    _validate_earnings_calendar_source(snapshot)
    market = str(snapshot.get("market") or "").strip().upper()
    if expected_market is not None and market != str(expected_market).strip().upper():
        raise ValueError("earnings calendar market mismatch")

    recorded_hash = str(snapshot.get("snapshot_hash") or "")
    hash_input = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if not _is_sha256(recorded_hash) or recorded_hash != _payload_hash(hash_input):
        raise ValueError("earnings calendar snapshot hash mismatch")

    expirations_by_underlier = snapshot.get("expirations_by_underlier")
    stored_by_underlier = snapshot.get("evidence_by_underlier")
    if not isinstance(expirations_by_underlier, Mapping) or not isinstance(
        stored_by_underlier, Mapping
    ):
        raise ValueError("earnings calendar projections are invalid")
    if set(stored_by_underlier) != set(expirations_by_underlier):
        raise ValueError("earnings calendar projection underliers mismatch")
    for code, expirations in expirations_by_underlier.items():
        stored_expirations = stored_by_underlier.get(code)
        if not isinstance(stored_expirations, Mapping) or set(stored_expirations) != set(
            expirations
        ):
            raise ValueError("earnings calendar projection expirations mismatch")
        for expiration in expirations:
            expected = _project_earnings_for_expiry(
                snapshot,
                underlier_code=str(code),
                expiration=str(expiration),
            )
            stored = stored_expirations.get(expiration)
            if not isinstance(stored, Mapping) or dict(stored) != expected:
                raise ValueError("earnings calendar stored projection mismatch")


def _project_earnings_for_expiry(
    snapshot: Mapping[str, Any],
    *,
    underlier_code: str,
    expiration: str | date,
) -> dict[str, Any]:
    scan_date = _strict_date(snapshot.get("scan_date"), field="scan_date")
    expiration_date = _strict_date(expiration, field="expiration")
    code = _normalize_underlier_code(
        underlier_code,
        expected_market=str(snapshot.get("market") or ""),
    )
    if expiration_date < scan_date:
        raise ValueError("earnings projection expiration precedes scan date")

    hard_start = max(
        scan_date,
        expiration_date - timedelta(days=EARNINGS_NEAR_EXPIRY_WINDOW_DAYS),
    )
    soft_end = hard_start - timedelta(days=1) if hard_start > scan_date else None
    matching_events = [
        dict(raw_event)
        for raw_event in snapshot.get("events", [])
        if isinstance(raw_event, Mapping)
        and raw_event.get("security") == code
        and scan_date
        <= _strict_date(raw_event.get("earnings_date"), field="earnings_date")
        <= expiration_date
    ]
    classified = classify_pending_earnings_events(
        matching_events,
        market_date=scan_date,
        expiration=expiration_date,
    )

    failed_intervals = [
        dict(item)
        for item in snapshot.get("intervals", [])
        if isinstance(item, Mapping) and item.get("status") != "ok"
    ]
    hard_failed = [
        item
        for item in failed_intervals
        if _interval_overlaps(item, start=hard_start, end=expiration_date)
    ]
    soft_failed = (
        [
            item
            for item in failed_intervals
            if _interval_overlaps(item, start=scan_date, end=soft_end)
        ]
        if soft_end is not None
        else []
    )
    blocking_events = classified["blocking_events"]
    hard_complete = not hard_failed
    if blocking_events or hard_complete:
        status = "ready"
        reason_code = None
    else:
        status = "data_unavailable"
        reason_code = _first_interval_reason(hard_failed)

    return {
        "status": status,
        "reason_code": reason_code,
        "absence_authoritative": hard_complete,
        "policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "market_date": scan_date.isoformat(),
        "expiration": expiration_date.isoformat(),
        "hard_window_start": hard_start.isoformat(),
        "hard_window_end": expiration_date.isoformat(),
        "hard_coverage_status": "complete" if hard_complete else "partial",
        "hard_reason_codes": _interval_reason_codes(hard_failed),
        "hard_failed_intervals": hard_failed,
        "soft_window_start": scan_date.isoformat() if soft_end is not None else None,
        "soft_window_end": soft_end.isoformat() if soft_end is not None else None,
        "soft_coverage_status": (
            "not_applicable"
            if soft_end is None
            else ("partial" if soft_failed else "complete")
        ),
        "soft_reason_codes": _interval_reason_codes(soft_failed),
        "soft_failed_intervals": soft_failed,
        "has_earnings_event": bool(classified["events"]),
        "has_blocking_earnings_event": bool(blocking_events),
        "events": classified["events"],
        "blocking_events": blocking_events,
        "nonblocking_events": classified["nonblocking_events"],
        "failed_intervals": sorted(
            {json.dumps(item, sort_keys=True): item for item in [*hard_failed, *soft_failed]}.values(),
            key=lambda item: (str(item.get("start") or ""), str(item.get("end") or "")),
        ),
    }


def _validate_earnings_calendar_source(snapshot: Mapping[str, Any]) -> None:
    if not isinstance(snapshot, Mapping):
        raise ValueError("earnings calendar snapshot must be an object")
    if snapshot.get("schema_version") != EARNINGS_CALENDAR_SCHEMA_VERSION:
        raise ValueError("earnings calendar schema mismatch")
    if snapshot.get("source") != "opend":
        raise ValueError("earnings calendar source mismatch")
    if snapshot.get("policy_version") != EARNINGS_NEAR_EXPIRY_POLICY_VERSION:
        raise ValueError("earnings calendar policy version mismatch")
    window_days = snapshot.get("window_days")
    if isinstance(window_days, bool) or window_days != EARNINGS_NEAR_EXPIRY_WINDOW_DAYS:
        raise ValueError("earnings calendar window mismatch")

    market = str(snapshot.get("market") or "").strip().upper()
    timezone_info = _MARKET_TIMEZONES.get(market)
    if timezone_info is None:
        raise ValueError("earnings calendar market is invalid")
    scan_date = _strict_date(snapshot.get("scan_date"), field="scan_date")
    scan_at = _strict_datetime(snapshot.get("scan_at_utc"), field="scan_at_utc")
    if scan_at.astimezone(timezone_info).date() != scan_date:
        raise ValueError("earnings calendar scan date mismatch")
    coverage_start = _strict_date(
        snapshot.get("coverage_start"),
        field="coverage_start",
    )
    coverage_end = _strict_date(snapshot.get("coverage_end"), field="coverage_end")
    if coverage_start != scan_date or coverage_end < coverage_start:
        raise ValueError("earnings calendar coverage bounds are invalid")

    raw_expirations = snapshot.get("expirations_by_underlier")
    if not isinstance(raw_expirations, Mapping) or not raw_expirations:
        raise ValueError("earnings calendar expirations are invalid")
    normalized_expirations: dict[str, list[str]] = {}
    for raw_code, raw_dates in raw_expirations.items():
        code = _normalize_underlier_code(raw_code, expected_market=market)
        if code != raw_code or not isinstance(raw_dates, list) or not raw_dates:
            raise ValueError("earnings calendar expiration identity is invalid")
        dates = [_strict_date(item, field="expiration") for item in raw_dates]
        if dates != sorted(set(dates)) or any(
            item < scan_date or item > coverage_end for item in dates
        ):
            raise ValueError("earnings calendar expiration set is invalid")
        normalized_expirations[code] = [item.isoformat() for item in dates]
    if dict(raw_expirations) != normalized_expirations:
        raise ValueError("earnings calendar expirations are not canonical")
    if max(
        _strict_date(item, field="expiration")
        for dates in normalized_expirations.values()
        for item in dates
    ) != coverage_end:
        raise ValueError("earnings calendar coverage end mismatch")

    raw_intervals = snapshot.get("intervals")
    if not isinstance(raw_intervals, list) or not raw_intervals:
        raise ValueError("earnings calendar intervals are invalid")
    interval_rows: list[tuple[date, date, Mapping[str, Any]]] = []
    cursor = coverage_start
    for raw in raw_intervals:
        if not isinstance(raw, Mapping) or set(raw) != {
            "start",
            "end",
            "status",
            "reason_code",
            "error",
            "observed_at_utc",
            "row_count",
            "result_hash",
        }:
            raise ValueError("earnings calendar interval shape is invalid")
        start = _strict_date(raw.get("start"), field="interval.start")
        end = _strict_date(raw.get("end"), field="interval.end")
        if start != cursor or end < start or (end - start).days + 1 > EARNINGS_CALENDAR_MAX_INTERVAL_DAYS:
            raise ValueError("earnings calendar interval partition is invalid")
        if end > coverage_end:
            raise ValueError("earnings calendar interval exceeds coverage")
        _strict_datetime(raw.get("observed_at_utc"), field="interval.observed_at_utc")
        status = str(raw.get("status") or "")
        if status == "ok":
            row_count = raw.get("row_count")
            if (
                isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 0
                or raw.get("reason_code") is not None
                or raw.get("error") is not None
                or not _is_sha256(raw.get("result_hash"))
            ):
                raise ValueError("earnings calendar successful interval is invalid")
        elif status == "data_unavailable":
            if (
                not str(raw.get("reason_code") or "").strip()
                or not str(raw.get("error") or "").strip()
                or raw.get("row_count") is not None
                or raw.get("result_hash") is not None
            ):
                raise ValueError("earnings calendar unavailable interval is invalid")
        else:
            raise ValueError("earnings calendar interval status is invalid")
        interval_rows.append((start, end, raw))
        cursor = end + timedelta(days=1)
    if cursor != coverage_end + timedelta(days=1):
        raise ValueError("earnings calendar interval coverage is incomplete")

    raw_events = snapshot.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("earnings calendar events are invalid")
    normalized_events: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping) or set(raw) != {
            "security",
            "earnings_date",
            "earnings_timestamp",
            "pub_type",
        }:
            raise ValueError("earnings calendar event shape is invalid")
        code = _normalize_underlier_code(raw.get("security"), expected_market=market)
        event_date = _strict_date(raw.get("earnings_date"), field="earnings_date")
        if not coverage_start <= event_date <= coverage_end:
            raise ValueError("earnings calendar event is outside coverage")
        timestamp = _normalize_timestamp(
            raw.get("earnings_timestamp"),
            market=market,
            event_date=event_date,
        )
        if raw.get("earnings_timestamp") is not None and timestamp is None:
            raise ValueError("earnings calendar event timestamp is invalid")
        event = {
            "security": code,
            "earnings_date": event_date.isoformat(),
            "earnings_timestamp": timestamp,
            "pub_type": _clean_optional_text(raw.get("pub_type")),
        }
        if dict(raw) != event:
            raise ValueError("earnings calendar event is not canonical")
        source_interval = next(
            (
                interval
                for start, end, interval in interval_rows
                if start <= event_date <= end
            ),
            None,
        )
        if source_interval is None or source_interval.get("status") != "ok":
            raise ValueError("earnings calendar event lacks successful source interval")
        normalized_events.append(event)
    if raw_events != _deduplicate_events(normalized_events):
        raise ValueError("earnings calendar events are duplicated or unsorted")

    for start, end, interval in interval_rows:
        if interval.get("status") != "ok":
            continue
        interval_events = [
            item
            for item in normalized_events
            if start
            <= _strict_date(item.get("earnings_date"), field="earnings_date")
            <= end
        ]
        if interval.get("row_count") != len(interval_events) or interval.get(
            "result_hash"
        ) != _payload_hash(interval_events):
            raise ValueError("earnings calendar interval result identity mismatch")

    all_ok = all(interval.get("status") == "ok" for _, _, interval in interval_rows)
    if snapshot.get("status") != ("ready" if all_ok else "data_unavailable"):
        raise ValueError("earnings calendar aggregate status mismatch")
    if snapshot.get("absence_authoritative") is not all_ok:
        raise ValueError("earnings calendar aggregate absence status mismatch")


def _interval_overlaps(
    item: Mapping[str, Any],
    *,
    start: date,
    end: date | None,
) -> bool:
    if end is None:
        return False
    return (
        _strict_date(item.get("start"), field="interval.start") <= end
        and _strict_date(item.get("end"), field="interval.end") >= start
    )


def _interval_reason_codes(items: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item.get("reason_code") or "earnings_calendar_coverage_incomplete")
            for item in items
        }
    )


def _first_interval_reason(items: list[Mapping[str, Any]]) -> str:
    if not items:
        return "earnings_calendar_coverage_incomplete"
    return str(
        items[0].get("reason_code") or "earnings_calendar_coverage_incomplete"
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


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


def load_earnings_evidence_for_candidate(
    *,
    input_root: Path,
    market: str,
    symbol: str,
    expiration: str,
) -> dict[str, Any]:
    """Read one immutable run-shared OpenD earnings projection fail closed."""

    market_norm = str(market or "").strip().upper()
    path = Path(input_root) / "earnings_calendar" / f"{market_norm}.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _candidate_earnings_unavailable(
            "earnings_calendar_snapshot_missing",
            artifact_path=path,
        )
    except Exception as exc:
        return _candidate_earnings_unavailable(
            "earnings_calendar_snapshot_invalid",
            artifact_path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(snapshot, dict):
        return _candidate_earnings_unavailable(
            "earnings_calendar_snapshot_invalid",
            artifact_path=path,
        )
    recorded_hash = str(snapshot.get("snapshot_hash") or "")
    try:
        validate_earnings_calendar_snapshot(
            snapshot,
            expected_market=market_norm,
        )
    except Exception as exc:
        return _candidate_earnings_unavailable(
            "earnings_calendar_snapshot_identity_invalid",
            artifact_path=path,
            snapshot_hash=recorded_hash or None,
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        identity = resolve_symbol_identity(symbol)
        if identity is None or identity.market != market_norm:
            raise ValueError("symbol market mismatch")
        projection = _project_earnings_for_expiry(
            snapshot,
            underlier_code=identity.futu_code,
            expiration=str(expiration or "").strip(),
        )
    except Exception as exc:
        return _candidate_earnings_unavailable(
            "earnings_calendar_candidate_identity_invalid",
            artifact_path=path,
            snapshot_hash=recorded_hash,
            error=f"{type(exc).__name__}: {exc}",
        )
    status = str(projection.get("status") or "").strip().lower()
    events = [
        dict(item)
        for item in projection.get("events", [])
        if isinstance(item, Mapping)
    ]
    has_event = bool(projection.get("has_earnings_event"))
    if status != "ready":
        return _candidate_earnings_unavailable(
            str(projection.get("reason_code") or "earnings_calendar_coverage_incomplete"),
            artifact_path=path,
            snapshot_hash=recorded_hash,
            projection=projection,
        )
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": projection["policy_version"],
        "earnings_window_days": projection["window_days"],
        "earnings_market_date": projection["market_date"],
        "earnings_hard_window_start": projection["hard_window_start"],
        "earnings_hard_window_end": projection["hard_window_end"],
        "earnings_hard_coverage_status": projection["hard_coverage_status"],
        "earnings_hard_reason_codes": list(projection["hard_reason_codes"]),
        "earnings_hard_failed_intervals": list(
            projection["hard_failed_intervals"]
        ),
        "earnings_soft_window_start": projection["soft_window_start"],
        "earnings_soft_window_end": projection["soft_window_end"],
        "earnings_soft_coverage_status": projection["soft_coverage_status"],
        "earnings_soft_reason_codes": list(projection["soft_reason_codes"]),
        "earnings_soft_failed_intervals": list(
            projection["soft_failed_intervals"]
        ),
        "earnings_has_event": has_event,
        "earnings_blocking_has_event": bool(
            projection["has_blocking_earnings_event"]
        ),
        "earnings_event_dates": ",".join(
            sorted({str(item.get("earnings_date") or "") for item in events if item.get("earnings_date")})
        ),
        "earnings_blocking_event_dates": ",".join(
            str(item["earnings_date"]) for item in projection["blocking_events"]
        ),
        "earnings_nonblocking_event_dates": ",".join(
            str(item["earnings_date"])
            for item in projection["nonblocking_events"]
        ),
        "earnings_events": events,
        "earnings_blocking_events": list(projection["blocking_events"]),
        "earnings_nonblocking_events": list(projection["nonblocking_events"]),
        "earnings_snapshot_hash": recorded_hash,
        "earnings_artifact_path": path.as_posix(),
    }


def annotate_candidates_with_earnings_evidence(
    candidates: Any,
    *,
    input_root: Path,
) -> Any:
    """Attach the single formal earnings truth to candidate rows."""

    import pandas as pd

    if candidates is None or not isinstance(candidates, pd.DataFrame) or candidates.empty:
        return candidates
    rows: list[dict[str, Any]] = []
    for row in candidates.to_dict("records"):
        payload = dict(row)
        payload.update(
            load_earnings_evidence_for_candidate(
                input_root=input_root,
                market=str(payload.get("market") or ""),
                symbol=str(payload.get("symbol") or ""),
                expiration=str(payload.get("expiration") or ""),
            )
        )
        rows.append(payload)
    out = pd.DataFrame(rows)
    for field in (name for name in out.columns if name.startswith("earnings_")):
        values = [row.get(field) for row in rows]
        if any(value is None for value in values):
            out[field] = pd.Series(values, dtype=object)
    return out


def _candidate_earnings_unavailable(
    reason_code: str,
    *,
    artifact_path: Path,
    snapshot_hash: str | None = None,
    error: str | None = None,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projected = dict(projection or {})
    return {
        "earnings_evidence_status": "data_unavailable",
        "earnings_reason_code": str(reason_code),
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": projected.get("market_date"),
        "earnings_hard_window_start": projected.get("hard_window_start"),
        "earnings_hard_window_end": projected.get("hard_window_end"),
        "earnings_hard_coverage_status": projected.get(
            "hard_coverage_status",
            "unavailable",
        ),
        "earnings_hard_reason_codes": list(
            projected.get("hard_reason_codes") or [str(reason_code)]
        ),
        "earnings_hard_failed_intervals": list(
            projected.get("hard_failed_intervals") or []
        ),
        "earnings_soft_window_start": projected.get("soft_window_start"),
        "earnings_soft_window_end": projected.get("soft_window_end"),
        "earnings_soft_coverage_status": projected.get(
            "soft_coverage_status",
            "unavailable",
        ),
        "earnings_soft_reason_codes": list(
            projected.get("soft_reason_codes") or []
        ),
        "earnings_soft_failed_intervals": list(
            projected.get("soft_failed_intervals") or []
        ),
        "earnings_has_event": None,
        "earnings_blocking_has_event": None,
        "earnings_event_dates": "",
        "earnings_blocking_event_dates": "",
        "earnings_nonblocking_event_dates": "",
        "earnings_events": [],
        "earnings_blocking_events": [],
        "earnings_nonblocking_events": [],
        "earnings_snapshot_hash": snapshot_hash,
        "earnings_artifact_path": Path(artifact_path).as_posix(),
        "earnings_error": error,
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
