from __future__ import annotations

import argparse
from typing import Any


def add_strategy_lab_commands(subparsers: Any) -> argparse.ArgumentParser:
    strategy_lab = subparsers.add_parser("strategy-lab", help="operate the Strategy Lab")
    commands = strategy_lab.add_subparsers(dest="strategy_lab_command", required=True)
    readiness = commands.add_parser("readiness", help="manage targeted readiness evidence")
    readiness_commands = readiness.add_subparsers(dest="strategy_lab_readiness_command", required=True)
    refresh = readiness_commands.add_parser(
        "refresh-history-k",
        help="preview or publish one targeted history-K readiness PoC",
    )
    refresh.add_argument("--profile-path", required=True)
    refresh.add_argument("--contract-symbol", required=True)
    refresh.add_argument("--underlier-code", required=True)
    refresh.add_argument("--sample-date", required=True)
    refresh.add_argument("--confirmed-probe-sha256", default=None)
    refresh.add_argument("--actor", default=None)
    refresh.add_argument("--write", action="store_true")

    canary = commands.add_parser("canary", help="preview two days of engineering-only Recipe projection")
    canary.add_argument("--profile-path", required=True)

    recipes = commands.add_parser("recipes", help="list currently supported experiment recipes")
    recipes.add_argument("--profile-path", required=True)
    recipes.add_argument("--fee-plan-receipt-path", required=True)
    recipes.add_argument("--maturity-cutoff-utc", required=True)

    preview = commands.add_parser("preview", help="preview a read-only experiment")
    preview.add_argument("--profile-path", required=True)
    preview.add_argument("--hypothesis", required=True)
    preview.add_argument("--recipe-id", required=True)
    preview.add_argument("--fee-plan-receipt-path", required=True)
    preview.add_argument("--maturity-cutoff-utc", required=True)

    confirm = commands.add_parser("confirm-research", help="confirm one current experiment preview")
    confirm.add_argument("--profile-path", required=True)
    confirm.add_argument("--hypothesis", required=True)
    confirm.add_argument("--recipe-id", required=True)
    confirm.add_argument("--fee-plan-receipt-path", required=True)
    confirm.add_argument("--maturity-cutoff-utc", required=True)
    confirm.add_argument("--confirmed-preview-sha256", required=True)
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--idempotency-key", required=True)

    validation_preview = commands.add_parser("preview-validation", help="preview the next 10 validation sessions")
    validation_preview.add_argument("--profile-path", required=True)
    validation_preview.add_argument("--experiment-id", required=True)
    validation_preview.add_argument("--requested-start", required=True)

    validation_confirm = commands.add_parser("confirm-validation", help="confirm one current validation preview")
    validation_confirm.add_argument("--profile-path", required=True)
    validation_confirm.add_argument("--experiment-id", required=True)
    validation_confirm.add_argument("--requested-start", required=True)
    validation_confirm.add_argument("--confirmed-preview-sha256", required=True)
    validation_confirm.add_argument("--actor", required=True)
    validation_confirm.add_argument("--idempotency-key", required=True)

    advance = commands.add_parser("advance", help="advance hidden validation evidence")
    advance.add_argument("--profile-path", required=True)
    advance_target = advance.add_mutually_exclusive_group(required=True)
    advance_target.add_argument("--experiment-id")
    advance_target.add_argument("--scheduled", action="store_true")

    status = commands.add_parser("status", help="read one experiment status")
    status.add_argument("--profile-path", required=True)
    status.add_argument("--experiment-id", required=True)

    research = commands.add_parser("research", help="resume bounded local research")
    research_commands = research.add_subparsers(dest="strategy_lab_research_command", required=True)
    execute = research_commands.add_parser("execute", help="execute at most one provider unit")
    execute.add_argument("--profile-path", required=True)
    execute.add_argument("--experiment-id", required=True)
    execute.add_argument("--actor", required=True)

    receipt = commands.add_parser("receipt", help="read one Strategy Lab receipt")
    receipt.add_argument("--profile-path", required=True)
    receipt.add_argument("--experiment-id", required=True)
    receipt.add_argument("--kind", choices=("research", "final"), default="research")
    return strategy_lab


__all__ = ["add_strategy_lab_commands"]
