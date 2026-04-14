"""Per-symbol pipeline orchestration.

Stage 3 refactor target: keep run_pipeline as a thin top-level CLI orchestrator.

This module intentionally contains the (large) process_symbol() function extracted
from run_pipeline.py with minimal/no behavioral changes.
"""

from __future__ import annotations

from pathlib import Path


from scripts.fx_loader import build_converter
from scripts.prefilters import apply_prefilters
from scripts.multiplier_steps import apply_multiplier_cache_to_required_data_csv
from scripts.required_data_steps import ensure_required_data
from scripts.sell_call_steps import empty_sell_call_summary, run_sell_call_scan_and_summarize
from scripts.sell_put_steps import empty_sell_put_summary, run_sell_put_scan_and_summarize


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
    state_dir: Path | None = None,
    is_scheduled: bool = False,
) -> list[dict]:
    """Run fetch/scan/render per symbol.

    NOTE: Extracted from scripts/run_pipeline.py. Keep changes minimal.
    """
    symbol = symbol_cfg['symbol']
    sym = symbol  # used by scanners' --symbols arg
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

    _FX = build_converter(fx_usd_per_cny=fx_usd_per_cny, hkdcny=hkdcny)

    # NOTE: in scheduled mode, suppress verbose printing.
    # We don't have IS_SCHEDULED here; caller passes it through run_cmd.
    # (run_cmd already has is_scheduled argument at call-sites below.)
    IS_SCHEDULED = bool(is_scheduled)

    # Prefilters
    pf = apply_prefilters(
        symbol=symbol,
        sp=sp,
        cc=cc,
        want_put=want_put,
        want_call=want_call,
        portfolio_ctx=portfolio_ctx,
        fx_usd_per_cny=fx_usd_per_cny,
        hkdcny=hkdcny,
    )
    want_put = pf.want_put
    want_call = pf.want_call
    sp = pf.sp
    cc = pf.cc
    stock = pf.stock

    # Multiplier static cache (best-effort): fill missing/invalid multiplier in required_data.csv
    try:
        apply_multiplier_cache_to_required_data_csv(base=base, required_data_dir=required_data_dir, symbol=symbol)
    except Exception:
        pass

    # ---------- Fetch required_data ----------
    fetch_cfg = symbol_cfg.get('fetch', {}) or {}
    min_dte = None
    max_dte = None
    try:
        min_dte = int(max(
            float(sp.get('min_dte') or 0) if want_put else 0,
            float(cc.get('min_dte') or 0) if want_call else 0,
            0,
        ))
    except Exception:
        min_dte = None
    try:
        max_dte = int(max(
            float(sp.get('max_dte') or 0) if want_put else 0,
            float(cc.get('max_dte') or 0) if want_call else 0,
            0,
        ))
        if max_dte <= 0:
            max_dte = None
    except Exception:
        max_dte = None

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
        state_dir=state_dir,
        fetch_source=str(fetch_cfg.get('source') or 'yahoo'),
        fetch_host=str(fetch_cfg.get('host') or '127.0.0.1'),
        fetch_port=int(fetch_cfg.get('port') or 11111),
        spot_from_pm=(fetch_cfg.get('spot_from_portfolio_management', None)),
        max_strike=(float(sp.get('max_strike')) if (want_put and sp.get('max_strike') is not None) else None),
        min_dte=min_dte,
        max_dte=max_dte,
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
                global_sell_put_d3=(symbol_cfg.get('_global_sell_put_d3') or {}),
                global_sell_put_d3_event=(symbol_cfg.get('_global_sell_put_d3_event') or {}),
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
                locked_shares_by_symbol=((portfolio_ctx or {}).get('option_ctx') or {}).get('locked_shares_by_symbol'),
                global_sell_call_d3=(symbol_cfg.get('_global_sell_call_d3') or {}),
                global_sell_call_d3_event=(symbol_cfg.get('_global_sell_call_d3_event') or {}),
            )
        )
    else:
        summary_rows.append(empty_sell_call_summary(symbol, symbol_cfg=symbol_cfg))

    return summary_rows
