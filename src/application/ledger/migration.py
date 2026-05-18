from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.domain.ledger import ContractKey, ProjectionResult, TradeEvent, project_trade_events
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_side,
    normalize_status,
    safe_float,
)
from domain.domain.trade_contract_identity import normalize_position_effect, normalize_trade_side


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    lot_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "lot_id": self.lot_id,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ReconciliationReport:
    legacy_open_lot_count: int
    ledger_open_lot_count: int
    issues: list[ReconciliationIssue]

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_open_lot_count": self.legacy_open_lot_count,
            "ledger_open_lot_count": self.ledger_open_lot_count,
            "issues": [item.to_dict() for item in self.issues],
            "has_errors": self.has_errors,
        }


@dataclass(frozen=True)
class ShadowReplayResult:
    source: str
    source_record_count: int
    imported_event_count: int
    projection: ProjectionResult
    import_diagnostics: list[LedgerDiagnostic] = field(default_factory=list)
    reconciliation: ReconciliationReport | None = None

    @property
    def has_errors(self) -> bool:
        return (
            any(item.severity == "error" for item in self.import_diagnostics)
            or self.projection.has_errors
            or bool(self.reconciliation and self.reconciliation.has_errors)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_record_count": self.source_record_count,
            "imported_event_count": self.imported_event_count,
            "projection": self.projection.to_dict(),
            "import_diagnostics": [item.to_dict() for item in self.import_diagnostics],
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation is not None else None,
            "has_errors": self.has_errors,
        }


def import_legacy_trade_events(events: list[Any]) -> tuple[list[TradeEvent], list[LedgerDiagnostic]]:
    imported: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    for item in events:
        event, item_diagnostics = legacy_trade_event_to_ledger_event(item)
        diagnostics.extend(item_diagnostics)
        if event is not None:
            imported.append(event)
    return imported, diagnostics


def shadow_replay_legacy_trade_events(events: list[Any]) -> ShadowReplayResult:
    imported, diagnostics = import_legacy_trade_events(events)
    projection = project_trade_events(imported)
    return ShadowReplayResult(
        source="legacy_trade_events",
        source_record_count=len(events),
        imported_event_count=len(imported),
        projection=projection,
        import_diagnostics=diagnostics,
    )


def import_position_lot_snapshot(
    records: list[dict[str, Any]],
    *,
    source: str = "legacy_position_lots",
) -> tuple[list[TradeEvent], list[LedgerDiagnostic]]:
    imported: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    for item in records:
        event, item_diagnostics = position_lot_snapshot_to_open_event(item, source=source)
        diagnostics.extend(item_diagnostics)
        if event is not None:
            imported.append(event)
    return imported, diagnostics


def shadow_replay_position_lot_snapshot(
    records: list[dict[str, Any]],
    *,
    source: str = "legacy_position_lots",
) -> ShadowReplayResult:
    imported, diagnostics = import_position_lot_snapshot(records, source=source)
    projection = project_trade_events(imported)
    reconciliation = reconcile_position_lot_snapshot(records, projection)
    return ShadowReplayResult(
        source=source,
        source_record_count=len(records),
        imported_event_count=len(imported),
        projection=projection,
        import_diagnostics=diagnostics,
        reconciliation=reconciliation,
    )


def legacy_trade_event_to_ledger_event(item: Any) -> tuple[TradeEvent | None, list[LedgerDiagnostic]]:
    event_id = str(_get(item, "event_id") or "").strip()
    diagnostics: list[LedgerDiagnostic] = []
    raw_payload = _dict_or_empty(_get(item, "raw_payload"))
    raw_fields = _dict_or_empty(raw_payload.get("fields"))
    position_effect = normalize_position_effect(_get(item, "position_effect"))
    raw_side = _get(item, "side")
    trade_side = normalize_trade_side(raw_side)
    event_type = _map_legacy_event_type(position_effect, raw_payload)
    position_side = _legacy_position_side(position_effect=position_effect, trade_side=trade_side, raw_side=raw_side)
    if not event_type or not position_side:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event_id,
                severity="error",
                code="legacy_event_type_unresolved",
                message="legacy trade event could not be mapped to a ledger event",
                details={
                    "position_effect": _get(item, "position_effect"),
                    "side": _get(item, "side"),
                },
            )
        )
        return None, diagnostics
    try:
        contract_key = ContractKey.from_values(
            broker=_coalesce(_get(item, "broker"), raw_fields.get("broker"), raw_fields.get("market")),
            account=_coalesce(_get(item, "account"), raw_fields.get("account")),
            underlying_symbol=_coalesce(_get(item, "symbol"), raw_fields.get("symbol")),
            option_type=_coalesce(_get(item, "option_type"), raw_fields.get("option_type")),
            position_side=position_side,
            strike=_coalesce(_get(item, "strike"), raw_fields.get("strike")),
            expiration_ymd=_coalesce(
                _get(item, "expiration_ymd"),
                raw_fields.get("expiration_ymd"),
                effective_expiration_ymd(raw_fields) if raw_fields else None,
            ),
        )
        event = TradeEvent(
            event_id=event_id,
            event_type=event_type,
            event_time_ms=int(_get(item, "trade_time_ms") or raw_fields.get("opened_at") or 0),
            contract_key=contract_key,
            contracts=max(0, int(_get(item, "contracts") or raw_fields.get("contracts") or 0)),
            price=float(_get(item, "price") or raw_fields.get("premium") or 0.0),
            currency=str(_coalesce(_get(item, "currency"), raw_fields.get("currency"), "")),
            source=str(_coalesce(_get(item, "source_name"), raw_payload.get("source"), "legacy_trade_events")),
            multiplier=float(_coalesce(_get(item, "multiplier"), raw_fields.get("multiplier"), 100) or 100),
            fees=float(raw_payload.get("fees") or 0.0),
            target_lot_id=_legacy_target_lot_id(raw_payload),
            target_event_id=_legacy_target_event_id(raw_payload),
            lot_id=_legacy_open_lot_id(event_id=event_id, raw_payload=raw_payload),
            raw_payload=raw_payload,
        )
    except Exception as exc:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event_id,
                severity="error",
                code="legacy_event_import_failed",
                message="legacy trade event import failed",
                details={"error": str(exc)},
            )
        )
        return None, diagnostics
    return event, diagnostics


def position_lot_snapshot_to_open_event(
    item: dict[str, Any],
    *,
    source: str,
) -> tuple[TradeEvent | None, list[LedgerDiagnostic]]:
    record_id = str(item.get("record_id") or "").strip()
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
    diagnostics: list[LedgerDiagnostic] = []
    if not record_id or not isinstance(fields, dict):
        diagnostics.append(
            LedgerDiagnostic(
                event_id=f"snapshot:{record_id}",
                severity="error",
                code="snapshot_record_invalid",
                message="position_lot snapshot record_id and fields are required",
            )
        )
        return None, diagnostics
    if normalize_status(fields.get("status")) == "close" or effective_contracts_open(fields) <= 0:
        return None, diagnostics
    try:
        contract_key = _contract_key_from_position_fields(fields)
        event = TradeEvent(
            event_id=f"snapshot:{source}:{record_id}",
            event_type="open",
            event_time_ms=int(fields.get("opened_at") or fields.get("last_action_at") or 0),
            contract_key=contract_key,
            contracts=effective_contracts_open(fields),
            price=float(safe_float(fields.get("premium")) or 0.0),
            currency=str(fields.get("currency") or ""),
            source=source,
            multiplier=float(effective_multiplier(fields) or 100),
            lot_id=record_id,
            raw_payload={"record_id": record_id, "fields": dict(fields), "source": source},
        )
    except Exception as exc:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=f"snapshot:{source}:{record_id}",
                severity="error",
                code="snapshot_import_failed",
                message="position_lot snapshot import failed",
                details={"record_id": record_id, "error": str(exc)},
            )
        )
        return None, diagnostics
    return event, diagnostics


def reconcile_position_lot_snapshot(
    records: list[dict[str, Any]],
    projection: ProjectionResult,
) -> ReconciliationReport:
    legacy_open = _legacy_open_lots_by_id(records)
    ledger_open = {
        lot.lot_id: lot
        for lot in projection.lots
        if lot.contracts_open > 0
    }
    issues: list[ReconciliationIssue] = []
    for lot_id, legacy in legacy_open.items():
        lot = ledger_open.get(lot_id)
        if lot is None:
            issues.append(
                ReconciliationIssue(
                    code="missing_in_ledger",
                    severity="error",
                    lot_id=lot_id,
                    message="legacy open lot is missing in ledger projection",
                )
            )
            continue
        if lot.contract_key != legacy["contract_key"]:
            issues.append(
                ReconciliationIssue(
                    code="identity_mismatch",
                    severity="error",
                    lot_id=lot_id,
                    message="legacy open lot identity differs from ledger projection",
                    details={
                        "legacy_contract_key": legacy["contract_key"].to_dict(),
                        "ledger_contract_key": lot.contract_key.to_dict(),
                    },
                )
            )
        if int(lot.contracts_open) != int(legacy["contracts_open"]):
            issues.append(
                ReconciliationIssue(
                    code="quantity_mismatch",
                    severity="error",
                    lot_id=lot_id,
                    message="legacy open lot quantity differs from ledger projection",
                    details={
                        "legacy_contracts_open": int(legacy["contracts_open"]),
                        "ledger_contracts_open": int(lot.contracts_open),
                    },
                )
            )
    for lot_id in sorted(set(ledger_open) - set(legacy_open)):
        issues.append(
            ReconciliationIssue(
                code="missing_in_legacy",
                severity="error",
                lot_id=lot_id,
                message="ledger projection open lot is missing in legacy snapshot",
            )
        )
    return ReconciliationReport(
        legacy_open_lot_count=len(legacy_open),
        ledger_open_lot_count=len(ledger_open),
        issues=issues,
    )


def _legacy_open_lots_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in records:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
        if not record_id or not isinstance(fields, dict):
            continue
        if normalize_status(fields.get("status")) == "close" or effective_contracts_open(fields) <= 0:
            continue
        try:
            out[record_id] = {
                "contract_key": _contract_key_from_position_fields(fields),
                "contracts_open": effective_contracts_open(fields),
            }
        except Exception:
            continue
    return out


def _contract_key_from_position_fields(fields: dict[str, Any]) -> ContractKey:
    return ContractKey.from_values(
        broker=fields.get("broker") or fields.get("market"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side"),
        strike=effective_strike(fields),
        expiration_ymd=fields.get("expiration_ymd") or effective_expiration_ymd(fields),
    )


def _map_legacy_event_type(position_effect: str | None, raw_payload: dict[str, Any]) -> str | None:
    if position_effect == "open":
        return "open"
    if position_effect == "close":
        close_type = str(raw_payload.get("close_type") or raw_payload.get("mode") or "").strip().lower()
        return "expire_close" if close_type == "expire_auto_close" else "close"
    if position_effect in {"adjust", "void"}:
        return position_effect
    return None


def _legacy_position_side(*, position_effect: str | None, trade_side: str | None, raw_side: Any = None) -> str | None:
    if position_effect == "open":
        if trade_side == "sell":
            return "short"
        if trade_side == "buy":
            return "long"
    if position_effect == "close":
        if trade_side == "buy":
            return "short"
        if trade_side == "sell":
            return "long"
    if position_effect in {"adjust", "void"}:
        normalized_side = normalize_side(raw_side)
        if normalized_side:
            return normalized_side
        if trade_side == "sell":
            return "short"
        if trade_side == "buy":
            return "long"
    return None


def _legacy_target_lot_id(raw_payload: dict[str, Any]) -> str | None:
    record_id = str(raw_payload.get("target_lot_id") or raw_payload.get("record_id") or "").strip()
    if record_id:
        return record_id
    source_event_id = str(raw_payload.get("close_target_source_event_id") or raw_payload.get("adjust_target_source_event_id") or "").strip()
    return f"lot_{source_event_id}" if source_event_id else None


def _legacy_target_event_id(raw_payload: dict[str, Any]) -> str | None:
    target = str(raw_payload.get("target_event_id") or raw_payload.get("void_target_event_id") or "").strip()
    return target or None


def _legacy_open_lot_id(*, event_id: str, raw_payload: dict[str, Any]) -> str | None:
    lot_record_id = str(raw_payload.get("lot_record_id") or "").strip()
    if lot_record_id:
        return lot_record_id
    explicit = str(raw_payload.get("lot_id") or "").strip()
    if explicit:
        return explicit
    return None if not event_id else f"lot_{event_id}"


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
