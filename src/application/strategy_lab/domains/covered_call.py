from __future__ import annotations

from src.application.strategy_lab.domains.base import StrategyDomainAdapter


ADAPTER = StrategyDomainAdapter(
    strategy_family="covered_call",
    display_name="Covered Call",
    decision_scope="single_leg_short_call_with_share_cover",
    hypothesis_scope="shadow_replay_parameter_set",
    tunable_parameters=(
        "min_dte",
        "max_dte",
        "min_iv_rv_ratio",
        "min_iv_minus_rv",
        "min_annualized_return",
    ),
    safety_boundaries=(
        "instrument_identity",
        "event_risk",
        "spread_liquidity_floor",
        "covered_share_availability",
        "cost_basis_floor",
        "trade_state",
    ),
    scorecard_metrics=(
        "callaway_rate",
        "right_tail_opportunity_cost",
        "premium_vs_opportunity_cost",
        "holding_coverage",
        "max_favorable_excursion_missed",
    ),
)
