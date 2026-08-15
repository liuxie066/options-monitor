from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.opening_quote_evidence import (
    OpeningUnderlierObservation,
    normalize_underlier_observation,
)
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import (
    DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT,
    OpenDEndpointRateLimit,
    OpenDFetchLimits,
)
from src.application.opend_utils import normalize_underlier
from src.application.option_chain_fetching import classify_option_chain_error
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway, retry_futu_gateway_call


REPO_ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT_KEEP_COLUMNS = [
    "code",
    "last_price",
    "bid_price",
    "ask_price",
    "ask_vol",
    "bid_vol",
    "volume",
    "option_open_interest",
    "option_implied_volatility",
    "option_delta",
    "option_contract_multiplier",
    "option_contract_size",
    "lot_size",
    "price_spread",
    "stock_owner",
    "stock_type",
    "suspension",
    "sec_status",
    "open_interest",
    "implied_volatility",
    "delta",
    "bid",
    "ask",
    "effective_at_ms",
    "snapshot_time_ms",
    "timestamp_ms",
    "update_time_ms",
    "timestamp",
    "update_time",
    "data_time",
    "time",
]


@dataclass(frozen=True)
class MarketSnapshotFetchResult:
    snap_map: dict[str, dict[str, Any]]
    errors: list[dict[str, Any]]
    requested_codes: frozenset[str]
    returned_codes: frozenset[str]
    missing_codes: frozenset[str]
    unexpected_codes: frozenset[str]
    complete: bool
    fallback_filled: int = 0
    fallback_failed: int = 0
    opend_call_count: int = 0
    requested_at_utc: str | None = None
    received_at_utc: str | None = None

    @property
    def requested_codes_count(self) -> int:
        return len(self.requested_codes)

    @property
    def returned_codes_count(self) -> int:
        return len(self.returned_codes)

    @property
    def missing_codes_count(self) -> int:
        return len(self.missing_codes)

    @property
    def unexpected_codes_count(self) -> int:
        return len(self.unexpected_codes)


def get_spot_opend(
    gateway: Any,
    underlier_code: str,
    *,
    base_dir: Path | None = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
    errors: list[dict[str, Any]] | None = None,
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    metrics: dict[str, Any] | None = None,
) -> float | None:
    """Try to get underlying spot from OpenD."""
    snapshot_limit = OpenDFetchLimits.from_flat_kwargs(
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    ).market_snapshot
    try:
        def _call_snapshot() -> Any:
            _increment_metric(metrics, "spot_snapshot_opend_calls")
            _increment_metric(metrics, "spot_snapshot_requested_codes")
            return gateway.get_snapshot([underlier_code])

        if base_dir is not None:
            df = rate_limited_call(
                base_dir=Path(base_dir),
                endpoint="market_snapshot",
                **snapshot_limit.call_kwargs(),
                call=_call_snapshot,
            )
        else:
            df = _call_snapshot()
        if df is None or df.empty:
            _append_opend_observation_error(
                errors,
                stage="underlier_snapshot",
                code=underlier_code,
                error_code="EMPTY_SNAPSHOT",
                message="empty underlier snapshot",
            )
            return None
        row = df.iloc[0]
        for key in ["last_price", "price", "cur_price", "close_price_5min", "open_price", "prev_close_price"]:
            value = _to_float(row.get(key))
            if value is not None and value > 0:
                return value
        _append_opend_observation_error(
            errors,
            stage="underlier_snapshot",
            code=underlier_code,
            error_code="MISSING_PRICE",
            message="underlier snapshot has no positive price field",
        )
        return None
    except Exception as exc:
        _append_opend_observation_error(
            errors,
            stage="underlier_snapshot",
            code=underlier_code,
            error_code=classify_option_chain_error(exc),
            message=str(exc),
        )
        return None


def get_underlier_observation_opend(
    gateway: Any,
    underlier_code: str,
    *,
    market: str,
    base_dir: Path | None = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
    errors: list[dict[str, Any]] | None = None,
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    metrics: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> OpeningUnderlierObservation:
    """Fetch one run-scoped OpenD spot observation without fallback prices."""

    snapshot_limit = OpenDFetchLimits.from_flat_kwargs(
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    ).market_snapshot

    def _limited(call: Callable[[], Any]) -> Any:
        if base_dir is None:
            return call()
        return rate_limited_call(
            base_dir=Path(base_dir),
            endpoint="market_snapshot",
            **snapshot_limit.call_kwargs(),
            call=call,
        )

    snapshot_row: dict[str, Any] | None = None
    market_state_row: dict[str, Any] | None = None
    try:
        def _snapshot_call() -> Any:
            _increment_metric(metrics, "spot_snapshot_opend_calls")
            _increment_metric(metrics, "spot_snapshot_requested_codes")
            return gateway.get_snapshot([underlier_code])

        snapshot = _limited(_snapshot_call)
        snapshot_row = _single_provider_row(snapshot, expected_code=underlier_code)
        if snapshot_row is None:
            _append_opend_observation_error(
                errors,
                stage="underlier_snapshot",
                code=underlier_code,
                error_code="UNDERLIER_SNAPSHOT_INVALID",
                message="underlier snapshot does not contain exactly one requested row",
            )
    except Exception as exc:
        _append_opend_observation_error(
            errors,
            stage="underlier_snapshot",
            code=underlier_code,
            error_code=classify_option_chain_error(exc),
            message=str(exc),
        )

    try:
        def _market_state_call() -> Any:
            _increment_metric(metrics, "spot_market_state_opend_calls")
            _increment_metric(metrics, "spot_market_state_requested_codes")
            return gateway.get_market_state([underlier_code])

        market_state = _limited(_market_state_call)
        market_state_row = _single_provider_row(
            market_state,
            expected_code=underlier_code,
        )
        if market_state_row is None:
            _append_opend_observation_error(
                errors,
                stage="underlier_market_state",
                code=underlier_code,
                error_code="UNDERLIER_MARKET_STATE_INVALID",
                message="market-state response does not contain exactly one requested row",
            )
    except Exception as exc:
        _append_opend_observation_error(
            errors,
            stage="underlier_market_state",
            code=underlier_code,
            error_code=classify_option_chain_error(exc),
            message=str(exc),
        )

    return normalize_underlier_observation(
        code=underlier_code,
        market=market,
        snapshot_row=snapshot_row,
        market_state_row=market_state_row,
        now_utc=now_utc,
    )


def _single_provider_row(value: Any, *, expected_code: str) -> dict[str, Any] | None:
    if value is None:
        return None
    rows: list[dict[str, Any]] = []
    if isinstance(value, list):
        rows = [dict(item) for item in value if isinstance(item, dict)]
    elif hasattr(value, "to_dict"):
        try:
            raw_rows = value.to_dict(orient="records")
        except TypeError:
            raw_rows = value.to_dict("records")
        if isinstance(raw_rows, list):
            rows = [dict(item) for item in raw_rows if isinstance(item, dict)]
    matches = [
        row
        for row in rows
        if str(row.get("code") or "").strip().upper()
        == str(expected_code or "").strip().upper()
    ]
    return matches[0] if len(matches) == 1 else None


def _increment_metric(metrics: dict[str, Any] | None, key: str, value: int = 1) -> None:
    if metrics is None:
        return
    try:
        metrics[key] = int(metrics.get(key) or 0) + int(value)
    except Exception:
        metrics[key] = int(value)


def get_underlier_spot(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    base_dir: Path | None = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
) -> float | None:
    gateway = build_ready_futu_quote_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        effective_base_dir = Path(base_dir) if base_dir is not None else REPO_ROOT
        return get_spot_opend(
            gateway,
            normalize_underlier(symbol, base_dir=effective_base_dir).code,
            base_dir=effective_base_dir,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
            rate_limited_call=rate_limited_opend_call,
        )
    finally:
        try:
            gateway.close()
        except Exception:
            pass


def get_underlier_observation(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    base_dir: Path | None = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
) -> OpeningUnderlierObservation:
    effective_base_dir = Path(base_dir) if base_dir is not None else REPO_ROOT
    underlier = normalize_underlier(symbol, base_dir=effective_base_dir)
    gateway = build_ready_futu_quote_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        return get_underlier_observation_opend(
            gateway,
            underlier.code,
            market=underlier.market,
            base_dir=effective_base_dir,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
            rate_limited_call=rate_limited_opend_call,
        )
    finally:
        try:
            gateway.close()
        except Exception:
            pass


def fetch_option_snapshots(
    *,
    option_codes: list[str],
    gateway: Any,
    snapshot_limit: OpenDEndpointRateLimit,
    base_dir: Path,
    snapshot_batch_size: int | None = None,
    snapshot_fallback_max_codes: int = 100,
    snapshot_fallback_batch_size: int = 20,
    no_retry: bool = False,
    retry_max_attempts: int = 4,
    retry_time_budget_sec: float = 8.0,
    retry_base_delay_sec: float = 0.8,
    retry_max_delay_sec: float = 6.0,
    retry_call: Callable[..., Any] = retry_futu_gateway_call,
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    classify_error: Callable[[Any], str] = classify_option_chain_error,
) -> MarketSnapshotFetchResult:
    snap_map: dict[str, dict[str, Any]] = {}
    snapshot_errors: list[dict[str, Any]] = []
    batch_requested_at: list[datetime] = []
    batch_received_at: list[datetime] = []
    requested_code_order = tuple(dict.fromkeys(
        code
        for raw_code in option_codes
        if (code := str(raw_code or "").strip())
    ))
    requested_codes = frozenset(requested_code_order)
    returned_codes: set[str] = set()
    duplicate_codes: set[str] = set()
    batch_size = int(snapshot_batch_size) if snapshot_batch_size else DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
    batch_size = max(1, batch_size)
    keep_columns = list(SNAPSHOT_KEEP_COLUMNS)
    opend_call_count = 0

    for start in range(0, len(requested_code_order), batch_size):
        batch = list(requested_code_order[start : start + batch_size])
        batch_requested_at.append(datetime.now(timezone.utc))
        try:
            def _call_snapshot(batch0: list[str] = batch) -> Any:
                def _gateway_snapshot_call() -> Any:
                    nonlocal opend_call_count
                    opend_call_count += 1
                    return gateway.get_snapshot(batch0)

                return rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    call=_gateway_snapshot_call,
                )

            snap = retry_call(
                "get_market_snapshot(batch)",
                _call_snapshot,
                no_retry=no_retry,
                retry_max_attempts=retry_max_attempts,
                retry_time_budget_sec=retry_time_budget_sec,
                retry_base_delay_sec=retry_base_delay_sec,
                retry_max_delay_sec=retry_max_delay_sec,
                quiet=True,
            )
        except Exception as exc:
            snapshot_errors.append(
                {
                    "stage": "market_snapshot",
                    "batch_start": start,
                    "batch_size": len(batch),
                    "error_code": classify_error(exc),
                    "message": str(exc),
                }
            )
            snap = None
        batch_received_at.append(datetime.now(timezone.utc))
        if snap is None or snap.empty:
            continue

        records, keep = keep_snapshot_record_columns(snap, keep_columns)
        if not keep:
            continue

        _merge_snapshot_records(
            records=records,
            requested_codes=requested_codes,
            returned_codes=returned_codes,
            duplicate_codes=duplicate_codes,
            snap_map=snap_map,
        )

    fallback_filled = 0
    fallback_failed = 0
    if requested_code_order and int(snapshot_fallback_max_codes) > 0:
        missing = [code for code in requested_code_order if code not in snap_map]
        if missing:
            (
                fallback_filled,
                fallback_failed,
                fallback_opend_calls,
                fallback_requested_at,
                fallback_received_at,
            ) = _fallback_fetch_missing_snapshots(
                missing_codes=missing,
                gateway=gateway,
                snapshot_limit=snapshot_limit,
                base_dir=base_dir,
                snap_map=snap_map,
                snapshot_errors=snapshot_errors,
                max_fallback_codes=snapshot_fallback_max_codes,
                fallback_batch_size=snapshot_fallback_batch_size,
                keep_columns=keep_columns,
                no_retry=no_retry,
                retry_max_attempts=retry_max_attempts,
                retry_time_budget_sec=retry_time_budget_sec,
                retry_base_delay_sec=retry_base_delay_sec,
                retry_max_delay_sec=retry_max_delay_sec,
                retry_call=retry_call,
                rate_limited_call=rate_limited_call,
                requested_codes=requested_codes,
                returned_codes=returned_codes,
                duplicate_codes=duplicate_codes,
            )
            opend_call_count += fallback_opend_calls
            batch_requested_at.extend(fallback_requested_at)
            batch_received_at.extend(fallback_received_at)

    returned_code_set = frozenset(returned_codes)
    missing_codes = frozenset(requested_codes.difference(snap_map))
    unexpected_codes = frozenset(returned_code_set.difference(requested_codes))
    if unexpected_codes:
        snapshot_errors.append(
            {
                "stage": "market_snapshot_completeness",
                "error_code": "SNAPSHOT_UNEXPECTED_CODES",
                "message": f"provider returned {len(unexpected_codes)} unrequested snapshot codes",
                "unexpected_codes": sorted(unexpected_codes),
            }
        )
    if missing_codes:
        snapshot_errors.append(
            {
                "stage": "market_snapshot_completeness",
                "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                "message": f"missing {len(missing_codes)} requested snapshot codes after fallback",
                "missing_codes": sorted(missing_codes),
            }
        )
    if duplicate_codes:
        snapshot_errors.append(
            {
                "stage": "market_snapshot_completeness",
                "error_code": "SNAPSHOT_DUPLICATE_CODES",
                "message": (
                    "provider returned duplicate rows for "
                    f"{len(duplicate_codes)} requested snapshot codes"
                ),
                "duplicate_codes": sorted(duplicate_codes),
            }
        )

    return MarketSnapshotFetchResult(
        snap_map=snap_map,
        errors=snapshot_errors,
        requested_codes=requested_codes,
        returned_codes=returned_code_set,
        missing_codes=missing_codes,
        unexpected_codes=unexpected_codes,
        complete=not missing_codes and not duplicate_codes,
        fallback_filled=fallback_filled,
        fallback_failed=fallback_failed,
        opend_call_count=opend_call_count,
        requested_at_utc=(
            min(batch_requested_at).isoformat() if batch_requested_at else None
        ),
        received_at_utc=(
            max(batch_received_at).isoformat() if batch_received_at else None
        ),
    )


def keep_snapshot_record_columns(snap: Any, keep_columns: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    cols = set(snap.columns)
    keep = [column for column in keep_columns if column in cols]
    if not keep or "code" not in keep:
        return [], []
    records: list[dict[str, Any]] = []
    try:
        records = [
            dict(rec)
            for rec in snap[keep].to_dict(orient="records")
            if isinstance(rec, dict)
        ]
    except Exception:
        try:
            for row in snap.to_dict("records"):
                records.append({column: row.get(column) for column in keep})
        except Exception:
            return [], keep
    return records, keep


def _merge_snapshot_records(
    *,
    records: list[dict[str, Any]],
    requested_codes: frozenset[str],
    returned_codes: set[str],
    duplicate_codes: set[str],
    snap_map: dict[str, dict[str, Any]],
) -> None:
    """Merge one provider response without hiding duplicate code evidence."""

    for rec in records:
        code = str(rec.get("code") or "").strip()
        if code:
            returned_codes.add(code)
        if code not in requested_codes or code in duplicate_codes:
            continue
        if code in snap_map:
            duplicate_codes.add(code)
            snap_map.pop(code, None)
            continue
        snap_map[code] = rec


def _fallback_fetch_missing_snapshots(
    *,
    missing_codes: list[str],
    gateway: Any,
    snapshot_limit: OpenDEndpointRateLimit,
    base_dir: Path,
    snap_map: dict[str, dict[str, Any]],
    snapshot_errors: list[dict[str, Any]],
    max_fallback_codes: int,
    fallback_batch_size: int,
    keep_columns: list[str],
    no_retry: bool,
    retry_max_attempts: int,
    retry_time_budget_sec: float,
    retry_base_delay_sec: float,
    retry_max_delay_sec: float,
    retry_call: Callable[..., Any],
    rate_limited_call: Callable[..., Any],
    requested_codes: frozenset[str],
    returned_codes: set[str],
    duplicate_codes: set[str],
) -> tuple[int, int, int, list[datetime], list[datetime]]:
    if not missing_codes or int(max_fallback_codes) <= 0:
        return 0, 0, 0, [], []

    allowed = list(missing_codes[: int(max_fallback_codes)])
    fallback_requested_at: list[datetime] = []
    fallback_received_at: list[datetime] = []
    dropped = max(0, len(missing_codes) - len(allowed))
    failed_count = 0
    opend_call_count = 0
    if dropped > 0:
        snapshot_errors.append(
            {
                "stage": "market_snapshot_fallback",
                "batch_start": len(allowed),
                "batch_size": dropped,
                "error_code": "FALLBACK_BUDGET_EXCEEDED",
                "message": f"fallback budget exceeded: dropped {dropped} codes",
            }
        )
        failed_count += dropped

    filled_count = 0
    batch_size = max(1, int(fallback_batch_size))
    for start in range(0, len(allowed), batch_size):
        batch = allowed[start : start + batch_size]
        fallback_requested_at.append(datetime.now(timezone.utc))
        try:
            def _call_fallback_snapshot(batch0: list[str] = batch) -> Any:
                def _gateway_fallback_snapshot_call() -> Any:
                    nonlocal opend_call_count
                    opend_call_count += 1
                    return gateway.get_snapshot(batch0)

                return rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    call=_gateway_fallback_snapshot_call,
                )

            snap = retry_call(
                "get_market_snapshot(fallback)",
                _call_fallback_snapshot,
                no_retry=no_retry,
                retry_max_attempts=retry_max_attempts,
                retry_time_budget_sec=retry_time_budget_sec,
                retry_base_delay_sec=retry_base_delay_sec,
                retry_max_delay_sec=retry_max_delay_sec,
                quiet=True,
            )
        except Exception as exc:
            snapshot_errors.append(
                {
                    "stage": "market_snapshot_fallback",
                    "batch_start": start,
                    "batch_size": len(batch),
                    "error_code": "FALLBACK_FAILED",
                    "message": str(exc),
                }
            )
            failed_count += len(batch)
            fallback_received_at.append(datetime.now(timezone.utc))
            continue

        if snap is None or snap.empty:
            snapshot_errors.append(
                {
                    "stage": "market_snapshot_fallback",
                    "batch_start": start,
                    "batch_size": len(batch),
                    "error_code": "FALLBACK_FAILED",
                    "message": "empty fallback snapshot",
                }
            )
            failed_count += len(batch)
            fallback_received_at.append(datetime.now(timezone.utc))
            continue

        records, keep = keep_snapshot_record_columns(snap, keep_columns)
        if not keep:
            snapshot_errors.append(
                {
                    "stage": "market_snapshot_fallback",
                    "batch_start": start,
                    "batch_size": len(batch),
                    "error_code": "FALLBACK_FAILED",
                    "message": "fallback snapshot missing code column",
                }
            )
            failed_count += len(batch)
            fallback_received_at.append(datetime.now(timezone.utc))
            continue
        fallback_received_at.append(datetime.now(timezone.utc))

        batch_codes = set(batch)
        filled_before = len(batch_codes.intersection(snap_map))
        _merge_snapshot_records(
            records=records,
            requested_codes=requested_codes,
            returned_codes=returned_codes,
            duplicate_codes=duplicate_codes,
            snap_map=snap_map,
        )
        filled_after = len(batch_codes.intersection(snap_map))
        filled_count += max(0, filled_after - filled_before)
        failed_count += max(0, len(batch_codes) - filled_after)

    return (
        filled_count,
        failed_count,
        opend_call_count,
        fallback_requested_at,
        fallback_received_at,
    )


def _append_opend_observation_error(
    errors: list[dict[str, Any]] | None,
    *,
    stage: str,
    code: str,
    error_code: str,
    message: str,
) -> None:
    if errors is None:
        return
    errors.append(
        {
            "stage": stage,
            "code": code,
            "error_code": error_code,
            "message": message,
        }
    )


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except Exception:
        return None
