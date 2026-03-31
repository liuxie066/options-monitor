"""Per-symbol pipeline orchestration.

Stage 3 refactor target: keep run_pipeline as a thin top-level CLI orchestrator.

This module intentionally contains the (large) process_symbol() function extracted
from run_pipeline.py with minimal/no behavioral changes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.fx_rates import CurrencyConverter, FxRates
from scripts.io_utils import safe_read_csv
from scripts.pipeline_steps import derive_put_max_strike_from_cash
from scripts.report_labels import add_sell_put_labels
from scripts.report_summaries import summarize_sell_call, summarize_sell_put
from scripts.subprocess_utils import run_cmd


def process_symbol(
    py: str,
    base: Path,
    symbol_cfg: dict,
    top_n: int,
    portfolio_ctx: dict | None = None,
    fx_usd_per_cny: float | None = None,
    hkdcny: float | None = None,
    timeout_sec: int | None = 120,
    *,
    required_data_dir: Path | None = None,
    report_dir: Path | None = None,
) -> list[dict]:
    """Run fetch/scan/render per symbol.

    NOTE: Extracted from scripts/run_pipeline.py. Keep changes minimal.
    """
    symbol = symbol_cfg['symbol']
    symbol_lower = symbol.lower()
    limit_expirations = symbol_cfg.get('fetch', {}).get('limit_expirations', 8)

    # Directories:
    # - required_data_dir: where {raw,parsed} required_data lives (new flow: run_dir/required_data)
    # - report_dir: where per-run reports are written
    if report_dir is None:
        report_dir = base / 'output' / 'reports'
    if required_data_dir is None:
        # legacy
        required_data_dir = base / 'output'

    summary_rows: list[dict] = []

    sp = symbol_cfg.get('sell_put', {}) or {}
    cc = symbol_cfg.get('sell_call', {}) or {}
    want_put = bool(sp.get('enabled', False))
    want_call = bool(cc.get('enabled', False))

    _FX = CurrencyConverter(FxRates(usd_per_cny=fx_usd_per_cny, cny_per_hkd=hkdcny))

    # NOTE: in scheduled mode, suppress verbose printing.
    # We don't have IS_SCHEDULED here; caller passes it through run_cmd.
    # (run_cmd already has is_scheduled argument at call-sites below.)
    IS_SCHEDULED = False

    # Pre-filter (call): if this account has no holdings row for the symbol, skip sell_call fully.
    stock = None
    if want_call and portfolio_ctx:
        try:
            stock = (portfolio_ctx.get('stocks_by_symbol') or {}).get(symbol)
        except Exception:
            stock = None
        if not stock:
            want_call = False

    # Pre-filter (put): derive a cash-based max_strike cap to reduce chain size.
    if want_put:
        try:
            cash_cap = derive_put_max_strike_from_cash(symbol, portfolio_ctx, fx_usd_per_cny, hkdcny)
            if cash_cap and cash_cap > 0:
                if sp.get('max_strike') is None or float(sp.get('max_strike')) > float(cash_cap):
                    sp = dict(sp)
                    sp['max_strike'] = float(cash_cap)
            if sp.get('max_strike') is not None and float(sp.get('max_strike')) <= 0:
                want_put = False
        except Exception:
            pass

    # Multiplier static cache (best-effort): fill missing/invalid multiplier in required_data.csv
    def _apply_multiplier_cache_to_required_data_csv(symbol: str) -> None:
        try:
            from scripts import multiplier_cache

            cache_path = multiplier_cache.default_cache_path(base)
            cache = multiplier_cache.load_cache(cache_path)
            m = multiplier_cache.get_cached_multiplier(cache, symbol)
            if not m:
                return

            parsed = (required_data_dir / 'parsed' / f"{symbol}_required_data.csv").resolve()
            if not parsed.exists() or parsed.stat().st_size <= 0:
                return

            df = safe_read_csv(parsed)
            if df.empty:
                return

            if 'multiplier' not in df.columns:
                df['multiplier'] = float(m)
            else:
                try:
                    mm = pd.to_numeric(df['multiplier'], errors='coerce')
                    bad = mm.isna() | (mm <= 0)
                    if bad.any():
                        df.loc[bad, 'multiplier'] = float(m)
                except Exception:
                    df['multiplier'] = float(m)

            df.to_csv(parsed, index=False)
        except Exception:
            pass

    # Fill multiplier where possible (best-effort)
    try:
        _apply_multiplier_cache_to_required_data_csv(symbol)
    except Exception:
        pass

    # ---------- Fetch required_data ----------
    symbol_lower = symbol.lower()
    sym = symbol

    raw = (required_data_dir / 'raw' / f"{sym}_required_data.json").resolve()
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if want_put or want_call:
        # Always fetch before scan if required_data missing.
        if not raw.exists() or raw.stat().st_size <= 0 or not parsed.exists() or parsed.stat().st_size <= 0:
            cmd = [
                py, 'scripts/fetch_required_data.py',
                '--symbols', sym,
                '--output-root', str(required_data_dir),
                '--limit-expirations', str(limit_expirations),
            ]
            if IS_SCHEDULED:
                cmd.append('--quiet')
            run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=IS_SCHEDULED)

    # ---------- Scan sell_put ----------
    if want_put:
        symbol_sp = (report_dir / f'{symbol_lower}_sell_put_candidates.csv').resolve()
        symbol_sp_labeled = (report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').resolve()

        cmd = [
            py, 'scripts/scan_sell_put.py',
            '--symbols', sym,
            '--input-root', str(required_data_dir),
            '--min-dte', str(sp.get('min_dte', 20)),
            '--max-dte', str(sp.get('max_dte', 60)),
            '--min-annualized-return', str(sp.get('min_annualized_net_return', 0.07)),
            '--min-open-interest', str(sp.get('min_open_interest', 100)),
            '--min-volume', str(sp.get('min_volume', 10)),
            '--out', str(symbol_sp),
            '--top', str(top_n),
        ]
        if sp.get('min_strike') is not None:
            cmd.extend(['--min-strike', str(sp.get('min_strike'))])
        if sp.get('max_strike') is not None:
            cmd.extend(['--max-strike', str(sp.get('max_strike'))])

        # CNY threshold -> option native (USD/HKD)
        cmd.extend([
            '--min-net-income', str(
                (lambda cny_threshold: (
                    0.0 if cny_threshold <= 0 else (
                        (
                            _FX.cny_to_native(
                                cny_threshold,
                                native_ccy=('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
                            )
                        )
                        or 0.0
                    )
                ))(float(sp.get('min_net_income') or 0.0))
            ),
        ])

        if IS_SCHEDULED:
            cmd.append('--quiet')

        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=IS_SCHEDULED)

        add_sell_put_labels(base, symbol_sp, symbol_sp_labeled)

        # account-aware: attach cash secured usage from option_positions (open short puts)
        df_sp_lab = safe_read_csv(symbol_sp_labeled)
        if not df_sp_lab.empty and portfolio_ctx:
            option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None

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
                        v = option_ctx.get('cash_secured_total_cny')
                        used_total_cny = float(v) if v is not None else None
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
            cash_avail_est = None
            cash_avail_cny = None
            cash_free_cny = None
            try:
                cash_by_ccy = (portfolio_ctx.get('cash_by_currency') or {}) if isinstance(portfolio_ctx, dict) else {}
                v = cash_by_ccy.get('USD')
                cash_avail = float(v) if v is not None else None

                cny = cash_by_ccy.get('CNY')
                cash_avail_cny = float(cny) if cny is not None else None

                if cash_avail_cny is not None:
                    if used_total_cny is not None:
                        cash_free_cny = cash_avail_cny - used_total_cny
                    else:
                        k = _FX.native_to_cny(1.0, native_ccy='USD')
                        cash_free_cny = (cash_avail_cny - (used_total_usd * float(k))) if k else None

                if cash_avail is None and cash_avail_cny is not None:
                    cash_avail_est = _FX.cny_to_native(cash_avail_cny, native_ccy='USD')
            except Exception:
                cash_avail = None
                cash_avail_est = None

            df_sp_lab['cash_secured_used_usd_total'] = used_total_usd
            df_sp_lab['cash_secured_used_usd_symbol'] = used_symbol_usd
            df_sp_lab['cash_secured_used_usd'] = used_total_usd

            if used_total_cny is not None:
                df_sp_lab['cash_secured_used_cny_total'] = float(used_total_cny)
            else:
                df_sp_lab['cash_secured_used_cny_total'] = pd.NA
            if used_symbol_cny is not None:
                df_sp_lab['cash_secured_used_cny_symbol'] = float(used_symbol_cny)
            else:
                df_sp_lab['cash_secured_used_cny_symbol'] = pd.NA
            df_sp_lab['cash_secured_used_cny'] = df_sp_lab['cash_secured_used_cny_total']

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

            df_sp_lab['cash_available_cny'] = (cash_avail_cny if cash_avail_cny is not None else pd.NA)
            df_sp_lab['cash_free_cny'] = (cash_free_cny if cash_free_cny is not None else pd.NA)

            # Cash requirement
            try:
                if 'multiplier' in df_sp_lab.columns:
                    m = df_sp_lab['multiplier'].fillna(100.0).astype(float)
                else:
                    m = 100.0

                native_req = df_sp_lab['strike'].astype(float) * m
                df_sp_lab['cash_required_usd'] = native_req

                ccy = None
                if 'currency' in df_sp_lab.columns and len(df_sp_lab) > 0:
                    ccy = str(df_sp_lab['currency'].iloc[0] or '').upper()

                c = (ccy or 'USD')
                k = _FX.native_to_cny(1.0, native_ccy=c)
                if k is None or k <= 0:
                    df_sp_lab['cash_required_cny'] = pd.NA
                else:
                    df_sp_lab['cash_required_cny'] = native_req.astype(float) * float(k)
            except Exception:
                df_sp_lab['cash_required_usd'] = pd.NA
                df_sp_lab['cash_required_cny'] = pd.NA

            df_sp_lab.to_csv(symbol_sp_labeled, index=False)

        if not IS_SCHEDULED:
            run_cmd([
                py, 'scripts/render_sell_put_alerts.py',
                '--input', str((report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').as_posix()),
                '--symbol', symbol,
                '--top', str(top_n),
                '--layered',
                '--output', str((report_dir / f'{symbol_lower}_sell_put_alerts.txt').as_posix()),
            ], cwd=base, is_scheduled=IS_SCHEDULED)

        summary_rows.append(summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg))
    else:
        summary_rows.append(summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg))

    # ---------- Scan sell_call ----------
    if want_call:
        shares_override = None
        avg_cost_override = None
        if stock:
            shares_override = stock.get('shares')
            avg_cost_override = stock.get('avg_cost')

        shares_total = shares_override if shares_override is not None else cc.get('shares', 100)
        avg_cost = avg_cost_override if avg_cost_override is not None else cc['avg_cost']

        symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
        cmd = [
            py, 'scripts/scan_sell_call.py',
            '--symbols', symbol,
            '--input-root', str(required_data_dir),
            '--avg-cost', str(avg_cost),
            '--shares', str(shares_total),
            '--min-dte', str(cc.get('min_dte', 20)),
            '--max-dte', str(cc.get('max_dte', 90)),
            '--min-annualized-premium-return', str(cc.get('min_annualized_net_premium_return', 0.07)),
            '--min-open-interest', str(cc.get('min_open_interest', 100)),
            '--min-volume', str(cc.get('min_volume', 10)),
            '--out', str(symbol_cc),
            '--top', str(top_n),
        ]
        if cc.get('min_strike') is not None:
            cmd.extend(['--min-strike', str(cc.get('min_strike'))])
        if cc.get('max_strike') is not None:
            cmd.extend(['--max-strike', str(cc.get('max_strike'))])

        if IS_SCHEDULED:
            cmd.append('--quiet')
        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=IS_SCHEDULED)

        df_cc = safe_read_csv(symbol_cc)
        if not IS_SCHEDULED:
            run_cmd([
                py, 'scripts/render_sell_call_alerts.py',
                '--input', str((report_dir / f'{symbol_lower}_sell_call_candidates.csv').as_posix()),
                '--symbol', symbol,
                '--top', str(top_n),
                '--layered',
                '--output', str((report_dir / f'{symbol_lower}_sell_call_alerts.txt').as_posix()),
            ], cwd=base, is_scheduled=IS_SCHEDULED)

        summary_rows.append(summarize_sell_call(df_cc, symbol, symbol_cfg=symbol_cfg))
    else:
        summary_rows.append(summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg))

    return summary_rows
