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
)
from src.infrastructure.exchange_rates import CurrencyConverter
from domain.domain.symbol_identity import canonical_symbol
from src.application.covered_call_strategy_risk import (
    enrich_and_filter_covered_call_underwriting,
)
from src.application.strategy_policy import SELL_CALL_FAMILY, strategy_semantics_for_side_config
from src.application.report_summaries import summarize_sell_call
from src.application.scan_sell_call import run_sell_call_scan
from src.application.candidate_scanning import (
    evidence_summary_from_decisions,
    project_evidence_scan_status,
)
from domain.domain.sell_call_config import (
    resolve_effective_sell_call_min_strike,
)


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def _empty_sell_call_result(
    *,
    symbol: str,
    symbol_cfg: dict[str, Any],
    status: str,
    reason: str,
) -> dict[str, Any]:
    result = summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
    result["_strategy_status"] = status
    result["_strategy_reason"] = reason
    return result


def run_sell_call_scan_and_summarize(
    *,
    symbol: str,
    symbol_cfg: dict[str, Any],
    cc: dict[str, Any],
    required_data_dir: Path,
    stock: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None = None,
    locked_shares_status: str | None = None,
    locked_shares_unavailable_reason: str | None = None,
    locked_shares_by_symbol: dict[str, int] | None = None,
    locked_shares_unavailable_by_symbol: dict[str, str] | None = None,
    global_sell_call_liquidity: dict[str, Any] | None = None,
    final_candidates_sink_fn: Callable[[str, list[dict[str, Any]]], None] | None = None,
    candidate_decisions_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None,
    required_data_frame: pd.DataFrame | None = None,
    demo_capacity: bool = False,
) -> dict[str, Any]:
    """Run the CC opening policy in memory and summarize it."""
    sell_call_semantics = strategy_semantics_for_side_config(family=SELL_CALL_FAMILY, side_cfg=cc)
    portfolio_source = str(
        (portfolio_ctx or {}).get("portfolio_source_name")
        if isinstance(portfolio_ctx, dict)
        else ""
    ).strip().lower()
    authority = (
        portfolio_ctx.get("capacity_authority")
        if isinstance(portfolio_ctx, dict)
        and isinstance(portfolio_ctx.get("capacity_authority"), dict)
        else {}
    )
    if not demo_capacity and (
        portfolio_source != "futu"
        or str(authority.get("status") or "").strip().lower() != "available"
    ):
        return _empty_sell_call_result(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="unavailable",
            reason="physical_account_capacity_authority_unavailable",
        )
    if not demo_capacity and not stock:
        return _empty_sell_call_result(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="not_applicable",
            reason="stock_context_missing",
        )

    shares_total = 0
    shares_can_sell = 0
    avg_cost = 0.0
    if not demo_capacity:
        try:
            assert stock is not None
            shares_raw = stock.get('shares')
            shares_can_sell_raw = stock.get('can_sell_qty')
            avg_cost_raw = stock.get('avg_cost')
            if shares_raw is None or shares_can_sell_raw is None or avg_cost_raw is None:
                raise ValueError("missing stock context")
            shares_total = int(shares_raw)
            shares_can_sell = int(shares_can_sell_raw)
            avg_cost = float(avg_cost_raw)
        except Exception:
            return _empty_sell_call_result(
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                status="not_applicable",
                reason="stock_context_invalid",
            )

    if not demo_capacity and (
        shares_total <= 0 or shares_can_sell < 0 or avg_cost <= 0
    ):
        return _empty_sell_call_result(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="not_applicable",
            reason="stock_context_non_positive",
        )

    locked = 0
    if not demo_capacity:
        try:
            assert stock is not None
            symbol_key = canonical_symbol(symbol) or str(symbol).upper()
            if (
                str(locked_shares_status or "").strip().lower() != "available"
                or not isinstance(locked_shares_by_symbol, dict)
                or not isinstance(locked_shares_unavailable_by_symbol, dict)
            ):
                reason = str(
                    locked_shares_unavailable_reason
                    or "option_positions_context_unavailable"
                )
                return _empty_sell_call_result(
                    symbol=symbol,
                    symbol_cfg=symbol_cfg,
                    status="unavailable",
                    reason=reason,
                )
            if symbol_key in locked_shares_unavailable_by_symbol:
                reason = str(
                    locked_shares_unavailable_by_symbol.get(symbol_key)
                    or "locked shares unavailable"
                )
                return _empty_sell_call_result(
                    symbol=symbol,
                    symbol_cfg=symbol_cfg,
                    status="unavailable",
                    reason=reason,
                )
            locked = int((locked_shares_by_symbol or {}).get(symbol_key, 0) or 0)
        except Exception:
            return _empty_sell_call_result(
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                status="unavailable",
                reason="share_coverage_calc_failed",
            )
    if not demo_capacity and locked < 0:
        return _empty_sell_call_result(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="unavailable",
            reason="share_coverage_calc_failed",
        )
    if not demo_capacity and locked > min(shares_total, shares_can_sell):
        return _empty_sell_call_result(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            status="unavailable",
            reason="locked_shares_exceed_eligible_underlying",
        )
    candidate_decisions: list[dict[str, Any]] = []

    liquidity = resolve_candidate_liquidity(global_sell_call_liquidity)
    window = resolve_candidate_window(cc, defaults=DEFAULT_SELL_CALL_WINDOW)
    df_cc = run_sell_call_scan(
        symbols=[symbol],
        input_root=required_data_dir,
        avg_cost=float(avg_cost),
        shares=int(shares_total),
        shares_can_sell=int(shares_can_sell),
        shares_locked=int(locked),
        capacity_facts=({"capacity_source": "demo_scenario"} if demo_capacity else {
            "capacity_identity_hash": stock.get("capacity_identity_hash"),
            "futu_account_id": stock.get("futu_account_id"),
            "capacity_trd_env": stock.get("trd_env"),
            "capacity_market": stock.get("market"),
            "capacity_source_observed_at": stock.get("source_observed_at"),
            "capacity_authority_status": stock.get("capacity_authority_status"),
        }),
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        min_strike=(cc.get('min_strike') if demo_capacity else resolve_effective_sell_call_min_strike(
            min_strike=cc.get('min_strike'),
            avg_cost=avg_cost,
            cost_multiplier=cc.get('min_strike_cost_multiplier', 1.02),
        )),
        max_strike=_optional_float(cc, 'max_strike'),
        # Underwriting applies return/income thresholds once, after CNY enrichment.
        min_annualized_net_return=0.0,
        min_strike_cost_multiplier=float(cc.get('min_strike_cost_multiplier', 1.02) or 1.02),
        min_net_income=0.0,
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        strategy_family=sell_call_semantics.strategy_family,
        strategy_profile=sell_call_semantics.scan_strategy_profile,
        calculation_decision_sink_fn=candidate_decisions.extend,
        required_data_frames=(
            {symbol: required_data_frame}
            if required_data_frame is not None
            else None
        ),
        demo_capacity=demo_capacity,
    )

    if not df_cc.empty:
        df_cc = enrich_and_filter_covered_call_underwriting(
            df_labeled=df_cc,
            symbol=symbol,
            sell_call_cfg={
                **cc,
                "max_spread_ratio": liquidity.max_spread_ratio,
            },
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
            decision_sink_fn=candidate_decisions.extend,
        )
    if final_candidates_sink_fn is not None:
        final_candidates_sink_fn(
            "call",
            [dict(item) for item in df_cc.to_dict("records")],
        )
    if candidate_decisions_sink_fn is not None:
        candidate_decisions_sink_fn("call", candidate_decisions)

    summary = summarize_sell_call(df_cc, symbol, symbol_cfg=symbol_cfg)
    evidence = evidence_summary_from_decisions(
        decisions=candidate_decisions,
        accepted_count=len(df_cc),
    )
    summary["_evidence_summary"] = evidence
    status, reason = _evidence_scan_status(
        evidence=evidence,
        candidate_count=len(df_cc),
    )
    summary["_strategy_status"] = status
    summary["_strategy_reason"] = reason
    return summary


def _evidence_scan_status(
    *,
    evidence: dict[str, Any],
    candidate_count: int,
) -> tuple[str, str | None]:
    return project_evidence_scan_status(
        evidence=evidence,
        candidate_count=candidate_count,
    )


def empty_sell_call_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_call(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
