#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import pandas as pd
from pandas.errors import EmptyDataError

from scripts.alert_rules import render_sell_call_comment
from scripts.io_utils import atomic_write_text
from scripts.report_formatting import num, pct, strike_text
from domain.domain.engine import (
    build_strategy_config,
    rank_scored_candidates,
)


def render_one(row) -> str:
    strike = strike_text(row["strike"])
    title = f"[Sell Call 候选] {row['symbol']} {row['expiration']} {strike}C"
    body = [
        title,
        "",
        f"Spot: {num(row['spot'])}",
        f"持仓成本: {num(row.get('avg_cost'))}",
        f"富途持仓(总): {int(row.get('shares_total', row.get('shares', 0)) or 0)} 股 | 已占用: {int(row.get('shares_locked', 0) or 0)} 股 | 可用: {int(row.get('shares_available_for_cover', 0) or 0)} 股",
        f"可覆盖张数: {int(row.get('covered_contracts_available', 0) or 0)} | fully-covered: {'是' if bool(row.get('is_fully_covered_available', False)) else '否'}",
        "",
        f"DTE: {int(row['dte'])}",
        f"卖出参考价(mid): {num(row['mid'])}  (收益率按 mid 价计算)",
        "",
        f"净收入({str(row.get('currency') or row.get('option_ccy') or 'N/A').upper()}): {num(row['net_income'])}",
        f"净权利金年化: {pct(row['annualized_net_premium_return'])}",
        f"若被行权总收益: {pct(row['if_exercised_total_return'])}",
        f"strike 相对现价空间: {pct(row['strike_above_spot_pct'])}",
        f"strike 相对成本空间: {pct(row['strike_above_cost_pct'])}",
        f"风险标签: {row.get('risk_label', '-')}",
        "",
        f"OI / Volume: {int(row.get('open_interest', 0) or 0)} / {int(row.get('volume', 0) or 0)}",
        f"Spread Ratio: {pct(row.get('spread_ratio', 0))}",
        "",
        f"判断: {render_sell_call_comment(row)}",
    ]
    return "\n".join(body)


def render_sell_call_alerts(
    *,
    input_path: str | Path | None = None,
    report_dir: str | Path = 'output/reports',
    top: int = 5,
    symbol: str | None = None,
    output_path: str | Path | None = None,
    layered: bool = False,
    base_dir: Path | None = None,
) -> str:
    """渲染 Sell Call 候选提醒文本并写入文件。"""
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

    report_dir_path = Path(report_dir)
    if not report_dir_path.is_absolute():
        report_dir_path = (base / report_dir_path).resolve()

    if input_path:
        input_file = Path(input_path)
        if not input_file.is_absolute():
            input_file = (base / input_file).resolve()
    else:
        input_file = (report_dir_path / 'sell_call_candidates.csv').resolve()

    if output_path:
        output_file = Path(output_path)
        if not output_file.is_absolute():
            output_file = (base / output_file).resolve()
    else:
        output_file = (report_dir_path / 'sell_call_alerts.txt').resolve()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = pd.read_csv(input_file)
    except (FileNotFoundError, EmptyDataError):
        df = pd.DataFrame()

    if symbol and not df.empty:
        df = df[df["symbol"] == symbol].copy()

    if df.empty:
        text = "无候选提醒。"
        atomic_write_text(output_file, text)
        print(text)
        return text

    strategy_cfg = build_strategy_config("call")
    top_df = rank_scored_candidates(df, strategy_cfg, layered=layered, top=top)

    blocks = [render_one(row) for _, row in top_df.iterrows()]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    atomic_write_text(output_file, text)
    print(text)
    print(f"[DONE] alerts -> {output_file}")
    return text


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
