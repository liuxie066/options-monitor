from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from domain.domain.ledger.position_fields import normalize_currency
from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.infrastructure.exchange_rates import CurrencyConverter


RETURN_FIRST_STRATEGY = "return_first"
SHORT_VOL_STRATEGY = "short_vol"


@dataclass(frozen=True)
class SellPutShortVolConfig:
    strategy: str = RETURN_FIRST_STRATEGY
    min_iv_rv_ratio: float = 1.15
    min_iv_minus_rv: float = 0.05
    min_abs_delta: float = 0.15
    max_abs_delta: float = 0.30
    target_abs_delta: float = 0.20
    max_single_trade_nav_pct: float = 0.08
    max_symbol_nav_pct: float = 0.20
    max_total_short_put_nav_pct: float = 0.50

    @property
    def enabled(self) -> bool:
        return self.strategy == SHORT_VOL_STRATEGY


@dataclass(frozen=True)
class PortfolioRiskContext:
    nav_cny: float | None
    stock_value_cny_by_symbol: dict[str, float]
    short_put_assignment_cny_by_symbol: dict[str, float]
    short_put_assignment_total_cny: float | None
    unavailable_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def resolve_sell_put_short_vol_config(raw: dict[str, Any] | None) -> SellPutShortVolConfig:
    cfg = raw if isinstance(raw, dict) else {}
    strategy = str(cfg.get("strategy") or cfg.get("strategy_profile") or RETURN_FIRST_STRATEGY).strip().lower()
    if strategy in {"", "legacy", "yield_first", "return"}:
        strategy = RETURN_FIRST_STRATEGY
    if strategy not in {RETURN_FIRST_STRATEGY, SHORT_VOL_STRATEGY}:
        strategy = RETURN_FIRST_STRATEGY

    short_vol = cfg.get("short_vol") if isinstance(cfg.get("short_vol"), dict) else {}
    concentration = cfg.get("concentration") if isinstance(cfg.get("concentration"), dict) else {}

    return SellPutShortVolConfig(
        strategy=strategy,
        min_iv_rv_ratio=_float_setting(short_vol, "min_iv_rv_ratio", 1.15),
        min_iv_minus_rv=_float_setting(short_vol, "min_iv_minus_rv", 0.05),
        min_abs_delta=_float_setting(short_vol, "min_abs_delta", 0.15),
        max_abs_delta=_float_setting(short_vol, "max_abs_delta", 0.30),
        target_abs_delta=_float_setting(short_vol, "target_abs_delta", 0.20),
        max_single_trade_nav_pct=_float_setting(concentration, "max_single_trade_nav_pct", 0.08),
        max_symbol_nav_pct=_float_setting(concentration, "max_symbol_nav_pct", 0.20),
        max_total_short_put_nav_pct=_float_setting(concentration, "max_total_short_put_nav_pct", 0.50),
    )


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
            amount_cny = _amount_to_cny(raw_amount, ccy, exchange_rate_converter=exchange_rate_converter)
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


def enrich_and_filter_sell_put_short_vol(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_put_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    out_path: Any,
) -> pd.DataFrame:
    if df_labeled is None or df_labeled.empty:
        return df_labeled

    cfg = resolve_sell_put_short_vol_config(sell_put_cfg)
    if not cfg.enabled:
        return df_labeled

    risk_ctx = build_portfolio_risk_context(
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
    )
    out = df_labeled.copy()
    reject_rows: list[dict[str, Any]] = []
    keep_mask: list[bool] = []
    scope = infer_trace_scope_from_path(out_path)

    for idx, row in out.iterrows():
        decision = evaluate_sell_put_short_vol_row(row.to_dict(), cfg=cfg, risk_ctx=risk_ctx)
        for key, value in decision.get("fields", {}).items():
            out.loc[idx, key] = value
        if decision["accepted"]:
            keep_mask.append(True)
            continue
        keep_mask.append(False)
        reject_rows.append(
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account"),
                symbol=row.get("symbol") or symbol,
                function="sell_put",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule=decision["rule"],
                metric_value=decision.get("metric_value"),
                threshold=decision.get("threshold"),
                contract_symbol=row.get("contract_symbol"),
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=decision.get("message") or "short-vol strategy risk filter",
                evidence_path=getattr(out_path, "name", str(out_path)),
                config_values={
                    "strategy": cfg.strategy,
                    "min_iv_rv_ratio": cfg.min_iv_rv_ratio,
                    "min_iv_minus_rv": cfg.min_iv_minus_rv,
                    "min_abs_delta": cfg.min_abs_delta,
                    "max_abs_delta": cfg.max_abs_delta,
                    "max_single_trade_nav_pct": cfg.max_single_trade_nav_pct,
                    "max_symbol_nav_pct": cfg.max_symbol_nav_pct,
                    "max_total_short_put_nav_pct": cfg.max_total_short_put_nav_pct,
                },
            )
        )

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty:
        try:
            from domain.domain.engine import CandidateScoreWeights, rank_candidate_rows

            weights = _score_weights_from_sell_put_cfg(sell_put_cfg)
            filtered = pd.DataFrame(rank_candidate_rows(filtered.to_dict("records"), mode="put", score_weights=weights))
        except Exception:
            pass
    try:
        filtered.to_csv(out_path, index=False)
    except Exception:
        pass
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(out_path), reject_rows)
    return filtered


def evaluate_sell_put_short_vol_row(
    row: dict[str, Any],
    *,
    cfg: SellPutShortVolConfig,
    risk_ctx: PortfolioRiskContext,
) -> dict[str, Any]:
    fields = _short_vol_fields(row, cfg=cfg, risk_ctx=risk_ctx)

    if fields["realized_volatility_estimate"] is None:
        return _reject_decision("volatility_estimate_missing", fields, None, "rv_est", "realized volatility estimate missing")
    if fields["implied_volatility"] is None:
        return _reject_decision("implied_volatility_missing", fields, None, "iv", "implied volatility missing")
    if fields["iv_rv_ratio"] is None or fields["iv_rv_ratio"] < cfg.min_iv_rv_ratio:
        return _reject_decision(
            "vol_edge_ratio_below_min",
            fields,
            fields["iv_rv_ratio"],
            cfg.min_iv_rv_ratio,
            "IV/RV ratio below short-vol threshold",
        )
    if fields["iv_minus_rv"] is None or fields["iv_minus_rv"] < cfg.min_iv_minus_rv:
        return _reject_decision(
            "vol_edge_spread_below_min",
            fields,
            fields["iv_minus_rv"],
            cfg.min_iv_minus_rv,
            "IV-RV spread below short-vol threshold",
        )

    abs_delta = fields["abs_delta"]
    if abs_delta is None:
        return _reject_decision("delta_missing", fields, None, [cfg.min_abs_delta, cfg.max_abs_delta], "delta missing")
    if abs_delta < cfg.min_abs_delta:
        return _reject_decision("delta_below_target_band", fields, abs_delta, cfg.min_abs_delta, "abs(delta) below band")
    if abs_delta > cfg.max_abs_delta:
        return _reject_decision("delta_above_target_band", fields, abs_delta, cfg.max_abs_delta, "abs(delta) above band")

    if fields["concentration_evaluable"] is not True:
        return _reject_decision(
            "concentration_not_evaluable",
            fields,
            None,
            "holdings_nav",
            ";".join(risk_ctx.unavailable_reasons) or "portfolio concentration unavailable",
        )
    if fields["single_trade_concentration"] is None or fields["single_trade_concentration"] > cfg.max_single_trade_nav_pct:
        return _reject_decision(
            "single_trade_concentration_exceeded",
            fields,
            fields["single_trade_concentration"],
            cfg.max_single_trade_nav_pct,
            "single candidate assignment notional exceeds NAV cap",
        )
    if fields["symbol_concentration_after"] is None or fields["symbol_concentration_after"] > cfg.max_symbol_nav_pct:
        return _reject_decision(
            "symbol_concentration_exceeded",
            fields,
            fields["symbol_concentration_after"],
            cfg.max_symbol_nav_pct,
            "symbol concentration after candidate exceeds NAV cap",
        )
    if fields["total_short_put_concentration_after"] is None or fields["total_short_put_concentration_after"] > cfg.max_total_short_put_nav_pct:
        return _reject_decision(
            "total_short_put_concentration_exceeded",
            fields,
            fields["total_short_put_concentration_after"],
            cfg.max_total_short_put_nav_pct,
            "total short-put assignment obligation exceeds NAV cap",
        )
    return {"accepted": True, "rule": "short_vol_candidate_accepted", "fields": fields}


def _short_vol_fields(
    row: dict[str, Any],
    *,
    cfg: SellPutShortVolConfig,
    risk_ctx: PortfolioRiskContext,
) -> dict[str, Any]:
    symbol = canonical_symbol(row.get("symbol"))
    iv = _float(row.get("implied_volatility"))
    rv = _first_float(
        row,
        "realized_volatility_estimate",
        "rv_estimate",
        "rv_est",
        "rv_60",
        "realized_volatility_60",
    )
    delta = _float(row.get("delta"))
    abs_delta = abs(delta) if delta is not None else None
    iv_rv_ratio = (iv / rv) if (iv is not None and rv is not None and rv > 0) else None
    iv_minus_rv = (iv - rv) if (iv is not None and rv is not None) else None

    assignment = _candidate_assignment_notional_cny(row, exchange_rate_converter=None)
    if assignment is None:
        # The fallback below needs the converter, but this function intentionally
        # stays pure. `cash_required_cny` is produced by the cash enrichment step.
        assignment = _float(row.get("cash_required_cny"))

    nav = risk_ctx.nav_cny
    existing_stock = risk_ctx.stock_value_cny_by_symbol.get(symbol or "", 0.0)
    existing_short_put = risk_ctx.short_put_assignment_cny_by_symbol.get(symbol or "", 0.0)
    existing_total_short_put = risk_ctx.short_put_assignment_total_cny

    concentration_evaluable = bool(
        nav is not None
        and nav > 0
        and assignment is not None
        and assignment > 0
        and existing_total_short_put is not None
        and not risk_ctx.unavailable_reasons
    )
    single_trade = (assignment / nav) if concentration_evaluable and nav else None
    symbol_after = ((existing_stock + existing_short_put + assignment) / nav) if concentration_evaluable and nav else None
    total_after = ((existing_total_short_put + assignment) / nav) if concentration_evaluable and nav else None
    delta_quality = None
    if abs_delta is not None:
        tolerance = max(cfg.max_abs_delta - cfg.min_abs_delta, 0.000001)
        delta_quality = max(0.0, 1.0 - (abs(abs_delta - cfg.target_abs_delta) / tolerance))
    vol_edge_score = None
    if iv_rv_ratio is not None and iv_minus_rv is not None:
        ratio_score = min(2.0, max(0.0, iv_rv_ratio - 1.0))
        spread_score = min(2.0, max(0.0, iv_minus_rv))
        vol_edge_score = ratio_score + spread_score
    concentration_score = None
    if symbol_after is not None and total_after is not None:
        concentration_score = max(0.0, 1.0 - max(symbol_after, total_after))

    return {
        "strategy_profile": SHORT_VOL_STRATEGY,
        "implied_volatility": _round_optional(iv),
        "realized_volatility_estimate": _round_optional(rv),
        "iv_rv_ratio": _round_optional(iv_rv_ratio),
        "iv_minus_rv": _round_optional(iv_minus_rv),
        "abs_delta": _round_optional(abs_delta),
        "delta_target_score": _round_optional(delta_quality),
        "vol_edge_score": _round_optional(vol_edge_score),
        "portfolio_nav_cny": _round_optional(nav),
        "assignment_notional_cny": _round_optional(assignment),
        "existing_stock_value_cny_symbol": _round_optional(existing_stock),
        "existing_short_put_assignment_cny_symbol": _round_optional(existing_short_put),
        "existing_short_put_assignment_cny_total": _round_optional(existing_total_short_put),
        "single_trade_concentration": _round_optional(single_trade),
        "symbol_concentration_after": _round_optional(symbol_after),
        "total_short_put_concentration_after": _round_optional(total_after),
        "concentration_score": _round_optional(concentration_score),
        "concentration_evaluable": concentration_evaluable,
        "concentration_unavailable_reason": ";".join(risk_ctx.unavailable_reasons) or pd.NA,
        "portfolio_risk_warnings": ";".join(risk_ctx.warnings) or pd.NA,
    }


def _candidate_assignment_notional_cny(
    row: dict[str, Any],
    *,
    exchange_rate_converter: CurrencyConverter | None,
) -> float | None:
    cash_required = _float(row.get("cash_required_cny"))
    if cash_required is not None and cash_required > 0:
        return cash_required
    if exchange_rate_converter is None:
        return None
    strike = _float(row.get("strike"))
    multiplier = _float(row.get("multiplier"))
    if strike is None or multiplier is None or strike <= 0 or multiplier <= 0:
        return None
    ccy = normalize_currency(row.get("currency")) or symbol_currency(row.get("symbol"))
    return _amount_to_cny(strike * multiplier, ccy, exchange_rate_converter=exchange_rate_converter)


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
        converted = _amount_to_cny(market_value, ccy, exchange_rate_converter=exchange_rate_converter)
        return converted, "market_value", None if converted is not None else f"stock_value_fx_missing:{symbol}:{ccy}"

    price = _first_float(stock, "market_price", "latest_price", "last_price", "price", "close_price", "spot")
    if price is not None and price > 0:
        converted = _amount_to_cny(price * shares, ccy, exchange_rate_converter=exchange_rate_converter)
        return converted, "market_price", None if converted is not None else f"stock_price_fx_missing:{symbol}:{ccy}"

    avg_cost = _first_float(stock, "avg_cost", "cost_price", "average_cost")
    if avg_cost is not None and avg_cost > 0:
        converted = _amount_to_cny(avg_cost * shares, ccy, exchange_rate_converter=exchange_rate_converter)
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
                converted = _amount_to_cny(amount, ccy, exchange_rate_converter=exchange_rate_converter)
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
                converted = _amount_to_cny(amount, ccy, exchange_rate_converter=exchange_rate_converter)
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


def _score_weights_from_sell_put_cfg(raw: dict[str, Any]):
    from domain.domain.engine import CandidateScoreWeights

    weights = raw.get("score_weights") if isinstance(raw.get("score_weights"), dict) else {}

    def get(name: str, default: float) -> float:
        return _float_setting(weights, name, default)

    return CandidateScoreWeights(
        annualized_return=get("annualized_return", 0.40),
        net_income=get("net_income", 0.000001),
        liquidity=get("liquidity", 0.10),
        risk_distance=get("risk_distance", 0.10),
        vol_edge=get("vol_edge", 0.50),
        delta_target=get("delta_target", 0.20),
        concentration=get("concentration", 0.20),
    )


def _reject_decision(rule: str, fields: dict[str, Any], metric_value: Any, threshold: Any, message: str) -> dict[str, Any]:
    return {
        "accepted": False,
        "rule": rule,
        "metric_value": metric_value,
        "threshold": threshold,
        "message": message,
        "fields": fields,
    }


def _amount_to_cny(
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


def _float_setting(raw: dict[str, Any], key: str, default: float) -> float:
    try:
        value = raw.get(key, default)
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _round_optional(value: Any) -> float | None:
    parsed = _float(value)
    if parsed is None:
        return None
    return round(float(parsed), 6)
