from __future__ import annotations

from domain.domain.ledger.events import lot_id_for_open_event

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
import uuid
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event, position_lots_fingerprint
from domain.domain.ledger.fees import FeeBasis
from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
    now_ms,
    strategy_metadata_fields_from_payload,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import (
    require_option_multiplier,
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from domain.domain.trade_execution import canonical_decimal, canonical_trade_execution_content, execution_instant_milliseconds, execution_source_status, futu_execution_time
from domain.domain.trade_account_identity import extract_visible_account_fields
from domain.domain.wheel import effective_wheel_events
from src.application.cash_conversion import (
    attach_trade_event_cash_conversions,
    load_cash_fx_payload,
)
from src.application.ledger.assigned_stock_projection import (
    project_assigned_stock_lifecycle_from_rows,
)
from src.application.ledger.event_codec import stored_trade_event_to_ledger_event, valid_void_target_event_id
from src.application.ledger.current_decision_projection import (
    capture_trade_event_decision_projection_fence,
    defer_current_decision_projection,
    finalize_current_decision_projection,
)
from src.application.ledger.position_projection_runtime import (
    run_position_projection_in_transaction,
)
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
    with_sqlite_repo_transaction,
    with_sqlite_repo_writer_lock,
)
from src.application.ledger.results import LedgerWriteResult, TradeEventInterventionPreview
from src.application.ledger.writer import (
    projection_diagnostics_summary,
)
from src.infrastructure.feishu_bitable import safe_float


_ORDER_IDENTITY_OVERRIDE_KEYS = frozenset({"futu_account_id", "order_id"})
_ORDER_IDENTITY_PROVENANCE_SCHEMA = "futu_order_identity_binding.v1"
_OPEND_TRADE_TIME_OVERRIDE_KEYS = frozenset({"trade_time_ms"})
_OPEND_TRADE_TIME_EVIDENCE_SCHEMA = "opend_order_evidence.v1"
_OPEND_TRADE_TIME_PROVENANCE_SCHEMA = "opend_trade_time_correction.v1"


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _void_event_for_target(events: list[dict[str, Any]], target_event_id: str) -> dict[str, Any] | None:
    target = str(target_event_id or "").strip()
    if not target:
        return None
    for event in events:
        if valid_void_target_event_id(event) == target:
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


def _lot_opened_at_ms(fields: Mapping[str, Any]) -> int:
    """The lot payload's opening instant.

    ``opened_at`` converged onto ``opened_at_ms`` (``write-side-definition.md`` §2);
    the retired flat key stays readable for a row written before the shape switch.
    """
    return int(fields.get("opened_at_ms") or fields.get("opened_at") or 0)


#: Both spellings, so a payload-carried compare leaves no key behind: the
#: membership comparison below is a whole-dict compare.
_LOT_OPENED_AT_KEYS = ("opened_at_ms", "opened_at")


def _contract_key_from_event_dict(event: dict[str, Any]) -> ContractKey:
    return ContractKey.from_values(
        broker=event.get("broker"),
        account=event.get("account"),
        underlying_symbol=_canonical_trade_symbol(event.get("symbol")),
        option_type=event.get("option_type"),
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
        multiplier=require_option_multiplier(target.get("multiplier")),
        target_event_id=str(target_event_id),
        raw_payload=raw_payload,
    )


def _repair_trade_event(*, event_id: str, core: dict[str, Any], raw_payload: dict[str, Any]) -> TradeEvent:
    original_event_type = str(core.get("event_type") or "").strip().lower()
    lifecycle_close_type = str(raw_payload.get("close_type") or "").strip().lower()
    event_type = (
        original_event_type
        if original_event_type in {"expire_close", "assignment", "exercise"}
        else (
            lifecycle_close_type
            if lifecycle_close_type in {"expire_close", "assignment", "exercise"}
            else _event_type_from_position_effect(core.get("position_effect"))
        )
    )
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
        multiplier=require_option_multiplier(core.get("multiplier")),
        fees=float(core.get("fees") or 0.0),
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
    target_lot_id = lot_id_for_open_event(_preview_event_to_trade_event(target))
    target_position_side = _event_position_side(target)
    target_sort_key = _event_sort_key(target)
    voided_event_ids: set[str] = set()
    for event in events:
        void_target_event_id = valid_void_target_event_id(event)
        if void_target_event_id:
            voided_event_ids.add(void_target_event_id)
    out: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id == target_event_id or event_id in voided_event_ids:
            continue
        if _event_sort_key(event) <= target_sort_key:
            continue
        effect = normalize_position_effect(event.get("position_effect")) or str(event.get("position_effect") or "").strip().lower()
        payload = _event_payload(event)
        lot_id = str(payload.get("record_id") or "").strip()
        source_event_id = str(
            payload.get("close_target_source_event_id")
            or payload.get("adjust_target_source_event_id")
            or ""
        ).strip()
        explicit_target = bool(
            (target_lot_id and lot_id == target_lot_id)
            or (target_event_id and source_event_id == target_event_id)
        )
        if explicit_target:
            out.append(
                {
                    "event_id": event_id,
                    "position_effect": effect,
                    "dependency": "explicit_target",
                    "record_id": lot_id or None,
                    "source_event_id": source_event_id or None,
                }
            )
            continue
        if effect != "close":
            continue
        if lot_id or source_event_id:
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


def _dependency_cutoff_ms(rows: Mapping[str, Any], target: Mapping[str, Any]) -> int:
    values = [int(safe_float(target.get("trade_time_ms")) or 0)]
    for key, time_fields in (
        ("trade_events", ("trade_time_ms", "event_time_ms")),
        ("account_assigned_stock_events", ("trade_time_ms", "event_time_ms")),
        ("account_wheel_events", ("occurred_at_ms", "recorded_at_ms")),
    ):
        for row in rows.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            values.extend(int(safe_float(row.get(field)) or 0) for field in time_fields)
    return max(1, *values)


def _explicit_lot_id(event: Mapping[str, Any]) -> str:
    payload = event.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    strategy_metadata = strategy_metadata_fields_from_payload(
        dict(payload),
        include_legacy=True,
    )
    for source in (event, payload, strategy_metadata):
        for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _stock_dependencies(
    rows: Mapping[str, Any],
    target: Mapping[str, Any],
) -> list[dict[str, str]]:
    target_event_id = str(target.get("event_id") or "").strip()
    if str(target.get("event_type") or "").strip().lower() not in {"assignment", "exercise"}:
        return []
    account = normalize_account(target.get("account"))
    report = project_assigned_stock_lifecycle_from_rows(
        rows,
        account=account,
        as_of_ms=_dependency_cutoff_ms(rows, target),
    )
    lot_ids = {
        str(row.get("stock_lot_id") or "").strip()
        for row in report.get("_all_assigned_stock_lots") or []
        if isinstance(row, Mapping)
        and str(row.get("source_assignment_event_id") or "").strip() == target_event_id
        and str(row.get("stock_lot_id") or "").strip()
    }
    if not lot_ids:
        return []

    dependencies: set[tuple[str, str, str, str]] = set()
    effective_sale_ids: set[str] = set()
    for row in report.get("assigned_stock_sale_rows") or []:
        if not isinstance(row, Mapping):
            continue
        lot_id = str(row.get("stock_lot_id") or "").strip()
        event_id = str(row.get("stock_event_id") or row.get("event_id") or "").strip()
        if lot_id in lot_ids and event_id:
            effective_sale_ids.add(event_id)
            dependencies.add(("assigned_stock_sale", event_id, lot_id, "effective"))

    effective_call_ids: set[str] = set()
    for row in report.get("covered_call_allocations") or []:
        if not isinstance(row, Mapping):
            continue
        lot_id = str(row.get("stock_lot_id") or "").strip()
        event_id = str(row.get("open_event_id") or "").strip()
        if lot_id in lot_ids and event_id:
            effective_call_ids.add(event_id)
            dependencies.add(("covered_call", event_id, lot_id, "effective"))

    for row in rows.get("account_assigned_stock_events") or []:
        if not isinstance(row, Mapping):
            continue
        lot_id = _explicit_lot_id(row)
        event_id = str(row.get("stock_event_id") or row.get("event_id") or "").strip()
        if lot_id in lot_ids and event_id and event_id not in effective_sale_ids:
            dependencies.add(("assigned_stock_sale", event_id, lot_id, "unresolved"))

    trade_events = [
        dict(row)
        for row in rows.get("trade_events") or []
        if isinstance(row, Mapping)
        and normalize_account(row.get("account")) == account
    ]
    voided_ids = {
        target_id
        for row in trade_events
        for target_id in [valid_void_target_event_id(row)]
        if target_id
    }
    for row in trade_events:
        event_id = str(row.get("event_id") or "").strip()
        if (
            not event_id
            or event_id == target_event_id
            or event_id in voided_ids
            or valid_void_target_event_id(row) is not None
        ):
            continue
        lot_id = _explicit_lot_id(row)
        if lot_id not in lot_ids or event_id in effective_sale_ids or event_id in effective_call_ids:
            continue
        kind = (
            "covered_call"
            if normalize_position_effect(row.get("position_effect")) == "open"
            and normalize_option_type(row.get("option_type")) == "call"
            and _event_position_side(row) == "short"
            else "assigned_stock_sale"
            if str(row.get("event_type") or "").strip().lower() in {"assignment", "exercise"}
            else "stock_lot_reference"
        )
        dependencies.add((kind, event_id, lot_id, "unresolved"))

    wheel_rows = [
        dict(row)
        for row in rows.get("account_wheel_events") or []
        if isinstance(row, Mapping)
    ]
    effective_wheel, invalid_wheel = effective_wheel_events(wheel_rows)
    for lot_id in lot_ids:
        group = (account, lot_id)
        if invalid_wheel.get(group):
            event_ids = sorted(
                {
                    str(row.get("event_id") or "").strip()
                    for row in wheel_rows
                    if normalize_account(row.get("account")) == account
                    and str(row.get("stock_lot_id") or "").strip() == lot_id
                    and str(row.get("event_id") or "").strip()
                }
            ) or [f"wheel-unresolved:{lot_id}"]
            dependencies.update(
                ("wheel_event", event_id, lot_id, "unresolved")
                for event_id in event_ids
            )
            continue
        dependencies.update(
            (
                "wheel_event",
                str(row.get("event_id") or "").strip(),
                lot_id,
                "effective",
            )
            for row in effective_wheel
            if str(row.get("stock_lot_id") or "").strip() == lot_id
            and str(row.get("event_type") or "").strip() != "wheel_event_voided"
            and str(row.get("event_id") or "").strip()
        )

    return [
        {"kind": kind, "event_id": event_id, "lot_id": lot_id, "status": status}
        for kind, event_id, lot_id, status in sorted(dependencies)
    ]


def _intervention_context(
    sqlite_repo: Any,
    *,
    target_event_id: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    target_id = str(target_event_id or "").strip()
    stored_events = (
        sqlite_repo.list_trade_events(conn=conn)
        if conn is not None
        else sqlite_repo.list_trade_events()
    )
    events = [dict(row) for row in stored_events]
    target = next(
        (row for row in events if str(row.get("event_id") or "").strip() == target_id),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    if str(target.get("event_type") or "").strip().lower() not in {"assignment", "exercise"}:
        return events, target, []
    account = normalize_account(target.get("account"))
    rows = (
        sqlite_repo.read_lifecycle_account_rows(account=account, conn=conn)
        if conn is not None
        else sqlite_repo.read_lifecycle_account_rows(account=account)
    )
    events = [dict(row) for row in rows.get("trade_events") or [] if isinstance(row, Mapping)]
    target = next(
        (row for row in events if str(row.get("event_id") or "").strip() == target_id),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    return events, target, _stock_dependencies(rows, target)


def _assert_trade_event_can_be_manually_voided(
    events: list[dict[str, Any]],
    target: dict[str, Any],
    *,
    stock_dependencies: list[dict[str, str]] | None = None,
) -> None:
    target_event_id = str(target.get("event_id") or "").strip()
    if str(target.get("position_effect") or "").strip().lower() == "void":
        raise ValueError(f"cannot void a void event: {target_event_id}")
    existing_void = _void_event_for_target(events, target_event_id)
    if existing_void is not None:
        raise ValueError(
            "trade event already voided: "
            f"{target_event_id} via {str(existing_void.get('event_id') or '').strip()}"
        )
    if stock_dependencies:
        raise ValueError(
            "cannot void or repair an assignment/exercise with downstream stock dependencies: "
            f"{target_event_id}; dependencies="
            f"{json.dumps(stock_dependencies, ensure_ascii=False, sort_keys=True)}"
        )


def persist_manual_void_event(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("trade event void requires SQLite transaction authority")
        events, target, stock_dependencies = _intervention_context(
            sqlite_repo,
            target_event_id=target_event_id,
            conn=conn,
        )
        _assert_trade_event_can_be_manually_voided(
            events,
            target,
            stock_dependencies=stock_dependencies,
        )
        event = _void_trade_event(
            event_id=f"manual-void-{target_event_id}-{uuid.uuid4().hex}",
            target=target,
            target_event_id=target_event_id,
            reason=void_reason,
            mode="manual_void",
            source="om option-positions",
            as_of_ms=as_of_ms,
        )
        decision_fence = capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
        )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            (event,),
            conn=conn,
            mode="forced_full",
        )
        result = {
            "event_id": event.event_id,
            "record_id": None,
            "created": bool(runtime.created_flags[0]),
            "position_lot_count": int(runtime.position_lot_count),
            "decision_projection": defer_current_decision_projection(decision_fence),
        }
        result.update(projection_diagnostics_summary(runtime.diagnostics))
        return result

    return LedgerWriteResult.from_payload(
        with_sqlite_repo_transaction(
            repo,
            _run,
            require_projection_publication=True,
        )
    )


def build_manual_void_preview(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events, target, stock_dependencies = _intervention_context(
        sqlite_repo,
        target_event_id=target_event_id,
    )
    _assert_trade_event_can_be_manually_voided(
        events,
        target,
        stock_dependencies=stock_dependencies,
    )
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
    return {key: value for key, value in dict(overrides or {}).items() if key == "multiplier" or value not in (None, "")}


def is_order_identity_repair_request(overrides: dict[str, Any]) -> bool:
    raw = dict(overrides or {})
    keys = set(_repair_override_payload(raw))
    identity_requested = any(raw.get(key) is not None for key in _ORDER_IDENTITY_OVERRIDE_KEYS)
    return identity_requested and not (keys - _ORDER_IDENTITY_OVERRIDE_KEYS)


def is_opend_trade_time_repair_request(overrides: dict[str, Any]) -> bool:
    effective = _repair_override_payload(overrides)
    return set(effective) == _OPEND_TRADE_TIME_OVERRIDE_KEYS


def is_futu_environment_repair_request(overrides: dict[str, Any]) -> bool:
    return overrides.get("trd_env") is not None or "futu_environment_evidence" in overrides


def _futu_environment_binding_plan(
    repo: Any, *, target_event_id: str, overrides: dict[str, Any], repair_reason: str,
    conn: sqlite3.Connection | None = None, bound_at_ms: int | None = None,
) -> dict[str, Any]:
    effective = _repair_override_payload(overrides)
    if set(effective) != {"trd_env", "futu_environment_evidence"} or effective["trd_env"] != "REAL":
        raise ValueError("Futu environment repair only accepts REAL and exact OpenD history evidence")
    reason = _order_identity_reason(repair_reason)
    rows = _storage_trade_event_rows(repo, conn=conn)
    target = next((row for row in rows if row["event_id"] == target_event_id), None)
    if target is None or target_event_id in {valid_void_target_event_id(_storage_payload(row)) for row in rows}:
        raise ValueError("Futu environment repair target is missing or voided")
    before = _storage_payload(target)
    event, diagnostics = stored_trade_event_to_ledger_event(before)
    if (event is None or any(d.severity == "error" for d in diagnostics)
            or event.event_type != "open" or event.asset_type != "option"
            or normalize_broker(event.contract_key.broker) != "富途" or event.source != "opend_push"):
        raise ValueError("Futu environment repair requires a canonical OpenD option open")
    raw = before.get("raw_payload") or {}
    physical = str(raw.get("futu_account_id") or "")
    deal_id = str(raw.get("source_deal_id") or raw.get("deal_id") or "")
    if (not physical.isascii() or not physical.isdigit() or physical.startswith("0") or not deal_id
            or target_event_id != f"futu:{event.contract_key.account}:{physical}:{deal_id}"
            or not raw.get("order_id") or not raw.get("code")):
        raise ValueError("Futu environment repair requires exact stored source identity")
    if (raw.get("execution_input") is not None
            or raw.get("trd_env") not in (None, "", "REAL")
            or raw.get("environment") not in (None, "", "REAL")
            or raw.get("broker_account_id") not in (None, "", f"futu:REAL:{physical}")):
        raise ValueError("Futu environment repair cannot replace existing execution identity")
    proof = effective["futu_environment_evidence"]
    if not isinstance(proof, dict):
        raise ValueError("Futu environment repair evidence must be an object")
    receipt = proof.get("diagnostics")
    if not isinstance(receipt, dict):
        raise ValueError("Futu environment repair history receipt must be an object")
    accounts = receipt.get("account_results")
    if not isinstance(accounts, list) or any(not isinstance(row, dict) for row in accounts):
        raise ValueError("Futu environment repair account receipt must be a list of objects")
    observed_ms = execution_instant_milliseconds(receipt.get("observed_at_utc"))
    instant = now_ms()
    if observed_ms is None or not 0 <= instant - observed_ms <= 60_000:
        raise ValueError("Futu environment repair evidence is stale or future-dated")
    if (receipt.get("schema_version") != "futu_history_query_receipt.v1"
            or receipt.get("dataset") != "executions" or receipt.get("trd_env") != "REAL"
            or receipt.get("coverage_status") != "complete" or len(accounts) != 1):
        raise ValueError("Futu environment repair requires complete single-account history")
    account = accounts[0]
    if (account.get("futu_account_id") != physical or account.get("trd_env") != "REAL"
            or account.get("coverage_status") != "complete"
            or account.get("coverage_complete") is not True or account.get("pagination_complete") is not True
            or account.get("truncated") is True or account.get("error") or account.get("ret") not in (0, "0")):
        raise ValueError("Futu environment repair account or coverage mismatch")
    for prefix in ("requested", "covered"):
        start = execution_instant_milliseconds(account.get(prefix + "_start_utc"))
        end = execution_instant_milliseconds(account.get(prefix + "_end_utc"))
        if start is None or end is None or not start <= event.event_time_ms < end:
            raise ValueError("Futu environment repair history does not cover source time")
    matches = proof.get("matches")
    if not isinstance(matches, list) or len(matches) != 1 or not isinstance(matches[0], dict):
        raise ValueError("Futu environment repair requires one unique matching deal")
    deal = matches[0]
    if (str(deal.get("deal_id") or deal.get("dealID") or "") != deal_id
            or str(deal.get("order_id") or "") != str(raw["order_id"])
            or str(deal.get("code") or "") != str(raw["code"])
            or deal.get("futu_account_id") != physical or deal.get("environment") != "REAL"
            or deal.get("broker_account_id") != f"futu:REAL:{physical}"
            or deal.get("trd_env") not in (None, "", "REAL")
            or any(value != physical for value in extract_visible_account_fields(deal).values())
            or deal.get("external_id_namespace") != "futu.deal"
            or deal.get("external_order_namespace") != "futu.order"
            or execution_source_status(deal) not in (None, "ok")):
        raise ValueError("Futu environment repair source deal identity mismatch")
    # Multiplier and canonical underlying are unchanged local instrument facts;
    # history supplies the exact stored source code and all fill economics.
    content = canonical_trade_execution_content({**deal, "internal_account": event.contract_key.account,
        "underlying_symbol": event.contract_key.underlying_symbol, "multiplier": event.multiplier})
    economic, associations = content["economic"], content["associations"]
    instrument = economic["instrument"]
    if (content["errors"] or economic["side"] != normalize_trade_side(event.raw_payload.get("side"))
            or economic["quantity"] != canonical_decimal(str(event.contracts)) or economic["price"] != canonical_decimal(str(event.price))
            or economic["currency"] != event.currency or associations["position_effect"] != "open"
            or instrument.get("option_type") != event.contract_key.option_type
            or instrument.get("strike") != canonical_decimal(str(event.contract_key.strike))
            or instrument.get("expiration_ymd") != event.contract_key.expiration_ymd
            or execution_instant_milliseconds(economic["occurred_at_utc"]) != event.event_time_ms):
        raise ValueError("Futu environment repair source deal economics mismatch")
    preserved = deepcopy(before)
    for key in ("trd_env", "futu_environment_provenance"):
        preserved.setdefault("raw_payload", {}).pop(key, None)
    preserved_hash = canonical_sha256(preserved)
    prior = raw.get("futu_environment_provenance")
    before_hash = _sha256_text(str(target["event_json"]))
    source_hash = prior.get("expected_before_sha256") if isinstance(prior, dict) else before_hash
    input_hash = canonical_sha256({"event_id": target_event_id, "before_sha256": source_hash,
        "deal_id": deal_id, "order_id": raw["order_id"], "content": content, "reason": reason})
    common = {"operation": "futu_environment_binding", "target_event_id": target_event_id,
        "before_environment": raw.get("trd_env") or None, "after_environment": "REAL",
        "futu_account_id": physical, "source_deal_id": deal_id, "expected_input_hash": input_hash,
        "expected_before_sha256": before_hash, "before_json": str(target["event_json"])}
    if raw.get("trd_env") == "REAL":
        if prior is not None and (not isinstance(prior, dict) or prior.get("input_hash") != input_hash
                or prior.get("preserved_payload_sha256") != preserved_hash):
            raise ValueError("Futu environment repair provenance conflict")
        return common | {"binding_status": "no_op", "after_json": str(target["event_json"]),
                         "after_sha256": before_hash}
    if prior is not None:
        raise ValueError("Futu environment repair provenance conflict")
    if bound_at_ms is None:
        return common | {"binding_status": "ready"}
    after = deepcopy(before)
    after["raw_payload"] = {**raw, "trd_env": "REAL", "futu_environment_provenance": {
        "schema_version": "futu_environment_binding.v1", "binding_id": "futu_environment_binding_" + input_hash[:24],
        "source": "opend_history", "reason": reason, "input_hash": input_hash,
        "expected_before_sha256": before_hash, "preserved_payload_sha256": preserved_hash,
        "bound_at_ms": bound_at_ms, "evidence": proof}}
    before_outer, after_outer = deepcopy(before), deepcopy(after)
    for payload in (before_outer, after_outer):
        for key in ("trd_env", "futu_environment_provenance"):
            payload.setdefault("raw_payload", {}).pop(key, None)
    if before_outer != after_outer:
        raise ValueError("Futu environment repair changed non-environment data")
    after_json = json.dumps(after, ensure_ascii=False, sort_keys=True)
    return common | {"binding_status": "ready", "after_json": after_json,
                     "after_sha256": _sha256_text(after_json), "bound_at_ms": bound_at_ms}


def preview_manual_futu_environment_binding(repo: Any, **kwargs: Any) -> dict[str, Any]:
    plan = _futu_environment_binding_plan(repo, **kwargs)
    return {k: v for k, v in (plan | {"mode": "no_op" if plan["binding_status"] == "no_op" else "dry_run",
                                    "advisory": True}).items()
            if k not in {"before_json", "after_json", "binding_status"}}


def persist_manual_futu_environment_binding(repo: Any, *, expected_input_hash: str | None, **kwargs: Any) -> dict[str, Any]:
    if not expected_input_hash:
        raise ValueError("Futu environment repair requires --expected-input-hash from preview")
    return _persist_manual_metadata_binding(repo, plan_builder=_futu_environment_binding_plan,
                                           expected_input_hash=expected_input_hash, **kwargs)


def _normalized_order_identity(overrides: dict[str, Any]) -> tuple[str, str]:
    effective = _repair_override_payload(overrides)
    if set(effective) != _ORDER_IDENTITY_OVERRIDE_KEYS:
        raise ValueError("order identity repair requires both --futu-account-id and --order-id")
    futu_account_id = str(effective["futu_account_id"])
    order_id = str(effective["order_id"])
    if (
        not futu_account_id.isascii()
        or not futu_account_id.isdigit()
        or futu_account_id.startswith("0")
    ):
        raise ValueError("futu_account_id must be canonical positive integer text")
    if not order_id or any(
        char.isspace() or unicodedata.category(char).startswith("C") for char in order_id
    ):
        raise ValueError("order_id must not contain whitespace or control characters")
    return futu_account_id, order_id


def _order_identity_reason(repair_reason: str) -> str:
    reason = str(repair_reason or "").strip()
    if reason == "manual_repair" or "opend" not in reason.lower():
        raise ValueError("order identity repair reason must reference the manual OpenD evidence")
    return reason


def _opend_trade_time_reason(repair_reason: str) -> str:
    reason = str(repair_reason or "").strip()
    if reason == "manual_repair" or "opend" not in reason.lower():
        raise ValueError("trade time correction reason must reference the OpenD evidence")
    return reason


def _storage_trade_event_rows(repo: Any, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    reader = getattr(sqlite_repo, "list_position_projection_event_rows", None)
    if not callable(reader):
        raise TypeError("order identity repair requires raw trade-event storage access")
    return [dict(item) for item in reader(conn=conn)]


def _storage_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row.get("event_json") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"trade event JSON is invalid: event_id={row.get('event_id')}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"trade event JSON must be an object: event_id={row.get('event_id')}")
    return payload


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding_id(event_id: str, futu_account_id: str, order_id: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join((event_id, futu_account_id, order_id)).encode("utf-8")
    ).hexdigest()[:24]
    return f"order_identity_binding_{digest}"


def _trade_time_correction_id(event_id: str, before_ms: int, after_ms: int) -> str:
    digest = hashlib.sha256(
        "\x1f".join((event_id, str(before_ms), str(after_ms))).encode("utf-8")
    ).hexdigest()[:24]
    return f"opend_trade_time_correction_{digest}"


def _assert_identity_only_payload_change(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    before_outer = deepcopy(before)
    after_outer = deepcopy(after)
    before_raw = before_outer.pop("raw_payload", {})
    after_raw = after_outer.pop("raw_payload", {})
    if before_outer != after_outer or not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
        raise ValueError("order identity repair changed non-identity event data")
    for key in (*_ORDER_IDENTITY_OVERRIDE_KEYS, "order_identity_provenance"):
        before_raw.pop(key, None)
        after_raw.pop(key, None)
    if before_raw != after_raw:
        raise ValueError("order identity repair changed non-identity raw payload data")


def _assert_trade_time_only_payload_change(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    before_outer = deepcopy(before)
    after_outer = deepcopy(after)
    before_outer.pop("event_time_ms", None)
    after_outer.pop("event_time_ms", None)
    before_raw = before_outer.pop("raw_payload", {})
    after_raw = after_outer.pop("raw_payload", {})
    if before_outer != after_outer or not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
        raise ValueError("trade time correction changed non-time event data")
    if "cash_conversions" in after_raw:
        raise ValueError("trade time correction retained stale cash conversions")
    before_raw.pop("cash_conversions", None)
    before_raw.pop("trade_time_correction_provenance", None)
    after_raw.pop("trade_time_correction_provenance", None)
    if before_raw != after_raw:
        raise ValueError("trade time correction changed unrelated raw payload data")

def _validated_opend_trade_time_evidence(
    event: TradeEvent,
    raw_payload: dict[str, Any],
) -> tuple[int, list[str], int]:
    evidence = raw_payload.get("opend_order_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("trade time correction requires stored OpenD order evidence")
    if str(evidence.get("provider") or "").strip().lower() != "opend":
        raise ValueError("trade time correction evidence provider must be OpenD")
    if str(evidence.get("schema_version") or "").strip() != _OPEND_TRADE_TIME_EVIDENCE_SCHEMA:
        raise ValueError("trade time correction requires opend_order_evidence.v1")
    orders = evidence.get("orders")
    if not isinstance(orders, list) or not orders:
        raise ValueError("trade time correction requires at least one OpenD order")
    classification = str(evidence.get("classification") or "").strip()
    if classification in {"single_order", "contract_lineage"} and len(orders) != 1:
        raise ValueError("single-order OpenD evidence must contain exactly one order")
    if classification == "multi_order_aggregate" and len(orders) < 2:
        raise ValueError("multi_order_aggregate OpenD evidence must contain multiple orders")
    if classification not in {
        "single_order",
        "contract_lineage",
        "multi_order_aggregate",
        "opend_price_override",
    }:
        raise ValueError("trade time correction OpenD evidence classification is unsupported")

    order_ids: list[str] = []
    trade_times: list[int] = []
    quantity = 0
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("trade time correction OpenD order must be an object")
        order_id = str(order.get("order_id") or "").strip()
        futu_account_id = str(order.get("futu_account_id") or "").strip()
        raw_time = order.get("trade_time_ms")
        raw_quantity = order.get("quantity")
        if not order_id or not futu_account_id:
            raise ValueError("trade time correction OpenD order identity is incomplete")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int) or raw_time <= 0:
            raise ValueError("trade time correction OpenD order time is invalid")
        if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, int) or raw_quantity <= 0:
            raise ValueError("trade time correction OpenD order quantity is invalid")
        order_ids.append(order_id)
        trade_times.append(raw_time)
        quantity += raw_quantity
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("trade time correction OpenD order ids must be unique")
    if quantity != int(event.contracts):
        raise ValueError("trade time correction OpenD quantity does not match the event")
    observed_at_ms = evidence.get("observed_at_ms")
    if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
        raise ValueError("trade time correction OpenD observation time is invalid")
    return min(trade_times), sorted(order_ids), observed_at_ms


def _order_identity_binding_plan(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    conn: sqlite3.Connection | None = None,
    bound_at_ms: int | None = None,
) -> dict[str, Any]:
    futu_account_id, order_id = _normalized_order_identity(overrides)
    reason = _order_identity_reason(repair_reason)
    rows = _storage_trade_event_rows(repo, conn=conn)
    target_id = str(target_event_id or "").strip()
    target_row = next((item for item in rows if str(item.get("event_id") or "") == target_id), None)
    if target_row is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    payloads = [(item, _storage_payload(item)) for item in rows]
    voided_event_ids = {
        target
        for _row, payload in payloads
        if (target := valid_void_target_event_id(payload))
    }
    if target_id in voided_event_ids:
        raise ValueError(f"trade event already voided: {target_id}")

    before_json = str(target_row["event_json"])
    before_payload = _storage_payload(target_row)
    event, diagnostics = stored_trade_event_to_ledger_event(before_payload)
    errors = [item.code for item in diagnostics if item.severity == "error"]
    if event is None or errors:
        raise ValueError(
            "order identity repair requires a canonical trade event: "
            f"{target_id}; diagnostics={','.join(errors) or 'event_decode_failed'}"
        )
    if event.event_type != "open":
        raise ValueError("order identity repair only supports option open events")
    if normalize_broker(event.contract_key.broker) != "富途":
        raise ValueError("order identity repair only supports Futu events")
    raw_payload = before_payload.get("raw_payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("trade event raw_payload must be an object")
    existing = (
        str(raw_payload.get("futu_account_id") or "").strip(),
        str(raw_payload.get("order_id") or "").strip(),
    )
    requested = (futu_account_id, order_id)
    for row, payload in payloads:
        other_id = str(row.get("event_id") or "")
        if other_id == target_id or other_id in voided_event_ids:
            continue
        other_raw = payload.get("raw_payload") or {}
        if not isinstance(other_raw, dict):
            continue
        other_identity = (
            str(other_raw.get("futu_account_id") or "").strip(),
            str(other_raw.get("order_id") or "").strip(),
        )
        if other_identity == requested:
            raise ValueError(f"order identity is already used by active event: {other_id}")

    expected_before_sha256 = _sha256_text(before_json)
    common = {
        "operation": "futu_order_identity_binding",
        "target_event_id": target_id,
        "before_identity": {
            "futu_account_id": existing[0] or None,
            "order_id": existing[1] or None,
        },
        "after_identity": {
            "futu_account_id": futu_account_id,
            "order_id": order_id,
        },
        "futu_account_id": futu_account_id,
        "order_id": order_id,
        "expected_before_sha256": expected_before_sha256,
        "fee_basis": fee_fact_for_event(event).basis.value,
    }
    if existing == requested:
        return common | {
            "binding_status": "no_op",
            "before_json": before_json,
            "after_json": before_json,
            "after_sha256": expected_before_sha256,
        }
    if fee_fact_for_event(event).basis == FeeBasis.ACTUAL:
        raise ValueError("order identity repair does not apply to an event with actual fee evidence")
    if any(existing):
        raise ValueError("order identity repair cannot fill a partial or conflicting identity")
    if "order_identity_provenance" in raw_payload:
        raise ValueError("order identity provenance already exists")
    if bound_at_ms is None:
        return common | {"binding_status": "ready", "before_json": before_json}

    after_payload = deepcopy(before_payload)
    after_raw = dict(raw_payload)
    after_raw.update(
        {
            "futu_account_id": futu_account_id,
            "order_id": order_id,
            "order_identity_provenance": {
                "schema_version": _ORDER_IDENTITY_PROVENANCE_SCHEMA,
                "binding_id": _binding_id(target_id, futu_account_id, order_id),
                "source": "manual_trade_event_repair",
                "reason": reason,
                "before_identity": {"futu_account_id": None, "order_id": None},
                "expected_before_sha256": expected_before_sha256,
                "bound_at_ms": int(bound_at_ms),
            },
        }
    )
    after_payload["raw_payload"] = after_raw
    _assert_identity_only_payload_change(before_payload, after_payload)
    after_event, after_diagnostics = stored_trade_event_to_ledger_event(after_payload)
    if after_event is None or any(item.severity == "error" for item in after_diagnostics):
        raise ValueError("order identity repair produced an invalid canonical trade event")
    after_json = json.dumps(after_payload, ensure_ascii=False, sort_keys=True)
    return common | {
        "binding_status": "ready",
        "before_json": before_json,
        "after_json": after_json,
        "after_sha256": _sha256_text(after_json),
        "bound_at_ms": int(bound_at_ms),
    }


def preview_manual_order_identity_binding(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
) -> dict[str, Any]:
    plan = _order_identity_binding_plan(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
    )
    return {
        key: value
        for key, value in (
            plan
            | {
                "mode": "no_op" if plan["binding_status"] == "no_op" else "dry_run",
                "advisory": True,
            }
        ).items()
        if key not in {"before_json", "after_json", "binding_status"}
    }


def persist_manual_order_identity_binding(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
) -> dict[str, Any]:
    return _persist_manual_metadata_binding(repo, target_event_id=target_event_id,
        overrides=overrides, repair_reason=repair_reason, plan_builder=_order_identity_binding_plan)


def _persist_manual_metadata_binding(
    repo: Any, *, target_event_id: str, overrides: dict[str, Any], repair_reason: str,
    plan_builder: Any, expected_input_hash: str | None = None,
) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("identity repair requires SQLite transaction authority")
        applied_at_ms = now_ms()
        plan = plan_builder(
            sqlite_repo,
            target_event_id=target_event_id,
            overrides=overrides,
            repair_reason=repair_reason,
            conn=conn,
            bound_at_ms=applied_at_ms,
        )
        if expected_input_hash is not None and plan.get("expected_input_hash") != expected_input_hash:
            raise ValueError("Futu environment repair preview input hash changed")
        if plan["binding_status"] == "no_op":
            return {
                key: value
                for key, value in (plan | {"mode": "no_op", "advisory": False}).items()
                if key not in {"before_json", "after_json", "binding_status"}
            }

        before_lots = sqlite_repo.list_position_lots(conn=conn)
        before_fingerprint = position_lots_fingerprint(before_lots)
        source_before = int(
            sqlite_repo.read_position_projection_source_state(conn=conn).get("source_generation") or 0
        )
        fence = capture_trade_event_decision_projection_fence(sqlite_repo, conn=conn)
        updated = sqlite_repo.compare_and_swap_trade_event_order_identity_json(
            event_id=plan["target_event_id"],
            expected_event_json=plan["before_json"],
            replacement_event_json=plan["after_json"],
            updated_at_ms=applied_at_ms,
            conn=conn,
        )
        if not updated:
            raise ValueError(f"order identity repair CAS conflict: {plan['target_event_id']}")
        readback = next(
            (
                item
                for item in _storage_trade_event_rows(sqlite_repo, conn=conn)
                if str(item.get("event_id") or "") == plan["target_event_id"]
            ),
            None,
        )
        if readback is None or str(readback.get("event_json") or "") != plan["after_json"]:
            raise ValueError(f"order identity repair readback failed: {plan['target_event_id']}")

        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            (),
            conn=conn,
            mode="forced_full",
        )
        publication = runtime.publication
        if publication.added or publication.changed or publication.removed:
            raise ValueError("order identity repair changed position lots")
        after_fingerprint = position_lots_fingerprint(sqlite_repo.list_position_lots(conn=conn))
        if after_fingerprint != before_fingerprint:
            raise ValueError("order identity repair changed the position lot fingerprint")
        decision = (
            finalize_current_decision_projection(
                sqlite_repo,
                fence=fence,
                updated_at_ms=applied_at_ms,
                conn=conn,
            )
            if fence is not None
            else None
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        source_after = int(
            sqlite_repo.read_position_projection_source_state(conn=conn).get("source_generation") or 0
        )
        return {
            key: value
            for key, value in (
                plan
                | {
                    "mode": "applied",
                    "advisory": False,
                    "position_lot_count": int(runtime.position_lot_count),
                    "position_lots_fingerprint": after_fingerprint,
                    "projection_source_generation_before": source_before,
                    "projection_source_generation_after": source_after,
                    "decision_projection": decision,
                }
            ).items()
            if key not in {"before_json", "after_json", "binding_status"}
        }

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )


def _preview_futu_raw_trade_time(
    event: TradeEvent,
    *,
    raw_payload: dict[str, Any],
    before_json: str,
    requested_time_ms: int,
) -> dict[str, Any]:
    """Inspect stored raw time only; this is not an executable repair plan."""
    if event.source != "opend_push" or event.event_type not in {"open", "close", "expire_close"}:
        raise ValueError("raw Futu time preview requires an OpenD fill event")
    if event.asset_type != "option":
        raise ValueError("raw Futu time preview only supports option events")
    if "trade_time_correction_provenance" in raw_payload:
        raise ValueError("trade time correction provenance already exists")
    # The archived execution_input may contain the original normalization error.
    # Only the raw provider fields are evidence for reinterpreting the instant.
    evidence = futu_execution_time(raw_payload)
    evidence_ms = execution_instant_milliseconds(evidence["occurred_at_utc"])
    if evidence["errors"] or evidence_ms is None:
        raise ValueError(f"raw Futu trade time unavailable: {','.join(evidence['errors'])}")
    if requested_time_ms != evidence_ms:
        raise ValueError(f"trade_time_ms must equal the stored raw Futu time: {evidence_ms}")
    execution = raw_payload.get("execution_input")
    conversions = raw_payload.get("cash_conversions")
    return {
        "operation": "futu_raw_trade_time_preview",
        "correction_status": "preview_only",
        "apply_supported": False,
        "evidence_scope": "stored_raw_time_only",
        "target_event_id": event.event_id,
        "target_event_type": event.event_type,
        "target_lot_id": lot_id_for_open_event(event) if event.event_type == "open" else event.target_lot_id,
        "before_trade_time_ms": event.event_time_ms,
        "after_trade_time_ms": evidence_ms,
        "time_change_required": event.event_time_ms != evidence_ms,
        "source_time": evidence["source_time"],
        "source_timezone": evidence["source_timezone"],
        "occurred_at_utc": evidence["occurred_at_utc"],
        "expected_before_sha256": _sha256_text(before_json),
        "stored_execution_input_present": execution is not None,
        "stored_execution_time": execution.get("occurred_at_utc") if isinstance(execution, dict) else None,
        "cash_conversion_keys": sorted(str(key) for key in conversions) if isinstance(conversions, dict) else [],
        "apply_blockers": [
            "raw_futu_time_apply_not_supported",
            "source_identity_and_execution_content_require_reconciliation",
            "cash_conversions_require_recomputation",
            "lot_allocation_lifecycle_attribution_and_inbox_require_validation",
        ],
    }


def _opend_trade_time_correction_plan(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    conn: sqlite3.Connection | None = None,
    corrected_at_ms: int | None = None,
) -> dict[str, Any]:
    effective = _repair_override_payload(overrides)
    if set(effective) != _OPEND_TRADE_TIME_OVERRIDE_KEYS:
        raise ValueError("OpenD trade time correction only accepts --trade-time-ms")
    requested_time_ms = effective["trade_time_ms"]
    if isinstance(requested_time_ms, bool) or not isinstance(requested_time_ms, int) or requested_time_ms <= 0:
        raise ValueError("trade_time_ms must be a positive integer")
    reason = _opend_trade_time_reason(repair_reason)
    rows = _storage_trade_event_rows(repo, conn=conn)
    target_id = str(target_event_id or "").strip()
    target_row = next((item for item in rows if str(item.get("event_id") or "") == target_id), None)
    if target_row is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    payloads = [(item, _storage_payload(item)) for item in rows]
    if target_id in {
        target
        for _row, payload in payloads
        if (target := valid_void_target_event_id(payload))
    }:
        raise ValueError(f"trade event already voided: {target_id}")

    before_json = str(target_row["event_json"])
    before_payload = _storage_payload(target_row)
    event, diagnostics = stored_trade_event_to_ledger_event(before_payload)
    errors = [item.code for item in diagnostics if item.severity == "error"]
    if event is None or errors:
        raise ValueError(
            "trade time correction requires a canonical trade event: "
            f"{target_id}; diagnostics={','.join(errors) or 'event_decode_failed'}"
        )
    if normalize_broker(event.contract_key.broker) != "富途":
        raise ValueError("trade time correction only supports Futu events")
    stored_time_ms = int(target_row.get("trade_time_ms") or 0)
    if stored_time_ms != int(event.event_time_ms):
        raise ValueError("trade event SQL and JSON times conflict")
    raw_payload = before_payload.get("raw_payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("trade event raw_payload must be an object")
    if "opend_order_evidence" not in raw_payload and event.source == "opend_push":
        if corrected_at_ms is not None:
            raise ValueError("raw Futu trade time is preview-only; apply is not supported")
        return _preview_futu_raw_trade_time(
            event,
            raw_payload=raw_payload,
            before_json=before_json,
            requested_time_ms=requested_time_ms,
        )
    if event.event_type != "open":
        raise ValueError("trade time correction only supports option open events")
    evidence_time_ms, evidence_order_ids, evidence_observed_at_ms = (
        _validated_opend_trade_time_evidence(event, raw_payload)
    )
    if requested_time_ms != evidence_time_ms:
        raise ValueError(
            f"trade_time_ms must equal the earliest stored OpenD order time: {evidence_time_ms}"
        )

    expected_before_sha256 = _sha256_text(before_json)
    common = {
        "operation": "opend_trade_time_correction",
        "target_event_id": target_id,
        "target_lot_id": lot_id_for_open_event(event),
        "before_trade_time_ms": stored_time_ms,
        "after_trade_time_ms": requested_time_ms,
        "evidence_order_ids": evidence_order_ids,
        "evidence_observed_at_ms": evidence_observed_at_ms,
        "expected_before_sha256": expected_before_sha256,
        "cash_conversion_backfill_required": (
            stored_time_ms != requested_time_ms
            or not isinstance(raw_payload.get("cash_conversions"), dict)
        ),
    }
    if stored_time_ms == requested_time_ms:
        return common | {
            "correction_status": "no_op",
            "before_json": before_json,
            "after_json": before_json,
            "after_sha256": expected_before_sha256,
        }
    if "trade_time_correction_provenance" in raw_payload:
        raise ValueError("trade time correction provenance already exists")
    if corrected_at_ms is None:
        return common | {"correction_status": "ready", "before_json": before_json}

    after_payload = deepcopy(before_payload)
    after_payload["event_time_ms"] = requested_time_ms
    after_raw = dict(raw_payload)
    removed_conversions = after_raw.pop("cash_conversions", None)
    invalidated_conversion_keys = (
        sorted(str(key) for key in removed_conversions)
        if isinstance(removed_conversions, dict)
        else []
    )
    after_raw["trade_time_correction_provenance"] = {
        "schema_version": _OPEND_TRADE_TIME_PROVENANCE_SCHEMA,
        "correction_id": _trade_time_correction_id(target_id, stored_time_ms, requested_time_ms),
        "source": "manual_trade_event_repair",
        "provider": "opend",
        "reason": reason,
        "before_trade_time_ms": stored_time_ms,
        "after_trade_time_ms": requested_time_ms,
        "evidence_schema_version": _OPEND_TRADE_TIME_EVIDENCE_SCHEMA,
        "evidence_order_ids": evidence_order_ids,
        "evidence_observed_at_ms": evidence_observed_at_ms,
        "expected_before_sha256": expected_before_sha256,
        "invalidated_cash_conversion_keys": invalidated_conversion_keys,
        "corrected_at_ms": int(corrected_at_ms),
    }
    after_payload["raw_payload"] = after_raw
    _assert_trade_time_only_payload_change(before_payload, after_payload)
    after_event, after_diagnostics = stored_trade_event_to_ledger_event(after_payload)
    if after_event is None or any(item.severity == "error" for item in after_diagnostics):
        raise ValueError("trade time correction produced an invalid canonical trade event")
    after_json = json.dumps(after_payload, ensure_ascii=False, sort_keys=True)
    return common | {
        "correction_status": "ready",
        "before_json": before_json,
        "after_json": after_json,
        "after_sha256": _sha256_text(after_json),
        "corrected_at_ms": int(corrected_at_ms),
        "invalidated_cash_conversion_count": (
            len(invalidated_conversion_keys)
        ),
    }


def preview_manual_opend_trade_time_correction(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
) -> dict[str, Any]:
    plan = _opend_trade_time_correction_plan(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
    )
    return {
        key: value
        for key, value in (
            plan
            | {
                "mode": "no_op" if plan["correction_status"] == "no_op" else "dry_run",
                "advisory": True,
            }
        ).items()
        if key not in {"before_json", "after_json", "correction_status"}
    }


def _assert_only_target_lot_opened_at_changed(
    before_lots: list[dict[str, Any]],
    after_lots: list[dict[str, Any]],
    *,
    target_lot_id: str,
    before_trade_time_ms: int,
    after_trade_time_ms: int,
) -> None:
    before_by_id = {str(item.get("record_id") or ""): deepcopy(item) for item in before_lots}
    after_by_id = {str(item.get("record_id") or ""): deepcopy(item) for item in after_lots}
    if set(before_by_id) != set(after_by_id) or target_lot_id not in before_by_id:
        raise ValueError("trade time correction changed position lot membership")
    changed_ids = {
        lot_id
        for lot_id in before_by_id
        if before_by_id[lot_id] != after_by_id[lot_id]
    }
    if changed_ids != {target_lot_id}:
        raise ValueError("trade time correction changed an unexpected position lot")
    before_target = before_by_id[target_lot_id]
    after_target = after_by_id[target_lot_id]
    before_fields = before_target.get("fields")
    after_fields = after_target.get("fields")
    if not isinstance(before_fields, dict) or not isinstance(after_fields, dict):
        raise ValueError("trade time correction target lot fields are invalid")
    if _lot_opened_at_ms(before_fields) != before_trade_time_ms:
        raise ValueError("trade time correction target lot has an unexpected opening time")
    if _lot_opened_at_ms(after_fields) != after_trade_time_ms:
        raise ValueError("trade time correction target lot opening time was not updated")
    for key in _LOT_OPENED_AT_KEYS:
        before_fields.pop(key, None)
        after_fields.pop(key, None)
    if before_target != after_target:
        raise ValueError("trade time correction changed non-time position lot data")


def persist_manual_opend_trade_time_correction(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("trade time correction requires SQLite transaction authority")
        applied_at_ms = now_ms()
        plan = _opend_trade_time_correction_plan(
            sqlite_repo,
            target_event_id=target_event_id,
            overrides=overrides,
            repair_reason=repair_reason,
            conn=conn,
            corrected_at_ms=applied_at_ms,
        )
        if plan["correction_status"] == "no_op":
            return {
                key: value
                for key, value in (plan | {"mode": "no_op", "advisory": False}).items()
                if key not in {"before_json", "after_json", "correction_status"}
            }

        before_lots = sqlite_repo.list_position_lots(conn=conn)
        before_fingerprint = position_lots_fingerprint(before_lots)
        source_before = int(
            sqlite_repo.read_position_projection_source_state(conn=conn).get("source_generation") or 0
        )
        fence = capture_trade_event_decision_projection_fence(sqlite_repo, conn=conn)
        updated = sqlite_repo.compare_and_swap_trade_event_time(
            event_id=plan["target_event_id"],
            expected_event_json=plan["before_json"],
            expected_trade_time_ms=plan["before_trade_time_ms"],
            replacement_event_json=plan["after_json"],
            replacement_trade_time_ms=plan["after_trade_time_ms"],
            updated_at_ms=applied_at_ms,
            conn=conn,
        )
        if not updated:
            raise ValueError(f"trade time correction CAS conflict: {plan['target_event_id']}")
        readback = next(
            (
                item
                for item in _storage_trade_event_rows(sqlite_repo, conn=conn)
                if str(item.get("event_id") or "") == plan["target_event_id"]
            ),
            None,
        )
        if (
            readback is None
            or str(readback.get("event_json") or "") != plan["after_json"]
            or int(readback.get("trade_time_ms") or 0) != plan["after_trade_time_ms"]
        ):
            raise ValueError(f"trade time correction readback failed: {plan['target_event_id']}")

        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            (),
            conn=conn,
            mode="forced_full",
        )
        publication = runtime.publication
        if publication.added or publication.removed or publication.changed != 1:
            raise ValueError("trade time correction changed unexpected position lots")
        after_lots = sqlite_repo.list_position_lots(conn=conn)
        _assert_only_target_lot_opened_at_changed(
            before_lots,
            after_lots,
            target_lot_id=plan["target_lot_id"],
            before_trade_time_ms=plan["before_trade_time_ms"],
            after_trade_time_ms=plan["after_trade_time_ms"],
        )
        after_fingerprint = position_lots_fingerprint(after_lots)
        decision = (
            finalize_current_decision_projection(
                sqlite_repo,
                fence=fence,
                updated_at_ms=applied_at_ms,
                conn=conn,
            )
            if fence is not None
            else None
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        source_after = int(
            sqlite_repo.read_position_projection_source_state(conn=conn).get("source_generation") or 0
        )
        return {
            key: value
            for key, value in (
                plan
                | {
                    "mode": "applied",
                    "advisory": False,
                    "position_lot_count": int(runtime.position_lot_count),
                    "position_lots_fingerprint_before": before_fingerprint,
                    "position_lots_fingerprint_after": after_fingerprint,
                    "projection_source_generation_before": source_before,
                    "projection_source_generation_after": source_after,
                    "decision_projection": decision,
                }
            ).items()
            if key not in {"before_json", "after_json", "correction_status"}
        }

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )


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
        if key in {
            "futu_account_id",
            "order_id",
            "record_id",
            "close_target_source_event_id",
        }:
            raw_payload[key] = value
            if key == "record_id":
                raw_payload["target_lot_id"] = value

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
    merged["multiplier"] = require_option_multiplier(merged.get("multiplier"))
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


def _manual_repair_source_evidence(
    sqlite_repo: Any,
    *,
    events: list[dict[str, Any]],
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from domain.domain.trade_execution import structured_deal_keys_from_ledger_event
    from src.application.trades.source_constraints import (
        TRADE_EVIDENCE_SET_REF_PREFIX,
        read_account_trade_source_constraints,
    )
    from src.application.trades.inbox_authority import resolve_execution_inbox_path

    ledger = Path(sqlite_repo.db_path)
    inbox_path = resolve_execution_inbox_path(
        sqlite_repo, ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3")
    )
    state = read_account_trade_source_constraints(
        inbox_path, account=str(target.get("account") or ""), trade_events=events
    )
    raw = target.get("raw_payload") or {}
    execution = raw.get("execution_input") if isinstance(raw, Mapping) else None
    refs: set[str] = set()
    for holder in (raw, execution):
        if not isinstance(holder, Mapping):
            continue
        refs.update(
            ref.removeprefix(TRADE_EVIDENCE_SET_REF_PREFIX)
            for ref in holder.get("evidence_refs") or []
            if isinstance(ref, str) and ref.startswith(TRADE_EVIDENCE_SET_REF_PREFIX)
        )
    prior_resolution = raw.get("resolved_trade_source_evidence") if isinstance(raw, Mapping) else None
    if isinstance(prior_resolution, Mapping):
        refs.update(str(value) for value in prior_resolution.get("inbox_ids") or [])
    keys = structured_deal_keys_from_ledger_event(target)
    inbox_ids = sorted(
        str(row["inbox_id"])
        for row in state.get("source_rows") or []
        if str(row["inbox_id"]) in refs or str(row["broker_deal_key"]) in keys
    )
    resolution = {
        "evidence_fingerprint": state["evidence_fingerprint"],
        "inbox_ids": inbox_ids,
    }
    return state, resolution


def _manual_repair_input_hash(
    *, target: dict[str, Any], stock_dependencies: list[dict[str, str]],
    downstream_dependencies: list[dict[str, Any]], source_state: dict[str, Any],
    overrides: dict[str, Any], repair_reason: str,
) -> str:
    return canonical_sha256({
        "target": target,
        "stock_dependencies": stock_dependencies,
        "downstream_dependencies": downstream_dependencies,
        "source_fingerprint": source_state["evidence_fingerprint"],
        "source_inbox_ids": source_state["inbox_ids"],
        "overrides": _repair_override_payload(overrides),
        "repair_reason": repair_reason,
    })


def build_manual_repair_preview(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    with with_sqlite_repo_writer_lock(sqlite_repo):
        events, target, stock_dependencies = _intervention_context(
            sqlite_repo, target_event_id=target_event_id,
        )
        source_state, source_resolution = _manual_repair_source_evidence(
            sqlite_repo, events=events, target=target
        )
    _assert_trade_event_can_be_manually_voided(
        events,
        target,
        stock_dependencies=stock_dependencies,
    )
    downstream_dependencies = _repair_downstream_dependencies(events, target)
    if downstream_dependencies:
        raise ValueError(
            "cannot repair an open event with downstream close/adjust dependencies: "
            f"{target_event_id}; void or repair downstream events first; "
            f"dependencies={json.dumps(downstream_dependencies, ensure_ascii=False, sort_keys=True)}"
        )

    expected_input_hash = _manual_repair_input_hash(
        target=target, stock_dependencies=stock_dependencies,
        downstream_dependencies=downstream_dependencies, source_state=source_state,
        overrides=overrides, repair_reason=repair_reason,
    )
    require_option_multiplier(target.get("multiplier"))
    core = _normalized_repair_core_event(target, overrides)
    void_event_id, repair_event_id = _manual_repair_event_ids(
        target_event_id=target_event_id,
        core_event=core,
        overrides=overrides,
        repair_reason=repair_reason,
    )
    core_raw_payload = dict(core.get("raw_payload") or {})
    # The replacement is a manual correction, not a second broker execution.
    origin_execution_id = core_raw_payload.pop("execution_id", None)
    core_raw_payload.pop("execution_input", None)
    core_raw_payload.pop("broker_deal_completion", None)
    if origin_execution_id:
        core_raw_payload["repair_origin_execution_id"] = origin_execution_id
    # Conversions belong to the original event identity and economics.
    core_raw_payload.pop("cash_conversions", None)
    core_raw_payload.update(
        {
            "source": "om trade-events",
            "mode": "manual_repair",
            "repair_target_event_id": str(target_event_id),
            "repair_reason": str(repair_reason or ""),
            "repair_overrides": _repair_override_payload(overrides),
            "repair_expected_input_hash": expected_input_hash,
            "resolved_trade_source_evidence": source_resolution,
        }
    )
    repair_event = _repair_trade_event(event_id=repair_event_id, core=core, raw_payload=core_raw_payload)
    repair_event = attach_trade_event_cash_conversions(
        repair_event,
        fx_payload=load_cash_fx_payload(sqlite_repo, persist=False),
        observed_at_ms=int(as_of_ms or now_ms()),
    )
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
        expected_input_hash=expected_input_hash,
    )


def readback_manual_repair_event(
    repo: Any, *, target_event_id: str, overrides: dict[str, Any],
    repair_reason: str, expected_input_hash: str | None,
) -> dict[str, Any] | None:
    """A lost apply response is safe to repeat only for the identical request."""
    if not expected_input_hash:
        return None
    sqlite_repo = require_option_positions_event_write_repo(repo)
    with with_sqlite_repo_writer_lock(sqlite_repo):
        events = [dict(row) for row in sqlite_repo.list_trade_events()]
        repair = next((event for event in events if (
            (raw := _event_payload(event)).get("mode") == "manual_repair"
            and raw.get("repair_target_event_id") == target_event_id
            and raw.get("repair_reason") == repair_reason
            and raw.get("repair_overrides") == _repair_override_payload(overrides)
            and raw.get("repair_expected_input_hash") == expected_input_hash
        )), None)
        if repair is None:
            return None
        void = next((event for event in events if (
            (raw := _event_payload(event)).get("mode") == "manual_repair_void"
            and raw.get("void_target_event_id") == target_event_id
            and raw.get("repair_event_id") == repair.get("event_id")
        )), None)
        if void is None:
            raise ValueError("manual repair readback found an incomplete repair pair")
        source_state, _ = _manual_repair_source_evidence(
            sqlite_repo, events=events, target=repair
        )
        saved_resolution = _event_payload(repair).get("resolved_trade_source_evidence") or {}
        return {
            "event_id": str(repair["event_id"]),
            "target_event_id": target_event_id,
            "void_event_id": str(void["event_id"]),
            "repair_event_id": str(repair["event_id"]),
            "void_created": False,
            "repair_created": False,
            "position_lot_count": len(sqlite_repo.list_position_lots()),
            "source_evidence_reopened": (
                saved_resolution.get("evidence_fingerprint")
                != source_state.get("evidence_fingerprint")
            ),
        }


def persist_manual_repair_event(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    expected_input_hash: str | None,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    preview = build_manual_repair_preview(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )
    if not str(expected_input_hash or "").strip():
        raise ValueError("manual repair requires expected_input_hash from preview")
    if preview.expected_input_hash != expected_input_hash:
        raise ValueError("manual repair preview is stale; preview again")
    void_event = _preview_event_to_trade_event(preview.void_event)
    repair_event = _preview_event_to_trade_event(preview.repair_event)

    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("trade event repair requires SQLite transaction authority")
        events, target, stock_dependencies = _intervention_context(
            sqlite_repo,
            target_event_id=target_event_id,
            conn=conn,
        )
        stored_repair = next(
            (event for event in events if event.get("event_id") == repair_event.event_id), None
        )
        stored_void = next(
            (event for event in events if event.get("event_id") == void_event.event_id), None
        )
        if stored_repair is not None or stored_void is not None:
            saved = _event_payload(stored_repair or {})
            if (
                stored_repair is None or stored_void is None
                or saved.get("repair_expected_input_hash") != expected_input_hash
                or saved.get("repair_target_event_id") != target_event_id
                or saved.get("repair_overrides") != _repair_override_payload(overrides)
                or saved.get("repair_reason") != repair_reason
                or _event_payload(stored_void).get("repair_event_id") != repair_event.event_id
            ):
                raise ValueError("manual repair readback conflict")
            source_state, _ = _manual_repair_source_evidence(
                sqlite_repo, events=events, target=stored_repair
            )
            saved_resolution = saved.get("resolved_trade_source_evidence") or {}
            return {
                "target_event_id": target_event_id,
                "void_event_id": void_event.event_id,
                "repair_event_id": repair_event.event_id,
                "void_created": False,
                "repair_created": False,
                "position_lot_count": len(sqlite_repo.list_position_lots(conn=conn)),
                "source_evidence_reopened": (
                    saved_resolution.get("evidence_fingerprint")
                    != source_state.get("evidence_fingerprint")
                ),
            }
        _assert_trade_event_can_be_manually_voided(
            events,
            target,
            stock_dependencies=stock_dependencies,
        )
        downstream_dependencies = _repair_downstream_dependencies(events, target)
        if downstream_dependencies:
            raise ValueError(
                "cannot repair an open event with downstream close/adjust dependencies: "
                f"{target_event_id}; void or repair downstream events first; "
                f"dependencies={json.dumps(downstream_dependencies, ensure_ascii=False, sort_keys=True)}"
            )
        source_state, _ = _manual_repair_source_evidence(
            sqlite_repo, events=events, target=target
        )
        actual_hash = _manual_repair_input_hash(
            target=target, stock_dependencies=stock_dependencies,
            downstream_dependencies=downstream_dependencies,
            source_state=source_state, overrides=overrides,
            repair_reason=repair_reason,
        )
        if actual_hash != expected_input_hash:
            raise ValueError("manual repair preview is stale; preview again")
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            (void_event, repair_event),
            conn=conn,
            mode="forced_full",
        )
        void_created, repair_created = runtime.created_flags
        result = {
            "target_event_id": str(target_event_id),
            "void_event_id": void_event.event_id,
            "repair_event_id": repair_event.event_id,
            "void_created": bool(void_created),
            "repair_created": bool(repair_created),
            "position_lot_count": int(runtime.position_lot_count),
        }
        result.update(projection_diagnostics_summary(runtime.diagnostics))
        return result

    result = with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )
    result["preview"] = preview.to_payload()
    return LedgerWriteResult(
        event_id=str(result.get("repair_event_id") or ""),
        position_lot_count=int(result.get("position_lot_count") or 0),
        details={key: value for key, value in result.items() if key not in {"event_id", "position_lot_count"}},
    )
