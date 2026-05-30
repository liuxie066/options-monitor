from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from domain.domain.short_vol_assessment import ShortVolAssessmentConfig, assess_short_vol_candidate
from domain.domain.symbol_identity import symbol_currency
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.application.short_vol_risk_context import (
    amount_to_cny,
    build_portfolio_risk_context,
    enrich_short_vol_contract_cny_fields,
)
from src.application.strategy_policy import RETURN_FIRST_PROFILE, SHORT_VOL_PROFILE, normalize_strategy_profile
from src.infrastructure.exchange_rates import CurrencyConverter


RETURN_FIRST_STRATEGY = RETURN_FIRST_PROFILE
SHORT_VOL_STRATEGY = SHORT_VOL_PROFILE


@dataclass(frozen=True)
class CoveredCallShortVolConfig(ShortVolAssessmentConfig):
    strategy: str = RETURN_FIRST_STRATEGY

    @property
    def enabled(self) -> bool:
        return self.strategy == SHORT_VOL_STRATEGY


def resolve_covered_call_short_vol_config(raw: dict[str, Any] | None) -> CoveredCallShortVolConfig:
    cfg = raw if isinstance(raw, dict) else {}
    strategy = normalize_strategy_profile(cfg.get("strategy") or cfg.get("strategy_profile"))

    short_vol = cfg.get("short_vol") if isinstance(cfg.get("short_vol"), dict) else {}
    concentration = cfg.get("concentration") if isinstance(cfg.get("concentration"), dict) else {}

    return CoveredCallShortVolConfig(
        strategy=strategy,
        min_iv_rv_ratio=_float_setting(short_vol, "min_iv_rv_ratio", 1.15),
        min_iv_minus_rv=_float_setting(short_vol, "min_iv_minus_rv", 0.05),
        min_abs_delta=_float_setting(short_vol, "min_abs_delta", 0.15),
        max_abs_delta=_float_setting(short_vol, "max_abs_delta", 0.30),
        target_abs_delta=_float_setting(short_vol, "target_abs_delta", 0.20),
        reject_event_risk=_bool_setting(short_vol, "reject_event_risk", True),
        event_source_fail_closed=_bool_setting(short_vol, "event_source_fail_closed", True),
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


def enrich_and_filter_covered_call_short_vol(
    *,
    df_labeled: pd.DataFrame,
    symbol: str,
    sell_call_cfg: dict[str, Any],
    portfolio_ctx: dict[str, Any] | None,
    exchange_rate_converter: CurrencyConverter,
    out_path: Any,
) -> pd.DataFrame:
    if df_labeled is None or df_labeled.empty:
        return df_labeled

    cfg = resolve_covered_call_short_vol_config(sell_call_cfg)
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
        row_payload = row.to_dict()
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
        decision = assess_short_vol_candidate(row_payload, mode="call", cfg=cfg, risk_ctx=risk_ctx)
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
                function="sell_call",
                mode="call",
                status="post_filtered",
                stage="post_filter",
                rule=decision["rule"],
                metric_value=decision.get("metric_value"),
                threshold=decision.get("threshold"),
                contract_symbol=row.get("contract_symbol"),
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=decision.get("message") or "covered-call short-vol strategy risk filter",
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
                    "reject_event_risk": cfg.reject_event_risk,
                    "event_source_fail_closed": cfg.event_source_fail_closed,
                    "enable_stress_check": cfg.enable_stress_check,
                    "stress_down_sigma_multiple": cfg.stress_down_sigma_multiple,
                    "gap_down_pct": cfg.gap_down_pct,
                    "call_gap_up_pct": cfg.call_gap_up_pct,
                    "max_call_gap_up_opportunity_cost_nav_pct": cfg.max_call_gap_up_opportunity_cost_nav_pct,
                    "max_call_gap_up_opportunity_cost_to_premium": cfg.max_call_gap_up_opportunity_cost_to_premium,
                },
            )
        )

    filtered = out.loc[keep_mask].copy()
    if not filtered.empty:
        try:
            from domain.domain.engine import rank_candidate_rows

            weights = _score_weights_from_sell_call_cfg(sell_call_cfg)
            filtered = pd.DataFrame(rank_candidate_rows(filtered.to_dict("records"), mode="call", score_weights=weights))
        except Exception:
            pass
    try:
        filtered.to_csv(out_path, index=False)
    except Exception as exc:
        raise RuntimeError(f"failed to persist short-vol filtered covered-call candidates: {out_path}") from exc
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(out_path), reject_rows)
    return filtered


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


def _score_weights_from_sell_call_cfg(raw: dict[str, Any]):
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
        path_risk=get("path_risk", 0.20),
    )


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
