"""Sell-put pipeline steps.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
    resolve_event_risk_config,
)
from scripts.exchange_rates import CurrencyConverter
from scripts.io_utils import safe_read_csv
from scripts.render_sell_put_alerts import render_sell_put_alerts
from scripts.report_labels import add_sell_put_labels
from scripts.report_summaries import summarize_sell_put
from scripts.scan_sell_put import run_sell_put_scan
from scripts.sell_put_cash import enrich_sell_put_candidates_with_cash
from scripts.sell_put_config import validate_min_annualized_net_return


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
) -> dict:
    symbol_sp = (report_dir / f'{symbol_lower}_sell_put_candidates.csv').resolve()
    symbol_sp_labeled = (report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv').resolve()

    resolved_min_annualized_net_return = validate_min_annualized_net_return(
        sp.get('min_annualized_net_return'),
        source=f'{symbol}.sell_put.min_annualized_net_return',
    )

    liquidity = resolve_candidate_liquidity(global_sell_put_liquidity)
    event_risk = resolve_event_risk_config(global_sell_put_event_risk)
    window = resolve_candidate_window(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
    global_min_net_income = float((global_sell_put_liquidity or {}).get('min_net_income', 0.0) or 0.0)
    min_net_income_cny = float(sp.get('min_net_income', global_min_net_income) or 0.0)

    min_net_income_native = (
        0.0
        if min_net_income_cny <= 0
        else (
            exchange_rate_converter.cny_to_native(
                min_net_income_cny,
                native_ccy=('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
            )
            or 0.0
        )
    )

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
        quiet=bool(is_scheduled),
    )

    add_sell_put_labels(base, symbol_sp, symbol_sp_labeled)

    # account-aware: attach cash secured usage from position lots (open short puts)
    df_sp_lab = safe_read_csv(symbol_sp_labeled)
    if not df_sp_lab.empty:
        df_sp_lab = enrich_sell_put_candidates_with_cash(
            df_labeled=df_sp_lab,
            symbol=symbol,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
            out_path=symbol_sp_labeled,
        )

        # Enforce cash headroom as a hard filter at candidate-filter stage:
        # - Prefer base(CNY) gating when both required/free are known.
        # - Fallback to USD gating when CNY fields are unavailable.
        try:
            d = df_sp_lab.copy()
            dropped = False

            if ('cash_required_cny' in d.columns) and ('cash_free_cny' in d.columns):
                req_cny = pd.to_numeric(d['cash_required_cny'], errors='coerce')
                free_cny = pd.to_numeric(d['cash_free_cny'], errors='coerce')
                mask_drop = req_cny.notna() & free_cny.notna() & (req_cny > free_cny)
                if mask_drop.any():
                    d = d.loc[~mask_drop].copy()
                    dropped = True

            if (not dropped) and ('cash_required_usd' in d.columns) and ('cash_free_usd' in d.columns):
                req_usd = pd.to_numeric(d['cash_required_usd'], errors='coerce')
                free_usd = pd.to_numeric(d['cash_free_usd'], errors='coerce')
                mask_drop = req_usd.notna() & free_usd.notna() & (req_usd > free_usd)
                if mask_drop.any():
                    d = d.loc[~mask_drop].copy()
                    dropped = True

            if dropped:
                d.to_csv(symbol_sp_labeled, index=False)
                df_sp_lab = d
        except Exception:
            pass

    if not is_scheduled:
        render_sell_put_alerts(
            input_path=report_dir / f'{symbol_lower}_sell_put_candidates_labeled.csv',
            symbol=symbol,
            top=int(top_n),
            layered=True,
            output_path=report_dir / f'{symbol_lower}_sell_put_alerts.txt',
            base_dir=base,
        )

    return summarize_sell_put(safe_read_csv(symbol_sp_labeled), symbol, symbol_cfg=symbol_cfg)


def empty_sell_put_summary(symbol: str, *, symbol_cfg: dict) -> dict:
    return summarize_sell_put(pd.DataFrame(), symbol, symbol_cfg=symbol_cfg)
