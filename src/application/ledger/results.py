from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from domain.domain.ledger.position_fields import PositionLotPatch


def _compact_payload(items: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in items.items() if value is not None}


@dataclass(frozen=True)
class LedgerPreflightResult:
    status: str
    read_model: str
    fail_closed: bool
    event_type: str | None = None
    event_time_ms: int | None = None
    target_lot_id: str | None = None
    event_id: str | None = None
    target_event_id: str | None = None
    contract_key: dict[str, Any] | None = None
    position_key: str | None = None
    contracts_open_before: int | None = None
    contracts_to_open: int | None = None
    contracts_to_close: int | None = None
    contracts_open_after: int | None = None
    position_contracts_open_after: int | None = None
    source_record_count: int | None = None
    imported_event_count: int | None = None
    projection_diagnostic_count: int | None = None
    reconciliation_issue_count: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", dict(self.details))
        if self.contract_key is not None:
            object.__setattr__(self, "contract_key", dict(self.contract_key))

    def with_details(self, **details: Any) -> "LedgerPreflightResult":
        return replace(self, details={**self.details, **details})

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            _compact_payload(
                {
                    "status": self.status,
                    "read_model": self.read_model,
                    "fail_closed": self.fail_closed,
                    "event_type": self.event_type,
                    "event_time_ms": self.event_time_ms,
                    "target_lot_id": self.target_lot_id,
                    "event_id": self.event_id,
                    "target_event_id": self.target_event_id,
                    "contract_key": dict(self.contract_key) if self.contract_key is not None else None,
                    "position_key": self.position_key,
                    "contracts_open_before": self.contracts_open_before,
                    "contracts_to_open": self.contracts_to_open,
                    "contracts_to_close": self.contracts_to_close,
                    "contracts_open_after": self.contracts_open_after,
                    "position_contracts_open_after": self.position_contracts_open_after,
                    "source_record_count": self.source_record_count,
                    "imported_event_count": self.imported_event_count,
                    "projection_diagnostic_count": self.projection_diagnostic_count,
                    "reconciliation_issue_count": self.reconciliation_issue_count,
                }
            )
        )
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass(frozen=True)
class ManualAdjustPreflightResult:
    fields: dict[str, Any]
    patch_contract: PositionLotPatch
    ledger_preflight: LedgerPreflightResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    @property
    def patch(self) -> dict[str, Any]:
        return self.patch_contract.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "patch_contract": self.patch_contract,
            "patch": self.patch,
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class LedgerWriteResult:
    event_id: str | None = None
    record_id: str | None = None
    created: bool | None = None
    position_lot_count: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", dict(self.details))

    @classmethod
    def from_payload(cls, payload: "LedgerWriteResult | dict[str, Any]") -> "LedgerWriteResult":
        if isinstance(payload, LedgerWriteResult):
            return payload
        known = {"event_id", "record_id", "created", "position_lot_count"}
        return cls(
            event_id=(str(payload["event_id"]) if payload.get("event_id") is not None else None),
            record_id=(str(payload["record_id"]) if payload.get("record_id") is not None else None),
            created=(bool(payload["created"]) if payload.get("created") is not None else None),
            position_lot_count=(
                int(payload["position_lot_count"]) if payload.get("position_lot_count") is not None else None
            ),
            details={key: value for key, value in payload.items() if key not in known},
        )

    def with_record_id(self, record_id: str | None) -> "LedgerWriteResult":
        return replace(self, record_id=(str(record_id).strip() if record_id is not None else None))

    def with_details(self, **details: Any) -> "LedgerWriteResult":
        return replace(self, details={**self.details, **details})

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            _compact_payload(
                {
                    "event_id": self.event_id,
                    "record_id": self.record_id,
                    "created": self.created,
                    "position_lot_count": self.position_lot_count,
                }
            )
        )
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass(frozen=True)
class ProjectionRefreshResult:
    trade_event_count: int
    position_lot_count: int
    projection_diagnostic_count: int = 0
    unmatched_explicit_close_count: int = 0
    unmatched_heuristic_close_count: int = 0
    projection_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "projection_diagnostics", [dict(item) for item in self.projection_diagnostics])
        object.__setattr__(self, "details", dict(self.details))

    @classmethod
    def from_payload(cls, payload: "ProjectionRefreshResult | dict[str, Any]") -> "ProjectionRefreshResult":
        if isinstance(payload, ProjectionRefreshResult):
            return payload
        known = {
            "trade_event_count",
            "position_lot_count",
            "projection_diagnostic_count",
            "unmatched_explicit_close_count",
            "unmatched_heuristic_close_count",
            "projection_diagnostics",
        }
        return cls(
            trade_event_count=int(payload.get("trade_event_count") or 0),
            position_lot_count=int(payload.get("position_lot_count") or 0),
            projection_diagnostic_count=int(payload.get("projection_diagnostic_count") or 0),
            unmatched_explicit_close_count=int(payload.get("unmatched_explicit_close_count") or 0),
            unmatched_heuristic_close_count=int(payload.get("unmatched_heuristic_close_count") or 0),
            projection_diagnostics=[
                dict(item) for item in list(payload.get("projection_diagnostics") or []) if isinstance(item, dict)
            ],
            details={key: value for key, value in payload.items() if key not in known},
        )

    def with_details(self, **details: Any) -> "ProjectionRefreshResult":
        return replace(self, details={**self.details, **details})

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            {
                "trade_event_count": int(self.trade_event_count),
                "position_lot_count": int(self.position_lot_count),
                "projection_diagnostic_count": int(self.projection_diagnostic_count),
                "unmatched_explicit_close_count": int(self.unmatched_explicit_close_count),
                "unmatched_heuristic_close_count": int(self.unmatched_heuristic_close_count),
                "projection_diagnostics": [dict(item) for item in self.projection_diagnostics],
            }
        )
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass(frozen=True)
class BrokerTradeOpenPreviewResult:
    command: Any
    fields: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    def to_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "fields": dict(self.fields),
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_payload().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_payload()


@dataclass(frozen=True)
class BrokerTradeOperation:
    action: str
    record_id: str | None = None
    contracts_to_close: int | None = None
    fields: dict[str, Any] | None = None
    patch: PositionLotPatch | dict[str, Any] | None = None
    matched_by: str | None = None
    event_id: str | None = None
    result: LedgerWriteResult | dict[str, Any] | None = None
    ledger_preflight: LedgerPreflightResult | dict[str, Any] | None = None
    close_target_resolution: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fields is not None:
            object.__setattr__(self, "fields", dict(self.fields))
        if isinstance(self.patch, dict):
            object.__setattr__(self, "patch", dict(self.patch))
        if isinstance(self.result, dict):
            object.__setattr__(self, "result", LedgerWriteResult.from_payload(self.result))
        if isinstance(self.ledger_preflight, dict):
            object.__setattr__(
                self,
                "ledger_preflight",
                LedgerPreflightResult(
                    status=str(self.ledger_preflight.get("status") or ""),
                    read_model=str(self.ledger_preflight.get("read_model") or ""),
                    fail_closed=bool(self.ledger_preflight.get("fail_closed")),
                    details=dict(self.ledger_preflight),
                ),
            )
        if self.close_target_resolution is not None:
            object.__setattr__(self, "close_target_resolution", dict(self.close_target_resolution))
        object.__setattr__(self, "details", dict(self.details))

    @classmethod
    def from_payload(cls, payload: "BrokerTradeOperation | dict[str, Any]") -> "BrokerTradeOperation":
        if isinstance(payload, BrokerTradeOperation):
            return payload
        known = {
            "action",
            "record_id",
            "contracts_to_close",
            "fields",
            "patch",
            "matched_by",
            "event_id",
            "result",
            "ledger_preflight",
            "close_target_resolution",
        }
        return cls(
            action=str(payload.get("action") or ""),
            record_id=(str(payload["record_id"]) if payload.get("record_id") is not None else None),
            contracts_to_close=(
                int(payload["contracts_to_close"]) if payload.get("contracts_to_close") is not None else None
            ),
            fields=(dict(payload["fields"]) if isinstance(payload.get("fields"), dict) else None),
            patch=(dict(payload["patch"]) if isinstance(payload.get("patch"), dict) else None),
            matched_by=(str(payload["matched_by"]) if payload.get("matched_by") is not None else None),
            event_id=(str(payload["event_id"]) if payload.get("event_id") is not None else None),
            result=payload.get("result") if isinstance(payload.get("result"), dict) else None,
            ledger_preflight=(
                dict(payload["ledger_preflight"]) if isinstance(payload.get("ledger_preflight"), dict) else None
            ),
            close_target_resolution=(
                dict(payload["close_target_resolution"])
                if isinstance(payload.get("close_target_resolution"), dict)
                else None
            ),
            details={key: value for key, value in payload.items() if key not in known},
        )

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.details)
        patch: dict[str, Any] | None
        if isinstance(self.patch, PositionLotPatch):
            patch = self.patch.to_dict()
        elif isinstance(self.patch, dict):
            patch = dict(self.patch)
        else:
            patch = None
        result = self.result.to_dict() if isinstance(self.result, LedgerWriteResult) else None
        ledger_preflight = (
            self.ledger_preflight.to_dict()
            if isinstance(self.ledger_preflight, LedgerPreflightResult)
            else None
        )
        payload.update(
            _compact_payload(
                {
                    "action": self.action,
                    "record_id": self.record_id,
                    "contracts_to_close": self.contracts_to_close,
                    "fields": dict(self.fields) if self.fields is not None else None,
                    "patch": patch,
                    "matched_by": self.matched_by,
                    "event_id": self.event_id,
                    "result": result,
                    "ledger_preflight": ledger_preflight,
                    "close_target_resolution": (
                        dict(self.close_target_resolution)
                        if self.close_target_resolution is not None
                        else None
                    ),
                }
            )
        )
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_payload().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_payload()


@dataclass(frozen=True)
class ExpiredCloseDecision:
    record_id: str
    position_id: str
    should_close: bool
    reason: str
    expiration_ms: int | None = None
    raw_expiration_ms: int | None = None
    expiration_ymd: str | None = None
    effective_exp_source: str = "none"
    contracts_open: int | None = None
    skip_reason: str | None = None
    patch: PositionLotPatch | dict[str, Any] | None = None
    close_target_resolution: dict[str, Any] | None = None
    ledger_preflight: LedgerPreflightResult | dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.patch, dict):
            object.__setattr__(self, "patch", dict(self.patch))
        if self.close_target_resolution is not None:
            object.__setattr__(self, "close_target_resolution", dict(self.close_target_resolution))
        if isinstance(self.ledger_preflight, dict):
            object.__setattr__(self, "ledger_preflight", dict(self.ledger_preflight))
        object.__setattr__(self, "details", dict(self.details))

    def with_skip(self, *, reason: str, skip_reason: str, contracts_open: int | None = None) -> "ExpiredCloseDecision":
        return replace(
            self,
            should_close=False,
            reason=reason,
            skip_reason=skip_reason,
            contracts_open=contracts_open,
            patch=None,
        )

    def with_close_target_resolution(self, payload: dict[str, Any]) -> "ExpiredCloseDecision":
        return replace(self, close_target_resolution=dict(payload))

    def with_ledger_preflight(self, payload: LedgerPreflightResult | dict[str, Any]) -> "ExpiredCloseDecision":
        return replace(self, ledger_preflight=payload)

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.details)
        if isinstance(self.patch, PositionLotPatch):
            patch = self.patch.to_dict()
        elif isinstance(self.patch, dict):
            patch = dict(self.patch)
        else:
            patch = None
        if isinstance(self.ledger_preflight, LedgerPreflightResult):
            ledger_preflight = self.ledger_preflight.to_dict()
        elif isinstance(self.ledger_preflight, dict):
            ledger_preflight = dict(self.ledger_preflight)
        else:
            ledger_preflight = None
        payload.update(
            _compact_payload(
                {
                    "record_id": self.record_id,
                    "position_id": self.position_id,
                    "expiration_ms": self.expiration_ms,
                    "raw_expiration_ms": self.raw_expiration_ms,
                    "expiration_ymd": self.expiration_ymd,
                    "effective_exp_source": self.effective_exp_source,
                    "should_close": self.should_close,
                    "reason": self.reason,
                    "skip_reason": self.skip_reason,
                    "contracts_open": self.contracts_open,
                    "patch": patch,
                    "close_target_resolution": (
                        dict(self.close_target_resolution)
                        if self.close_target_resolution is not None
                        else None
                    ),
                    "ledger_preflight": ledger_preflight,
                }
            )
        )
        if self.patch is None:
            payload["patch"] = None
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_payload().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_payload()


@dataclass(frozen=True)
class ExpiredCloseApplyResult:
    decision: ExpiredCloseDecision
    result: LedgerWriteResult | dict[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.result, dict):
            object.__setattr__(self, "result", LedgerWriteResult.from_payload(self.result))

    def to_payload(self) -> dict[str, Any]:
        payload = self.decision.to_payload()
        payload["result"] = self.result.to_dict() if isinstance(self.result, LedgerWriteResult) else {}
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_payload().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_payload()


@dataclass(frozen=True)
class ExpiredCloseRunResult:
    decisions: list[ExpiredCloseDecision]
    applied: list[ExpiredCloseApplyResult]
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", list(self.decisions))
        object.__setattr__(self, "applied", list(self.applied))
        object.__setattr__(self, "errors", [str(item) for item in self.errors])

    def to_legacy_tuple(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        return (
            [item.to_payload() for item in self.decisions],
            [item.to_payload() for item in self.applied],
            list(self.errors),
        )

    def to_payload(self) -> dict[str, Any]:
        decisions, applied, errors = self.to_legacy_tuple()
        return {"decisions": decisions, "applied": applied, "errors": errors}

    def __iter__(self) -> Any:
        return iter(self.to_legacy_tuple())


@dataclass(frozen=True)
class TradeEventInterventionPreview:
    target_event: dict[str, Any]
    void_event: dict[str, Any]
    repair_event: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_event", dict(self.target_event))
        object.__setattr__(self, "void_event", dict(self.void_event))
        if self.repair_event is not None:
            object.__setattr__(self, "repair_event", dict(self.repair_event))

    @property
    def appended_events(self) -> list[dict[str, Any]]:
        events = [dict(self.void_event)]
        if self.repair_event is not None:
            events.append(dict(self.repair_event))
        return events

    @classmethod
    def from_payload(cls, payload: "TradeEventInterventionPreview | dict[str, Any]") -> "TradeEventInterventionPreview":
        if isinstance(payload, TradeEventInterventionPreview):
            return payload
        return cls(
            target_event=dict(payload.get("target_event") or {}),
            void_event=dict(payload.get("void_event") or {}),
            repair_event=(dict(payload["repair_event"]) if isinstance(payload.get("repair_event"), dict) else None),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_event": dict(self.target_event),
            "void_event": dict(self.void_event),
        }
        if self.repair_event is not None:
            payload["repair_event"] = dict(self.repair_event)
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_payload().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_payload()


@dataclass(frozen=True)
class OpenLedgerResult:
    result: LedgerWriteResult
    fields: dict[str, Any]
    ledger_preflight: LedgerPreflightResult
    command: Any | None = None
    duplicate_checked_before_write: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "result": self.result.to_dict(),
            "fields": dict(self.fields),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }
        if self.command is not None:
            payload["command"] = self.command
        if self.duplicate_checked_before_write:
            payload["duplicate_checked_before_write"] = True
        return payload


@dataclass(frozen=True)
class ManualCloseLedgerResult:
    result: LedgerWriteResult
    fields: dict[str, Any]
    patch: PositionLotPatch
    ledger_preflight: LedgerPreflightResult
    close_target_resolution: dict[str, Any] | None = None
    duplicate_checked_before_patch: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))
        if self.close_target_resolution is not None:
            object.__setattr__(self, "close_target_resolution", dict(self.close_target_resolution))

    def with_close_target_resolution(self, payload: dict[str, Any]) -> "ManualCloseLedgerResult":
        return replace(self, close_target_resolution=dict(payload))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "result": self.result.to_dict(),
            "fields": dict(self.fields),
            "patch": self.patch.to_dict(),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }
        if self.close_target_resolution is not None:
            payload["close_target_resolution"] = dict(self.close_target_resolution)
        if self.duplicate_checked_before_patch:
            payload["duplicate_checked_before_patch"] = True
        return payload


@dataclass(frozen=True)
class ManualAdjustLedgerResult:
    result: LedgerWriteResult
    fields: dict[str, Any]
    patch: PositionLotPatch
    ledger_preflight: LedgerPreflightResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    def to_payload(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict(),
            "fields": dict(self.fields),
            "patch": self.patch.to_dict(),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }


@dataclass(frozen=True)
class TradeEventInterventionLedgerResult:
    result: LedgerWriteResult
    preview: TradeEventInterventionPreview | dict[str, Any]
    ledger_preflight: LedgerPreflightResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "preview", TradeEventInterventionPreview.from_payload(self.preview))

    def to_payload(self) -> dict[str, Any]:
        preview = TradeEventInterventionPreview.from_payload(self.preview)
        return {
            "result": self.result.to_dict(),
            "preview": preview.to_payload(),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }


@dataclass(frozen=True)
class ManualOpenPreviewResult:
    fields: dict[str, Any]
    command: Any
    ledger_preflight: LedgerPreflightResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"fields": dict(self.fields), "command": self.command}
        if self.ledger_preflight is not None:
            payload["ledger_preflight"] = self.ledger_preflight.to_dict()
        return payload


@dataclass(frozen=True)
class ManualClosePreviewResult:
    fields: dict[str, Any]
    patch: PositionLotPatch
    ledger_preflight: LedgerPreflightResult
    close_target_resolution: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "close_target_resolution", dict(self.close_target_resolution))

    def to_payload(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "patch": self.patch.to_dict(),
            "close_target_resolution": dict(self.close_target_resolution),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }


@dataclass(frozen=True)
class ManualAdjustPreviewResult:
    fields: dict[str, Any]
    patch: PositionLotPatch
    ledger_preflight: LedgerPreflightResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", dict(self.fields))

    def to_payload(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "patch": self.patch.to_dict(),
            "ledger_preflight": self.ledger_preflight.to_dict(),
        }


__all__ = [
    "LedgerPreflightResult",
    "LedgerWriteResult",
    "BrokerTradeOpenPreviewResult",
    "BrokerTradeOperation",
    "ExpiredCloseApplyResult",
    "ExpiredCloseDecision",
    "ExpiredCloseRunResult",
    "ManualAdjustLedgerResult",
    "ManualAdjustPreflightResult",
    "ManualAdjustPreviewResult",
    "ManualCloseLedgerResult",
    "ManualClosePreviewResult",
    "ManualOpenPreviewResult",
    "OpenLedgerResult",
    "ProjectionRefreshResult",
    "TradeEventInterventionPreview",
    "TradeEventInterventionLedgerResult",
]
