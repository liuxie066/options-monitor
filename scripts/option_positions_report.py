#!/usr/bin/env python3
"""Read-only reports for option_positions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.option_positions_core.reporting import build_monthly_income_report
from scripts.option_positions_core.service import OptionPositionsRepository, load_table_ref


def money(value: float | int | None, currency: str) -> str:
    if value is None:
        return "-"
    v = float(value)
    ccy = str(currency or "").upper()
    if ccy == "USD":
        return f"${v:,.2f}"
    if ccy == "HKD":
        return f"HKD {v:,.2f}"
    if ccy == "CNY":
        return f"¥{v:,.2f}"
    return f"{v:,.2f} {ccy}"


def print_monthly_income(report: dict, *, include_rows: bool = False) -> None:
    print("# Option Positions Monthly Income")
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
    print("| month | account | currency | realized_gross | closed_contracts | positions |")
    print("|---|---|---:|---:|---:|---:|")
    summary = report.get("summary") or []
    if not summary:
        print("| - | - | - | - | 0 | 0 |")
    else:
        for row in summary:
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('currency')} | "
                f"{money(row.get('realized_gross'), row.get('currency') or '')} | "
                f"{row.get('closed_contracts')} | {row.get('positions')} |"
            )

    if include_rows:
        print("")
        print("## Details")
        print("")
        print("| month | account | symbol | currency | contracts | premium | close_price | multiplier | realized_gross | close_type | record_id |")
        print("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
        for row in report.get("rows") or []:
            ccy = row.get("currency") or ""
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | {ccy} | "
                f"{row.get('contracts_closed')} | {row.get('premium')} | {row.get('close_price')} | "
                f"{row.get('multiplier')} | {money(row.get('realized_gross'), ccy)} | "
                f"{row.get('close_type')} | {row.get('record_id')} |"
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
    parser = argparse.ArgumentParser(description="Option positions reports")
    parser.add_argument("--pm-config", default="../portfolio-management/config.json")

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_monthly = sub.add_parser("monthly-income", help="monthly realized gross income from closed option positions")
    p_monthly.add_argument("--broker", default="富途")
    p_monthly.add_argument("--market", default=None, help="DEPRECATED alias of --broker")
    p_monthly.add_argument("--account", default=None)
    p_monthly.add_argument("--month", default=None, help="YYYY-MM")
    p_monthly.add_argument("--format", choices=["text", "json"], default="text")
    p_monthly.add_argument("--include-rows", action="store_true")

    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    pm_config = Path(args.pm_config)
    if not pm_config.is_absolute():
        pm_config = (base / pm_config).resolve()

    if args.cmd == "monthly-income":
        broker = args.market or args.broker
        repo = OptionPositionsRepository(load_table_ref(pm_config))
        records = repo.list_records(page_size=500)
        report = build_monthly_income_report(
            records,
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
