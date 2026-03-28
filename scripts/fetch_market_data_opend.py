#!/usr/bin/env python3
from __future__ import annotations

"""Fetch required option data using Futu OpenD (via futu-api).

Outputs the same CSV schema as `scripts/fetch_market_data.py` so downstream scanners keep working.

This is intentionally **minimal and pragmatic**:
- Fetch option contracts via `get_option_chain(underlier_code)`
- Choose the first N expirations (closest)
- Fetch per-contract quotes/greeks via `get_market_snapshot(option_codes)` in batches

Notes:
- This script requires `futu-api` + its deps (pandas/numpy/protobuf/pycryptodome/simplejson).
- For US underliers, your OpenD might not have stock quote right; spot may fail.
  In that case you can pass `--spot` manually or keep using Yahoo-based script for spot.

Usage:
  python3 scripts/fetch_market_data_opend.py --symbols HK.00700 --limit-expirations 2
  python3 scripts/fetch_market_data_opend.py --symbols 00700.HK --limit-expirations 2
  python3 scripts/fetch_market_data_opend.py --symbols NVDA --limit-expirations 2

"""

import argparse
import json
import math
import random
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import json


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
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.opend_utils import normalize_underlier


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


def _pick_col(row: pd.Series, *cands: str):
    for c in cands:
        if c in row and pd.notna(row[c]):
            return row[c]
    return None


def get_spot_opend(ctx, underlier_code: str) -> float | None:
    """Try to get underlying spot from OpenD."""
    try:
        ret, df = ctx.get_market_snapshot([underlier_code])
        if ret != 0 or df is None or df.empty:
            return None
        v = to_float(df.iloc[0].get('last_price'))
        return v
    except Exception:
        return None


def fetch_symbol(symbol: str, limit_expirations: int | None = None, host: str = '127.0.0.1', port: int = 11111, spot_override: float | None = None, *, spot_from_pm: bool = False, base_dir: Path | None = None, option_types: str = 'put,call', min_strike: float | None = None, max_strike: float | None = None, retry_max_attempts: int = 4, retry_time_budget_sec: float = 8.0, retry_base_delay_sec: float = 0.8, retry_max_delay_sec: float = 6.0, no_retry: bool = False) -> dict[str, Any]:
    from futu import OpenQuoteContext, RET_OK

    u = normalize_underlier(symbol)
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        def _is_rate_limited(err: str) -> bool:
            s = (err or '')
            sl = s.lower()
            return ('频率太高' in s) or ('最多10次' in s) or ('rate limit' in sl) or ('too frequent' in sl)

        def _is_transient(err: str) -> bool:
            sl = (err or '').lower()
            keys = ['timeout', 'timed out', 'econnreset', 'econnrefused', 'connection', 'disconnected', 'callclose']
            return any(k in sl for k in keys)

        def _opend_call_with_retry(what: str, fn, quiet: bool = False):
            if no_retry or (retry_max_attempts <= 1):
                return fn()
            t0 = time.monotonic()
            attempt = 0
            delay = float(retry_base_delay_sec or 0.5)
            max_delay = float(retry_max_delay_sec or 6.0)
            budget = float(retry_time_budget_sec or 0.0)
            last_err = None
            while True:
                attempt += 1
                try:
                    out = fn()
                    if isinstance(out, tuple) and len(out) >= 2:
                        ret, data = out[0], out[1]
                        if ret == RET_OK:
                            return out
                        last_err = str(data)
                        raise RuntimeError(f"ret={ret} err={last_err}")
                    return out
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"

                if attempt >= int(retry_max_attempts):
                    raise RuntimeError(f"{what} failed after {attempt} attempts: {last_err}")

                sleep_s = min(max_delay, max(0.0, delay))
                if _is_rate_limited(last_err or ''):
                    sleep_s = max(sleep_s, 2.0)

                if (budget > 0) and ((time.monotonic() - t0) + sleep_s > budget):
                    raise RuntimeError(f"{what} failed (retry budget {budget}s exceeded): {last_err}")

                # If not transient or rate-limited, don't keep retrying.
                if (not _is_transient(last_err or '')) and (not _is_rate_limited(last_err or '')):
                    raise RuntimeError(f"{what} failed (non-transient): {last_err}")

                if not quiet:
                    print(f"[WARN] {what} failed (attempt {attempt}/{retry_max_attempts}): {last_err}; sleep {sleep_s:.1f}s")

                time.sleep(sleep_s + random.uniform(0.0, 0.2))
                delay = min(max_delay, delay * 2.0)

        # Fail fast if OpenD requires phone verification / cannot connect.
        # Without this, futu-api may retry for minutes, hanging unattended cron jobs.
        try:
            # Prefer get_global_state() which exists across futu-api versions.
            ret0, state = _opend_call_with_retry('get_global_state', lambda: ctx.get_global_state(), quiet=True)
            if ret0 != RET_OK:
                raise RuntimeError(f"OpenD not ready (get_global_state ret={ret0})")
            if not isinstance(state, dict) or state.get('program_status_type') not in (None, '', 'READY'):
                # If OpenD is not READY, it may be waiting for phone verification.
                raise RuntimeError(f"OpenD not READY: {state}")
            if not state.get('qot_logined', True):
                raise RuntimeError(f"OpenD quote not logged in: {state}")
        except Exception:
            # Close ctx then propagate.
            raise
        spot = spot_override

        # Spot policy:
        # - HK/CN: try OpenD snapshot (usually available)
        # - US: do NOT attempt OpenD spot by default (often no stock quote right); use external fallback(s)
        if spot is None:
            if u.market != 'US':
                spot = get_spot_opend(ctx, u.code)

        # US spot: do not use OpenD (often no stock quote right).
        # Preferred fallback is portfolio-management's PriceFetcher (it has caching + multiple sources).
        # If still missing, keep None and require explicit --spot from user.
        if spot is None and u.market == 'US' and spot_from_pm and base_dir is not None:
            # portfolio_context does not include price; fetch it from portfolio-management via subprocess
            # to avoid dependency/import issues.
            try:
                import json
                import subprocess
                from pathlib import Path as _Path

                ctx_path = (base_dir / 'output' / 'state' / 'portfolio_context.json').resolve()
                if ctx_path.exists() and ctx_path.stat().st_size > 0:
                    ctxj = json.loads(ctx_path.read_text(encoding='utf-8'))
                    ticker = u.code.split('.', 1)[1]
                    stock = (ctxj.get('stocks_by_symbol') or {}).get(ticker)
                    if stock and stock.get('symbol'):
                        # Use portfolio-management's PriceFetcher directly (no Feishu deps).
                        pm_dir = (_Path(__file__).resolve().parents[2] / 'portfolio-management').resolve()
                        # IMPORTANT: do NOT .resolve() here, otherwise we lose the venv context (symlink collapses to system python)
                        pm_py = pm_dir / '.venv' / 'bin' / 'python'
                        code = (
                            "import sys, json; "
                            "sys.path.insert(0, '.'); "
                            "from src.price_fetcher import PriceFetcher; "
                            f"r=PriceFetcher().fetch('{ticker}'); "
                            "print(json.dumps(r, ensure_ascii=False))"
                        )
                        out = subprocess.check_output([str(pm_py), '-c', code], cwd=str(pm_dir), timeout=12)
                        txt = out.decode('utf-8', errors='ignore')
                        # price_fetcher prints rate-cache logs; extract the last JSON object in output.
                        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
                        jline = None
                        for ln in reversed(lines):
                            if ln.startswith('{') and ln.endswith('}'):
                                jline = ln
                                break
                        r = json.loads(jline or '{}')
                        if r and r.get('price'):
                            spot = float(r.get('price'))
            except Exception:
                pass
        # spot may still be None; keep it. Downstream scans will skip rows if spot is required.
        if spot is None and u.market == 'US' and (not spot_from_pm):
            # Make it explicit in meta by leaving spot None; caller can provide --spot.
            pass

        ret, chain = _opend_call_with_retry('get_option_chain', lambda: ctx.get_option_chain(u.code), quiet=False)
        if ret != RET_OK:
            raise RuntimeError(f"get_option_chain failed: {chain}")

        if chain is None or chain.empty:
            return {
                'symbol': symbol,
                'underlier_code': u.code,
                'spot': spot,
                'expiration_count': 0,
                'expirations': [],
                'rows': [],
                'meta': {'source': 'opend', 'error': 'empty_chain'},
            }

        # Derive expirations (strike_time) and pick first N
        chain = chain.copy()
        chain['expiration'] = chain['strike_time'].astype(str).str.slice(0, 10)
        expirations = sorted({x for x in chain['expiration'].tolist() if isinstance(x, str) and len(x) >= 10})
        if limit_expirations:
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
            if (min_strike is not None) or (max_strike is not None):
                if 'strike_price' in chain.columns:
                    sp = pd.to_numeric(chain['strike_price'], errors='coerce')
                    if min_strike is not None:
                        chain = chain[sp >= float(min_strike)].copy()
                        sp = pd.to_numeric(chain['strike_price'], errors='coerce')
                    if max_strike is not None:
                        chain = chain[sp <= float(max_strike)].copy()
        except Exception:
            pass

        # Fetch snapshots for option codes in batches
        option_codes = [str(x) for x in chain['code'].tolist() if isinstance(x, str) and x]

        snapshots: list[pd.DataFrame] = []
        BATCH = 200
        for i in range(0, len(option_codes), BATCH):
            batch = option_codes[i:i+BATCH]
            try:
                ret2, snap = _opend_call_with_retry('get_market_snapshot(batch)', lambda: ctx.get_market_snapshot(batch), quiet=True)
            except Exception:
                ret2, snap = (None, None)
            if ret2 == RET_OK and snap is not None and not snap.empty:
                snapshots.append(snap)

        snap_df = pd.concat(snapshots, ignore_index=True) if snapshots else pd.DataFrame()
        snap_map = {}
        if not snap_df.empty and 'code' in snap_df.columns:
            for _, r in snap_df.iterrows():
                code = str(r.get('code'))
                if code:
                    snap_map[code] = r

        today = date.today()
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
                if (min_strike is not None) and (strike < float(min_strike)):
                    continue
                if (max_strike is not None) and (strike > float(max_strike)):
                    continue

            srow = snap_map.get(opt_code)
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
                'option_codes': len(option_codes),
                'snapshots_rows': int(len(snap_df)) if not snap_df.empty else 0,
            },
        }
    except Exception as e:
        return {
            'symbol': symbol,
            'underlier_code': (u.code if 'u' in locals() else None),
            'spot': spot_override,
            'expiration_count': 0,
            'expirations': [],
            'rows': [],
            'meta': {'source': 'opend', 'host': host, 'port': port, 'error': f'{type(e).__name__}: {e}'},
        }

    finally:
        try:
            ctx.close()
        except Exception:
            pass


def save_outputs(base: Path, symbol: str, payload: dict[str, Any]):
    raw_dir = base / 'output' / 'raw'
    parsed_dir = base / 'output' / 'parsed'
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    df = pd.DataFrame(payload.get('rows') or [])
    if df.empty:
        df = pd.DataFrame(columns=COLUMNS)
    else:
        # ensure stable columns order
        for c in COLUMNS:
            if c not in df.columns:
                df[c] = pd.NA
        df = df[COLUMNS]
    df.to_csv(csv_path, index=False)
    return raw_path, csv_path


def main():
    ap = argparse.ArgumentParser(description='Fetch required option data from Futu OpenD')
    ap.add_argument('--symbols', nargs='+', required=True)
    ap.add_argument('--limit-expirations', type=int, default=2)
    ap.add_argument('--option-types', default='put,call', help='Comma-separated option types to include: put,call (default: put,call)')
    ap.add_argument('--min-strike', type=float, default=None)
    ap.add_argument('--max-strike', type=float, default=None)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=11111)
    ap.add_argument('--spot', type=float, default=None, help='override spot if OpenD has no quote right')
    ap.add_argument('--spot-from-pm', action='store_true', help='for US symbols: if OpenD has no stock quote right, fallback to portfolio-management get_price()')
    ap.add_argument('--quiet', action='store_true', help='quiet mode: suppress non-critical prints')
    ap.add_argument('--no-retry', action='store_true', help='Disable OpenD retries/backoff')
    ap.add_argument('--retry-max-attempts', type=int, default=4)
    ap.add_argument('--retry-time-budget-sec', type=float, default=8.0)
    ap.add_argument('--retry-base-delay-sec', type=float, default=0.8)
    ap.add_argument('--retry-max-delay-sec', type=float, default=6.0)
    args = ap.parse_args()

    opt_types = set([s.strip().lower() for s in str(args.option_types or '').split(',') if s.strip()])
    want_put = ('put' in opt_types) if opt_types else True
    want_call = ('call' in opt_types) if opt_types else True

    base = Path(__file__).resolve().parents[1]

    opend_metrics_path = (base / 'output_shared' / 'state' / 'opend_metrics.json').resolve()

    for sym in args.symbols:
        t0 = time.monotonic()
        payload = fetch_symbol(
            sym,
            limit_expirations=args.limit_expirations,
            host=args.host,
            port=args.port,
            spot_override=args.spot,
            spot_from_pm=bool(args.spot_from_pm),
            base_dir=base,
            option_types=('put,call' if (want_put and want_call) else ('put' if want_put else 'call')),
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            retry_max_attempts=int(args.retry_max_attempts),
            retry_time_budget_sec=float(args.retry_time_budget_sec),
            retry_base_delay_sec=float(args.retry_base_delay_sec),
            retry_max_delay_sec=float(args.retry_max_delay_sec),
            no_retry=bool(args.no_retry),
        )
        raw_path, csv_path = save_outputs(base, sym, payload)
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
