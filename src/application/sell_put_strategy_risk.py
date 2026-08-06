from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE,
    InsuranceUnderwritingConfig,
    evaluate_underwriting_candidate,
    normalize_underwriting_strategy,
    rank_underwriting_candidates,
)
from domain.domain.short_vol_assessment import portfolio_concentration_fields
from src.application.short_vol_risk_context import (
    build_portfolio_risk_context,
    enrich_short_vol_contract_cny_fields,
)
from src.infrastructure.exchange_rates import CurrencyConverter


def resolve_sell_put_underwriting_config(raw: dict[str, Any] | None) -> InsuranceUnderwritingConfig:
    cfg = raw if isinstance(raw, dict) else {}
    raw_strategy = cfg.get("strategy") or cfg.get("strategy_profile")
    strategy = normalize_underwriting_strategy(raw_strategy)
    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    window = resolve_candidate_window(cfg, defaults=DEFAULT_SELL_PUT_WINDOW)
    liquidity = resolve_candidate_liquidity(
        cfg.get("liquidity") if isinstance(cfg.get("liquidity"), dict) else None,
        defaults=DEFAULT_CANDIDATE_LIQUIDITY,
    )

    return InsuranceUnderwritingConfig(
        strategy=strategy,
        min_annualized_return=_float_setting_from_sources("min_annualized_net_return", 0.10, pricing, cfg),
        min_net_income=_float_setting_from_sources("min_net_income", 50.0, pricing, cfg),
        min_iv_rv_ratio=_float_setting_from_sources("min_iv_rv_ratio", 1.10, pricing, cfg),
        min_iv_minus_rv=_float_setting_from_sources("min_iv_minus_rv", 0.05, pricing, cfg),
        min_strike=_optional_float_setting(cfg, "min_strike"),
        max_strike=_optional_float_setting(cfg, "max_strike"),
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        max_spread_ratio=_float_setting_from_sources(
            "max_spread_ratio",
            liquidity.max_spread_ratio,
            pricing,
            cfg,
        ),
    )


def enrich_and_filter_sell_put_underwriting(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_put_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> pd.DataFrame:
    if df_labeled is None or df_labeled.empty:
        return df_labeled

    cfg = resolve_sell_put_underwriting_config(sell_put_cfg)
    if not cfg.enabled:
        return df_labeled

    risk_ctx = build_portfolio_risk_context(
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
    )
    out = df_labeled.copy()
    keep_mask: list[bool] = []

    for idx, row in out.iterrows():
        row_payload = row.to_dict()
        row_payload.update(
            enrich_short_vol_contract_cny_fields(
                row_payload,
                exchange_rate_converter=exchange_rate_converter,
            )
        )
        row_payload.update(portfolio_concentration_fields(row_payload, mode="put", risk_ctx=risk_ctx))
        for key in (
            "net_income_cny",
            "option_contract_point_value_cny",
            "portfolio_nav_cny",
            "assignment_notional_cny",
            "existing_stock_value_cny_symbol",
            "existing_short_put_assignment_cny_symbol",
            "existing_short_put_assignment_cny_total",
            "single_trade_concentration",
            "symbol_concentration_after",
            "total_short_put_concentration_after",
            "concentration_score",
            "concentration_evaluable",
            "concentration_unavailable_reason",
            "portfolio_risk_warnings",
        ):
            if key in row_payload:
                out.loc[idx, key] = row_payload.get(key)
        decision = evaluate_sell_put_underwriting_row(row_payload, cfg=cfg)
        for key, value in decision.get("fields", {}).items():
            out.loc[idx, key] = value
        if decision["accepted"]:
            keep_mask.append(True)
            continue
        keep_mask.append(False)

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty:
        filtered = pd.DataFrame(rank_underwriting_candidates(filtered.to_dict("records"), mode="put", cfg=cfg))
    return filtered


def evaluate_sell_put_underwriting_row(
    row: dict[str, Any],
    *,
    cfg: InsuranceUnderwritingConfig,
) -> dict[str, Any]:
    return evaluate_underwriting_candidate(row, mode="put", cfg=cfg)


def _float_setting(raw: dict[str, Any], key: str, default: float) -> float:
    try:
        value = raw.get(key, default)
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _float_setting_from_sources(key: str, default: float, *sources: dict[str, Any]) -> float:
    for source in sources:
        if not isinstance(source, dict) or key not in source:
            continue
        return _float_setting(source, key, default)
    return float(default)


def _optional_float_setting(raw: dict[str, Any], key: str) -> float | None:
    try:
        value = raw.get(key)
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None
