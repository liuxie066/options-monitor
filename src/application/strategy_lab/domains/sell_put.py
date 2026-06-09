from __future__ import annotations

from src.application.strategy_lab.domains.base import StrategyDomainAdapter


ADAPTER = StrategyDomainAdapter(
    strategy_family="sell_put",
    display_name="Sell Put",
    decision_scope="single_leg_short_put",
    hypothesis_scope="shadow_replay_parameter_set",
    tunable_parameters=(
        "min_abs_delta",
        "max_abs_delta",
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
        "single_trade_concentration_cap",
        "cash_secured_capacity",
        "trade_state",
    ),
    scorecard_metrics=(
        "assignment_rate",
        "cash_efficiency",
        "downside_stress",
        "premium_per_capital_at_risk",
        "tail_loss",
    ),
)
