"""Sell-put cash labeling helpers.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

This module is intentionally small and side-effect free except writing to the labeled CSV.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from domain.domain.cash_secured_utils import (
    cash_secured_unavailable_for_cash_snapshot,
    cash_secured_symbol_by_ccy,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import require_option_multiplier
from domain.domain import strategy_core
from src.infrastructure.exchange_rates import CurrencyConverter
from src.application.portfolio_context_service import cash_snapshot_is_usable
from src.application.cash_totals import sum_by_currency_to_cny

log = logging.getLogger(__name__)


def _option_multiplier(value: Any) -> int | None:
    try:
        return require_option_multiplier(value)
    except (TypeError, ValueError):
        return None


def sell_put_cash_context_unavailable_reason(
    option_ctx: dict[str, Any] | None,
    portfolio_ctx: dict[str, Any] | None,
) -> str:
    if not isinstance(portfolio_ctx, dict) or not cash_snapshot_is_usable(portfolio_ctx):
        return "broker_cash_snapshot_unavailable"
    if not isinstance(option_ctx, dict):
        return "option_positions_cash_secured_context_unavailable"
    context_status = str(option_ctx.get("context_status") or "").strip().lower()
    if context_status and context_status != "available":
        return "option_positions_cash_secured_context_unavailable"
    unavailable = cash_secured_unavailable_for_cash_snapshot(option_ctx, portfolio_ctx)
    if unavailable is not None and not isinstance(unavailable, dict):
        return "option_positions_cash_secured_context_unavailable"
    if isinstance(unavailable, dict) and unavailable:
        return ";".join(
            f"{str(sym)}:{str(reason)}"
            for sym, reason in sorted(
                unavailable.items(),
                key=lambda item: str(item[0]),
            )
        )
    if not isinstance(option_ctx.get("cash_secured_by_symbol_by_ccy"), dict):
        return "option_positions_cash_secured_context_unavailable"
    return ""


def sell_put_opening_capacity_inputs(
    *, symbol: str, strike: Any, multiplier: Any, currency: Any,
    portfolio_ctx: dict[str, Any] | None, exchange_rate_converter: CurrencyConverter,
) -> dict[str, Any]:
    """Compatibility projection of the core capacity result."""
    capacity = sell_put_opening_capacity(
        symbol=symbol, strike=strike, multiplier=multiplier, currency=currency,
        portfolio_ctx=portfolio_ctx, exchange_rate_converter=exchange_rate_converter,
    )
    return {key: value for key, value in capacity.items() if key.startswith("put_cash_")}


def sell_put_opening_capacity(
    *,
    symbol: str,
    strike: Any,
    multiplier: Any,
    currency: Any,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> dict[str, Any]:
    """Return gross assignment requirement and effective native cash headroom."""

    try:
        strike_value = float(strike)
        multiplier_value = require_option_multiplier(multiplier)
    except (TypeError, ValueError):
        return {
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": "assignment_requirement_invalid",
        }
    if strike_value <= 0 or multiplier_value <= 0 or not isinstance(portfolio_ctx, dict):
        return {
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": "assignment_requirement_or_portfolio_context_invalid",
        }

    option_ctx = portfolio_ctx.get("option_ctx")
    cash_secured_unavailable_reason = sell_put_cash_context_unavailable_reason(
        option_ctx if isinstance(option_ctx, dict) else None, portfolio_ctx
    )
    if cash_secured_unavailable_reason:
        return {
            "put_cash_required": strike_value * multiplier_value,
            "put_cash_free": None,
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": cash_secured_unavailable_reason,
        }

    try:
        normalized_by_ccy = normalize_cash_secured_by_symbol_by_ccy(option_ctx)
        total_by_ccy = normalize_cash_secured_total_by_ccy(
            option_ctx,
            by_symbol_by_ccy=normalized_by_ccy,
        )
        cash_by_ccy = portfolio_ctx.get("cash_by_currency")
        if not isinstance(cash_by_ccy, dict):
            return {
                "put_cash_capacity_available": False,
                "put_cash_capacity_reason": "cash_by_currency_missing",
            }
        native_currency = normalize_currency(currency)
        return strategy_core.calculate_capacity(
            {"strike": strike_value, "multiplier": multiplier_value, "currency": native_currency},
            mode="put", exchange_rate_converter=exchange_rate_converter,
            capacity_inputs=strategy_core.PutCapacityInputs(
                tuple(cash_by_ccy.items()), tuple(total_by_ccy.items()), portfolio_ctx.get("_sell_put_fx_status"),
            ),
        )
    except (TypeError, ValueError):
        return {
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": "cash_capacity_inputs_invalid",
        }



def enrich_sell_put_candidates_with_cash(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    portfolio_ctx: dict | None,
    exchange_rate_converter: CurrencyConverter,
    demo_capacity: bool = False,
) -> pd.DataFrame:
    """Add cash secured usage / cash available / cash required columns."""

    df_sp_lab = df_labeled
    if df_sp_lab is None or df_sp_lab.empty:
        return df_sp_lab

    if demo_capacity:
        out = df_sp_lab.copy()
        strike = pd.to_numeric(
            out["strike"] if "strike" in out else pd.Series(index=out.index, dtype=float),
            errors="coerce",
        )
        multiplier = (
            out["multiplier"].map(_option_multiplier)
            if "multiplier" in out
            else pd.Series(index=out.index, dtype=float)
        )
        required = strike * multiplier
        valid = strike.gt(0) & multiplier.gt(0)
        out["cash_required_native"] = required.where(valid, pd.NA)
        out["cash_free_effective_native"] = required.where(valid, pd.NA)
        out["cash_available_effective_native"] = required.where(valid, pd.NA)
        out["cash_native_currency"] = out.get("currency", pd.NA)
        out["cash_capacity_basis"] = "demo_scenario"
        out["capacity_source"] = "demo_scenario"
        out["max_new_contracts"] = valid.astype(int)
        for idx in out.index[valid]:
            # Demo supplies exactly one contract's collateral; no real cash authority.
            native = normalize_currency(out.loc[idx].get("currency")) or "CNY"
            capacity = strategy_core.calculate_capacity(
                {"strike": strike.loc[idx], "multiplier": multiplier.loc[idx], "currency": native},
                mode="put", exchange_rate_converter=exchange_rate_converter,
                capacity_inputs=strategy_core.PutCapacityInputs(((native, required.loc[idx]),)),
            )
            out.at[idx, "max_new_contracts"] = capacity["max_new_contracts"]
        out["cash_pool_additive_across_candidates"] = False
        out["cash_secured_used_usd_total"] = 0.0
        out["cash_secured_used_usd_symbol"] = 0.0
        out["cash_secured_used_usd"] = 0.0
        out["cash_secured_used_cny_total"] = 0.0
        out["cash_secured_used_cny_symbol"] = 0.0
        out["cash_secured_used_cny"] = 0.0
        out["cash_requirement_unavailable_reason"] = pd.NA
        out.loc[~valid, "cash_requirement_unavailable_reason"] = (
            "assignment_requirement_invalid"
        )
        return out

    if not portfolio_ctx:
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
    cash_secured_unavailable_reason = sell_put_cash_context_unavailable_reason(
        option_ctx, portfolio_ctx
    )
    total_by_ccy_norm: dict[str, float] = {}
    cny_rates = {
        "usdcny_exchange_rate": exchange_rate_converter.native_to_cny(1, native_ccy="USD"),
        "cny_per_hkd_exchange_rate": exchange_rate_converter.native_to_cny(1, native_ccy="HKD"),
    }

    if cash_secured_unavailable_reason:
        log.warning(
            "sell_put_cash: cash_secured unavailable; fail-closed cash gating: %s",
            cash_secured_unavailable_reason,
        )
    if option_ctx and not cash_secured_unavailable_reason:
        try:
            norm_by_ccy = normalize_cash_secured_by_symbol_by_ccy(option_ctx)
            total_by_ccy_norm = normalize_cash_secured_total_by_ccy(option_ctx, by_symbol_by_ccy=norm_by_ccy)
            sym_used_by_ccy = cash_secured_symbol_by_ccy(option_ctx, symbol, by_symbol_by_ccy=norm_by_ccy)

            used_symbol_usd = float((sym_used_by_ccy or {}).get('USD') or 0.0)
            used_total_usd = float(total_by_ccy_norm.get('USD') or 0.0)
            used_total_cny = sum_by_currency_to_cny(total_by_ccy_norm, **cny_rates)
            used_symbol_cny = sum_by_currency_to_cny(sym_used_by_ccy, **cny_rates)
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
        cash_usable = isinstance(portfolio_ctx, dict) and cash_snapshot_is_usable(portfolio_ctx)
        cash_by_ccy = (portfolio_ctx.get('cash_by_currency') or {}) if cash_usable else {}
        v = cash_by_ccy.get('USD')
        cash_avail = float(v) if v is not None else None

        cny = cash_by_ccy.get('CNY')
        cash_avail_cny = float(cny) if cny is not None else None
        if cash_usable:
            cash_avail_total_cny = sum_by_currency_to_cny(cash_by_ccy, **cny_rates)

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
            m = df_sp_lab['multiplier'].map(_option_multiplier)
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

    # Canonical CSP capacity: gross assignment requirement in the option's
    # native currency; native cash first and fresh FX funds at 100%.
    df_sp_lab['cash_required_native'] = pd.NA
    df_sp_lab['cash_free_effective_native'] = pd.NA
    df_sp_lab['cash_available_effective_native'] = pd.NA
    df_sp_lab['cash_native_currency'] = pd.NA
    df_sp_lab['cash_capacity_basis'] = pd.NA
    df_sp_lab['max_new_contracts'] = 0
    df_sp_lab['cash_pool_additive_across_candidates'] = False
    capacity_authority = (
        portfolio_ctx.get("capacity_authority")
        if isinstance(portfolio_ctx, dict)
        and isinstance(portfolio_ctx.get("capacity_authority"), dict)
        else {}
    )
    df_sp_lab['capacity_identity_hash'] = (
        portfolio_ctx.get("capacity_identity_hash")
        if isinstance(portfolio_ctx, dict)
        else pd.NA
    )
    df_sp_lab['futu_account_id'] = capacity_authority.get("futu_account_id", pd.NA)
    df_sp_lab['capacity_trd_env'] = capacity_authority.get("trd_env", pd.NA)
    df_sp_lab['capacity_market'] = capacity_authority.get("market", pd.NA)
    df_sp_lab['capacity_source_observed_at'] = portfolio_ctx.get("cash_source_observed_at", pd.NA) if isinstance(portfolio_ctx, dict) else pd.NA
    df_sp_lab['capacity_authority_status'] = capacity_authority.get(
        "status", pd.NA
    )
    df_sp_lab['cash_fx_status'] = pd.NA
    cash_by_ccy = (
        portfolio_ctx.get('cash_by_currency')
        if isinstance(portfolio_ctx, dict)
        and isinstance(portfolio_ctx.get('cash_by_currency'), dict)
        else None
    )
    fx_status = (
        portfolio_ctx.get("_sell_put_fx_status")
        if isinstance(portfolio_ctx, dict)
        else None
    )
    for idx, row in df_sp_lab.iterrows():
        native_currency = normalize_currency(row.get('currency'))
        required_native = None
        try:
            strike_value = float(row.get('strike'))
            multiplier_value = require_option_multiplier(row.get('multiplier'))
            if strike_value > 0 and multiplier_value > 0:
                required_native = strike_value * multiplier_value
        except (TypeError, ValueError):
            pass
        if native_currency:
            df_sp_lab.at[idx, 'cash_native_currency'] = native_currency
        if required_native is not None:
            df_sp_lab.at[idx, 'cash_required_native'] = required_native
        if not native_currency or required_native is None:
            continue
        available_headroom = strategy_core.calculate_capacity(
            {"strike": row.get("strike"), "multiplier": row.get("multiplier"), "currency": native_currency},
            mode="put", exchange_rate_converter=exchange_rate_converter,
            capacity_inputs=strategy_core.PutCapacityInputs(tuple((cash_by_ccy or {}).items()), fx_status=fx_status),
        )
        capacity = sell_put_opening_capacity(
            symbol=symbol,
            strike=row.get('strike'),
            multiplier=row.get('multiplier'),
            currency=native_currency,
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=exchange_rate_converter,
        )
        capacity_reason = str(
            capacity.get("put_cash_capacity_reason") or ""
        ).strip()
        df_sp_lab.at[idx, 'cash_fx_status'] = capacity_reason or pd.NA
        if cash_secured_unavailable_reason:
            continue
        if (
            capacity.get("put_cash_capacity_available") is not True
            or capacity.get("put_cash_free") is None
        ):
            raw_reason = df_sp_lab.at[idx, 'cash_requirement_unavailable_reason']
            empty_reason = '' if pd.isna(raw_reason) else str(raw_reason).strip()
            if not empty_reason:
                df_sp_lab.at[idx, 'cash_requirement_unavailable_reason'] = (
                    capacity_reason or "sell_put_cash_capacity_unavailable"
                )
            continue
        free_cash = float(capacity["put_cash_free"])
        df_sp_lab.at[idx, 'cash_free_effective_native'] = free_cash
        df_sp_lab.at[idx, 'max_new_contracts'] = capacity["max_new_contracts"]
        if available_headroom["put_cash_capacity_available"] and available_headroom["put_cash_free"] is not None:
            df_sp_lab.at[idx, 'cash_available_effective_native'] = available_headroom["put_cash_free"]
        df_sp_lab.at[idx, 'cash_capacity_basis'] = f'same_currency_then_fx:{native_currency}'

        if native_currency == 'USD':
            df_sp_lab.at[idx, 'cash_free_usd'] = free_cash
        free_cny = exchange_rate_converter.convert(
            free_cash,
            from_ccy=native_currency,
            to_ccy='CNY',
        )
        available_cny = (
            exchange_rate_converter.convert(
                available_headroom["put_cash_free"],
                from_ccy=native_currency,
                to_ccy='CNY',
            )
            if available_headroom["put_cash_capacity_available"]
            and available_headroom["put_cash_free"] is not None
            else None
        )
        if free_cny is not None:
            df_sp_lab.at[idx, 'cash_free_total_cny'] = free_cny
        if available_cny is not None:
            df_sp_lab.at[idx, 'cash_available_total_cny'] = available_cny

    return df_sp_lab
