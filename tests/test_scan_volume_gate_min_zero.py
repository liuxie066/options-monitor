from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_sell_put_keeps_zero_volume_when_min_volume_zero() -> None:
    _add_repo_to_syspath()
    from scripts.cli import scan_sell_put_cli

    with TemporaryDirectory() as td:
        root = Path(td)
        parsed_dir = root / 'parsed'
        parsed_dir.mkdir(parents=True, exist_ok=True)

        pd.DataFrame([
            {
                'symbol': '0700.HK',
                'option_type': 'put',
                'expiration': '2026-05-01',
                'dte': 14,
                'contract_symbol': 'TSTP',
                'strike': 90.0,
                'spot': 100.0,
                'bid': 1.9,
                'ask': 2.1,
                'mid': 2.0,
                'open_interest': 100,
                'volume': 0,
                'implied_volatility': 0.2,
                'delta': -0.2,
                'multiplier': 100,
                'currency': 'HKD',
            }
        ]).to_csv(parsed_dir / '0700.HK_required_data.csv', index=False)

        out_path = root / 'sell_put_candidates.csv'
        old_argv = sys.argv
        try:
            sys.argv = [
                'scan_sell_put_cli.py',
                '--symbols', '0700.HK',
                '--input-root', str(root),
                '--output', str(out_path),
                '--min-open-interest', '50',
                '--min-volume', '0',
                '--min-net-income', '0',
                '--min-annualized-net-return', '0',
                '--quiet',
            ]
            scan_sell_put_cli.main()
        finally:
            sys.argv = old_argv

        out = pd.read_csv(out_path)
        assert len(out) == 1
        assert float(out.iloc[0]['volume']) == 0.0
