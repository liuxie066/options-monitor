from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.engine import rank_combo_yield_rows
from src.infrastructure.io_utils import atomic_write_text
from src.application.report_formatting import num, pct


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


def render_one(row: pd.Series) -> str:
    symbol = str(row.get("symbol") or "-")
    expiration = str(row.get("expiration") or "-")
    put_strike = _strike_token(row.get("put_strike"))
    call_strike = _strike_token(row.get("call_strike"))
    option_ccy = str(row.get("option_ccy") or row.get("currency") or "").strip().upper() or "N/A"
    dte = _safe_float(row.get("dte"))
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
    derived = str(row.get("derived_from_sell_put_strategy") or "").strip()
    policy_line = f"Funding Put: {derived}" if derived else None
    upside_lift = _safe_float(row.get("upside_lift"))
    upside_lift_to_call_cost = _safe_float(row.get("upside_lift_to_call_cost"))
    upside_lift_to_put_credit = _safe_float(row.get("upside_lift_to_put_credit"))
    call_candidate_count = _safe_float(row.get("call_candidate_count"))
    candidate_line = None
    if call_candidate_count is not None and call_candidate_count > 1:
        candidate_line = f"Call候选: {int(call_candidate_count)}个"
    return "\n".join(
        [
            f"[组合收益推荐] {symbol} {expiration} {put_strike}P + {call_strike}C",
            "",
            f"DTE: {int(dte) if dte is not None else '-'}",
            *([policy_line] if policy_line else []),
            f"净权利金({option_ccy}): {num(row.get('net_credit'))}",
            f"净权利金年化: {('-' if annualized_net_credit_yield is None else pct(annualized_net_credit_yield))}",
            f"资金覆盖: Call成本/Put权利金={('-' if call_cost_to_put_credit is None else pct(call_cost_to_put_credit))} | 净权利金保留={('-' if net_credit_retention is None else pct(net_credit_retention))}",
            f"上行弹性: 潜在收益={('-' if upside_lift is None else num(upside_lift))} | 成本倍数={('-' if upside_lift_to_call_cost is None else f'{upside_lift_to_call_cost:.2f}x')} | 权利金倍数={('-' if upside_lift_to_put_credit is None else f'{upside_lift_to_put_credit:.2f}x')}",
            f"场景评分: {('-' if scenario_score is None else pct(scenario_score))}",
            f"场景年化: {('-' if annualized_scenario_score is None else pct(annualized_scenario_score))}",
            f"Put: strike={put_strike} | bid={('-' if put_bid is None else num(put_bid))} | delta={('-' if put_delta is None else f'{put_delta:.2f}')}",
            f"Call: strike={call_strike} | ask={('-' if call_ask is None else num(call_ask))} | delta={('-' if call_delta is None else f'{call_delta:.2f}')}",
            *( [candidate_line] if candidate_line else [] ),
            f"Expected Move: {('-' if expected_move is None else num(expected_move))} | IV={('-' if expected_move_iv is None else pct(expected_move_iv))}",
            f"组合价差比: {pct(row.get('combo_spread_ratio'))}",
            "",
            "判断: 已按组合收益筛出推荐 Call，可作为 Combo Yield 组合方案。",
        ]
    )


def render_combo_yield_alerts(
    *,
    candidates: pd.DataFrame | list[dict[str, Any]],
    top: int = 5,
    output_path: str | Path,
) -> str:
    output_file = Path(output_path).resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    df = candidates.copy() if isinstance(candidates, pd.DataFrame) else pd.DataFrame(candidates)

    if df.empty:
        text = "无候选提醒。"
        atomic_write_text(output_file, text)
        return text

    ranked = rank_combo_yield_rows(df.to_dict("records"))
    top_df = pd.DataFrame(ranked[: int(top)]) if ranked else pd.DataFrame()
    if top_df.empty:
        text = "无候选提醒。"
        atomic_write_text(output_file, text)
        return text

    blocks = [render_one(row) for row in top_df.to_dict("records")]
    text = "\n\n" + ("\n\n".join(blocks)) + "\n"
    atomic_write_text(output_file, text)
    return text
