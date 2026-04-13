"""Report builders.

Extracted from run_pipeline.py (Stage 3).

Functions in this module may do small file I/O for report outputs.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def build_symbols_summary(summary_rows: list[dict], report_dir: Path, *, is_scheduled: bool = False):
    df = pd.DataFrame(summary_rows)
    # Backward/partial schema compatibility: tolerate missing columns in summary rows.
    defaults = {
        'symbol': '-',
        'strategy': '-',
        'candidate_count': 0,
        'top_contract': None,
        'annualized_return': None,
        'net_income': None,
        'strike': None,
        'dte': None,
        'risk_label': None,
        'note': '',
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
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
            annual = '-' if pd.isna(r['annualized_return']) else f"{float(r['annualized_return']) * 100:.2f}%"
            income = '-' if pd.isna(r['net_income']) else f"{float(r['net_income']):,.2f}"
            strike = '-' if pd.isna(r['strike']) else (str(int(r['strike'])) if float(r['strike']).is_integer() else f"{float(r['strike']):.2f}")
            dte = '-' if pd.isna(r['dte']) else str(int(r['dte']))
            candidate_count = 0 if pd.isna(r['candidate_count']) else int(r['candidate_count'])
            top_contract = '-' if pd.isna(r['top_contract']) or not r['top_contract'] else r['top_contract']
            risk_label = '-' if pd.isna(r['risk_label']) or not r['risk_label'] else r['risk_label']
            note = '-' if pd.isna(r['note']) or not r['note'] else r['note']
            lines.append(
                f"- {r['symbol']} | {r['strategy']} | 候选 {candidate_count} | "
                f"Top {top_contract} | 年化 {annual} | 净收入 {income} | "
                f"DTE {dte} | Strike {strike} | {risk_label} | {note}"
            )

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
