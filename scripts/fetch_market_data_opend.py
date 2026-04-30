#!/usr/bin/env python3
from __future__ import annotations

"""Fetch required option data using Futu OpenD (via futu-api).

Outputs the same CSV schema as the legacy fetcher so downstream scanners keep working.

This is intentionally **minimal and pragmatic**:
- Fetch option contracts via `get_option_chain(underlier_code)`
- Choose the first N expirations (closest)
- Fetch per-contract quotes/greeks via `get_market_snapshot(option_codes)` in batches

Notes:
- This script requires `futu-api` + its deps (pandas/numpy/protobuf/pycryptodome/simplejson).
- For US underliers, your OpenD might not have stock quote right; spot may fail.
  In that case you can pass `--spot` manually.

Usage:
  python3 scripts/fetch_market_data_opend.py --symbols HK.00700 --limit-expirations 2
  python3 scripts/fetch_market_data_opend.py --symbols 00700.HK --limit-expirations 2
  python3 scripts/fetch_market_data_opend.py --symbols NVDA --limit-expirations 2

"""

import argparse
import json
import math
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd



def _append_metrics_json(metrics_path: Path, payload: dict, max_entries: int = 400):
    """Append payload into a bounded JSON list file. Keeps last max_entries records."""
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if metrics_path.exists() and metrics_path.stat().st_size > 0:
            try:
                obj = json.loads(metrics_path.read_text(encoding='utf-8'))
                if isinstance(obj, list):
                    arr = obj
            except Exception:
                arr = []
        arr.append(payload)
        if len(arr) > int(max_entries):
            arr = arr[-int(max_entries):]
        metrics_path.write_text(json.dumps(arr, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass


COLUMNS = [
    'symbol','option_type','expiration','dte','contract_symbol','strike','spot',
    'bid','ask','last_price','mid','volume','open_interest','implied_volatility',
    'in_the_money','currency','otm_pct','delta','multiplier'
]

# Allow running as a script (python scripts/xxx.py) without package install
# by ensuring repo root is on sys.path.
import sys
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.futu_gateway import (
    build_ready_futu_gateway,
    retry_futu_gateway_call,
)
from scripts.opend_utils import normalize_underlier, get_trading_date
from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.option_chain_fetching import (
    OptionChainFetchRequest,
    classify_option_chain_error,
    fetch_option_chains,
    prune_option_chain_cache,
)

def _chain_cache_path(base_dir: Path, u_code: str) -> Path:
    safe = u_code.replace('.', '_')
    return base_dir / 'cache' / 'opend_option_chain' / f'{safe}.json'


def _load_chain_cache(path: Path) -> dict | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        import json
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_chain_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding='utf-8')
    except Exception:
        pass


def _prune_chain_cache(base_dir: Path, keep_days: int) -> None:
    try:
        prune_option_chain_cache(base_dir, keep_days)
    except Exception:
        pass


def _is_chain_cache_fresh(obj: dict, today: date) -> bool:
    try:
        if not isinstance(obj, dict):
            return False
        asof = obj.get('asof_date')
        if not asof:
            return False
        return str(asof) == today.isoformat()
    except Exception:
        return False


def _chain_cache_covers_explicit_expirations(obj: dict, explicit_expirations: list[str] | None) -> bool:
    try:
        requested = sorted({
            exp
            for exp in (normalize_expiration_ymd(x) for x in (explicit_expirations or []))
            if exp
        })
        if not requested:
            return True
        if not isinstance(obj, dict):
            return False
        rows = obj.get('rows') or []
        cached = {
            exp
            for row in rows
            if isinstance(row, dict)
            for exp in [normalize_expiration_ymd((row or {}).get('strike_time') or (row or {}).get('expiration'))]
            if exp
        }
        return all(exp in cached for exp in requested)
    except Exception:
        return False


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


def get_spot_opend(gateway, underlier_code: str) -> float | None:
    """Try to get underlying spot from OpenD."""
    try:
        df = gateway.get_snapshot([underlier_code])
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        # Prefer last_price; fallback to other common fields.
        for k in ['last_price', 'price', 'cur_price', 'close_price_5min', 'open_price', 'prev_close_price']:
            v = to_float(row.get(k))
            if v is not None and v > 0:
                return v
        return None
    except Exception:
        return None


def get_underlier_spot(symbol: str, *, host: str = "127.0.0.1", port: int = 11111, base_dir: Path | None = None) -> float | None:
    _ = base_dir
    gateway = build_ready_futu_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        return get_spot_opend(gateway, normalize_underlier(symbol).code)
    finally:
        try:
            gateway.close()
        except Exception:
            pass


def list_option_expirations(symbol: str, *, host: str = "127.0.0.1", port: int = 11111, base_dir: Path | None = None) -> list[str]:
    _ = base_dir
    gateway = build_ready_futu_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        df_e = retry_futu_gateway_call(
            'get_option_expiration_date',
            lambda: gateway.get_option_expiration_dates(normalize_underlier(symbol).code),
            quiet=False,
        )
        if df_e is None or df_e.empty:
            return []
        return sorted({
            exp
            for exp in (normalize_expiration_ymd(x) for x in df_e.get('strike_time').tolist())
            if exp
        })
    finally:
        try:
            gateway.close()
        except Exception:
            pass


def fetch_symbol(symbol: str, limit_expirations: int | None = None, host: str = '127.0.0.1', port: int = 11111, spot_override: float | None = None, *, base_dir: Path | None = None, option_types: str = 'put,call', min_strike: float | None = None, max_strike: float | None = None, side_strike_windows: dict[str, dict[str, float | None]] | None = None, min_dte: int | None = None, max_dte: int | None = None, explicit_expirations: list[str] | None = None, retry_max_attempts: int = 4, retry_time_budget_sec: float = 8.0, retry_base_delay_sec: float = 0.8, retry_max_delay_sec: float = 6.0, no_retry: bool = False, chain_cache: bool = False, chain_cache_force_refresh: bool = False, freshness_policy: str = 'cache_first', max_wait_sec: float = 90.0, option_chain_window_sec: float = 30.0, option_chain_max_calls: int = 10) -> dict[str, Any]:
    u = normalize_underlier(symbol)
    explicit_expirations_norm = sorted({
        exp
        for exp in (normalize_expiration_ymd(x) for x in (explicit_expirations or []))
        if exp
    })
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
            spot = get_spot_opend(gateway, u.code)

        # Trading-date anchor for DTE / cache freshness.
        today = get_trading_date(u.market)

        # IMPORTANT:
        # futu-api get_option_chain() defaults to an expiration date window of [today, today+30d]
        # when start/end are None. For some underliers (e.g., HK.09992), the next expiry may be
        # beyond 30 days, which makes the default call look like it has only 0DTE options.
        #
        # So we:
        # 1) call get_option_expiration_date() to enumerate expirations
        # 2) take the closest N expirations (limit_expirations)
        # 3) delegate per-expiration chain fetches to the shared coordinator, which owns
        #    cross-process rate limiting and per-expiration cache shards.
        if explicit_expirations_norm:
            expirations_all = explicit_expirations_norm
        else:
            try:
                df_e = retry_futu_gateway_call(
                    'get_option_expiration_date',
                    lambda: gateway.get_option_expiration_dates(u.code),
                    no_retry=no_retry,
                    retry_max_attempts=retry_max_attempts,
                    retry_time_budget_sec=retry_time_budget_sec,
                    retry_base_delay_sec=retry_base_delay_sec,
                    retry_max_delay_sec=retry_max_delay_sec,
                    quiet=False,
                )
                if df_e is None or df_e.empty:
                    expirations_all = []
                else:
                    expirations_all = sorted({
                        exp
                        for exp in (normalize_expiration_ymd(x) for x in df_e.get('strike_time').tolist())
                        if exp
                    })
            except Exception:
                expirations_all = []

        expirations_pick0 = expirations_all
        # If min_dte/max_dte is requested, filter expirations by DTE window.
        if expirations_all and (not explicit_expirations_norm) and ((min_dte is not None) or (max_dte is not None)):
            try:
                from datetime import datetime
                today0 = today
                filtered = []
                for e in expirations_all:
                    try:
                        d0 = datetime.fromisoformat(str(e)[:10]).date()
                        dte0 = int((d0 - today0).days)
                        if (min_dte is not None) and (dte0 < int(min_dte)):
                            continue
                        if (max_dte is not None) and (dte0 > int(max_dte)):
                            continue
                        filtered.append(str(e)[:10])
                    except Exception:
                        continue
                expirations_pick0 = filtered if filtered else expirations_all
            except Exception:
                expirations_pick0 = expirations_all

        if explicit_expirations_norm:
            expirations_pick = expirations_pick0
        elif limit_expirations and expirations_pick0:
            expirations_pick = expirations_pick0[: int(limit_expirations)]
        else:
            expirations_pick = expirations_pick0

        effective_policy = 'force_refresh' if chain_cache_force_refresh else str(freshness_policy or 'cache_first')
        chain_result = fetch_option_chains(
            gateway=gateway,
            request=OptionChainFetchRequest(
                symbol=symbol,
                underlier_code=u.code,
                expirations=list(expirations_pick),
                host=host,
                port=int(port),
                option_types=option_types,
                strike_windows=side_strike_windows or {},
                base_dir=(Path(base_dir) if base_dir is not None else REPO_ROOT),
                asof_date=today.isoformat(),
                freshness_policy=effective_policy if effective_policy in {'cache_first', 'refresh_missing', 'force_refresh'} else 'cache_first',
                chain_cache=bool(chain_cache),
                max_wait_sec=float(max_wait_sec),
                window_sec=float(option_chain_window_sec),
                max_calls=int(option_chain_max_calls),
                is_force_refresh=bool(chain_cache_force_refresh or effective_policy == 'force_refresh'),
                no_retry=no_retry,
                retry_max_attempts=retry_max_attempts,
                retry_time_budget_sec=retry_time_budget_sec,
                retry_base_delay_sec=retry_base_delay_sec,
                retry_max_delay_sec=retry_max_delay_sec,
            ),
            retry_call=retry_futu_gateway_call,
        )

        chain_obj = {
            'asof_date': today.isoformat(),
            'underlier_code': u.code,
            'rows': chain_result.rows,
            'expirations_all': expirations_all,
            'expirations_pick': expirations_pick,
            'fetch_result': chain_result.to_meta(),
        }

        # Rehydrate into a DataFrame for existing downstream logic.
        chain = pd.DataFrame(chain_obj.get('rows') or [])
        if chain is None or chain.empty:
            fetch_meta = dict(chain_obj.get('fetch_result') or {})
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
                    'from_cache_expirations': fetch_meta.get('from_cache_expirations') or [],
                    'fetched_expirations': fetch_meta.get('fetched_expirations') or [],
                    'opend_call_count': int(fetch_meta.get('opend_call_count') or 0),
                },
            }

        # Derive expirations (strike_time) and pick first N
        chain = chain.copy()
        chain['expiration'] = chain['strike_time'].astype(str).str.slice(0, 10)
        expirations = sorted({x for x in chain['expiration'].tolist() if isinstance(x, str) and len(x) >= 10})
        if explicit_expirations_norm:
            expirations = [exp for exp in explicit_expirations_norm if exp in set(expirations)]
        elif limit_expirations:
            expirations = expirations[: int(limit_expirations)]

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

        # Build a minimal snapshot map directly (avoid concatenating large DataFrames / storing Series for memory efficiency)
        snap_map: dict[str, dict[str, Any]] = {}
        BATCH = 200
        for i in range(0, len(option_codes), BATCH):
            batch = option_codes[i:i+BATCH]
            try:
                snap = retry_futu_gateway_call(
                    'get_market_snapshot(batch)',
                    lambda: gateway.get_snapshot(batch),
                    no_retry=no_retry,
                    retry_max_attempts=retry_max_attempts,
                    retry_time_budget_sec=retry_time_budget_sec,
                    retry_base_delay_sec=retry_base_delay_sec,
                    retry_max_delay_sec=retry_max_delay_sec,
                    quiet=True,
                )
            except Exception:
                snap = None
            if snap is None or snap.empty:
                continue

            # Extract only the columns we actually use downstream.
            cols = set(snap.columns)
            want = [
                'code',
                'last_price',
                'bid_price',
                'ask_price',
                'volume',
                'option_open_interest',
                'option_implied_volatility',
                'option_delta',
                'option_contract_multiplier',
                # fallbacks that sometimes appear
                'lot_size',
                'open_interest',
                'implied_volatility',
                'delta',
                'bid',
                'ask',
            ]
            keep = [c for c in want if c in cols]
            if not keep or 'code' not in keep:
                continue

            try:
                for rec in snap[keep].to_dict(orient='records'):
                    code = str(rec.get('code') or '')
                    if code:
                        snap_map[code] = rec
            except Exception:
                # Fallback: slower but robust
                try:
                    for _, r in snap.iterrows():
                        code = str(r.get('code') or '')
                        if not code:
                            continue
                        snap_map[code] = {k: r.get(k) for k in keep}
                except Exception:
                    pass

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
                from scripts.opend_normalize import normalize_iv
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

        fetch_result_meta = chain_obj.get('fetch_result') or {}
        fetch_errors = fetch_result_meta.get('errors') if isinstance(fetch_result_meta.get('errors'), list) else []
        fetch_error_message = next(
            (
                str(item.get('message'))
                for item in fetch_errors
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
                'status': str(fetch_result_meta.get('status') or 'ok'),
                'error_code': fetch_result_meta.get('error_code'),
                'error': fetch_error_message,
                'expiration_statuses': fetch_result_meta.get('expiration_statuses') or {},
                'errors': fetch_errors,
                'from_cache_expirations': fetch_result_meta.get('from_cache_expirations') or [],
                'fetched_expirations': fetch_result_meta.get('fetched_expirations') or [],
                'opend_call_count': int(fetch_result_meta.get('opend_call_count') or 0),
                'option_codes': len(option_codes),
                'snapshots_rows': int(len(snap_map)),
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
            },
        }

    finally:
        try:
            gateway.close()
        except Exception:
            pass


def save_outputs(base: Path, symbol: str, payload: dict[str, Any], *, output_root: Path | None = None):
    root = (output_root.resolve() if output_root is not None else (base / 'output').resolve())
    raw_dir = root / 'raw'
    parsed_dir = root / 'parsed'
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    # Boundary validation: drop rows missing critical fields (strike/expiration/dte/option_type)
    try:
        from scripts.required_data_validate import validate_required_rows

        rows0 = payload.get('rows') or []
        rows1, st = validate_required_rows(rows0)
        payload['rows'] = rows1
        meta = payload.get('meta') or {}
        if not isinstance(meta, dict):
            meta = {'meta': str(meta)}
        meta['validation'] = {
            'total_rows': int(st.total_rows),
            'kept_rows': int(st.kept_rows),
            'dropped_rows': int(st.dropped_rows),
            'missing_strike': int(st.missing_strike),
            'missing_expiration': int(st.missing_expiration),
            'missing_dte': int(st.missing_dte),
            'missing_option_type': int(st.missing_option_type),
        }
        payload['meta'] = meta
    except Exception:
        pass

    # Atomic writes: avoid half-written json/csv when process is killed mid-write.
    from scripts.io_utils import atomic_write_text
    import io

    atomic_write_text(raw_path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')

    df = pd.DataFrame(payload.get('rows') or [])
    meta = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}
    if df.empty and str((meta or {}).get('status') or '').lower() == 'error' and csv_path.exists() and csv_path.stat().st_size > 0:
        return raw_path, csv_path

    if df.empty:
        df_out = pd.DataFrame(columns=COLUMNS)
    else:
        for c in COLUMNS:
            if c not in df.columns:
                df[c] = pd.NA
        df_out = df[COLUMNS]

    buf = io.StringIO()
    df_out.to_csv(buf, index=False)
    atomic_write_text(csv_path, buf.getvalue(), encoding='utf-8')
    return raw_path, csv_path


def main():
    ap = argparse.ArgumentParser(description='Fetch required option data from Futu OpenD')
    ap.add_argument('--symbols', nargs='+', required=True)
    ap.add_argument('--limit-expirations', type=int, default=2)
    ap.add_argument('--chain-cache', action='store_true', help='Enable option_chain day-cache (per underlier) to reduce OpenD calls')
    ap.add_argument('--chain-cache-force-refresh', action='store_true', help='Force refresh option_chain even if cache is fresh')
    ap.add_argument('--chain-cache-keep-days', type=int, default=7, help='Keep N days of option_chain cache files (default: 7)')
    ap.add_argument('--option-types', default='put,call', help='Comma-separated option types to include: put,call (default: put,call)')
    ap.add_argument('--min-strike', type=float, default=None)
    ap.add_argument('--max-strike', type=float, default=None)
    ap.add_argument('--min-dte', type=int, default=None, help='Only pick expirations with DTE >= min_dte before applying limit-expirations')
    ap.add_argument('--max-dte', type=int, default=None, help='Only pick expirations with DTE <= max_dte before applying limit-expirations')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=11111)
    ap.add_argument('--spot', type=float, default=None, help='override spot if OpenD has no quote right')
    ap.add_argument('--quiet', action='store_true', help='quiet mode: suppress non-critical prints')
    ap.add_argument('--no-retry', action='store_true', help='Disable OpenD retries/backoff')
    ap.add_argument('--retry-max-attempts', type=int, default=4)
    ap.add_argument('--retry-time-budget-sec', type=float, default=8.0)
    ap.add_argument('--retry-base-delay-sec', type=float, default=0.8)
    ap.add_argument('--retry-max-delay-sec', type=float, default=6.0)
    ap.add_argument('--option-chain-max-wait-sec', type=float, default=90.0, help='Max seconds to wait for shared option-chain rate-limit budget')
    ap.add_argument('--option-chain-window-sec', type=float, default=30.0, help='Shared option-chain rate-limit window seconds')
    ap.add_argument('--option-chain-max-calls', type=int, default=10, help='Shared option-chain max calls per window')
    ap.add_argument('--output-root', default=None, help='Output root containing raw/ and parsed/ (default: ./output)')
    args = ap.parse_args()

    opt_types = set([s.strip().lower() for s in str(args.option_types or '').split(',') if s.strip()])
    want_put = ('put' in opt_types) if opt_types else True
    want_call = ('call' in opt_types) if opt_types else True

    base = Path(__file__).resolve().parents[1]
    output_root = (Path(args.output_root).resolve() if args.output_root else None)

    if args.chain_cache:
        _prune_chain_cache(base, int(args.chain_cache_keep_days))

    opend_metrics_path = (base / 'output_shared' / 'state' / 'opend_metrics.json').resolve()

    for sym in args.symbols:
        t0 = time.monotonic()
        payload = fetch_symbol(
            sym,
            limit_expirations=args.limit_expirations,
            host=args.host,
            port=args.port,
            spot_override=args.spot,
            base_dir=base,
            chain_cache=bool(args.chain_cache),
            chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
            option_types=('put,call' if (want_put and want_call) else ('put' if want_put else 'call')),
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            retry_max_attempts=int(args.retry_max_attempts),
            retry_time_budget_sec=float(args.retry_time_budget_sec),
            retry_base_delay_sec=float(args.retry_base_delay_sec),
            retry_max_delay_sec=float(args.retry_max_delay_sec),
            no_retry=bool(args.no_retry),
            max_wait_sec=float(args.option_chain_max_wait_sec),
            option_chain_window_sec=float(args.option_chain_window_sec),
            option_chain_max_calls=int(args.option_chain_max_calls),
        )
        raw_path, csv_path = save_outputs(base, sym, payload, output_root=output_root)
        try:
            meta = payload.get('meta') or {}
            _append_metrics_json(opend_metrics_path, {
                'as_of_utc': datetime.now().astimezone().isoformat(),
                'symbol': sym,
                'ms': int((time.monotonic() - t0) * 1000),
                'rows': int(len(payload.get('rows') or [])),
                'expiration_count': int(payload.get('expiration_count') or 0),
                'underlier_code': payload.get('underlier_code'),
                'host': meta.get('host'),
                'port': meta.get('port'),
                'error': meta.get('error'),
            })
        except Exception:
            pass
        if not args.quiet:
            print(f"[OK] {sym} source=opend")
            print(f"  underlier={payload.get('underlier_code')} spot={payload.get('spot')}")
            print(f"  expirations={payload.get('expiration_count')} rows={len(payload.get('rows') or [])}")
            print(f"  raw={raw_path}")
            print(f"  csv={csv_path}")


if __name__ == '__main__':
    main()
