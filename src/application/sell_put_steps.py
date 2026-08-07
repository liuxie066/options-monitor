"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from src.application.report_labels import label_sell_put_candidates
from src.application.report_summaries import summarize_sell_put
from src.application.scan_sell_put import run_sell_put_scan
from src.application.candidate_scanning import evidence_summary_from_decisions
from src.application.sell_put_cash import (
    enrich_sell_put_candidates_with_cash,
)
from src.application.sell_put_strategy_risk import (
    enrich_and_filter_sell_put_underwriting,
    resolve_sell_put_underwriting_config,
)
from src.application.strategy_policy import SELL_PUT_FAMILY, strategy_semantics_for_side_config


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def _enrich_sell_put_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> pd.DataFrame:
    if df_labeled.empty:
        return df_labeled
    return enrich_sell_put_candidates_with_cash(
        df_labeled=df_labeled,
        symbol=symbol,
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
    )


def run_sell_put_scan_and_summarize(
    *,
    py: str,
    base: Path,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    sp: dict[str, Any],
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    global_sell_put_liquidity: dict[str, Any] | None = None,
    run_sell_put: bool = True,
    yield_enhancement_sell_put_cfg: dict[str, Any] | None = None,
    final_candidates_sink_fn: Callable[[str, list[dict[str, Any]]], None] | None = None,
    candidate_decisions_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None,
) -> list[dict[str, Any]]:
    del py, base, top_n, report_dir, timeout_sec, yield_enhancement_sell_put_cfg
    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sp)

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    window = resolve_candidate_window(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
    candidate_decisions: list[dict[str, Any]] = []

    if run_sell_put:
        underwriting_cfg = resolve_sell_put_underwriting_config(sp)
        df_sp_lab = run_sell_put_scan(
            symbols=[sym],
            input_root=required_data_dir,
            output=None,
            min_dte=window.min_dte,
            max_dte=window.max_dte,
            # Underwriting applies return/income thresholds once, after CNY enrichment.
            min_annualized_net_return=0.0,
            min_net_income=0.0,
            min_strike=_optional_float(sp, 'min_strike'),
            max_strike=_optional_float(sp, 'max_strike'),
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=liquidity.max_spread_ratio,
            strategy_family=sell_put_semantics.strategy_family,
            strategy_profile=sell_put_semantics.scan_strategy_profile,
            quiet=True,
            calculation_decision_sink_fn=candidate_decisions.extend,
        )
        df_sp_lab = label_sell_put_candidates(df_sp_lab)
        if not df_sp_lab.empty:
            df_sp_lab = _enrich_sell_put_cash(
                df_labeled=df_sp_lab,
                symbol=symbol,
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
            )
        if not df_sp_lab.empty:
            df_sp_lab = enrich_and_filter_sell_put_underwriting(
                df_labeled=df_sp_lab,
                symbol=symbol,
                sell_put_cfg={
                    **sp,
                    "max_spread_ratio": liquidity.max_spread_ratio,
                },
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                decision_sink_fn=candidate_decisions.extend,
            )
    else:
        df_sp_lab = pd.DataFrame()


    if final_candidates_sink_fn is not None:
        final_candidates_sink_fn(
            "put",
            [dict(item) for item in df_sp_lab.to_dict("records")],
        )
    if candidate_decisions_sink_fn is not None:
        candidate_decisions_sink_fn("put", candidate_decisions)

    summary = summarize_sell_put(df_sp_lab, symbol, symbol_cfg=symbol_cfg)
    evidence = evidence_summary_from_decisions(
        decisions=candidate_decisions,
        accepted_count=len(df_sp_lab),
    )
    summary["_evidence_summary"] = evidence
    status, reason = _evidence_scan_status(
        evidence=evidence,
        candidate_count=len(df_sp_lab),
    )
    summary["_strategy_status"] = status
    summary["_strategy_reason"] = reason
    return [summary]


def _evidence_scan_status(
    *,
    evidence: dict[str, Any],
    candidate_count: int,
) -> tuple[str, str | None]:
    """Project per-scope evidence coverage into a scan status.

    A zero-candidate scope is only a genuine ``no_candidate`` when every
    evaluated contract was either accepted or rejected by policy/ineligibility
    with complete evidence. Any contract that could not even be evaluated
    downgrades the scope to a data-availability status instead of a silent
    empty result.
    """

    if candidate_count > 0:
        return "completed", None
    unavailable = int(evidence.get("evidence_unavailable_count") or 0)
    if unavailable == 0:
        return "completed", "no_candidate"
    evaluated = int(evidence.get("evaluated_contract_count") or 0)
    if evaluated > 0 and unavailable < evaluated:
        return "completed", "partial_data"
    return "unavailable", "data_unavailable"


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
