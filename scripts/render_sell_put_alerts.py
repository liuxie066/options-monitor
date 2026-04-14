#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError

from scripts.io_utils import atomic_write_text
from domain.domain.engine import (
    build_strategy_config,
    rank_scored_candidates,
)


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

    cash_req_cny = None
    try:
        v = row.get('cash_required_cny')
        cash_req_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_req_cny = None

    cash_used_total = None
    cash_used_symbol = None
    try:
        v = row.get('cash_secured_used_cny_total')
        cash_used_total = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_used_total = None
    try:
        v = row.get('cash_secured_used_cny_symbol')
        cash_used_symbol = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_used_symbol = None

    if cash_used_total is None:
        try:
            cash_used_total = float(row.get('cash_secured_used_usd_total') or row.get('cash_secured_used_usd') or 0.0)
        except Exception:
            cash_used_total = 0.0
    if cash_used_symbol is None:
        try:
            cash_used_symbol = float(row.get('cash_secured_used_usd_symbol') or 0.0)
        except Exception:
            cash_used_symbol = 0.0

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

    headroom = None
    try:
        if cash_req_cny is not None and cash_free_cny is not None:
            headroom = float(cash_free_cny) - float(cash_req_cny)
    except Exception:
        headroom = None

    body = [
        title,
        "",
        f"Spot: {num(row['spot'])}",
        f"DTE: {int(row['dte'])}",
        f"卖出参考价(mid): {num(row['mid'])}  (收益率按 mid 价计算)",
        "",
        f"担保现金需求(1张, CNY): {('-' if cash_req_cny is None else '¥' + num(cash_req_cny, 0))}",
        f"已占用担保现金(全账户, CNY口径): {('-' if cash_used_total is None else '¥' + num(cash_used_total, 0))}",
        f"其中该标的占用(估算, CNY口径): {('-' if cash_used_symbol is None else '¥' + num(cash_used_symbol, 0))}",
        f"富途现金(base, CNY): {('-' if cash_avail_cny is None else '¥' + num(cash_avail_cny, 0))}",
        f"现金余量(base, 扣占用折算, CNY): {('-' if cash_free_cny is None else '¥' + num(cash_free_cny, 0))}",
        f"加仓后余量(估算, CNY): {('-' if headroom is None else '¥' + num(headroom, 0))}",
        "",
        f"净收入({str(row.get('currency') or row.get('option_ccy') or 'N/A').upper()}): {num(row['net_income'])}",
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


def render_sell_put_alerts(
    *,
    input_path: str | Path | None = None,
    report_dir: str | Path = 'output/reports',
    top: int = 5,
    symbol: str | None = None,
    output_path: str | Path | None = None,
    layered: bool = False,
    base_dir: Path | None = None,
) -> str:
    """渲染 Sell Put 候选提醒文本并写入文件。"""
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

    report_dir_path = Path(report_dir)
    if not report_dir_path.is_absolute():
        report_dir_path = (base / report_dir_path).resolve()

    if input_path:
        input_file = Path(input_path)
        if not input_file.is_absolute():
            input_file = (base / input_file).resolve()
    else:
        input_file = (report_dir_path / 'sell_put_candidates_labeled.csv').resolve()

    if output_path:
        output_file = Path(output_path)
        if not output_file.is_absolute():
            output_file = (base / output_file).resolve()
    else:
        output_file = (report_dir_path / 'sell_put_alerts.txt').resolve()

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

    strategy_cfg = build_strategy_config("put")
    top_df = rank_scored_candidates(df, strategy_cfg, layered=layered, top=top)

    blocks = [render_one(row) for _, row in top_df.iterrows()]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    atomic_write_text(output_file, text)
    print(text)
    print(f"[DONE] alerts -> {output_file}")
    return text
