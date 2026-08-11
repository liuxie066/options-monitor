"""Combo Yield opening-strategy orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from pandas.errors import EmptyDataError

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
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.application.candidate_scanning import (
    evidence_summary_from_decisions,
    project_evidence_scan_status,
)
from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts
from src.application.report_labels import (
    add_sell_put_labels,
    label_sell_put_candidates,
)
from src.application.report_summaries import summarize_yield_enhancement
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_call_helper import (
    attach_best_linked_calls,
    build_yield_enhancement_rank_shadow,
    find_sell_put_yield_enhancement_pairs,
    get_yield_enhancement_pair_diagnostics,
    select_best_yield_enhancement_pairs,
)
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.application.yield_enhancement_config import (
    YieldEnhancementPolicy,
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
    wants_yield_enhancement_inline,
    wants_yield_enhancement_separate,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from src.infrastructure.io_utils import atomic_write_text, safe_read_csv


COMBO_YIELD_FAMILY = "combo_yield"


@dataclass(frozen=True)
class ComboYieldResult:
    recommended_pairs: pd.DataFrame
    separate_enabled: bool
    candidates_path: Path
    alerts_path: Path


def _atomic_write_dataframe(path: Path, df: pd.DataFrame) -> None:
    atomic_write_text(path, df.to_csv(index=False))


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
    out_path: Path | None = None,
    **_compat: Any,
) -> pd.DataFrame:
    """Attach Funding Put cash facts without pre-empting Candidate Engine."""

    # ``out_path`` retains the historical ``*_cash_filtered.csv`` audit name.
    # Its rows are a cash-enriched universe, not a formal candidate authority;
    # Candidate Engine underwriting below owns the only capacity rejection.
    return enrich_sell_put_candidates_with_cash(
        df_labeled=df_labeled,
        symbol=symbol,
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
        out_path=out_path,
    )


# Read-only Shadow Replay imports the historical name. Keep the import surface
# stable while making both paths use the same enrichment-only authority model.
enrich_and_filter_combo_funding_cash = enrich_combo_funding_cash


def _empty_result(*, report_dir: Path, symbol_lower: str) -> ComboYieldResult:
    return ComboYieldResult(
        recommended_pairs=pd.DataFrame(),
        separate_enabled=False,
        candidates_path=(report_dir / f"{symbol_lower}_combo_yield_candidates.csv").resolve(),
        alerts_path=(report_dir / f"{symbol_lower}_combo_yield_alerts.txt").resolve(),
    )


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
    cash_filter_put_candidates_fn: Callable[..., pd.DataFrame] | None = enrich_combo_funding_cash,
    underwriting_filter_put_candidates_fn: Callable[..., pd.DataFrame] = enrich_and_filter_sell_put_underwriting,
    now_utc_fn: Callable[[], datetime] = _utc_now,
    combo_pairs_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> tuple[ComboYieldResult, dict[str, Any] | None]:
    """Run the Combo Yield scan and return an optional summary row."""

    result = _empty_result(report_dir=report_dir, symbol_lower=symbol_lower)
    if not bool(yield_enhancement_policy.enabled):
        return result, None

    symbol_yield_put_universe = (report_dir / f"{symbol_lower}_combo_yield_put_universe.csv").resolve()
    symbol_yield_put_universe_labeled = (
        report_dir / f"{symbol_lower}_combo_yield_put_universe_labeled.csv"
    ).resolve()
    # Compatibility-only audit filename. The formal Funding Put path remains
    # typed and in memory, and Candidate Engine owns the capacity filter.
    symbol_yield_put_universe_cash_audit = (
        report_dir / f"{symbol_lower}_combo_yield_put_universe_cash_filtered.csv"
    ).resolve()
    symbol_yield_put_universe_underwritten = (
        report_dir / f"{symbol_lower}_combo_yield_put_universe_underwritten.csv"
    ).resolve()
    funding_put_min_annualized_return = resolve_min_annualized_net_return(
        symbol_cfg={"sell_put": yield_sp},
    )
    funding_put_decisions: list[dict[str, Any]] = []

    scanned_put_universe = run_put_scan_fn(
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
        strategy_family=COMBO_YIELD_FAMILY,
        strategy_profile=yield_enhancement_policy.mode,
        quiet=True,
        calculation_decision_sink_fn=funding_put_decisions.extend,
    )
    label_put_candidates_fn(base, symbol_yield_put_universe, symbol_yield_put_universe_labeled)
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
            out_path=symbol_yield_put_universe_cash_audit,
            strategy_family=COMBO_YIELD_FAMILY,
            strategy_profile=yield_enhancement_policy.mode,
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
        _atomic_write_dataframe(
            symbol_yield_put_universe_underwritten,
            df_yield_put_candidates_for_pairs,
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
        _atomic_write_dataframe(pair_diagnostics_path, pair_diagnostics)
    except Exception as exc:
        raise RuntimeError(f"failed to persist Combo Yield pair diagnostics: {pair_diagnostics_path}") from exc

    recommended_yield_pairs_df = select_pairs_fn(raw_yield_pairs_df)
    occurrence_scope = infer_trace_scope_from_path(result.candidates_path)
    occurrence_account = str(occurrence_scope.get("account") or "").strip().lower()
    occurrence_run_id = str(occurrence_scope.get("run_id") or "").strip()
    if occurrence_account and occurrence_run_id and not recommended_yield_pairs_df.empty:
        recommended_yield_pairs_df = attach_combo_candidate_occurrences(
            recommended_yield_pairs_df,
            account=occurrence_account,
            market=symbol_market(symbol),
            run_id=occurrence_run_id,
            generated_at_utc=now_utc_fn(),
        )
    rank_shadow_path = (report_dir / f"{symbol_lower}_combo_yield_rank_shadow.csv").resolve()
    try:
        _atomic_write_dataframe(rank_shadow_path, build_yield_enhancement_rank_shadow(raw_yield_pairs_df))
    except Exception as exc:
        raise RuntimeError(f"failed to persist Combo Yield rank shadow: {rank_shadow_path}") from exc

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
            _atomic_write_dataframe(result.candidates_path, recommended_yield_pairs_df)
        except Exception as exc:
            raise RuntimeError(f"failed to persist Combo Yield candidates: {result.candidates_path}") from exc

    final_result = ComboYieldResult(
        recommended_pairs=recommended_yield_pairs_df,
        separate_enabled=separate_enabled,
        candidates_path=result.candidates_path,
        alerts_path=result.alerts_path,
    )
    if combo_pairs_sink_fn is not None:
        combo_pairs_sink_fn(
            [
                dict(item)
                for item in final_result.recommended_pairs.to_dict("records")
            ]
        )

    if not is_scheduled and final_result.separate_enabled:
        render_alerts_fn(
            input_path=final_result.candidates_path,
            symbol=symbol,
            top=int(top_n),
            output_path=final_result.alerts_path,
            base_dir=base,
        )

    evidence = evidence_summary_from_decisions(
        decisions=funding_put_decisions,
        accepted_count=len(df_yield_put_candidates_for_pairs),
    )
    strategy_status, strategy_reason = project_evidence_scan_status(
        evidence=evidence,
        candidate_count=len(final_result.recommended_pairs),
    )
    summary = None
    if final_result.separate_enabled:
        summary = summarize_yield_enhancement(final_result.recommended_pairs, symbol, symbol_cfg=symbol_cfg)
        summary["_evidence_summary"] = evidence
        summary["_strategy_status"] = strategy_status
        summary["_strategy_reason"] = strategy_reason
    if inline_enabled:
        attach_calls_fn(
            df_candidates=df_sell_put_labeled,
            pairs_df=recommended_yield_pairs_df,
            out_path=sell_put_labeled_path,
        )
    return final_result, summary


def materialize_empty_combo_yield_artifacts(*, report_dir: Path, symbol_lower: str) -> ComboYieldResult:
    """Replace current Combo Yield outputs with explicit empty artifacts."""

    result = _empty_result(report_dir=report_dir, symbol_lower=symbol_lower)
    report_dir.mkdir(parents=True, exist_ok=True)
    sell_put_labeled_path = (report_dir / f"{symbol_lower}_sell_put_candidates_labeled.csv").resolve()
    if sell_put_labeled_path.exists() and sell_put_labeled_path.stat().st_size > 0:
        try:
            sell_put_labeled = pd.read_csv(sell_put_labeled_path)
        except EmptyDataError:
            sell_put_labeled = pd.DataFrame()
        except Exception as exc:
            raise RuntimeError(f"failed to read inline Combo Yield artifact: {sell_put_labeled_path}") from exc
        linked_columns = [
            column for column in sell_put_labeled.columns if str(column).startswith("linked_call_")
        ]
        if linked_columns:
            try:
                _atomic_write_dataframe(
                    sell_put_labeled_path,
                    sell_put_labeled.drop(columns=linked_columns),
                )
            except Exception as exc:
                raise RuntimeError(f"failed to clear inline Combo Yield artifact: {sell_put_labeled_path}") from exc

    _atomic_write_dataframe(result.candidates_path, pd.DataFrame())
    atomic_write_text(result.alerts_path, "")
    for suffix in (
        "combo_yield_pair_diagnostics.csv",
        "combo_yield_rank_shadow.csv",
        "combo_yield_put_universe.csv",
        "combo_yield_put_universe_labeled.csv",
        "combo_yield_put_universe_cash_filtered.csv",
        "combo_yield_put_universe_underwritten.csv",
    ):
        _atomic_write_dataframe((report_dir / f"{symbol_lower}_{suffix}").resolve(), pd.DataFrame())
    return result


def empty_combo_yield_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_yield_enhancement(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)


def run_combo_yield_for_symbol_and_summarize(
    *,
    base: Path,
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
    combo_pairs_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any] | None:
    """Symbol-level Combo Yield facade with independent config and artifact ownership."""

    yield_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    policy = derive_yield_enhancement_policy(yield_cfg, market=symbol_market(symbol))
    if not policy.enabled:
        materialize_empty_combo_yield_artifacts(report_dir=report_dir, symbol_lower=symbol_lower)
        return None

    variant = str((policy.config or {}).get("variant") or "sp_lc").strip().lower()
    if variant == "cc_lp":
        return run_cc_lp_variant(
            base=base,
            sym=sym,
            symbol=symbol,
            symbol_lower=symbol_lower,
            symbol_cfg=symbol_cfg,
            yield_cfg=yield_cfg,
            policy=policy,
            required_data_dir=required_data_dir,
            report_dir=report_dir,
            exchange_rate_converter=exchange_rate_converter,
            portfolio_ctx=portfolio_ctx,
            combo_pairs_sink_fn=combo_pairs_sink_fn,
        )

    materialize_empty_combo_yield_artifacts(report_dir=report_dir, symbol_lower=symbol_lower)
    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    yield_window = resolve_candidate_window(sell_put_cfg, defaults=DEFAULT_SELL_PUT_WINDOW)
    funding_put_cfg = dict(sell_put_cfg)
    funding_put_cfg["strategy"] = policy.derived_from_sell_put_strategy
    sell_put_labeled_path = (report_dir / f"{symbol_lower}_sell_put_candidates_labeled.csv").resolve()
    df_sell_put_labeled = (
        safe_read_csv(sell_put_labeled_path)
        if bool(sell_put_cfg.get("enabled", False))
        else pd.DataFrame()
    )

    _result, summary = run_combo_yield_scan_and_summarize(
        base=base,
        sym=sym,
        symbol=symbol,
        symbol_lower=symbol_lower,
        symbol_cfg=symbol_cfg,
        yield_enhancement_cfg=yield_cfg,
        yield_sp=funding_put_cfg,
        yield_enhancement_policy=policy,
        df_sell_put_labeled=df_sell_put_labeled,
        sell_put_labeled_path=sell_put_labeled_path,
        required_data_dir=required_data_dir,
        report_dir=report_dir,
        yield_window=yield_window,
        liquidity=liquidity,
        exchange_rate_converter=exchange_rate_converter,
        portfolio_ctx=portfolio_ctx,
        top_n=top_n,
        is_scheduled=is_scheduled,
        cash_filter_put_candidates_fn=cash_filter_put_candidates_fn,
        combo_pairs_sink_fn=combo_pairs_sink_fn,
    )
    return summary


def run_cc_lp_variant(
    *,
    base: Path,
    sym: str,
    symbol: str,
    symbol_lower: str,
    symbol_cfg: dict[str, Any],
    yield_cfg: dict[str, Any],
    policy: YieldEnhancementPolicy,
    required_data_dir: Path,
    report_dir: Path,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict[str, Any] | None,
    run_cc_lp_scan_fn: Callable[..., pd.DataFrame] = run_cc_lp_scan,
    combo_pairs_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any] | None:
    """Run the CC+LP variant of Combo Yield for one symbol."""

    del base, sym, symbol_lower, yield_cfg
    stock = (portfolio_ctx or {}).get("stock") if isinstance(portfolio_ctx, dict) else None
    sell_call_cfg = dict(symbol_cfg.get("sell_call") or {})
    global_sell_call_liquidity = symbol_cfg.get("_global_sell_call_liquidity") or {}
    df = run_cc_lp_scan_fn(
        symbol=symbol,
        required_data_dir=required_data_dir,
        report_dir=report_dir,
        sell_call_cfg=sell_call_cfg,
        exchange_rate_converter=exchange_rate_converter,
        portfolio_ctx=portfolio_ctx,
        stock=stock,
        global_sell_call_liquidity=global_sell_call_liquidity,
        strategy_profile=policy.mode,
    )
    if df.empty:
        summary = summarize_cc_lp_result(
            df=df,
            symbol=symbol,
            status="no_candidate" if stock else "not_applicable",
            reason="" if stock else "stock_context_missing",
        )
        return summary
    if combo_pairs_sink_fn is not None:
        combo_pairs_sink_fn([dict(item) for item in df.to_dict("records")])
    return summarize_cc_lp_result(
        df=df,
        symbol=symbol,
        status="candidates_found",
    )
