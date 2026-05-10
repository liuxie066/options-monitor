"""Read-only reports for position lots.

Render and dispatch helpers for `./om option-positions report ...`.
Business logic lives in `src.application.option_positions_facade` /
`src.application.option_positions_reporting`; this module only wires the CLI
subcommand to those services and renders Markdown output.
"""

from __future__ import annotations

import json
from typing import Any

from src.application.option_positions_facade import (
    build_option_positions_monthly_income_report,
    format_position_money,
)


def print_monthly_income(report: dict[str, Any], *, include_rows: bool = False) -> None:
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
    print(
        "| month | account | currency | realized_gross | realized_gross_cny | "
        "premium_received_gross | premium_received_gross_cny | closed_contracts | "
        "premium_contracts | positions | premium_positions |"
    )
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
        print(
            "| month | account | symbol | currency | contracts | premium | close_price | "
            "multiplier | realized_gross | close_type | record_id |"
        )
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
            print(
                "| month | account | symbol | currency | contracts | premium | multiplier | "
                "premium_received_gross | record_id |"
            )
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


def run_report(args, *, base, repo) -> int:
    """Dispatch `option-positions report <subcmd>` against an already-resolved repo."""
    sub = getattr(args, "report_cmd", None)
    if sub == "monthly-income":
        report = build_option_positions_monthly_income_report(
            repo,
            base=base,
            account=args.account,
            broker=args.broker,
            month=args.month,
        )
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print_monthly_income(report, include_rows=bool(args.include_rows))
        return 0
    raise SystemExit(f"unknown report subcommand: {sub}")
