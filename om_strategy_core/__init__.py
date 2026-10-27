"""Public offline strategy and option performance core; implementation lives only in domain.domain."""
from domain.domain.strategy_core import (
    __version__, CallCapacityInputs, PutCapacityInputs, ResolvedStrategy, StrategyTemplate,
    calculate_capacity, calculate_constraints, calculate_metrics, evaluate_candidates, resolve_strategy,
    rank_candidates, explain_rank,
)
from domain.domain.currency_conversion import CurrencyConverter, ExchangeRates
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from domain.domain.short_vol_assessment import ShortVolPortfolioContext
from .identity import runtime_identity

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance import (
    OptionFeeCashEvent, PeriodRequest, PeriodWindow, calculate_option_performance,
    cashflow_input_digest, normalize_performance_period,
)

__all__ = [
    "__version__", "CallCapacityInputs", "PutCapacityInputs", "ResolvedStrategy",
    "calculate_constraints", "calculate_metrics", "evaluate_candidates", "resolve_strategy",
    "CurrencyConverter", "ExchangeRates", "InsuranceUnderwritingConfig", "ShortVolPortfolioContext",
    "runtime_identity", "StrategyTemplate", "calculate_capacity", "rank_candidates", "explain_rank",
    "ContractKey", "TradeEvent", "OptionFeeCashEvent", "PeriodRequest", "PeriodWindow",
    "calculate_option_performance", "cashflow_input_digest", "normalize_performance_period",
]
