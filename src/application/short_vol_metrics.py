from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.infrastructure.io_utils import atomic_write_json


TRADING_DAYS_PER_YEAR = 252
DEFAULT_RV_WINDOWS = (20, 60, 120)
RV_DTE_WEIGHTS: tuple[tuple[int, tuple[tuple[int, float], ...]], ...] = (
    (30, ((20, 0.70), (60, 0.30))),
    (60, ((20, 0.30), (60, 0.50), (120, 0.20))),
    (90, ((20, 0.20), (60, 0.40), (120, 0.40))),
)
TERM_MATCHED_RV_SCHEMA = "term_matched_rv.v1"
QFQ_HISTORY_CACHE_SCHEMA = "opend_qfq_history_cache.v1"
QFQ_HISTORY_AUTYPE = "QFQ"
QFQ_RECHECK_SESSIONS = 5


@dataclass(frozen=True)
class TermMatchedRVObservation:
    schema_version: str
    expiration: str
    status: str
    reason: str | None
    term_matched_rv: float | None
    remaining_sessions: int | None
    lookback_sessions: int | None
    input_start: str | None
    input_end: str | None
    input_close_session_count: int
    input_return_count: int
    input_hash: str | None
    missing_sessions: tuple[str, ...] = ()
    legacy_weighted_rv: float | None = None
    shadow_difference: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missing_sessions"] = list(self.missing_sessions)
        return payload


@dataclass(frozen=True)
class RealizedVolatilitySnapshot:
    rv_20: float | None = None
    rv_60: float | None = None
    rv_120: float | None = None
    rv_estimate: float | None = None
    sample_count: int = 0
    status: str = "missing"
    reason: str | None = None
    term_matched: dict[str, TermMatchedRVObservation] = field(default_factory=dict)
    qfq_history_evidence: dict[str, Any] = field(default_factory=dict)
    trading_calendar_evidence: dict[str, Any] = field(default_factory=dict)

    def to_row_fields(
        self,
        *,
        dte: int | None = None,
        expiration: str | None = None,
    ) -> dict[str, Any]:
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
        fields = {
            "realized_volatility_20": self.rv_20,
            "realized_volatility_60": self.rv_60,
            "realized_volatility_120": self.rv_120,
            "realized_volatility_estimate": estimate,
        }
        term = self.term_matched.get(str(expiration or ""))
        if term is not None:
            fields.update(
                {
                    "term_matched_rv": term.term_matched_rv,
                    "term_matched_rv_status": term.status,
                    "term_matched_rv_reason": term.reason,
                    "term_matched_rv_remaining_sessions": term.remaining_sessions,
                    "term_matched_rv_lookback_sessions": term.lookback_sessions,
                    "term_matched_rv_input_start": term.input_start,
                    "term_matched_rv_input_end": term.input_end,
                    "term_matched_rv_input_session_count": (
                        term.input_close_session_count
                    ),
                    "term_matched_rv_input_hash": term.input_hash,
                    "term_matched_rv_legacy_shadow": term.legacy_weighted_rv,
                    "term_matched_rv_shadow_difference": term.shadow_difference,
                }
            )
        return fields

    def to_meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "sample_count": self.sample_count,
            **self.to_row_fields(),
            "estimation_policy": "term_matched_sessions_v1",
            "term_matched": {
                expiration: observation.to_dict()
                for expiration, observation in sorted(self.term_matched.items())
            },
            "qfq_history": dict(self.qfq_history_evidence),
            "trading_calendar": dict(self.trading_calendar_evidence),
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
    market: str | None = None,
    expirations: Iterable[str] | None = None,
    base_dir: Path | None = None,
    lookback_calendar_days: int = 240,
) -> RealizedVolatilitySnapshot:
    expiration_dates = _normalize_expiration_dates(expirations)
    if not expiration_dates or not str(market or "").strip():
        return _fetch_legacy_diagnostic_snapshot(
            gateway,
            underlier_code=underlier_code,
            trading_day=trading_day,
            lookback_calendar_days=lookback_calendar_days,
        )

    market_code = str(market or "").strip().upper()
    max_remaining_calendar_days = max(
        0,
        (max(expiration_dates.values()) - trading_day).days,
    )
    history_calendar_days = max(
        240,
        int(lookback_calendar_days),
        (max(20, max_remaining_calendar_days) * 2) + 30,
    )
    history_start = trading_day - timedelta(days=history_calendar_days)
    calendar_end = max(expiration_dates.values())

    try:
        calendar_rows = gateway.get_trading_days(
            market=market_code,
            start=history_start.isoformat(),
            end=calendar_end.isoformat(),
        )
        calendar_dates = _trading_calendar_dates(calendar_rows)
    except Exception as exc:
        return _unavailable_term_snapshot(
            expiration_dates,
            reason=f"trading_calendar_error:{type(exc).__name__}",
            calendar_evidence={
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "market": market_code,
                "start": history_start.isoformat(),
                "end": calendar_end.isoformat(),
            },
        )

    calendar_evidence = {
        "status": "ok",
        "market": market_code,
        "start": history_start.isoformat(),
        "end": calendar_end.isoformat(),
        "session_count": len(calendar_dates),
        "input_hash": _hash_values([item.isoformat() for item in calendar_dates]),
    }
    if not calendar_dates:
        return _unavailable_term_snapshot(
            expiration_dates,
            reason="trading_calendar_empty",
            calendar_evidence=calendar_evidence,
        )

    try:
        history_rows, history_evidence = _load_refresh_qfq_history(
            gateway,
            market=market_code,
            underlier_code=str(underlier_code),
            trading_day=trading_day,
            history_start=history_start,
            base_dir=base_dir,
        )
    except Exception as exc:
        return _unavailable_term_snapshot(
            expiration_dates,
            reason=f"qfq_history_error:{type(exc).__name__}",
            calendar_evidence=calendar_evidence,
            history_evidence={
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "market": market_code,
                "underlier_code": str(underlier_code),
                "autype": QFQ_HISTORY_AUTYPE,
            },
        )

    diagnostic = compute_realized_volatility_snapshot(history_rows)
    term_matched = _compute_term_matched_observations(
        expiration_dates=expiration_dates,
        trading_day=trading_day,
        calendar_dates=calendar_dates,
        history_rows=history_rows,
        diagnostic=diagnostic,
    )
    missing = [
        item.expiration
        for item in term_matched.values()
        if item.status != "ok"
    ]
    status = "ok" if not missing else "partial"
    reason = None if not missing else "term_matched_rv_incomplete"
    return RealizedVolatilitySnapshot(
        rv_20=diagnostic.rv_20,
        rv_60=diagnostic.rv_60,
        rv_120=diagnostic.rv_120,
        rv_estimate=None,
        sample_count=diagnostic.sample_count,
        status=status,
        reason=reason,
        term_matched=term_matched,
        qfq_history_evidence=history_evidence,
        trading_calendar_evidence=calendar_evidence,
    )


def qfq_history_cache_path(
    base_dir: Path,
    *,
    market: str,
    underlier_code: str,
) -> Path:
    safe_market = str(market or "").strip().upper() or "UNKNOWN"
    safe_underlier = str(underlier_code or "").strip().upper().replace(".", "_")
    return (
        Path(base_dir)
        / "cache"
        / "opend_qfq_history"
        / safe_market
        / f"{safe_underlier}.json"
    )


def _fetch_legacy_diagnostic_snapshot(
    gateway: Any,
    *,
    underlier_code: str,
    trading_day: date,
    lookback_calendar_days: int,
) -> RealizedVolatilitySnapshot:
    start = trading_day - timedelta(days=max(140, int(lookback_calendar_days)))
    try:
        data = _fetch_qfq_history_rows(
            gateway,
            underlier_code=underlier_code,
            start=start,
            end=trading_day,
        )
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
        return RealizedVolatilitySnapshot(
            status="error",
            reason=f"{type(exc).__name__}: {exc}",
        )


def _normalize_expiration_dates(
    values: Iterable[str] | None,
) -> dict[str, date]:
    normalized: dict[str, date] = {}
    for value in values or ():
        raw = str(value or "").strip()[:10]
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            continue
        normalized[parsed.isoformat()] = parsed
    return dict(sorted(normalized.items()))


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        value = value["rows"]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict("records")
        except Exception:
            return []
        return [dict(item) for item in rows if isinstance(item, dict)]
    return []


def _trading_calendar_dates(value: Any) -> list[date]:
    dates: set[date] = set()
    for row in _rows(value):
        raw = str(
            row.get("time")
            or row.get("date")
            or row.get("trade_date")
            or ""
        ).strip()[:10]
        kind = str(row.get("trade_date_type") or "").strip().upper()
        if kind and kind not in {"WHOLE", "MORNING", "AFTERNOON", "TRADING"}:
            continue
        try:
            dates.add(date.fromisoformat(raw))
        except ValueError:
            continue
    return sorted(dates)


def _fetch_qfq_history_rows(
    gateway: Any,
    *,
    underlier_code: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    raw_rows: list[dict[str, Any]] = []
    page_req_key = None
    for _ in range(20):
        result = gateway.request_history_kline(
            code=str(underlier_code),
            start=start.isoformat(),
            end=end.isoformat(),
            ktype="K_DAY",
            autype=QFQ_HISTORY_AUTYPE,
            fields=["time_key", "close"],
            max_count=300,
            page_req_key=page_req_key,
        )
        chunk = result.get("data") if isinstance(result, dict) else result
        raw_rows.extend(_rows(chunk))
        next_key = result.get("page_req_key") if isinstance(result, dict) else None
        if next_key in (None, ""):
            break
        if next_key == page_req_key:
            raise RuntimeError("history kline pagination did not advance")
        page_req_key = next_key
    else:
        raise RuntimeError("history kline pagination exceeded 20 pages")
    return _normalize_history_rows(
        raw_rows,
        start=start,
        completed_before=end,
    )


def _normalize_history_rows(
    rows: Iterable[dict[str, Any]],
    *,
    start: date,
    completed_before: date,
) -> list[dict[str, Any]]:
    by_date: dict[date, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(
            row.get("time_key")
            or row.get("time")
            or row.get("date")
            or row.get("trade_date")
            or ""
        ).strip()[:10]
        try:
            session = date.fromisoformat(raw_date)
        except ValueError:
            continue
        close = _first_float(row, "close", "close_price")
        if (
            session < start
            or session >= completed_before
            or close is None
            or close <= 0
        ):
            continue
        existing = by_date.get(session)
        if existing is not None and not math.isclose(
            existing,
            close,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(f"conflicting QFQ close for {session.isoformat()}")
        by_date[session] = float(close)
    return [
        {"date": session.isoformat(), "close": by_date[session]}
        for session in sorted(by_date)
    ]


def _load_refresh_qfq_history(
    gateway: Any,
    *,
    market: str,
    underlier_code: str,
    trading_day: date,
    history_start: date,
    base_dir: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache_path = (
        qfq_history_cache_path(
            base_dir,
            market=market,
            underlier_code=underlier_code,
        )
        if base_dir is not None
        else None
    )
    cached_rows, cached_covered_from = _load_qfq_history_cache(
        cache_path,
        market=market,
        underlier_code=underlier_code,
        completed_before=trading_day,
    )
    cached_dates = [date.fromisoformat(str(item["date"])) for item in cached_rows]
    cache_lacks_required_horizon = bool(
        cached_dates
        and (
            cached_covered_from is None
            or cached_covered_from > history_start
        )
    )
    refresh_start = history_start
    if len(cached_dates) >= QFQ_RECHECK_SESSIONS and not cache_lacks_required_horizon:
        refresh_start = max(history_start, cached_dates[-QFQ_RECHECK_SESSIONS])
    refreshed = _fetch_qfq_history_rows(
        gateway,
        underlier_code=underlier_code,
        start=refresh_start,
        end=trading_day,
    )
    revision_detected = _history_revision_detected(
        cached_rows,
        refreshed,
        overlap_start=refresh_start,
    )
    if revision_detected and refresh_start > history_start:
        refresh_start = history_start
        refreshed = _fetch_qfq_history_rows(
            gateway,
            underlier_code=underlier_code,
            start=history_start,
            end=trading_day,
        )
    cached_merged = _merge_history_rows(
        ([] if revision_detected else cached_rows),
        refreshed,
        replace_from=(date.min if revision_detected else refresh_start),
        completed_before=trading_day,
    )
    required_rows = _normalize_history_rows(
        cached_merged,
        start=history_start,
        completed_before=trading_day,
    )
    cache_status = "disabled"
    covered_from = (
        history_start
        if revision_detected or cached_covered_from is None
        else min(cached_covered_from, history_start)
    )
    if cache_path is not None:
        atomic_write_json(
            cache_path,
            {
                "schema_version": QFQ_HISTORY_CACHE_SCHEMA,
                "market": market,
                "underlier_code": underlier_code,
                "autype": QFQ_HISTORY_AUTYPE,
                "covered_from": covered_from.isoformat(),
                "completed_before": trading_day.isoformat(),
                "rows": cached_merged,
                "rows_hash": _hash_values(cached_merged),
            },
            sort_keys=True,
        )
        cache_status = "refreshed" if cached_rows else "created"
    evidence = {
        "status": "ok",
        "market": market,
        "underlier_code": underlier_code,
        "autype": QFQ_HISTORY_AUTYPE,
        "cache_identity": f"{market}:{underlier_code}:{QFQ_HISTORY_AUTYPE}",
        "cache_status": cache_status,
        "refresh_start": refresh_start.isoformat(),
        "completed_before": trading_day.isoformat(),
        "revision_detected": revision_detected,
        "session_count": len(required_rows),
        "input_hash": _hash_values(required_rows),
        "cached_session_count": len(cached_merged),
        "cache_hash": _hash_values(cached_merged),
    }
    return required_rows, evidence


def _load_qfq_history_cache(
    path: Path | None,
    *,
    market: str,
    underlier_code: str,
    completed_before: date,
) -> tuple[list[dict[str, Any]], date | None]:
    if path is None or not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != QFQ_HISTORY_CACHE_SCHEMA
        or str(payload.get("market") or "").strip().upper() != market
        or str(payload.get("underlier_code") or "").strip().upper()
        != underlier_code.upper()
        or str(payload.get("autype") or "").strip().upper()
        != QFQ_HISTORY_AUTYPE
        or not isinstance(payload.get("rows"), list)
        or payload.get("rows_hash") != _hash_values(payload.get("rows"))
    ):
        return [], None
    try:
        covered_from = date.fromisoformat(str(payload.get("covered_from") or ""))
    except ValueError:
        covered_from = None
    return (
        _normalize_history_rows(
            payload["rows"],
            start=date.min,
            completed_before=completed_before,
        ),
        covered_from,
    )


def _history_revision_detected(
    cached: list[dict[str, Any]],
    refreshed: list[dict[str, Any]],
    *,
    overlap_start: date,
) -> bool:
    cached_map = {
        str(item.get("date")): float(item["close"])
        for item in cached
        if date.fromisoformat(str(item.get("date"))) >= overlap_start
    }
    refreshed_map = {
        str(item.get("date")): float(item["close"])
        for item in refreshed
    }
    overlap = set(cached_map).intersection(refreshed_map)
    return any(
        not math.isclose(
            cached_map[key],
            refreshed_map[key],
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for key in overlap
    )


def _merge_history_rows(
    cached: list[dict[str, Any]],
    refreshed: list[dict[str, Any]],
    *,
    replace_from: date,
    completed_before: date,
) -> list[dict[str, Any]]:
    retained = [
        item
        for item in cached
        if date.fromisoformat(str(item["date"])) < replace_from
    ]
    return _normalize_history_rows(
        [*retained, *refreshed],
        start=date.min,
        completed_before=completed_before,
    )


def _compute_term_matched_observations(
    *,
    expiration_dates: dict[str, date],
    trading_day: date,
    calendar_dates: list[date],
    history_rows: list[dict[str, Any]],
    diagnostic: RealizedVolatilitySnapshot,
) -> dict[str, TermMatchedRVObservation]:
    close_by_date = {
        date.fromisoformat(str(item["date"])): float(item["close"])
        for item in history_rows
    }
    completed_sessions = [item for item in calendar_dates if item < trading_day]
    observations: dict[str, TermMatchedRVObservation] = {}
    for expiration, expiration_date in expiration_dates.items():
        remaining = [
            item
            for item in calendar_dates
            if trading_day <= item <= expiration_date
        ]
        remaining_count = len(remaining)
        lookback = max(20, remaining_count)
        legacy = realized_volatility_estimate_for_dte(
            dte=(expiration_date - trading_day).days,
            rv_20=diagnostic.rv_20,
            rv_60=diagnostic.rv_60,
            rv_120=diagnostic.rv_120,
        )
        if expiration_date < trading_day or not remaining:
            observations[expiration] = _missing_term_observation(
                expiration=expiration,
                reason="future_trading_sessions_missing",
                remaining_sessions=remaining_count,
                lookback_sessions=lookback,
                legacy_weighted_rv=legacy,
            )
            continue
        required_sessions = completed_sessions[-(lookback + 1) :]
        if len(required_sessions) < lookback + 1:
            observations[expiration] = _missing_term_observation(
                expiration=expiration,
                reason="historical_calendar_horizon_insufficient",
                remaining_sessions=remaining_count,
                lookback_sessions=lookback,
                legacy_weighted_rv=legacy,
            )
            continue
        missing_sessions = tuple(
            item.isoformat()
            for item in required_sessions
            if item not in close_by_date
        )
        if missing_sessions:
            observations[expiration] = _missing_term_observation(
                expiration=expiration,
                reason="qfq_history_session_gap",
                remaining_sessions=remaining_count,
                lookback_sessions=lookback,
                missing_sessions=missing_sessions,
                legacy_weighted_rv=legacy,
            )
            continue
        input_rows = [
            {"date": item.isoformat(), "close": close_by_date[item]}
            for item in required_sessions
        ]
        returns = [
            math.log(float(cur["close"]) / float(prev["close"]))
            for prev, cur in zip(input_rows[:-1], input_rows[1:])
        ]
        rv = _annualized_std(returns)
        if rv is None:
            observations[expiration] = _missing_term_observation(
                expiration=expiration,
                reason="term_matched_returns_insufficient",
                remaining_sessions=remaining_count,
                lookback_sessions=lookback,
                legacy_weighted_rv=legacy,
            )
            continue
        rounded_rv = _round_optional(rv)
        observations[expiration] = TermMatchedRVObservation(
            schema_version=TERM_MATCHED_RV_SCHEMA,
            expiration=expiration,
            status="ok",
            reason=None,
            term_matched_rv=rounded_rv,
            remaining_sessions=remaining_count,
            lookback_sessions=lookback,
            input_start=str(input_rows[0]["date"]),
            input_end=str(input_rows[-1]["date"]),
            input_close_session_count=len(input_rows),
            input_return_count=len(returns),
            input_hash=_hash_values(input_rows),
            legacy_weighted_rv=legacy,
            shadow_difference=(
                _round_optional(float(rounded_rv) - float(legacy))
                if rounded_rv is not None and legacy is not None
                else None
            ),
        )
    return observations


def _missing_term_observation(
    *,
    expiration: str,
    reason: str,
    remaining_sessions: int | None,
    lookback_sessions: int | None,
    missing_sessions: tuple[str, ...] = (),
    legacy_weighted_rv: float | None = None,
) -> TermMatchedRVObservation:
    return TermMatchedRVObservation(
        schema_version=TERM_MATCHED_RV_SCHEMA,
        expiration=expiration,
        status="data_unavailable",
        reason=reason,
        term_matched_rv=None,
        remaining_sessions=remaining_sessions,
        lookback_sessions=lookback_sessions,
        input_start=None,
        input_end=None,
        input_close_session_count=0,
        input_return_count=0,
        input_hash=None,
        missing_sessions=missing_sessions,
        legacy_weighted_rv=legacy_weighted_rv,
    )


def _unavailable_term_snapshot(
    expiration_dates: dict[str, date],
    *,
    reason: str,
    calendar_evidence: dict[str, Any] | None = None,
    history_evidence: dict[str, Any] | None = None,
) -> RealizedVolatilitySnapshot:
    return RealizedVolatilitySnapshot(
        status="error",
        reason=reason,
        term_matched={
            expiration: _missing_term_observation(
                expiration=expiration,
                reason=reason,
                remaining_sessions=None,
                lookback_sessions=None,
            )
            for expiration in expiration_dates
        },
        qfq_history_evidence=dict(history_evidence or {}),
        trading_calendar_evidence=dict(calendar_evidence or {}),
    )


def _hash_values(value: Any) -> str:
    canonical = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
