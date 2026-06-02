from __future__ import annotations

"""Offline shadow replay public facade.

The implementation is split by pipeline stage:

- capture: accepted/rejected universe and rank evidence
- collection: point-in-time mark sampling from local cache or OpenD
- marking: local mark path generation from required-data quotes
- settlement: local outcome fact derivation
- analysis/readiness: offline review surfaces only
"""

from src.application.shadow_replay.analysis import analyze_shadow_replay_dataset
from src.application.shadow_replay.capture import build_shadow_replay_dataset
from src.application.shadow_replay.collection import collect_shadow_replay_marks
from src.application.shadow_replay.data_plan import run_shadow_replay_data_plan
from src.application.shadow_replay.marking import mark_shadow_replay_dataset
from src.application.shadow_replay.parameter_backtest import run_shadow_replay_parameter_backtest
from src.application.shadow_replay.readiness import summarize_shadow_replay_readiness
from src.application.shadow_replay.settlement import settle_shadow_replay_dataset
from src.application.shadow_replay.status import shadow_replay_dataset_status

__all__ = [
    "analyze_shadow_replay_dataset",
    "build_shadow_replay_dataset",
    "collect_shadow_replay_marks",
    "mark_shadow_replay_dataset",
    "run_shadow_replay_data_plan",
    "run_shadow_replay_parameter_backtest",
    "settle_shadow_replay_dataset",
    "shadow_replay_dataset_status",
    "summarize_shadow_replay_readiness",
]
