from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from scripts.option_positions_core.domain import (
    EXPIRE_AUTO_CLOSE,
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_expire_auto_close_patch,
    build_open_fields,
    effective_expiration,
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_side,
)
from scripts.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    source_type: str
    source_name: str
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    position_effect: str
    contracts: int
    price: float
    strike: float | None
    multiplier: int | None
    expiration_ymd: str | None
    currency: str
    trade_time_ms: int | None
    order_id: str | None
    multiplier_source: str | None
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectionDiagnostic:
    event_id: str
    severity: str
    code: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectionResult:
    lots: list[dict[str, Any]]
    diagnostics: list[ProjectionDiagnostic]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lots": self.lots,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    return TradeEvent(
        event_id=str(getattr(deal, "deal_id", "") or "").strip(),
        source_type="broker_trade_event",
        source_name="opend_push",
        broker=normalize_broker(getattr(deal, "broker", None) or "富途"),
        account=normalize_account(getattr(deal, "internal_account", None) or ""),
        symbol=_canonical_trade_symbol(getattr(deal, "symbol", "")),
        option_type=normalize_option_type(getattr(deal, "option_type", None) or ""),
        side=normalize_trade_side(getattr(deal, "side", None)) or "",
        position_effect=normalize_position_effect(getattr(deal, "position_effect", None)) or "",
        contracts=int(getattr(deal, "contracts", 0) or 0),
        price=float(getattr(deal, "price", 0.0) or 0.0),
        strike=(float(getattr(deal, "strike")) if getattr(deal, "strike", None) is not None else None),
        multiplier=(int(getattr(deal, "multiplier")) if getattr(deal, "multiplier", None) is not None else None),
        expiration_ymd=normalize_contract_expiration(getattr(deal, "expiration_ymd", None)),
        currency=normalize_currency(getattr(deal, "currency", None)),
        trade_time_ms=(int(getattr(deal, "trade_time_ms")) if getattr(deal, "trade_time_ms", None) is not None else None),
        order_id=(str(getattr(deal, "order_id", "") or "").strip() or None),
        multiplier_source=(str(getattr(deal, "multiplier_source", "") or "").strip() or None),
        raw_payload=(dict(getattr(deal, "raw_payload", {}) or {})),
    )


def _same_strike(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) < 1e-9


def _open_lot_record(event: TradeEvent) -> dict[str, Any]:
    if str(event.source_type).strip().lower() == "bootstrap_snapshot":
        payload = dict(event.raw_payload or {})
        record_id = str(payload.get("lot_record_id") or event.event_id or "").strip()
        snapshot_fields = payload.get("fields") or {}
        if record_id and isinstance(snapshot_fields, dict):
            fields = dict(snapshot_fields)
            fields["source_event_id"] = event.event_id
            fields["event_source_type"] = event.source_type
            fields["event_source_name"] = event.source_name
            return {"record_id": record_id, "fields": fields}
    cmd = OpenPositionCommand(
        broker=event.broker,
        account=event.account,
        symbol=event.symbol,
        option_type=event.option_type,
        side="short",
        contracts=int(event.contracts),
        currency=event.currency,
        strike=event.strike,
        multiplier=event.multiplier,
        expiration_ymd=event.expiration_ymd,
        premium_per_share=event.price,
        note=(
            f"source={event.source_name} "
            f"event_id={event.event_id} "
            f"order_id={event.order_id or ''} "
            f"multiplier_source={event.multiplier_source or ''}"
        ).strip(),
        opened_at_ms=event.trade_time_ms,
    )
    fields = build_open_fields(cmd)
    fields["source_event_id"] = event.event_id
    fields["event_source_type"] = event.source_type
    fields["event_source_name"] = event.source_name
    return {"record_id": f"lot_{event.event_id}", "fields": fields}


def _matches_close(fields: dict[str, Any], event: TradeEvent) -> bool:
    if effective_contracts_open(fields) <= 0:
        return False
    expiration_matches = False
    effective_exp_ms, exp_source = effective_expiration(fields)
    if event.expiration_ymd:
        if effective_exp_ms is not None:
            expiration_matches = exp_source == "expiration" and str(fields.get("expiration") or "") == str(effective_exp_ms)
        else:
            expiration_matches = str(fields.get("note") or "").find(f"exp={event.expiration_ymd}") >= 0
    elif effective_exp_ms is None:
        expiration_matches = True
    return (
        normalize_broker(fields.get("broker")) == event.broker
        and normalize_account(fields.get("account")) == event.account
        and _canonical_trade_symbol(fields.get("symbol")) == _canonical_trade_symbol(event.symbol)
        and normalize_option_type(fields.get("option_type")) == event.option_type
        and normalize_side(fields.get("side")) == "short"
        and _same_strike(fields.get("strike"), event.strike)
        and str(fields.get("source_event_id") or "") != event.event_id
        and expiration_matches
    )


def _close_target_source_event_id(event: TradeEvent) -> str:
    if str(event.position_effect).strip().lower() != "close":
        return ""
    payload = event.raw_payload or {}
    return str(payload.get("close_target_source_event_id") or "").strip()


def _close_target_record_id(event: TradeEvent) -> str:
    if str(event.position_effect).strip().lower() != "close":
        return ""
    payload = event.raw_payload or {}
    return str(payload.get("record_id") or "").strip()


def _is_expire_auto_close_event(event: TradeEvent) -> bool:
    if str(event.position_effect).strip().lower() != "close":
        return False
    payload = event.raw_payload or {}
    close_type = str(payload.get("close_type") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    return close_type == EXPIRE_AUTO_CLOSE or mode == EXPIRE_AUTO_CLOSE


def _auto_close_grace_days(event: TradeEvent) -> int | None:
    payload = event.raw_payload or {}
    raw = payload.get("auto_close_grace_days")
    if raw is None:
        raw = payload.get("grace_days")
    try:
        return int(raw) if raw not in (None, "") else None
    except Exception:
        return None


def _build_close_projection_patch(fields: dict[str, Any], event: TradeEvent, *, contracts_to_close: int) -> dict[str, Any]:
    if _is_expire_auto_close_event(event):
        payload = event.raw_payload or {}
        return build_expire_auto_close_patch(
            fields,
            as_of_ms=event.trade_time_ms,
            close_reason=str(payload.get("close_reason") or "expired"),
            exp_source=(str(payload.get("auto_close_exp_src") or payload.get("effective_exp_source") or "").strip() or None),
            grace_days=_auto_close_grace_days(event),
        )
    return build_buy_to_close_patch(
        fields,
        contracts_to_close=contracts_to_close,
        close_price=event.price,
        close_reason="broker_trade_buy_to_close",
        as_of_ms=event.trade_time_ms,
    )


def _matches_close_target(fields: dict[str, Any], event: TradeEvent) -> bool:
    return (
        normalize_broker(fields.get("broker")) == event.broker
        and normalize_account(fields.get("account")) == event.account
        and _canonical_trade_symbol(fields.get("symbol")) == _canonical_trade_symbol(event.symbol)
        and normalize_option_type(fields.get("option_type")) == event.option_type
        and normalize_side(fields.get("side")) == "short"
        and str(fields.get("source_event_id") or "") != event.event_id
    )


def _void_target_event_id(event: TradeEvent) -> str:
    if str(event.position_effect).strip().lower() != "void":
        return ""
    payload = event.raw_payload or {}
    return str(payload.get("void_target_event_id") or "").strip()


def _adjust_target_source_event_id(event: TradeEvent) -> str:
    if str(event.position_effect).strip().lower() != "adjust":
        return ""
    payload = event.raw_payload or {}
    return str(payload.get("adjust_target_source_event_id") or "").strip()


def _adjust_target_record_id(event: TradeEvent) -> str:
    if str(event.position_effect).strip().lower() != "adjust":
        return ""
    payload = event.raw_payload or {}
    return str(payload.get("record_id") or "").strip()


def _normalize_events(events: list[dict[str, Any]] | list[TradeEvent]) -> list[TradeEvent]:
    normalized_events: list[TradeEvent] = []
    for item in events:
        if isinstance(item, TradeEvent):
            normalized_events.append(item)
            continue
        if not isinstance(item, dict):
            continue
        normalized_events.append(TradeEvent(**item))
    normalized_events.sort(key=lambda row: (int(row.trade_time_ms or 0), row.event_id))
    return normalized_events


def _append_diagnostic(
    diagnostics: list[ProjectionDiagnostic],
    *,
    event: TradeEvent,
    code: str,
    message: str,
    severity: str = "error",
    details: dict[str, Any] | None = None,
) -> None:
    diagnostics.append(
        ProjectionDiagnostic(
            event_id=event.event_id,
            severity=severity,
            code=code,
            message=message,
            details=dict(details or {}),
        )
    )


def _find_lot_by_record_id(lots: list[dict[str, Any]], record_id: str) -> dict[str, Any] | None:
    target_record_id = str(record_id or "").strip()
    if not target_record_id:
        return None
    for lot in lots:
        if str(lot.get("record_id") or "").strip() == target_record_id:
            return lot
    return None


def _find_lot_by_source_event_id(lots: list[dict[str, Any]], source_event_id: str) -> dict[str, Any] | None:
    target_source_event_id = str(source_event_id or "").strip()
    if not target_source_event_id:
        return None
    for lot in lots:
        fields = lot.get("fields") or {}
        if str(fields.get("source_event_id") or "").strip() == target_source_event_id:
            return lot
    return None


def project_position_lot_records_with_diagnostics(
    events: list[dict[str, Any]] | list[TradeEvent],
) -> ProjectionResult:
    normalized_events = _normalize_events(events)
    voided_event_ids = {target for target in (_void_target_event_id(event) for event in normalized_events) if target}
    lots: list[dict[str, Any]] = []
    diagnostics: list[ProjectionDiagnostic] = []
    for event in normalized_events:
        if _void_target_event_id(event):
            continue
        if event.event_id in voided_event_ids:
            continue
        payload = event.raw_payload or {}
        adjust_target_source_event_id = _adjust_target_source_event_id(event)
        adjust_target_record_id = _adjust_target_record_id(event)
        if adjust_target_source_event_id or adjust_target_record_id:
            patch = payload.get("patch") or {}
            if isinstance(patch, dict):
                target_lot = (
                    _find_lot_by_record_id(lots, adjust_target_record_id)
                    if adjust_target_record_id
                    else _find_lot_by_source_event_id(lots, adjust_target_source_event_id)
                )
                if target_lot is None:
                    _append_diagnostic(
                        diagnostics,
                        event=event,
                        code="adjust_explicit_target_not_found",
                        message="adjust event target lot not found in projection",
                        details={
                            "record_id": adjust_target_record_id or None,
                            "source_event_id": adjust_target_source_event_id or None,
                        },
                    )
                    continue
                fields = target_lot.get("fields") or {}
                if (
                    adjust_target_record_id
                    and adjust_target_source_event_id
                    and str(fields.get("source_event_id") or "").strip() != adjust_target_source_event_id
                ):
                    _append_diagnostic(
                        diagnostics,
                        event=event,
                        code="adjust_explicit_target_conflict",
                        message="adjust event target record_id conflicts with source_event_id target",
                        details={
                            "record_id": adjust_target_record_id,
                            "source_event_id": adjust_target_source_event_id,
                            "lot_source_event_id": str(fields.get("source_event_id") or "").strip() or None,
                        },
                    )
                    continue
                merged = dict(fields)
                merged.update(patch)
                target_lot["fields"] = merged
            continue
        if str(event.source_type).strip().lower() == "bootstrap_snapshot":
            seeded = _open_lot_record(event)
            if seeded.get("record_id") and isinstance(seeded.get("fields"), dict):
                lots.append(seeded)
            continue
        if not event.event_id or event.contracts <= 0:
            continue
        if event.position_effect == "open" and event.side == "sell":
            lots.append(_open_lot_record(event))
            continue
        if event.position_effect != "close" or event.side != "buy":
            continue

        remaining = int(event.contracts)
        close_target_source_event_id = _close_target_source_event_id(event)
        close_target_record_id = _close_target_record_id(event)
        explicit_target = bool(close_target_source_event_id or close_target_record_id)
        if close_target_record_id:
            target_lot = _find_lot_by_record_id(lots, close_target_record_id)
            if target_lot is None:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_target_not_found",
                    message="close event target record_id not found in projection",
                    details={
                        "record_id": close_target_record_id,
                        "source_event_id": close_target_source_event_id or None,
                    },
                )
                continue
            fields = target_lot.get("fields") or {}
            if close_target_source_event_id and str(fields.get("source_event_id") or "").strip() != close_target_source_event_id:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_target_conflict",
                    message="close event target record_id conflicts with source_event_id target",
                    details={
                        "record_id": close_target_record_id,
                        "source_event_id": close_target_source_event_id,
                        "lot_source_event_id": str(fields.get("source_event_id") or "").strip() or None,
                    },
                )
                continue
            open_qty = effective_contracts_open(fields)
            if open_qty <= 0:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_target_already_closed",
                    message="close event targeted a lot with no open contracts",
                    severity="warn",
                    details={"record_id": close_target_record_id},
                )
                continue
            if not _matches_close_target(fields, event):
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_target_mismatch",
                    message="close event targeted lot does not match broker/account/symbol/option semantics",
                    details={"record_id": close_target_record_id},
                )
                continue
            if open_qty < remaining:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_target_oversized",
                    message="close event contracts exceed remaining open contracts on target lot",
                    details={
                        "record_id": close_target_record_id,
                        "contracts_requested": remaining,
                        "contracts_open": open_qty,
                    },
                )
                continue
            patch = _build_close_projection_patch(
                fields,
                event,
                contracts_to_close=remaining,
            )
            merged = dict(fields)
            merged.update(patch)
            merged["last_close_event_id"] = event.event_id
            target_lot["fields"] = merged
            continue
        if close_target_source_event_id:
            target_lot = _find_lot_by_source_event_id(lots, close_target_source_event_id)
            if target_lot is None:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_source_event_target_not_found",
                    message="close event source_event_id target not found in projection",
                    details={"source_event_id": close_target_source_event_id},
                )
                continue
            fields = target_lot.get("fields") or {}
            open_qty = effective_contracts_open(fields)
            if open_qty <= 0:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_source_event_target_already_closed",
                    message="close event targeted a source_event_id with no open contracts",
                    severity="warn",
                    details={"source_event_id": close_target_source_event_id},
                )
                continue
            if not _matches_close_target(fields, event):
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_source_event_target_mismatch",
                    message="close event source_event_id target does not match broker/account/symbol/option semantics",
                    details={"source_event_id": close_target_source_event_id},
                )
                continue
            if open_qty < remaining:
                _append_diagnostic(
                    diagnostics,
                    event=event,
                    code="close_explicit_source_event_target_oversized",
                    message="close event contracts exceed remaining open contracts on source_event_id target",
                    details={
                        "source_event_id": close_target_source_event_id,
                        "contracts_requested": remaining,
                        "contracts_open": open_qty,
                    },
                )
                continue
            patch = _build_close_projection_patch(
                fields,
                event,
                contracts_to_close=remaining,
            )
            merged = dict(fields)
            merged.update(patch)
            merged["last_close_event_id"] = event.event_id
            target_lot["fields"] = merged
            continue
        for lot in lots:
            fields = lot.get("fields") or {}
            open_qty = effective_contracts_open(fields)
            if open_qty <= 0:
                continue
            if not _matches_close(fields, event):
                continue
            take = min(open_qty, remaining)
            patch = _build_close_projection_patch(
                fields,
                event,
                contracts_to_close=take,
            )
            merged = dict(fields)
            merged.update(patch)
            merged["last_close_event_id"] = event.event_id
            lot["fields"] = merged
            remaining -= take
            if remaining <= 0:
                break
            if explicit_target:
                break
        if remaining > 0:
            _append_diagnostic(
                diagnostics,
                event=event,
                code="close_unmatched_contracts",
                message="close event contracts could not be fully matched to open lots",
                severity="warn",
                details={
                    "contracts_requested": int(event.contracts),
                    "contracts_matched": int(event.contracts) - int(remaining),
                    "contracts_unmatched": int(remaining),
                },
            )
    return ProjectionResult(lots=lots, diagnostics=diagnostics)


def project_position_lot_records(events: list[dict[str, Any]] | list[TradeEvent]) -> list[dict[str, Any]]:
    return project_position_lot_records_with_diagnostics(events).lots
