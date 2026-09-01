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
from domain.domain.option_position_identity import normalize_currency
from domain.domain.risk_capacity import (
    SellPutEffectiveCash,
    compute_sell_put_effective_cash,
)
from src.infrastructure.exchange_rates import CurrencyConverter

log = logging.getLogger(__name__)


def _cash_secured_context_unavailable_reason(
    option_ctx: dict[str, Any] | None,
) -> str:
    if not isinstance(option_ctx, dict):
        return "option_positions_cash_secured_context_unavailable"
    context_status = str(option_ctx.get("context_status") or "").strip().lower()
    if context_status and context_status != "available":
        return "option_positions_cash_secured_context_unavailable"
    unavailable = option_ctx.get("cash_secured_unavailable_by_symbol")
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


def _effective_cash_in_native_currency(
    *,
    cash_by_ccy: dict[str, Any] | None,
    cash_secured_by_ccy: dict[str, Any] | None,
    native_currency: str,
    exchange_rate_converter: CurrencyConverter,
    cash_required_native: Any = None,
    fx_status: str | None = None,
) -> SellPutEffectiveCash:
    return compute_sell_put_effective_cash(
        cash_by_currency=cash_by_ccy,
        cash_secured_by_currency=cash_secured_by_ccy,
        native_currency=native_currency,
        convert_currency=lambda amount, source, target: exchange_rate_converter.convert(
            amount,
            from_ccy=source,
            to_ccy=target,
        ),
        cash_required_native=cash_required_native,
        fx_status=fx_status,
    )


def sell_put_opening_capacity_inputs(
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
        multiplier_value = float(multiplier)
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

    portfolio_source = str(
        portfolio_ctx.get("portfolio_source_name") or ""
    ).strip().lower()
    authority = portfolio_ctx.get("capacity_authority")
    if portfolio_source and (
        portfolio_source != "futu"
        or not isinstance(authority, dict)
        or authority.get("status") != "available"
    ):
        return {
            "put_cash_required": strike_value * multiplier_value,
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": "physical_account_capacity_authority_unavailable",
        }

    option_ctx = portfolio_ctx.get("option_ctx")
    cash_secured_unavailable_reason = _cash_secured_context_unavailable_reason(
        option_ctx if isinstance(option_ctx, dict) else None
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
        cash_headroom = _effective_cash_in_native_currency(
            cash_by_ccy=cash_by_ccy,
            cash_secured_by_ccy=total_by_ccy,
            native_currency=native_currency,
            exchange_rate_converter=exchange_rate_converter,
            cash_required_native=strike_value * multiplier_value,
            fx_status=portfolio_ctx.get("_sell_put_fx_status"),
        )
    except (TypeError, ValueError):
        return {
            "put_cash_capacity_available": False,
            "put_cash_capacity_reason": "cash_capacity_inputs_invalid",
        }

    native_required = strike_value * multiplier_value
    if cash_headroom.available and cash_headroom.cash_free is not None:
        return {
            "put_cash_required": native_required,
            "put_cash_free": cash_headroom.cash_free,
            "put_cash_capacity_available": True,
            "put_cash_capacity_reason": cash_headroom.reason,
        }
    return {
        "put_cash_required": native_required,
        "put_cash_free": None,
        "put_cash_capacity_available": False,
        "put_cash_capacity_reason": cash_headroom.reason,
    }


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
        multiplier = pd.to_numeric(
            out["multiplier"]
            if "multiplier" in out
            else pd.Series(index=out.index, dtype=float),
            errors="coerce",
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
    cash_secured_unavailable_reason = _cash_secured_context_unavailable_reason(
        option_ctx
    )
    total_by_ccy_norm: dict[str, float] = {}

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
    df_sp_lab['capacity_source_observed_at'] = capacity_authority.get(
        "source_observed_at", pd.NA
    )
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
            multiplier_value = float(row.get('multiplier'))
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
        available_headroom = _effective_cash_in_native_currency(
            cash_by_ccy=cash_by_ccy,
            cash_secured_by_ccy={},
            native_currency=native_currency,
            exchange_rate_converter=exchange_rate_converter,
            cash_required_native=required_native,
            fx_status=fx_status,
        )
        capacity = sell_put_opening_capacity_inputs(
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
        df_sp_lab.at[idx, 'max_new_contracts'] = int(
            free_cash // required_native
        )
        if available_headroom.available and available_headroom.cash_free is not None:
            df_sp_lab.at[idx, 'cash_available_effective_native'] = available_headroom.cash_free
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
                available_headroom.cash_free,
                from_ccy=native_currency,
                to_ccy='CNY',
            )
            if available_headroom.available
            and available_headroom.cash_free is not None
            else None
        )
        if free_cny is not None:
            df_sp_lab.at[idx, 'cash_free_total_cny'] = free_cny
        if available_cny is not None:
            df_sp_lab.at[idx, 'cash_available_total_cny'] = available_cny

    return df_sp_lab
