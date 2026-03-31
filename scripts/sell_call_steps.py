"""Sell-call pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.io_utils import safe_read_csv
from scripts.report_summaries import summarize_sell_call
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
) -> dict:
    """Run sell_call scan + (optional) render + summarize.

    Returns the summary row dict (same schema as summarize_sell_call).
    """
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
