#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.scan_sell_call import run_sell_call_scan


SHARES_MIN_ERROR = "shares 必须至少 100，sell call 才有意义。"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sell Call scan on Yahoo required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--avg-cost", type=float, required=True, help="Average holding cost per share")
    parser.add_argument("--shares", type=int, default=100)
    parser.add_argument("--shares-locked", type=int, default=0)
    parser.add_argument("--shares-available-for-cover", type=int, default=None)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=90)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net premium return in [0,1]")
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-open-interest", type=float, default=100)
    parser.add_argument("--min-volume", type=float, default=10)
    parser.add_argument("--max-spread-ratio", type=float, default=0.30)
    parser.add_argument("--d3-event-enabled", dest="d3_event_enabled", action="store_true", default=None)
    parser.add_argument("--no-d3-event-enabled", dest="d3_event_enabled", action="store_false")
    parser.add_argument("--d3-event-mode", type=str, default="warn")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output/reports/sell_call_candidates.csv)")
    parser.add_argument("--reject-log-output", default=None, help="Reject log CSV path (default: <output>_reject_log.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: ./output)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Sell call 扫描 CLI 入口。"""
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output" / "reports" / "sell_call_candidates.csv")

    try:
        run_sell_call_scan(
            symbols=args.symbols,
            input_root=input_root,
            output=out_path,
            avg_cost=args.avg_cost,
            shares=args.shares,
            shares_locked=args.shares_locked,
            shares_available_for_cover=args.shares_available_for_cover,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            min_annualized_net_return=args.min_annualized_net_return,
            min_net_income=args.min_net_income,
            min_open_interest=args.min_open_interest,
            min_volume=args.min_volume,
            max_spread_ratio=args.max_spread_ratio,
            d3_event_cfg={
                "enabled": True if args.d3_event_enabled is None else bool(args.d3_event_enabled),
                "mode": str(args.d3_event_mode or "warn"),
            },
            reject_log_output=(Path(args.reject_log_output).resolve() if args.reject_log_output else None),
            quiet=args.quiet,
        )
    except ValueError as e:
        msg = str(e)
        if msg == SHARES_MIN_ERROR:
            raise SystemExit(msg)
        raise SystemExit(f"[ARG_ERROR] {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
