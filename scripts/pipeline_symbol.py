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
from scripts.report_summaries import summarize_sell_call, summarize_sell_put
from scripts.multiplier_steps import apply_multiplier_cache_to_required_data_csv
from scripts.required_data_steps import ensure_required_data
from scripts.sell_call_steps import empty_sell_call_summary, run_sell_call_scan_and_summarize
from scripts.sell_put_steps import empty_sell_put_summary, run_sell_put_scan_and_summarize
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
    is_scheduled: bool = False,
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
    IS_SCHEDULED = bool(is_scheduled)

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
    try:
        apply_multiplier_cache_to_required_data_csv(base=base, required_data_dir=required_data_dir, symbol=symbol)
    except Exception:
        pass

    # ---------- Fetch required_data ----------
    ensure_required_data(
        py=py,
        base=base,
        symbol=symbol,
        required_data_dir=required_data_dir,
        limit_expirations=limit_expirations,
        want_put=want_put,
        want_call=want_call,
        timeout_sec=timeout_sec,
        is_scheduled=IS_SCHEDULED,
    )

    # ---------- Scan sell_put ----------
    if want_put:
        summary_rows.append(
            run_sell_put_scan_and_summarize(
                py=py,
                base=base,
                sym=sym,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                sp=sp,
                top_n=top_n,
                required_data_dir=required_data_dir,
                report_dir=report_dir,
                timeout_sec=timeout_sec,
                is_scheduled=IS_SCHEDULED,
                fx=_FX,
                portfolio_ctx=portfolio_ctx,
            )
        )
    else:
        summary_rows.append(empty_sell_put_summary(symbol, symbol_cfg=symbol_cfg))

    # ---------- Scan sell_call ----------
    if want_call:
        summary_rows.append(
            run_sell_call_scan_and_summarize(
                py=py,
                base=base,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                cc=cc,
                top_n=top_n,
                required_data_dir=required_data_dir,
                report_dir=report_dir,
                timeout_sec=timeout_sec,
                is_scheduled=IS_SCHEDULED,
                stock=stock,
            )
        )
    else:
        summary_rows.append(empty_sell_call_summary(symbol, symbol_cfg=symbol_cfg))

    return summary_rows
