"""Public offline CSP/CC core; implementation lives only in domain.domain."""
from domain.domain.strategy_core import (
    __version__, CallCapacityInputs, PutCapacityInputs, ResolvedStrategy, StrategyTemplate,
    calculate_capacity, calculate_constraints, calculate_metrics, evaluate_candidates, resolve_strategy,
    rank_candidates, explain_rank,
)
from domain.domain.currency_conversion import CurrencyConverter, ExchangeRates
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from .identity import runtime_identity

__all__ = [
    "__version__", "CallCapacityInputs", "PutCapacityInputs", "ResolvedStrategy",
    "calculate_constraints", "calculate_metrics", "evaluate_candidates", "resolve_strategy",
    "CurrencyConverter", "ExchangeRates", "InsuranceUnderwritingConfig", "ShortVolPortfolioContext",
    "runtime_identity", "StrategyTemplate", "calculate_capacity", "rank_candidates", "explain_rank",
]
