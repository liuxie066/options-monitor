"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from domain.domain.symbol_identity import symbol_currency, symbol_market
from src.infrastructure.io_utils import safe_read_csv
from src.application.render_sell_put_alerts import render_sell_put_alerts
from src.application.report_labels import add_sell_put_labels
from src.application.report_summaries import summarize_sell_put
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_call_helper import (
    attach_best_linked_calls,
    find_sell_put_yield_enhancement_pairs,
    select_best_yield_enhancement_pairs,
)
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.application.strategy_policy import SELL_PUT_FAMILY, strategy_semantics_for_side_config
from src.application.combo_yield_steps import run_combo_yield_scan_and_summarize
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from domain.domain.sell_put_config import validate_min_annualized_net_return
from domain.domain.risk_capacity import compute_sell_put_cash_capacity
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
)

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
) -> list[dict[str, Any]]:
    symbol_sp = (report_dir / f'{symbol_lower}_sell_put_candidates.csv').resolve()
    symbol_sp_labeled = (report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').resolve()
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    yield_sp = dict(yield_enhancement_sell_put_cfg or sp)
    yield_enhancement_policy = derive_yield_enhancement_policy(
        yield_enhancement_cfg,
        yield_sp,
        market=symbol_market(symbol),
    )

    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sp)
    resolved_min_annualized_net_return = 0.0 if sell_put_semantics.scan_uses_underwriting_gate else validate_min_annualized_net_return(
        sp.get('min_annualized_net_return'),
        source=f'{symbol}.sell_put.min_annualized_net_return',
    )

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    event_risk = resolve_event_risk_config(global_sell_put_event_risk)
    window = resolve_candidate_window(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
    yield_window = resolve_candidate_window(yield_sp, defaults=DEFAULT_SELL_PUT_WINDOW)
    global_min_net_income = float((global_sell_put_liquidity or {}).get('min_net_income', 0.0) or 0.0)
    min_net_income_cny = float(sp.get('min_net_income', global_min_net_income) or 0.0)

    min_net_income_native = 0.0
    sell_put_scan_allowed = True
    if sell_put_semantics.scan_uses_underwriting_gate:
        min_net_income_native = 0.0
    elif min_net_income_cny > 0:
        native_ccy = symbol_currency(symbol)
        if not native_ccy:
            log.warning("sell_put_steps: currency unresolved for %s; fail closed", symbol)
            sell_put_scan_allowed = False
        else:
            converted_min_income = exchange_rate_converter.cny_to_native(
                min_net_income_cny,
                native_ccy=native_ccy,
            )
            if converted_min_income is None:
                log.warning("sell_put_steps: min_net_income conversion unavailable for %s/%s; fail closed", symbol, native_ccy)
                sell_put_scan_allowed = False
            else:
                min_net_income_native = float(converted_min_income)

    if run_sell_put and sell_put_scan_allowed:
        run_sell_put_scan(
            symbols=[sym],
            input_root=required_data_dir,
            output=symbol_sp,
            min_dte=window.min_dte,
            max_dte=window.max_dte,
            min_annualized_net_return=resolved_min_annualized_net_return,
            min_net_income=float(min_net_income_native),
            min_strike=_optional_float(sp, 'min_strike'),
            max_strike=_optional_float(sp, 'max_strike'),
            min_open_interest=liquidity.min_open_interest,
            min_volume=liquidity.min_volume,
            max_spread_ratio=liquidity.max_spread_ratio,
            event_risk_cfg=event_risk,
            score_weights=sp.get('score_weights'),
            strategy_family=sell_put_semantics.strategy_family,
            strategy_profile=sell_put_semantics.scan_strategy_profile,
            quiet=bool(is_scheduled),
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

    _yield_result, yield_summary = run_combo_yield_scan_and_summarize(
        base=base,
        sym=sym,
        symbol=symbol,
        symbol_lower=symbol_lower,
        symbol_cfg=symbol_cfg,
        yield_enhancement_cfg=yield_enhancement_cfg,
        yield_sp=yield_sp,
        yield_enhancement_policy=yield_enhancement_policy,
        df_sell_put_labeled=df_sp_lab,
        sell_put_labeled_path=symbol_sp_labeled,
        required_data_dir=required_data_dir,
        report_dir=report_dir,
        yield_window=yield_window,
        liquidity=liquidity,
        event_risk=event_risk,
        exchange_rate_converter=exchange_rate_converter,
        portfolio_ctx=portfolio_ctx,
        top_n=top_n,
        is_scheduled=bool(is_scheduled),
        run_put_scan_fn=run_sell_put_scan,
        label_put_candidates_fn=add_sell_put_labels,
        find_pairs_fn=find_sell_put_yield_enhancement_pairs,
        select_pairs_fn=select_best_yield_enhancement_pairs,
        attach_calls_fn=attach_best_linked_calls,
        cash_filter_put_candidates_fn=_enrich_and_filter_sell_put_cash,
        underwriting_filter_put_candidates_fn=enrich_and_filter_sell_put_underwriting,
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

    rows = [summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg)]
    if yield_summary is not None:
        rows.append(yield_summary)
    return rows


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
