"""Optional Position Advice capability for options-monitor."""

from .covered_call_roll import (
    evaluate_conditional_covered_call_roll,
    evaluate_covered_call_roll,
    old_covered_call_in_roll_scope,
    rank_covered_call_roll_candidates,
)
from .economics import remaining_max_annualized_return
from .policy import evaluate_close_advice, evaluate_user_provided_close_advice
from .presentation import format_close_advice_reason
from .short_put_roll import (
    evaluate_short_put_roll,
    evaluate_user_provided_short_put_roll,
    old_position_in_roll_scope,
    rank_short_put_roll_candidates,
)

__all__ = [
    "evaluate_close_advice",
    "format_close_advice_reason",
    "remaining_max_annualized_return",
    "evaluate_short_put_roll",
    "old_position_in_roll_scope",
    "rank_short_put_roll_candidates",
    "evaluate_covered_call_roll",
    "old_covered_call_in_roll_scope",
    "rank_covered_call_roll_candidates",
    "evaluate_user_provided_close_advice",
    "evaluate_user_provided_short_put_roll",
    "evaluate_conditional_covered_call_roll",
]
