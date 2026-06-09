from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StrategyDomainAdapter:
    strategy_family: str
    display_name: str
    decision_scope: str
    hypothesis_scope: str
    tunable_parameters: tuple[str, ...]
    safety_boundaries: tuple[str, ...]
    scorecard_metrics: tuple[str, ...]
    hypothesis_enabled: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_family": self.strategy_family,
            "display_name": self.display_name,
            "decision_scope": self.decision_scope,
            "hypothesis_scope": self.hypothesis_scope,
            "tunable_parameters": list(self.tunable_parameters),
            "safety_boundaries": list(self.safety_boundaries),
            "scorecard_metrics": list(self.scorecard_metrics),
            "hypothesis_enabled": self.hypothesis_enabled,
        }
