"""Read-only reports for position lots."""

from __future__ import annotations

import json
from typing import Any

from src.application.ledger.api import format_position_money, open_performance_evidence_repository
from src.application.performance.service import build_option_period_performance




def _metric_text(value: Any) -> str:
    metric = value if isinstance(value, dict) else {}
    by_currency = metric.get("by_currency") if isinstance(metric.get("by_currency"), dict) else {}
    parts = [format_position_money(amount, currency) for currency, amount in sorted(by_currency.items())]
    if metric.get("cny") is not None:
        parts.append(format_position_money(metric.get("cny"), "CNY"))
    quality = metric.get("quality") if isinstance(metric.get("quality"), dict) else {}
    status = str(quality.get("status") or "")
    text = " / ".join(parts) if parts else "-"
    return f"{text} ({status})" if status and status != "observed" else text


def print_option_performance(report: dict[str, Any]) -> None:
    period = report.get("period") if isinstance(report.get("period"), dict) else {}
    activity = report.get("activity") if isinstance(report.get("activity"), dict) else {}
    cash = report.get("cash") if isinstance(report.get("cash"), dict) else {}
    pnl = report.get("pnl") if isinstance(report.get("pnl"), dict) else {}
    quality = report.get("quality") if isinstance(report.get("quality"), dict) else {}
    filters = report.get("filters") if isinstance(report.get("filters"), dict) else {}
    print("# Option Performance (monthly-income deprecated alias)")
    print("")
    print(
        f"period={period.get('kind') or '-'} "
        f"{period.get('requested_start_date') or '-'}..{period.get('requested_end_date') or '-'} "
        f"status={period.get('status') or '-'}"
    )
    print(
        "filters: "
        f"month={filters.get('month') or '-'} | account={filters.get('account') or '-'} | "
        f"broker={filters.get('broker') or '-'}"
    )
    print("")
    print("## PnL")
    print(f"- period_total_net: {_metric_text(pnl.get('period_total_net'))}")
    print(f"- period_total_gross: {_metric_text(pnl.get('period_total_gross'))}")
    print(f"- realized_net: {_metric_text(pnl.get('realized_net'))}")
    print(f"- realized_gross: {_metric_text(pnl.get('realized_gross'))}")
    print("")
    print("## Cash")
    print(f"- total_cash_change_net: {_metric_text(cash.get('total_cash_change_net'))}")
    print(f"- option_trade_cash_gross: {_metric_text(cash.get('option_trade_cash_gross'))}")
    print(f"- stock_settlement_cash_gross: {_metric_text(cash.get('stock_settlement_cash_gross'))}")
    print("")
    print("## Activity")
    print(f"- premium_collected_gross: {_metric_text(activity.get('premium_collected_gross'))}")
    print(f"- premium_paid_gross: {_metric_text(activity.get('premium_paid_gross'))}")
    print("")
    print("PnL, cash, and premium activity are parallel namespaces; do not add or subtract them as residual components.")
    missing = quality.get("missing") if isinstance(quality.get("missing"), list) else []
    if missing:
        print("missing: " + ", ".join(str(item) for item in missing[:10]))


def _format_rate(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "-"


def _format_cny(value: Any) -> str:
    return format_position_money(value, "CNY")


def print_monthly_income(report: dict[str, Any], *, include_rows: bool = False) -> None:
    print("# Position Lots Monthly Income")
    print("")
    print("- net_cashflow_gross: 按交易发生月统计资金流")
    print("- realized_pnl_gross: 按平仓/到期月统计已实现收益")
    print("- open_basis_lifecycle_pnl_gross: 按开仓月回填整组生命周期收益")
    print("- premium_received_gross: short 开仓收到的权利金")
    print("- realized_gross: 平仓/到期实现收益")
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

    return_summary = report.get("return_summary") or []
    if return_summary:
        print("")
        print("## Return Summary")
        for row in return_summary:
            if not isinstance(row, dict):
                continue
            print("")
            print(f"### {row.get('account') or '-'} {row.get('month') or '-'} 收益摘要")
            print(f"- 净收益率：{_format_rate(row.get('net_return_rate'))}")
            print(f"- 净收入：{_format_cny(row.get('net_income_cny'))}")
            print(f"- 现金担保：{_format_cny(row.get('cash_secured_cny'))}")
            print(
                f"- 按 {row.get('annualized_basis_days') or 0} 天折年化："
                f"{_format_rate(row.get('annualized_net_return_rate'))}"
            )
            print(f"- 权利金毛收益率：{_format_rate(row.get('premium_return_rate'))}")
            print(f"- 口径：{row.get('return_basis') or 'current_cash_secured'}")

    diagnostics = report.get("diagnostics") or []
    if diagnostics:
        print("")
        print("## Diagnostics")
        for row in diagnostics[:6]:
            if not isinstance(row, dict):
                continue
            missing = row.get("missing_fields") if isinstance(row.get("missing_fields"), list) else []
            print(
                "- "
                f"{row.get('account') or '-'} {row.get('month') or '-'} "
                f"status={row.get('status') or '-'} "
                f"events={row.get('matched_trade_events_count') or 0} "
                f"lots={row.get('position_lot_snapshots_count', row.get('matched_lots_count')) or 0} "
                f"closed={row.get('closed_lots_count') or 0} "
                f"premium={row.get('premium_rows_count') or 0} "
                f"cash_secured={'yes' if row.get('cash_secured_collateral_status') == 'reported' or row.get('cash_secured_available') else 'no'} "
                f"missing={','.join(str(item) for item in missing) if missing else '-'}"
            )

    print("")
    print(
        "| month | account | currency | net_cashflow_gross | assignment_stock_net_cashflow_gross | realized_pnl_gross | "
        "open_basis_lifecycle_pnl_gross | premium_received_gross | realized_gross | closed_contracts | "
        "premium_contracts | positions | premium_positions |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = report.get("summary") or []
    if not summary:
        print("| - | - | - | - | - | - | - | - | 0 | 0 | 0 | 0 | 0 |")
    else:
        for row in summary:
            print(
                f"| {row.get('month')} | {row.get('account')} | {row.get('currency')} | "
                f"{format_position_money(row.get('net_cashflow_gross'), row.get('currency') or '')} | "
                f"{format_position_money(row.get('assignment_stock_net_cashflow_gross'), row.get('currency') or '')} | "
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

        stock_settlement_rows = report.get("stock_settlement_rows") or []
        if stock_settlement_rows:
            print("")
            print("## Assignment Stock Settlement Details")
            print("")
            print(
                "| month | account | symbol | stock_side | currency | shares | price | "
                "cash_in_gross | cash_out_gross | net_cashflow_gross | event_id |"
            )
            print("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|")
            for row in stock_settlement_rows:
                ccy = row.get("currency") or ""
                print(
                    f"| {row.get('month')} | {row.get('account')} | {row.get('symbol')} | "
                    f"{str(row.get('trade_action') or '').replace('assignment_stock_', '')} | {ccy} | "
                    f"{row.get('shares')} | {row.get('price')} | "
                    f"{format_position_money(row.get('cash_in_gross'), ccy)} | "
                    f"{format_position_money(row.get('cash_out_gross'), ccy)} | "
                    f"{format_position_money(row.get('net_cashflow_gross'), ccy)} | {row.get('event_id')} |"
                )

        assignment_lifecycle_rows = report.get("assignment_lifecycle_rows") or []
        if assignment_lifecycle_rows:
            print("")
            print("## Assignment Stock Lifecycle")
            print("")
            print(
                "| month | account | symbol | status | review_status | currency | shares_remaining | "
                "stock_cost_per_share | spot | stock_unrealized_pnl | stock_realized_pnl | "
                "option_premium_attribution | assignment_lifecycle_pnl | quote_status | stock_lot_id |"
            )
            print("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
            for row in assignment_lifecycle_rows:
                ccy = row.get("currency") or ""
                print(
                    f"| {row.get('opened_month') or row.get('month')} | {row.get('account')} | {row.get('symbol')} | "
                    f"{row.get('status')} | {row.get('review_status')} | {ccy} | "
                    f"{row.get('shares_remaining')} | {row.get('stock_cost_per_share')} | {row.get('spot')} | "
                    f"{format_position_money(row.get('assigned_stock_unrealized_pnl'), ccy)} | "
                    f"{format_position_money(row.get('assigned_stock_realized_pnl'), ccy)} | "
                    f"{format_position_money(row.get('option_premium_attribution'), ccy)} | "
                    f"{format_position_money(row.get('assignment_lifecycle_pnl'), ccy)} | "
                    f"{row.get('quote_status')} | {row.get('stock_lot_id')} |"
                )

        assigned_stock_review_rows = report.get("assigned_stock_review_rows") or []
        if assigned_stock_review_rows:
            print("")
            print("## Assignment Stock Review")
            print("")
            print("| status | month | account | symbol | stock_lot_id | event_id | message |")
            print("|---|---|---|---|---|---|---|")
            for row in assigned_stock_review_rows:
                print(
                    f"| {row.get('status')} | {row.get('month') or '-'} | {row.get('account') or '-'} | "
                    f"{row.get('symbol') or '-'} | {row.get('stock_lot_id') or '-'} | "
                    f"{row.get('event_id') or row.get('stock_event_id') or '-'} | {row.get('message') or '-'} |"
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
        del base
        period = (
            {"period": "month", "month": args.month}
            if str(args.month or "").strip()
            else {"period": "mtd"}
        )
        report = build_option_period_performance(
            repo,
            period=period,
            account=args.account,
            broker=args.broker,
            include_rows=bool(args.include_rows),
            evidence_repo=open_performance_evidence_repository(repo),
            refresh_quotes=False,
        )
        report = dict(report)
        report["schema_version"] = "option_performance_report.output.v1"
        report["assignment_lifecycle"] = report.pop("assigned_stock", {})
        report["filters"] = {"month": args.month, "account": args.account, "broker": args.broker}
        report["deprecation"] = {
            "status": "deprecated_alias",
            "replacement": "./om option-performance report",
        }
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print_option_performance(report)
        return 0
    raise SystemExit(f"unknown report subcommand: {sub}")
