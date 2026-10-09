from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.cash_secured_utils import cash_secured_unavailable_for_cash_snapshot
from domain.domain.option_position_identity import normalize_currency
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from domain.domain.portfolio_assignment_scenario import project_non_option_assignment_assets
from src.application.portfolio_context_service import cash_snapshot_is_usable
from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.infrastructure.exchange_rates import CurrencyConverter
from src.application.numeric_helpers import float_or_none as _float


PortfolioRiskContext = ShortVolPortfolioContext


def build_portfolio_risk_context(
    *,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> PortfolioRiskContext:
    context = portfolio_ctx if isinstance(portfolio_ctx, dict) else {}
    evidence = context.get("position_sizing_evidence")
    option_ctx = context.get("option_ctx") if isinstance(context.get("option_ctx"), dict) else {}
    positions = (
        option_ctx.get("assignment_positions")
        if (option_ctx.get("context_status") == "available" and option_ctx.get("decision_snapshot_status") == "trusted")
        else None
    )
    if not isinstance(positions, list) or any(not isinstance(row, dict) or not row.get("account") for row in positions):
        positions = None
    scope = evidence.get("scope") if isinstance(evidence, dict) else None
    accounts = scope.get("accounts") if isinstance(scope, dict) else None
    account = accounts[0] if isinstance(accounts, list) and len(accounts) == 1 else None
    short_put, total, reasons = _short_put_assignment_from_option_ctx(
        option_ctx,
        portfolio_ctx=context,
        exchange_rate_converter=exchange_rate_converter,
    )
    baseline = project_non_option_assignment_assets(
        accounts=[account] if account else [], portfolio_evidence=evidence or {}, option_positions=[]
    )
    if not isinstance(evidence, dict):
        reasons.append("position_sizing_evidence_missing")
        if not cash_snapshot_is_usable(context):
            reasons.append("broker_cash_snapshot_unavailable")
        snapshot = context.get("position_snapshot_input")
        if not isinstance(snapshot, dict) or snapshot.get("completeness") != "complete":
            reasons.append("broker_positions_unavailable")
    else:
        reasons.extend(baseline.get("unavailable_reasons") or [])
    return PortfolioRiskContext(
        nav_cny=_float(baseline.get("net_assets_cny")),
        stock_value_cny_by_symbol={
            code: _float(value)
            for code, value in baseline.get("stock_value_cny_by_symbol", {}).items()
            if _float(value) is not None
        },
        short_put_assignment_cny_by_symbol=short_put,
        short_put_assignment_total_cny=total,
        unavailable_reasons=tuple(reasons),
        account=account,
        portfolio_evidence=evidence if isinstance(evidence, dict) else None,
        assignment_positions=tuple(positions) if isinstance(positions, list) else None,
        warnings=tuple(evidence.get("warnings") or []) if isinstance(evidence, dict) else (),
    )


def amount_to_cny(
    value: Any,
    ccy: Any,
    *,
    exchange_rate_converter: CurrencyConverter,
) -> float | None:
    amount = _float(value)
    if amount is None:
        return None
    currency = normalize_currency(ccy)
    if currency in {"CNY", "RMB"}:
        return float(amount)
    if not currency:
        return None
    converted = exchange_rate_converter.native_to_cny(float(amount), native_ccy=currency)
    return float(converted) if converted is not None else None


def enrich_short_vol_contract_cny_fields(
    row: dict[str, Any],
    *,
    exchange_rate_converter: CurrencyConverter,
) -> dict[str, float]:
    ccy = normalize_currency(row.get("currency") or row.get("option_ccy")) or symbol_currency(row.get("symbol"))
    fields: dict[str, float] = {}
    if _float(row.get("net_income_cny")) is None:
        net_income = _first_float(row, "net_income", "net_credit")
        net_income_cny = amount_to_cny(net_income, ccy, exchange_rate_converter=exchange_rate_converter)
        if net_income_cny is not None:
            fields["net_income_cny"] = net_income_cny
    if _float(row.get("option_contract_point_value_cny")) is None:
        multiplier = _first_float(row, "multiplier", "option_contract_multiplier", "option_contract_size")
        point_value_cny = amount_to_cny(multiplier, ccy, exchange_rate_converter=exchange_rate_converter)
        if point_value_cny is not None:
            fields["option_contract_point_value_cny"] = point_value_cny
    return fields


def _short_put_assignment_from_option_ctx(
    option_ctx: dict[str, Any],
    *,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> tuple[dict[str, float], float | None, list[str]]:
    unavailable: list[str] = []
    by_symbol: dict[str, float] = {}
    raw_by_symbol = option_ctx.get("cash_secured_by_symbol_by_ccy") if isinstance(option_ctx, dict) else {}
    if isinstance(raw_by_symbol, dict):
        for raw_symbol, by_ccy in raw_by_symbol.items():
            symbol = canonical_symbol(raw_symbol)
            if not symbol or not isinstance(by_ccy, dict):
                continue
            total = 0.0
            ok = True
            for ccy, amount in by_ccy.items():
                converted = amount_to_cny(amount, ccy, exchange_rate_converter=exchange_rate_converter)
                if converted is None:
                    unavailable.append(f"short_put_assignment_fx_missing:{symbol}:{normalize_currency(ccy) or ccy}")
                    ok = False
                    continue
                total += float(converted)
            if ok:
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + total

    total_cny = _float(option_ctx.get("cash_secured_total_cny")) if isinstance(option_ctx, dict) else None
    if total_cny is None:
        total_by_ccy = option_ctx.get("cash_secured_total_by_ccy") if isinstance(option_ctx, dict) else {}
        if isinstance(total_by_ccy, dict):
            total_cny = 0.0
            for ccy, amount in total_by_ccy.items():
                converted = amount_to_cny(amount, ccy, exchange_rate_converter=exchange_rate_converter)
                if converted is None:
                    unavailable.append(f"short_put_total_fx_missing:{normalize_currency(ccy) or ccy}")
                    total_cny = None
                    break
                total_cny += float(converted)
    if total_cny is None and by_symbol:
        total_cny = sum(by_symbol.values())
    unresolved = cash_secured_unavailable_for_cash_snapshot(option_ctx, portfolio_ctx)
    if isinstance(unresolved, dict):
        for raw_symbol, reason in unresolved.items():
            unavailable.append(f"{canonical_symbol(raw_symbol) or raw_symbol}:{reason}")
    elif unresolved is not None:
        unavailable.append(str(unresolved))
    return by_symbol, total_cny, unavailable


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None
