from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


def test_sell_put_accepts_but_ignores_legacy_liquidity_gate_parameters() -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    with TemporaryDirectory() as td:
        root = Path(td)
        parsed_dir = root / "parsed"
        parsed_dir.mkdir(parents=True, exist_ok=True)

        pd.DataFrame(
            [
                {
                    "symbol": "0700.HK",
                    "market": "hk",
                    "option_type": "put",
                    "expiration": "2026-05-01",
                    "dte": 14,
                    "contract_symbol": "TSTP",
                    "strike": 90.0,
                    "spot": 100.0,
                    "bid": 1.9,
                    "ask": 2.1,
                    "mid": 2.0,
                    "quote_update_time": "2026-04-17 10:00:00",
                    "open_interest": 0,
                    "volume": 0,
                    "implied_volatility": 0.2,
                    "delta": -0.2,
                    "multiplier": 100,
                    "currency": "HKD",
                }
            ]
        ).to_csv(parsed_dir / "0700.HK_required_data.csv", index=False)

        out = run_sell_put_scan(
            symbols=["0700.HK"],
            input_root=root,
            output=root / "sell_put_candidates.csv",
            min_open_interest=999_999,
            min_volume=999_999,
            min_net_income=0,
            min_annualized_net_return=0,
            quiet=True,
            quote_freshness_now_utc=datetime(2026, 4, 17, 2, 1, tzinfo=timezone.utc),
        )

        assert len(out) == 1
