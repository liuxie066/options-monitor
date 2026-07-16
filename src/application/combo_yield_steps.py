"""Combo Yield opening-strategy orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.domain.candidate_defaults import CandidateLiquidityDefaults, CandidateWindowDefaults
from domain.domain.insurance_underwriting import evaluate_event_risk_candidate
from domain.domain.sell_put_config import resolve_min_annualized_net_return
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts
from src.application.report_labels import add_sell_put_labels
from src.application.report_summaries import summarize_yield_enhancement
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_call_helper import (
    attach_best_linked_calls,
    build_yield_enhancement_rank_shadow,
    find_sell_put_yield_enhancement_pairs,
    get_yield_enhancement_pair_diagnostics,
    select_best_yield_enhancement_pairs,
)
from src.application.yield_enhancement_config import (
    YieldEnhancementPolicy,
    wants_yield_enhancement_inline,
    wants_yield_enhancement_separate,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from src.infrastructure.io_utils import safe_read_csv


COMBO_YIELD_FAMILY = "combo_yield"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComboYieldResult:
    recommended_pairs: pd.DataFrame
    separate_enabled: bool
    candidates_path: Path
    alerts_path: Path


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def _empty_result(*, report_dir: Path, symbol_lower: str) -> ComboYieldResult:
    return ComboYieldResult(
        recommended_pairs=pd.DataFrame(),
        separate_enabled=False,
        candidates_path=(report_dir / f"{symbol_lower}_combo_yield_candidates.csv").resolve(),
        alerts_path=(report_dir / f"{symbol_lower}_combo_yield_alerts.txt").resolve(),
    )


def _filter_combo_yield_event_risk(
    df: pd.DataFrame,
    *,
    symbol: str,
    sell_put_cfg: dict[str, Any],
    policy: YieldEnhancementPolicy,
    out_path: Path,
) -> pd.DataFrame:
    if df.empty:
        return df

    reject_event_risk = bool(sell_put_cfg.get("reject_event_risk", True))
    event_source_fail_closed = bool(sell_put_cfg.get("event_source_fail_closed", True))
    scope = infer_trace_scope_from_path(out_path)
    keep_mask: list[bool] = []
    trace_rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        decision = evaluate_event_risk_candidate(
            row.to_dict(),
            reject_event_risk=reject_event_risk,
            event_source_fail_closed=event_source_fail_closed,
        )
        keep_mask.append(bool(decision["accepted"]))
        if decision["accepted"]:
            continue
        trace_rows.append(
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account"),
                symbol=row.get("symbol") or symbol,
                function=COMBO_YIELD_FAMILY,
                mode=policy.mode,
                strategy_family=COMBO_YIELD_FAMILY,
                strategy_profile=policy.mode,
                status="rejected",
                stage="combo_event_risk",
                rule=decision["rule"],
                metric_value=decision.get("metric_value"),
                threshold=decision.get("threshold"),
                contract_symbol=row.get("contract_symbol"),
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=decision.get("message") or "combo yield event risk filter",
                evidence_path=out_path.name,
                config_values={
                    **policy.to_fields(),
                    "reject_event_risk": reject_event_risk,
                    "event_source_fail_closed": event_source_fail_closed,
                },
                replay_fields=row.to_dict(),
            )
        )

    filtered = df.loc[keep_mask].copy()
    try:
        filtered.to_csv(out_path, index=False)
    except Exception as exc:
        raise RuntimeError(f"failed to persist combo yield event-filtered put universe: {out_path}") from exc
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(out_path), trace_rows)
    return filtered


def run_combo_yield_scan_and_summarize(
    *,
    base: Path,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    yield_enhancement_cfg: dict[str, Any],
    yield_sp: dict[str, Any],
    yield_enhancement_policy: YieldEnhancementPolicy,
    df_sell_put_labeled: pd.DataFrame,
    sell_put_labeled_path: Path,
    required_data_dir: Path,
    report_dir: Path,
    yield_window: CandidateWindowDefaults,
    liquidity: CandidateLiquidityDefaults,
    event_risk: dict[str, Any],
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    top_n: int,
    is_scheduled: bool,
    run_put_scan_fn: Callable[..., Any] = run_sell_put_scan,
    label_put_candidates_fn: Callable[..., Any] = add_sell_put_labels,
    find_pairs_fn: Callable[..., pd.DataFrame] = find_sell_put_yield_enhancement_pairs,
    select_pairs_fn: Callable[[pd.DataFrame], pd.DataFrame] = select_best_yield_enhancement_pairs,
    attach_calls_fn: Callable[..., pd.DataFrame] = attach_best_linked_calls,
    render_alerts_fn: Callable[..., str] = render_yield_enhancement_alerts,
    cash_filter_put_candidates_fn: Callable[..., pd.DataFrame] | None = None,
) -> tuple[ComboYieldResult, dict[str, Any] | None]:
    """Run the Combo Yield scan and return an optional summary row."""

    result = _empty_result(report_dir=report_dir, symbol_lower=symbol_lower)
    if not bool(yield_enhancement_policy.enabled):
        return result, None

    symbol_yield_put_universe = (report_dir / f"{symbol_lower}_combo_yield_put_universe.csv").resolve()
    symbol_yield_put_universe_labeled = (
        report_dir / f"{symbol_lower}_combo_yield_put_universe_labeled.csv"
    ).resolve()
    symbol_yield_put_universe_cash_filtered = (
        report_dir / f"{symbol_lower}_combo_yield_put_universe_cash_filtered.csv"
    ).resolve()
    funding_put_min_annualized_return = resolve_min_annualized_net_return(
        symbol_cfg={"sell_put": yield_sp},
    )

    run_put_scan_fn(
        symbols=[sym],
        input_root=required_data_dir,
        output=symbol_yield_put_universe,
        min_dte=yield_window.min_dte,
        max_dte=yield_window.max_dte,
        min_annualized_net_return=funding_put_min_annualized_return,
        min_net_income=0.0,
        min_strike=_optional_float(yield_sp, "min_strike"),
        max_strike=_optional_float(yield_sp, "max_strike"),
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        event_risk_cfg=event_risk,
        score_weights=yield_sp.get("score_weights"),
        strategy_family=COMBO_YIELD_FAMILY,
        strategy_profile=yield_enhancement_policy.mode,
        quiet=True,
    )
    label_put_candidates_fn(base, symbol_yield_put_universe, symbol_yield_put_universe_labeled)
    df_yield_put_universe = safe_read_csv(symbol_yield_put_universe_labeled)
    if not df_yield_put_universe.empty:
        df_yield_put_universe["funding_put_eligible"] = True
        df_yield_put_universe["funding_put_min_annualized_return"] = funding_put_min_annualized_return
        df_yield_put_universe["put_only_annualized_net_return"] = df_yield_put_universe.get(
            "annualized_net_return_on_cash_basis"
        )
    df_yield_put_event_filtered = _filter_combo_yield_event_risk(
        df_yield_put_universe,
        symbol=symbol,
        sell_put_cfg=yield_sp,
        policy=yield_enhancement_policy,
        out_path=symbol_yield_put_universe_labeled,
    )
    df_yield_put_candidates_for_pairs = df_yield_put_event_filtered
    if cash_filter_put_candidates_fn is not None and not df_yield_put_event_filtered.empty:
        df_yield_put_candidates_for_pairs = cash_filter_put_candidates_fn(
            df_labeled=df_yield_put_event_filtered.copy(),
            symbol=symbol,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
            out_path=symbol_yield_put_universe_cash_filtered,
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=yield_enhancement_policy.mode,
        )

    raw_yield_pairs_df = find_pairs_fn(
        df_candidates=df_yield_put_candidates_for_pairs,
        symbol=symbol,
        input_root=required_data_dir,
        yield_enhancement_cfg=yield_enhancement_cfg,
        sell_put_cfg=yield_sp,
        global_yield_enhancement_liquidity=(symbol_cfg.get("_global_yield_enhancement_liquidity") or {}),
        output_path=None,
    )
    scope = infer_trace_scope_from_path(result.candidates_path)
    pair_diagnostics_path = (report_dir / f"{symbol_lower}_combo_yield_pair_diagnostics.csv").resolve()
    try:
        pair_diagnostics = get_yield_enhancement_pair_diagnostics(raw_yield_pairs_df)
        pair_diagnostics["run_id"] = scope.get("run_id")
        pair_diagnostics["account"] = scope.get("account")
        pair_diagnostics.to_csv(pair_diagnostics_path, index=False)
    except Exception as exc:
        log.warning("combo_yield_steps: failed to write pair diagnostics for %s: %s", symbol, exc)

    recommended_yield_pairs_df = select_pairs_fn(raw_yield_pairs_df)
    rank_shadow_path = (report_dir / f"{symbol_lower}_combo_yield_rank_shadow.csv").resolve()
    try:
        build_yield_enhancement_rank_shadow(raw_yield_pairs_df).to_csv(rank_shadow_path, index=False)
    except Exception as exc:
        log.warning("combo_yield_steps: failed to write shadow rank artifact for %s: %s", symbol, exc)

    trace_rows = [
        build_candidate_filter_trace_row(
            run_id=scope.get("run_id"),
            account=scope.get("account"),
            symbol=symbol,
            function=COMBO_YIELD_FAMILY,
            mode=yield_enhancement_policy.mode,
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=yield_enhancement_policy.mode,
            status="rejected",
            stage="combo_pair_filter",
            rule=str(reason),
            metric_value=int(count),
            threshold=0,
            message=f"combo yield pair rejection count: {reason}",
            evidence_path=result.candidates_path.name,
            config_values={
                **yield_enhancement_policy.to_fields(),
                "funding_put_min_annualized_return": funding_put_min_annualized_return,
            },
        )
        for reason, count in sorted(dict(raw_yield_pairs_df.attrs.get("reject_counts") or {}).items())
        if int(count) > 0
    ]
    yield_threshold: float | int = 1
    if df_yield_put_universe.empty:
        yield_rule = "combo_yield_no_funding_put_eligible"
        yield_status = "post_filtered"
        yield_threshold = funding_put_min_annualized_return
    elif df_yield_put_event_filtered.empty:
        yield_rule = "combo_yield_put_event_filtered"
        yield_status = "post_filtered"
    elif df_yield_put_candidates_for_pairs.empty:
        yield_rule = "combo_yield_put_cash_filtered"
        yield_status = "post_filtered"
    elif raw_yield_pairs_df.empty:
        yield_rule = "combo_yield_no_pair"
        yield_status = "post_filtered"
    elif recommended_yield_pairs_df.empty:
        yield_rule = "combo_yield_no_recommended_pair"
        yield_status = "post_filtered"
    else:
        yield_rule = "combo_yield_pair_accepted"
        yield_status = "accepted"
    trace_rows.append(
        build_candidate_filter_trace_row(
            run_id=scope.get("run_id"),
            account=scope.get("account"),
            symbol=symbol,
            function=COMBO_YIELD_FAMILY,
            mode=yield_enhancement_policy.mode,
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=yield_enhancement_policy.mode,
            status=yield_status,
            stage="post_filter",
            rule=yield_rule,
            metric_value=len(recommended_yield_pairs_df),
            threshold=yield_threshold,
            message="combo yield pair selection",
            evidence_path=result.candidates_path.name,
            config_values={
                **yield_enhancement_policy.to_fields(),
                "funding_put_min_annualized_return": funding_put_min_annualized_return,
            },
        )
    )
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(result.candidates_path), trace_rows)

    separate_enabled = bool(yield_enhancement_policy.enabled) and wants_yield_enhancement_separate(yield_enhancement_cfg)
    inline_enabled = bool(yield_enhancement_policy.enabled) and wants_yield_enhancement_inline(yield_enhancement_cfg)
    if separate_enabled:
        try:
            recommended_yield_pairs_df.to_csv(result.candidates_path, index=False)
        except Exception:
            pass
    if inline_enabled:
        attach_calls_fn(
            df_candidates=df_sell_put_labeled,
            pairs_df=recommended_yield_pairs_df,
            out_path=sell_put_labeled_path,
        )

    final_result = ComboYieldResult(
        recommended_pairs=recommended_yield_pairs_df,
        separate_enabled=separate_enabled,
        candidates_path=result.candidates_path,
        alerts_path=result.alerts_path,
    )

    if not is_scheduled and final_result.separate_enabled:
        render_alerts_fn(
            input_path=final_result.candidates_path,
            symbol=symbol,
            top=int(top_n),
            output_path=final_result.alerts_path,
            base_dir=base,
        )

    summary = None
    if final_result.separate_enabled:
        summary = summarize_yield_enhancement(final_result.recommended_pairs, symbol, symbol_cfg=symbol_cfg)
    return final_result, summary
