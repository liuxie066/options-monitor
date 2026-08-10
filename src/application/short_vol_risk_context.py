from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.option_position_identity import normalize_currency
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.infrastructure.exchange_rates import CurrencyConverter


PortfolioRiskContext = ShortVolPortfolioContext


def build_portfolio_risk_context(
    *,
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> PortfolioRiskContext:
    if not isinstance(portfolio_ctx, dict):
        return PortfolioRiskContext(
            nav_cny=None,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=None,
            unavailable_reasons=("holdings_context_missing",),
        )

    global_portfolio = portfolio_ctx.get("_global_portfolio_ctx")
    holdings_ctx = global_portfolio if isinstance(global_portfolio, dict) else portfolio_ctx
    option_ctx = portfolio_ctx.get("_global_option_ctx")
    if not isinstance(option_ctx, dict):
        option_ctx = portfolio_ctx.get("option_ctx") if isinstance(portfolio_ctx.get("option_ctx"), dict) else {}

    unavailable: list[str] = []
    warnings: list[str] = []
    nav_cny = 0.0

    cash_by_currency = holdings_ctx.get("cash_by_currency") if isinstance(holdings_ctx, dict) else {}
    if isinstance(cash_by_currency, dict):
        for ccy, raw_amount in cash_by_currency.items():
            amount_cny = amount_to_cny(raw_amount, ccy, exchange_rate_converter=exchange_rate_converter)
            if amount_cny is None:
                unavailable.append(f"cash_fx_missing:{normalize_currency(ccy) or ccy}")
                continue
            nav_cny += float(amount_cny)

    stock_value_by_symbol: dict[str, float] = {}
    stocks = holdings_ctx.get("stocks_by_symbol") if isinstance(holdings_ctx, dict) else {}
    if isinstance(stocks, dict):
        for raw_symbol, raw_stock in stocks.items():
            if not isinstance(raw_stock, dict):
                continue
            symbol = canonical_symbol(raw_stock.get("symbol") or raw_symbol)
            shares = _float(raw_stock.get("shares") or raw_stock.get("quantity"))
            if not symbol or shares is None or shares <= 0:
                continue
            value_cny, basis, reason = _stock_value_cny(
                symbol=symbol,
                stock=raw_stock,
                shares=shares,
                exchange_rate_converter=exchange_rate_converter,
            )
            if value_cny is None:
                unavailable.append(reason or f"stock_value_missing:{symbol}")
                continue
            if basis == "avg_cost":
                warnings.append(f"stock_value_estimated_from_avg_cost:{symbol}")
            nav_cny += float(value_cny)
            stock_value_by_symbol[symbol] = stock_value_by_symbol.get(symbol, 0.0) + float(value_cny)

    short_put_by_symbol, short_put_total, short_put_unavailable = _short_put_assignment_from_option_ctx(
        option_ctx,
        exchange_rate_converter=exchange_rate_converter,
    )
    unavailable.extend(short_put_unavailable)

    return PortfolioRiskContext(
        nav_cny=nav_cny if nav_cny > 0 else None,
        stock_value_cny_by_symbol=stock_value_by_symbol,
        short_put_assignment_cny_by_symbol=short_put_by_symbol,
        short_put_assignment_total_cny=short_put_total,
        unavailable_reasons=tuple(sorted(set(unavailable))),
        warnings=tuple(sorted(set(warnings))),
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


def _stock_value_cny(
    *,
    symbol: str,
    stock: dict[str, Any],
    shares: float,
    exchange_rate_converter: CurrencyConverter,
) -> tuple[float | None, str | None, str | None]:
    value_cny = _first_float(stock, "market_value_cny", "market_val_cny", "value_cny")
    if value_cny is not None and value_cny >= 0:
        return value_cny, "market_value_cny", None

    ccy = normalize_currency(stock.get("currency")) or symbol_currency(symbol)
    market_value = _first_float(stock, "market_value", "market_val", "value", "amount")
    if market_value is not None and market_value >= 0:
        converted = amount_to_cny(market_value, ccy, exchange_rate_converter=exchange_rate_converter)
        return converted, "market_value", None if converted is not None else f"stock_value_fx_missing:{symbol}:{ccy}"

    price = _first_float(stock, "market_price", "latest_price", "last_price", "price", "close_price", "spot")
    if price is not None and price > 0:
        converted = amount_to_cny(price * shares, ccy, exchange_rate_converter=exchange_rate_converter)
        return converted, "market_price", None if converted is not None else f"stock_price_fx_missing:{symbol}:{ccy}"

    # ``avg_cost`` is strictly average acquisition cost.  Do not accept
    # OpenD ``cost_price`` here because securities accounts define it as
    # diluted cost.
    avg_cost = _first_float(stock, "avg_cost", "average_cost")
    if avg_cost is not None and avg_cost > 0:
        converted = amount_to_cny(avg_cost * shares, ccy, exchange_rate_converter=exchange_rate_converter)
        return converted, "avg_cost", None if converted is not None else f"stock_avg_cost_fx_missing:{symbol}:{ccy}"

    return None, None, f"stock_value_missing:{symbol}"


def _short_put_assignment_from_option_ctx(
    option_ctx: dict[str, Any],
    *,
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
    if isinstance(option_ctx.get("cash_secured_unavailable_by_symbol"), dict):
        for raw_symbol, reason in option_ctx.get("cash_secured_unavailable_by_symbol", {}).items():
            unavailable.append(f"{canonical_symbol(raw_symbol) or raw_symbol}:{reason}")
    return by_symbol, total_cny, unavailable


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        parsed = float(value)
    except Exception:
        return None
    try:
        if parsed != parsed:
            return None
    except Exception:
        pass
    return parsed
