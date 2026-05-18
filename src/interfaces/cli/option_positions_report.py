"""Read-only reports for position lots."""

from __future__ import annotations

import json
from typing import Any

from src.application.ledger.api import (
    format_position_money,
    position_monthly_income_report,
)


def print_monthly_income(report: dict[str, Any], *, include_rows: bool = False) -> None:
    print("# Position Lots Monthly Income")
    print("")
    print("- net_cashflow_gross: 按交易发生月统计资金流")
    print("- realized_pnl_gross: 按平仓/到期月统计已实现收益")
    print("- open_basis_lifecycle_pnl_gross: 按开仓月回填整组生命周期收益")
    print("- premium_received_gross / realized_gross: 兼容字段")
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
        "| month | account | currency | net_cashflow_gross | realized_pnl_gross | "
        "open_basis_lifecycle_pnl_gross | premium_received_gross | realized_gross | closed_contracts | "
        "premium_contracts | positions | premium_positions |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = report.get("summary") or []
    if not summary:
        print("| - | - | - | - | - | - | - | - | 0 | 0 | 0 | 0 |")
    else:
        for row in summary:
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('currency')} | "
                f"{format_position_money(row.get('net_cashflow_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('realized_pnl_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('open_basis_lifecycle_pnl_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('premium_received_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('realized_gross'), row.get('currency') or '')} | "
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

        cashflow_rows = report.get("cashflow_rows") or []
        if cashflow_rows:
            print("")
            print("## Cashflow Details")
            print("")
            print(
                "| month | account | symbol | action | currency | contracts | price | "
                "cash_in_gross | cash_out_gross | net_cashflow_gross | event_id |"
            )
            print("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|")
            for row in cashflow_rows:
                ccy = row.get("currency") or ""
                print(
                    f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | {row.get('trade_action')} | {ccy} | "
                    f"{row.get('contracts')} | {row.get('price')} | "
                    f"{format_position_money(row.get('cash_in_gross'), ccy)} | "
                    f"{format_position_money(row.get('cash_out_gross'), ccy)} | "
                    f"{format_position_money(row.get('net_cashflow_gross'), ccy)} | {row.get('event_id')} |"
                )

        open_basis_rows = report.get("open_basis_rows") or []
        if open_basis_rows:
            print("")
            print("## Open Basis Attribution")
            print("")
            print(
                "| month | account | symbol | currency | sell_open_premium | sell_close_cost_actual | "
                "enhancement_call_buy_cost | enhancement_call_sell_proceeds_actual | lifecycle_pnl | is_final |"
            )
            print("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
            for row in open_basis_rows:
                ccy = row.get("currency") or ""
                print(
                    f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | {ccy} | "
                    f"{format_position_money(row.get('sell_open_premium'), ccy)} | "
                    f"{format_position_money(row.get('sell_close_cost_actual'), ccy)} | "
                    f"{format_position_money(row.get('enhancement_call_buy_cost'), ccy)} | "
                    f"{format_position_money(row.get('enhancement_call_sell_proceeds_actual'), ccy)} | "
                    f"{format_position_money(row.get('open_basis_lifecycle_pnl_gross'), ccy)} | "
                    f"{row.get('is_final')} |"
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
        report = position_monthly_income_report(
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
