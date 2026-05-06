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

from domain.domain.engine import rank_yield_enhancement_rows
from scripts.io_utils import atomic_write_text
from scripts.report_formatting import num, pct


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
    put_strike = _strike_token(row.get("put_strike"))
    call_strike = _strike_token(row.get("call_strike"))
    option_ccy = str(row.get("option_ccy") or row.get("currency") or "").strip().upper() or "N/A"
    dte = _safe_float(row.get("dte"))
    put_delta = _safe_float(row.get("put_delta"))
    call_ask = _safe_float(row.get("call_ask"))
    call_delta = _safe_float(row.get("call_delta"))
    expected_move = _safe_float(row.get("expected_move"))
    expected_move_iv = _safe_float(row.get("expected_move_iv"))
    scenario_score = _safe_float(row.get("scenario_score"))
    annualized_scenario_score = _safe_float(row.get("annualized_scenario_score"))
    call_candidate_count = _safe_float(row.get("call_candidate_count"))
    candidate_line = None
    if call_candidate_count is not None and call_candidate_count > 1:
        candidate_line = f"Call候选: {int(call_candidate_count)}个"
    return "\n".join(
        [
            f"[收益增强推荐] {symbol} {expiration} {put_strike}P + {call_strike}C",
            "",
            f"DTE: {int(dte) if dte is not None else '-'}",
            f"净权利金({option_ccy}): {num(row.get('net_credit'))}",
            f"场景评分: {('-' if scenario_score is None else pct(scenario_score))}",
            f"场景年化: {('-' if annualized_scenario_score is None else pct(annualized_scenario_score))}",
            f"Put: strike={put_strike} | delta={('-' if put_delta is None else f'{put_delta:.2f}')}",
            f"Call: strike={call_strike} | ask={('-' if call_ask is None else num(call_ask))} | delta={('-' if call_delta is None else f'{call_delta:.2f}')}",
            *( [candidate_line] if candidate_line else [] ),
            f"Expected Move: {('-' if expected_move is None else num(expected_move))} | IV={('-' if expected_move_iv is None else pct(expected_move_iv))}",
            f"组合价差比: {pct(row.get('combo_spread_ratio'))}",
            "",
            "判断: 已按组合收益筛出推荐 Call，可作为该 Sell Put 的收益增强方案。",
        ]
    )


def render_yield_enhancement_alerts(
    *,
    input_path: str | Path | None = None,
    report_dir: str | Path = 'output/reports',
    top: int = 5,
    symbol: str | None = None,
    output_path: str | Path | None = None,
    base_dir: Path | None = None,
) -> str:
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

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
            'yield_enhancement_candidates.csv',
            symbol=symbol,
        )

    if output_path:
        output_file = Path(output_path)
        if not output_file.is_absolute():
            output_file = (base / output_file).resolve()
    else:
        output_file = _default_report_file(
            report_dir_path,
            'yield_enhancement_alerts.txt',
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
    parser = argparse.ArgumentParser(description='Render yield enhancement alert text from candidate CSV')
    parser.add_argument(
        '--input',
        default=None,
        help='Input CSV path (default: <report-dir>/<symbol>_yield_enhancement_candidates.csv when --symbol is set; otherwise <report-dir>/yield_enhancement_candidates.csv)',
    )
    parser.add_argument('--report-dir', default='output/reports', help='Report dir for default input/output (default: output/reports)')
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--symbol', default=None)
    parser.add_argument(
        '--output',
        default=None,
        help='Output txt path (default: <report-dir>/<symbol>_yield_enhancement_alerts.txt when --symbol is set; otherwise <report-dir>/yield_enhancement_alerts.txt)',
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
