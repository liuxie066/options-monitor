"""Sell-call pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from domain.domain.symbol_identity import canonical_symbol
from src.infrastructure.io_utils import safe_read_csv
from src.application.candidate_underwriting_decisions import (
    apply_insurance_underwriting_to_all_decisions,
)
from src.application.covered_call_strategy_risk import (
    enrich_and_filter_covered_call_underwriting,
    resolve_covered_call_underwriting_config,
)
from src.application.strategy_policy import SELL_CALL_FAMILY, strategy_semantics_for_side_config
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
)
from domain.domain.strategy_vocab import STRATEGY_COVERED_CALL, strategy_display_name


COVERED_CALL_TRACE_LABEL = strategy_display_name(STRATEGY_COVERED_CALL).lower()


def _append_share_coverage_trace(
    *,
    output_path: Path,
    symbol: str,
    status: str,
    rule: str,
    message: str,
    metric_value: Any = None,
    threshold: Any = None,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
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
                strategy_family=strategy_family,
                strategy_profile=strategy_profile,
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


def _empty_sell_call_result(
    *,
    report_dir: Path,
    symbol_lower: str,
    symbol: str,
    symbol_cfg: dict[str, Any],
    status: str,
    reason: str,
) -> dict[str, Any]:
    materialize_empty_sell_call_artifacts(
        report_dir=report_dir,
        symbol_lower=symbol_lower,
    )
    result = summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
    result["_strategy_status"] = status
    result["_strategy_reason"] = reason
    return result


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
    portfolio_ctx: dict[str, Any] | None = None,
    locked_shares_status: str | None = None,
    locked_shares_unavailable_reason: str | None = None,
    locked_shares_by_symbol: dict[str, int] | None = None,
    locked_shares_unavailable_by_symbol: dict[str, str] | None = None,
    global_sell_call_liquidity: dict[str, Any] | None = None,
    global_sell_call_event_risk: dict[str, Any] | None = None,
    risk_policy_version: str | None = None,
    quote_snapshot_id: str | None = None,
    all_decisions_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Run sell_call scan + (optional) render + summarize.

    Returns the summary row dict (same schema as summarize_sell_call).
    """
    symbol_cc = report_dir / f'{symbol_lower}_sell_call_candidates.csv'
    sell_call_semantics = strategy_semantics_for_side_config(family=SELL_CALL_FAMILY, side_cfg=cc)
    # sell_call avg_cost/shares are sourced from account-scoped portfolio context.
    # The upstream portfolio source may be OpenD or holdings, but the downstream stock schema stays the same.
    if not stock:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="not_applicable",
            rule="stock_context_missing",
            message=f"{COVERED_CALL_TRACE_LABEL} stock context missing",
            strategy_family=sell_call_semantics.strategy_family,
            strategy_profile=sell_call_semantics.scan_strategy_profile,
        )
        return _empty_sell_call_result(
            report_dir=report_dir,
            symbol_lower=symbol_lower,
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="not_applicable",
            reason="stock_context_missing",
        )

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
            message=f"{COVERED_CALL_TRACE_LABEL} stock context invalid",
            strategy_family=sell_call_semantics.strategy_family,
            strategy_profile=sell_call_semantics.scan_strategy_profile,
        )
        return _empty_sell_call_result(
            report_dir=report_dir,
            symbol_lower=symbol_lower,
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="not_applicable",
            reason="stock_context_invalid",
        )

    if shares_total <= 0 or avg_cost <= 0:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="not_applicable",
            rule="stock_context_non_positive",
            message=f"{COVERED_CALL_TRACE_LABEL} shares or avg_cost non-positive",
            metric_value=shares_total,
            threshold=1,
            strategy_family=sell_call_semantics.strategy_family,
            strategy_profile=sell_call_semantics.scan_strategy_profile,
            config_values={"avg_cost": avg_cost},
        )
        return _empty_sell_call_result(
            report_dir=report_dir,
            symbol_lower=symbol_lower,
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="not_applicable",
            reason="stock_context_non_positive",
        )

    locked = 0
    try:
        symbol_key = canonical_symbol(symbol) or str(symbol).upper()
        if str(locked_shares_status or "available").strip().lower() != "available":
            reason = str(
                locked_shares_unavailable_reason
                or "option_positions_context_unavailable"
            )
            _append_share_coverage_trace(
                output_path=symbol_cc,
                symbol=symbol,
                status="unavailable",
                rule="locked_shares_context_unavailable",
                message=reason,
                strategy_family=sell_call_semantics.strategy_family,
                strategy_profile=sell_call_semantics.scan_strategy_profile,
            )
            return _empty_sell_call_result(
                report_dir=report_dir,
                symbol_lower=symbol_lower,
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                status="unavailable",
                reason=reason,
            )
        if locked_shares_unavailable_by_symbol and symbol_key in locked_shares_unavailable_by_symbol:
            reason = str(locked_shares_unavailable_by_symbol.get(symbol_key) or "locked shares unavailable")
            _append_share_coverage_trace(
                output_path=symbol_cc,
                symbol=symbol,
                status="unavailable",
                rule="locked_shares_unavailable",
                message=reason,
                strategy_family=sell_call_semantics.strategy_family,
                strategy_profile=sell_call_semantics.scan_strategy_profile,
            )
            return _empty_sell_call_result(
                report_dir=report_dir,
                symbol_lower=symbol_lower,
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                status="unavailable",
                reason=reason,
            )
        if locked_shares_by_symbol and symbol:
            locked = int(locked_shares_by_symbol.get(symbol_key, 0) or 0)
    except Exception:
        _append_share_coverage_trace(
            output_path=symbol_cc,
            symbol=symbol,
            status="post_filtered",
            rule="share_coverage_calc_failed",
            message=f"{COVERED_CALL_TRACE_LABEL} share coverage calculation failed",
            strategy_family=sell_call_semantics.strategy_family,
            strategy_profile=sell_call_semantics.scan_strategy_profile,
        )
        return _empty_sell_call_result(
            report_dir=report_dir,
            symbol_lower=symbol_lower,
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="unavailable",
            reason="share_coverage_calc_failed",
        )
    shares_available_for_cover = max(0, int(shares_total) - int(locked))

    liquidity = resolve_candidate_liquidity(global_sell_call_liquidity)
    event_risk = resolve_event_risk_config(global_sell_call_event_risk)
    window = resolve_candidate_window(cc, defaults=DEFAULT_SELL_CALL_WINDOW)
    underwriting_cfg = resolve_covered_call_underwriting_config(cc)
    scan_decisions_sink = all_decisions_sink_fn
    if all_decisions_sink_fn is not None and underwriting_cfg.enabled:

        def _underwritten_sink(rows: list[dict[str, Any]]) -> None:
            all_decisions_sink_fn(
                apply_insurance_underwriting_to_all_decisions(
                    rows,
                    mode="call",
                    cfg=underwriting_cfg,
                    exchange_rate_converter=exchange_rate_converter,
                )
            )

        scan_decisions_sink = _underwritten_sink

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
        # Underwriting applies return/income thresholds once, after CNY enrichment.
        min_annualized_net_return=0.0,
        min_strike_cost_multiplier=float(cc.get('min_strike_cost_multiplier', 1.0) or 1.0),
        min_net_income=0.0,
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        event_risk_cfg=event_risk,
        score_weights=None,
        strategy_family=sell_call_semantics.strategy_family,
        strategy_profile=sell_call_semantics.scan_strategy_profile,
        quiet=bool(is_scheduled),
        risk_policy_version=risk_policy_version,
        quote_snapshot_id=quote_snapshot_id,
        all_decisions_sink_fn=scan_decisions_sink,
    )

    df_cc = safe_read_csv(symbol_cc)
    if not df_cc.empty:
        df_cc = enrich_and_filter_covered_call_underwriting(
            df_labeled=df_cc,
            symbol=symbol,
            sell_call_cfg=cc,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
            out_path=symbol_cc,
        )
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


def materialize_empty_sell_call_artifacts(*, report_dir: Path, symbol_lower: str) -> None:
    """Replace current Covered Call outputs with explicit empty artifacts."""

    report_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame().to_csv(
        (report_dir / f"{symbol_lower}_sell_call_candidates.csv").resolve(),
        index=False,
    )
    (report_dir / f"{symbol_lower}_sell_call_alerts.txt").resolve().write_text(
        "",
        encoding="utf-8",
    )


def empty_sell_call_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
