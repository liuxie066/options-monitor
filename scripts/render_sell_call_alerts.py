#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd
from pandas.errors import EmptyDataError


def pct(v, digits=2):
    if pd.isna(v):
        return "-"
    return f"{float(v)*100:.{digits}f}%"


def num(v, digits=2):
    if pd.isna(v):
        return "-"
    return f"{float(v):,.{digits}f}"


def default_comment(row) -> str:
    risk = row.get("risk_label", "未知")
    annual = float(row.get("annualized_net_premium_return", 0) or 0)
    total = float(row.get("if_exercised_total_return", 0) or 0)

    if risk == "激进":
        return "权利金不错，但行权价离现价较近，更容易卖飞。"
    if annual >= 0.10 and total >= 0.15:
        return "权利金收益和被行权后的总收益都比较平衡，可优先看。"
    if annual >= 0.06:
        return "收益尚可，适合作为 sell call 备选。"
    return "可作为备选观察。"


def render_one(row) -> str:
    strike = num(row["strike"], 0 if float(row["strike"]).is_integer() else 2)
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
        f"净收入: {num(row['net_income'])}",
        f"净权利金年化: {pct(row['annualized_net_premium_return'])}",
        f"若被行权总收益: {pct(row['if_exercised_total_return'])}",
        f"strike 相对现价空间: {pct(row['strike_above_spot_pct'])}",
        f"strike 相对成本空间: {pct(row['strike_above_cost_pct'])}",
        f"风险标签: {row.get('risk_label', '-')}",
        "",
        f"OI / Volume: {int(row.get('open_interest', 0) or 0)} / {int(row.get('volume', 0) or 0)}",
        f"Spread Ratio: {pct(row.get('spread_ratio', 0))}",
        "",
        f"判断: {default_comment(row)}",
    ]
    return "\n".join(body)


def pick_layered(df: pd.DataFrame) -> pd.DataFrame:
    selected = []
    used = set()
    layer_order = ["激进", "中性", "保守"]

    for layer in layer_order:
        layer_df = df[df["risk_label"] == layer].copy()
        if layer_df.empty:
            continue
        layer_df = layer_df.sort_values(
            ["annualized_net_premium_return", "if_exercised_total_return", "net_income"],
            ascending=[False, False, False],
        )
        row = layer_df.iloc[0]
        key = (row["symbol"], row["expiration"], row["strike"])
        if key not in used:
            selected.append(row)
            used.add(key)

    remaining = df.copy()
    if used:
        remaining = remaining[~remaining.apply(lambda r: (r["symbol"], r["expiration"], r["strike"]) in used, axis=1)]
    remaining = remaining.sort_values(
        ["annualized_net_premium_return", "if_exercised_total_return", "net_income"],
        ascending=[False, False, False],
    )
    for _, row in remaining.iterrows():
        if len(selected) >= 5:
            break
        selected.append(row)
    return pd.DataFrame(selected)


def main():
    parser = argparse.ArgumentParser(description="Render Sell Call alert text from candidate CSV")
    parser.add_argument("--input", default="output/reports/sell_call_candidates.csv")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--output", default="output/reports/sell_call_alerts.txt")
    parser.add_argument("--layered", action="store_true")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    input_path = base / args.input
    output_path = base / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = pd.read_csv(input_path)
    except (FileNotFoundError, EmptyDataError):
        df = pd.DataFrame()
    if args.symbol and not df.empty:
        df = df[df["symbol"] == args.symbol].copy()

    if df.empty:
        text = "无候选提醒。"
        output_path.write_text(text, encoding="utf-8")
        print(text)
        return

    if args.layered and "risk_label" in df.columns:
        top = pick_layered(df).head(args.top)
    else:
        df = df.sort_values(
            ["annualized_net_premium_return", "if_exercised_total_return", "net_income"],
            ascending=[False, False, False],
        )
        top = df.head(args.top)

    blocks = [render_one(row) for _, row in top.iterrows()]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    output_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"[DONE] alerts -> {output_path}")


if __name__ == "__main__":
    main()
