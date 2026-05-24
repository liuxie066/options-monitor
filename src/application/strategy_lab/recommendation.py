from __future__ import annotations

from typing import Any

from src.application.strategy_lab.contracts import BacktestResult


RECOMMENDATIONS: tuple[str, ...] = ("not_evaluable", "reject", "watch", "shadow", "review")


def recommend_strategy_lab(preflight: dict[str, Any], result: BacktestResult | None) -> dict[str, Any]:
    if preflight.get("status") != "evaluable":
        return {
            "recommendation": "not_evaluable",
            "reason": preflight.get("reason") or "preflight_not_evaluable",
            "rationale": list(preflight.get("missing") or []),
            "next_actions": list(preflight.get("next_actions") or []),
        }
    if result is None:
        return {
            "recommendation": "not_evaluable",
            "reason": "experiment_not_run",
            "rationale": ["missing_experiment_result"],
            "next_actions": ["rerun_experiment"],
        }
    comparison = dict(result.comparison)
    candidate = result.candidate_metrics
    sample_size = int(candidate.decision.get("sample_size") or 0)
    risk_worsening = bool(comparison.get("risk_worsening"))
    lift = _best_lift(
        comparison.get("return_per_locked_cash_day_lift"),
        comparison.get("realized_pnl_lift"),
        comparison.get("net_cash_inflow_lift"),
    )
    if risk_worsening:
        return {
            "recommendation": "reject",
            "reason": "risk_worsening",
            "rationale": ["candidate risk is worse than baseline"],
            "next_actions": ["review_risk_constraints_before_retrying"],
        }
    if lift is None or lift <= 0:
        return {
            "recommendation": "reject",
            "reason": "no_positive_lift",
            "rationale": ["candidate did not improve capital efficiency, realized PnL, or cash inflow"],
            "next_actions": ["try_a_different_candidate_policy_or_expand_dataset"],
        }
    if sample_size < 10:
        return {
            "recommendation": "watch",
            "reason": "positive_lift_but_low_sample",
            "rationale": ["candidate improves metrics but sample size is still low"],
            "next_actions": ["collect_more_lifecycle_outcomes_before_shadow"],
        }
    return {
        "recommendation": "shadow",
        "reason": "positive_lift_without_risk_worsening",
        "rationale": ["candidate improves capital efficiency or PnL without detected risk worsening"],
        "next_actions": ["start_shadow_review_with_preview_confirm_flow"],
    }


def _best_lift(*values: Any) -> float | None:
    parsed: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            parsed.append(float(value))
        except Exception:
            continue
    return max(parsed) if parsed else None
