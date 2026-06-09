from __future__ import annotations

from src.application.strategy_lab.domains.base import StrategyDomainAdapter


ADAPTER = StrategyDomainAdapter(
    strategy_family="combo_yield",
    display_name="Combo Yield",
    decision_scope="group_level_multi_leg",
    hypothesis_scope="group_level_observed_universe",
    tunable_parameters=(
        "min_put_distance_pct",
        "max_call_distance_pct",
        "min_net_premium",
        "min_premium_to_downside_ratio",
        "max_abs_put_delta",
    ),
    safety_boundaries=(
        "strategy_group_identity",
        "leg_identity",
        "leg_role",
        "group_payoff",
        "leg_slippage",
        "broker_facing_state",
    ),
    scorecard_metrics=(
        "combo_total_pnl",
        "leg_slippage",
        "funding_quality",
        "upside_participation",
        "group_level_drawdown",
    ),
    hypothesis_enabled=False,
)
