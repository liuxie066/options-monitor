#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError
import yaml


def run(cmd: list[str], cwd: Path, timeout_sec: int | None = None):
    """Run a subprocess with optional timeout.

    Timeout is important for unattended cron usage: a single hanging symbol must not block the whole pipeline.

    Scheduled mode policy:
    - capture stdout/stderr to reduce log I/O
    - only print tail on failure
    """
    if not IS_SCHEDULED:
        print(f"[RUN] {' '.join(cmd)}" + (f" (timeout={timeout_sec}s)" if timeout_sec else ""))

    try:
        if IS_SCHEDULED:
            result = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec, capture_output=True, text=True)
        else:
            result = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout_sec}s: {' '.join(cmd)}")

    if result.returncode != 0:
        if IS_SCHEDULED:
            out = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
            if out:
                tail = '\n'.join(out.splitlines()[-60:])
                print(f"[ERR] {' '.join(cmd)}\n{tail}")
        raise SystemExit(result.returncode)


def safe_read_csv(path: Path) -> pd.DataFrame:
    try:
        if path.exists() and path.stat().st_size > 0:
            return pd.read_csv(path)
    except EmptyDataError:
        pass
    return pd.DataFrame()


def copy_if_exists(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and src.stat().st_size > 0:
        shutil.copyfile(src, dst)
    else:
        pd.DataFrame().to_csv(dst, index=False)


def is_fresh(path: Path, max_age_sec: int) -> bool:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        age = time.time() - path.stat().st_mtime
        return age <= float(max_age_sec)
    except Exception:
        return False


def load_cached_json(path: Path) -> dict | None:
    """Best-effort cached JSON loader.

    Returns None if file is missing/invalid/clearly incomplete.
    """
    try:
        if not path.exists() or path.stat().st_size <= 2:
            return None
        obj = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(obj, dict):
            return None
        # sanity keys
        if 'as_of_utc' not in obj and 'filters' not in obj:
            return None
        return obj
    except Exception:
        return None


def add_sell_put_labels(base: Path, input_path: Path, output_path: Path):
    df = safe_read_csv(input_path)

    def band(v):
        if pd.isna(v):
            return 'unknown'
        if v < 0.03:
            return '<3%'
        if v < 0.07:
            return '3%-7%'
        return '>=7%'

    def label(v):
        if pd.isna(v):
            return '未知'
        if v < 0.03:
            return '激进'
        if v < 0.07:
            return '中性'
        return '保守'

    if not df.empty:
        df['otm_band'] = df['otm_pct'].apply(band)
        df['risk_label'] = df['otm_pct'].apply(label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)


def summarize_sell_put(df: pd.DataFrame, symbol: str) -> dict:
    row = {
        'symbol': symbol,
        'strategy': 'sell_put',
        'candidate_count': 0,
        'top_contract': '',
        'expiration': '',
        'strike': None,
        'dte': None,
        'net_income': None,
        'annualized_return': None,
        'risk_label': '',
        'delta': None,
        'cash_secured_used_usd': 0.0,
        'cash_required_usd': None,
        'cash_available_usd': None,
        'cash_free_usd': None,
        'cash_available_usd_est': None,
        'cash_free_usd_est': None,
        'cash_available_cny': None,
        'cash_free_cny': None,
        'cash_required_cny': None,
        'mid': None,
        'bid': None,
        'ask': None,
        'option_ccy': None,
        'note': '无候选',
    }
    if df.empty:
        return row
    row['candidate_count'] = len(df)
    # Pick the top contract with a more execution-friendly preference:
    # prefer abs(delta) close to target, then higher annualized return, then net income.
    target_abs_delta = 0.22
    try:
        # Configurable target (symbol-level overrides template)
        target_abs_delta = float((symbol_cfg.get('sell_put') or {}).get('target_abs_delta') or target_abs_delta)
    except Exception:
        pass
    d = df.copy()
    try:
        if 'delta' in d.columns:
            d['_abs_delta'] = d['delta'].abs()
            d['_delta_dist'] = (d['_abs_delta'] - target_abs_delta).abs()
        else:
            d['_delta_dist'] = 999.0
    except Exception:
        d['_delta_dist'] = 999.0

    top = d.sort_values(
        ['_delta_dist', 'annualized_net_return_on_cash_basis', 'net_income'],
        ascending=[True, False, False],
    ).iloc[0]
    cash_secured_used = 0.0
    cash_avail = None
    cash_free = None
    cash_avail_est = None
    cash_free_est = None
    cash_avail_cny = None
    cash_free_cny = None
    cash_required_cny = None
    try:
        if 'cash_secured_used_usd' in df.columns and len(df) > 0:
            cash_secured_used = float(df['cash_secured_used_usd'].iloc[0] or 0.0)
        if 'cash_available_usd' in df.columns and len(df) > 0 and pd.notna(df['cash_available_usd'].iloc[0]):
            cash_avail = float(df['cash_available_usd'].iloc[0])
        if 'cash_free_usd' in df.columns and len(df) > 0 and pd.notna(df['cash_free_usd'].iloc[0]):
            cash_free = float(df['cash_free_usd'].iloc[0])
        if 'cash_available_usd_est' in df.columns and len(df) > 0 and pd.notna(df['cash_available_usd_est'].iloc[0]):
            cash_avail_est = float(df['cash_available_usd_est'].iloc[0])
        if 'cash_free_usd_est' in df.columns and len(df) > 0 and pd.notna(df['cash_free_usd_est'].iloc[0]):
            cash_free_est = float(df['cash_free_usd_est'].iloc[0])
        if 'cash_available_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_available_cny'].iloc[0]):
            cash_avail_cny = float(df['cash_available_cny'].iloc[0])
        if 'cash_free_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_free_cny'].iloc[0]):
            cash_free_cny = float(df['cash_free_cny'].iloc[0])
        if 'cash_required_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_required_cny'].iloc[0]):
            cash_required_cny = float(df['cash_required_cny'].iloc[0])
    except Exception:
        cash_secured_used = 0.0
        cash_avail = None
        cash_free = None
        cash_avail_est = None
        cash_free_est = None

    cash_required = None
    try:
        cash_required = float(top['strike']) * 100.0
    except Exception:
        cash_required = None

    row.update({
        'top_contract': f"{top['expiration']} {int(top['strike']) if float(top['strike']).is_integer() else top['strike']}P",
        'expiration': top['expiration'],
        'strike': float(top['strike']),
        'dte': int(top['dte']),
        'net_income': float(top['net_income']),
        'annualized_return': float(top['annualized_net_return_on_cash_basis']),
        'risk_label': top.get('risk_label', ''),
        'delta': (float(top['delta']) if 'delta' in top and pd.notna(top['delta']) else None),
        'cash_secured_used_usd': cash_secured_used,
        'cash_required_usd': cash_required,
        'cash_available_usd': cash_avail,
        'cash_free_usd': cash_free,
        'cash_available_usd_est': cash_avail_est,
        'cash_free_usd_est': cash_free_est,
        'cash_available_cny': cash_avail_cny,
        'cash_free_cny': cash_free_cny,
        'cash_required_cny': cash_required_cny,
        'mid': (float(top['mid']) if 'mid' in top else None),
        'bid': (float(top['bid']) if 'bid' in top and pd.notna(top['bid']) else None),
        'ask': (float(top['ask']) if 'ask' in top and pd.notna(top['ask']) else None),
        'option_ccy': ('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
        'note': '有候选',
    })
    return row


def summarize_sell_call(df: pd.DataFrame, symbol: str) -> dict:
    row = {
        'symbol': symbol,
        'strategy': 'sell_call',
        'candidate_count': 0,
        'top_contract': '',
        'expiration': '',
        'strike': None,
        'dte': None,
        'net_income': None,
        'annualized_return': None,
        'risk_label': '',
        'delta': None,
        'mid': None,
        'bid': None,
        'ask': None,
        'option_ccy': None,
        'note': '无候选',
    }
    if df.empty:
        return row
    row['candidate_count'] = len(df)
    # Prefer delta close to a steady target, then higher premium return.
    target_delta = 0.28
    try:
        target_delta = float((symbol_cfg.get('sell_call') or {}).get('target_delta') or target_delta)
    except Exception:
        pass
    d = df.copy()
    try:
        if 'delta' in d.columns:
            d['_delta_dist'] = (d['delta'] - target_delta).abs()
        else:
            d['_delta_dist'] = 999.0
    except Exception:
        d['_delta_dist'] = 999.0

    top = d.sort_values(
        ['_delta_dist', 'annualized_net_premium_return', 'if_exercised_total_return', 'net_income'],
        ascending=[True, False, False, False],
    ).iloc[0]
    cover_avail = 0
    try:
        cover_avail = int(top.get('covered_contracts_available', 0) or 0)
    except Exception:
        cover_avail = 0

    row.update({
        'top_contract': f"{top['expiration']} {int(top['strike']) if float(top['strike']).is_integer() else top['strike']}C",
        'expiration': top['expiration'],
        'strike': float(top['strike']),
        'dte': int(top['dte']),
        'net_income': float(top['net_income']),
        'annualized_return': float(top['annualized_net_premium_return']),
        'risk_label': top.get('risk_label', ''),
        'delta': (float(top['delta']) if 'delta' in top and pd.notna(top['delta']) else None),
        'mid': (float(top['mid']) if 'mid' in top else None),
        'bid': (float(top['bid']) if 'bid' in top and pd.notna(top['bid']) else None),
        'ask': (float(top['ask']) if 'ask' in top and pd.notna(top['ask']) else None),
        'option_ccy': ('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
        'note': f"有候选 | cover_avail {cover_avail} | shares_total {int(top.get('shares_total', 0) or 0)} | shares_locked {int(top.get('shares_locked', 0) or 0)}",
    })
    return row


def process_symbol(
    py: str,
    base: Path,
    symbol_cfg: dict,
    top_n: int,
    portfolio_ctx: dict | None = None,
    fx_usd_per_cny: float | None = None,
    hkdcny: float | None = None,
    timeout_sec: int | None = 120,
) -> list[dict]:
    symbol = symbol_cfg['symbol']
    symbol_lower = symbol.lower()
    limit_expirations = symbol_cfg.get('fetch', {}).get('limit_expirations', 8)
    report_dir = base / 'output' / 'reports'
    summary_rows: list[dict] = []

    # ===== Fetch market data =====
    # Default: Yahoo (US)
    # Optional: OpenD (HK/US) when fetch.source == 'opend'
    fetch_cfg = symbol_cfg.get('fetch', {}) or {}
    fetch_source = str(fetch_cfg.get('source') or 'yahoo').strip().lower()
    if fetch_source == 'opend':
        host = str(fetch_cfg.get('host') or '127.0.0.1')
        port = int(fetch_cfg.get('port') or 11111)
        cmd = [
            py, 'scripts/fetch_market_data_opend.py',
            '--symbols', symbol,
            '--limit-expirations', str(limit_expirations),
            '--host', host,
            '--port', str(port),
        ]
        # If OpenD has no US stock quote right, we can still compute OTM% using portfolio-management prices.
        if bool(fetch_cfg.get('spot_from_portfolio_management', False)):
            cmd.append('--spot-from-pm')
        if IS_SCHEDULED:
            # Quiet mode for fetch_market_data_opend.py
            cmd.append('--quiet')
        run(cmd, cwd=base, timeout_sec=timeout_sec)
    else:
        run([
            py, 'scripts/fetch_market_data.py',
            '--symbols', symbol,
            '--limit-expirations', str(limit_expirations),
        ], cwd=base, timeout_sec=timeout_sec)

    sp = symbol_cfg.get('sell_put', {})
    if sp.get('enabled', False):
        # If required_data.csv is empty (rate limit / data source failure), skip scans gracefully.
        try:
            parsed = base / 'output' / 'parsed' / f"{symbol}_required_data.csv"
            df_req0 = safe_read_csv(parsed)
            if df_req0.empty:
                print(f"[WARN] {symbol} required_data empty; skip sell_put scan")
                summary_rows.append(summarize_sell_put(pd.DataFrame(), symbol))
                return summary_rows
        except Exception:
            pass
        # Auto data-quality policy (reduce config complexity):
        # If delta gating is enabled but quotes are mostly empty (Yahoo bid/ask=0),
        # do NOT hard-fail the symbol. Instead, degrade gracefully:
        # - disable require_bid_ask
        # - disable delta gating
        # - keep other filters (OTM/return/spread)
        try:
            parsed = base / 'output' / 'parsed' / f"{symbol}_required_data.csv"
            if parsed.exists() and parsed.stat().st_size > 0:
                df_req = pd.read_csv(parsed)
                df_req = df_req[df_req.get('option_type') == 'put'] if 'option_type' in df_req.columns else df_req
                # Only consider rows within strike range if provided
                if sp.get('min_strike') is not None:
                    df_req = df_req[df_req.get('strike') >= float(sp.get('min_strike'))]
                if sp.get('max_strike') is not None:
                    df_req = df_req[df_req.get('strike') <= float(sp.get('max_strike'))]
                if 'bid' in df_req.columns and 'ask' in df_req.columns and len(df_req) > 0:
                    valid_quotes = ((df_req['bid'] > 0) & (df_req['ask'] > 0)).sum()
                    valid_ratio = float(valid_quotes) / float(len(df_req))
                    # Heuristic: if less than 5% rows have real quotes, treat the data as low quality.
                    if valid_ratio < 0.05:
                        sp = dict(sp)
                        # drop strict quote requirements
                        sp['require_bid_ask'] = False
                        # delta gating becomes meaningless when IV/quotes are unreliable
                        sp.pop('min_abs_delta', None)
                        sp.pop('max_abs_delta', None)
                        # IV gate also becomes suspect; do not block all candidates due to bad IV
                        sp.pop('min_iv', None)
                        sp.pop('max_iv', None)
                        # loosen execution-related gates so we can still surface something (with warnings)
                        sp['min_open_interest'] = 0
                        sp['min_volume'] = 0
        except Exception:
            pass
        cmd = [
            py, 'scripts/scan_sell_put.py',
            '--symbols', symbol,
            '--min-dte', str(sp.get('min_dte', 20)),
            '--max-dte', str(sp.get('max_dte', 90)),
            '--min-otm-pct', str(sp.get('min_otm_pct', 0.0)),
            '--min-annualized-net-return', str(sp.get('min_annualized_net_return', 0.03)),
            # NOTE: config min_net_income is normalized to base CNY.
            # scan_sell_put.py now computes net_income in the option's native currency.
            # We convert the CNY threshold into the option currency using FX when possible.
            '--min-net-income', str(
                # Config threshold is in base CNY; scanners run in option's native currency.
                # USD: CNY->USD using fx_usd_per_cny (USD per 1 CNY)
                # HKD: CNY->HKD using HKDCNY (CNY per 1 HKD)
                (0.0 if float(sp.get('min_net_income') or 0.0) <= 0 else (
                    (float(sp.get('min_net_income') or 0.0) * float(fx_usd_per_cny))
                    if (not str(symbol).upper().endswith('.HK')) and fx_usd_per_cny
                    else (
                        (float(sp.get('min_net_income') or 0.0) / float(hkdcny))
                        if (str(symbol).upper().endswith('.HK') and hkdcny)
                        else 0.0
                    )
                ))
            ),
            '--min-open-interest', str(sp.get('min_open_interest', 100)),
            '--min-volume', str(sp.get('min_volume', 10)),
            '--max-spread-ratio', str(sp.get('max_spread_ratio', 0.30)),
        ]
        # Data quality gates (optional)
        if sp.get('require_bid_ask'):
            cmd.append('--require-bid-ask')
        if sp.get('min_iv') is not None:
            cmd.extend(['--min-iv', str(sp.get('min_iv'))])
        if sp.get('max_iv') is not None:
            cmd.extend(['--max-iv', str(sp.get('max_iv'))])

        # Delta filter (optional): abs(delta) in [min_abs_delta, max_abs_delta]
        if sp.get('min_abs_delta') is not None:
            cmd.extend(['--min-abs-delta', str(sp.get('min_abs_delta'))])
        if sp.get('max_abs_delta') is not None:
            cmd.extend(['--max-abs-delta', str(sp.get('max_abs_delta'))])

        if sp.get('min_strike') is not None:
            cmd.extend(['--min-strike', str(sp.get('min_strike'))])
        if sp.get('max_strike') is not None:
            cmd.extend(['--max-strike', str(sp.get('max_strike'))])
        if IS_SCHEDULED:
            cmd.append('--quiet')
        run(cmd, cwd=base, timeout_sec=timeout_sec)

        shared_sp = report_dir / 'sell_put_candidates.csv'
        symbol_sp = report_dir / f'{symbol_lower}_sell_put_candidates.csv'
        copy_if_exists(shared_sp, symbol_sp)

        shared_sp_labeled = report_dir / 'sell_put_candidates_labeled.csv'
        symbol_sp_labeled = report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv'
        add_sell_put_labels(base, symbol_sp, shared_sp_labeled)
        copy_if_exists(shared_sp_labeled, symbol_sp_labeled)

        # account-aware: attach cash secured usage from option_positions (open short puts)
        df_sp_lab = safe_read_csv(symbol_sp_labeled)
        if not df_sp_lab.empty and portfolio_ctx:
            option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None

            # New schema (preferred): cash_secured_by_symbol_by_ccy / cash_secured_total_by_ccy / cash_secured_total_cny
            # Old schema (legacy): cash_secured_by_symbol (USD-only)
            used_symbol_usd = 0.0
            used_total_usd = 0.0
            used_total_cny = None
            used_symbol_cny = None

            if option_ctx:
                try:
                    by_sym_ccy = option_ctx.get('cash_secured_by_symbol_by_ccy') or {}
                    tot_by_ccy = option_ctx.get('cash_secured_total_by_ccy') or {}
                    if isinstance(by_sym_ccy, dict) and (by_sym_ccy or tot_by_ccy):
                        used_symbol_usd = float(((by_sym_ccy.get(symbol) or {}).get('USD')) or 0.0)
                        used_total_usd = float((tot_by_ccy.get('USD')) or 0.0)
                        # Prefer the context-provided CNY-normalized totals.
                        v = option_ctx.get('cash_secured_total_cny')
                        used_total_cny = float(v) if v is not None else None
                        # Best-effort: symbol-level CNY is not always provided; if absent we'll keep None.
                        vs = None
                        try:
                            vs = (option_ctx.get('cash_secured_by_symbol_cny') or {}).get(symbol)
                        except Exception:
                            vs = None
                        used_symbol_cny = float(vs) if vs is not None else None
                    else:
                        used_map = (option_ctx.get('cash_secured_by_symbol') or {})
                        used_symbol_usd = float(used_map.get(symbol) or 0.0)
                        used_total_usd = float(sum(float(v or 0.0) for v in used_map.values()))
                except Exception:
                    used_symbol_usd = 0.0
                    used_total_usd = 0.0
                    used_total_cny = None
                    used_symbol_cny = None

            cash_avail = None
            cash_avail_est = None  # USD equivalent (from base CNY)
            cash_avail_cny = None
            cash_free_cny = None
            try:
                cash_by_ccy = (portfolio_ctx.get('cash_by_currency') or {}) if isinstance(portfolio_ctx, dict) else {}
                v = cash_by_ccy.get('USD')
                cash_avail = float(v) if v is not None else None

                cny = cash_by_ccy.get('CNY')
                cash_avail_cny = float(cny) if cny is not None else None

                if cash_avail_cny is not None:
                    # Base-currency (CNY) free cash after reserving existing cash-secured puts.
                    # Prefer option_ctx.cash_secured_total_cny (already FX-normalized).
                    if used_total_cny is not None:
                        cash_free_cny = cash_avail_cny - used_total_cny
                    else:
                        # Fallback: treat used_total_usd as USD and convert into CNY
                        if fx_usd_per_cny:
                            usdcny = 1.0 / float(fx_usd_per_cny)
                            cash_free_cny = cash_avail_cny - (used_total_usd * usdcny)
                        else:
                            cash_free_cny = None

                # If no USD cash record in holdings, derive USD equivalent from base CNY using fx.
                if cash_avail is None and cash_avail_cny is not None and fx_usd_per_cny:
                    cash_avail_est = float(cash_avail_cny) * float(fx_usd_per_cny)
            except Exception:
                cash_avail = None
                cash_avail_est = None

            # Account-level cash usage.
            # Keep legacy USD columns, but also provide CNY-normalized view for display.
            df_sp_lab['cash_secured_used_usd_total'] = used_total_usd
            df_sp_lab['cash_secured_used_usd_symbol'] = used_symbol_usd
            df_sp_lab['cash_secured_used_usd'] = used_total_usd

            # CNY-normalized totals (preferred for unified display)
            if used_total_cny is not None:
                df_sp_lab['cash_secured_used_cny_total'] = float(used_total_cny)
            else:
                df_sp_lab['cash_secured_used_cny_total'] = pd.NA
            if used_symbol_cny is not None:
                df_sp_lab['cash_secured_used_cny_symbol'] = float(used_symbol_cny)
            else:
                df_sp_lab['cash_secured_used_cny_symbol'] = pd.NA
            # Backward-compatible alias
            df_sp_lab['cash_secured_used_cny'] = df_sp_lab['cash_secured_used_cny_total']

            # If we have true USD cash from holdings, use it; else use USD equivalent derived from base CNY.
            if cash_avail is not None:
                df_sp_lab['cash_available_usd'] = cash_avail
                df_sp_lab['cash_available_usd_est'] = pd.NA
                df_sp_lab['cash_free_usd'] = cash_avail - used_total_usd
                df_sp_lab['cash_free_usd_est'] = pd.NA
            else:
                df_sp_lab['cash_available_usd'] = pd.NA
                df_sp_lab['cash_free_usd'] = pd.NA
                df_sp_lab['cash_available_usd_est'] = (cash_avail_est if cash_avail_est is not None else pd.NA)
                if cash_avail_est is not None:
                    df_sp_lab['cash_free_usd_est'] = cash_avail_est - used_total_usd
                else:
                    df_sp_lab['cash_free_usd_est'] = pd.NA

            # Base-currency cash (CNY) columns: always prefer this for risk control.
            df_sp_lab['cash_available_cny'] = (cash_avail_cny if cash_avail_cny is not None else pd.NA)
            df_sp_lab['cash_free_cny'] = (cash_free_cny if cash_free_cny is not None else pd.NA)

            # ===== Cash requirement =====
            # Old columns are named *_usd for historical reasons. We keep them for compatibility,
            # but going forward we want everything normalized into CNY for display/risk.
            try:
                # per-contract multiplier (preferred from data source; fallback 100)
                if 'multiplier' in df_sp_lab.columns:
                    m = df_sp_lab['multiplier'].fillna(100.0).astype(float)
                else:
                    m = 100.0

                # native requirement in option currency: strike * multiplier
                native_req = df_sp_lab['strike'].astype(float) * m

                # keep legacy USD column (only meaningful for USD options; for HKD it's just the native amount)
                df_sp_lab['cash_required_usd'] = native_req

                # normalize to CNY using FX
                ccy = None
                if 'currency' in df_sp_lab.columns and len(df_sp_lab) > 0:
                    ccy = str(df_sp_lab['currency'].iloc[0] or '').upper()

                if ccy == 'HKD':
                    # HKDCNY is CNY per 1 HKD
                    try:
                        # Load from shared cache path (same as fx_rates.py uses)
                        import json as _json
                        from pathlib import Path as _Path
                        rate_cache = (base / 'output/state/rate_cache.json').resolve()
                        workspace = _Path(__file__).resolve().parents[2]
                        shared_path = workspace / 'portfolio-management' / '.data' / 'rate_cache.json'
                        # minimal inline: prefer existing cache file
                        rates = None
                        for p in [rate_cache, shared_path]:
                            if p.exists() and p.stat().st_size > 0:
                                d = _json.loads(p.read_text(encoding='utf-8'))
                                rates = (d.get('rates') or {})
                                break
                        hkdcny = float(rates.get('HKDCNY')) if rates and rates.get('HKDCNY') else None
                    except Exception:
                        hkdcny = None
                    if hkdcny:
                        df_sp_lab['cash_required_cny'] = native_req.astype(float) * float(hkdcny)
                    else:
                        df_sp_lab['cash_required_cny'] = pd.NA
                else:
                    # USD -> CNY using USDCNY derived from fx_usd_per_cny
                    if fx_usd_per_cny:
                        usdcny = 1.0 / float(fx_usd_per_cny)
                        df_sp_lab['cash_required_cny'] = native_req.astype(float) * float(usdcny)
                    else:
                        df_sp_lab['cash_required_cny'] = pd.NA
            except Exception:
                df_sp_lab['cash_required_usd'] = pd.NA
                df_sp_lab['cash_required_cny'] = pd.NA

            df_sp_lab.to_csv(symbol_sp_labeled, index=False)

        if not IS_SCHEDULED:
            run([
                py, 'scripts/render_sell_put_alerts.py',
                '--input', f'output/reports/{symbol_lower}_sell_put_candidates_labeled.csv',
                '--symbol', symbol,
                '--top', str(top_n),
                '--layered',
                '--output', f'output/reports/{symbol_lower}_sell_put_alerts.txt',
                ], cwd=base)
        summary_rows.append(summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol))
    else:
        summary_rows.append(summarize_sell_put(pd.DataFrame(), symbol))

    cc = symbol_cfg.get('sell_call', {})
    if cc.get('enabled', False):
        # allow overriding shares/avg_cost from portfolio context (holdings), so alerts become account-aware
        shares_override = None
        avg_cost_override = None
        stock = None
        if portfolio_ctx:
            stock = (portfolio_ctx.get('stocks_by_symbol') or {}).get(symbol)
            if stock:
                shares_override = stock.get('shares')
                avg_cost_override = stock.get('avg_cost')

            # NOTE: do NOT deduct locked shares when passing shares into scan_sell_call.
            # The scan itself is just opportunity scanning; coverage is enforced/annotated after scan.

        # Safety: if there is no holdings row for this symbol in this account, skip sell_call.
        # This avoids recommending covered calls for accounts that do not hold the underlying.
        if not stock:
            summary_rows.append(summarize_sell_call(pd.DataFrame(), symbol))
            return summary_rows

        shares_total = shares_override if shares_override is not None else cc.get('shares', 100)
        avg_cost = avg_cost_override if avg_cost_override is not None else cc['avg_cost']

        cmd = [
            py, 'scripts/scan_sell_call.py',
            '--symbols', symbol,
            '--avg-cost', str(avg_cost),
            '--shares', str(shares_total),
            '--min-dte', str(cc.get('min_dte', 20)),
            '--max-dte', str(cc.get('max_dte', 90)),
            '--min-otm-pct', str(cc.get('min_otm_pct', 0.0)),
            '--min-annualized-net-return', str(cc.get('min_annualized_net_return', 0.03)),
            '--min-if-exercised-total-return', str(cc.get('min_if_exercised_total_return', 0.0)),
            '--min-open-interest', str(cc.get('min_open_interest', 100)),
            '--min-volume', str(cc.get('min_volume', 10)),
            '--max-spread-ratio', str(cc.get('max_spread_ratio', 0.30)),
        ]
        if cc.get('min_strike') is not None:
            cmd.extend(['--min-strike', str(cc.get('min_strike'))])
        if cc.get('max_strike') is not None:
            cmd.extend(['--max-strike', str(cc.get('max_strike'))])
        # Data quality gates (optional)
        if cc.get('require_bid_ask'):
            cmd.append('--require-bid-ask')
        if cc.get('min_iv') is not None:
            cmd.extend(['--min-iv', str(cc.get('min_iv'))])
        if cc.get('max_iv') is not None:
            cmd.extend(['--max-iv', str(cc.get('max_iv'))])

        # Delta filter (optional): call delta in [min_delta, max_delta]
        if cc.get('min_delta') is not None:
            cmd.extend(['--min-delta', str(cc.get('min_delta'))])
        if cc.get('max_delta') is not None:
            cmd.extend(['--max-delta', str(cc.get('max_delta'))])
        run(cmd, cwd=base, timeout_sec=timeout_sec)

        shared_cc = report_dir / 'sell_call_candidates.csv'
        symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
        copy_if_exists(shared_cc, symbol_cc)

        df_cc = safe_read_csv(symbol_cc)
        # enrich candidates with holdings + option-locked shares (account-aware)
        if not df_cc.empty and portfolio_ctx:
            stock = (portfolio_ctx.get('stocks_by_symbol') or {}).get(symbol)
            option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None
            locked = 0
            if option_ctx:
                locked = int((option_ctx.get('locked_shares_by_symbol') or {}).get(symbol) or 0)
            shares_total_v = int((stock or {}).get('shares') or shares_total)
            shares_available = max(shares_total_v - locked, 0)
            covered_contracts_available = shares_available // 100

            df_cc['shares_total'] = shares_total_v
            df_cc['shares_locked'] = locked
            df_cc['shares_available_for_cover'] = shares_available
            df_cc['covered_contracts_available'] = covered_contracts_available
            df_cc['is_fully_covered_available'] = covered_contracts_available >= 1
            df_cc.to_csv(symbol_cc, index=False)

        if not IS_SCHEDULED:
            run([
                py, 'scripts/render_sell_call_alerts.py',
                '--input', f'output/reports/{symbol_lower}_sell_call_candidates.csv',
                '--symbol', symbol,
                '--top', str(top_n),
                '--layered',
                '--output', f'output/reports/{symbol_lower}_sell_call_alerts.txt',
            ], cwd=base)

        summary_rows.append(summarize_sell_call(df_cc, symbol))
    else:
        summary_rows.append(summarize_sell_call(pd.DataFrame(), symbol))

    return summary_rows


def build_symbols_summary(base: Path, summary_rows: list[dict]):
    report_dir = base / 'output' / 'reports'
    df = pd.DataFrame(summary_rows)
    csv_path = report_dir / 'symbols_summary.csv'
    txt_path = report_dir / 'symbols_summary.txt'
    df.to_csv(csv_path, index=False)

    lines = ['# Symbols Summary', '']
    if df.empty:
        lines.append('无结果。')
    else:
        ordered = df.copy()
        ordered['annualized_return_sort'] = ordered['annualized_return'].fillna(-1)
        ordered = ordered.sort_values(['symbol', 'strategy'])
        for _, r in ordered.iterrows():
            annual = '-' if pd.isna(r['annualized_return']) else f"{float(r['annualized_return'])*100:.2f}%"
            income = '-' if pd.isna(r['net_income']) else f"{float(r['net_income']):,.2f}"
            strike = '-' if pd.isna(r['strike']) else (str(int(r['strike'])) if float(r['strike']).is_integer() else f"{float(r['strike']):.2f}")
            dte = '-' if pd.isna(r['dte']) else str(int(r['dte']))
            lines.append(
                f"- {r['symbol']} | {r['strategy']} | 候选 {int(r['candidate_count'])} | "
                f"Top {r['top_contract'] or '-'} | 年化 {annual} | 净收入 {income} | "
                f"DTE {dte} | Strike {strike} | {r['risk_label'] or '-'} | {r['note']}"
            )
    if not IS_SCHEDULED:
        txt_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f"[DONE] symbols summary text -> {txt_path}")
    print(f"[DONE] symbols summary -> {csv_path}")



def build_symbols_digest(base: Path, symbols: list[str]):
    report_dir = base / 'output' / 'reports'
    lines = ['# Symbols Strategy Digest', '']

    for symbol in symbols:
        lines.append(f'## {symbol}')
        sp_path = report_dir / f'{symbol.lower()}_sell_put_alerts.txt'
        cc_path = report_dir / f'{symbol.lower()}_sell_call_alerts.txt'

        lines.append('### Sell Put')
        if sp_path.exists() and sp_path.stat().st_size > 0:
            lines.append(sp_path.read_text(encoding='utf-8').strip())
        else:
            lines.append('无候选。')
        lines.append('')

        lines.append('### Sell Call')
        if cc_path.exists() and cc_path.stat().st_size > 0:
            lines.append(cc_path.read_text(encoding='utf-8').strip())
        else:
            lines.append('无候选。')
        lines.append('')

    out_path = report_dir / 'symbols_digest.txt'
    out_path.write_text('\n'.join(lines), encoding='utf-8')


    print(f'[DONE] symbols digest -> {out_path}')


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge two dicts. override wins."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_profiles(item: dict, profiles: dict | None) -> dict:
    if not isinstance(item, dict):
        return item
    if not profiles or not isinstance(profiles, dict):
        return item

    use = item.get('use')
    if not use:
        return item

    use_list = []
    if isinstance(use, str):
        use_list = [use]
    elif isinstance(use, list):
        use_list = [x for x in use if isinstance(x, str)]

    merged: dict = {}
    for name in use_list:
        p = profiles.get(name)
        if isinstance(p, dict):
            merged = _deep_merge(merged, p)

    # Item overrides profile defaults
    item2 = dict(item)
    item2.pop('use', None)
    merged = _deep_merge(merged, item2)
    return merged


# Runtime mode flags (set in main())
RUNTIME_MODE = 'dev'
IS_SCHEDULED = False


def main():
    global RUNTIME_MODE, IS_SCHEDULED

    parser = argparse.ArgumentParser(description='Run options-monitor pipeline')
    parser.add_argument('--config', required=True, help='Path to JSON config (single-symbol or watchlist). YAML is legacy.')
    parser.add_argument('--mode', default='dev', choices=['dev', 'scheduled'], help='Runtime mode: dev (verbose) vs scheduled (fast)')
    parser.add_argument('--symbols', default=None, help='Comma-separated symbol whitelist; only process these symbols')
    parser.add_argument('--stage', default='all', choices=['fetch','scan','alert','notify','all'], help='Pipeline stage: fetch|scan|alert|notify|all (dev speed; runs up to this stage)')
    parser.add_argument('--stage-only', default=None, choices=['alert','notify'], help='Run ONLY a late stage (no fetch/scan). Requires existing output files.')
    args = parser.parse_args()

    RUNTIME_MODE = str(args.mode)
    IS_SCHEDULED = (RUNTIME_MODE == 'scheduled')
    STAGE = str(args.stage)
    STAGE_ONLY = (str(args.stage_only) if args.stage_only else None)

    def want(name: str) -> bool:
        # stage-only mode: run ONLY the requested late stage
        if STAGE_ONLY is not None:
            return name == STAGE_ONLY
        # normal mode: run up to STAGE
        if STAGE == 'all':
            return True
        order = ['fetch', 'scan', 'alert', 'notify']
        try:
            return order.index(name) <= order.index(STAGE)
        except Exception:
            return True

    def stage_only_changes_out() -> str:
        # In dev iteration, stage-only should not mutate snapshot/change history.
        # (Otherwise, formatting tests would pollute symbols_summary_prev.csv / changes.)
        return '/dev/null'


    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    # config supports YAML or JSON
    if cfg_path.suffix.lower() == '.json':
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    else:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

    # Validate config early (fail fast)
    # - dev mode: always validate
    # - scheduled mode: validate only when config content changes (hash cache)
    try:
        from scripts.validate_config import validate_config as _validate_config

        should_validate = True
        if IS_SCHEDULED:
            try:
                import hashlib
                import json as _json
                state_dir = (base / 'output' / 'state').resolve()
                state_dir.mkdir(parents=True, exist_ok=True)
                cache_path = state_dir / 'config_validation_cache.json'
                payload = _json.dumps(cfg, ensure_ascii=False, sort_keys=True)
                h = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                prev = None
                if cache_path.exists() and cache_path.stat().st_size > 0:
                    prev = _json.loads(cache_path.read_text(encoding='utf-8')).get('sha256')
                if prev == h:
                    should_validate = False
                else:
                    cache_path.write_text(_json.dumps({'sha256': h}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            except Exception:
                should_validate = True

        if should_validate:
            _validate_config(cfg)
    except SystemExit:
        raise
    except Exception:
        # don't block if validator import fails
        pass

    py = sys.executable

    # naming aliases (prefer more intuitive names):
    # - templates == profiles (legacy internal name)
    # - symbols == watchlist (legacy internal name)
    if 'templates' in cfg and 'profiles' not in cfg:
        cfg['profiles'] = cfg.get('templates')
    if 'symbols' in cfg and 'watchlist' not in cfg:
        cfg['watchlist'] = cfg.get('symbols')

    if 'watchlist' in cfg:
        # Optional symbols whitelist (comma-separated)
        sym_whitelist = None
        if args.symbols:
            sym_whitelist = {s.strip() for s in str(args.symbols).split(',') if s.strip()}

        top_n = cfg.get('outputs', {}).get('top_n_alerts', 3)
        runtime = cfg.get('runtime', {}) or {}
        symbol_timeout_sec = int(runtime.get('symbol_timeout_sec', 120))
        portfolio_timeout_sec = int(runtime.get('portfolio_timeout_sec', 60))

        # stage-only late-stage runner: skip fetch/scan and re-use existing outputs.
        # Typical usage:
        #   --stage-only alert  (requires output/reports/symbols_summary.csv)
        #   --stage-only notify (requires output/reports/symbols_alerts.txt)
        if STAGE_ONLY is not None:
            summary_path = base / 'output' / 'reports' / 'symbols_summary.csv'
            alerts_path = base / 'output' / 'reports' / 'symbols_alerts.txt'
            if STAGE_ONLY == 'alert':
                if not (summary_path.exists() and summary_path.stat().st_size > 0):
                    raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {summary_path}")
            if STAGE_ONLY == 'notify':
                if not (alerts_path.exists() and alerts_path.stat().st_size > 0):
                    raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {alerts_path}")

            changes_out = stage_only_changes_out() if STAGE_ONLY else ('/dev/null' if IS_SCHEDULED else 'output/reports/symbols_changes.txt')
            alert_cmd = [
                py, 'scripts/alert_engine.py',
                '--summary-input', 'output/reports/symbols_summary.csv',
                '--output', 'output/reports/symbols_alerts.txt',
                '--changes-output', changes_out,
            ]
            # stage-only: do NOT update snapshot/history
            if (not IS_SCHEDULED) and (not STAGE_ONLY):
                alert_cmd.extend([
                    '--previous-summary', 'output/state/symbols_summary_prev.csv',
                    '--update-snapshot',
                ])
            if want('alert'):
                run(alert_cmd, cwd=base)

            if want('notify'):
                run([
                    py, 'scripts/notify_symbols.py',
                    '--alerts-input', 'output/reports/symbols_alerts.txt',
                    '--changes-input', (changes_out if STAGE_ONLY else ('/dev/null' if IS_SCHEDULED else 'output/reports/symbols_changes.txt')),
                    '--output', 'output/reports/symbols_notification.txt',
                ], cwd=base)

            print(f"[INFO] stage-only done: {STAGE_ONLY}")
            return

        symbols = []
        summary_rows: list[dict] = []

        if not want('scan'):
            portfolio_ctx = None
            option_ctx = None
            fx_usd_per_cny = None
            hkdcny = None
        else:
            # portfolio context
            portfolio_cfg = cfg.get('portfolio', {}) or {}
            pm_config = portfolio_cfg.get('pm_config', '../portfolio-management/config.json')
            market = portfolio_cfg.get('market', '富途')
            account = portfolio_cfg.get('account')

            portfolio_ctx = None
            option_ctx = None

            # Cache policy (TTL seconds)
            # - scheduled: longer TTL (reduce PM subprocess overhead)
            # - dev: shorter TTL (keep reasonably fresh)
            ttl_opt_ctx = int(runtime.get('option_positions_context_ttl_sec', 900 if IS_SCHEDULED else 120) or 0)
            ttl_port_ctx = int(runtime.get('portfolio_context_ttl_sec', 900 if IS_SCHEDULED else 60) or 0)

            # 1) portfolio_context cache
            try:
                port_path = (base / 'output/state/portfolio_context.json').resolve()
                cached = None
                if ttl_port_ctx > 0 and is_fresh(port_path, ttl_port_ctx):
                    cached = load_cached_json(port_path)
                if cached is not None:
                    portfolio_ctx = cached
                else:
                    cmd = [
                        py, 'scripts/fetch_portfolio_context.py',
                        '--pm-config', str(pm_config),
                        '--market', str(market),
                        '--out', 'output/state/portfolio_context.json',
                    ]
                    if account:
                        cmd.extend(['--account', str(account)])
                    run(cmd, cwd=base, timeout_sec=portfolio_timeout_sec)
                    portfolio_ctx = load_cached_json(port_path) or json.loads(port_path.read_text(encoding='utf-8'))
            except BaseException as e:
                # Important: run() raises SystemExit on non-zero return codes.
                # For unattended cron, portfolio context is best-effort and should not kill the whole scan.
                print(f"[WARN] portfolio context not available: {e}")
                portfolio_ctx = None

            # 2) option_positions_context cache (and auto-close only on refresh)
            try:
                opt_path = (base / 'output/state/option_positions_context.json').resolve()
                refreshed = False
                cached = None
                if ttl_opt_ctx > 0 and is_fresh(opt_path, ttl_opt_ctx):
                    cached = load_cached_json(opt_path)
                if cached is not None:
                    option_ctx = cached
                else:
                    cmd = [
                        py, 'scripts/fetch_option_positions_context.py',
                        '--pm-config', str(pm_config),
                        '--market', str(market),
                        '--out', 'output/state/option_positions_context.json',
                    ]
                    if account:
                        cmd.extend(['--account', str(account)])
                    run(cmd, cwd=base, timeout_sec=portfolio_timeout_sec)
                    option_ctx = load_cached_json(opt_path) or json.loads(opt_path.read_text(encoding='utf-8'))
                    refreshed = True

                if refreshed:
                    # Auto-close expired open positions (table maintenance) without extra scans.
                    # Only run when we refreshed context (avoid repeated close calls during rapid dev loops).
                    try:
                        run([
                            py, 'scripts/auto_close_expired_positions.py',
                            '--pm-config', str(pm_config),
                            '--context', 'output/state/option_positions_context.json',
                            '--grace-days', '1',
                            '--max-close', '20',
                            '--summary-out', 'output/reports/auto_close_summary.txt',
                        ], cwd=base, timeout_sec=portfolio_timeout_sec)
                    except Exception as e2:
                        print(f"[WARN] auto-close expired positions failed: {e2}")

            except BaseException as e:
                # best-effort; do not kill pipeline if this fails
                print(f"[WARN] option positions context not available: {e}")
                option_ctx = None

            # FX (once per pipeline).
            fx_usd_per_cny = None
            hkdcny = None
            try:
                # scripts/ is not a package; load fx_rates.py by path
                import importlib.util
                fx_path = (base / 'scripts' / 'fx_rates.py').resolve()
                import sys as _sys
                spec = importlib.util.spec_from_file_location('fx_rates', fx_path)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                # dataclasses expects module to exist in sys.modules during exec
                _sys.modules['fx_rates'] = mod
                spec.loader.exec_module(mod)  # type: ignore
                fx_usd_per_cny = mod.get_usd_per_cny(base)  # type: ignore
                # also load HKDCNY (CNY per 1 HKD) from cache
                try:
                    rates = mod.get_rates((base / 'output/state/rate_cache.json').resolve(), None)  # type: ignore
                    hkdcny = float(rates.get('HKDCNY')) if rates and rates.get('HKDCNY') else None
                except Exception:
                    hkdcny = None
            except BaseException as e:
                # best-effort
                print(f"[WARN] fx rates not available: {e}")

        profiles = cfg.get('profiles') or {}

        for item in cfg['watchlist']:
            try:
                # Optional whitelist filter
                if sym_whitelist is not None:
                    s0 = str((item or {}).get('symbol') or '').strip()
                    if s0 and s0 not in sym_whitelist:
                        continue

                item = apply_profiles(item, profiles)
                # inject option_ctx into portfolio_ctx for now (minimal change):
                if portfolio_ctx is not None and option_ctx is not None:
                    portfolio_ctx['option_ctx'] = option_ctx
                if not want('scan'):
                    # fetch-only: just pull required_data and stop
                    item_fetch = dict(item)
                    item_fetch['sell_put'] = {'enabled': False}
                    item_fetch['sell_call'] = {'enabled': False}
                    process_symbol(py, base, item_fetch, top_n, portfolio_ctx=None, fx_usd_per_cny=None, hkdcny=None, timeout_sec=symbol_timeout_sec)
                else:
                    summary_rows.extend(process_symbol(py, base, item, top_n, portfolio_ctx=portfolio_ctx, fx_usd_per_cny=fx_usd_per_cny, hkdcny=hkdcny, timeout_sec=symbol_timeout_sec))
            except Exception as e:
                symbol = item.get('symbol', 'UNKNOWN')
                print(f'[WARN] {symbol} processing failed: {e}')
                summary_rows.append({
                    'symbol': symbol,
                    'strategy': 'sell_put',
                    'candidate_count': 0,
                    'top_contract': '',
                    'expiration': '',
                    'strike': None,
                    'dte': None,
                    'net_income': None,
                    'annualized_return': None,
                    'risk_label': '',
                    'note': f'处理失败: {e}',
                })
                summary_rows.append({
                    'symbol': symbol,
                    'strategy': 'sell_call',
                    'candidate_count': 0,
                    'top_contract': '',
                    'expiration': '',
                    'strike': None,
                    'dte': None,
                    'net_income': None,
                    'annualized_return': None,
                    'risk_label': '',
                    'note': f'处理失败: {e}',
                })
            symbols.append(item['symbol'])

        # fetch-only stage: stop after market-data fetch
        # (but do not interfere with stage-only late-stage runs)
        if (STAGE_ONLY is None) and (not want('scan')):
            print(f"[INFO] stage={STAGE}: fetch done")
            return

        build_symbols_summary(base, summary_rows)
        if not IS_SCHEDULED:
            build_symbols_digest(base, symbols)
        changes_out = ('/dev/null' if IS_SCHEDULED else 'output/reports/symbols_changes.txt')
        alert_cmd = [
            py, 'scripts/alert_engine.py',
            '--summary-input', 'output/reports/symbols_summary.csv',
            '--output', 'output/reports/symbols_alerts.txt',
            '--changes-output', changes_out,
        ]
        if not IS_SCHEDULED:
            alert_cmd.extend([
                '--previous-summary', 'output/state/symbols_summary_prev.csv',
                '--update-snapshot',
            ])
        # alert policy overrides (optional)
        try:
            policy = cfg.get('alert_policy')
            if isinstance(policy, dict) and policy:
                p = base / 'output' / 'state' / 'alert_policy.json'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding='utf-8')
                alert_cmd.extend(['--policy-json', str(p)])
            elif isinstance(policy, str) and policy.strip():
                alert_cmd.extend(['--policy-json', policy.strip()])
        except Exception:
            pass
        if want('alert'):
            run(alert_cmd, cwd=base)

        if want('notify'):
            run([
                py, 'scripts/notify_symbols.py',
                '--alerts-input', 'output/reports/symbols_alerts.txt',
                '--changes-input', ('/dev/null' if IS_SCHEDULED else 'output/reports/symbols_changes.txt'),
                '--output', 'output/reports/symbols_notification.txt',
            ], cwd=base)

        # Append cash summaries at the bottom (optional).
        # In multi-account merged notifications, we prefer adding cash footer only once in send_if_needed_multi.py.
        include_cash_footer = True
        try:
            include_cash_footer = bool((cfg.get('notifications') or {}).get('include_cash_footer', True))
        except Exception:
            include_cash_footer = True

        if include_cash_footer and (not IS_SCHEDULED):
            run([
                py, 'scripts/append_cash_summary.py',
                '--pm-config', str(pm_config),
                '--market', str(market),
                '--accounts', 'lx', 'sy',
                '--notification', 'output/reports/symbols_notification.txt',
            ], cwd=base)

        notifications_cfg = cfg.get('notifications', {}) or {}
        if notifications_cfg.get('enabled', False):
            print('[INFO] notifications enabled in config; pipeline prepared notification text for sending.')
        else:
            print('[INFO] notifications disabled; generated notification text only.')
        if not IS_SCHEDULED:
            print('\n[DONE] Symbols pipeline finished')
            print('- output/reports/symbols_summary.csv')
            print('- output/reports/symbols_summary.txt')
            print('- output/reports/symbols_digest.txt')
            print('- output/reports/symbols_alerts.txt')
            print('- output/reports/symbols_changes.txt')
            print('- output/reports/symbols_notification.txt')
            print('')

        return

    top_n = cfg.get('outputs', {}).get('top_n_alerts', 3)
    process_symbol(py, base, cfg, top_n)
    print('\n[DONE] Single-symbol pipeline finished')
    print('- output/reports/{symbol}_sell_put_candidates*.csv / alerts.txt')
    print('- output/reports/{symbol}_sell_call_candidates.csv / alerts.txt')


if __name__ == '__main__':
    main()
