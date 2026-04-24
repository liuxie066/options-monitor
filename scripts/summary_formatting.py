from __future__ import annotations

from typing import Any

import pandas as pd

from scripts.report_formatting import num, pct, strike_text


SUMMARY_DEFAULTS = {
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


def apply_summary_defaults(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col, default in SUMMARY_DEFAULTS.items():
        if col not in out.columns:
            out[col] = default
    return out


def _present_text(value: Any, default: str = '-') -> str:
    if pd.isna(value) or not value:
        return default
    return str(value)


def _present_int(value: Any, default: str = '-') -> str:
    if pd.isna(value):
        return default
    return str(int(value))


def format_summary_row(row: pd.Series) -> str:
    candidate_count = 0 if pd.isna(row['candidate_count']) else int(row['candidate_count'])
    return (
        f"- {row['symbol']} | {row['strategy']} | 候选 {candidate_count} | "
        f"Top {_present_text(row['top_contract'])} | 年化 {pct(row['annualized_return'])} | "
        f"净收入 {num(row['net_income'])} | DTE {_present_int(row['dte'])} | "
        f"Strike {strike_text(row['strike'])} | {_present_text(row['risk_label'])} | "
        f"{_present_text(row['note'])}"
    )
