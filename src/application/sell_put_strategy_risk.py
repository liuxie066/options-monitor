from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE,
    InsuranceUnderwritingConfig,
    evaluate_underwriting_candidate,
    normalize_underwriting_strategy,
    rank_underwriting_candidates,
)
from domain.domain.short_vol_assessment import ShortVolAssessmentConfig
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.application.short_vol_risk_context import (
    PortfolioRiskContext,
    enrich_short_vol_contract_cny_fields,
)
from src.application.strategy_policy import RETURN_FIRST_PROFILE, SHORT_VOL_PROFILE, normalize_strategy_profile
from src.infrastructure.exchange_rates import CurrencyConverter


RETURN_FIRST_STRATEGY = RETURN_FIRST_PROFILE
SHORT_VOL_STRATEGY = SHORT_VOL_PROFILE


@dataclass(frozen=True)
class SellPutShortVolConfig(ShortVolAssessmentConfig):
    strategy: str = SHORT_VOL_STRATEGY

    @property
    def enabled(self) -> bool:
        return self.strategy == SHORT_VOL_STRATEGY


def resolve_sell_put_underwriting_config(raw: dict[str, Any] | None) -> InsuranceUnderwritingConfig:
    cfg = raw if isinstance(raw, dict) else {}
    raw_strategy = cfg.get("strategy") or cfg.get("strategy_profile")
    strategy = normalize_underwriting_strategy(raw_strategy)
    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}

    return InsuranceUnderwritingConfig(
        strategy=strategy,
        min_annualized_return=_float_setting_from_sources("min_annualized_net_return", 0.10, pricing, cfg),
        min_net_income=_float_setting_from_sources("min_net_income", 50.0, pricing, cfg),
        min_iv_rv_ratio=_float_setting_from_sources("min_iv_rv_ratio", 1.10, pricing, cfg),
        min_iv_minus_rv=_float_setting_from_sources("min_iv_minus_rv", 0.05, pricing, cfg),
        reject_event_risk=_bool_setting_from_sources("reject_event_risk", True, pricing, cfg),
        event_source_fail_closed=_bool_setting_from_sources("event_source_fail_closed", True, pricing, cfg),
        premium_score_cap=_float_setting_from_sources("premium_score_cap", 1.5, pricing, cfg),
        min_strike=_optional_float_setting(cfg, "min_strike"),
        max_strike=_optional_float_setting(cfg, "max_strike"),
    )


def resolve_sell_put_short_vol_config(raw: dict[str, Any] | None) -> SellPutShortVolConfig:
    cfg = raw if isinstance(raw, dict) else {}
    raw_strategy = cfg.get("strategy") or cfg.get("strategy_profile")
    strategy = normalize_strategy_profile(raw_strategy)
    short_vol = cfg.get("short_vol") if isinstance(cfg.get("short_vol"), dict) else {}
    concentration = cfg.get("concentration") if isinstance(cfg.get("concentration"), dict) else {}

    return SellPutShortVolConfig(
        strategy=strategy if strategy == SHORT_VOL_STRATEGY else SHORT_VOL_STRATEGY,
        min_iv_rv_ratio=_float_setting_from_sources("min_iv_rv_ratio", 1.10, cfg, short_vol),
        min_iv_minus_rv=_float_setting_from_sources("min_iv_minus_rv", 0.05, cfg, short_vol),
        min_abs_delta=_float_setting(short_vol, "min_abs_delta", 0.15),
        max_abs_delta=_float_setting(short_vol, "max_abs_delta", 0.30),
        target_abs_delta=_float_setting(short_vol, "target_abs_delta", 0.20),
        reject_event_risk=_bool_setting_from_sources("reject_event_risk", True, cfg, short_vol),
        event_source_fail_closed=_bool_setting_from_sources("event_source_fail_closed", True, cfg, short_vol),
        enable_stress_check=_bool_setting(short_vol, "enable_stress_check", True),
        stress_down_sigma_multiple=_float_setting(short_vol, "stress_down_sigma_multiple", 2.0),
        max_put_sigma_stress_loss_nav_pct=_float_setting(short_vol, "max_put_sigma_stress_loss_nav_pct", 0.02),
        gap_down_pct=_float_setting(short_vol, "gap_down_pct", 0.10),
        max_put_gap_down_loss_nav_pct=_float_setting(short_vol, "max_put_gap_down_loss_nav_pct", 0.03),
        call_gap_up_pct=_float_setting(short_vol, "call_gap_up_pct", 0.10),
        max_call_gap_up_opportunity_cost_nav_pct=_float_setting(
            short_vol,
            "max_call_gap_up_opportunity_cost_nav_pct",
            0.02,
        ),
        max_call_gap_up_opportunity_cost_to_premium=_float_setting(
            short_vol,
            "max_call_gap_up_opportunity_cost_to_premium",
            3.0,
        ),
        max_single_trade_nav_pct=_float_setting(concentration, "max_single_trade_nav_pct", 0.08),
        max_symbol_nav_pct=_float_setting(concentration, "max_symbol_nav_pct", 0.20),
        max_total_short_put_nav_pct=_float_setting(concentration, "max_total_short_put_nav_pct", 0.50),
    )


def enrich_and_filter_sell_put_underwriting(
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

    cfg = resolve_sell_put_underwriting_config(sell_put_cfg)
    if not cfg.enabled:
        return df_labeled

    _ = portfolio_ctx
    out = df_labeled.copy()
    reject_rows: list[dict[str, Any]] = []
    keep_mask: list[bool] = []
    scope = infer_trace_scope_from_path(out_path)

    for idx, row in out.iterrows():
        row_payload = row.to_dict()
        row_payload.update(
            enrich_short_vol_contract_cny_fields(
                row_payload,
                exchange_rate_converter=exchange_rate_converter,
            )
        )
        for key in ("net_income_cny", "option_contract_point_value_cny"):
            if key in row_payload:
                out.loc[idx, key] = row_payload.get(key)
        decision = evaluate_sell_put_underwriting_row(row_payload, cfg=cfg)
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
                strategy_family="sell_put",
                strategy_profile=INSURANCE_UNDERWRITING_PROFILE,
                status="post_filtered",
                stage="post_filter",
                rule=decision["rule"],
                metric_value=decision.get("metric_value"),
                threshold=decision.get("threshold"),
                contract_symbol=row.get("contract_symbol"),
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=decision.get("message") or "insurance underwriting strategy filter",
                evidence_path=getattr(out_path, "name", str(out_path)),
                replay_fields={**row_payload, **dict(decision.get("fields") or {})},
                config_values={
                    "strategy": INSURANCE_UNDERWRITING_PROFILE,
                    "strategy_family": "sell_put",
                    "strategy_profile": INSURANCE_UNDERWRITING_PROFILE,
                    "legacy_strategy_profile": SHORT_VOL_STRATEGY,
                    "min_annualized_return": cfg.min_annualized_return,
                    "min_net_income": cfg.min_net_income,
                    "min_iv_rv_ratio": cfg.min_iv_rv_ratio,
                    "min_iv_minus_rv": cfg.min_iv_minus_rv,
                    "reject_event_risk": cfg.reject_event_risk,
                    "event_source_fail_closed": cfg.event_source_fail_closed,
                },
            )
        )

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty:
        filtered = pd.DataFrame(rank_underwriting_candidates(filtered.to_dict("records"), mode="put", cfg=cfg))
    try:
        filtered.to_csv(out_path, index=False)
    except Exception as exc:
        raise RuntimeError(f"failed to persist insurance-underwriting filtered sell-put candidates: {out_path}") from exc
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(out_path), reject_rows)
    return filtered


def enrich_and_filter_sell_put_short_vol(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_put_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    out_path: Any,
) -> pd.DataFrame:
    return enrich_and_filter_sell_put_underwriting(
        df_labeled=df_labeled,
        symbol=symbol,
        sell_put_cfg=sell_put_cfg,
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=exchange_rate_converter,
        out_path=out_path,
    )


def evaluate_sell_put_underwriting_row(
    row: dict[str, Any],
    *,
    cfg: InsuranceUnderwritingConfig,
) -> dict[str, Any]:
    return evaluate_underwriting_candidate(row, mode="put", cfg=cfg)


def evaluate_sell_put_short_vol_row(
    row: dict[str, Any],
    *,
    cfg: InsuranceUnderwritingConfig | SellPutShortVolConfig,
    risk_ctx: PortfolioRiskContext | None,
) -> dict[str, Any]:
    _ = risk_ctx
    underwriting_cfg = (
        cfg
        if isinstance(cfg, InsuranceUnderwritingConfig)
        else InsuranceUnderwritingConfig(
            min_iv_rv_ratio=cfg.min_iv_rv_ratio,
            min_iv_minus_rv=cfg.min_iv_minus_rv,
            reject_event_risk=cfg.reject_event_risk,
            event_source_fail_closed=cfg.event_source_fail_closed,
        )
    )
    return evaluate_sell_put_underwriting_row(row, cfg=underwriting_cfg)


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


def _bool_setting(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _bool_setting_from_sources(key: str, default: bool, *sources: dict[str, Any]) -> bool:
    for source in sources:
        if not isinstance(source, dict) or key not in source:
            continue
        return _bool_setting(source, key, default)
    return bool(default)
