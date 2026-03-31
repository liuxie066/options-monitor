"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.fx_rates import CurrencyConverter
from scripts.io_utils import safe_read_csv
from scripts.report_labels import add_sell_put_labels
from scripts.report_summaries import summarize_sell_put
from scripts.sell_put_cash import enrich_sell_put_candidates_with_cash
from scripts.subprocess_utils import run_cmd


def run_sell_put_scan_and_summarize(
    *,
    py: str,
    base: Path,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict,
    sp: dict,
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    fx: CurrencyConverter,
    portfolio_ctx: dict | None,
) -> dict:
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
                        fx.cny_to_native(
                            cny_threshold,
                            native_ccy=('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
                        )
                    )
                    or 0.0
                )
            ))(float(sp.get('min_net_income') or 0.0))
        ),
    ])

    if is_scheduled:
        cmd.append('--quiet')

    run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)

    add_sell_put_labels(base, symbol_sp, symbol_sp_labeled)

    # account-aware: attach cash secured usage from option_positions (open short puts)
    df_sp_lab = safe_read_csv(symbol_sp_labeled)
    if not df_sp_lab.empty:
        enrich_sell_put_candidates_with_cash(
            df_labeled=df_sp_lab,
            symbol=symbol,
            portfolio_ctx=portfolio_ctx,
            fx=fx,
            out_path=symbol_sp_labeled,
        )

    if not is_scheduled:
        run_cmd([
            py, 'scripts/render_sell_put_alerts.py',
            '--input', str((report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').as_posix()),
            '--symbol', symbol,
            '--top', str(top_n),
            '--layered',
            '--output', str((report_dir / f'{symbol_lower}_sell_put_alerts.txt').as_posix()),
        ], cwd=base, is_scheduled=is_scheduled)

    return summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg)


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict) -> dict:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
