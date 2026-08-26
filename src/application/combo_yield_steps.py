"""Combo Yield opening-strategy orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    CandidateLiquidityDefaults,
    CandidateWindowDefaults,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from domain.domain.combo_candidate_evidence import build_combo_candidate_occurrence
from domain.domain.sell_put_config import resolve_min_annualized_net_return
from domain.domain.symbol_identity import symbol_market
from src.application.cc_lp_steps import (
    CC_LP_FAMILY,
    run_cc_lp_scan,
    summarize_cc_lp_result,
)
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    infer_trace_scope_from_path,
)
from src.application.candidate_scanning import (
    evidence_summary_from_decisions,
    project_evidence_scan_status,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
)
from src.application.render_combo_yield_alerts import render_combo_yield_alerts
from src.application.report_labels import label_sell_put_candidates
from src.application.report_summaries import summarize_combo_yield
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_call_helper import (
    build_combo_yield_rank_shadow,
    find_sell_put_combo_yield_pairs,
    get_combo_yield_pair_diagnostics,
    select_best_combo_yield_pairs,
)
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.application.combo_yield_config import (
    ComboYieldPolicy,
    derive_combo_yield_policy,
    resolve_combo_yield_cfg,
)
from src.infrastructure.exchange_rates import CurrencyConverter


COMBO_YIELD_FAMILY = "combo_yield"
_COMBO_YIELD_CANDIDATE_EVIDENCE_PATH = (
    f"state/{COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE}"
)
_CANDIDATE_FILTER_TRACE_EVIDENCE_PATH = "candidate_filter_trace.jsonl"


@dataclass(frozen=True)
class ComboYieldResult:
    recommended_pairs: pd.DataFrame


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def attach_combo_candidate_occurrences(
    df: pd.DataFrame,
    *,
    account: str,
    market: str,
    run_id: str,
    generated_at_utc: datetime,
) -> pd.DataFrame:
    """Attach immutable occurrence metadata at the account/run publication boundary."""

    if df.empty:
        return df.copy()
    out = df.copy()
    rows: list[dict[str, Any]] = []
    for raw in out.to_dict(orient="records"):
        row = dict(raw)
        data_as_of = (
            row.get("data_as_of_utc")
            or row.get("as_of_utc")
            or generated_at_utc
        )
        try:
            occurrence = build_combo_candidate_occurrence(
                row,
                account=account,
                market=market,
                run_id=run_id,
                generated_at_utc=generated_at_utc,
                data_as_of_utc=data_as_of,
            )
        except ValueError:
            occurrence = {}
        row.update(occurrence)
        rows.append(row)
    return pd.DataFrame(rows)


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def enrich_combo_funding_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> pd.DataFrame:
    """Attach Funding Put cash facts without pre-empting Candidate Engine."""

    return enrich_sell_put_candidates_with_cash(
        df_labeled=df_labeled,
        symbol=symbol,
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
    )


def _empty_result() -> ComboYieldResult:
    return ComboYieldResult(recommended_pairs=pd.DataFrame())


def run_combo_yield_scan_and_summarize(
    *,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    combo_yield_cfg: dict[str, Any],
    yield_sp: dict[str, Any],
    combo_yield_policy: ComboYieldPolicy,
    required_data_dir: Path,
    report_dir: Path,
    yield_window: CandidateWindowDefaults,
    liquidity: CandidateLiquidityDefaults,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    top_n: int,
    is_scheduled: bool,
    run_put_scan_fn: Callable[..., Any] = run_sell_put_scan,
    find_pairs_fn: Callable[..., pd.DataFrame] = find_sell_put_combo_yield_pairs,
    select_pairs_fn: Callable[[pd.DataFrame], pd.DataFrame] = select_best_combo_yield_pairs,
    render_alerts_fn: Callable[..., str] = render_combo_yield_alerts,
    cash_filter_put_candidates_fn: Callable[..., pd.DataFrame] | None = enrich_combo_funding_cash,
    underwriting_filter_put_candidates_fn: Callable[..., pd.DataFrame] = enrich_and_filter_sell_put_underwriting,
    now_utc_fn: Callable[[], datetime] = _utc_now,
    combo_evidence_sink_fn: Callable[[dict[str, Any]], None] | None = None,
    required_data_frame: pd.DataFrame | None = None,
) -> tuple[ComboYieldResult, dict[str, Any] | None]:
    """Run the Combo Yield scan and return an optional summary row."""

    result = _empty_result()
    if not bool(combo_yield_policy.enabled):
        return result, None

    trace_path = (report_dir / "candidate_filter_trace.jsonl").resolve()
    scope = infer_trace_scope_from_path(trace_path)
    trace_evidence_path = (
        _COMBO_YIELD_CANDIDATE_EVIDENCE_PATH
        if combo_evidence_sink_fn is not None
        and scope.get("run_id")
        and scope.get("account")
        else _CANDIDATE_FILTER_TRACE_EVIDENCE_PATH
    )
    funding_put_min_annualized_return = resolve_min_annualized_net_return(
        symbol_cfg={"sell_put": yield_sp},
    )
    funding_put_decisions: list[dict[str, Any]] = []

    scanned_put_universe = run_put_scan_fn(
        symbols=[sym],
        input_root=required_data_dir,
        min_dte=yield_window.min_dte,
        max_dte=yield_window.max_dte,
        min_annualized_net_return=funding_put_min_annualized_return,
        min_net_income=0.0,
        min_strike=_optional_float(yield_sp, "min_strike"),
        max_strike=_optional_float(yield_sp, "max_strike"),
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        strategy_family=COMBO_YIELD_FAMILY,
        strategy_profile=combo_yield_policy.derived_from_sell_put_strategy,
        calculation_decision_sink_fn=funding_put_decisions.extend,
        required_data_frames=(
            {symbol: required_data_frame}
            if required_data_frame is not None
            else None
        ),
    )
    if not isinstance(scanned_put_universe, pd.DataFrame):
        raise RuntimeError(
            "Combo Yield funding-put scan did not return a typed candidate universe"
        )
    # Candidate evidence includes typed lists and booleans. Keep the formal
    # underwriting path in memory so an audit CSV can never become a
    # calculation authority or coerce complete evidence into strings.
    df_yield_put_universe = label_sell_put_candidates(scanned_put_universe)
    if not df_yield_put_universe.empty:
        df_yield_put_universe["funding_put_eligible"] = True
        df_yield_put_universe["funding_put_min_annualized_return"] = funding_put_min_annualized_return
        df_yield_put_universe["put_only_annualized_net_return"] = df_yield_put_universe.get(
            "annualized_net_return_on_cash_basis"
        )
        df_yield_put_universe["put_only_period_net_return"] = df_yield_put_universe.get(
            "period_net_return_on_cash_basis"
        )
    df_yield_put_cash_enriched = df_yield_put_universe
    if cash_filter_put_candidates_fn is not None and not df_yield_put_universe.empty:
        df_yield_put_cash_enriched = cash_filter_put_candidates_fn(
            df_labeled=df_yield_put_universe.copy(),
            symbol=symbol,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
        )
    df_yield_put_candidates_for_pairs = df_yield_put_cash_enriched
    if not df_yield_put_cash_enriched.empty:
        df_yield_put_candidates_for_pairs = underwriting_filter_put_candidates_fn(
            df_labeled=df_yield_put_cash_enriched.copy(),
            symbol=symbol,
            sell_put_cfg=yield_sp,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
            decision_sink_fn=funding_put_decisions.extend,
        )

    raw_yield_pairs_df = find_pairs_fn(
        df_candidates=df_yield_put_candidates_for_pairs,
        symbol=symbol,
        input_root=required_data_dir,
        combo_yield_cfg=combo_yield_cfg,
        sell_put_cfg=yield_sp,
        global_combo_yield_liquidity=(symbol_cfg.get("_global_combo_yield_liquidity") or {}),
        required_data_frame=required_data_frame,
    )
    pair_diagnostics = get_combo_yield_pair_diagnostics(raw_yield_pairs_df)
    pair_diagnostics["run_id"] = scope.get("run_id")
    pair_diagnostics["account"] = scope.get("account")

    recommended_yield_pairs_df = select_pairs_fn(raw_yield_pairs_df)
    occurrence_account = str(scope.get("account") or "").strip().lower()
    occurrence_run_id = str(scope.get("run_id") or "").strip()
    if occurrence_account and occurrence_run_id and not recommended_yield_pairs_df.empty:
        recommended_yield_pairs_df = attach_combo_candidate_occurrences(
            recommended_yield_pairs_df,
            account=occurrence_account,
            market=symbol_market(symbol),
            run_id=occurrence_run_id,
            generated_at_utc=now_utc_fn(),
        )
    rank_shadow = build_combo_yield_rank_shadow(raw_yield_pairs_df)

    trace_rows = [
        build_candidate_filter_trace_row(
            run_id=scope.get("run_id"),
            account=scope.get("account"),
            symbol=symbol,
            function=COMBO_YIELD_FAMILY,
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=combo_yield_policy.derived_from_sell_put_strategy,
            status="rejected",
            stage="combo_pair_filter",
            rule=str(reason),
            metric_value=int(count),
            threshold=0,
            message=f"combo yield pair rejection count: {reason}",
            evidence_path=trace_evidence_path,
            config_values={
                **combo_yield_policy.to_fields(),
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
    elif df_yield_put_cash_enriched.empty:
        yield_rule = "combo_yield_put_cash_enrichment_empty"
        yield_status = "post_filtered"
    elif df_yield_put_candidates_for_pairs.empty:
        yield_rule = "combo_yield_put_underwriting_filtered"
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
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=combo_yield_policy.derived_from_sell_put_strategy,
            status=yield_status,
            stage="post_filter",
            rule=yield_rule,
            metric_value=len(recommended_yield_pairs_df),
            threshold=yield_threshold,
            message="combo yield pair selection",
            evidence_path=trace_evidence_path,
            config_values={
                **combo_yield_policy.to_fields(),
                "funding_put_min_annualized_return": funding_put_min_annualized_return,
            },
        )
    )
    append_candidate_filter_trace_rows(trace_path, trace_rows)

    final_result = ComboYieldResult(recommended_pairs=recommended_yield_pairs_df)
    if combo_evidence_sink_fn is not None:
        combo_evidence_sink_fn(
            {
                "schema_version": "combo_yield_scan_evidence.v1",
                "variant": "sp_lc",
                "symbol": symbol,
                "funding_put_decisions": [
                    dict(item) for item in funding_put_decisions
                ],
                "pair_evaluations": [
                    dict(item) for item in pair_diagnostics.to_dict("records")
                ],
                "rank_records": [
                    dict(item) for item in rank_shadow.to_dict("records")
                ],
                "ranked_pairs": [
                    dict(item)
                    for item in final_result.recommended_pairs.to_dict("records")
                ],
            }
        )

    if not is_scheduled:
        render_alerts_fn(
            candidates=final_result.recommended_pairs,
            top=int(top_n),
            output_path=(report_dir / f"{symbol_lower}_combo_yield_alerts.txt").resolve(),
        )

    evidence = evidence_summary_from_decisions(
        decisions=funding_put_decisions,
        accepted_count=len(df_yield_put_candidates_for_pairs),
    )
    strategy_status, strategy_reason = project_evidence_scan_status(
        evidence=evidence,
        candidate_count=len(final_result.recommended_pairs),
    )
    summary = summarize_combo_yield(
        final_result.recommended_pairs,
        symbol,
        symbol_cfg=symbol_cfg,
    )
    summary["_evidence_summary"] = evidence
    summary["_strategy_status"] = strategy_status
    summary["_strategy_reason"] = strategy_reason
    return final_result, summary


def empty_combo_yield_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_combo_yield(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)


def run_combo_yield_for_symbol_and_summarize(
    *,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    sell_put_cfg: dict[str, Any],
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    is_scheduled: bool,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    global_sell_put_liquidity: dict[str, Any] | None = None,
    cash_filter_put_candidates_fn: Callable[..., pd.DataFrame] | None = enrich_combo_funding_cash,
    combo_evidence_sink_fn: Callable[[dict[str, Any]], None] | None = None,
    required_data_frame: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """Symbol-level Combo Yield facade with independent config and artifact ownership."""

    yield_cfg = resolve_combo_yield_cfg(symbol_cfg)
    policy = derive_combo_yield_policy(yield_cfg, market=symbol_market(symbol))
    if not policy.enabled:
        return None

    variant = str((policy.config or {}).get("variant") or "sp_lc").strip().lower()
    if variant == "cc_lp":
        return run_cc_lp_variant(
            symbol=symbol,
            symbol_cfg=symbol_cfg,
            policy=policy,
            required_data_dir=required_data_dir,
            exchange_rate_converter=exchange_rate_converter,
            portfolio_ctx=portfolio_ctx,
            combo_evidence_sink_fn=combo_evidence_sink_fn,
            required_data_frame=required_data_frame,
        )

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    yield_window = resolve_candidate_window(sell_put_cfg, defaults=DEFAULT_SELL_PUT_WINDOW)
    funding_put_cfg = dict(sell_put_cfg)
    funding_put_cfg["strategy"] = policy.derived_from_sell_put_strategy
    _result, summary = run_combo_yield_scan_and_summarize(
        sym=sym,
        symbol=symbol,
        symbol_lower=symbol_lower,
        symbol_cfg=symbol_cfg,
        combo_yield_cfg=yield_cfg,
        yield_sp=funding_put_cfg,
        combo_yield_policy=policy,
        required_data_dir=required_data_dir,
        report_dir=report_dir,
        yield_window=yield_window,
        liquidity=liquidity,
        exchange_rate_converter=exchange_rate_converter,
        portfolio_ctx=portfolio_ctx,
        top_n=top_n,
        is_scheduled=is_scheduled,
        cash_filter_put_candidates_fn=cash_filter_put_candidates_fn,
        combo_evidence_sink_fn=combo_evidence_sink_fn,
        required_data_frame=required_data_frame,
    )
    return summary


def run_cc_lp_variant(
    *,
    symbol: str,
    symbol_cfg: dict[str, Any],
    policy: ComboYieldPolicy,
    required_data_dir: Path,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    run_cc_lp_scan_fn: Callable[..., pd.DataFrame] = run_cc_lp_scan,
    combo_evidence_sink_fn: Callable[[dict[str, Any]], None] | None = None,
    required_data_frame: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """Run the CC+LP variant of Combo Yield for one symbol."""

    stock = (portfolio_ctx or {}).get("stock") if isinstance(portfolio_ctx, dict) else None
    sell_call_cfg = dict(symbol_cfg.get("sell_call") or {})
    global_sell_call_liquidity = symbol_cfg.get("_global_sell_call_liquidity") or {}
    df = run_cc_lp_scan_fn(
        symbol=symbol,
        required_data_dir=required_data_dir,
        sell_call_cfg=sell_call_cfg,
        exchange_rate_converter=exchange_rate_converter,
        portfolio_ctx=portfolio_ctx,
        stock=stock,
        global_sell_call_liquidity=global_sell_call_liquidity,
        strategy_profile="cc_lp_funding_call",
        required_data_frame=required_data_frame,
    )
    if combo_evidence_sink_fn is not None:
        combo_evidence_sink_fn(
            {
                "schema_version": "combo_yield_scan_evidence.v1",
                "variant": "cc_lp",
                "symbol": symbol,
                "ranked_pairs": [dict(item) for item in df.to_dict("records")],
            }
        )
    if df.empty:
        summary = summarize_cc_lp_result(
            df=df,
            symbol=symbol,
            status="no_candidate" if stock else "not_applicable",
            reason="" if stock else "stock_context_missing",
        )
        return summary
    return summarize_cc_lp_result(
        df=df,
        symbol=symbol,
        status="candidates_found",
    )
