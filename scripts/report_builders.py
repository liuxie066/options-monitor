"""Report builders.

Extracted from run_pipeline.py (Stage 3).

Functions in this module may do small file I/O for report outputs.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.summary_formatting import apply_summary_defaults, format_summary_row

def build_symbols_summary(summary_rows: list[dict], report_dir: Path, *, is_scheduled: bool = False):
    df = apply_summary_defaults(pd.DataFrame(summary_rows))
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
            lines.append(format_summary_row(r))

    if not is_scheduled:
        txt_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f"[DONE] symbols summary text -> {txt_path}")

    if not is_scheduled:
        print(f"[DONE] symbols summary -> {csv_path}")

def build_symbols_digest(symbols: list[str], report_dir: Path):
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
