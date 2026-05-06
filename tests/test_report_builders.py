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
        assert '净收入 -' in txt

        df = pd.read_csv(csv_path)
        assert 'annualized_return' in df.columns
        assert 'net_income' in df.columns
        assert pd.isna(df.loc[0, 'annualized_return'])
        assert pd.isna(df.loc[0, 'net_income'])


def test_build_symbols_digest_deduplicates_symbols_with_yield_enhancement_section() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.report_builders import build_symbols_digest

    with TemporaryDirectory() as td:
        report_dir = Path(td)
        (report_dir / "nvda_sell_put_alerts.txt").write_text("put alert\n", encoding="utf-8")
        (report_dir / "nvda_sell_call_alerts.txt").write_text("call alert\n", encoding="utf-8")
        (report_dir / "nvda_yield_enhancement_alerts.txt").write_text("enhance alert\n", encoding="utf-8")

        build_symbols_digest(["NVDA", "NVDA", "NVDA"], report_dir)

        text = (report_dir / "symbols_digest.txt").read_text(encoding="utf-8")
        assert text.count("## NVDA") == 1
        assert "### Sell Put" in text
        assert "### Sell Call" in text
        assert "### Yield Enhancement" in text
        assert "enhance alert" in text
        assert "### Rebound Combo" not in text
        assert "combo alert" not in text
