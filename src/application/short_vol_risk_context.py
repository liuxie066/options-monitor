from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.cash_secured_utils import cash_secured_unavailable_for_cash_snapshot, normalize_cash_secured_total_by_ccy
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from domain.domain.portfolio_assignment_scenario import project_non_option_assignment_assets
from src.application.portfolio_context_service import cash_snapshot_is_usable
from domain.domain.symbol_identity import canonical_symbol
from domain.domain.currency_conversion import amount_to_cny, enrich_short_vol_contract_cny_fields
from src.infrastructure.exchange_rates import CurrencyConverter
from src.application.numeric_helpers import float_or_none as _float
from src.application.cash_totals import sum_by_currency_to_cny


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
        warnings=tuple(evidence.get("warnings") or []) + tuple(
            f"closed_market_quote:{quote.get('market')}:{quote.get('observed_at')}"
            for quote in evidence.get("quotes") or []
            if isinstance(quote, dict) and quote.get("valuation_quality") == "closed_market_carried"
        ) if isinstance(evidence, dict) else (),
    )


def _short_put_assignment_from_option_ctx(
    option_ctx: dict[str, Any],
    *,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> tuple[dict[str, float], float | None, list[str]]:
    unavailable: list[str] = []
    by_symbol: dict[str, float] = {}
    cny_rates = {
        "usdcny_exchange_rate": exchange_rate_converter.native_to_cny(1, native_ccy="USD"),
        "cny_per_hkd_exchange_rate": exchange_rate_converter.native_to_cny(1, native_ccy="HKD"),
    }
    raw_by_symbol = option_ctx.get("cash_secured_by_symbol_by_ccy") if isinstance(option_ctx, dict) else {}
    if isinstance(raw_by_symbol, dict):
        for raw_symbol, by_ccy in raw_by_symbol.items():
            symbol = canonical_symbol(raw_symbol)
            if not symbol or not isinstance(by_ccy, dict):
                continue
            total = sum_by_currency_to_cny(by_ccy, **cny_rates)
            if total is None:
                unavailable.append(f"short_put_assignment_fx_missing:{symbol}")
            else:
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + total

    total_cny = sum_by_currency_to_cny(normalize_cash_secured_total_by_ccy(option_ctx), **cny_rates)
    if total_cny is None:
        unavailable.append("short_put_total_fx_missing")
    unresolved = cash_secured_unavailable_for_cash_snapshot(option_ctx, portfolio_ctx)
    if isinstance(unresolved, dict):
        for raw_symbol, reason in unresolved.items():
            symbol = canonical_symbol(raw_symbol) or raw_symbol
            unavailable.append(f"{symbol}:{reason}")
            by_symbol.pop(symbol, None)
    elif unresolved is not None:
        unavailable.append(str(unresolved))
        by_symbol = {}
    if unavailable:
        total_cny = None
    return by_symbol, total_cny, unavailable
