from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from domain.domain.ledger import (
    ProjectionResult,
    ResumableProjectionResult,
    ResumableProjectionState,
    TradeEvent,
    project_resumable_trade_events,
)
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.ledger.lots import PositionLot
from src.application.ledger.event_codec import (
    effective_import_diagnostics,
    iter_import_stored_trade_events,
)
from src.application.ledger.position_records import PositionLotRecord


PROJECTION_CONTRACT_VERSION = "position_lot_projection.v2"
_AUTO_CLOSE_FIELD_KEYS = (
    "auto_close_exp_src",
    "auto_close_grace_days",
)


@dataclass(frozen=True)
class PublishedPositionLotProjection:
    lots: list[PositionLotRecord]
    diagnostics: list[LedgerDiagnostic]
    ledger_projection: ProjectionResult
    resumable_state: ResumableProjectionState | None = None
    resumable_publication_state: ResumablePublicationState | None = None

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lots": [lot.to_dict() for lot in self.lots],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "has_errors": self.has_errors,
            "ledger_projection": self.ledger_projection.to_dict(),
        }


@dataclass(frozen=True)
class ResumablePublicationState:
    fields_by_lot_id: dict[str, dict[str, Any]]
    auto_close_baseline_by_lot_id: dict[str, dict[str, Any]]

    def __post_init__(self) -> None:
        if not isinstance(self.fields_by_lot_id, dict):
            raise TypeError("publication state fields_by_lot_id must be a dict")
        normalized: dict[str, dict[str, Any]] = {}
        for raw_lot_id, raw_fields in self.fields_by_lot_id.items():
            lot_id = str(raw_lot_id or "").strip()
            if not lot_id or not isinstance(raw_fields, dict):
                raise ValueError("publication state requires lot fields by id")
            normalized[lot_id] = deepcopy(raw_fields)
        raw_baselines = self.auto_close_baseline_by_lot_id
        if not isinstance(raw_baselines, dict) or set(raw_baselines) != set(
            normalized
        ):
            raise ValueError("publication state auto-close baselines must match lots")
        baselines: dict[str, dict[str, Any]] = {}
        for lot_id, raw_baseline in raw_baselines.items():
            if not isinstance(raw_baseline, dict) or not set(raw_baseline).issubset(
                _AUTO_CLOSE_FIELD_KEYS
            ):
                raise ValueError("publication state auto-close baseline is invalid")
            baselines[lot_id] = deepcopy(raw_baseline)
        object.__setattr__(
            self,
            "fields_by_lot_id",
            dict(sorted(normalized.items())),
        )
        object.__setattr__(
            self,
            "auto_close_baseline_by_lot_id",
            dict(sorted(baselines.items())),
        )

    @classmethod
    def empty(cls) -> "ResumablePublicationState":
        return cls(
            fields_by_lot_id={},
            auto_close_baseline_by_lot_id={},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "resumable_publication_state.v1",
            "active_lots": [
                {
                    "lot_id": lot_id,
                    "fields": deepcopy(fields),
                    "auto_close_baseline": deepcopy(
                        self.auto_close_baseline_by_lot_id[lot_id]
                    ),
                }
                for lot_id, fields in self.fields_by_lot_id.items()
            ],
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, decoded: Any) -> "ResumablePublicationState":
        if not isinstance(decoded, dict) or set(decoded) != {
            "schema_version",
            "active_lots",
        }:
            raise ValueError("publication state fields differ from v1 schema")
        if decoded["schema_version"] != "resumable_publication_state.v1":
            raise ValueError("publication state schema is unsupported")
        rows = decoded["active_lots"]
        if not isinstance(rows, list):
            raise ValueError("publication active_lots must be an array")
        fields_by_lot_id: dict[str, dict[str, Any]] = {}
        baselines_by_lot_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "lot_id",
                "fields",
                "auto_close_baseline",
            }:
                raise ValueError("publication lot fields differ from v1 schema")
            lot_id = str(row["lot_id"] or "").strip()
            if lot_id in fields_by_lot_id:
                raise ValueError(f"duplicate publication lot_id: {lot_id}")
            if not isinstance(row["fields"], dict):
                raise ValueError("publication lot fields must be an object")
            if not isinstance(row["auto_close_baseline"], dict):
                raise ValueError("publication auto-close baseline must be an object")
            fields_by_lot_id[lot_id] = row["fields"]
            baselines_by_lot_id[lot_id] = row["auto_close_baseline"]
        return cls(
            fields_by_lot_id=fields_by_lot_id,
            auto_close_baseline_by_lot_id=baselines_by_lot_id,
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ResumablePublicationState":
        def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in items:
                if key in out:
                    raise ValueError(f"duplicate publication state key: {key}")
                out[key] = value
            return out

        try:
            decoded = json.loads(
                bytes(payload).decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_raise_nonfinite(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("publication state is not valid UTF-8 JSON") from exc
        state = cls.from_dict(decoded)
        if state.to_json_bytes() != bytes(payload):
            raise ValueError("publication state JSON is not canonical")
        return state


@dataclass(frozen=True)
class ResumablePublishedPositionLotProjection:
    domain_state: ResumableProjectionState | None
    publication_state: ResumablePublicationState | None
    active_lots: tuple[PositionLotRecord, ...]
    touched_lots: tuple[PositionLotRecord, ...]
    diagnostics: tuple[LedgerDiagnostic, ...]
    requires_full_replay: bool = False
    full_replay_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return (
            self.domain_state is not None
            and self.publication_state is not None
            and not self.requires_full_replay
            and not self.diagnostics
        )


def _raise_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite publication state number: {value}")


def project_stored_trade_events_to_position_lots(events: list[Any]) -> PublishedPositionLotProjection:
    (
        ledger_events,
        import_diagnostics,
    ) = _collect_projection_inputs(events)
    domain_projection = project_resumable_trade_events(
        ledger_events,
        entry_mode="full",
    )
    ledger_projection = domain_projection.to_projection_result()
    effective_diagnostics = effective_import_diagnostics(
        ledger_events=ledger_events,
        import_diagnostics=import_diagnostics,
        projection_diagnostics=ledger_projection.diagnostics,
    )
    diagnostics = [*effective_diagnostics, *ledger_projection.diagnostics]
    if not diagnostics:
        current = _fold_resumable_publication(
            projection=domain_projection,
            publication_state=None,
        )
        if current.eligible:
            return PublishedPositionLotProjection(
                lots=list(current.touched_lots),
                diagnostics=[],
                ledger_projection=ledger_projection,
                resumable_state=current.domain_state,
                resumable_publication_state=current.publication_state,
            )
    lots = [_position_lot_to_legacy_record(lot) for lot in ledger_projection.lots]
    return PublishedPositionLotProjection(
        lots=lots,
        diagnostics=diagnostics,
        ledger_projection=ledger_projection,
    )


def project_stored_trade_events_to_resumable_position_lots(
    events: list[Any],
    *,
    domain_state: ResumableProjectionState | None = None,
    publication_state: ResumablePublicationState | None = None,
    entry_mode: str = "full",
) -> ResumablePublishedPositionLotProjection:
    mode = str(entry_mode or "").strip().lower()
    if mode == "tail" and (domain_state is None or publication_state is None):
        raise ValueError("tail publication requires domain and publication state")
    if mode == "full" and (domain_state is not None or publication_state is not None):
        raise ValueError("full publication cannot accept resumable state")
    if mode == "tail":
        assert domain_state is not None
        assert publication_state is not None
        domain_lot_ids = {item.lot_id for item in domain_state.active_lots}
        publication_lot_ids = set(publication_state.fields_by_lot_id)
        if domain_lot_ids != publication_lot_ids:
            return ResumablePublishedPositionLotProjection(
                domain_state=None,
                publication_state=None,
                active_lots=(),
                touched_lots=(),
                diagnostics=(),
                requires_full_replay=True,
                full_replay_reason="resumable_state_lot_ids_mismatch",
            )

    (
        ledger_events,
        import_diagnostics,
    ) = _collect_projection_inputs(events)
    projection = project_resumable_trade_events(
        ledger_events,
        initial_state=domain_state,
        entry_mode=mode,
    )
    if mode == "tail":
        effective_import = list(import_diagnostics)
    else:
        effective_import = effective_import_diagnostics(
            ledger_events=ledger_events,
            import_diagnostics=import_diagnostics,
            projection_diagnostics=list(projection.diagnostics),
        )
    diagnostics = tuple([*effective_import, *projection.diagnostics])
    if projection.requires_full_replay or diagnostics or projection.state is None:
        return ResumablePublishedPositionLotProjection(
            domain_state=None,
            publication_state=None,
            active_lots=(),
            touched_lots=(),
            diagnostics=diagnostics,
            requires_full_replay=(mode == "tail"),
            full_replay_reason=(
                projection.full_replay_reason
                or (
                    "tail_diagnostic"
                    if diagnostics and mode == "tail"
                    else "resumable_state_invalid"
                )
            ),
        )

    return _fold_resumable_publication(
        projection=projection,
        publication_state=publication_state,
    )


def _collect_projection_inputs(
    events: list[Any],
) -> tuple[
    list[TradeEvent],
    list[LedgerDiagnostic],
]:
    ledger_events: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    for _payload, event, item_diagnostics in iter_import_stored_trade_events(events):
        diagnostics.extend(item_diagnostics)
        if event is not None:
            ledger_events.append(event)
    return ledger_events, diagnostics


def _fold_resumable_publication(
    *,
    projection: ResumableProjectionResult,
    publication_state: ResumablePublicationState | None,
) -> ResumablePublishedPositionLotProjection:
    fields_by_lot_id = (
        deepcopy(publication_state.fields_by_lot_id)
        if publication_state is not None
        else {}
    )
    auto_close_baselines = (
        deepcopy(publication_state.auto_close_baseline_by_lot_id)
        if publication_state is not None
        else {}
    )
    touched_by_lot_id: dict[str, PositionLotRecord] = {}
    touched_order: list[str] = []
    for transition in projection.transitions:
        if not transition.applied or transition.lot_after is None:
            continue
        record = _position_lot_to_legacy_record(transition.lot_after)
        if record.lot_id not in touched_by_lot_id:
            touched_order.append(record.lot_id)
        touched_by_lot_id[record.lot_id] = record
        if transition.lot_before is None:
            auto_close_baselines[record.lot_id] = {
                key: deepcopy(record.fields[key])
                for key in _AUTO_CLOSE_FIELD_KEYS
                if key in record.fields
            }
        if transition.finalized and publication_state is not None:
            fields_by_lot_id.pop(record.lot_id, None)
            auto_close_baselines.pop(record.lot_id, None)
        else:
            fields_by_lot_id[record.lot_id] = deepcopy(record.fields)

    active_lot_ids = {
        item.lot_id
        for item in (
            projection.state.active_lots if projection.state is not None else ()
        )
    }
    fields_by_lot_id = {
        lot_id: fields
        for lot_id, fields in fields_by_lot_id.items()
        if lot_id in active_lot_ids
    }
    auto_close_baselines = {
        lot_id: baseline
        for lot_id, baseline in auto_close_baselines.items()
        if lot_id in active_lot_ids
    }

    next_publication_state = ResumablePublicationState(
        fields_by_lot_id=fields_by_lot_id,
        auto_close_baseline_by_lot_id=auto_close_baselines,
    )
    active_records = tuple(
        PositionLotRecord(lot_id=lot_id, fields=deepcopy(fields))
        for lot_id, fields in next_publication_state.fields_by_lot_id.items()
    )
    return ResumablePublishedPositionLotProjection(
        domain_state=projection.state,
        publication_state=next_publication_state,
        active_lots=active_records,
        touched_lots=tuple(
            touched_by_lot_id[lot_id] for lot_id in touched_order
        ),
        diagnostics=(),
    )


def ensure_projection_publishable(
    projection: PublishedPositionLotProjection,
    *,
    operation: str,
) -> None:
    error_codes = sorted(
        {
            str(item.code or "").strip() or "unknown"
            for item in projection.diagnostics
            if item.severity == "error"
        }
    )
    if error_codes:
        raise ValueError(
            f"{str(operation or 'position projection').strip()} failed: "
            + ",".join(error_codes)
        )


def _position_lot_to_legacy_record(lot: PositionLot) -> PositionLotRecord:
    """The published record: ``PositionLot.to_dict()`` and nothing else.

    The payload is a pure function of the projected lot (I-1). The retired
    assembly it replaces seeded from the open event's stored ``fields`` snapshot
    and then re-derived the read model's key set on top, which made ``fields_json``
    depend on whatever keys a historical snapshot happened to carry -- the open
    set that let ``exp``/``underlying_shares_locked`` leak into a new payload.

    Everything the old assembly added on top of the lot is either folded into the
    lot already or has moved to the event layer: ``close_type``/``close_reason``/
    ``close_price``/``closed_at``/``auto_close_*`` from the closing event's
    payload, the strategy metadata family from the adjust events, and the
    ``note`` provenance line from the open event's source fields.
    """
    return PositionLotRecord(lot_id=lot.lot_id, fields=lot.to_dict())


__all__ = [
    "PROJECTION_CONTRACT_VERSION",
    "PublishedPositionLotProjection",
    "ResumablePublicationState",
    "ResumablePublishedPositionLotProjection",
    "ensure_projection_publishable",
    "project_stored_trade_events_to_resumable_position_lots",
    "project_stored_trade_events_to_position_lots",
]
