#!/usr/bin/env python3
"""Read-only reports for position lots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from src.application.option_positions_facade import (
    build_option_positions_monthly_income_report,
    format_position_money,
    resolve_option_positions_repo,
)


def print_monthly_income(report: dict, *, include_rows: bool = False) -> None:
    print("# Position Lots Monthly Income")
    print("")
    print("- realized_gross: 按 closed_at 归月的已实现毛收益")
    print("- premium_received_gross: 按 opened_at 归月的 short 开仓权利金到账")
    filters = report.get("filters") or {}
    parts = []
    if filters.get("month"):
        parts.append(f"month={filters['month']}")
    if filters.get("account"):
        parts.append(f"account={filters['account']}")
    if filters.get("broker"):
        parts.append(f"broker={filters['broker']}")
    if parts:
        print("")
        print("filters: " + " | ".join(parts))

    print("")
    print("| month | account | currency | realized_gross | realized_gross_cny | premium_received_gross | premium_received_gross_cny | closed_contracts | premium_contracts | positions | premium_positions |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = report.get("summary") or []
    if not summary:
        print("| - | - | - | - | - | - | - | 0 | 0 | 0 | 0 |")
    else:
        for row in summary:
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('currency')} | "
                f"{format_position_money(row.get('realized_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('realized_gross_cny'), 'CNY')} | "
                f"{format_position_money(row.get('premium_received_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('premium_received_gross_cny'), 'CNY')} | "
                f"{row.get('closed_contracts')} | {row.get('premium_contracts')} | "
                f"{row.get('positions')} | {row.get('premium_positions')} |"
            )

    if include_rows:
        print("")
        print("## Realized Details")
        print("")
        print("| month | account | symbol | currency | contracts | premium | close_price | multiplier | realized_gross | close_type | record_id |")
        print("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
        for row in report.get("rows") or []:
            ccy = row.get("currency") or ""
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | {ccy} | "
                f"{row.get('contracts_closed')} | {row.get('premium')} | {row.get('close_price')} | "
                f"{row.get('multiplier')} | {format_position_money(row.get('realized_gross'), ccy)} | "
                f"{row.get('close_type')} | {row.get('record_id')} |"
            )

        premium_rows = report.get("premium_rows") or []
        if premium_rows:
            print("")
            print("## Premium Received Details")
            print("")
            print("| month | account | symbol | currency | contracts | premium | multiplier | premium_received_gross | record_id |")
            print("|---|---|---|---:|---:|---:|---:|---:|---|")
            for row in premium_rows:
                ccy = row.get("currency") or ""
                print(
                    f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | {ccy} | "
                    f"{row.get('contracts')} | {row.get('premium')} | {row.get('multiplier')} | "
                    f"{format_position_money(row.get('premium_received_gross'), ccy)} | {row.get('record_id')} |"
                )

    warnings = report.get("warnings") or []
    if warnings:
        print("")
        print("## Warnings")
        for item in warnings[:50]:
            print(f"- {item}")
        if len(warnings) > 50:
            print(f"- ... {len(warnings) - 50} more")


def main() -> int:
    parser = argparse.ArgumentParser(description="Position lot reports")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_monthly = sub.add_parser(
        "monthly-income",
        help="monthly option income report with two views: realized_gross by closed_at and premium_received_gross by opened_at",
        description=(
            "Monthly option income report.\n"
            "- realized_gross: groups closed positions by closed_at month.\n"
            "- premium_received_gross: groups short option premium receipts by opened_at month.\n"
            "- *_cny columns are best-effort exchange-rate conversions from rate_cache.json."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_monthly.add_argument("--broker", default="富途")
    p_monthly.add_argument("--market", default=None, help="DEPRECATED alias of --broker")
    p_monthly.add_argument("--account", default=None)
    p_monthly.add_argument("--month", default=None, help="YYYY-MM")
    p_monthly.add_argument("--format", choices=["text", "json"], default="text")
    p_monthly.add_argument("--include-rows", action="store_true")

    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)

    if args.cmd == "monthly-income":
        broker = args.market or args.broker
        report = build_option_positions_monthly_income_report(
            repo,
            base=base,
            account=args.account,
            broker=broker,
            month=args.month,
        )
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print_monthly_income(report, include_rows=bool(args.include_rows))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
