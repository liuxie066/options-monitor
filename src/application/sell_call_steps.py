"""Sell-call pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.infrastructure.io_utils import safe_read_csv
from src.application.render_sell_call_alerts import render_sell_call_alerts
from src.application.report_summaries import summarize_sell_call
from src.application.scan_sell_call import run_sell_call_scan
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from domain.domain.sell_call_config import (
    resolve_effective_sell_call_min_strike,
    resolve_min_annualized_net_premium_return_from_sell_call_cfg,
)


def _append_share_coverage_trace(
    *,
    output_path: Path,
    symbol: str,
    status: str,
    rule: str,
    message: str,
    metric_value: Any = None,
    threshold: Any = None,
    config_values: dict[str, Any] | None = None,
) -> None:
    scope = infer_trace_scope_from_path(output_path)
    append_candidate_filter_trace_rows(
        candidate_trace_path_for_output(output_path),
        [
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account"),
                symbol=symbol,
                function="share_coverage",
                mode="call",
                status=status,
                stage="post_filter",
                rule=rule,
                metric_value=metric_value,
                threshold=threshold,
                message=message,
                evidence_path=output_path.name,
                config_values=config_values or {},
            )
        ],
    )


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def run_sell_call_scan_and_summarize(
    *,
    py: str,
    base: Path,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    cc: dict[str, Any],
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    stock: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    locked_shares_by_symbol: dict[str, int] | None = None,
    locked_shares_unavailable_by_symbol: dict[str, str] | None = None,
    global_sell_call_liquidity: dict[str, Any] | None = None,
    global_sell_call_event_risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run sell_call scan + (optional) render + summarize.

    Returns the summary row dict (same schema as summarize_sell_call).
    """
    symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
    # sell_call avg_cost/shares are sourced from account-scoped portfolio context.
    # The upstream portfolio source may be OpenD or holdings, but the downstream stock schema stays the same.
    if not stock:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="not_applicable",
            rule="stock_context_missing",
            message="sell call stock context missing",
        )
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    try:
        shares_raw = stock.get('shares')
        avg_cost_raw = stock.get('avg_cost')
        if shares_raw is None or avg_cost_raw is None:
            raise ValueError("missing stock context")
        shares_total = int(shares_raw)
        avg_cost = float(avg_cost_raw)
    except Exception:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="not_applicable",
            rule="stock_context_invalid",
            message="sell call stock context invalid",
        )
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    if shares_total <= 0 or avg_cost <= 0:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="not_applicable",
            rule="stock_context_non_positive",
            message="sell call shares or avg_cost non-positive",
            metric_value=shares_total,
            threshold=1,
            config_values={"avg_cost": avg_cost},
        )
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)

    locked = 0
    try:
        symbol_key = canonical_symbol(symbol) or str(symbol).upper()
        if locked_shares_unavailable_by_symbol and symbol_key in locked_shares_unavailable_by_symbol:
            _append_share_coverage_trace(
                output_path=symbol_cc,
                symbol=symbol,
                status="post_filtered",
                rule="locked_shares_unavailable",
                message=str(locked_shares_unavailable_by_symbol.get(symbol_key) or "locked shares unavailable"),
            )
            return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
        if locked_shares_by_symbol and symbol:
            locked = int(locked_shares_by_symbol.get(symbol_key, 0) or 0)
    except Exception:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="post_filtered",
            rule="share_coverage_calc_failed",
            message="sell call share coverage calculation failed",
        )
        return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
    shares_available_for_cover = max(0, int(shares_total) - int(locked))

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
        native_ccy = symbol_currency(symbol)
        if not native_ccy:
            return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
        converted_min_income = exchange_rate_converter.cny_to_native(
            min_net_income_cny,
            native_ccy=native_ccy,
        )
        if converted_min_income is None:
            return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
        min_net_income_native = float(converted_min_income)

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
        min_strike=resolve_effective_sell_call_min_strike(
            min_strike=cc.get('min_strike'),
            avg_cost=avg_cost,
            cost_multiplier=cc.get('min_strike_cost_multiplier', 1.0),
        ),
        max_strike=_optional_float(cc, 'max_strike'),
        min_annualized_net_return=min_annualized,
        min_strike_cost_multiplier=float(cc.get('min_strike_cost_multiplier', 1.0) or 1.0),
        min_net_income=float(min_net_income_native),
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        event_risk_cfg=event_risk,
        score_weights=cc.get('score_weights'),
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


def empty_sell_call_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
