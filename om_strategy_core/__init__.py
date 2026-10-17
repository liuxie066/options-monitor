"""Public offline CSP/CC core; implementation lives only in domain.domain."""
from domain.domain.strategy_core import (
    __version__, CallCapacityInputs, PutCapacityInputs, ResolvedStrategy,
    calculate_constraints, calculate_metrics, evaluate_candidates, resolve_strategy,
)
from domain.domain.currency_conversion import CurrencyConverter, ExchangeRates
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from .identity import runtime_identity

__all__ = [
    "__version__", "CallCapacityInputs", "PutCapacityInputs", "ResolvedStrategy",
    "calculate_constraints", "calculate_metrics", "evaluate_candidates", "resolve_strategy",
    "CurrencyConverter", "ExchangeRates", "InsuranceUnderwritingConfig", "ShortVolPortfolioContext",
    "runtime_identity",
]
