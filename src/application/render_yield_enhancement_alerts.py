#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import pandas as pd
from pandas.errors import EmptyDataError

from domain.domain.engine import rank_yield_enhancement_rows
from src.infrastructure.io_utils import atomic_write_text
from src.application.report_formatting import num, pct


COMBO_YIELD_CANDIDATES_BASENAME = "combo_yield_candidates.csv"
COMBO_YIELD_ALERTS_BASENAME = "combo_yield_alerts.txt"
LEGACY_YIELD_ENHANCEMENT_CANDIDATES_BASENAME = "yield_enhancement_candidates.csv"


def _safe_float(value) -> float | None:
    try:
        return float(value) if value is not None and not pd.isna(value) else None
    except Exception:
        return None


def _strike_token(value) -> str:
    number = _safe_float(value)
    if number is None:
        return "-"
    if float(number).is_integer():
        return str(int(number))
    return str(number)


def _default_report_file(report_dir_path: Path, basename: str, *, symbol: str | None = None) -> Path:
    if symbol:
        return (report_dir_path / f"{symbol.lower()}_{basename}").resolve()
    return (report_dir_path / basename).resolve()


def render_one(row: pd.Series) -> str:
    symbol = str(row.get("symbol") or "-")
    expiration = str(row.get("expiration") or "-")
    expiry_structure = str(row.get("expiry_structure") or "same_expiry").strip().lower()
    put_expiration = str(row.get("put_expiration") or expiration)
    call_expiration = str(row.get("call_expiration") or expiration)
    put_strike = _strike_token(row.get("put_strike"))
    call_strike = _strike_token(row.get("call_strike"))
    option_ccy = str(row.get("option_ccy") or row.get("currency") or "").strip().upper() or "N/A"
    dte = _safe_float(row.get("dte"))
    put_dte = _safe_float(row.get("put_dte"))
    call_dte = _safe_float(row.get("call_dte"))
    put_delta = _safe_float(row.get("put_delta"))
    put_bid = _safe_float(row.get("put_bid"))
    if put_bid is None:
        put_bid = _safe_float(row.get("bid"))
    call_ask = _safe_float(row.get("call_ask"))
    call_delta = _safe_float(row.get("call_delta"))
    expected_move = _safe_float(row.get("expected_move"))
    expected_move_iv = _safe_float(row.get("expected_move_iv"))
    scenario_score = _safe_float(row.get("scenario_score"))
    annualized_scenario_score = _safe_float(row.get("annualized_scenario_score"))
    annualized_net_credit_yield = _safe_float(row.get("annualized_net_credit_yield"))
    call_cost_to_put_credit = _safe_float(row.get("call_cost_to_put_credit"))
    net_credit_retention = _safe_float(row.get("net_credit_retention"))
    mode = str(row.get("yield_enhancement_mode") or "").strip()
    derived = str(row.get("derived_from_sell_put_strategy") or "").strip()
    mode_line = None
    if mode:
        mode_line = f"策略模式: {mode}" + (f" | derived_from={derived}" if derived else "")
    upside_lift = _safe_float(row.get("upside_lift"))
    upside_lift_to_call_cost = _safe_float(row.get("upside_lift_to_call_cost"))
    upside_lift_to_put_credit = _safe_float(row.get("upside_lift_to_put_credit"))
    call_candidate_count = _safe_float(row.get("call_candidate_count"))
    candidate_line = None
    if call_candidate_count is not None and call_candidate_count > 1:
        candidate_line = f"Call候选: {int(call_candidate_count)}个"
    diagonal = expiry_structure == "diagonal"
    title = (
        f"[组合收益推荐] {symbol} Put {put_expiration} {put_strike}P + Call {call_expiration} {call_strike}C"
        if diagonal
        else f"[组合收益推荐] {symbol} {expiration} {put_strike}P + {call_strike}C"
    )
    dte_line = (
        f"DTE: Put={int(put_dte) if put_dte is not None else '-'} | Call={int(call_dte) if call_dte is not None else '-'}"
        if diagonal
        else f"DTE: {int(dte) if dte is not None else '-'}"
    )
    terminal_line = (
        "到期终值指标: 不可评估（不预测 Put 到期时 Call 剩余价值）"
        if diagonal
        else None
    )
    return "\n".join(
        [
            title,
            "",
            dte_line,
            *([mode_line] if mode_line else []),
            f"净权利金({option_ccy}): {num(row.get('net_credit'))}",
            f"净权利金年化: {('-' if annualized_net_credit_yield is None else pct(annualized_net_credit_yield))}",
            f"资金覆盖: Call成本/Put权利金={('-' if call_cost_to_put_credit is None else pct(call_cost_to_put_credit))} | 净权利金保留={('-' if net_credit_retention is None else pct(net_credit_retention))}",
            *([] if diagonal else [
                f"上行弹性: 潜在收益={('-' if upside_lift is None else num(upside_lift))} | 成本倍数={('-' if upside_lift_to_call_cost is None else f'{upside_lift_to_call_cost:.2f}x')} | 权利金倍数={('-' if upside_lift_to_put_credit is None else f'{upside_lift_to_put_credit:.2f}x')}",
                f"场景评分: {('-' if scenario_score is None else pct(scenario_score))}",
                f"场景年化: {('-' if annualized_scenario_score is None else pct(annualized_scenario_score))}",
            ]),
            f"Put: strike={put_strike} | bid={('-' if put_bid is None else num(put_bid))} | delta={('-' if put_delta is None else f'{put_delta:.2f}')}",
            f"Call: strike={call_strike} | ask={('-' if call_ask is None else num(call_ask))} | delta={('-' if call_delta is None else f'{call_delta:.2f}')}",
            *( [candidate_line] if candidate_line else [] ),
            *([terminal_line] if terminal_line else [
                f"Expected Move: {('-' if expected_move is None else num(expected_move))} | IV={('-' if expected_move_iv is None else pct(expected_move_iv))}",
            ]),
            f"组合价差比: {pct(row.get('combo_spread_ratio'))}",
            "",
            "判断: 已按组合收益筛出推荐 Call，可作为 Combo Yield 组合方案。",
        ]
    )


def render_yield_enhancement_alerts(
    *,
    input_path: str | Path | None = None,
    report_dir: str | Path = 'output_shared/reports',
    top: int = 5,
    symbol: str | None = None,
    output_path: str | Path | None = None,
    base_dir: Path | None = None,
) -> str:
    base = (base_dir or Path(__file__).resolve().parents[2]).resolve()

    report_dir_path = Path(report_dir)
    if not report_dir_path.is_absolute():
        report_dir_path = (base / report_dir_path).resolve()

    if input_path:
        input_file = Path(input_path)
        if not input_file.is_absolute():
            input_file = (base / input_file).resolve()
    else:
        input_file = _default_report_file(
            report_dir_path,
            COMBO_YIELD_CANDIDATES_BASENAME,
            symbol=symbol,
        )
        legacy_input_file = _default_report_file(
            report_dir_path,
            LEGACY_YIELD_ENHANCEMENT_CANDIDATES_BASENAME,
            symbol=symbol,
        )
        if not input_file.exists() and legacy_input_file.exists():
            input_file = legacy_input_file

    if output_path:
        output_file = Path(output_path)
        if not output_file.is_absolute():
            output_file = (base / output_file).resolve()
    else:
        output_file = _default_report_file(
            report_dir_path,
            COMBO_YIELD_ALERTS_BASENAME,
            symbol=symbol,
        )

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
        return text

    ranked = rank_yield_enhancement_rows(df.to_dict("records"))
    top_df = pd.DataFrame(ranked[: int(top)]) if ranked else pd.DataFrame()
    if top_df.empty:
        text = "无候选提醒。"
        atomic_write_text(output_file, text)
        return text

    blocks = [render_one(row) for _, row in top_df.iterrows()]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    atomic_write_text(output_file, text)
    print(text)
    print(f"[DONE] alerts -> {output_file}")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Render Combo Yield alert text from candidate CSV')
    parser.add_argument(
        '--input',
        default=None,
        help='Input CSV path (default: <report-dir>/<symbol>_combo_yield_candidates.csv when --symbol is set; otherwise <report-dir>/combo_yield_candidates.csv)',
    )
    parser.add_argument('--report-dir', default='output_shared/reports', help='Report dir for default input/output (default: output_shared/reports)')
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--symbol', default=None)
    parser.add_argument(
        '--output',
        default=None,
        help='Output txt path (default: <report-dir>/<symbol>_combo_yield_alerts.txt when --symbol is set; otherwise <report-dir>/combo_yield_alerts.txt)',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    render_yield_enhancement_alerts(
        input_path=args.input,
        report_dir=args.report_dir,
        top=args.top,
        symbol=args.symbol,
        output_path=args.output,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
