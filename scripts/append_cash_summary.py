#!/usr/bin/env python3
from __future__ import annotations

"""Operational CLI wrapper for appending cash summary footers."""

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.cash_summary_footer import append_cash_summary_footer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Append per-account cash summary to notification footer")
    parser.add_argument("--config", default=None, help="options-monitor config used to resolve default accounts")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--market", default="富途")
    parser.add_argument("--accounts", nargs="+", default=None)
    parser.add_argument("--report-dir", default="output/reports", help="Report dir for default notification path")
    parser.add_argument("--notification", default=None, help="Notification text file")
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = (REPO_ROOT / report_dir).resolve()
    notification = Path(args.notification) if args.notification else report_dir / "symbols_notification.txt"
    append_cash_summary_footer(
        base=REPO_ROOT,
        notification=notification,
        config=args.config,
        data_config=args.data_config,
        market=str(args.market),
        accounts=([str(account) for account in args.accounts] if args.accounts is not None else None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
