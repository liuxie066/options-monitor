#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.alert_engine import run_alert_engine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build alert and change text from symbols summary')
    parser.add_argument('--summary-input', default='output/reports/symbols_summary.csv')
    parser.add_argument('--output', default='output/reports/symbols_alerts.txt')
    parser.add_argument('--changes-output', default='output/reports/symbols_changes.txt')
    parser.add_argument('--previous-summary', default=None, help='Previous summary snapshot CSV (default: <state-dir>/symbols_summary_prev.csv)')
    parser.add_argument('--state-dir', default=None, help='[optional] state dir for symbols_summary_prev.csv (overrides --previous-summary when set)')
    parser.add_argument('--update-snapshot', action='store_true')
    parser.add_argument('--policy-json', default=None, help='JSON file for alert policy overrides')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """alert_engine CLI 入口。"""
    args = parse_args(argv)
    result = run_alert_engine(
        summary_input=args.summary_input,
        output=args.output,
        changes_output=args.changes_output,
        previous_summary=args.previous_summary,
        state_dir=args.state_dir,
        update_snapshot=args.update_snapshot,
        policy_json=args.policy_json,
    )

    changes_path = str(result['changes_path'])
    if changes_path != '/dev/null':
        print(result['alert_text'])
        print(f"[DONE] alerts -> {result['output_path']}")
        print(result['changes_text'])
        print(f"[DONE] changes -> {result['changes_path']}")

    if result.get('snapshot_updated'):
        print(f"[DONE] snapshot updated -> {result['previous_path']}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
