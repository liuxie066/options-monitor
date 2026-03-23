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
    annual = float(row.get("annualized_net_return_on_cash_basis", 0) or 0)
    spread = float(row.get("spread_ratio", 1) or 1)

    if risk == "激进":
        return "年化很高，但离现价较近，偏激进。"
    if annual >= 0.20 and spread <= 0.20:
        return "收益和安全边际比较平衡，可优先看。"
    if annual >= 0.12:
        return "收益尚可，整体可考虑。"
    return "可作为备选观察。"


def render_one(row) -> str:
    symbol = row["symbol"]
    expiration = row["expiration"]
    strike = num(row["strike"], 0 if float(row["strike"]).is_integer() else 2)
    title = f"[Sell Put 候选] {symbol} {expiration} {strike}P"

    cash_req = None
    cash_req_cny = None
    try:
        cash_req = float(row.get('cash_required_usd')) if not pd.isna(row.get('cash_required_usd')) else None
    except Exception:
        cash_req = None
    if cash_req is None:
        try:
            cash_req = float(row['strike']) * 100.0
        except Exception:
            cash_req = None

    try:
        v = row.get('cash_required_cny')
        cash_req_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_req_cny = None

    cash_used_total = 0.0
    cash_used_symbol = 0.0
    try:
        cash_used_total = float(row.get('cash_secured_used_usd_total') or row.get('cash_secured_used_usd') or 0.0)
    except Exception:
        cash_used_total = 0.0
    try:
        cash_used_symbol = float(row.get('cash_secured_used_usd_symbol') or 0.0)
    except Exception:
        cash_used_symbol = 0.0

    cash_avail = None
    cash_avail_est = None
    try:
        v = row.get('cash_available_usd')
        cash_avail = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_avail = None
    try:
        v = row.get('cash_available_usd_est')
        cash_avail_est = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_avail_est = None

    cash_free = None
    cash_free_est = None

    cash_avail_cny = None
    cash_free_cny = None
    try:
        v = row.get('cash_available_cny')
        cash_avail_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_avail_cny = None
    try:
        v = row.get('cash_free_cny')
        cash_free_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_free_cny = None
    try:
        v = row.get('cash_free_usd')
        cash_free = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_free = None
    try:
        v = row.get('cash_free_usd_est')
        cash_free_est = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_free_est = None

    headroom = None
    headroom_est = None
    try:
        if cash_req is not None and cash_free is not None:
            headroom = float(cash_free) - float(cash_req)
    except Exception:
        headroom = None
    try:
        if cash_req is not None and cash_free_est is not None:
            headroom_est = float(cash_free_est) - float(cash_req)
    except Exception:
        headroom_est = None

    body = [
        title,
        "",
        f"Spot: {num(row['spot'])}",
        f"DTE: {int(row['dte'])}",
        f"卖出参考价(mid): {num(row['mid'])}  (收益率按 mid 价计算)",
        "",
        f"担保现金需求(1张): {('-' if cash_req is None else '$' + num(cash_req, 0))} / {('-' if cash_req_cny is None else '¥' + num(cash_req_cny, 0))}",
        f"已占用担保现金(全账户): {'$' + num(cash_used_total, 0) if cash_used_total else '$0'}",
        f"其中该标的占用: {'$' + num(cash_used_symbol, 0) if cash_used_symbol else '$0'}",
        f"富途CNY现金(base): {('-' if cash_avail_cny is None else '¥' + num(cash_avail_cny, 0))}",
        f"现金余量(base, 扣占用折算): {('-' if cash_free_cny is None else '¥' + num(cash_free_cny, 0))}",
        f"富途USD现金: {('-' if cash_avail is None else '$' + num(cash_avail, 0))}",
        f"现金余量(USD口径): {('-' if cash_free is None else '$' + num(cash_free, 0))}",
        f"加仓后余量(USD口径): {('-' if headroom is None else '$' + num(headroom, 0))}",
        f"富途USD现金(折算, 以CNY现金换算): {('-' if cash_avail_est is None else '$' + num(cash_avail_est, 0))}",
        f"现金余量(折算): {('-' if cash_free_est is None else '$' + num(cash_free_est, 0))}",
        f"加仓后余量(折算): {('-' if headroom_est is None else '$' + num(headroom_est, 0))}",
        "",
        f"净收入: {num(row['net_income'])}",
        f"净年化: {pct(row['annualized_net_return_on_cash_basis'])}",
        f"OTM: {pct(row['otm_pct'])}",
        f"风险标签: {row.get('risk_label', '-')}",
        "",
        f"OI / Volume: {int(row.get('open_interest', 0) or 0)} / {int(row.get('volume', 0) or 0)}",
        f"Spread Ratio: {pct(row.get('spread_ratio', 0))}",
        "",
        f"判断: {default_comment(row)}",
    ]
    return "\n".join(body)


def pick_layered(df: pd.DataFrame) -> pd.DataFrame:
    """Pick one candidate from each risk layer if available, then fill remaining by ranking."""
    selected = []
    used = set()

    layer_order = ["激进", "中性", "保守"]
    for layer in layer_order:
        layer_df = df[df["risk_label"] == layer].copy()
        if layer_df.empty:
            continue
        layer_df = layer_df.sort_values(
            ["annualized_net_return_on_cash_basis", "net_income"],
            ascending=[False, False],
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
        ["annualized_net_return_on_cash_basis", "net_income"],
        ascending=[False, False],
    )

    for _, row in remaining.iterrows():
        if len(selected) >= 5:
            break
        selected.append(row)

    return pd.DataFrame(selected)


def main():
    parser = argparse.ArgumentParser(description="Render Sell Put alert text from candidate CSV")
    parser.add_argument("--input", default="output/reports/sell_put_candidates_labeled.csv")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--output", default="output/reports/sell_put_alerts.txt")
    parser.add_argument("--layered", action="store_true", help="Pick layered alerts by risk label")
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
        sort_cols = []
        ascending = []
        if "annualized_net_return_on_cash_basis" in df.columns:
            sort_cols.append("annualized_net_return_on_cash_basis")
            ascending.append(False)
        if "net_income" in df.columns:
            sort_cols.append("net_income")
            ascending.append(False)
        if sort_cols:
            df = df.sort_values(sort_cols, ascending=ascending)
        top = df.head(args.top)

    blocks = [render_one(row) for _, row in top.iterrows()]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    output_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"[DONE] alerts -> {output_path}")


if __name__ == "__main__":
    main()
