"""Sell-call pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.io_utils import safe_read_csv
from scripts.report_summaries import summarize_sell_call
from scripts.sell_call_config import resolve_min_annualized_net_premium_return_from_sell_call_cfg
from scripts.subprocess_utils import run_cmd


def run_sell_call_scan_and_summarize(
    *,
    py: str,
    base: Path,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict,
    cc: dict,
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    stock: dict | None,
    locked_shares_by_symbol: dict[str, int] | None = None,
) -> dict:
    """Run sell_call scan + (optional) render + summarize.

    Returns the summary row dict (same schema as summarize_sell_call).
    """
    # sell_call avg_cost/shares are sourced from account holdings context only.
    # Covered-call cost basis and shares are account-scoped and must come from holdings context.
    if not stock:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    try:
        shares_total = int(stock.get('shares'))
        avg_cost = float(stock.get('avg_cost'))
    except Exception:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    if shares_total <= 0 or avg_cost <= 0:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    locked = 0
    try:
        if locked_shares_by_symbol and symbol:
            locked = int(locked_shares_by_symbol.get(str(symbol).upper(), 0) or 0)
    except Exception:
        locked = 0
    shares_available_for_cover = max(0, int(shares_total) - int(locked))

    symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
    min_annualized = resolve_min_annualized_net_premium_return_from_sell_call_cfg(
        sell_call_cfg=cc,
        source_prefix=f'{symbol}.sell_call',
    )

    cmd = [
        py, 'scripts/scan_sell_call.py',
        '--symbols', symbol,
        '--input-root', str(required_data_dir),
        '--avg-cost', str(avg_cost),
        '--shares', str(shares_total),
        '--shares-locked', str(int(locked)),
        '--shares-available-for-cover', str(int(shares_available_for_cover)),
        '--min-dte', str(cc.get('min_dte', 20)),
        '--max-dte', str(cc.get('max_dte', 90)),
        '--min-otm-pct', str(cc.get('min_otm_pct', 0.0)),
        '--min-annualized-net-return', str(min_annualized),
        '--min-if-exercised-total-return', str(cc.get('min_if_exercised_total_return', 0.0)),
        '--min-open-interest', str(cc.get('min_open_interest', 100)),
        '--min-volume', str(cc.get('min_volume', 10)),
        '--max-spread-ratio', str(cc.get('max_spread_ratio', 0.30)),
        '--output', str(symbol_cc),
    ]
    if cc.get('min_strike') is not None:
        cmd.extend(['--min-strike', str(cc.get('min_strike'))])
    if cc.get('max_strike') is not None:
        cmd.extend(['--max-strike', str(cc.get('max_strike'))])

    # Optional execution-quality filters
    if cc.get('require_bid_ask') is not None:
        if bool(cc.get('require_bid_ask')):
            cmd.append('--require-bid-ask')

    if cc.get('min_iv') is not None:
        cmd.extend(['--min-iv', str(cc.get('min_iv'))])
    if cc.get('max_iv') is not None:
        cmd.extend(['--max-iv', str(cc.get('max_iv'))])

    if cc.get('min_delta') is not None:
        cmd.extend(['--min-delta', str(cc.get('min_delta'))])
    if cc.get('max_delta') is not None:
        cmd.extend(['--max-delta', str(cc.get('max_delta'))])

    if is_scheduled:
        cmd.append('--quiet')
    run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)

    df_cc = safe_read_csv(symbol_cc)
    if not is_scheduled:
        run_cmd([
            py, 'scripts/render_sell_call_alerts.py',
            '--input', str((report_dir / f'{symbol_lower}_sell_call_candidates.csv').as_posix()),
            '--symbol', symbol,
            '--top', str(top_n),
            '--layered',
            '--output', str((report_dir / f'{symbol_lower}_sell_call_alerts.txt').as_posix()),
        ], cwd=base, is_scheduled=is_scheduled)

    return summarize_sell_call(df_cc, symbol, symbol_cfg=symbol_cfg)


def empty_sell_call_summary(symbol: str, *, symbol_cfg: dict) -> dict:
    return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
