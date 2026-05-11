from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import (
    DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT,
    OpenDEndpointRateLimit,
    OpenDFetchLimits,
)
from src.application.opend_utils import normalize_underlier
from src.application.option_chain_fetching import classify_option_chain_error
from src.infrastructure.futu_gateway import build_ready_futu_gateway, retry_futu_gateway_call


REPO_ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT_KEEP_COLUMNS = [
    "code",
    "last_price",
    "bid_price",
    "ask_price",
    "volume",
    "option_open_interest",
    "option_implied_volatility",
    "option_delta",
    "option_contract_multiplier",
    "lot_size",
    "open_interest",
    "implied_volatility",
    "delta",
    "bid",
    "ask",
]


@dataclass(frozen=True)
class MarketSnapshotFetchResult:
    snap_map: dict[str, dict[str, Any]]
    errors: list[dict[str, Any]]
    fallback_filled: int = 0
    fallback_failed: int = 0


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
) -> float | None:
    """Try to get underlying spot from OpenD."""
    snapshot_limit = OpenDFetchLimits.from_flat_kwargs(
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    ).market_snapshot
    try:
        def _call_snapshot() -> Any:
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
    gateway = build_ready_futu_gateway(
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
    batch_size = int(snapshot_batch_size) if snapshot_batch_size else DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
    batch_size = max(1, batch_size)
    keep_columns = list(SNAPSHOT_KEEP_COLUMNS)

    for start in range(0, len(option_codes), batch_size):
        batch = option_codes[start : start + batch_size]
        try:
            snap = retry_call(
                "get_market_snapshot(batch)",
                lambda batch0=batch: rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    call=lambda: gateway.get_snapshot(batch0),
                ),
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
        if snap is None or snap.empty:
            continue

        records, keep = keep_snapshot_record_columns(snap, keep_columns)
        if not keep:
            continue

        for rec in records:
            code = str(rec.get("code") or "")
            if code:
                snap_map[code] = rec

    fallback_filled = 0
    fallback_failed = 0
    if option_codes and int(snapshot_fallback_max_codes) > 0:
        missing = [code for code in option_codes if code not in snap_map]
        if missing:
            fallback_filled, fallback_failed = _fallback_fetch_missing_snapshots(
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
            )

    return MarketSnapshotFetchResult(
        snap_map=snap_map,
        errors=snapshot_errors,
        fallback_filled=fallback_filled,
        fallback_failed=fallback_failed,
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
            for _, row in snap.iterrows():
                records.append({column: row.get(column) for column in keep})
        except Exception:
            return [], keep
    return records, keep


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
) -> tuple[int, int]:
    if not missing_codes or int(max_fallback_codes) <= 0:
        return 0, 0

    allowed = list(missing_codes[: int(max_fallback_codes)])
    dropped = max(0, len(missing_codes) - len(allowed))
    failed_count = 0
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
        try:
            snap = retry_call(
                "get_market_snapshot(fallback)",
                lambda batch0=batch: rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    call=lambda: gateway.get_snapshot(batch0),
                ),
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
            continue

        filled_before = len(snap_map)
        for rec in records:
            code = str(rec.get("code") or "")
            if code:
                snap_map[code] = rec
        filled_count += max(0, len(snap_map) - filled_before)

    return filled_count, failed_count


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
