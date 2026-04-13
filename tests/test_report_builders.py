"""Regression tests for report_builders schema compatibility."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


def test_build_symbols_summary_tolerates_missing_annualized_return_column() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.report_builders import build_symbols_summary

    summary_rows = [
        {
            'symbol': '0700.HK',
            'strategy': 'sell_put',
            'candidate_count': 1,
            'top_contract': 'TENCENT-TEST',
            'net_income': 100.0,
            'strike': 280.0,
            'dte': 30,
            'risk_label': '中',
            'note': 'ok',
        }
    ]

    with TemporaryDirectory() as td:
        report_dir = Path(td)
        build_symbols_summary(summary_rows, report_dir, is_scheduled=False)

        txt_path = report_dir / 'symbols_summary.txt'
        csv_path = report_dir / 'symbols_summary.csv'
        assert txt_path.exists()
        assert csv_path.exists()

        txt = txt_path.read_text(encoding='utf-8')
        assert '年化 -' in txt

        df = pd.read_csv(csv_path)
        assert 'annualized_return' in df.columns
        assert pd.isna(df.loc[0, 'annualized_return'])
