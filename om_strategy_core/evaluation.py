"""Offline valuation/cash facade; summarize/compare v1/v2 are frozen compatibility.

New backtest metrics are maintained by OptionBench, without an OM dependency on OB.
"""

from domain.domain.performance.strategy_evaluation import (
    EvaluationBook,
    IncompleteCashError,
    bind_cash_conversions,
    close_execution,
    compare_evaluations,
    compare_evaluations_v2,
    convert_option_cashflow,
    eligible_stock_marks,
    decision_exchange_rates,
    legacy_market,
    resize_execution,
    summarize_evaluation,
    summarize_evaluation_v2,
    value_portfolio,
)

__all__ = [
    "EvaluationBook",
    "IncompleteCashError",
    "value_portfolio",
    "summarize_evaluation",
    "summarize_evaluation_v2",
    "compare_evaluations",
    "compare_evaluations_v2",
    "convert_option_cashflow",
    "bind_cash_conversions",
    "legacy_market",
    "eligible_stock_marks",
    "decision_exchange_rates",
    "resize_execution",
    "close_execution",
]
