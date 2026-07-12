from __future__ import annotations

"""Strategy Lab public facade.

This package is the product layer above Research / Shadow Replay. It normalizes
replay candidates into strategy decision instances, wraps local replay evidence
lifecycle data-plans, generates controlled single-leg hypotheses, and reuses
Shadow Replay for read-only candidate-impact experiments. It can also render
advisory-only dry-run proposals from experiment artifacts and redacted LLM
context for local strategy analysis.
"""

from src.application.strategy_lab.decisions import build_decision_instances
from src.application.strategy_lab.combo_evaluator import run_combo_yield_group_experiment
from src.application.strategy_lab.experiment import run_strategy_lab_experiment
from src.application.strategy_lab.hypotheses import generate_strategy_lab_hypotheses
from src.application.strategy_lab.llm_context import build_strategy_lab_llm_context
from src.application.strategy_lab.proposal import build_strategy_lab_proposal
from src.application.strategy_lab.readiness import analyze_strategy_lab_readiness
from src.application.strategy_lab.update import run_strategy_lab_update

__all__ = [
    "analyze_strategy_lab_readiness",
    "build_strategy_lab_proposal",
    "build_strategy_lab_llm_context",
    "build_decision_instances",
    "generate_strategy_lab_hypotheses",
    "run_combo_yield_group_experiment",
    "run_strategy_lab_experiment",
    "run_strategy_lab_update",
]
