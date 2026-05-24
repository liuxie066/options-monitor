from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


STRATEGY_TYPES: tuple[str, ...] = (
    "sell_put",
    "sell_call",
    "yield_enhancement",
    "close_advice",
)

BACKTEST_CONCLUSIONS: tuple[str, ...] = (
    "reject",
    "watch",
    "shadow",
    "candidate",
)

BLOCKED_STANDARD_RATIO_METRICS: frozenset[str] = frozenset(
    {
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
    }
)

CORE_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "net_cash_inflow",
        "realized_pnl",
        "net_premium",
        "premium_capture_rate",
        "annualized_return_on_locked_cash",
        "locked_cash_days",
        "return_per_locked_cash_day",
        "margin_utilization_avg",
        "margin_utilization_peak",
        "cash_buffer_min",
        "assignment_rate",
        "strike_breach_rate",
        "worst_trade_pnl",
        "worst_expiry_pnl",
        "tail_loss_scenario",
        "max_drawdown_proxy",
        "concentration_by_symbol",
        "concentration_by_expiry",
        "candidate_count",
        "selected_count",
        "reject_reason_distribution",
        "avg_holding_days",
        "expired_count",
        "early_close_count",
        "turnover",
        "baseline_lift",
        "risk_worsening",
        "sample_size",
        "confidence_level",
        "overfit_warning",
    }
)


def validate_strategy_type(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "put": "sell_put",
        "short_put": "sell_put",
        "call": "sell_call",
        "short_call": "sell_call",
        "ye": "yield_enhancement",
        "yield": "yield_enhancement",
        "close": "close_advice",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in STRATEGY_TYPES:
        raise ValueError(f"unsupported strategy_type: {value}")
    return normalized


def validate_metric_names(metrics: list[str] | tuple[str, ...] | set[str] | None) -> tuple[str, ...]:
    if not metrics:
        return tuple(sorted(CORE_METRIC_NAMES))
    normalized = tuple(str(item or "").strip().lower() for item in metrics if str(item or "").strip())
    blocked = sorted(set(normalized) & BLOCKED_STANDARD_RATIO_METRICS)
    if blocked:
        raise ValueError(
            "standard ratio metrics require a trusted equity curve and are not part of Strategy Lab MVP: "
            + ", ".join(blocked)
        )
    return normalized


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    path: str
    row_index: int | None = None

    @classmethod
    def from_path(cls, *, kind: str, path: Path | str, row_index: int | None = None, base: Path | None = None) -> "EvidenceRef":
        resolved = Path(path)
        display = str(resolved)
        if base is not None:
            try:
                display = str(resolved.resolve().relative_to(base.resolve()))
            except Exception:
                display = str(resolved)
        return cls(kind=str(kind), path=display, row_index=row_index)


@dataclass(frozen=True)
class EvidenceArtifact:
    kind: str
    path: str
    row_count: int
    sample_rows: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CandidateSnapshot:
    row_id: str
    symbol: str | None
    account: str | None
    strategy_type: str | None
    contract_symbol: str | None
    option_type: str | None
    side: str | None
    strike: float | None
    expiry: str | None
    dte: int | None
    premium: float | None
    delta: float | None
    contracts: int | None
    multiplier: float | None
    locked_cash: float | None
    selected: bool | None
    reject_reasons: tuple[str, ...]
    evidence_ref: EvidenceRef
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _freeze_mapping(self.raw))


@dataclass(frozen=True)
class StrategyLabEvidence:
    artifacts: tuple[EvidenceArtifact, ...] = ()
    candidates: tuple[CandidateSnapshot, ...] = ()
    reject_logs: tuple[CandidateSnapshot, ...] = ()
    traces: tuple[Mapping[str, Any], ...] = ()
    replay_rows: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        rows_by_kind: dict[str, int] = {}
        artifact_rows: list[dict[str, Any]] = []
        for artifact in self.artifacts:
            rows_by_kind[artifact.kind] = rows_by_kind.get(artifact.kind, 0) + int(artifact.row_count)
            artifact_rows.append(
                {
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "row_count": int(artifact.row_count),
                }
            )
        return {
            "artifact_count": len(self.artifacts),
            "candidate_count": len(self.candidates),
            "reject_log_count": len(self.reject_logs),
            "trace_count": len(self.traces),
            "replay_row_count": len(self.replay_rows),
            "artifact_rows_by_kind": rows_by_kind,
            "artifact_rows": artifact_rows,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class StrategyPolicy:
    name: str
    strategy_type: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_type", validate_strategy_type(self.strategy_type))
        object.__setattr__(self, "params", _freeze_mapping(self.params))


@dataclass(frozen=True)
class StrategyExperiment:
    experiment_id: str
    strategy_type: str
    baseline_policy: StrategyPolicy
    candidate_policy: StrategyPolicy
    account: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    requested_metrics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        strategy_type = validate_strategy_type(self.strategy_type)
        object.__setattr__(self, "strategy_type", strategy_type)
        object.__setattr__(self, "requested_metrics", validate_metric_names(self.requested_metrics))
        if self.baseline_policy.strategy_type != strategy_type:
            raise ValueError("baseline_policy strategy_type must match experiment strategy_type")
        if self.candidate_policy.strategy_type != strategy_type:
            raise ValueError("candidate_policy strategy_type must match experiment strategy_type")


@dataclass(frozen=True)
class MetricSet:
    returns: Mapping[str, Any] = field(default_factory=dict)
    capital: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    execution: Mapping[str, Any] = field(default_factory=dict)
    decision: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "returns", _freeze_mapping(self.returns))
        object.__setattr__(self, "capital", _freeze_mapping(self.capital))
        object.__setattr__(self, "risk", _freeze_mapping(self.risk))
        object.__setattr__(self, "execution", _freeze_mapping(self.execution))
        object.__setattr__(self, "decision", _freeze_mapping(self.decision))
        observed = set(self.returns) | set(self.capital) | set(self.risk) | set(self.execution) | set(self.decision)
        blocked = sorted(observed & BLOCKED_STANDARD_RATIO_METRICS)
        if blocked:
            raise ValueError(
                "standard ratio metrics require a trusted equity curve and are not part of Strategy Lab MVP: "
                + ", ".join(blocked)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "returns": dict(self.returns),
            "capital": dict(self.capital),
            "risk": dict(self.risk),
            "execution": dict(self.execution),
            "decision": dict(self.decision),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BacktestResult:
    experiment: StrategyExperiment
    baseline_metrics: MetricSet
    candidate_metrics: MetricSet
    comparison: Mapping[str, Any]
    conclusion: str
    evidence: StrategyLabEvidence
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        conclusion = str(self.conclusion or "").strip().lower()
        if conclusion not in BACKTEST_CONCLUSIONS:
            raise ValueError(f"unsupported backtest conclusion: {self.conclusion}")
        object.__setattr__(self, "conclusion", conclusion)
        object.__setattr__(self, "comparison", _freeze_mapping(self.comparison))

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment.experiment_id,
            "strategy_type": self.experiment.strategy_type,
            "conclusion": self.conclusion,
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "candidate_metrics": self.candidate_metrics.to_dict(),
            "comparison": dict(self.comparison),
            "evidence_summary": self.evidence.summary(),
            "warnings": list(self.warnings),
        }
