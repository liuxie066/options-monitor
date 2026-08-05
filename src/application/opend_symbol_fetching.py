from __future__ import annotations

"""Fetch required option data using Futu OpenD.

This module owns the reusable OpenD symbol-fetch orchestration. The CLI adapter
is ``python -m src.application.opend_symbol_fetching_cli``.

This is intentionally **minimal and pragmatic**:
- Fetch option contracts via `get_option_chain(underlier_code)`
- Choose the first N expirations (closest)
- Fetch per-contract quotes/greeks via `get_market_snapshot(option_codes)` in batches

Notes:
- This module requires `futu-api` + its deps (pandas/numpy/protobuf/pycryptodome/simplejson).
- For US underliers, your OpenD might not have stock quote right; spot may fail.
  In that case you can pass `--spot` manually.
"""

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pandas as pd


# Allow running as a script (python scripts/xxx.py) without package install
# by ensuring repo root is on sys.path.
import sys
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.infrastructure.futu_gateway import (
    build_ready_futu_quote_gateway,
    retry_futu_gateway_call,
)
from src.application.opend_utils import normalize_underlier, get_trading_date
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.opend_fetch_config import OpenDFetchLimits
from src.application.opend_market_snapshot_fetching import (
    MarketSnapshotFetchResult,
    fetch_option_snapshots,
    get_spot_opend,
)
from src.application.opend_normalize import normalize_opend_option_type
from src.application.opend_symbol_chain_fetching import fetch_symbol_option_chain
from src.application.option_chain_fetching import classify_option_chain_error
from src.application.short_vol_metrics import RealizedVolatilitySnapshot, fetch_realized_volatility_snapshot


def to_float(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    except Exception:
        return None


def calc_mid(bid, ask, last_price=None):
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 6)
    if last_price is not None and last_price > 0:
        return round(last_price, 6)
    return None


REQUIRED_REALIZED_VOLATILITY_INCOMPLETE = "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
SNAPSHOT_COVERAGE_INCOMPLETE = "SNAPSHOT_COVERAGE_INCOMPLETE"
OPTION_CHAIN_SCOPE_COVERAGE_SCHEMA = "option_chain_scope_coverage.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _no_contracts_realized_volatility() -> RealizedVolatilitySnapshot:
    return RealizedVolatilitySnapshot(
        status="not_applicable_no_contracts",
        reason="not_applicable_no_contracts",
    )


def _required_realized_volatility_error(
    snapshot: RealizedVolatilitySnapshot,
) -> dict[str, Any] | None:
    status = str(snapshot.status or "").strip().lower()
    estimate = to_float(snapshot.rv_estimate)
    if status == "ok" and estimate is not None and math.isfinite(estimate) and estimate > 0:
        return None
    reason = str(snapshot.reason or "").strip()
    detail = reason or f"status={status or 'missing'}, estimate={snapshot.rv_estimate!r}"
    return {
        "stage": "realized_volatility",
        "error_code": REQUIRED_REALIZED_VOLATILITY_INCOMPLETE,
        "message": f"required realized volatility is incomplete: {detail}",
        "realized_volatility_status": status or "missing",
        "realized_volatility_estimate": snapshot.rv_estimate,
    }


def _filter_option_chain_for_request(
    *,
    chain: pd.DataFrame,
    option_types: str,
    min_strike: float | None,
    max_strike: float | None,
    side_strike_windows: dict[str, dict[str, float | None]] | None,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Apply the request-owned filters without turning malformed input into emptiness."""

    requested_types = tuple(
        dict.fromkeys(
            normalized
            for value in str(option_types or "").split(",")
            if (normalized := normalize_opend_option_type(value)) in {"put", "call"}
        )
    )
    if not requested_types:
        raise ValueError("required-data option types are missing or invalid")
    required_columns = {"option_type", "strike_price", "expiration", "code"}
    missing_columns = required_columns.difference(str(column) for column in chain.columns)
    if missing_columns:
        raise ValueError(
            "option chain lacks required filter columns: "
            + ",".join(sorted(missing_columns))
        )

    filtered = cast(pd.DataFrame, chain.copy())
    filtered["_ot"] = filtered["option_type"].map(normalize_opend_option_type)
    if filtered["_ot"].isin({"put", "call"}).sum() != len(filtered):
        raise ValueError("option chain contains an invalid option type")
    filtered = cast(
        pd.DataFrame,
        filtered[filtered["_ot"].isin(requested_types)].copy(),
    )
    strikes = pd.to_numeric(filtered["strike_price"], errors="coerce")
    if strikes.isna().any():
        raise ValueError("option chain contains an invalid strike price")

    def _validated_bound(value: object, *, name: str) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} is invalid")
        return parsed

    global_min = _validated_bound(min_strike, name="minimum strike")
    global_max = _validated_bound(max_strike, name="maximum strike")
    if global_min is not None and global_max is not None and global_min > global_max:
        raise ValueError("minimum strike exceeds maximum strike")
    windows = side_strike_windows or {}
    if not isinstance(windows, dict):
        raise ValueError("side strike windows are invalid")
    unknown_sides = set(windows).difference({"put", "call"})
    if unknown_sides:
        raise ValueError("side strike windows contain an invalid option type")

    keep = pd.Series(True, index=filtered.index, dtype=bool)
    for option_type in requested_types:
        raw_window = windows.get(option_type) or {}
        if not isinstance(raw_window, dict):
            raise ValueError("side strike window is invalid")
        side_min = _validated_bound(
            raw_window.get("min_strike"), name=f"{option_type} minimum strike"
        )
        side_max = _validated_bound(
            raw_window.get("max_strike"), name=f"{option_type} maximum strike"
        )
        effective_min = side_min if side_min is not None else global_min
        effective_max = side_max if side_max is not None else global_max
        if effective_min is not None and effective_max is not None and effective_min > effective_max:
            raise ValueError(f"{option_type} minimum strike exceeds maximum strike")
        side_mask = filtered["_ot"] == option_type
        if effective_min is not None:
            keep &= ~side_mask | (strikes >= effective_min)
        if effective_max is not None:
            keep &= ~side_mask | (strikes <= effective_max)
    return cast(pd.DataFrame, filtered[keep].copy()), requested_types


def _build_option_chain_scope_coverage(
    *,
    chain: pd.DataFrame,
    option_types: tuple[str, ...],
    expirations: list[str],
    expiration_statuses: object,
) -> dict[str, object]:
    statuses = expiration_statuses if isinstance(expiration_statuses, dict) else {}
    scopes: list[dict[str, object]] = []
    for option_type in option_types:
        for expiration in expirations:
            scoped = chain[
                (chain["_ot"] == option_type)
                & (chain["expiration"].astype(str) == expiration)
            ]
            codes = sorted(
                {
                    str(value).strip()
                    for value in scoped["code"].tolist()
                    if str(value).strip()
                }
            )
            scopes.append(
                {
                    "option_type": option_type,
                    "expiration": expiration,
                    "chain_status": str(statuses.get(expiration) or "").strip(),
                    "filtered_contract_codes": codes,
                    "filtered_contract_count": len(codes),
                }
            )
    return {"schema_version": OPTION_CHAIN_SCOPE_COVERAGE_SCHEMA, "scopes": scopes}


def _snapshot_completeness_meta(
    result: MarketSnapshotFetchResult | None,
    *,
    empty_complete: bool = False,
) -> dict[str, Any]:
    if result is None:
        return {
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": bool(empty_complete),
        }
    return {
        "snapshot_requested_codes": result.requested_codes_count,
        "snapshot_returned_codes": result.returned_codes_count,
        "snapshot_missing_codes": result.missing_codes_count,
        "snapshot_unexpected_codes": result.unexpected_codes_count,
        "snapshot_requested_code_set": sorted(result.requested_codes),
        "snapshot_returned_code_set": sorted(result.returned_codes),
        "snapshot_missing_code_set": sorted(result.missing_codes),
        "snapshot_unexpected_code_set": sorted(result.unexpected_codes),
        "snapshot_complete": bool(result.complete),
    }


def _as_date(s: str) -> date:
    # futu strike_time is usually 'YYYY-MM-DD'
    return datetime.strptime(s[:10], '%Y-%m-%d').date()


def _resolve_request_trading_date(
    *,
    value: str | None,
    market: str,
) -> date:
    if value is None:
        return get_trading_date(market)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("required-data trading date is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("required-data trading date is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError("required-data trading date is invalid")
    return parsed


def _safe_int(x):
    try:
        if x is None:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return int(x)
    except Exception:
        return None


def _pick_col(row: Any, *cands: str):
    # row can be a pandas Series or a plain dict (we prefer dicts for memory efficiency)
    try:
        if row is None:
            return None
        if isinstance(row, dict):
            for c in cands:
                if c in row and row[c] is not None and (not (isinstance(row[c], float) and math.isnan(row[c]))):
                    return row[c]
            return None
        # pandas Series-like
        for c in cands:
            if c in row and pd.notna(row[c]):
                return row[c]
        return None
    except Exception:
        return None


def _tuple_col(row: tuple[Any, ...], columns: dict[str, int], name: str) -> Any:
    idx = columns.get(name)
    if idx is None:
        return None
    try:
        return row[idx]
    except Exception:
        return None


@dataclass(frozen=True)
class FetchSymbolRequest:
    symbol: str
    limit_expirations: int | None = None
    host: str = '127.0.0.1'
    port: int = 11111
    spot_override: float | None = None
    fetch_spot_if_missing: bool = True
    base_dir: Path | None = None
    option_types: str = 'put,call'
    min_strike: float | None = None
    max_strike: float | None = None
    side_strike_windows: dict[str, dict[str, float | None]] | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    explicit_expirations: list[Any] | None = None
    retry_max_attempts: int = 4
    retry_time_budget_sec: float = 8.0
    retry_base_delay_sec: float = 0.8
    retry_max_delay_sec: float = 6.0
    no_retry: bool = False
    chain_cache: bool = False
    chain_cache_force_refresh: bool = False
    freshness_policy: str = 'cache_first'
    max_wait_sec: float = 90.0
    option_chain_window_sec: float = 30.0
    option_chain_max_calls: int = 10
    snapshot_max_wait_sec: float = 30.0
    snapshot_window_sec: float = 30.0
    snapshot_max_calls: int = 60
    expiration_max_wait_sec: float = 30.0
    expiration_window_sec: float = 30.0
    expiration_max_calls: int = 60
    gateway: Any = None
    snapshot_batch_size: int | None = None
    snapshot_fallback_max_codes: int = 100
    snapshot_fallback_batch_size: int = 20
    include_realized_volatility: bool = False
    trading_date: str | None = None

    @property
    def effective_base_dir(self) -> Path:
        return Path(self.base_dir) if self.base_dir is not None else REPO_ROOT

    @property
    def limits(self) -> OpenDFetchLimits:
        return OpenDFetchLimits.from_flat_kwargs(
            max_wait_sec=self.max_wait_sec,
            option_chain_window_sec=self.option_chain_window_sec,
            option_chain_max_calls=self.option_chain_max_calls,
            snapshot_max_wait_sec=self.snapshot_max_wait_sec,
            snapshot_window_sec=self.snapshot_window_sec,
            snapshot_max_calls=self.snapshot_max_calls,
            expiration_max_wait_sec=self.expiration_max_wait_sec,
            expiration_window_sec=self.expiration_window_sec,
            expiration_max_calls=self.expiration_max_calls,
        )


def fetch_symbol(symbol: str, limit_expirations: int | None = None, host: str = '127.0.0.1', port: int = 11111, spot_override: float | None = None, *, fetch_spot_if_missing: bool = True, base_dir: Path | None = None, option_types: str = 'put,call', min_strike: float | None = None, max_strike: float | None = None, side_strike_windows: dict[str, dict[str, float | None]] | None = None, min_dte: int | None = None, max_dte: int | None = None, explicit_expirations: list[str] | None = None, trading_date: str | None = None, retry_max_attempts: int = 4, retry_time_budget_sec: float = 8.0, retry_base_delay_sec: float = 0.8, retry_max_delay_sec: float = 6.0, no_retry: bool = False, chain_cache: bool = False, chain_cache_force_refresh: bool = False, freshness_policy: str = 'cache_first', max_wait_sec: float = 90.0, option_chain_window_sec: float = 30.0, option_chain_max_calls: int = 10, snapshot_max_wait_sec: float = 30.0, snapshot_window_sec: float = 30.0, snapshot_max_calls: int = 60, expiration_max_wait_sec: float = 30.0, expiration_window_sec: float = 30.0, expiration_max_calls: int = 60, gateway: Any = None, snapshot_batch_size: int | None = None, snapshot_fallback_max_codes: int = 100, snapshot_fallback_batch_size: int = 20, include_realized_volatility: bool = False) -> dict[str, Any]:
    return fetch_symbol_request(
        FetchSymbolRequest(
            symbol=symbol,
            limit_expirations=limit_expirations,
            host=host,
            port=port,
            spot_override=spot_override,
            fetch_spot_if_missing=fetch_spot_if_missing,
            base_dir=base_dir,
            option_types=option_types,
            min_strike=min_strike,
            max_strike=max_strike,
            side_strike_windows=side_strike_windows,
            min_dte=min_dte,
            max_dte=max_dte,
            explicit_expirations=explicit_expirations,
            trading_date=trading_date,
            retry_max_attempts=retry_max_attempts,
            retry_time_budget_sec=retry_time_budget_sec,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            no_retry=no_retry,
            chain_cache=chain_cache,
            chain_cache_force_refresh=chain_cache_force_refresh,
            freshness_policy=freshness_policy,
            max_wait_sec=max_wait_sec,
            option_chain_window_sec=option_chain_window_sec,
            option_chain_max_calls=option_chain_max_calls,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
            expiration_max_wait_sec=expiration_max_wait_sec,
            expiration_window_sec=expiration_window_sec,
            expiration_max_calls=expiration_max_calls,
            gateway=gateway,
            snapshot_batch_size=snapshot_batch_size,
            snapshot_fallback_max_codes=snapshot_fallback_max_codes,
            snapshot_fallback_batch_size=snapshot_fallback_batch_size,
            include_realized_volatility=bool(include_realized_volatility),
        )
    )


def fetch_symbol_request(
    request: FetchSymbolRequest,
    *,
    snapshot_fallback_max_codes: int | None = None,
    snapshot_fallback_batch_size: int | None = None,
) -> dict[str, Any]:
    if not isinstance(request.fetch_spot_if_missing, bool):
        raise TypeError("fetch_spot_if_missing must be a bool")
    if snapshot_fallback_max_codes is not None or snapshot_fallback_batch_size is not None:
        request = replace(
            request,
            snapshot_fallback_max_codes=(
                int(snapshot_fallback_max_codes)
                if snapshot_fallback_max_codes is not None
                else request.snapshot_fallback_max_codes
            ),
            snapshot_fallback_batch_size=(
                int(snapshot_fallback_batch_size)
                if snapshot_fallback_batch_size is not None
                else request.snapshot_fallback_batch_size
            ),
        )
    symbol = request.symbol
    host = request.host
    port = request.port
    spot_override = request.spot_override
    option_types = request.option_types
    min_strike = request.min_strike
    max_strike = request.max_strike
    side_strike_windows = request.side_strike_windows
    min_dte = request.min_dte
    max_dte = request.max_dte
    explicit_expirations = request.explicit_expirations
    retry_max_attempts = request.retry_max_attempts
    retry_time_budget_sec = request.retry_time_budget_sec
    retry_base_delay_sec = request.retry_base_delay_sec
    retry_max_delay_sec = request.retry_max_delay_sec
    no_retry = request.no_retry
    chain_cache = request.chain_cache
    effective_base_dir = request.effective_base_dir
    u = normalize_underlier(symbol, base_dir=effective_base_dir)
    opend_limits = request.limits
    snapshot_limit = opend_limits.market_snapshot
    external_gateway = request.gateway is not None
    explicit_expirations_norm = sorted({
        exp
        for exp in (normalize_expiration_ymd(x) for x in (explicit_expirations or []))
        if exp
    })
    if request.trading_date is not None and not explicit_expirations_norm:
        raise ValueError(
            "anchored required-data request lacks explicit expirations"
        )
    spot_errors: list[dict[str, Any]] = []
    spot_fetch_meta: dict[str, Any] = {
        "spot_snapshot_opend_calls": 0,
        "spot_snapshot_requested_codes": 0,
    }
    rv_snapshot = RealizedVolatilitySnapshot(status="skipped", reason="not_requested")
    if external_gateway:
        gateway = request.gateway
    else:
        gateway = build_ready_futu_quote_gateway(
            host=host,
            port=int(port),
            is_option_chain_cache_enabled=bool(chain_cache),
        )

    try:
        spot = spot_override

        # Spot policy:
        # - HK/CN: try OpenD snapshot (usually available)
        # - US: also rely on OpenD only; if quote right is missing, keep spot as None
        if spot is None and request.fetch_spot_if_missing:
            spot = get_spot_opend(
                gateway,
                u.code,
                base_dir=effective_base_dir,
                snapshot_max_wait_sec=snapshot_limit.max_wait_sec,
                snapshot_window_sec=snapshot_limit.window_sec,
                snapshot_max_calls=snapshot_limit.max_calls,
                errors=spot_errors,
                rate_limited_call=rate_limited_opend_call,
                metrics=spot_fetch_meta,
            )

        # Trading-date anchor for DTE / cache freshness.
        today = _resolve_request_trading_date(
            value=request.trading_date,
            market=u.market,
        )

        chain_bundle = fetch_symbol_option_chain(
            gateway=gateway,
            request=request,
            underlier_code=u.code,
            today=today,
            explicit_expirations_norm=explicit_expirations_norm,
            limits=opend_limits,
            retry_call=retry_futu_gateway_call,
            rate_limited_call=rate_limited_opend_call,
        )

        # Reuse the fetched DataFrame when available to avoid records <-> DataFrame round trips.
        raw_chain = chain_bundle.frame
        chain: pd.DataFrame = raw_chain if isinstance(raw_chain, pd.DataFrame) else pd.DataFrame(chain_bundle.rows or [])
        if chain.empty:
            fetch_meta = dict(chain_bundle.fetch_meta or {})
            status = str(fetch_meta.get('status') or 'error')
            source_outcome = str(
                fetch_meta.get('source_outcome') or 'provider_error'
            )
            reason_code = (
                str(fetch_meta.get('reason_code') or '').strip() or None
            )
            error_code = (
                str(fetch_meta.get('error_code') or '').strip() or None
            )
            raw_fetch_errors = fetch_meta.get('errors')
            fetch_errors = [item for item in raw_fetch_errors if isinstance(item, dict)] if isinstance(raw_fetch_errors, list) else []
            if request.include_realized_volatility and source_outcome == "success_empty":
                rv_snapshot = _no_contracts_realized_volatility()
            elif request.include_realized_volatility:
                rv_snapshot = RealizedVolatilitySnapshot(
                    status="skipped",
                    reason="option_chain_unavailable",
                )
            error_message = next(
                (
                    str(item.get('message'))
                    for item in fetch_errors
                    if isinstance(item, dict) and str(item.get('message') or '').strip()
                ),
                error_code.lower() if error_code else None,
            )
            return {
                'symbol': symbol,
                'underlier_code': u.code,
                'spot': spot,
                'expiration_count': 0,
                'expirations': [],
                'rows': [],
                'meta': {
                    'source': 'opend',
                    'host': host,
                    'port': port,
                    'status': status,
                    'error_code': error_code,
                    'error': error_message,
                    'source_outcome': source_outcome,
                    'reason_code': reason_code,
                    'expiration_statuses': fetch_meta.get('expiration_statuses') or {},
                    'errors': fetch_errors,
                    'diagnostics': fetch_meta.get('diagnostics') or [],
                    'spot_errors': spot_errors,
                    'from_cache_expirations': fetch_meta.get('from_cache_expirations') or [],
                    'fetched_expirations': fetch_meta.get('fetched_expirations') or [],
                    'stale_cache_expirations': fetch_meta.get('stale_cache_expirations') or [],
                    'stale_cache_asof_dates': fetch_meta.get('stale_cache_asof_dates') or {},
                    'expiration_opend_calls': int(fetch_meta.get('expiration_opend_calls') or 0),
                    'expiration_cache_hits': int(fetch_meta.get('expiration_cache_hits') or 0),
                    'opend_call_count': int(fetch_meta.get('opend_call_count') or 0),
                    'rate_gate_wait_sec': float(fetch_meta.get('rate_gate_wait_sec') or 0.0),
                    'spot_snapshot_opend_calls': int(spot_fetch_meta.get('spot_snapshot_opend_calls') or 0),
                    'spot_snapshot_requested_codes': int(spot_fetch_meta.get('spot_snapshot_requested_codes') or 0),
                    **_snapshot_completeness_meta(None, empty_complete=(source_outcome == "success_empty")),
                    'realized_volatility': rv_snapshot.to_meta(),
                    'source_observed_at': fetch_meta.get('source_observed_at'),
                    'completed_at_utc': fetch_meta.get('completed_at_utc'),
                    'trading_date': today.isoformat(),
                },
            }

        # Derive expirations (strike_time) and pick first N
        chain = cast(pd.DataFrame, chain.copy())
        chain['expiration'] = chain['strike_time'].astype(str).str.slice(0, 10)
        expirations = sorted({x for x in chain['expiration'].tolist() if isinstance(x, str) and len(x) >= 10})
        if explicit_expirations_norm:
            expirations = [exp for exp in explicit_expirations_norm if exp in set(expirations)]
        elif request.limit_expirations:
            expirations = expirations[: int(request.limit_expirations)]

        chain = cast(pd.DataFrame, chain[chain['expiration'].isin(expirations)].copy())

        # Early filters BEFORE snapshots (performance-critical). Malformed provider
        # data or request bounds are provider failures, never proof of emptiness.
        chain, requested_option_types = _filter_option_chain_for_request(
            chain=chain,
            option_types=option_types,
            min_strike=min_strike,
            max_strike=max_strike,
            side_strike_windows=side_strike_windows,
        )
        option_chain_scope_coverage = _build_option_chain_scope_coverage(
            chain=chain,
            option_types=requested_option_types,
            expirations=expirations,
            expiration_statuses=(chain_bundle.fetch_meta or {}).get("expiration_statuses"),
        )

        # Fetch snapshots for option codes in batches
        option_codes = [str(x) for x in chain['code'].tolist() if isinstance(x, str) and x]

        if request.include_realized_volatility:
            if option_codes:
                rv_snapshot = fetch_realized_volatility_snapshot(
                    gateway,
                    underlier_code=u.code,
                    trading_day=today,
                )
            else:
                rv_snapshot = _no_contracts_realized_volatility()

        snapshot_result = fetch_option_snapshots(
            option_codes=option_codes,
            gateway=gateway,
            snapshot_limit=snapshot_limit,
            base_dir=effective_base_dir,
            snapshot_batch_size=request.snapshot_batch_size,
            snapshot_fallback_max_codes=request.snapshot_fallback_max_codes,
            snapshot_fallback_batch_size=request.snapshot_fallback_batch_size,
            no_retry=no_retry,
            retry_max_attempts=retry_max_attempts,
            retry_time_budget_sec=retry_time_budget_sec,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            retry_call=retry_futu_gateway_call,
            rate_limited_call=rate_limited_opend_call,
            classify_error=classify_option_chain_error,
        )
        snap_map = snapshot_result.snap_map
        snapshot_errors = snapshot_result.errors
        snapshot_fallback_filled = snapshot_result.fallback_filled
        snapshot_fallback_failed = snapshot_result.fallback_failed
        snapshot_opend_call_count = snapshot_result.opend_call_count

        rows: list[dict[str, Any]] = []

        chain_columns = {str(column): idx for idx, column in enumerate(chain.columns)}
        for r in chain.itertuples(index=False, name=None):
            opt_code = str(_tuple_col(r, chain_columns, 'code'))
            exp = str(_tuple_col(r, chain_columns, 'expiration'))
            try:
                dte = (_as_date(exp) - today).days
            except Exception:
                dte = None

            strike = to_float(_tuple_col(r, chain_columns, 'strike_price'))
            option_type = str(_tuple_col(r, chain_columns, 'option_type') or '').lower()
            if option_type in ('call', 'put'):
                pass
            else:
                # futu option_type might be 'CALL'/'PUT' or numeric; best-effort
                if 'call' in option_type:
                    option_type = 'call'
                elif 'put' in option_type:
                    option_type = 'put'

            srow = snap_map.get(opt_code)
            # srow is a dict of minimal snapshot fields
            last_price = to_float(_pick_col(srow, 'last_price')) if srow is not None else None
            bid = to_float(_pick_col(srow, 'bid_price', 'bid')) if srow is not None else None
            ask = to_float(_pick_col(srow, 'ask_price', 'ask')) if srow is not None else None
            vol = to_float(_pick_col(srow, 'volume')) if srow is not None else None

            # Option-specific columns may be prefixed in market_snapshot
            oi = _pick_col(srow, 'option_open_interest', 'open_interest', 'net_open_interest') if srow is not None else None
            oi = to_float(oi)
            iv = _pick_col(srow, 'option_implied_volatility', 'implied_volatility') if srow is not None else None
            iv = to_float(iv)
            # Normalize OpenD IV to decimal (e.g. 25 -> 0.25)
            try:
                from src.application.opend_normalize import normalize_iv
                iv = normalize_iv(iv)
            except Exception:
                # fallback: keep existing heuristic
                if iv is not None and iv > 3.0:
                    iv = iv / 100.0
            delta = _pick_col(srow, 'option_delta', 'delta') if srow is not None else None
            delta = to_float(delta)

            # Prefer multiplier from snapshot if present (more authoritative), fallback to chain lot_size.
            snap_mult = _safe_int(_pick_col(srow, 'option_contract_multiplier', 'option_contract_size', 'lot_size')) if srow is not None else None

            # OpenD provides lot_size in option_chain; for stock options this is usually the contract multiplier.
            lot_size = _safe_int(_tuple_col(r, chain_columns, 'lot_size'))
            multiplier = snap_mult or lot_size

            row = {
                'symbol': symbol,
                'option_type': option_type,
                'expiration': exp,
                'dte': dte,
                'contract_symbol': opt_code,  # keep column name, value becomes futu option code
                'strike': strike,
                'spot': spot,
                'bid': bid,
                'ask': ask,
                'last_price': last_price,
                'mid': calc_mid(bid, ask, last_price),
                'volume': vol,
                'open_interest': oi,
                'implied_volatility': iv,
                **rv_snapshot.to_row_fields(),
                'in_the_money': None,
                'currency': u.currency,
                'otm_pct': None,
                'delta': delta,
                # contract multiplier (shares per contract)
                'multiplier': multiplier,
            }

            if strike is not None and spot is not None and spot > 0 and option_type in ('put','call'):
                if option_type == 'put':
                    row['otm_pct'] = (spot - strike) / spot
                else:
                    row['otm_pct'] = (strike - spot) / spot

            rows.append(row)

        fetch_result_meta = chain_bundle.fetch_meta or {}
        raw_fetch_errors = fetch_result_meta.get('errors')
        fetch_errors = [item for item in raw_fetch_errors if isinstance(item, dict)] if isinstance(raw_fetch_errors, list) else []
        rv_error = (
            _required_realized_volatility_error(rv_snapshot)
            if request.include_realized_volatility and option_codes
            else None
        )
        combined_errors = [*fetch_errors, *snapshot_errors, *([rv_error] if rv_error is not None else [])]
        status = str(fetch_result_meta.get('status') or 'ok')
        error_code = fetch_result_meta.get('error_code')
        if not snapshot_result.complete:
            status = 'error'
            error_code = SNAPSHOT_COVERAGE_INCOMPLETE
        elif rv_error is not None:
            status = 'error'
            error_code = REQUIRED_REALIZED_VOLATILITY_INCOMPLETE
        fetch_error_message = next(
            (
                str(item.get('message'))
                for item in combined_errors
                if isinstance(item, dict)
                and str(item.get('error_code') or '').strip() == str(error_code or '').strip()
                and str(item.get('message') or '').strip()
            ),
            next(
                (
                    str(item.get('message'))
                    for item in combined_errors
                    if status != 'ok'
                    and isinstance(item, dict)
                    and str(item.get('message') or '').strip()
                ),
                None,
            ),
        )
        completed_at_utc = _utc_now_iso()
        source_observed_at = fetch_result_meta.get('source_observed_at') or completed_at_utc
        scope_rows = [
            scope
            for scope in option_chain_scope_coverage['scopes']
            if isinstance(scope, dict)
        ]
        filtered_empty = (
            not option_codes
            and str(fetch_result_meta.get('source_outcome') or '').strip().lower()
            == 'success_rows'
            and bool(scope_rows)
            and all(
                str(scope.get('chain_status') or '').strip()
                in {'cache', 'fetched'}
                for scope in scope_rows
            )
        )
        source_outcome = (
            'success_empty'
            if filtered_empty
            else fetch_result_meta.get('source_outcome')
        )
        reason_code = (
            'no_contract_rows'
            if filtered_empty
            else fetch_result_meta.get('reason_code')
        )

        return {
            'symbol': symbol,
            'underlier_code': u.code,
            'spot': spot,
            'expiration_count': len(expirations),
            'expirations': expirations,
            'rows': rows,
            'meta': {
                'source': 'opend',
                'host': host,
                'port': port,
                'status': status,
                'error_code': error_code,
                'error': fetch_error_message,
                'source_outcome': source_outcome,
                'reason_code': reason_code,
                'expiration_statuses': fetch_result_meta.get('expiration_statuses') or {},
                'errors': combined_errors,
                'diagnostics': fetch_result_meta.get('diagnostics') or [],
                'from_cache_expirations': fetch_result_meta.get('from_cache_expirations') or [],
                'fetched_expirations': fetch_result_meta.get('fetched_expirations') or [],
                'stale_cache_expirations': fetch_result_meta.get('stale_cache_expirations') or [],
                'stale_cache_asof_dates': fetch_result_meta.get('stale_cache_asof_dates') or {},
                'expiration_opend_calls': int(fetch_result_meta.get('expiration_opend_calls') or 0),
                'expiration_cache_hits': int(fetch_result_meta.get('expiration_cache_hits') or 0),
                'opend_call_count': int(fetch_result_meta.get('opend_call_count') or 0),
                'rate_gate_wait_sec': float(fetch_result_meta.get('rate_gate_wait_sec') or 0.0),
                'spot_snapshot_opend_calls': int(spot_fetch_meta.get('spot_snapshot_opend_calls') or 0),
                'spot_snapshot_requested_codes': int(spot_fetch_meta.get('spot_snapshot_requested_codes') or 0),
                'option_codes': len(option_codes),
                **_snapshot_completeness_meta(snapshot_result),
                'snapshot_opend_call_count': int(snapshot_opend_call_count),
                'snapshots_rows': int(len(snap_map)),
                'snapshot_fallback_filled': int(snapshot_fallback_filled),
                'snapshot_fallback_failed': int(snapshot_fallback_failed),
                'snapshot_errors': snapshot_errors,
                'spot_errors': spot_errors,
                'realized_volatility': rv_snapshot.to_meta(),
                'side_strike_windows': side_strike_windows or {},
                'option_chain_scope_coverage': option_chain_scope_coverage,
                'source_observed_at': source_observed_at,
                'completed_at_utc': completed_at_utc,
                'trading_date': today.isoformat(),
            },
        }
    except Exception as e:
        error_text = f'{type(e).__name__}: {e}'
        return {
            'symbol': symbol,
            'underlier_code': (u.code if 'u' in locals() else None),
            'spot': spot_override,
            'expiration_count': 0,
            'expirations': [],
            'rows': [],
            'meta': {
                'source': 'opend',
                'host': host,
                'port': port,
                'status': 'error',
                'error_code': classify_option_chain_error(e),
                'error': error_text,
                'spot_errors': spot_errors,
                **_snapshot_completeness_meta(None, empty_complete=False),
                'realized_volatility': rv_snapshot.to_meta(),
            },
        }

    finally:
        if not external_gateway:
            try:
                gateway.close()
            except Exception:
                pass
