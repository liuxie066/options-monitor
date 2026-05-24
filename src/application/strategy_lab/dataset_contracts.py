from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from src.application.strategy_lab.contracts import (
    CandidateSnapshot,
    EvidenceArtifact,
    EvidenceRef,
    StrategyLabEvidence,
)


DATASET_SCHEMA_VERSION = "strategy_lab_dataset.v1"


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _freeze_rows(values: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None) -> tuple[Mapping[str, Any], ...]:
    return tuple(_freeze_mapping(item) for item in (values or ()))


@dataclass(frozen=True)
class StrategyLabDataset:
    dataset_id: str
    created_at: str
    scope: Mapping[str, Any]
    sources: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[Mapping[str, Any], ...] = ()
    rejects: tuple[Mapping[str, Any], ...] = ()
    traces: tuple[Mapping[str, Any], ...] = ()
    replay_rows: tuple[Mapping[str, Any], ...] = ()
    outcomes: tuple[Mapping[str, Any], ...] = ()
    trade_events: tuple[Mapping[str, Any], ...] = ()
    position_lots: tuple[Mapping[str, Any], ...] = ()
    capital_snapshots: tuple[Mapping[str, Any], ...] = ()
    market_snapshots: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    schema_version: str = DATASET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DATASET_SCHEMA_VERSION:
            raise ValueError(f"unsupported strategy lab dataset schema: {self.schema_version}")
        object.__setattr__(self, "scope", _freeze_mapping(self.scope))
        object.__setattr__(self, "sources", _freeze_mapping(self.sources))
        for attr in (
            "candidates",
            "rejects",
            "traces",
            "replay_rows",
            "outcomes",
            "trade_events",
            "position_lots",
            "capital_snapshots",
            "market_snapshots",
        ):
            object.__setattr__(self, attr, _freeze_rows(list(getattr(self, attr))))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings if str(item).strip()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "created_at": self.created_at,
            "scope": dict(self.scope),
            "sources": dict(self.sources),
            "candidates": [dict(item) for item in self.candidates],
            "rejects": [dict(item) for item in self.rejects],
            "traces": [dict(item) for item in self.traces],
            "replay_rows": [dict(item) for item in self.replay_rows],
            "outcomes": [dict(item) for item in self.outcomes],
            "trade_events": [dict(item) for item in self.trade_events],
            "position_lots": [dict(item) for item in self.position_lots],
            "capital_snapshots": [dict(item) for item in self.capital_snapshots],
            "market_snapshots": [dict(item) for item in self.market_snapshots],
            "warnings": list(self.warnings),
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyLabDataset":
        if not isinstance(payload, Mapping):
            raise ValueError("strategy lab dataset payload must be an object")
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            dataset_id=str(payload.get("dataset_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            scope=dict(payload.get("scope") or {}),
            sources=dict(payload.get("sources") or {}),
            candidates=tuple(dict(item) for item in _rows(payload.get("candidates"))),
            rejects=tuple(dict(item) for item in _rows(payload.get("rejects"))),
            traces=tuple(dict(item) for item in _rows(payload.get("traces"))),
            replay_rows=tuple(dict(item) for item in _rows(payload.get("replay_rows"))),
            outcomes=tuple(dict(item) for item in _rows(payload.get("outcomes"))),
            trade_events=tuple(dict(item) for item in _rows(payload.get("trade_events"))),
            position_lots=tuple(dict(item) for item in _rows(payload.get("position_lots"))),
            capital_snapshots=tuple(dict(item) for item in _rows(payload.get("capital_snapshots"))),
            market_snapshots=tuple(dict(item) for item in _rows(payload.get("market_snapshots"))),
            warnings=tuple(str(item) for item in (payload.get("warnings") or ())),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "candidate_count": len(self.candidates),
            "reject_count": len(self.rejects),
            "trace_count": len(self.traces),
            "replay_row_count": len(self.replay_rows),
            "outcome_count": len(self.outcomes),
            "trade_event_count": len(self.trade_events),
            "position_lot_count": len(self.position_lots),
            "capital_snapshot_count": len(self.capital_snapshots),
            "market_snapshot_count": len(self.market_snapshots),
            "warning_count": len(self.warnings),
        }

    def to_evidence(self) -> StrategyLabEvidence:
        artifacts = tuple(
            EvidenceArtifact(
                kind=str(item.get("kind") or "dataset"),
                path=str(item.get("path") or ""),
                row_count=int(item.get("row_count") or 0),
                sample_rows=tuple(dict(row) for row in _rows(item.get("sample_rows"))),
            )
            for item in _rows(dict(self.sources).get("artifacts"))
        )
        return StrategyLabEvidence(
            artifacts=artifacts,
            candidates=tuple(candidate_snapshot_from_dict(item, default_kind="candidate") for item in self.candidates),
            reject_logs=tuple(candidate_snapshot_from_dict(item, default_kind="reject_log") for item in self.rejects),
            traces=tuple(_freeze_rows(list(self.traces))),
            replay_rows=tuple(_freeze_rows(list(self.replay_rows))),
            warnings=tuple(self.warnings),
        )


def candidate_snapshot_to_dict(snapshot: CandidateSnapshot) -> dict[str, Any]:
    return {
        "row_id": snapshot.row_id,
        "symbol": snapshot.symbol,
        "account": snapshot.account,
        "strategy_type": snapshot.strategy_type,
        "contract_symbol": snapshot.contract_symbol,
        "option_type": snapshot.option_type,
        "side": snapshot.side,
        "strike": snapshot.strike,
        "expiry": snapshot.expiry,
        "dte": snapshot.dte,
        "premium": snapshot.premium,
        "delta": snapshot.delta,
        "contracts": snapshot.contracts,
        "multiplier": snapshot.multiplier,
        "locked_cash": snapshot.locked_cash,
        "selected": snapshot.selected,
        "reject_reasons": list(snapshot.reject_reasons),
        "evidence_ref": {
            "kind": snapshot.evidence_ref.kind,
            "path": snapshot.evidence_ref.path,
            "row_index": snapshot.evidence_ref.row_index,
        },
        "raw": dict(snapshot.raw),
    }


def candidate_snapshot_from_dict(payload: Mapping[str, Any], *, default_kind: str) -> CandidateSnapshot:
    ref = dict(payload.get("evidence_ref") or {})
    raw = dict(payload.get("raw") or {})
    if not raw:
        raw = {key: value for key, value in payload.items() if key not in {"evidence_ref", "raw", "reject_reasons"}}
    return CandidateSnapshot(
        row_id=str(payload.get("row_id") or raw.get("row_id") or ""),
        symbol=_optional_text(payload.get("symbol")),
        account=_optional_text(payload.get("account"), lower=True),
        strategy_type=_optional_text(payload.get("strategy_type")),
        contract_symbol=_optional_text(payload.get("contract_symbol")),
        option_type=_optional_text(payload.get("option_type")),
        side=_optional_text(payload.get("side")),
        strike=_optional_float(payload.get("strike")),
        expiry=_optional_text(payload.get("expiry")),
        dte=_optional_int(payload.get("dte")),
        premium=_optional_float(payload.get("premium")),
        delta=_optional_float(payload.get("delta")),
        contracts=_optional_int(payload.get("contracts")),
        multiplier=_optional_float(payload.get("multiplier")),
        locked_cash=_optional_float(payload.get("locked_cash")),
        selected=_optional_bool(payload.get("selected")),
        reject_reasons=tuple(str(item) for item in (payload.get("reject_reasons") or ()) if str(item).strip()),
        evidence_ref=EvidenceRef(
            kind=str(ref.get("kind") or default_kind),
            path=str(ref.get("path") or ""),
            row_index=_optional_int(ref.get("row_index")),
        ),
        raw=raw,
    )


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, tuple):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _optional_text(value: Any, *, lower: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lower else text


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return None

