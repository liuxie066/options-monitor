from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_CALL_WINDOW,
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
from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.symbol_identity import symbol_currency
from src.application.short_vol_risk_context import amount_to_cny, enrich_short_vol_contract_cny_fields
from src.infrastructure.exchange_rates import CurrencyConverter


def resolve_covered_call_underwriting_config(raw: dict[str, Any] | None) -> InsuranceUnderwritingConfig:
    cfg = raw if isinstance(raw, dict) else {}
    raw_strategy = cfg.get("strategy") or cfg.get("strategy_profile")
    strategy = normalize_underwriting_strategy(raw_strategy)
    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    window = resolve_candidate_window(cfg, defaults=DEFAULT_SELL_CALL_WINDOW)
    liquidity = resolve_candidate_liquidity(
        cfg.get("liquidity") if isinstance(cfg.get("liquidity"), dict) else None,
        defaults=DEFAULT_CANDIDATE_LIQUIDITY,
    )

    return InsuranceUnderwritingConfig(
        strategy=strategy,
        min_annualized_return=_float_setting_from_sources(
            "min_annualized_net_premium_return",
            _float_setting_from_sources("min_annualized_net_return", 0.10, pricing, cfg),
            pricing,
            cfg,
        ),
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


def enrich_and_filter_covered_call_underwriting(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_call_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
) -> pd.DataFrame:
    if df_labeled is None or df_labeled.empty:
        return df_labeled

    cfg = resolve_covered_call_underwriting_config(sell_call_cfg)
    if not cfg.enabled:
        return df_labeled

    _ = portfolio_ctx
    out = df_labeled.copy()
    keep_mask: list[bool] = []

    for idx, row in out.iterrows():
        row_payload = row.to_dict()
        effective_min_strike = resolve_effective_sell_call_min_strike(
            min_strike=sell_call_cfg.get("min_strike"),
            avg_cost=row_payload.get("avg_cost"),
            cost_multiplier=sell_call_cfg.get("min_strike_cost_multiplier", 1.02),
        )
        spot = _float(row_payload.get("spot"))
        if spot is not None:
            effective_min_strike = spot if effective_min_strike is None else max(effective_min_strike, spot)
        if effective_min_strike is not None:
            row_payload["effective_min_strike"] = effective_min_strike
            out.loc[idx, "effective_min_strike"] = effective_min_strike
        row_payload.setdefault(
            "covered_notional_cny",
            _covered_notional_cny(row_payload, exchange_rate_converter=exchange_rate_converter),
        )
        row_payload.update(
            enrich_short_vol_contract_cny_fields(
                row_payload,
                exchange_rate_converter=exchange_rate_converter,
            )
        )
        for key in ("covered_notional_cny", "net_income_cny", "option_contract_point_value_cny"):
            if key in row_payload:
                out.loc[idx, key] = row_payload.get(key)
        decision = evaluate_covered_call_underwriting_row(row_payload, cfg=cfg)
        for key, value in decision.get("fields", {}).items():
            out.loc[idx, key] = value
        if decision["accepted"]:
            keep_mask.append(True)
            continue
        keep_mask.append(False)

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty:
        filtered = pd.DataFrame(rank_underwriting_candidates(filtered.to_dict("records"), mode="call", cfg=cfg))
    return filtered


def evaluate_covered_call_underwriting_row(
    row: dict[str, Any],
    *,
    cfg: InsuranceUnderwritingConfig,
) -> dict[str, Any]:
    return evaluate_underwriting_candidate(row, mode="call", cfg=cfg)


def _covered_notional_cny(
    row: dict[str, Any],
    *,
    exchange_rate_converter: CurrencyConverter,
) -> float | None:
    spot = _float(row.get("spot"))
    multiplier = _float(row.get("multiplier"))
    if spot is None or multiplier is None or spot <= 0 or multiplier <= 0:
        return None
    ccy = row.get("currency") or symbol_currency(row.get("symbol"))
    return amount_to_cny(spot * multiplier, ccy, exchange_rate_converter=exchange_rate_converter)


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
