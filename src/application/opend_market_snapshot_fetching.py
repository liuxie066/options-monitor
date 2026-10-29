from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable, Mapping

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
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway, retry_futu_gateway_call, check_fetch_control
from src.infrastructure.futu_dataframe import futu_dataframe_records
from src.infrastructure.opend_retcodes import classify_opend_error


@dataclass(frozen=True)
class OpenDUnderlierObservation(OpeningUnderlierObservation):
    fetch_diagnostics: list[dict[str, Any]] = field(default_factory=list)


def trading_calendar_dates(receipt: Any, *, start: date, end: date) -> list[str]:
    if not isinstance(receipt, Mapping) or (
        receipt.get("retcode") != 0
        or receipt.get("coverage_complete") is not True
        or receipt.get("pagination_complete") is not True
        or receipt.get("page_count") != 1
    ):
        raise ValueError("calendar receipt incomplete")
    dates: set[str] = set()
    rows = receipt.get("rows")
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("provider rows invalid")
    for row in rows:
        raw = row.get("time") or row.get("date") or row.get("trade_date")
        kind = str(row.get("trade_date_type") or "").strip().upper()
        if not isinstance(raw, str) or kind not in {"WHOLE", "MORNING", "AFTERNOON"}:
            raise ValueError("calendar row invalid")
        day = date.fromisoformat(raw)
        if day.isoformat() != raw or not start <= day <= end:
            raise ValueError("calendar date outside request")
        dates.add(raw)
    if not dates:
        raise ValueError("calendar returned no trading dates")
    return sorted(dates)


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
    "option_gamma",
    "option_theta",
    "option_vega",
    "option_rho",
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
    "gamma",
    "theta",
    "vega",
    "rho",
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
    code_receipts: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> float | None:
    """Try to get underlying spot from OpenD."""
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    snapshot_limit = OpenDFetchLimits.from_flat_kwargs(
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    ).market_snapshot
    try:
        def _call_snapshot() -> Any:
            _increment_metric(metrics, "spot_snapshot_opend_calls")
            _increment_metric(metrics, "spot_snapshot_requested_codes")
            check_fetch_control(deadline, cancelled)
            try:
                result = gateway.get_snapshot([underlier_code])
            finally:
                check_fetch_control(deadline, cancelled)
            return result

        if base_dir is not None:
            df = rate_limited_call(
                base_dir=Path(base_dir),
                endpoint="market_snapshot",
                **snapshot_limit.call_kwargs(),
                **controls,
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
        check_fetch_control(deadline, cancelled)
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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> OpeningUnderlierObservation:
    """Fetch one run-scoped OpenD spot observation without fallback prices."""
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}

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
            **controls,
            call=call,
        )

    snapshot_row: dict[str, Any] | None = None
    market_state_row: dict[str, Any] | None = None
    try:
        def _snapshot_call() -> Any:
            _increment_metric(metrics, "spot_snapshot_opend_calls")
            _increment_metric(metrics, "spot_snapshot_requested_codes")
            check_fetch_control(deadline, cancelled)
            try:
                result = gateway.get_snapshot([underlier_code])
            finally:
                check_fetch_control(deadline, cancelled)
            return result

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
        check_fetch_control(deadline, cancelled)
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
            check_fetch_control(deadline, cancelled)
            try:
                result = gateway.get_market_state([underlier_code])
            finally:
                check_fetch_control(deadline, cancelled)
            return result

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
        check_fetch_control(deadline, cancelled)
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


def get_underlier_observations_opend(
    gateway: Any,
    underlier_codes: list[str],
    *,
    market: str,
    base_dir: Path,
    snapshot_limit: OpenDEndpointRateLimit,
    snapshot_batch_size: int,
    stop_monotonic: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, OpeningUnderlierObservation]:
    """Fetch exact run-scoped underlier observations in bounded batches."""
    check_fetch_control(deadline, cancelled)
    codes = list(
        dict.fromkeys(
            code
            for raw_code in underlier_codes
            if (code := str(raw_code or "").strip().upper())
        )
    )
    batch_size = max(1, int(snapshot_batch_size))
    observations: dict[str, OpeningUnderlierObservation] = {}

    def _limited(call: Callable[[], Any], endpoint_stop: float) -> Any:
        kwargs = snapshot_limit.call_kwargs()
        remaining = endpoint_stop - monotonic()
        if remaining <= 0:
            raise TimeoutError("underlier observation deadline reached")
        kwargs["max_wait_sec"] = min(float(kwargs["max_wait_sec"]), remaining)

        def _call_before_deadline() -> Any:
            if monotonic() >= endpoint_stop:
                raise TimeoutError("underlier observation deadline reached")
            check_fetch_control(deadline, cancelled)
            try:
                return call()
            finally:
                check_fetch_control(deadline, cancelled)
                if monotonic() >= endpoint_stop:
                    raise TimeoutError("underlier observation deadline reached")

        return rate_limited_call(
            base_dir=Path(base_dir),
            endpoint="market_snapshot",
            **kwargs,
            deadline=endpoint_stop,
            cancelled=cancelled,
            call=_call_before_deadline,
        )

    def _fetch(endpoint: str, batch: list[str], endpoint_diagnostics: list[dict[str, Any]]) -> Any:
        started = monotonic()
        endpoint_stop = min(
            value for value in (started + 30.0, deadline, stop_monotonic)
            if value is not None
        )
        evidence: dict[str, Any] = {
            "endpoint": endpoint,
            "attempts": 0,
            "errors": [],
            "stop_reason": "success",
        }
        retry_diagnostics: dict[str, Any] = {}

        def _attempt() -> Any:
            def _provider_call() -> Any:
                evidence["attempts"] += 1
                try:
                    return getattr(gateway, endpoint)(batch)
                except Exception as exc:
                    evidence["errors"].append({
                        "attempt": evidence["attempts"],
                        "error_code": classify_option_chain_error(exc),
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                    })
                    raise

            return _limited(_provider_call, endpoint_stop)

        try:
            return retry_futu_gateway_call(
                endpoint,
                _attempt,
                retry_max_attempts=2,
                retry_time_budget_sec=30.0,
                deadline=endpoint_stop,
                cancelled=cancelled,
                quiet=True,
                diagnostics=retry_diagnostics,
            )
        except Exception as exc:
            evidence.update(
                error_code=classify_option_chain_error(exc),
                error_class=type(exc).__name__,
                message=str(exc),
            )
            if cancelled is not None and cancelled():
                evidence["stop_reason"] = "cancelled"
            elif deadline is not None and monotonic() >= deadline:
                evidence["stop_reason"] = "deadline"
            elif stop_monotonic is not None and monotonic() >= stop_monotonic:
                evidence["stop_reason"] = "stop_monotonic"
            elif monotonic() >= endpoint_stop:
                evidence["stop_reason"] = "time_budget"
            elif retry_diagnostics.get("retry_count", 0) >= 1:
                evidence["stop_reason"] = "max_attempts"
            elif classify_opend_error(exc).is_retryable:
                evidence["stop_reason"] = "time_budget"
            else:
                evidence["stop_reason"] = "non_retryable"
            check_fetch_control(deadline, cancelled)
            return None
        finally:
            evidence["elapsed_sec"] = max(0.0, monotonic() - started)
            endpoint_diagnostics.append(evidence)
            if diagnostics is not None:
                diagnostics.extend({**evidence, "symbol": code} for code in batch)

    for start in range(0, len(codes), batch_size):
        check_fetch_control(deadline, cancelled)
        if stop_monotonic is not None and monotonic() >= stop_monotonic:
            break
        batch = codes[start : start + batch_size]
        endpoint_diagnostics: list[dict[str, Any]] = []
        snapshot = _fetch("get_snapshot", batch, endpoint_diagnostics)
        market_state = _fetch("get_market_state", batch, endpoint_diagnostics)
        observed_at = now_utc()
        expected_codes = frozenset(batch)
        snapshot_rows = _provider_rows_by_code(
            snapshot,
            expected_codes=expected_codes,
        )
        market_state_rows = _provider_rows_by_code(
            market_state,
            expected_codes=expected_codes,
        )
        for code in batch:
            observation = normalize_underlier_observation(
                code=code,
                market=market,
                snapshot_row=snapshot_rows.get(code),
                market_state_row=market_state_rows.get(code),
                now_utc=observed_at,
            )
            observations[code] = OpenDUnderlierObservation(
                **observation.to_dict(),
                fetch_diagnostics=[{**item, "symbol": code} for item in endpoint_diagnostics],
            )
    return observations


def _provider_rows_by_code(
    value: Any,
    *,
    expected_codes: frozenset[str],
) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    rows: list[dict[str, Any]] = []
    if isinstance(value, list):
        rows = [dict(item) for item in value if isinstance(item, dict)]
    elif hasattr(value, "to_dict"):
        try:
            raw_rows = futu_dataframe_records(value)
        except TypeError:
            raw_rows = value.to_dict("records")
        if isinstance(raw_rows, list):
            rows = [dict(item) for item in raw_rows if isinstance(item, dict)]
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        normalized_rows.append({**row, "code": code})
    indexed: dict[str, dict[str, Any]] = {}
    _merge_snapshot_records(
        records=normalized_rows,
        requested_codes=expected_codes,
        returned_codes=set(),
        duplicate_codes=set(),
        snap_map=indexed,
    )
    return indexed


def _single_provider_row(value: Any, *, expected_code: str) -> dict[str, Any] | None:
    code = str(expected_code or "").strip().upper()
    return _provider_rows_by_code(value, expected_codes=frozenset({code})).get(code)


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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> float | None:
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    gateway = build_ready_futu_quote_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
        **controls,
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
            **controls,
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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> OpeningUnderlierObservation:
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    effective_base_dir = Path(base_dir) if base_dir is not None else REPO_ROOT
    underlier = normalize_underlier(symbol, base_dir=effective_base_dir)
    gateway = build_ready_futu_quote_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
        **controls,
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
            **controls,
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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    snapshot_cache: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> MarketSnapshotFetchResult:
    """Keep each option's batch receipt; result bounds include cached receipts and all attempts."""
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
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
    code_receipts: dict[str, tuple[str | None, str | None]] = {}
    if snapshot_cache is not None:
        for code in requested_code_order:
            if code in snapshot_cache:
                row, requested_at, received_at = snapshot_cache[code]
                snap_map[code] = dict(row)
                snap_map[code]["snapshot_requested_at_utc"] = requested_at
                snap_map[code]["snapshot_received_at_utc"] = received_at
                code_receipts[code] = (requested_at, received_at)
    if diagnostics is not None:
        diagnostics["reuse_hits"] = diagnostics.get("reuse_hits", 0) + len(snap_map)
    missing_order = [code for code in requested_code_order if code not in snap_map]
    returned_codes: set[str] = set(snap_map)
    duplicate_codes: set[str] = set()
    batch_size = int(snapshot_batch_size) if snapshot_batch_size else DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
    batch_size = max(1, batch_size)
    keep_columns = list(SNAPSHOT_KEEP_COLUMNS)
    opend_call_count = 0

    for start in range(0, len(missing_order), batch_size):
        check_fetch_control(deadline, cancelled)
        batch = list(missing_order[start : start + batch_size])
        requested_at = datetime.now(timezone.utc)
        batch_requested_at.append(requested_at)
        try:
            def _call_snapshot(batch0: list[str] = batch) -> Any:
                def _gateway_snapshot_call() -> Any:
                    nonlocal opend_call_count
                    opend_call_count += 1
                    if diagnostics is not None:
                        calls = diagnostics.setdefault("opend_calls", {})
                        calls["market_snapshot"] = calls.get("market_snapshot", 0) + 1
                    check_fetch_control(deadline, cancelled)
                    try:
                        result = gateway.get_snapshot(batch0)
                    finally:
                        check_fetch_control(deadline, cancelled)
                    return result

                return rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    **controls,
                    **({"diagnostics": diagnostics} if diagnostics is not None else {}),
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
                **({"diagnostics": diagnostics} if diagnostics is not None else {}),
                **controls,
            )
        except Exception as exc:
            check_fetch_control(deadline, cancelled)
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
        received_at = datetime.now(timezone.utc)
        batch_received_at.append(received_at)
        if snap is None or snap.empty:
            continue

        records, keep = keep_snapshot_record_columns(snap, keep_columns)
        if not keep:
            continue

        for record in records:
            record["snapshot_requested_at_utc"] = requested_at.isoformat()
            record["snapshot_received_at_utc"] = received_at.isoformat()
        _merge_snapshot_records(
            records=records,
            requested_codes=requested_codes,
            returned_codes=returned_codes,
            duplicate_codes=duplicate_codes,
            snap_map=snap_map,
            code_receipts=code_receipts,
            receipt=(batch_requested_at[-1].isoformat(), batch_received_at[-1].isoformat()),
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
                code_receipts=code_receipts,
                diagnostics=diagnostics,
                **controls,
            )
            opend_call_count += fallback_opend_calls
            batch_requested_at.extend(fallback_requested_at)
            batch_received_at.extend(fallback_received_at)

    check_fetch_control(deadline, cancelled)
    requested_times = [value.isoformat() for value in batch_requested_at]
    received_times = [value.isoformat() for value in batch_received_at]
    requested_times.extend(r[0] for r in code_receipts.values() if r[0])
    received_times.extend(r[1] for r in code_receipts.values() if r[1])
    if snapshot_cache is not None:
        for code, row in snap_map.items():
            if code not in snapshot_cache:
                snapshot_cache[code] = (dict(row), *code_receipts[code])
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
        requested_at_utc=min(requested_times, default=None),
        received_at_utc=max(received_times, default=None),
        code_receipts=code_receipts,
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
            for rec in futu_dataframe_records(snap[keep])
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
    code_receipts: dict[str, tuple[str | None, str | None]] | None = None,
    receipt: tuple[str, str] | None = None,
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
            if code_receipts is not None:
                code_receipts.pop(code, None)
            continue
        snap_map[code] = rec
        if code_receipts is not None and receipt is not None:
            code_receipts[code] = receipt


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
    code_receipts: dict[str, tuple[str | None, str | None]],
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[int, int, int, list[datetime], list[datetime]]:
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
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
        check_fetch_control(deadline, cancelled)
        batch = allowed[start : start + batch_size]
        requested_at = datetime.now(timezone.utc)
        fallback_requested_at.append(requested_at)
        try:
            def _call_fallback_snapshot(batch0: list[str] = batch) -> Any:
                def _gateway_fallback_snapshot_call() -> Any:
                    nonlocal opend_call_count
                    opend_call_count += 1
                    if diagnostics is not None:
                        calls = diagnostics.setdefault("opend_calls", {})
                        calls["market_snapshot"] = calls.get("market_snapshot", 0) + 1
                    check_fetch_control(deadline, cancelled)
                    try:
                        result = gateway.get_snapshot(batch0)
                    finally:
                        check_fetch_control(deadline, cancelled)
                    return result

                return rate_limited_call(
                    base_dir=base_dir,
                    endpoint="market_snapshot",
                    **snapshot_limit.call_kwargs(),
                    **controls,
                    **({"diagnostics": diagnostics} if diagnostics is not None else {}),
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
                **({"diagnostics": diagnostics} if diagnostics is not None else {}),
                **controls,
            )
        except Exception as exc:
            check_fetch_control(deadline, cancelled)
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

        received_at = datetime.now(timezone.utc)
        fallback_received_at.append(received_at)
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

        for record in records:
            record["snapshot_requested_at_utc"] = requested_at.isoformat()
            record["snapshot_received_at_utc"] = received_at.isoformat()
        batch_codes = set(batch)
        filled_before = len(batch_codes.intersection(snap_map))
        _merge_snapshot_records(
            records=records,
            requested_codes=requested_codes,
            returned_codes=returned_codes,
            duplicate_codes=duplicate_codes,
            snap_map=snap_map,
            code_receipts=code_receipts,
            receipt=(fallback_requested_at[-1].isoformat(), fallback_received_at[-1].isoformat()),
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
