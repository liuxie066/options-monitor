"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from src.infrastructure.exchange_rates import CurrencyConverter
from domain.domain.symbol_identity import symbol_currency
from src.infrastructure.io_utils import safe_read_csv
from src.application.render_sell_put_alerts import render_sell_put_alerts
from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts
from src.application.report_labels import add_sell_put_labels
from src.application.report_summaries import summarize_sell_put, summarize_yield_enhancement
from src.application.scan_sell_put import run_sell_put_scan
from src.application.sell_put_call_helper import (
    attach_best_linked_calls,
    find_sell_put_yield_enhancement_pairs,
    select_best_yield_enhancement_pairs,
)
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from domain.domain.sell_put_config import validate_min_annualized_net_return
from domain.domain.risk_capacity import compute_sell_put_cash_capacity
from src.application.yield_enhancement_config import (
    resolve_yield_enhancement_cfg,
    wants_yield_enhancement_inline,
    wants_yield_enhancement_separate,
)

log = logging.getLogger(__name__)


def _row_value(row: pd.Series, column: str):
    if column in row.index:
        return row.get(column)
    return None


def _sell_put_cash_block_mask(df: pd.DataFrame) -> pd.Series:
    # Risk control is fail-closed: if no trustworthy cash basis is available,
    # do not keep the sell-put candidate.
    mask = df.apply(
        lambda row: not compute_sell_put_cash_capacity(
            cash_required_cny=_row_value(row, 'cash_required_cny'),
            cash_free_cny=_row_value(row, 'cash_free_cny'),
            cash_free_total_cny=_row_value(row, 'cash_free_total_cny'),
            cash_required_usd=_row_value(row, 'cash_required_usd'),
            cash_free_usd=_row_value(row, 'cash_free_usd'),
        ).accepted,
        axis=1,
    ).astype(bool)
    if 'cash_secured_unavailable_reason' in df.columns:
        unavailable = df['cash_secured_unavailable_reason'].fillna('').astype(str).str.strip() != ''
        mask = mask | unavailable
    if 'cash_requirement_unavailable_reason' in df.columns:
        unavailable = df['cash_requirement_unavailable_reason'].fillna('').astype(str).str.strip() != ''
        mask = mask | unavailable
    return mask


def _enrich_and_filter_sell_put_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict | None,
    exchange_rate_converter: CurrencyConverter,
    out_path: Path,
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
            d = d.loc[~mask_drop].copy()
            d.to_csv(out_path, index=False)
            df_out = d
    except Exception as exc:
        log.warning("sell_put_steps: cash hard filter failed for %s; fail closed: %s", symbol, exc)
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
    symbol_cfg: dict,
    sp: dict,
    top_n: int,
    required_data_dir: Path,
    report_dir: Path,
    timeout_sec: int | None,
    is_scheduled: bool,
    exchange_rate_converter: CurrencyConverter,
    portfolio_ctx: dict | None,
    global_sell_put_liquidity: dict | None = None,
    global_sell_put_event_risk: dict | None = None,
) -> list[dict]:
    symbol_sp = (report_dir / f'{symbol_lower}_sell_put_candidates.csv').resolve()
    symbol_sp_labeled = (report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').resolve()
    symbol_yield_put_universe = (report_dir / f'{symbol_lower}_yield_enhancement_put_universe.csv').resolve()
    symbol_yield_put_universe_labeled = (report_dir / f'{symbol_lower}_yield_enhancement_put_universe_labeled.csv').resolve()
    symbol_yield_enhancement = (report_dir / f'{symbol_lower}_yield_enhancement_candidates.csv').resolve()
    yield_enhancement_alerts = (report_dir / f'{symbol_lower}_yield_enhancement_alerts.txt').resolve()
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    yield_enhancement_inline = wants_yield_enhancement_inline(yield_enhancement_cfg)
    yield_enhancement_separate = wants_yield_enhancement_separate(yield_enhancement_cfg)

    resolved_min_annualized_net_return = validate_min_annualized_net_return(
        sp.get('min_annualized_net_return'),
        source=f'{symbol}.sell_put.min_annualized_net_return',
    )

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    event_risk = resolve_event_risk_config(global_sell_put_event_risk)
    window = resolve_candidate_window(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
    global_min_net_income = float((global_sell_put_liquidity or {}).get('min_net_income', 0.0) or 0.0)
    min_net_income_cny = float(sp.get('min_net_income', global_min_net_income) or 0.0)

    min_net_income_native = 0.0
    sell_put_scan_allowed = True
    if min_net_income_cny > 0:
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

    if sell_put_scan_allowed:
        run_sell_put_scan(
            symbols=[sym],
            input_root=required_data_dir,
            output=symbol_sp,
            min_dte=window.min_dte,
            max_dte=window.max_dte,
            min_annualized_net_return=resolved_min_annualized_net_return,
            min_net_income=float(min_net_income_native),
            min_strike=(float(sp.get('min_strike')) if sp.get('min_strike') is not None else None),
            max_strike=(float(sp.get('max_strike')) if sp.get('max_strike') is not None else None),
            min_open_interest=liquidity.min_open_interest,
            min_volume=liquidity.min_volume,
            max_spread_ratio=liquidity.max_spread_ratio,
            event_risk_cfg=event_risk,
            score_weights=sp.get('score_weights'),
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
            )
    else:
        df_sp_lab = pd.DataFrame()
        try:
            df_sp_lab.to_csv(symbol_sp, index=False)
            df_sp_lab.to_csv(symbol_sp_labeled, index=False)
        except Exception as exc:
            log.warning("sell_put_steps: failed to write fail-closed sell-put CSV for %s: %s", symbol, exc)

    df_yield_put_universe = df_sp_lab
    if bool(yield_enhancement_cfg.get("enabled", False)):
        run_sell_put_scan(
            symbols=[sym],
            input_root=required_data_dir,
            output=symbol_yield_put_universe,
            min_dte=window.min_dte,
            max_dte=window.max_dte,
            min_annualized_net_return=0.0,
            min_net_income=0.0,
            min_strike=(float(sp.get('min_strike')) if sp.get('min_strike') is not None else None),
            max_strike=(float(sp.get('max_strike')) if sp.get('max_strike') is not None else None),
            min_open_interest=liquidity.min_open_interest,
            min_volume=liquidity.min_volume,
            max_spread_ratio=liquidity.max_spread_ratio,
            event_risk_cfg=event_risk,
            score_weights=sp.get('score_weights'),
            quiet=True,
        )
        add_sell_put_labels(base, symbol_yield_put_universe, symbol_yield_put_universe_labeled)
        df_yield_put_universe = safe_read_csv(symbol_yield_put_universe_labeled)
        if not df_yield_put_universe.empty:
            df_yield_put_universe = _enrich_and_filter_sell_put_cash(
                df_labeled=df_yield_put_universe,
                symbol=symbol,
                portfolio_ctx=portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                out_path=symbol_yield_put_universe_labeled,
            )

    raw_yield_pairs_df = find_sell_put_yield_enhancement_pairs(
        df_candidates=df_yield_put_universe,
        symbol=symbol,
        input_root=required_data_dir,
        yield_enhancement_cfg=yield_enhancement_cfg,
        sell_put_cfg=sp,
        global_yield_enhancement_liquidity=(symbol_cfg.get('_global_yield_enhancement_liquidity') or {}),
        output_path=None,
    )
    recommended_yield_pairs_df = select_best_yield_enhancement_pairs(raw_yield_pairs_df)
    if yield_enhancement_separate:
        try:
            recommended_yield_pairs_df.to_csv(symbol_yield_enhancement, index=False)
        except Exception:
            pass
    if yield_enhancement_inline:
        df_sp_lab = attach_best_linked_calls(
            df_candidates=df_sp_lab,
            pairs_df=recommended_yield_pairs_df,
            out_path=symbol_sp_labeled,
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
        if yield_enhancement_separate:
            render_yield_enhancement_alerts(
                input_path=symbol_yield_enhancement,
                symbol=symbol,
                top=int(top_n),
                output_path=yield_enhancement_alerts,
                base_dir=base,
            )

    rows = [summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg)]
    if yield_enhancement_separate:
        rows.append(summarize_yield_enhancement(recommended_yield_pairs_df, symbol, symbol_cfg=symbol_cfg))
    return rows


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict) -> dict:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
