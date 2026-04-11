#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.scan_scheduler import run_scheduler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Scan scheduler / frequency controller for options-monitor')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='output/state', help='Directory for scheduler_state.json (default: output/state)')
    parser.add_argument('--state', default=None, help='[deprecated] explicit scheduler_state.json path. Prefer --state-dir.')
    parser.add_argument('--schedule-key', default='schedule', help='Top-level key to read schedule config from (default: schedule). Example: schedule_hk')
    parser.add_argument('--account', default=None, help='Account id for per-account notify cooldown state (optional).')
    parser.add_argument('--run-if-due', action='store_true', help='When due, run scripts/run_pipeline.py --config <config>')
    parser.add_argument('--mark-notified', action='store_true', help='Update last_notify_utc to now (call this only AFTER you actually sent a notification)')
    parser.add_argument('--mark-scanned', action='store_true', help='Update last_scan_utc to now (call this only AFTER you actually ran a scan)')
    parser.add_argument('--jsonl', action='store_true', help='Print a single-line JSON decision (for automation)')
    parser.add_argument('--force', action='store_true', help='Force running regardless of schedule')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """scan_scheduler CLI 入口。"""
    args = parse_args(argv)
    run_scheduler(
        config=args.config,
        state_dir=args.state_dir,
        state=args.state,
        schedule_key=args.schedule_key,
        account=args.account,
        run_if_due=args.run_if_due,
        mark_notified=args.mark_notified,
        mark_scanned=args.mark_scanned,
        jsonl=args.jsonl,
        force=args.force,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
