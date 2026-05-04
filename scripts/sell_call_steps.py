"""Sell-call pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from scripts.exchange_rates import CurrencyConverter
from scripts.trade_symbol_identity import symbol_currency
from scripts.io_utils import safe_read_csv
from scripts.render_sell_call_alerts import render_sell_call_alerts
from scripts.report_summaries import summarize_sell_call
from scripts.scan_sell_call import run_sell_call_scan
from scripts.sell_call_config import resolve_min_annualized_net_premium_return_from_sell_call_cfg


def run_sell_call_scan_and_summarize(
    *,
    py: str,
    base: Path,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict,
    cc: dict,
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    stock: dict | None,
    exchange_rate_converter: CurrencyConverter,
    locked_shares_by_symbol: dict[str, int] | None = None,
    global_sell_call_liquidity: dict | None = None,
    global_sell_call_event_risk: dict | None = None,
) -> dict:
    """Run sell_call scan + (optional) render + summarize.

    Returns the summary row dict (same schema as summarize_sell_call).
    """
    # sell_call avg_cost/shares are sourced from account-scoped portfolio context.
    # The upstream portfolio source may be OpenD or holdings, but the downstream stock schema stays the same.
    if not stock:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    try:
        shares_total = int(stock.get('shares'))
        avg_cost = float(stock.get('avg_cost'))
    except Exception:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    if shares_total <= 0 or avg_cost <= 0:
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    locked = 0
    try:
        if locked_shares_by_symbol and symbol:
            locked = int(locked_shares_by_symbol.get(str(symbol).upper(), 0) or 0)
    except Exception:
        locked = 0
    shares_available_for_cover = max(0, int(shares_total) - int(locked))

    symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
    min_annualized = resolve_min_annualized_net_premium_return_from_sell_call_cfg(
        sell_call_cfg=cc,
        source_prefix=f'{symbol}.sell_call',
    )
    liquidity = resolve_candidate_liquidity(global_sell_call_liquidity)
    event_risk = resolve_event_risk_config(global_sell_call_event_risk)
    window = resolve_candidate_window(cc, defaults=DEFAULT_SELL_CALL_WINDOW)

    # Config min_net_income is always CNY. The scanners expect option-native
    # currency thresholds (USD for US symbols, HKD for HK symbols).
    global_min_net_income = float((global_sell_call_liquidity or {}).get('min_net_income', 0.0) or 0.0)
    min_net_income_cny = float(cc.get('min_net_income', global_min_net_income) or 0.0)
    min_net_income_native = 0.0
    if min_net_income_cny > 0:
        min_net_income_native = (
            exchange_rate_converter.cny_to_native(
                min_net_income_cny,
                native_ccy=(symbol_currency(symbol) or 'USD'),
            )
            or 0.0
        )

    run_sell_call_scan(
        symbols=[symbol],
        input_root=required_data_dir,
        output=symbol_cc,
        avg_cost=float(avg_cost),
        shares=int(shares_total),
        shares_locked=int(locked),
        shares_available_for_cover=int(shares_available_for_cover),
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        min_strike=(float(cc.get('min_strike')) if cc.get('min_strike') is not None else None),
        max_strike=(float(cc.get('max_strike')) if cc.get('max_strike') is not None else None),
        min_annualized_net_return=min_annualized,
        min_net_income=float(min_net_income_native),
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        event_risk_cfg=event_risk,
        quiet=bool(is_scheduled),
    )

    df_cc = safe_read_csv(symbol_cc)
    if not is_scheduled:
        render_sell_call_alerts(
            input_path=report_dir / f'{symbol_lower}_sell_call_candidates.csv',
            symbol=symbol,
            top=int(top_n),
            layered=True,
            output_path=report_dir / f'{symbol_lower}_sell_call_alerts.txt',
            base_dir=base,
        )

    return summarize_sell_call(df_cc, symbol, symbol_cfg=symbol_cfg)


def empty_sell_call_summary(symbol: str, *, symbol_cfg: dict) -> dict:
    return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
