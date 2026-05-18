from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    now_ms,
)
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.ledger.event_codec import stored_trade_event_to_ledger_event
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
    with_sqlite_repo_transaction,
)
from src.application.ledger.results import LedgerWriteResult, TradeEventInterventionPreview
from src.application.ledger.writer import (
    persist_trade_event_object,
    projection_diagnostics_summary,
)
from src.infrastructure.feishu_bitable import safe_float


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _get_trade_event_dict(repo: Any, *, event_id: str) -> dict[str, Any]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    target = next(
        (
            item
            for item in sqlite_repo.list_trade_events()
            if str(item.get("event_id") or "").strip() == str(event_id or "").strip()
        ),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {event_id}")
    return dict(target)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _void_event_for_target(events: list[dict[str, Any]], target_event_id: str) -> dict[str, Any] | None:
    target = str(target_event_id or "").strip()
    if not target:
        return None
    for event in events:
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        if str(_event_payload(event).get("void_target_event_id") or "").strip() == target:
            return dict(event)
    return None


def _event_sort_key(event: dict[str, Any]) -> tuple[int, str]:
    return (int(safe_float(event.get("trade_time_ms")) or 0), str(event.get("event_id") or ""))


def _event_position_side(event: dict[str, Any]) -> str | None:
    raw_side = str(event.get("side") or "").strip().lower()
    trade_side = normalize_trade_side(raw_side)
    position_side = normalize_side(raw_side) if raw_side else None
    effect = normalize_position_effect(event.get("position_effect")) or str(event.get("position_effect") or "").strip().lower()
    if effect == "open":
        if trade_side == "sell" or position_side == "short":
            return "short"
        if trade_side == "buy" or position_side == "long":
            return "long"
    if effect == "close":
        if trade_side == "buy":
            return "short"
        if trade_side == "sell":
            return "long"
    return None


def _contract_key_from_event_dict(event: dict[str, Any]) -> ContractKey:
    position_side = _event_position_side(event) or normalize_side(event.get("side"), strict=True)
    return ContractKey.from_values(
        broker=event.get("broker"),
        account=event.get("account"),
        underlying_symbol=_canonical_trade_symbol(event.get("symbol")),
        option_type=event.get("option_type"),
        position_side=position_side,
        strike=event.get("strike"),
        expiration_ymd=event.get("expiration_ymd"),
    )


def _event_type_from_position_effect(position_effect: Any) -> str:
    effect = normalize_position_effect(position_effect) or str(position_effect or "").strip().lower()
    if effect == "open":
        return "open"
    if effect == "close":
        return "close"
    return effect


def _void_trade_event(
    *,
    event_id: str,
    target: dict[str, Any],
    target_event_id: str,
    reason: str,
    mode: str,
    source: str,
    as_of_ms: int | None,
    repair_event_id: str | None = None,
) -> TradeEvent:
    raw_payload: dict[str, Any] = {
        "source": source,
        "source_type": "manual_trade_event",
        "mode": mode,
        "void_target_event_id": str(target_event_id),
        "void_reason": str(reason or ""),
    }
    if repair_event_id:
        raw_payload["repair_event_id"] = repair_event_id
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=int(as_of_ms or now_ms()),
        contract_key=_contract_key_from_event_dict(target),
        contracts=0,
        price=0.0,
        currency=normalize_currency(target.get("currency")),
        source="cli_trade_event_repair" if repair_event_id else "cli_manual_void",
        multiplier=float(safe_float(target.get("multiplier")) or 100.0),
        target_event_id=str(target_event_id),
        raw_payload=raw_payload,
    )


def _repair_trade_event(*, event_id: str, core: dict[str, Any], raw_payload: dict[str, Any]) -> TradeEvent:
    event_type = _event_type_from_position_effect(core.get("position_effect"))
    target_lot_id = None
    if event_type in {"close", "expire_close", "assignment", "exercise", "adjust"}:
        target_lot_id = str(raw_payload.get("target_lot_id") or raw_payload.get("record_id") or "").strip() or None
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=int(core.get("trade_time_ms") or now_ms()),
        contract_key=_contract_key_from_event_dict(core),
        contracts=int(core.get("contracts") or 0),
        price=float(core.get("price") or 0.0),
        currency=normalize_currency(core.get("currency")),
        source="cli_trade_event_repair",
        multiplier=float(safe_float(core.get("multiplier")) or 100.0),
        target_lot_id=target_lot_id,
        lot_id=(str(raw_payload.get("lot_id") or raw_payload.get("lot_record_id") or "").strip() or None),
        raw_payload=raw_payload,
    )


def _preview_event_to_trade_event(payload: dict[str, Any]) -> TradeEvent:
    event, diagnostics = stored_trade_event_to_ledger_event(payload)
    errors = [item for item in diagnostics if item.severity == "error"]
    if event is None or errors:
        codes = ", ".join(item.code for item in errors) or "event_decode_failed"
        raise ValueError(f"manual intervention preview event is invalid: {codes}")
    return event


def _open_event_lot_record_id(event: dict[str, Any]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        return ""
    if str(event.get("source_type") or "").strip().lower() == "bootstrap_snapshot":
        payload_record_id = str(_event_payload(event).get("lot_record_id") or "").strip()
        if payload_record_id:
            return payload_record_id
    return f"lot_{event_id}"


def _same_trade_event_contract(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_strike = safe_float(left.get("strike"))
    right_strike = safe_float(right.get("strike"))
    if (left_strike is None) != (right_strike is None):
        return False
    if left_strike is not None and right_strike is not None and abs(float(left_strike) - float(right_strike)) >= 1e-9:
        return False
    return (
        normalize_broker(left.get("broker")) == normalize_broker(right.get("broker"))
        and normalize_account(left.get("account")) == normalize_account(right.get("account"))
        and _canonical_trade_symbol(left.get("symbol")) == _canonical_trade_symbol(right.get("symbol"))
        and normalize_option_type(left.get("option_type")) == normalize_option_type(right.get("option_type"))
        and normalize_contract_expiration(left.get("expiration_ymd")) == normalize_contract_expiration(right.get("expiration_ymd"))
    )


def _repair_downstream_dependencies(events: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    target_event_id = str(target.get("event_id") or "").strip()
    if normalize_position_effect(target.get("position_effect")) != "open":
        return []
    target_lot_record_id = _open_event_lot_record_id(target)
    target_position_side = _event_position_side(target)
    target_sort_key = _event_sort_key(target)
    voided_event_ids = {
        str(_event_payload(event).get("void_target_event_id") or "").strip()
        for event in events
        if str(event.get("position_effect") or "").strip().lower() == "void"
    }
    out: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id == target_event_id or event_id in voided_event_ids:
            continue
        if _event_sort_key(event) <= target_sort_key:
            continue
        effect = normalize_position_effect(event.get("position_effect")) or str(event.get("position_effect") or "").strip().lower()
        payload = _event_payload(event)
        record_id = str(payload.get("record_id") or "").strip()
        source_event_id = str(
            payload.get("close_target_source_event_id")
            or payload.get("adjust_target_source_event_id")
            or ""
        ).strip()
        explicit_target = bool(
            (target_lot_record_id and record_id == target_lot_record_id)
            or (target_event_id and source_event_id == target_event_id)
        )
        if explicit_target:
            out.append(
                {
                    "event_id": event_id,
                    "position_effect": effect,
                    "dependency": "explicit_target",
                    "record_id": record_id or None,
                    "source_event_id": source_event_id or None,
                }
            )
            continue
        if effect != "close":
            continue
        if record_id or source_event_id:
            continue
        if target_position_side and _event_position_side(event) != target_position_side:
            continue
        if not _same_trade_event_contract(target, event):
            continue
        out.append(
            {
                "event_id": event_id,
                "position_effect": effect,
                "dependency": "heuristic_close_match",
                "record_id": None,
                "source_event_id": None,
            }
        )
    return out


def _assert_trade_event_can_be_manually_voided(events: list[dict[str, Any]], target: dict[str, Any]) -> None:
    target_event_id = str(target.get("event_id") or "").strip()
    if str(target.get("position_effect") or "").strip().lower() == "void":
        raise ValueError(f"cannot void a void event: {target_event_id}")
    existing_void = _void_event_for_target(events, target_event_id)
    if existing_void is not None:
        raise ValueError(
            "trade event already voided: "
            f"{target_event_id} via {str(existing_void.get('event_id') or '').strip()}"
        )


def persist_manual_void_event(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
    target = next(
        (
            item
            for item in events
            if str(item.get("event_id") or "").strip() == str(target_event_id or "").strip()
        ),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    _assert_trade_event_can_be_manually_voided(events, target)

    event = _void_trade_event(
        event_id=f"manual-void-{target_event_id}-{uuid.uuid4().hex}",
        target=target,
        target_event_id=target_event_id,
        reason=void_reason,
        mode="manual_void",
        source="om option-positions",
        as_of_ms=as_of_ms,
    )
    return persist_trade_event_object(repo, event)


def build_manual_void_preview(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
    target = next(
        (
            item
            for item in events
            if str(item.get("event_id") or "").strip() == str(target_event_id or "").strip()
        ),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    _assert_trade_event_can_be_manually_voided(events, target)
    digest = hashlib.sha256(
        json.dumps(
            {
                "target_event_id": str(target_event_id or "").strip(),
                "void_reason": str(void_reason or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    event = _void_trade_event(
        event_id=f"manual-void-preview-{digest}",
        target=target,
        target_event_id=target_event_id,
        reason=void_reason,
        mode="manual_void_preview",
        source="om trade-events",
        as_of_ms=as_of_ms,
    )
    return TradeEventInterventionPreview(target_event=dict(target), void_event=event.to_dict())


def _repair_override_payload(overrides: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(overrides or {}).items() if value not in (None, "")}


def _normalized_repair_core_event(target: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target)
    for key, value in _repair_override_payload(overrides).items():
        if key in {
            "broker",
            "account",
            "symbol",
            "option_type",
            "side",
            "position_effect",
            "contracts",
            "price",
            "strike",
            "multiplier",
            "expiration_ymd",
            "currency",
            "trade_time_ms",
            "order_id",
        }:
            merged[key] = value

    raw_payload = dict(target.get("raw_payload") or {})
    for key, value in _repair_override_payload(overrides).items():
        if key in {"record_id", "close_target_source_event_id"}:
            raw_payload[key] = value

    merged["source_type"] = "manual_trade_event"
    merged["source_name"] = "cli_trade_event_repair"
    merged["broker"] = normalize_broker(merged.get("broker"))
    merged["account"] = normalize_account(merged.get("account"))
    merged["symbol"] = _canonical_trade_symbol(merged.get("symbol"))
    merged["option_type"] = normalize_option_type(merged.get("option_type"))
    merged["side"] = normalize_trade_side(merged.get("side")) or str(merged.get("side") or "").strip().lower()
    merged["position_effect"] = normalize_position_effect(merged.get("position_effect")) or str(merged.get("position_effect") or "").strip().lower()
    merged["contracts"] = int(safe_float(merged.get("contracts")) or 0)
    merged["price"] = float(safe_float(merged.get("price")) or 0.0)
    merged["strike"] = safe_float(merged.get("strike"))
    raw_multiplier = safe_float(merged.get("multiplier"))
    merged["multiplier"] = int(float(raw_multiplier)) if raw_multiplier is not None else None
    merged["expiration_ymd"] = normalize_contract_expiration(merged.get("expiration_ymd"))
    merged["currency"] = normalize_currency(merged.get("currency"))
    merged["trade_time_ms"] = int(safe_float(merged.get("trade_time_ms")) or now_ms())
    merged["order_id"] = str(merged.get("order_id") or "").strip() or None
    merged["multiplier_source"] = str(merged.get("multiplier_source") or "").strip() or None
    merged["raw_payload"] = raw_payload
    return merged


def _manual_repair_event_ids(
    *,
    target_event_id: str,
    core_event: dict[str, Any],
    overrides: dict[str, Any],
    repair_reason: str,
) -> tuple[str, str]:
    seed = {
        "target_event_id": str(target_event_id or "").strip(),
        "core_event": {key: value for key, value in core_event.items() if key != "event_id"},
        "overrides": _repair_override_payload(overrides),
        "repair_reason": str(repair_reason or "").strip(),
    }
    digest = hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return f"manual-repair-void-{digest}", f"manual-repair-{digest}"


def build_manual_repair_preview(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    target = _get_trade_event_dict(repo, event_id=target_event_id)
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
    _assert_trade_event_can_be_manually_voided(events, target)
    downstream_dependencies = _repair_downstream_dependencies(events, target)
    if downstream_dependencies:
        raise ValueError(
            "cannot repair an open event with downstream close/adjust dependencies: "
            f"{target_event_id}; void or repair downstream events first; "
            f"dependencies={json.dumps(downstream_dependencies, ensure_ascii=False, sort_keys=True)}"
        )

    core = _normalized_repair_core_event(target, overrides)
    void_event_id, repair_event_id = _manual_repair_event_ids(
        target_event_id=target_event_id,
        core_event=core,
        overrides=overrides,
        repair_reason=repair_reason,
    )
    core_raw_payload = dict(core.get("raw_payload") or {})
    core_raw_payload.update(
        {
            "source": "om trade-events",
            "mode": "manual_repair",
            "repair_target_event_id": str(target_event_id),
            "repair_reason": str(repair_reason or ""),
            "repair_overrides": _repair_override_payload(overrides),
        }
    )
    repair_event = _repair_trade_event(event_id=repair_event_id, core=core, raw_payload=core_raw_payload)
    void_event = _void_trade_event(
        event_id=void_event_id,
        target=target,
        target_event_id=target_event_id,
        reason=str(repair_reason or "manual_repair"),
        mode="manual_repair_void",
        source="om trade-events",
        as_of_ms=as_of_ms,
        repair_event_id=repair_event_id,
    )
    return TradeEventInterventionPreview(
        target_event=target,
        void_event=void_event.to_dict(),
        repair_event=repair_event.to_dict(),
    )


def persist_manual_repair_event(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    preview = build_manual_repair_preview(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )
    void_event = _preview_event_to_trade_event(preview["void_event"])
    repair_event = _preview_event_to_trade_event(preview["repair_event"])

    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is not None:
            void_created = sqlite_repo.upsert_trade_event(void_event, conn=conn)
            repair_created = sqlite_repo.upsert_trade_event(repair_event, conn=conn)
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events(conn=conn))
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records, conn=conn)
        else:
            void_created = sqlite_repo.upsert_trade_event(void_event)
            repair_created = sqlite_repo.upsert_trade_event(repair_event)
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events())
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records)
        result = {
            "target_event_id": str(target_event_id),
            "void_event_id": void_event.event_id,
            "repair_event_id": repair_event.event_id,
            "void_created": bool(void_created),
            "repair_created": bool(repair_created),
            "position_lot_count": int(lot_count),
        }
        result.update(projection_diagnostics_summary(projection.diagnostics))
        return result

    result = with_sqlite_repo_transaction(repo, _run)
    result["preview"] = preview.to_payload()
    return LedgerWriteResult(
        event_id=str(result.get("repair_event_id") or ""),
        position_lot_count=int(result.get("position_lot_count") or 0),
        details={key: value for key, value in result.items() if key not in {"event_id", "position_lot_count"}},
    )
