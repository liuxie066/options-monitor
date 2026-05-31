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
from src.application.shadow_replay.collection import collect_shadow_replay_marks
from src.application.shadow_replay.capture import build_shadow_replay_dataset
from src.application.shadow_replay.marking import mark_shadow_replay_dataset
from src.application.shadow_replay.readiness import summarize_shadow_replay_readiness
from src.application.shadow_replay.settlement import settle_shadow_replay_dataset

__all__ = [
    "analyze_shadow_replay_dataset",
    "build_shadow_replay_dataset",
    "collect_shadow_replay_marks",
    "mark_shadow_replay_dataset",
    "settle_shadow_replay_dataset",
    "summarize_shadow_replay_readiness",
]
