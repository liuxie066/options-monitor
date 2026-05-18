"""Sell-put cash labeling helpers.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

This module is intentionally small and side-effect free except writing to the labeled CSV.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from domain.domain.cash_secured_utils import (
    cash_secured_symbol_by_ccy,
    cash_secured_symbol_cny,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
    read_cash_secured_total_cny,
)
from domain.domain.ledger.position_fields import normalize_currency
from src.infrastructure.exchange_rates import CurrencyConverter
from src.infrastructure.io_utils import safe_read_csv

log = logging.getLogger(__name__)


def _sum_cash_total_cny(
    cash_by_ccy: dict[str, Any] | None,
    *,
    exchange_rate_converter: CurrencyConverter,
) -> float | None:
    if not isinstance(cash_by_ccy, dict):
        return None

    total = 0.0
    ok = True
    for ccy, value in cash_by_ccy.items():
        try:
            amount = float(value)
        except Exception:
            continue
        if not amount:
            continue
        native_ccy = str(ccy or "").strip().upper()
        if native_ccy in ("CNY", "RMB"):
            total += amount
            continue
        converted = exchange_rate_converter.native_to_cny(amount, native_ccy=native_ccy)
        if converted is None:
            ok = False
            break
        total += float(converted)
    return total if ok else None


def enrich_sell_put_candidates_with_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict | None,
    exchange_rate_converter: CurrencyConverter,
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
        except Exception as e:
            log.warning("sell_put_cash: failed to write CSV: %s", e)
        return df_sp_lab

    option_ctx: dict[str, Any] | None = None
    try:
        option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None
    except Exception as e:
        log.warning("sell_put_cash: failed to read option_ctx: %s", e)
        option_ctx = None

    used_symbol_usd = 0.0
    used_total_usd = 0.0
    used_total_cny = None
    used_symbol_cny = None
    cash_secured_unavailable_reason = ""

    if option_ctx:
        unavailable = option_ctx.get("cash_secured_unavailable_by_symbol")
        if isinstance(unavailable, dict) and unavailable:
            cash_secured_unavailable_reason = ";".join(
                f"{sym}:{reason}" for sym, reason in sorted(unavailable.items())
            )
            log.warning(
                "sell_put_cash: cash_secured unavailable; fail-closed cash gating: %s",
                cash_secured_unavailable_reason,
            )
        try:
            norm_by_ccy = normalize_cash_secured_by_symbol_by_ccy(option_ctx)
            total_by_ccy_norm = normalize_cash_secured_total_by_ccy(option_ctx, by_symbol_by_ccy=norm_by_ccy)
            sym_used_by_ccy = cash_secured_symbol_by_ccy(option_ctx, symbol, by_symbol_by_ccy=norm_by_ccy)

            used_symbol_usd = float((sym_used_by_ccy or {}).get('USD') or 0.0)
            used_total_usd = float(total_by_ccy_norm.get('USD') or 0.0)
            used_total_cny = read_cash_secured_total_cny(option_ctx)
            used_symbol_cny = cash_secured_symbol_cny(
                option_ctx,
                symbol,
                by_symbol_by_ccy=norm_by_ccy,
                native_to_cny=lambda amt, ccy: exchange_rate_converter.native_to_cny(amt, native_ccy=ccy),
            )
        except Exception as e:
            log.warning("sell_put_cash: cash_secured calc failed for %s: %s", symbol, e)
            used_symbol_usd = 0.0
            used_total_usd = 0.0
            used_total_cny = None
            used_symbol_cny = None

    cash_avail = None
    cash_avail_cny = None
    cash_free_cny = None
    cash_avail_total_cny = None
    cash_free_total_cny = None
    try:
        cash_by_ccy = (portfolio_ctx.get('cash_by_currency') or {}) if isinstance(portfolio_ctx, dict) else {}
        v = cash_by_ccy.get('USD')
        cash_avail = float(v) if v is not None else None

        cny = cash_by_ccy.get('CNY')
        cash_avail_cny = float(cny) if cny is not None else None
        cash_avail_total_cny = _sum_cash_total_cny(
            cash_by_ccy,
            exchange_rate_converter=exchange_rate_converter,
        )

        if cash_avail_cny is not None:
            cash_free_cny = (cash_avail_cny - used_total_cny) if used_total_cny is not None else None

        if cash_avail_total_cny is not None and used_total_cny is not None:
            cash_free_total_cny = cash_avail_total_cny - used_total_cny
    except Exception as e:
        log.warning("sell_put_cash: cash_available calc failed: %s", e)
        cash_avail = None
        cash_avail_total_cny = None
        cash_free_total_cny = None

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
        df_sp_lab['cash_available_usd_est'] = pd.NA
        df_sp_lab['cash_free_usd_est'] = pd.NA

    df_sp_lab['cash_available_cny'] = (cash_avail_cny if cash_avail_cny is not None else pd.NA)
    df_sp_lab['cash_free_cny'] = (cash_free_cny if cash_free_cny is not None else pd.NA)
    df_sp_lab['cash_available_total_cny'] = (cash_avail_total_cny if cash_avail_total_cny is not None else pd.NA)
    df_sp_lab['cash_free_total_cny'] = (cash_free_total_cny if cash_free_total_cny is not None else pd.NA)
    df_sp_lab['cash_secured_unavailable_reason'] = cash_secured_unavailable_reason or pd.NA
    if cash_secured_unavailable_reason:
        df_sp_lab['cash_secured_used_usd_total'] = pd.NA
        df_sp_lab['cash_secured_used_usd_symbol'] = pd.NA
        df_sp_lab['cash_secured_used_usd'] = pd.NA
        df_sp_lab['cash_secured_used_cny_total'] = pd.NA
        df_sp_lab['cash_secured_used_cny_symbol'] = pd.NA
        df_sp_lab['cash_secured_used_cny'] = pd.NA
        df_sp_lab['cash_free_usd'] = pd.NA
        df_sp_lab['cash_free_usd_est'] = pd.NA
        df_sp_lab['cash_free_cny'] = pd.NA
        df_sp_lab['cash_free_total_cny'] = pd.NA

    # Cash requirement
    try:
        if 'multiplier' in df_sp_lab.columns:
            m = pd.to_numeric(df_sp_lab['multiplier'], errors='coerce')
        else:
            m = pd.Series([pd.NA] * len(df_sp_lab), index=df_sp_lab.index, dtype='float64')

        strike = pd.to_numeric(df_sp_lab['strike'], errors='coerce')
        native_req = strike.astype(float) * m.astype(float)
        df_sp_lab['cash_requirement_unavailable_reason'] = pd.NA

        missing_strike = strike.isna() | (strike.astype(float) <= 0)
        missing_m = m.isna() | (m.astype(float) <= 0)
        if missing_strike.any():
            df_sp_lab.loc[missing_strike, 'cash_requirement_unavailable_reason'] = 'sell_put_candidate_strike_missing'
        if missing_m.any():
            df_sp_lab.loc[missing_m, 'cash_requirement_unavailable_reason'] = 'sell_put_candidate_multiplier_missing'

        ccy = ""
        if 'currency' in df_sp_lab.columns and len(df_sp_lab) > 0:
            ccy = normalize_currency(df_sp_lab['currency'].iloc[0])
        if not ccy:
            df_sp_lab['cash_requirement_unavailable_reason'] = (
                df_sp_lab['cash_requirement_unavailable_reason']
                .fillna('')
                .astype(str)
                .where(lambda s: s.str.strip() != '', 'sell_put_candidate_currency_missing')
            )

        if ccy == 'USD':
            df_sp_lab['cash_required_usd'] = native_req
        else:
            df_sp_lab['cash_required_usd'] = pd.NA

        try:
            missing_req = missing_strike | missing_m
            if missing_req.any():
                df_sp_lab.loc[missing_req, 'cash_required_usd'] = pd.NA
        except Exception:
            pass

        k = exchange_rate_converter.native_to_cny(1.0, native_ccy=ccy) if ccy else None
        if k is None or k <= 0:
            df_sp_lab['cash_required_cny'] = pd.NA
            if ccy:
                empty_reason = df_sp_lab['cash_requirement_unavailable_reason'].fillna('').astype(str).str.strip() == ''
                df_sp_lab.loc[empty_reason, 'cash_requirement_unavailable_reason'] = f'sell_put_candidate_cny_rate_missing:{ccy}'
        else:
            df_sp_lab['cash_required_cny'] = native_req.astype(float) * float(k)
            try:
                missing_req = missing_strike | missing_m
                if missing_req.any():
                    df_sp_lab.loc[missing_req, 'cash_required_cny'] = pd.NA
            except Exception:
                pass
    except Exception as e:
        log.warning("sell_put_cash: cash_required calc failed: %s", e)
        df_sp_lab['cash_required_usd'] = pd.NA
        df_sp_lab['cash_required_cny'] = pd.NA
        df_sp_lab['cash_requirement_unavailable_reason'] = 'sell_put_candidate_cash_requirement_calc_failed'

    try:
        df_sp_lab.to_csv(out_path, index=False)
    except Exception as e:
        log.warning("sell_put_cash: failed to write CSV: %s", e)

    return df_sp_lab
