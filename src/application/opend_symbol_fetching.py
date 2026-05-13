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
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


# Allow running as a script (python scripts/xxx.py) without package install
# by ensuring repo root is on sys.path.
import sys
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.infrastructure.futu_gateway import (
    build_ready_futu_gateway,
    retry_futu_gateway_call,
)
from src.application.opend_utils import normalize_underlier, get_trading_date
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.opend_fetch_config import OpenDFetchLimits
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots, get_spot_opend
from src.application.opend_symbol_chain_fetching import fetch_symbol_option_chain
from src.application.option_chain_fetching import classify_option_chain_error


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


def _as_date(s: str) -> date:
    # futu strike_time is usually 'YYYY-MM-DD'
    return datetime.strptime(s[:10], '%Y-%m-%d').date()


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


@dataclass(frozen=True)
class FetchSymbolRequest:
    symbol: str
    limit_expirations: int | None = None
    host: str = '127.0.0.1'
    port: int = 11111
    spot_override: float | None = None
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


def fetch_symbol(symbol: str, limit_expirations: int | None = None, host: str = '127.0.0.1', port: int = 11111, spot_override: float | None = None, *, base_dir: Path | None = None, option_types: str = 'put,call', min_strike: float | None = None, max_strike: float | None = None, side_strike_windows: dict[str, dict[str, float | None]] | None = None, min_dte: int | None = None, max_dte: int | None = None, explicit_expirations: list[str] | None = None, retry_max_attempts: int = 4, retry_time_budget_sec: float = 8.0, retry_base_delay_sec: float = 0.8, retry_max_delay_sec: float = 6.0, no_retry: bool = False, chain_cache: bool = False, chain_cache_force_refresh: bool = False, freshness_policy: str = 'cache_first', max_wait_sec: float = 90.0, option_chain_window_sec: float = 30.0, option_chain_max_calls: int = 10, snapshot_max_wait_sec: float = 30.0, snapshot_window_sec: float = 30.0, snapshot_max_calls: int = 60, expiration_max_wait_sec: float = 30.0, expiration_window_sec: float = 30.0, expiration_max_calls: int = 60, gateway: Any = None, snapshot_batch_size: int | None = None, snapshot_fallback_max_codes: int = 100, snapshot_fallback_batch_size: int = 20) -> dict[str, Any]:
    return fetch_symbol_request(
        FetchSymbolRequest(
            symbol=symbol,
            limit_expirations=limit_expirations,
            host=host,
            port=port,
            spot_override=spot_override,
            base_dir=base_dir,
            option_types=option_types,
            min_strike=min_strike,
            max_strike=max_strike,
            side_strike_windows=side_strike_windows,
            min_dte=min_dte,
            max_dte=max_dte,
            explicit_expirations=explicit_expirations,
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
        )
    )


def fetch_symbol_request(
    request: FetchSymbolRequest,
    *,
    snapshot_fallback_max_codes: int | None = None,
    snapshot_fallback_batch_size: int | None = None,
) -> dict[str, Any]:
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
    spot_errors: list[dict[str, Any]] = []
    spot_fetch_meta: dict[str, Any] = {
        "spot_snapshot_opend_calls": 0,
        "spot_snapshot_requested_codes": 0,
    }
    if external_gateway:
        gateway = request.gateway
    else:
        gateway = build_ready_futu_gateway(
            host=host,
            port=int(port),
            is_option_chain_cache_enabled=bool(chain_cache),
        )

    try:
        spot = spot_override

        # Spot policy:
        # - HK/CN: try OpenD snapshot (usually available)
        # - US: also rely on OpenD only; if quote right is missing, keep spot as None
        if spot is None:
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
        today = get_trading_date(u.market)

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

        # Rehydrate into a DataFrame for existing downstream logic.
        chain = pd.DataFrame(chain_bundle.rows or [])
        if chain is None or chain.empty:
            fetch_meta = dict(chain_bundle.fetch_meta or {})
            status = str(fetch_meta.get('status') or 'error')
            error_code = str(fetch_meta.get('error_code') or 'EMPTY_CHAIN')
            fetch_errors = fetch_meta.get('errors') if isinstance(fetch_meta.get('errors'), list) else []
            error_message = next(
                (
                    str(item.get('message'))
                    for item in fetch_errors
                    if isinstance(item, dict) and str(item.get('message') or '').strip()
                ),
                error_code.lower() if error_code else 'empty_chain',
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
                    'expiration_statuses': fetch_meta.get('expiration_statuses') or {},
                    'errors': fetch_errors,
                    'spot_errors': spot_errors,
                    'from_cache_expirations': fetch_meta.get('from_cache_expirations') or [],
                    'fetched_expirations': fetch_meta.get('fetched_expirations') or [],
                    'expiration_opend_calls': int(fetch_meta.get('expiration_opend_calls') or 0),
                    'expiration_cache_hits': int(fetch_meta.get('expiration_cache_hits') or 0),
                    'opend_call_count': int(fetch_meta.get('opend_call_count') or 0),
                    'rate_gate_wait_sec': float(fetch_meta.get('rate_gate_wait_sec') or 0.0),
                    'spot_snapshot_opend_calls': int(spot_fetch_meta.get('spot_snapshot_opend_calls') or 0),
                    'spot_snapshot_requested_codes': int(spot_fetch_meta.get('spot_snapshot_requested_codes') or 0),
                },
            }

        # Derive expirations (strike_time) and pick first N
        chain = chain.copy()
        chain['expiration'] = chain['strike_time'].astype(str).str.slice(0, 10)
        expirations = sorted({x for x in chain['expiration'].tolist() if isinstance(x, str) and len(x) >= 10})
        if explicit_expirations_norm:
            expirations = [exp for exp in explicit_expirations_norm if exp in set(expirations)]
        elif request.limit_expirations:
            expirations = expirations[: int(request.limit_expirations)]

        chain = chain[chain['expiration'].isin(expirations)].copy()

        # Early filters BEFORE snapshots (performance-critical):
        # - option type (put/call)
        # - strike range (min_strike/max_strike)
        try:
            ot_set = {s.strip().lower() for s in str(option_types or '').split(',') if s.strip()}
            if ot_set and 'option_type' in chain.columns:
                def _norm_ot(x):
                    s = str(x or '').lower()
                    if s in ('call','put'):
                        return s
                    if 'call' in s:
                        return 'call'
                    if 'put' in s:
                        return 'put'
                    return s
                chain['_ot'] = chain['option_type'].apply(_norm_ot)
                chain = chain[chain['_ot'].isin(ot_set)].copy()
        except Exception:
            pass

        try:
            if (min_strike is not None) or (max_strike is not None) or side_strike_windows:
                if 'strike_price' in chain.columns:
                    sp = pd.to_numeric(chain['strike_price'], errors='coerce')
                    if side_strike_windows:
                        def _row_keep(raw_option_type, raw_strike) -> bool:
                            strike_v = to_float(raw_strike)
                            if strike_v is None:
                                return False
                            opt = str(raw_option_type or '').lower()
                            if opt not in ('put', 'call'):
                                if 'put' in opt:
                                    opt = 'put'
                                elif 'call' in opt:
                                    opt = 'call'
                            side_window = (side_strike_windows or {}).get(opt) if opt else None
                            side_min = to_float((side_window or {}).get('min_strike')) if isinstance(side_window, dict) else None
                            side_max = to_float((side_window or {}).get('max_strike')) if isinstance(side_window, dict) else None
                            effective_min = side_min if side_min is not None else min_strike
                            effective_max = side_max if side_max is not None else max_strike
                            if effective_min is not None and strike_v < float(effective_min):
                                return False
                            if effective_max is not None and strike_v > float(effective_max):
                                return False
                            return True
                        mask = [
                            _row_keep(raw_option_type, raw_strike)
                            for raw_option_type, raw_strike in zip(chain.get('option_type'), chain.get('strike_price'))
                        ]
                        chain = chain[mask].copy()
                    else:
                        if min_strike is not None:
                            chain = chain[sp >= float(min_strike)].copy()
                            sp = pd.to_numeric(chain['strike_price'], errors='coerce')
                        if max_strike is not None:
                            chain = chain[sp <= float(max_strike)].copy()
        except Exception:
            pass

        # Fetch snapshots for option codes in batches
        option_codes = [str(x) for x in chain['code'].tolist() if isinstance(x, str) and x]

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
        snapshot_requested_codes = snapshot_result.requested_codes_count

        rows: list[dict[str, Any]] = []

        for _, r in chain.iterrows():
            opt_code = str(r.get('code'))
            exp = str(r.get('expiration'))
            try:
                dte = (_as_date(exp) - today).days
            except Exception:
                dte = None

            strike = to_float(r.get('strike_price'))
            option_type = str(r.get('option_type') or '').lower()
            if option_type in ('call', 'put'):
                pass
            else:
                # futu option_type might be 'CALL'/'PUT' or numeric; best-effort
                if 'call' in option_type:
                    option_type = 'call'
                elif 'put' in option_type:
                    option_type = 'put'

            # Filter by option type
            ot_set = set([s.strip().lower() for s in str(option_types or '').split(',') if s.strip()])
            if ot_set and option_type and (option_type not in ot_set):
                continue

            # Filter by strike range (best-effort)
            if strike is not None:
                side_window = (side_strike_windows or {}).get(option_type) if option_type else None
                side_min = to_float((side_window or {}).get('min_strike')) if isinstance(side_window, dict) else None
                side_max = to_float((side_window or {}).get('max_strike')) if isinstance(side_window, dict) else None
                effective_min = side_min if side_min is not None else min_strike
                effective_max = side_max if side_max is not None else max_strike
                if (effective_min is not None) and (strike < float(effective_min)):
                    continue
                if (effective_max is not None) and (strike > float(effective_max)):
                    continue

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
            lot_size = _safe_int(r.get('lot_size'))
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
        fetch_errors = fetch_result_meta.get('errors') if isinstance(fetch_result_meta.get('errors'), list) else []
        combined_errors = [*fetch_errors, *snapshot_errors]
        snapshot_error_code = next(
            (
                str(item.get('error_code'))
                for item in snapshot_errors
                if isinstance(item, dict) and str(item.get('error_code') or '').strip()
            ),
            None,
        )
        status = str(fetch_result_meta.get('status') or 'ok')
        error_code = fetch_result_meta.get('error_code') or snapshot_error_code
        if snapshot_errors and status == 'ok':
            status = 'partial' if snap_map else 'error'
        fetch_error_message = next(
            (
                str(item.get('message'))
                for item in combined_errors
                if isinstance(item, dict) and str(item.get('message') or '').strip()
            ),
            None,
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
                'expiration_statuses': fetch_result_meta.get('expiration_statuses') or {},
                'errors': combined_errors,
                'from_cache_expirations': fetch_result_meta.get('from_cache_expirations') or [],
                'fetched_expirations': fetch_result_meta.get('fetched_expirations') or [],
                'expiration_opend_calls': int(fetch_result_meta.get('expiration_opend_calls') or 0),
                'expiration_cache_hits': int(fetch_result_meta.get('expiration_cache_hits') or 0),
                'opend_call_count': int(fetch_result_meta.get('opend_call_count') or 0),
                'rate_gate_wait_sec': float(fetch_result_meta.get('rate_gate_wait_sec') or 0.0),
                'spot_snapshot_opend_calls': int(spot_fetch_meta.get('spot_snapshot_opend_calls') or 0),
                'spot_snapshot_requested_codes': int(spot_fetch_meta.get('spot_snapshot_requested_codes') or 0),
                'option_codes': len(option_codes),
                'snapshot_requested_codes': int(snapshot_requested_codes),
                'snapshot_opend_call_count': int(snapshot_opend_call_count),
                'snapshots_rows': int(len(snap_map)),
                'snapshot_fallback_filled': int(snapshot_fallback_filled),
                'snapshot_fallback_failed': int(snapshot_fallback_failed),
                'snapshot_errors': snapshot_errors,
                'spot_errors': spot_errors,
                'side_strike_windows': side_strike_windows or {},
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
            },
        }

    finally:
        if not external_gateway:
            try:
                gateway.close()
            except Exception:
                pass
