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

from scripts.alert_rules import render_sell_put_comment
from scripts.io_utils import atomic_write_text
from scripts.report_formatting import num, pct, strike_text
from domain.domain.engine import (
    build_strategy_config,
    rank_scored_candidates,
)


def _render_judgment(row) -> str:
    def _to_float(value):
        try:
            return float(value) if value is not None and not pd.isna(value) else None
        except Exception:
            return None

    cash_req_cny = _to_float(row.get("cash_required_cny"))
    cash_free_cny = _to_float(row.get("cash_free_cny"))
    cash_free_total_cny = _to_float(row.get("cash_free_total_cny"))
    cash_req_usd = _to_float(row.get("cash_required_usd"))
    cash_free_usd = _to_float(row.get("cash_free_usd"))
    cash_free_usd_est = _to_float(row.get("cash_free_usd_est"))

    if cash_req_cny is not None and cash_free_cny is not None and cash_req_cny > cash_free_cny:
        return f"所需担保现金约 ¥{cash_req_cny:,.0f}，但当前 base(CNY) 现金余量约 ¥{cash_free_cny:,.0f}（扣占用后折算），可能无法再加仓。"
    if cash_req_cny is not None and cash_free_cny is None and cash_free_total_cny is not None and cash_req_cny > cash_free_total_cny:
        return f"所需担保现金约 ¥{cash_req_cny:,.0f}，但当前总可用折算约 ¥{cash_free_total_cny:,.0f}（扣占用后折算），可能无法再加仓。"
    if cash_req_usd is not None and cash_free_usd is not None and cash_req_usd > cash_free_usd:
        return f"所需担保现金约 ${cash_req_usd:,.0f}，但当前账户可用担保现金约 ${cash_free_usd:,.0f}（已扣占用），可能无法再加仓。"
    if cash_req_usd is not None and cash_free_cny is None and cash_free_total_cny is None and cash_free_usd is None and cash_free_usd_est is not None and cash_req_usd > cash_free_usd_est:
        return f"所需担保现金约 ${cash_req_usd:,.0f}，但账户可用担保现金(折算USD)约 ${cash_free_usd_est:,.0f}（已扣占用）；可能无法再加仓，仅供观察。"

    return render_sell_put_comment(row)


def _active_cash_view(row) -> tuple[str, str, float | None, float | None, float | None]:
    def _to_float(value):
        try:
            return float(value) if value is not None and not pd.isna(value) else None
        except Exception:
            return None

    cash_req_cny = _to_float(row.get("cash_required_cny"))
    cash_free_cny = _to_float(row.get("cash_free_cny"))
    cash_free_total_cny = _to_float(row.get("cash_free_total_cny"))
    cash_req_usd = _to_float(row.get("cash_required_usd"))
    cash_free_usd = _to_float(row.get("cash_free_usd"))
    cash_free_usd_est = _to_float(row.get("cash_free_usd_est"))

    if cash_req_cny is not None and cash_free_cny is not None:
        return ("CNY", "base(CNY) 现金余量", cash_req_cny, cash_free_cny, cash_free_cny - cash_req_cny)
    if cash_req_cny is not None and cash_free_total_cny is not None:
        return ("CNY", "总可用折算(CNY)", cash_req_cny, cash_free_total_cny, cash_free_total_cny - cash_req_cny)
    if cash_req_usd is not None and cash_free_usd is not None:
        return ("USD", "账户可用担保现金(USD)", cash_req_usd, cash_free_usd, cash_free_usd - cash_req_usd)
    if cash_req_usd is not None and cash_free_usd_est is not None:
        return ("USD", "账户可用担保现金(折算USD)", cash_req_usd, cash_free_usd_est, cash_free_usd_est - cash_req_usd)
    return ("CNY", "base(CNY) 现金余量", cash_req_cny, cash_free_cny, None)


def render_one(row) -> str:
    symbol = row["symbol"]
    expiration = row["expiration"]
    strike = strike_text(row["strike"])
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
    cash_avail_total_cny = None
    cash_free_total_cny = None
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
        v = row.get('cash_available_total_cny')
        cash_avail_total_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_avail_total_cny = None
    try:
        v = row.get('cash_free_total_cny')
        cash_free_total_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        cash_free_total_cny = None

    headroom = None
    headroom_label = "加仓后余量(估算, CNY)"
    try:
        if cash_req_cny is not None and cash_free_cny is not None:
            headroom = float(cash_free_cny) - float(cash_req_cny)
        elif cash_req_cny is not None and cash_free_total_cny is not None:
            headroom = float(cash_free_total_cny) - float(cash_req_cny)
            headroom_label = "加仓后余量(总折算估算, CNY)"
    except Exception:
        headroom = None

    active_ccy, active_free_label, active_req, active_free, active_headroom = _active_cash_view(row)
    active_symbol = "$" if active_ccy == "USD" else "¥"

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
        f"总现金折算(CNY): {('-' if cash_avail_total_cny is None else '¥' + num(cash_avail_total_cny, 0))}",
        f"总可用折算(扣占用, CNY): {('-' if cash_free_total_cny is None else '¥' + num(cash_free_total_cny, 0))}",
        f"{headroom_label}: {('-' if headroom is None else '¥' + num(headroom, 0))}",
        f"担保现金需求(生效口径, {active_ccy}): {('-' if active_req is None else active_symbol + num(active_req, 0))}",
        f"{active_free_label}: {('-' if active_free is None else active_symbol + num(active_free, 0))}",
        f"加仓后余量(生效口径, {active_ccy}): {('-' if active_headroom is None else active_symbol + num(active_headroom, 0))}",
        "",
        f"净收入({str(row.get('currency') or row.get('option_ccy') or 'N/A').upper()}): {num(row['net_income'])}",
        f"净年化: {pct(row['annualized_net_return_on_cash_basis'])}",
        f"OTM: {pct(row['otm_pct'])}",
        f"风险标签: {row.get('risk_label', '-')}",
        "",
        f"OI / Volume: {int(row.get('open_interest', 0) or 0)} / {int(row.get('volume', 0) or 0)}",
        f"Spread Ratio: {pct(row.get('spread_ratio', 0))}",
        "",
        f"判断: {_render_judgment(row)}",
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Render Sell Put alert text from candidate CSV')
    parser.add_argument('--input', default=None, help='Input CSV path (default: <report-dir>/sell_put_candidates_labeled.csv)')
    parser.add_argument('--report-dir', default='output/reports', help='Report dir for default input/output (default: output/reports)')
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--symbol', default=None)
    parser.add_argument('--output', default=None, help='Output txt path (default: <report-dir>/sell_put_alerts.txt)')
    parser.add_argument('--layered', action='store_true', help='Pick layered alerts by risk label')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    render_sell_put_alerts(
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
