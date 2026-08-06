"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from src.infrastructure.io_utils import safe_read_csv
from src.application.render_sell_put_alerts import render_sell_put_alerts
from src.application.report_labels import add_sell_put_labels
from src.application.report_summaries import summarize_sell_put
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_cash import (
    enrich_sell_put_candidates_with_cash,
    sell_put_opening_capacity_inputs,
)
from src.application.candidate_underwriting_decisions import (
    apply_insurance_underwriting_to_all_decisions,
)
from src.application.sell_put_strategy_risk import (
    enrich_and_filter_sell_put_underwriting,
    resolve_sell_put_underwriting_config,
)
from src.application.strategy_policy import SELL_PUT_FAMILY, strategy_semantics_for_side_config
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from domain.domain.risk_capacity import compute_sell_put_cash_capacity

log = logging.getLogger(__name__)


def _row_value(row: pd.Series, column: str) -> Any:
    if column in row.index:
        return row.get(column)
    return None


def _text_or_empty(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value or "").strip()


def _sell_put_cash_block_mask(df: pd.DataFrame) -> pd.Series:
    # Risk control is fail-closed: if no trustworthy cash basis is available,
    # do not keep the sell-put candidate.
    mask = cast(
        pd.Series,
        df.apply(
            lambda row: not compute_sell_put_cash_capacity(
                cash_required_native=_row_value(row, 'cash_required_native'),
                cash_free_effective_native=_row_value(row, 'cash_free_effective_native'),
                cash_native_currency=_row_value(row, 'cash_native_currency'),
                cash_required_cny=_row_value(row, 'cash_required_cny'),
                cash_free_cny=_row_value(row, 'cash_free_cny'),
                cash_free_total_cny=_row_value(row, 'cash_free_total_cny'),
                cash_required_usd=_row_value(row, 'cash_required_usd'),
                cash_free_usd=_row_value(row, 'cash_free_usd'),
            ).accepted,
            axis=1,
        ),
    ).astype(bool)
    if 'cash_secured_unavailable_reason' in df.columns:
        unavailable = cast(
            pd.Series,
            df['cash_secured_unavailable_reason'].fillna('').astype(str).str.strip() != '',
        )
        mask = mask | unavailable
    if 'cash_requirement_unavailable_reason' in df.columns:
        unavailable = cast(
            pd.Series,
            df['cash_requirement_unavailable_reason'].fillna('').astype(str).str.strip() != '',
        )
        mask = mask | unavailable
    return cast(pd.Series, mask)


def _optional_float(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return float(value)


def _sell_put_cash_trace_rows(
    *,
    df: pd.DataFrame,
    symbol: str,
    out_path: Path,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
) -> list[dict[str, Any]]:
    scope = infer_trace_scope_from_path(out_path)
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        cash_secured_reason = _text_or_empty(row.get("cash_secured_unavailable_reason"))
        requirement_reason = _text_or_empty(row.get("cash_requirement_unavailable_reason"))
        capacity = compute_sell_put_cash_capacity(
            cash_required_native=_row_value(row, 'cash_required_native'),
            cash_free_effective_native=_row_value(row, 'cash_free_effective_native'),
            cash_native_currency=_row_value(row, 'cash_native_currency'),
            cash_required_cny=_row_value(row, 'cash_required_cny'),
            cash_free_cny=_row_value(row, 'cash_free_cny'),
            cash_free_total_cny=_row_value(row, 'cash_free_total_cny'),
            cash_required_usd=_row_value(row, 'cash_required_usd'),
            cash_free_usd=_row_value(row, 'cash_free_usd'),
        )
        if cash_secured_reason:
            rule = "cash_secured_unavailable"
            message = cash_secured_reason
        elif requirement_reason:
            rule = requirement_reason
            message = requirement_reason
        else:
            rule = capacity.reason
            message = "sell put cash reserve filter"
        rows.append(
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account"),
                symbol=row.get("symbol") or symbol,
                function="cash_reserve",
                mode="put",
                strategy_family=strategy_family,
                strategy_profile=strategy_profile,
                status="post_filtered",
                stage="post_filter",
                rule=rule,
                metric_value=capacity.cash_required,
                threshold=capacity.cash_free,
                contract_symbol=row.get("contract_symbol"),
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=message,
                evidence_path=out_path.name,
                replay_fields=row.to_dict(),
                config_values={"basis": capacity.basis},
            )
        )
    return rows


def _enrich_and_filter_sell_put_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    out_path: Path,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
) -> pd.DataFrame:
    if df_labeled.empty:
        return df_labeled
    df_out = enrich_sell_put_candidates_with_cash(
        df_labeled=df_labeled,
        symbol=symbol,
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
        out_path=out_path,
    )

    # Enforce cash headroom as a hard filter at candidate-filter stage:
    # - Prefer base(CNY) gating when both required/free are known.
    # - Fallback to total(CNY) only when base(CNY) is unavailable.
    # - Fallback to USD gating when CNY fields are unavailable.
    try:
        d = df_out.copy()
        mask_drop = _sell_put_cash_block_mask(d)

        if mask_drop.any():
            append_candidate_filter_trace_rows(
                candidate_trace_path_for_output(out_path),
                _sell_put_cash_trace_rows(
                    df=d.loc[mask_drop].copy(),
                    symbol=symbol,
                    out_path=out_path,
                    strategy_family=strategy_family,
                    strategy_profile=strategy_profile,
                ),
            )
            d = d.loc[~mask_drop].copy()
            d.to_csv(out_path, index=False)
            df_out = d
    except Exception as exc:
        log.warning("sell_put_steps: cash hard filter failed for %s; fail closed: %s", symbol, exc)
        scope = infer_trace_scope_from_path(out_path)
        append_candidate_filter_trace_rows(
            candidate_trace_path_for_output(out_path),
            [
                build_candidate_filter_trace_row(
                    run_id=scope.get("run_id"),
                    account=scope.get("account"),
                    symbol=symbol,
                    function="cash_reserve",
                    mode="put",
                    strategy_family=strategy_family,
                    strategy_profile=strategy_profile,
                    status="post_filtered",
                    stage="post_filter",
                    rule="cash_filter_failed_closed",
                    message=str(exc),
                    evidence_path=out_path.name,
                )
            ],
        )
        df_out = df_out.iloc[0:0].copy()
        try:
            df_out.to_csv(out_path, index=False)
        except Exception as write_exc:
            log.warning("sell_put_steps: failed to write fail-closed CSV for %s: %s", symbol, write_exc)
    return df_out


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
    global_sell_put_event_risk: dict[str, Any] | None = None,
    run_sell_put: bool = True,
    yield_enhancement_sell_put_cfg: dict[str, Any] | None = None,
    risk_policy_version: str | None = None,
    quote_snapshot_id: str | None = None,
    all_decisions_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
    final_candidates_sink_fn: Callable[[str, list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    symbol_sp = (report_dir / f'{symbol_lower}_sell_put_candidates.csv').resolve()
    symbol_sp_labeled = (report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').resolve()
    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sp)

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    event_risk = resolve_event_risk_config(global_sell_put_event_risk)
    window = resolve_candidate_window(sp, defaults=DEFAULT_SELL_PUT_WINDOW)

    if run_sell_put:
        underwriting_cfg = resolve_sell_put_underwriting_config(sp)
        scan_decisions_sink = all_decisions_sink_fn
        if all_decisions_sink_fn is not None and underwriting_cfg.enabled:

            def _underwritten_sink(rows: list[dict[str, Any]]) -> None:
                all_decisions_sink_fn(
                    apply_insurance_underwriting_to_all_decisions(
                        rows,
                        mode="put",
                        cfg=underwriting_cfg,
                        exchange_rate_converter=exchange_rate_converter,
                    )
                )

            scan_decisions_sink = _underwritten_sink
        run_sell_put_scan(
            symbols=[sym],
            input_root=required_data_dir,
            output=symbol_sp,
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
            event_risk_cfg=event_risk,
            score_weights=None,
            strategy_family=sell_put_semantics.strategy_family,
            strategy_profile=sell_put_semantics.scan_strategy_profile,
            quiet=bool(is_scheduled),
            risk_policy_version=risk_policy_version,
            quote_snapshot_id=quote_snapshot_id,
            all_decisions_sink_fn=scan_decisions_sink,
            put_cash_capacity_fn=lambda contract: sell_put_opening_capacity_inputs(
                symbol=symbol,
                strike=contract.strike,
                multiplier=contract.multiplier,
                currency=contract.currency,
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
            ),
        )
        add_sell_put_labels(base, symbol_sp, symbol_sp_labeled)
        df_sp_lab = safe_read_csv(symbol_sp_labeled)
        if not df_sp_lab.empty:
            df_sp_lab = _enrich_and_filter_sell_put_cash(
                df_labeled=df_sp_lab,
                symbol=symbol,
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                out_path=symbol_sp_labeled,
                strategy_family=sell_put_semantics.strategy_family,
                strategy_profile=sell_put_semantics.scan_strategy_profile,
            )
        if not df_sp_lab.empty:
            df_sp_lab = enrich_and_filter_sell_put_underwriting(
                df_labeled=df_sp_lab,
                symbol=symbol,
                sell_put_cfg=sp,
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                out_path=symbol_sp_labeled,
            )
    else:
        df_sp_lab = pd.DataFrame()
        try:
            df_sp_lab.to_csv(symbol_sp, index=False)
            df_sp_lab.to_csv(symbol_sp_labeled, index=False)
        except Exception as exc:
            log.warning("sell_put_steps: failed to write fail-closed sell-put CSV for %s: %s", symbol, exc)


    if final_candidates_sink_fn is not None:
        final_candidates_sink_fn(
            "put",
            [dict(item) for item in df_sp_lab.to_dict("records")],
        )

    if not is_scheduled:
        render_sell_put_alerts(
            input_path=report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv',
            symbol=symbol,
            top=int(top_n),
            layered=True,
            output_path=report_dir / f'{symbol_lower}_sell_put_alerts.txt',
            base_dir=base,
        )

    return [summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg)]


def materialize_empty_sell_put_artifacts(*, report_dir: Path, symbol_lower: str) -> None:
    """Replace current Sell Put outputs with explicit empty artifacts."""

    report_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame().to_csv((report_dir / f"{symbol_lower}_sell_put_candidates.csv").resolve(), index=False)
    pd.DataFrame().to_csv((report_dir / f"{symbol_lower}_sell_put_candidates_labeled.csv").resolve(), index=False)
    (report_dir / f"{symbol_lower}_sell_put_alerts.txt").resolve().write_text("", encoding="utf-8")


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
