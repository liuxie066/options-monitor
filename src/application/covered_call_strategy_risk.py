from __future__ import annotations

from typing import Any, Callable

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
    normalize_underwriting_strategy,
)
from domain.domain.sell_call_config import validate_min_strike_cost_multiplier
from src.application.short_vol_risk_context import build_portfolio_risk_context
from domain.domain.strategy_core import ResolvedStrategy, calculate_constraints, evaluate_candidates, resolve_strategy, rank_candidates
from src.application.candidate_scanning import attach_strategy_identity, resolve_scan_strategy
from src.infrastructure.exchange_rates import CurrencyConverter
from src.application.payload_helpers import config_float_from_sources as _float_setting_from_sources
from src.application.payload_helpers import config_optional_float as _optional_float_setting


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
        min_strike_cost_multiplier=(
            validate_min_strike_cost_multiplier(
                cfg.get("min_strike_cost_multiplier", 1.02), source="sell_call.min_strike_cost_multiplier",
            ) if strategy == INSURANCE_UNDERWRITING_PROFILE else 1.02
        ),
    )


def enrich_and_filter_covered_call_underwriting(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_call_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    decision_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
    resolved_strategy: ResolvedStrategy | None = None,
) -> pd.DataFrame:
    if df_labeled is None or df_labeled.empty:
        if resolved_strategy is not None:
            resolve_scan_strategy(
                mode="call", cfg=resolved_strategy.config, symbol=symbol,
                portfolio_ctx=portfolio_ctx, resolved_strategy=resolved_strategy,
            )
        return df_labeled

    cfg = (resolved_strategy.config if resolved_strategy is not None
           else resolve_covered_call_underwriting_config(sell_call_cfg))
    strategy = resolve_scan_strategy(
        mode="call", cfg=cfg, symbol=symbol, portfolio_ctx=portfolio_ctx, resolved_strategy=resolved_strategy,
    )
    if strategy is not None:
        cfg = strategy.config

    risk_ctx = build_portfolio_risk_context(portfolio_ctx=portfolio_ctx, exchange_rate_converter=exchange_rate_converter)
    out = df_labeled.copy()
    if strategy is not None:
        out["strategy_core"] = pd.Series([None] * len(out), index=out.index, dtype=object)
    keep_mask: list[bool] = []
    decision_records: list[dict[str, Any]] = []

    for idx, row in df_labeled.iterrows():
        row_payload = calculate_constraints(
            row.to_dict(), mode="call", risk_ctx=risk_ctx,
            exchange_rate_converter=exchange_rate_converter,
            strategy=strategy, min_strike=cfg.min_strike,
            min_strike_cost_multiplier=(
                cfg.min_strike_cost_multiplier if strategy is not None
                else sell_call_cfg.get("min_strike_cost_multiplier", 1.02)
            ),
        )
        if strategy is not None:
            row_payload = attach_strategy_identity(row_payload, strategy)
            out.at[idx, "strategy_core"] = row_payload["strategy_core"]
        for key in (
            "effective_min_strike", "covered_notional_cny", "net_income_cny", "option_contract_point_value_cny",
            "position_sizing_basis", "symbol_concentration_current",
            "symbol_concentration_after_existing_assignments", "symbol_concentration_after",
            "portfolio_nav_cny", "portfolio_nav_after_existing_assignments_cny",
            "portfolio_nav_after_candidate_assignment_cny", "concentration_unavailable_reason",
            "concentration_evaluable", "portfolio_risk_warnings",
        ):
            if key in row_payload:
                out.loc[idx, key] = row_payload.get(key)
        for key, value in row_payload.items():
            if key.startswith("policy_") or key == "effective_min_strike":
                out.loc[idx, key] = value
        if not cfg.enabled:
            keep_mask.append(True)
            continue
        decision = evaluate_covered_call_underwriting_row(row_payload, cfg=cfg, resolved_strategy=strategy)
        for key, value in decision.get("fields", {}).items():
            out.loc[idx, key] = value
        opening_decision = dict(decision.get("opening_decision") or {})
        normalized_input = dict(
            opening_decision.get("normalized_input") or row_payload
        )
        decision_records.append(
            {
                "normalized_input": normalized_input,
                "opening_decision": opening_decision,
            }
        )
        if decision["accepted"]:
            keep_mask.append(True)
            continue
        keep_mask.append(False)

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty and cfg.enabled:
        filtered = pd.DataFrame(rank_candidates(filtered.to_dict("records"), mode="call", strategy=strategy))
    if decision_sink_fn is not None and cfg.enabled:
        decision_sink_fn(decision_records)
    return filtered


def evaluate_covered_call_underwriting_row(
    row: dict[str, Any],
    *,
    cfg: InsuranceUnderwritingConfig,
    resolved_strategy: ResolvedStrategy | None = None,
) -> dict[str, Any]:
    if resolved_strategy is None and not cfg.enabled:
        from domain.domain.insurance_underwriting import evaluate_underwriting_candidate

        return evaluate_underwriting_candidate(row, mode="call", cfg=cfg)
    strategy = resolved_strategy or resolve_strategy(mode="call", config=cfg)
    if strategy.mode != "call":
        raise ValueError("resolved strategy mode conflicts with underwriting")
    return evaluate_candidates([row], strategy=strategy)["decisions"][0]
