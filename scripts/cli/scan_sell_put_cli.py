#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.scan_sell_put import run_sell_put_scan


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sell Put scan on Yahoo required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--min-otm-pct", type=float, default=0.05)
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net return in [0,1]")
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-open-interest", type=float, default=100)
    parser.add_argument("--min-volume", type=float, default=10)
    parser.add_argument("--max-spread-ratio", type=float, default=0.30)
    parser.add_argument("--min-iv", type=float, default=None, help="min implied volatility (decimal, e.g. 0.15)")
    parser.add_argument("--max-iv", type=float, default=None, help="max implied volatility (decimal, e.g. 2.0)")
    parser.add_argument("--require-bid-ask", action="store_true", help="require bid>0 and ask>0 (better fillability)")
    parser.add_argument("--min-abs-delta", type=float, default=None, help="min abs(delta) (e.g. 0.15)")
    parser.add_argument("--max-abs-delta", type=float, default=None, help="max abs(delta) (e.g. 0.28)")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output/reports/sell_put_candidates.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: ./output)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Sell put 扫描 CLI 入口。"""
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output" / "reports" / "sell_put_candidates.csv")

    try:
        run_sell_put_scan(
            symbols=args.symbols,
            input_root=input_root,
            output=out_path,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            min_otm_pct=args.min_otm_pct,
            min_annualized_net_return=args.min_annualized_net_return,
            min_net_income=args.min_net_income,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            min_open_interest=args.min_open_interest,
            min_volume=args.min_volume,
            max_spread_ratio=args.max_spread_ratio,
            min_iv=args.min_iv,
            max_iv=args.max_iv,
            require_bid_ask=args.require_bid_ask,
            min_abs_delta=args.min_abs_delta,
            max_abs_delta=args.max_abs_delta,
            quiet=args.quiet,
        )
    except ValueError as e:
        raise SystemExit(f"[ARG_ERROR] {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
