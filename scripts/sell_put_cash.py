"""Sell-put cash labeling helpers.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

This module is intentionally small and side-effect free except writing to the labeled CSV.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from scripts.fx_rates import CurrencyConverter
from scripts.io_utils import safe_read_csv


def enrich_sell_put_candidates_with_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict | None,
    fx: CurrencyConverter,
    out_path,
) -> pd.DataFrame:
    """Add cash secured usage / cash available / cash required columns onto labeled candidates.

    Writes the enriched DataFrame back to out_path (csv) and returns it.

    NOTE: Behavior preserved from the original inline block as much as possible.
    """

    df_sp_lab = df_labeled
    if df_sp_lab is None or df_sp_lab.empty:
        return df_sp_lab

    if not portfolio_ctx:
        try:
            df_sp_lab.to_csv(out_path, index=False)
        except Exception:
            pass
        return df_sp_lab

    option_ctx: dict[str, Any] | None = None
    try:
        option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None
    except Exception:
        option_ctx = None

    used_symbol_usd = 0.0
    used_total_usd = 0.0
    used_total_cny = None
    used_symbol_cny = None

    if option_ctx:
        try:
            by_sym_ccy = option_ctx.get('cash_secured_by_symbol_by_ccy') or {}
            tot_by_ccy = option_ctx.get('cash_secured_total_by_ccy') or {}
            if isinstance(by_sym_ccy, dict) and (by_sym_ccy or tot_by_ccy):
                used_symbol_usd = float(((by_sym_ccy.get(symbol) or {}).get('USD')) or 0.0)
                used_total_usd = float((tot_by_ccy.get('USD')) or 0.0)
                v = option_ctx.get('cash_secured_total_cny')
                used_total_cny = float(v) if v is not None else None
                vs = None
                try:
                    vs = (option_ctx.get('cash_secured_by_symbol_cny') or {}).get(symbol)
                except Exception:
                    vs = None
                used_symbol_cny = float(vs) if vs is not None else None
            else:
                used_map = (option_ctx.get('cash_secured_by_symbol') or {})
                used_symbol_usd = float(used_map.get(symbol) or 0.0)
                used_total_usd = float(sum(float(v or 0.0) for v in used_map.values()))
        except Exception:
            used_symbol_usd = 0.0
            used_total_usd = 0.0
            used_total_cny = None
            used_symbol_cny = None

    cash_avail = None
    cash_avail_est = None
    cash_avail_cny = None
    cash_free_cny = None
    try:
        cash_by_ccy = (portfolio_ctx.get('cash_by_currency') or {}) if isinstance(portfolio_ctx, dict) else {}
        v = cash_by_ccy.get('USD')
        cash_avail = float(v) if v is not None else None

        cny = cash_by_ccy.get('CNY')
        cash_avail_cny = float(cny) if cny is not None else None

        if cash_avail_cny is not None:
            if used_total_cny is not None:
                cash_free_cny = cash_avail_cny - used_total_cny
            else:
                k = fx.native_to_cny(1.0, native_ccy='USD')
                cash_free_cny = (cash_avail_cny - (used_total_usd * float(k))) if k else None

        if cash_avail is None and cash_avail_cny is not None:
            cash_avail_est = fx.cny_to_native(cash_avail_cny, native_ccy='USD')
    except Exception:
        cash_avail = None
        cash_avail_est = None

    df_sp_lab['cash_secured_used_usd_total'] = used_total_usd
    df_sp_lab['cash_secured_used_usd_symbol'] = used_symbol_usd
    df_sp_lab['cash_secured_used_usd'] = used_total_usd

    if used_total_cny is not None:
        df_sp_lab['cash_secured_used_cny_total'] = float(used_total_cny)
    else:
        df_sp_lab['cash_secured_used_cny_total'] = pd.NA
    if used_symbol_cny is not None:
        df_sp_lab['cash_secured_used_cny_symbol'] = float(used_symbol_cny)
    else:
        df_sp_lab['cash_secured_used_cny_symbol'] = pd.NA
    df_sp_lab['cash_secured_used_cny'] = df_sp_lab['cash_secured_used_cny_total']

    if cash_avail is not None:
        df_sp_lab['cash_available_usd'] = cash_avail
        df_sp_lab['cash_available_usd_est'] = pd.NA
        df_sp_lab['cash_free_usd'] = cash_avail - used_total_usd
        df_sp_lab['cash_free_usd_est'] = pd.NA
    else:
        df_sp_lab['cash_available_usd'] = pd.NA
        df_sp_lab['cash_free_usd'] = pd.NA
        df_sp_lab['cash_available_usd_est'] = (cash_avail_est if cash_avail_est is not None else pd.NA)
        if cash_avail_est is not None:
            df_sp_lab['cash_free_usd_est'] = cash_avail_est - used_total_usd
        else:
            df_sp_lab['cash_free_usd_est'] = pd.NA

    df_sp_lab['cash_available_cny'] = (cash_avail_cny if cash_avail_cny is not None else pd.NA)
    df_sp_lab['cash_free_cny'] = (cash_free_cny if cash_free_cny is not None else pd.NA)

    # Cash requirement
    try:
        if 'multiplier' in df_sp_lab.columns:
            m = pd.to_numeric(df_sp_lab['multiplier'], errors='coerce')
        else:
            m = pd.Series([pd.NA] * len(df_sp_lab), index=df_sp_lab.index, dtype='float64')

        strike = pd.to_numeric(df_sp_lab['strike'], errors='coerce')
        native_req = strike.astype(float) * m.astype(float)
        df_sp_lab['cash_required_usd'] = native_req
        # If multiplier missing, cash requirement is unknown.
        try:
            missing_m = m.isna() | (m.astype(float) <= 0)
            if missing_m.any():
                df_sp_lab.loc[missing_m, 'cash_required_usd'] = pd.NA
        except Exception:
            pass

        ccy = None
        if 'currency' in df_sp_lab.columns and len(df_sp_lab) > 0:
            ccy = str(df_sp_lab['currency'].iloc[0] or '').upper()

        c = (ccy or 'USD')
        k = fx.native_to_cny(1.0, native_ccy=c)
        if k is None or k <= 0:
            df_sp_lab['cash_required_cny'] = pd.NA
        else:
            df_sp_lab['cash_required_cny'] = native_req.astype(float) * float(k)
            try:
                missing_m = m.isna() | (m.astype(float) <= 0)
                if missing_m.any():
                    df_sp_lab.loc[missing_m, 'cash_required_cny'] = pd.NA
            except Exception:
                pass
    except Exception:
        df_sp_lab['cash_required_usd'] = pd.NA
        df_sp_lab['cash_required_cny'] = pd.NA

    try:
        df_sp_lab.to_csv(out_path, index=False)
    except Exception:
        pass

    return df_sp_lab
