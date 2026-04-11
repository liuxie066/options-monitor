#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.render_sell_call_alerts import render_sell_call_alerts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Render Sell Call alert text from candidate CSV')
    parser.add_argument('--input', default=None, help='Input CSV path (default: <report-dir>/sell_call_candidates.csv)')
    parser.add_argument('--report-dir', default='output/reports', help='Report dir for default input/output (default: output/reports)')
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--symbol', default=None)
    parser.add_argument('--output', default=None, help='Output txt path (default: <report-dir>/sell_call_alerts.txt)')
    parser.add_argument('--layered', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """render_sell_call_alerts CLI 入口。"""
    args = parse_args(argv)
    render_sell_call_alerts(
        input_path=args.input,
        report_dir=args.report_dir,
        top=args.top,
        symbol=args.symbol,
        output_path=args.output,
        layered=args.layered,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
