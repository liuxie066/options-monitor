"""Row-reading micro-helpers and the Wheel lifecycle/branch read models."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import TradeEvent, project_trade_events
from domain.domain.ledger.events import validate_trade_event
from domain.domain.trade_execution import execution_identity_from_input
from domain.domain.ledger.cash_facts import (
    assignment_principal_anchor,
    broker_settlement_multiplier_evidence,
)
from domain.domain.ledger.position_fields import (
    apply_strategy_metadata_patch,
    strategy_metadata_fields_from_payload,
)
from domain.domain.symbol_identity import symbol_market
from domain.domain.strategy_membership import strategy_metadata_has_owner
from domain.domain.trade_contract_identity import contract_share_quantity

from ._common import (
    WHEEL_EVENT_SCHEMA_V1,
    WHEEL_EVENT_SCHEMA_V2,
    WHEEL_PROJECTION_SCHEMA,
    _finite_float,
    _wheel_market,
)
from .events import _positive_int, _required_text, normalize_wheel_event


def _lot_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row.get("fields")
    return dict(fields) if isinstance(fields, Mapping) else dict(row)


def _event_type(row: Mapping[str, Any]) -> str:
    payload = row.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    return str(
        row.get("event_type") or payload.get("close_type") or ""
    ).strip().lower()


def lot_contract_key(fields: Mapping[str, Any]) -> Mapping[str, Any]:
    """A lot payload's nested ``contract_key``, or ``{}`` when it is not one.

    The converged payload (``PositionLot.to_dict()``) carries the option
    contract under ``contract_key``; the six flat siblings it replaced
    (``account``/``broker``/``symbol``/``option_type``/``strike``/
    ``expiration_ymd``) are retired.
    """
    key = fields.get("contract_key")
    return key if isinstance(key, Mapping) else {}


#: The strategy-metadata family (design §7.5). It left ``fields_json`` in the
#: convergence batch -- the fact's home is the event layer -- so a reader that
#: needs it for a lot replays that lot's events instead of the stored payload.
STRATEGY_METADATA_KEYS = (
    "strategy",
    "leg_role",
    "strategy_group_id",
    "source_stock_lot_id",
    "source_wheel_branch_id",
    "strategy_snapshot",
    "yield_enhancement_mode",
)


def _pre_batch_snapshot_family(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The family as a pre-batch import event stored it, or ``{}``.

    An import event used to seed the whole legacy lot row under ``fields``
    (``bootstrap._bootstrap_trade_event``), and the retired publisher assembly
    copied the family straight out of that snapshot into ``fields_json``. New
    imports no longer seed the snapshot, but the events already written keep it,
    so reading it here is what stops an imported lot's family from becoming
    unrecoverable the moment ``fields_json`` stops carrying it. Only the
    declared patch keys are read back out -- never the snapshot itself.

    ``include_legacy=False`` on purpose. A nested snapshot is the *retired* row
    copied verbatim, so its durable keys are the family proper, while its
    ``yield_enhancement_mode`` is the spelling a later ``adjust`` patch
    explicitly supersedes -- returning it would let an old import's retired mode
    outrank that adjust. A family expressible *only* through the retired mode
    therefore stays unread from a pre-batch snapshot; the canonical top-level
    read still honours that spelling where a current writer put it.
    """

    snapshot = payload.get("fields")
    if not isinstance(snapshot, Mapping):
        return {}
    return strategy_metadata_fields_from_payload(dict(snapshot))


def lot_strategy_metadata_from_trade_events(
    trade_events: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-lot strategy metadata rebuilt from the event layer (design §7.5).

    Replays the open event's payload and every adjust patch in event order, the
    same two sources the retired publisher assembly read before the family moved
    out of the lot shape. Keyed by lot id; a lot with no strategy metadata is
    absent from the mapping.
    """
    by_lot_id: dict[str, dict[str, Any]] = {}

    def _lot_id_for(event: Mapping[str, Any]) -> str:
        payload = event.get("raw_payload")
        payload = payload if isinstance(payload, Mapping) else {}
        explicit = str(event.get("lot_id") or payload.get("record_id") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        return explicit or (f"lot_{event_id}" if event_id else "")

    for raw in sorted(_active_trade_events(trade_events), key=lambda row: (int(row.get("event_time_ms") or 0), str(row.get("event_id") or ""))):
        if not isinstance(raw, Mapping):
            continue
        event_type = str(raw.get("event_type") or "").strip().lower()
        payload = raw.get("raw_payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if event_type == "open":
            lot_id = _lot_id_for(raw)
            if not lot_id:
                continue
            metadata = strategy_metadata_fields_from_payload(
                dict(payload),
                include_legacy=True,
            )
            if not metadata:
                metadata = _pre_batch_snapshot_family(payload)
            if metadata:
                by_lot_id.setdefault(lot_id, {}).update(metadata)
            continue
        if event_type != "adjust":
            continue
        lot_id = str(raw.get("target_lot_id") or payload.get("target_lot_id") or "").strip()
        patch = payload.get("patch")
        if not lot_id or not isinstance(patch, Mapping):
            continue
        if not any(key in patch for key in STRATEGY_METADATA_KEYS):
            continue
        merged = apply_strategy_metadata_patch(
            by_lot_id.get(lot_id, {}),
            dict(patch),
            include_legacy=True,
        )
        by_lot_id[lot_id] = {
            key: merged[key] for key in STRATEGY_METADATA_KEYS if key in merged
        }
    return by_lot_id


def merge_lot_strategy_metadata(
    fields: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Fold event-derived strategy metadata into a lot's fields.

    Only keys the payload does not carry at all are filled, so an explicit
    clear (``None``) a caller wrote stays a clear instead of being resurrected
    from the events it was derived out of.
    """
    out = dict(fields)
    for key, value in metadata.items():
        if key not in out:
            out[key] = value
    return out


def attach_lot_strategy_metadata(
    row: Mapping[str, Any],
    strategy_by_lot_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """A lot row's fields with its event-derived strategy metadata folded in."""
    fields = _lot_fields(row)
    lot_id = str(row.get("record_id") or row.get("lot_id") or "").strip()
    metadata = strategy_by_lot_id.get(lot_id)
    if not lot_id or not metadata:
        return fields
    return merge_lot_strategy_metadata(fields, metadata)


def lot_strategy_metadata_for_lot(
    lot_id: Any,
    trade_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """One lot's event-derived strategy metadata (design §7.5)."""
    key = str(lot_id or "").strip()
    if not key:
        return {}
    return dict(lot_strategy_metadata_from_trade_events(trade_events).get(key, {}))


def _trade_account(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("account") or key.get("account") or "").strip().lower()


def _trade_symbol(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(
        row.get("symbol") or key.get("underlying_symbol") or key.get("symbol") or ""
    ).strip().upper()


def _trade_option_type(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("option_type") or key.get("option_type") or "").strip().lower()


def _trade_position_side(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    explicit = str(row.get("position_side") or key.get("position_side") or "").strip().lower()
    if explicit:
        return explicit
    side = str(row.get("side") or "").strip().lower()
    effect = str(row.get("position_effect") or "").strip().lower()
    if effect == "open":
        return "short" if side == "sell" else "long" if side == "buy" else ""
    if effect == "close":
        return "short" if side == "buy" else "long" if side == "sell" else ""
    return ""


def _active_trade_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    voided = set()
    for row in rows:
        if _event_type(row) != "void":
            continue
        try:
            event = TradeEvent.from_dict(dict(row))
            if not any(item.severity == "error" for item in validate_trade_event(event)):
                voided.add(str(event.target_event_id or ""))
        except (TypeError, ValueError, KeyError):
            continue
    return [
        dict(row)
        for row in rows
        if _event_type(row) != "void"
        and str(row.get("event_id") or "").strip() not in voided
    ]


def _contracts_open(fields: Mapping[str, Any]) -> int:
    try:
        if str(fields.get("status") or "").strip().lower() == "close":
            return 0
        return max(
            0,
            int(fields.get("contracts_open", fields.get("contracts_opened", 0)) or 0),
        )
    except (TypeError, ValueError):
        return 0


def _stable_stock_fact(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    fields = (
        "stock_lot_id",
        "source_assignment_event_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "assigned_at_ms",
        "shares_opened",
        "shares_remaining",
        "shares_sold",
        "assignment_price",
        "assignment_fees",
        "stock_cost_basis_total",
        "stock_principal_basis_total",
        "stock_sale_cash_in_net",
        "stock_sale_cash_in_gross",
        "stock_sale_fees",
        "assigned_stock_realized_pnl",
        "sale_event_ids",
    )
    return {field: row.get(field) for field in fields}


def _intent_contracts(payload: Mapping[str, Any]) -> int | None:
    for field in ("contracts", "granted_contracts", "quantity"):
        value = payload.get(field)
        if value in (None, ""):
            continue
        try:
            return _positive_int(value, "intent contracts")
        except (TypeError, ValueError):
            return None
    return None


def _intent_state(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of_ms: int,
    known_trade_event_ids: set[str],
    direction: str = "call",
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    prefix = f"wheel_{direction_value}_intent_"
    by_intent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    reasons: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith(prefix):
            continue
        intent_id = str(event.get("intent_id") or "").strip()
        if not intent_id:
            reasons.append("intent_id_missing")
            continue
        by_intent[intent_id].append(event)
    active: list[str] = []
    summaries: list[dict[str, Any]] = []
    for intent_id in sorted(by_intent):
        intent_events = by_intent[intent_id]
        created = [item for item in intent_events if item["event_type"] == f"{prefix}created"]
        cancelled = [item for item in intent_events if item["event_type"] == f"{prefix}cancelled"]
        consumed = [item for item in intent_events if item["event_type"] == f"{prefix}consumed"]
        if len(created) != 1:
            reasons.append("intent_creation_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        creation = created[0]
        created_contracts = _intent_contracts(creation["payload"])
        try:
            expires_at_ms = int(creation["payload"].get("expires_at_ms") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if created_contracts is None or expires_at_ms <= int(creation["occurred_at_ms"]):
            reasons.append("intent_contract_invalid")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        if len(cancelled) > 1:
            reasons.append("intent_cancellation_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        cancel_at = int(cancelled[0]["occurred_at_ms"]) if cancelled else None
        if cancel_at is not None and cancel_at < int(creation["occurred_at_ms"]):
            reasons.append("intent_causality_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        consumed_contracts = 0
        intent_conflict = False
        for item in consumed:
            quantity = _intent_contracts(item["payload"])
            source_id = str(item.get("source_trade_event_id") or "").strip()
            occurred_at_ms = int(item["occurred_at_ms"])
            if (
                quantity is None
                or not source_id
                or source_id not in known_trade_event_ids
                or occurred_at_ms < int(creation["occurred_at_ms"])
                or occurred_at_ms > expires_at_ms
                or (cancel_at is not None and occurred_at_ms >= cancel_at)
            ):
                intent_conflict = True
                break
            consumed_contracts += quantity
        if intent_conflict or consumed_contracts > created_contracts:
            reasons.append("intent_consumption_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        remaining = created_contracts - consumed_contracts
        status = (
            "cancelled"
            if cancel_at is not None
            else "consumed"
            if remaining == 0
            else "expired"
            if as_of_ms > expires_at_ms
            else "active"
        )
        if status == "active":
            active.append(intent_id)
        summaries.append(
            {
                "intent_id": intent_id,
                "status": status,
                "created_event_id": creation["event_id"],
                "created_at_ms": int(creation["occurred_at_ms"]),
                "expires_at_ms": expires_at_ms,
                "contracts": created_contracts,
                "consumed_contracts": consumed_contracts,
                "remaining_contracts": remaining,
                "payload": dict(creation["payload"]),
            }
        )
    return active, reasons, summaries


def _conflict_resolution_has_proof(resolution: Mapping[str, Any], conflict: Mapping[str, Any],
                                   trade_events: Sequence[Mapping[str, Any]]) -> bool:
    events = _active_trade_events(trade_events)
    proof = next((row for row in events if row.get("event_id") == resolution["payload"]["resolution_evidence_event_id"]), {})
    raw = proof.get("raw_payload") or {}
    if (proof.get("event_type") != "adjust" or proof.get("source") not in {"trade_attribution", "wheel_linkage", "post_trade_combo_reconciliation"}
            or raw.get("attribution_origin") != "manual" or not raw.get("actor") or not raw.get("attribution_request_id")
            or int(proof.get("event_time_ms") or 0) < conflict["occurred_at_ms"]
            or _trade_account(proof) != conflict["account"]):
        return False
    opening = next((row for row in events if row.get("event_type") == "open" and row.get("lot_id") == proof.get("target_lot_id")), {})
    identity = execution_identity_from_input((opening.get("raw_payload") or {}).get("execution_input") or {})
    if identity not in conflict["payload"]["execution_keys"]:
        return False
    return (raw.get("attribution_action") == "ordinary" or bool(raw.get("attribution_candidate_id"))
            and set(conflict["payload"].get("candidate_ids") or []) <= set(raw.get("attribution_candidate_ids") or []))


def effective_wheel_events(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    as_of_ms: int | None = None,
    known_trade_event_ids: set[str] | None = None,
    trade_events: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], set[str]]]:
    events_by_id: dict[str, dict[str, Any]] = {}
    invalid_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for raw in wheel_events:
        group = (
            str(raw.get("account") or "").strip().lower(),
            str(
                raw.get("wheel_branch_id")
                or raw.get("stock_lot_id")
                or ""
            ).strip(),
        )
        try:
            event = normalize_wheel_event(raw)
        except (TypeError, ValueError):
            if all(group):
                invalid_by_group[group].add("invalid_wheel_event")
            continue
        if as_of_ms is not None and int(event["occurred_at_ms"]) > int(as_of_ms):
            continue
        previous = events_by_id.get(event["event_id"])
        if previous is not None and previous["payload_hash"] != event["payload_hash"]:
            invalid_by_group[(event["account"], event["wheel_branch_id"])].add(
                "wheel_event_id_conflict"
            )
            invalid_by_group[(previous["account"], previous["wheel_branch_id"])].add(
                "wheel_event_id_conflict"
            )
            continue
        if previous is None or event["recorded_at_ms"] < previous["recorded_at_ms"]:
            events_by_id[event["event_id"]] = event

    voided_ids: set[str] = set()
    valid_void_ids: set[str] = set()
    for event in events_by_id.values():
        if event["event_type"] != "wheel_event_voided":
            continue
        group = (event["account"], event["wheel_branch_id"])
        target_id = str(event["payload"].get("target_wheel_event_id") or "").strip()
        target = events_by_id.get(target_id)
        if (
            target is None
            or target["event_type"] in {"wheel_event_voided", "wheel_attribution_conflict", "wheel_attribution_conflict_resolved"}
            or (target["account"], target["wheel_branch_id"]) != group
        ):
            invalid_by_group[group].add("wheel_void_target_invalid")
            continue
        voided_ids.add(target_id)
        valid_void_ids.add(event["event_id"])

    conflicts = {event["event_id"]: event for event in events_by_id.values()
                 if event["event_type"] == "wheel_attribution_conflict"}
    resolved = set()
    for event in events_by_id.values():
        if event["event_type"] != "wheel_attribution_conflict_resolved":
            continue
        target = conflicts.get(event["payload"]["conflict_event_id"])
        group = (event["account"], event["wheel_branch_id"])
        if (target is None or group != (target["account"], target["wheel_branch_id"])
                or event["occurred_at_ms"] < target["occurred_at_ms"]
                or event["payload"]["resolution_evidence_event_id"] not in (known_trade_event_ids or set())
                or not _conflict_resolution_has_proof(event, target, trade_events)):
            invalid_by_group[group].add("invalid_attribution_conflict_resolution")
        else:
            resolved.add(target["event_id"])
    for event_id, event in conflicts.items():
        if event_id not in resolved:
            invalid_by_group[(event["account"], event["wheel_branch_id"])].add("strategy_attribution_conflict")

    return (
        [
            event
            for event in events_by_id.values()
            if event["event_id"] not in voided_ids
            and (
                event["event_type"] != "wheel_event_voided"
                or event["event_id"] in valid_void_ids
            )
        ],
        invalid_by_group,
    )


def project_wheel_intents(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    account: str,
    wheel_branch_id: str,
    direction: str,
    as_of_ms: int,
    known_trade_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    account_value = _required_text(account, "account").lower()
    branch_id = _required_text(wheel_branch_id, "wheel_branch_id")
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    instant = _positive_int(as_of_ms, "as_of_ms")
    events, _invalid = effective_wheel_events(
        [
            event
            for event in wheel_events
            if str(event.get("account") or "").strip().lower() == account_value
            and str(
                event.get("wheel_branch_id")
                or event.get("stock_lot_id")
                or ""
            ).strip()
            == branch_id
        ],
        as_of_ms=instant,
    )
    _active, _reasons, summaries = _intent_state(
        events,
        as_of_ms=instant,
        known_trade_event_ids=set(known_trade_event_ids or ()),
        direction=direction_value,
    )
    return summaries


def project_wheel_call_intents(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    account: str,
    lot_id: str,
    as_of_ms: int,
    known_trade_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    account_value = _required_text(account, "account").lower()
    stock_lot_value = _required_text(lot_id, "stock_lot_id")
    instant = _positive_int(as_of_ms, "as_of_ms")
    events, _invalid = effective_wheel_events(
        [
            event
            for event in wheel_events
            if str(event.get("account") or "").strip().lower() == account_value
            and str(
                event.get("wheel_branch_id")
                or event.get("stock_lot_id")
                or ""
            ).strip()
            == stock_lot_value
        ],
        as_of_ms=instant,
    )
    _active, _reasons, summaries = _intent_state(
        events,
        as_of_ms=instant,
        known_trade_event_ids=set(known_trade_event_ids or ()),
    )
    return summaries


def project_wheel_call_linkage_candidates(
    wheel_batches: Sequence[Mapping[str, Any]],
    unlinked_short_call_lots: Sequence[Mapping[str, Any]],
    rejected_linkages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    effective_linkages, _invalid = effective_wheel_events(rejected_linkages)
    rejected = {
        (
            str((event.get("payload") or {}).get("call_open_event_id") or "").strip(),
            str(event.get("stock_lot_id") or "").strip(),
        )
        for event in effective_linkages
        if str(event.get("event_type") or "").strip()
        == "wheel_call_linkage_rejected"
    }
    candidates: list[dict[str, Any]] = []
    for row in unlinked_short_call_lots:
        fields = _lot_fields(row)
        contract_key = lot_contract_key(fields)
        if (
            str(contract_key.get("option_type") or "").strip().lower() != "call"
            or str(fields.get("position_side") or "").strip().lower()
            != "short"
            or _contracts_open(fields) <= 0
            or strategy_metadata_has_owner(fields)
        ):
            continue
        call_lot_id = _required_text(row.get("record_id"), "call_record_id")
        call_open_event_id = _required_text(
            fields.get("open_event_id"),
            "call_open_event_id",
        )
        account = str(contract_key.get("account") or "").strip().lower()
        symbol = str(contract_key.get("underlying_symbol") or "").strip().upper()
        for batch in wheel_batches:
            lot_id = str(batch.get("stock_lot_id") or "").strip()
            if (
                batch.get("lifecycle_status") != "active"
                or batch.get("integrity_status") != "trusted"
                or str(batch.get("account") or "").strip().lower() != account
                or str(batch.get("symbol") or "").strip().upper() != symbol
                or (call_open_event_id, lot_id) in rejected
            ):
                continue
            try:
                required_shares = _contracts_open(fields) * int(
                    float(fields.get("multiplier") or 0)
                )
                shares_remaining = (int(batch.get("shares_remaining"))
                                    - int(batch.get("active_option_committed_shares") or 0))
            except (TypeError, ValueError):
                continue
            if required_shares <= 0 or shares_remaining < required_shares:
                continue
            digest = canonical_sha256(
                {
                    "call_open_event_id": call_open_event_id,
                    "stock_lot_id": lot_id,
                }
            )[:24]
            stable_call = {
                key: fields.get(key)
                for key in (
                    "lot_id",
                    "open_event_id",
                    "position_side",
                    "contracts_open",
                    "multiplier",
                )
            }
            stable_call["contract_key"] = {
                key: contract_key.get(key)
                for key in (
                    "account",
                    "underlying_symbol",
                    "option_type",
                    "strike",
                    "expiration_ymd",
                )
            }
            candidates.append(
                {
                    "linkage_candidate_id": f"wheel-call-linkage:{digest}",
                    "input_snapshot_hash": canonical_sha256(
                        {
                            "call_record_id": call_lot_id,
                            "call": stable_call,
                            "stock_lot_id": lot_id,
                            "batch_generation_hash": batch.get(
                                "batch_generation_hash"
                            ),
                        }
                    ),
                    "account": account,
                    "symbol": symbol,
                    "call_record_id": call_lot_id,
                    "call_open_event_id": call_open_event_id,
                    "stock_lot_id": lot_id,
                    "contracts": _contracts_open(fields),
                    "multiplier": int(float(fields.get("multiplier") or 0)),
                    "required_shares": required_shares,
                    "batch_generation_hash": batch.get("batch_generation_hash"),
                }
            )
    return sorted(
        candidates,
        key=lambda item: (
            str(item["account"]),
            str(item["symbol"]),
            str(item["call_record_id"]),
            str(item["stock_lot_id"]),
        ),
    )


def project_wheel_linkage_candidates(
    wheel_branches: Sequence[Mapping[str, Any]],
    unlinked_short_option_lots: Sequence[Mapping[str, Any]],
    rejected_linkages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic Call and Put linkage choices without guessing."""

    call_branches = [
        {
            **row,
            "batch_generation_hash": row.get("batch_generation_hash"),
        }
        for row in wheel_branches
        if str(row.get("direction") or "call").strip().lower() == "call"
    ]
    call_branch_by_stock_lot = {
        str(row.get("stock_lot_id") or ""): row for row in call_branches
    }
    call_candidates = [
        {
            **item,
            "direction": "call",
            "wheel_branch_id": str(
                call_branch_by_stock_lot[item["stock_lot_id"]].get(
                    "wheel_branch_id"
                )
                or item["stock_lot_id"]
            ),
            "option_record_id": item["call_record_id"],
            "option_open_event_id": item["call_open_event_id"],
            "batch_generation_hash": item.get("batch_generation_hash"),
        }
        for item in project_wheel_call_linkage_candidates(
            call_branches,
            unlinked_short_option_lots,
            rejected_linkages,
        )
    ]
    effective_linkages, _invalid = effective_wheel_events(rejected_linkages)
    rejected = {
        (
            str(
                (event.get("payload") or {}).get("option_open_event_id")
                or (event.get("payload") or {}).get("put_open_event_id")
                or ""
            ).strip(),
            str(event.get("wheel_branch_id") or "").strip(),
        )
        for event in effective_linkages
        if str(event.get("event_type") or "").strip()
        == "wheel_put_linkage_rejected"
    }
    put_candidates: list[dict[str, Any]] = []
    for row in unlinked_short_option_lots:
        fields = _lot_fields(row)
        contract_key = lot_contract_key(fields)
        if (
            str(contract_key.get("option_type") or "").strip().lower() != "put"
            or str(fields.get("position_side") or "").strip().lower()
            != "short"
            or _contracts_open(fields) <= 0
            or strategy_metadata_has_owner(fields)
        ):
            continue
        lot_id = _required_text(row.get("record_id"), "option_record_id")
        open_event_id = _required_text(
            fields.get("open_event_id"),
            "option_open_event_id",
        )
        account = str(contract_key.get("account") or "").strip().lower()
        symbol = str(contract_key.get("underlying_symbol") or "").strip().upper()
        for branch in wheel_branches:
            branch_id = str(branch.get("wheel_branch_id") or "").strip()
            if (
                str(branch.get("direction") or "").strip().lower() != "put"
                or branch.get("lifecycle_status") != "active"
                or branch.get("integrity_status") != "trusted"
                or str(branch.get("account") or "").strip().lower() != account
                or str(branch.get("symbol") or "").strip().upper() != symbol
                or (open_event_id, branch_id) in rejected
            ):
                continue
            try:
                contracts = _contracts_open(fields)
                multiplier = int(float(fields.get("multiplier") or 0))
                remaining = (int(branch.get("remaining_contracts") or 0)
                             - int(branch.get("active_option_committed_contracts") or 0))
                branch_multiplier = int(branch.get("multiplier") or 0)
            except (TypeError, ValueError):
                continue
            if (
                contracts <= 0
                or contracts > remaining
                or multiplier <= 0
                or multiplier != branch_multiplier
            ):
                continue
            digest = canonical_sha256(
                {
                    "direction": "put",
                    "option_open_event_id": open_event_id,
                    "wheel_branch_id": branch_id,
                }
            )[:24]
            generation_hash = str(branch.get("batch_generation_hash") or "")
            put_candidates.append(
                {
                    "linkage_candidate_id": f"wheel-put-linkage:{digest}",
                    "input_snapshot_hash": canonical_sha256(
                        {
                            "option_record_id": lot_id,
                            "option": {
                                **{
                                    key: fields.get(key)
                                    for key in (
                                        "lot_id",
                                        "open_event_id",
                                        "position_side",
                                        "contracts_open",
                                        "multiplier",
                                    )
                                },
                                "contract_key": {
                                    key: contract_key.get(key)
                                    for key in (
                                        "account",
                                        "underlying_symbol",
                                        "option_type",
                                        "strike",
                                        "expiration_ymd",
                                    )
                                },
                            },
                            "wheel_branch_id": branch_id,
                            "batch_generation_hash": generation_hash,
                        }
                    ),
                    "account": account,
                    "symbol": symbol,
                    "direction": "put",
                    "option_record_id": lot_id,
                    "option_open_event_id": open_event_id,
                    "wheel_branch_id": branch_id,
                    "contracts": contracts,
                    "multiplier": multiplier,
                    "cash_reservation_amount": float(
                        contract_key.get("strike") or 0
                    )
                    * contract_share_quantity(contracts, multiplier),
                    "cash_reservation_currency": str(
                        fields.get("currency") or branch.get("currency") or ""
                    ).strip().upper(),
                    "batch_generation_hash": generation_hash,
                }
            )
    return sorted(
        [*call_candidates, *put_candidates],
        key=lambda item: (
            str(item.get("account") or ""),
            str(item.get("symbol") or ""),
            str(item.get("direction") or ""),
            str(item.get("option_record_id") or ""),
            str(item.get("wheel_branch_id") or ""),
        ),
    )


def project_wheel_lifecycles(
    wheel_events: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]],
    position_lots: Sequence[Mapping[str, Any]],
    assigned_stock_projection: Mapping[str, Any],
    as_of_ms: int,
) -> list[dict[str, Any]]:
    """Rebuild Wheel batches from immutable facts; never guesses a missing link."""

    instant = _positive_int(as_of_ms, "as_of_ms")
    effective_events, invalid_by_group = effective_wheel_events(
        wheel_events,
        as_of_ms=instant,
        trade_events=trade_events,
        known_trade_event_ids={str(event.get("event_id") or "") for event in _active_trade_events(trade_events)
                               if int(event.get("event_time_ms") or 0) <= instant},
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    legacy_groups = {(event["account"], event["stock_lot_id"]) for event in effective_events
                     if event["event_schema_version"] == WHEEL_EVENT_SCHEMA_V1}
    for event in effective_events:
        if event["event_schema_version"] != WHEEL_EVENT_SCHEMA_V1 and not (
            event["event_type"] in {"wheel_attribution_conflict", "wheel_attribution_conflict_resolved"}
            and (event["account"], event["wheel_branch_id"]) in legacy_groups
        ):
            continue
        grouped[(event["account"], event["wheel_branch_id"])].append(event)

    active_trade_events = _active_trade_events(trade_events)
    trade_by_id = {
        str(row.get("event_id") or "").strip(): row
        for row in active_trade_events
        if str(row.get("event_id") or "").strip()
    }
    all_stock_rows = assigned_stock_projection.get("_all_assigned_stock_lots")
    if not isinstance(all_stock_rows, Sequence):
        all_stock_rows = assigned_stock_projection.get("assigned_stock_lots") or []
    stock_rows_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_stock_rows:
        if isinstance(row, Mapping) and str(row.get("stock_lot_id") or "").strip():
            stock_rows_by_id[str(row["stock_lot_id"]).strip()].append(row)
    review_rows = [
        row
        for row in assigned_stock_projection.get("assigned_stock_review_rows") or []
        if isinstance(row, Mapping)
    ]

    strategy_by_lot_id = lot_strategy_metadata_from_trade_events(active_trade_events)
    lots = [
        (
            str(row.get("record_id") or "").strip(),
            attach_lot_strategy_metadata(row, strategy_by_lot_id),
        )
        for row in position_lots
    ]
    results: list[dict[str, Any]] = []
    for group in sorted(grouped):
        account, lot_id = group
        batch_events = sorted(
            grouped[group],
            key=lambda item: (int(item["occurred_at_ms"]), str(item["event_id"])),
        )
        starts = [item for item in batch_events if item["event_type"] == "wheel_started"]
        if not starts:
            continue
        reasons = set(invalid_by_group.get(group, set()))
        if len(starts) != 1:
            reasons.add("wheel_start_conflict")
        start = starts[0]
        terminals = [
            item
            for item in batch_events
            if item["event_type"] in {"wheel_called_away", "wheel_manual_ended"}
        ]
        if len(terminals) > 1:
            reasons.add("wheel_terminal_conflict")

        stock_matches = stock_rows_by_id.get(lot_id, [])
        stock_row = stock_matches[0] if len(stock_matches) == 1 else None
        if len(stock_matches) > 1:
            reasons.add("assigned_stock_lot_conflict")
        start_trade_id = str(start.get("source_trade_event_id") or "").strip()
        start_trade = trade_by_id.get(start_trade_id)
        if (
            not start_trade_id
            or start_trade is None
            or _event_type(start_trade) != "assignment"
            or _trade_account(start_trade) != account
            or _trade_option_type(start_trade) != "put"
            or _trade_position_side(start_trade) != "short"
        ):
            reasons.add("wheel_start_source_invalid")
        if stock_row is not None and str(stock_row.get("source_assignment_event_id") or "") != start_trade_id:
            reasons.add("wheel_start_stock_lot_mismatch")

        linked_lots: list[tuple[str, dict[str, Any]]] = []
        for call_lot_id, fields in lots:
            call_key = lot_contract_key(fields)
            if str(call_key.get("account") or "").strip().lower() != account:
                continue
            if str(fields.get("source_stock_lot_id") or "").strip() != lot_id:
                continue
            if (
                str(fields.get("strategy") or "").strip().lower() != "wheel"
                or str(fields.get("leg_role") or "").strip().lower() != "wheel_call"
                or str(fields.get("strategy_group_id") or "").strip()
                or str(call_key.get("option_type") or "").strip().lower() != "call"
                or str(fields.get("position_side") or "").strip().lower() != "short"
            ):
                reasons.add("wheel_call_linkage_conflict")
                continue
            linked_lots.append((call_lot_id, fields))
        active_call_lot_ids = sorted(
            call_lot_id for call_lot_id, fields in linked_lots if _contracts_open(fields) > 0
        )

        assignment_ids = {
            str(row.get("event_id") or "").strip()
            for row in active_trade_events
            if _event_type(row) == "assignment"
            and str(row.get("target_lot_id") or "").strip()
            in {call_lot_id for call_lot_id, _fields in linked_lots}
        }
        called_events = [item for item in terminals if item["event_type"] == "wheel_called_away"]
        manual_events = [item for item in terminals if item["event_type"] == "wheel_manual_ended"]
        if called_events:
            source_id = str(called_events[0].get("source_trade_event_id") or "").strip()
            if source_id not in assignment_ids:
                reasons.add("wheel_called_away_source_invalid")

        active_intent_ids, intent_reasons, intent_summaries = _intent_state(
            batch_events,
            as_of_ms=instant,
            known_trade_event_ids=set(trade_by_id),
        )
        reasons.update(intent_reasons)

        shares_remaining: int | None = None
        if stock_row is not None:
            try:
                shares_remaining = int(stock_row.get("shares_remaining"))
            except (TypeError, ValueError):
                reasons.add("assigned_stock_shares_unavailable")
            if shares_remaining is not None and shares_remaining < 0:
                reasons.add("assigned_stock_shares_conflict")
        multiplier = None
        if start_trade is not None:
            try:
                multiplier = int(float(start_trade.get("multiplier") or 0))
            except (TypeError, ValueError):
                multiplier = None
        if multiplier is None or multiplier <= 0:
            reasons.add("contract_multiplier_unavailable")

        locked_shares = 0
        for _lot_id, fields in linked_lots:
            if _contracts_open(fields) <= 0:
                continue
            try:
                locked_shares += contract_share_quantity(_contracts_open(fields), fields.get("multiplier"))
            except (TypeError, ValueError):
                reasons.add("wheel_call_multiplier_invalid")
        if shares_remaining is not None and locked_shares > shares_remaining:
            reasons.add("wheel_call_overcovers_batch")
        rejected_call_event_ids = {
            str((item.get("payload") or {}).get("call_open_event_id") or "").strip()
            for item in batch_events
            if item["event_type"] == "wheel_call_linkage_rejected"
        }
        unresolved_lots: list[tuple[str, dict[str, Any]]] = []
        for call_lot_id, fields in lots:
            call_key = lot_contract_key(fields)
            if (
                str(call_key.get("account") or "").strip().lower() != account
                or str(call_key.get("underlying_symbol") or "").strip().upper()
                != str((stock_row or {}).get("symbol") or _trade_symbol(start_trade or {}))
                or str(call_key.get("option_type") or "").strip().lower() != "call"
                or str(fields.get("position_side") or "").strip().lower() != "short"
                or _contracts_open(fields) <= 0
                or strategy_metadata_has_owner(fields)
                or str(fields.get("open_event_id") or "").strip()
                in rejected_call_event_ids
            ):
                continue
            try:
                required = _contracts_open(fields) * int(
                    float(fields.get("multiplier") or 0)
                )
            except (TypeError, ValueError):
                continue
            if shares_remaining is not None and 0 < required <= shares_remaining:
                unresolved_lots.append((call_lot_id, fields))
        unresolved_call_lot_ids = sorted(call_lot_id for call_lot_id, _fields in unresolved_lots)
        if manual_events and (active_call_lot_ids or active_intent_ids):
            reasons.add("manual_end_has_active_call_or_intent")
        if called_events and shares_remaining != 0:
            reasons.add("called_away_stock_not_zero")
        if shares_remaining == 0 and assignment_ids and not called_events:
            reasons.add("called_away_event_missing")

        for review in review_rows:
            if str(review.get("stock_lot_id") or "").strip() != lot_id:
                continue
            if str(review.get("status") or "") in {
                "source_conflict",
                "incomplete_inventory_basis",
                "manual_review_required",
                "missing_stock_settlement",
            }:
                reasons.add("assigned_stock_projection_conflict")

        try:
            reserved_shares = sum(
                contract_share_quantity(item.get("remaining_contracts"), (item.get("payload") or {}).get("multiplier"))
                for item in intent_summaries if item.get("status") == "active"
            )
        except (TypeError, ValueError):
            reserved_shares = None
            reasons.add("wheel_intent_units_invalid")
        if any(item.get("status") == "conflict" for item in intent_summaries):
            reserved_shares = None
        conflict_codes = {
            reason
            for reason in reasons
            if reason.endswith("conflict")
            or reason.endswith("_invalid")
            or reason in {
                "invalid_wheel_event",
                "wheel_start_stock_lot_mismatch",
                "manual_end_has_active_call_or_intent",
                "called_away_stock_not_zero",
                "called_away_event_missing",
            }
        }
        integrity_status = "conflict" if conflict_codes else "trusted"
        terminal = terminals[0] if len(terminals) == 1 else None
        lifecycle_status = (
            "called_away"
            if terminal is not None and terminal["event_type"] == "wheel_called_away"
            else "manual_ended"
            if terminal is not None and terminal["event_type"] == "wheel_manual_ended"
            else "active"
        )
        if integrity_status == "conflict" or lifecycle_status != "active":
            phase = None
        elif unresolved_call_lot_ids:
            phase = "linkage_unresolved"
        elif active_call_lot_ids:
            phase = "call_open"
        elif active_intent_ids:
            phase = "call_pending"
        elif shares_remaining is not None and multiplier is not None and shares_remaining < multiplier:
            phase = "residual_stock"
        elif stock_row is None or shares_remaining is None or multiplier is None:
            phase = "data_unavailable"
        else:
            phase = "ready"

        related_lot_ids = {
            call_lot_id for call_lot_id, _fields in [*linked_lots, *unresolved_lots]
        }
        related_trade_ids = {
            start_trade_id,
            *assignment_ids,
            *{
                str(fields.get("open_event_id") or "").strip()
                for _lot_id, fields in linked_lots
            },
            *{
                str(item.get("source_trade_event_id") or "").strip()
                for item in batch_events
            },
        }
        related_trades = [
            row
            for row in active_trade_events
            if str(row.get("event_id") or "").strip() in related_trade_ids
            or str(row.get("target_lot_id") or "").strip() in related_lot_ids
        ]
        generation_payload = {
            "schema_version": WHEEL_PROJECTION_SCHEMA,
            "account": account,
            "stock_lot_id": lot_id,
            "wheel_events": [
                {
                    key: event.get(key)
                    for key in (
                        "event_id",
                        "account",
                        "stock_lot_id",
                        "event_type",
                        "occurred_at_ms",
                        "intent_id",
                        "source_trade_event_id",
                        "payload",
                        "payload_hash",
                    )
                }
                for event in batch_events
            ],
            "position_lots": [
                {"record_id": call_lot_id, "fields": fields}
                for call_lot_id, fields in [*linked_lots, *unresolved_lots]
            ],
            "trade_events": related_trades,
            "assigned_stock": _stable_stock_fact(stock_row),
        }
        batch_generation_hash = canonical_sha256(generation_payload)
        symbol = str(
            (stock_row or {}).get("symbol") or _trade_symbol(start_trade or {})
        ).strip().upper()
        result = {
            "account": account,
            "market": str(symbol_market(symbol) or "").strip().lower() or None,
            "symbol": symbol,
            "stock_lot_id": lot_id,
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "integrity_status": integrity_status,
            "reason_codes": sorted(reasons),
            "shares_remaining": shares_remaining,
            "batch_generation_hash": batch_generation_hash,
            "start_event_id": start["event_id"],
            "terminal_event_id": terminal["event_id"] if terminal is not None else None,
            "active_call_lot_ids": active_call_lot_ids,
            "active_option_committed_shares": None if "wheel_call_multiplier_invalid" in reasons else locked_shares,
            "unresolved_call_lot_ids": unresolved_call_lot_ids,
            "active_intent_ids": active_intent_ids,
            "active_intent_reserved_shares": reserved_shares,
            "candidate": None,
        }
        result["projection_hash"] = canonical_sha256(
            {
                "schema_version": WHEEL_PROJECTION_SCHEMA,
                "batch_generation_hash": batch_generation_hash,
                "as_of_ms": instant,
                "derived": result,
            }
        )
        results.append(result)
    return results


def project_wheel_branches(
    wheel_events: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]],
    position_lots: Sequence[Mapping[str, Any]],
    assigned_stock_projection: Mapping[str, Any],
    as_of_ms: int,
    *,
    monitoring_gate: str = "disabled",
) -> list[dict[str, Any]]:
    """Project v2 branches while adapting legacy Call batches unchanged."""

    instant = _positive_int(as_of_ms, "as_of_ms")
    gate = str(monitoring_gate or "disabled").strip().lower()
    if gate not in {"enabled", "disabled", "config_mismatch"}:
        raise ValueError("invalid Wheel monitoring gate")
    legacy_batches = project_wheel_lifecycles(
        wheel_events,
        trade_events,
        position_lots,
        assigned_stock_projection,
        instant,
    )
    branches: list[dict[str, Any]] = []
    for batch in legacy_batches:
        phase = {
            "call_open": "option_open",
            "call_pending": "intent_pending",
            "residual_stock": "residual_capacity",
        }.get(batch.get("phase"), batch.get("phase"))
        lifecycle_status = (
            "converted"
            if batch.get("lifecycle_status") == "called_away"
            else batch.get("lifecycle_status")
        )
        branches.append(
            {
                **batch,
                "wheel_branch_id": batch["stock_lot_id"],
                "parent_branch_id": None,
                "direction": "call",
                "lifecycle_status": lifecycle_status,
                "phase": phase,
                "monitoring_gate": gate,
                "batch_generation_hash": batch["batch_generation_hash"],
                "legacy_call_adapter": True,
            }
        )

    effective_events, invalid_by_group = effective_wheel_events(
        wheel_events,
        as_of_ms=instant,
        trade_events=trade_events,
        known_trade_event_ids={str(event.get("event_id") or "") for event in _active_trade_events(trade_events)
                               if int(event.get("event_time_ms") or 0) <= instant},
    )
    v2_events = [
        event
        for event in effective_events
        if event["event_schema_version"] == WHEEL_EVENT_SCHEMA_V2
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in v2_events:
        grouped[(event["account"], event["wheel_branch_id"])].append(event)
    created_events = [
        event for event in v2_events if event["event_type"] == "wheel_branch_created"
    ]
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in created_events:
        parent = str(event["payload"].get("parent_branch_id") or "").strip()
        if parent:
            children_by_parent[parent].append(event)

    active_trade_events = _active_trade_events(trade_events)
    trade_by_id = {
        str(item.get("event_id") or "").strip(): item
        for item in active_trade_events
        if str(item.get("event_id") or "").strip()
    }
    trade_ids = set(trade_by_id)
    allocations_by_lot: dict[str, list[Any]] = defaultdict(list)
    allocation_projection_available = True
    try:
        economic_projection = project_trade_events(
            [TradeEvent.from_dict(dict(item)) for item in trade_events]
        )
    except (TypeError, ValueError, OverflowError):
        allocation_projection_available = False
    else:
        for allocation in economic_projection.allocations:
            allocations_by_lot[allocation.target_lot_id].append(allocation)
    stock_rows = assigned_stock_projection.get("_all_assigned_stock_lots")
    if not isinstance(stock_rows, Sequence):
        stock_rows = assigned_stock_projection.get("assigned_stock_lots") or []
    stock_by_id = {
        str(item.get("stock_lot_id") or "").strip(): item
        for item in stock_rows
        if isinstance(item, Mapping) and str(item.get("stock_lot_id") or "").strip()
    }
    strategy_by_lot_id = lot_strategy_metadata_from_trade_events(active_trade_events)
    lots = [
        (
            str(item.get("record_id") or "").strip(),
            attach_lot_strategy_metadata(item, strategy_by_lot_id),
        )
        for item in position_lots
        if isinstance(item, Mapping)
    ]
    known_legacy_ids = {
        str(item.get("wheel_branch_id") or "") for item in branches
    }
    for group in sorted(grouped):
        account, branch_id = group
        if branch_id in known_legacy_ids:
            continue
        events = sorted(
            grouped[group],
            key=lambda item: (int(item["occurred_at_ms"]), str(item["event_id"])),
        )
        creations = [
            item for item in events if item["event_type"] == "wheel_branch_created"
        ]
        if not creations:
            continue
        reasons = set(invalid_by_group.get(group, set()))
        if len(creations) != 1:
            reasons.add("wheel_branch_creation_conflict")
        created = creations[0]
        payload = created["payload"]
        direction = str(payload.get("direction") or "").strip().lower()
        if direction not in {"call", "put"}:
            reasons.add("wheel_branch_direction_invalid")
        symbol = str(payload.get("symbol") or "").strip().upper()
        try:
            market = _wheel_market(symbol)
        except ValueError:
            market = ""
            reasons.add("wheel_branch_market_invalid")
        if str(payload.get("market") or "").strip().lower() not in {"", market}:
            reasons.add("wheel_branch_market_mismatch")
        if any(
            str((item.get("payload") or {}).get("market") or "").strip().lower()
            not in {"", market}
            for item in events
        ):
            reasons.add("wheel_event_market_mismatch")
        source_assignment_event_id = str(
            payload.get("source_assignment_event_id")
            or created.get("source_trade_event_id")
            or ""
        ).strip()
        if not source_assignment_event_id or source_assignment_event_id not in trade_ids:
            reasons.add("wheel_branch_source_invalid")
        source_assignment = None
        try:
            source_assignment = TradeEvent.from_dict(
                dict(trade_by_id[source_assignment_event_id])
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        try:
            initial_contracts = _positive_int(payload.get("contracts"), "contracts")
        except ValueError:
            initial_contracts = 0
            reasons.add("wheel_branch_quantity_invalid")
        try:
            multiplier = (
                _positive_int(payload.get("multiplier"), "multiplier")
                if payload.get("multiplier") is not None
                else 0
            )
        except ValueError:
            multiplier = 0
            reasons.add("wheel_branch_quantity_invalid")
        multiplier_source = str(payload.get("multiplier_source") or "").strip()
        multiplier_evidence_hash = str(
            payload.get("multiplier_evidence_hash") or ""
        ).strip()
        assignment_multiplier_evidence = (
            broker_settlement_multiplier_evidence(source_assignment)
            if source_assignment is not None
            else None
        )
        if (
            assignment_multiplier_evidence is not None
            and assignment_multiplier_evidence["multiplier"] == multiplier
        ):
            multiplier_source = "broker_settlement_pair"
            multiplier_evidence_hash = canonical_sha256(
                assignment_multiplier_evidence
            )
        if not multiplier_source or not multiplier_evidence_hash:
            reasons.add("multiplier_unproven")
        elif multiplier_source == "unproven":
            reasons.add("multiplier_unproven")
        elif multiplier_source == "conflict":
            reasons.add("multiplier_conflict")
        principal_anchor = payload.get("principal_anchor")
        currency = payload.get("currency")
        principal_anchor_fact_ids = tuple(
            payload.get("principal_anchor_fact_ids") or ()
        )
        if principal_anchor in (None, "") and source_assignment is not None:
            (
                current_anchor,
                current_currency,
                _current_anchor_reason,
                current_fact_ids,
            ) = assignment_principal_anchor(source_assignment, direction)
            if current_anchor is not None:
                principal_anchor = current_anchor
                currency = current_currency
                principal_anchor_fact_ids = current_fact_ids
        if principal_anchor in (None, ""):
            reasons.add(
                str(payload.get("principal_anchor_reason") or "principal_anchor_unavailable")
            )
        if not str(currency or "").strip():
            reasons.add("assignment_currency_unavailable")
        child_events = children_by_parent.get(branch_id, [])
        converted_contracts = 0
        for child in child_events:
            try:
                converted_contracts += _positive_int(
                    child["payload"].get("contracts"),
                    "contracts",
                )
            except ValueError:
                reasons.add("wheel_child_quantity_invalid")
            child_source = str(
                child["payload"].get("source_assignment_event_id")
                or child.get("source_trade_event_id")
                or ""
            ).strip()
            if not child_source or child_source not in trade_ids:
                reasons.add("wheel_child_source_conflict")
        remaining_contracts = initial_contracts - converted_contracts
        if remaining_contracts < 0:
            reasons.add("wheel_branch_conversion_conflict")

        decisions = [
            item for item in events if item["event_type"] == "wheel_branch_decided"
        ]
        manual_ends = [
            item for item in events if item["event_type"] == "wheel_manual_ended"
        ]
        decision_values = {
            str(item["payload"].get("decision") or "").strip().lower()
            for item in decisions
        }
        if len(decisions) > 1 or len(decision_values) > 1 or (
            decisions and manual_ends
        ) or len(manual_ends) > 1:
            reasons.add("wheel_branch_decision_conflict")
        initial_status = str(
            payload.get("initial_lifecycle_status") or "active"
        ).strip().lower()
        lifecycle_status = initial_status
        terminal_event_id = None
        if manual_ends:
            lifecycle_status = "manual_ended"
            terminal_event_id = manual_ends[0]["event_id"]
        elif decisions:
            decision = next(iter(decision_values), "")
            terminal_event_id = decisions[0]["event_id"] if decision == "end" else None
            if initial_status != "pending_decision":
                reasons.add("wheel_branch_decision_conflict")
            elif decision == "start":
                lifecycle_status = "active"
            elif decision == "end":
                lifecycle_status = "manual_ended"
        if lifecycle_status == "active" and initial_contracts > 0 and remaining_contracts == 0:
            lifecycle_status = "converted"

        linked_lots = [
            (option_lot_id, fields)
            for option_lot_id, fields in lots
            if str(lot_contract_key(fields).get("account") or "").strip().lower() == account
            and str(fields.get("source_wheel_branch_id") or "").strip() == branch_id
        ]
        realized_put_net_pnl: float | None = None
        if direction == "put":
            realized_net = Decimal(0)
            realized_net_available = allocation_projection_available
            for option_lot_id, fields in linked_lots:
                try:
                    closed_contracts = int(fields.get("contracts_closed") or 0)
                except (TypeError, ValueError):
                    realized_net_available = False
                    break
                allocations = allocations_by_lot.get(option_lot_id, [])
                if closed_contracts < 0 or sum(item.contracts for item in allocations) != closed_contracts:
                    realized_net_available = False
                    break
                if any(item.realized_pnl_net is None for item in allocations):
                    realized_net_available = False
                    break
                realized_net += sum(
                    (item.realized_pnl_net for item in allocations),
                    Decimal(0),
                )
            if realized_net_available:
                realized_put_net_pnl = float(realized_net)
            else:
                reasons.add("realized_put_net_pnl_unavailable")
        active_lot_ids = sorted(
            option_lot_id
            for option_lot_id, fields in linked_lots
            if _contracts_open(fields) > 0
        )
        expected_role = f"wheel_{direction}"
        for _lot_id, fields in linked_lots:
            if (
                str(fields.get("strategy") or "").strip().lower() != "wheel"
                or str(fields.get("leg_role") or "").strip().lower() != expected_role
                or str(fields.get("strategy_group_id") or "").strip()
            ):
                reasons.add("wheel_option_linkage_conflict")
        active_intent_ids, intent_reasons, intent_summaries = _intent_state(
            events,
            as_of_ms=instant,
            known_trade_event_ids=trade_ids,
            direction=direction if direction in {"call", "put"} else "call",
        )
        reasons.update(intent_reasons)
        active_intent_reservations: list[dict[str, Any]] = []
        if direction == "put":
            for summary in intent_summaries:
                if summary.get("status") != "active":
                    continue
                intent_payload = summary.get("payload")
                intent_payload = (
                    intent_payload if isinstance(intent_payload, Mapping) else {}
                )
                strike = _finite_float(intent_payload.get("strike"))
                intent_multiplier = _finite_float(intent_payload.get("multiplier"))
                currency = str(
                    intent_payload.get("cash_reservation_currency") or ""
                ).strip().upper()
                capacity_hash = str(
                    intent_payload.get("capacity_identity_hash") or ""
                ).strip()
                remaining = int(summary.get("remaining_contracts") or 0)
                if (
                    strike is None
                    or strike <= 0
                    or intent_multiplier is None
                    or intent_multiplier <= 0
                    or not intent_multiplier.is_integer()
                    or remaining <= 0
                    or not currency
                    or not capacity_hash
                ):
                    reasons.add("intent_cash_reservation_invalid")
                    continue
                active_intent_reservations.append(
                    {
                        "account": account,
                        "wheel_branch_id": branch_id,
                        "intent_id": summary["intent_id"],
                        "batch_generation_hash": intent_payload.get(
                            "batch_generation_hash"
                        ),
                        "capacity_identity_hash": capacity_hash,
                        "currency": currency,
                        "cash_reservation_amount": round(
                            strike * contract_share_quantity(remaining, intent_multiplier),
                            6,
                        ),
                        "remaining_contracts": remaining,
                    }
                )
        try:
            committed_shares = sum(contract_share_quantity(_contracts_open(fields), fields.get("multiplier"))
                                   for _, fields in linked_lots if _contracts_open(fields) > 0)
        except (TypeError, ValueError):
            committed_shares = None
            reasons.add("wheel_option_units_invalid")
        try:
            reserved_shares = sum(contract_share_quantity(item.get("remaining_contracts"), (item.get("payload") or {}).get("multiplier"))
                for item in intent_summaries if item.get("status") == "active")
        except (TypeError, ValueError):
            reserved_shares = None
            reasons.add("wheel_intent_units_invalid")
        if any(item.get("status") == "conflict" for item in intent_summaries):
            reserved_shares = None
        lot_id = str(created.get("stock_lot_id") or "").strip() or None
        stock_row = stock_by_id.get(lot_id or "")
        if direction == "call" and stock_row is None:
            reasons.add("assigned_stock_lot_unavailable")

        conflict = any(
            reason.endswith("conflict")
            or reason.endswith("_invalid")
            or reason == "invalid_wheel_event"
            for reason in reasons
        )
        integrity_status = "conflict" if conflict else "trusted"
        if lifecycle_status in {"converted", "manual_ended"}:
            phase = lifecycle_status
        elif lifecycle_status == "pending_decision":
            phase = "pending_decision"
        elif conflict:
            phase = "conflict"
        elif active_lot_ids:
            phase = "option_open"
        elif active_intent_ids:
            phase = "intent_pending"
        elif remaining_contracts <= 0 or (
            direction == "call"
            and stock_row is not None
            and int(stock_row.get("shares_remaining") or 0) < multiplier
        ):
            phase = "residual_capacity"
        elif reasons:
            phase = "data_unavailable"
        else:
            phase = "ready"

        generation_payload = {
            "schema_version": WHEEL_PROJECTION_SCHEMA,
            "account": account,
            "wheel_branch_id": branch_id,
            "events": events,
            "children": sorted(
                (
                    {
                        "event_id": item["event_id"],
                        "payload_hash": item["payload_hash"],
                    }
                    for item in child_events
                ),
                key=lambda item: item["event_id"],
            ),
            "position_lots": [
                {"record_id": option_lot_id, "fields": fields}
                for option_lot_id, fields in linked_lots
            ],
            "assigned_stock": _stable_stock_fact(stock_row),
            "realized_put_net_pnl_in_current_stage": realized_put_net_pnl,
            "source_assignment_facts": {
                "multiplier_source": multiplier_source,
                "multiplier_evidence_hash": multiplier_evidence_hash,
                "principal_anchor": principal_anchor,
                "principal_anchor_fact_ids": principal_anchor_fact_ids,
                "currency": currency,
            },
        }
        batch_generation_hash = canonical_sha256(generation_payload)
        branch = {
            "account": account,
            "market": market or None,
            "symbol": symbol,
            "wheel_branch_id": branch_id,
            "parent_branch_id": str(payload.get("parent_branch_id") or "").strip() or None,
            "direction": direction,
            "stock_lot_id": lot_id,
            "source_assignment_event_id": source_assignment_event_id,
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "monitoring_gate": gate,
            "integrity_status": integrity_status,
            "reason_codes": sorted(reasons),
            "initial_contracts": initial_contracts,
            "converted_contracts": converted_contracts,
            "remaining_contracts": max(0, remaining_contracts),
            "multiplier": multiplier or None,
            "shares_opened": (
                int(stock_row.get("shares_opened") or 0) if stock_row is not None else None
            ),
            "shares_remaining": (
                int(stock_row.get("shares_remaining") or 0)
                if stock_row is not None
                else None
            ),
            "principal_anchor": principal_anchor,
            "realized_put_net_pnl_in_current_stage": realized_put_net_pnl,
            "currency": currency,
            "activation_window": payload.get("activation_window"),
            "start_event_id": created["event_id"],
            "terminal_event_id": terminal_event_id,
            "active_option_lot_ids": active_lot_ids,
            "active_intent_reserved_shares": reserved_shares,
            "active_option_committed_contracts": sum(_contracts_open(fields) for _, fields in linked_lots),
            "active_option_committed_shares": committed_shares,
            "active_intent_ids": active_intent_ids,
            "active_intent_reserved_contracts": sum(
                int(item.get("remaining_contracts") or 0)
                for item in intent_summaries
                if item.get("status") == "active"
            ),
            "active_intent_reservations": active_intent_reservations,
            "batch_generation_hash": batch_generation_hash,
            "legacy_call_adapter": False,
            "candidate": None,
        }
        branch["projection_hash"] = canonical_sha256(
            {
                "schema_version": WHEEL_PROJECTION_SCHEMA,
                "batch_generation_hash": batch_generation_hash,
                "as_of_ms": instant,
                "derived": branch,
            }
        )
        branches.append(branch)
    return sorted(
        branches,
        key=lambda item: (
            str(item.get("account") or ""),
            str(item.get("wheel_branch_id") or ""),
        ),
    )


def project_wheel_coverage(branch: Mapping[str, Any]) -> dict[str, Any]:
    """Quantity coverage; reservations never count as committed option exposure."""
    def quantity(value: Any) -> int | None:
        try:
            number = Decimal(str(value))
            return int(number) if number.is_finite() and number >= 0 and number == int(number) else None
        except (ValueError, TypeError, ArithmeticError):
            return None
    direction = branch.get("direction") or "call"
    multiplier = quantity(branch.get("multiplier"))
    target = quantity(branch.get("shares_remaining"))
    if direction == "put":
        remaining = quantity(branch.get("remaining_contracts"))
        target = remaining * multiplier if remaining is not None and multiplier else None
    committed = quantity(branch.get("active_option_committed_shares"))
    if committed is None and not branch.get("active_option_lot_ids") and not branch.get("active_call_lot_ids"):
        committed = 0
    reserved = quantity(branch.get("active_intent_reserved_shares"))
    if "active_intent_reserved_shares" not in branch:
        contracts = quantity(branch.get("active_intent_reserved_contracts", 0))
        reserved = contracts * multiplier if contracts is not None and multiplier else 0 if contracts == 0 else None
    reasons = []
    if target is None or committed is None or reserved is None or not multiplier:
        reasons.append("coverage_quantity_unavailable")
    if branch.get("phase") == "linkage_unresolved":
        reasons.append("linkage_unresolved")
    if branch.get("integrity_status") != "trusted":
        reasons.extend(branch.get("reason_codes") or ["wheel_integrity_conflict"])
    available = max(0, target - committed - reserved) if all(v is not None for v in (target, committed, reserved)) else None
    overallocated = bool(multiplier) and all(v is not None for v in (target, committed, reserved)) and committed + reserved > target
    status = ("overallocated" if overallocated else "unavailable" if reasons else
              "not_applicable" if target == 0 else "none" if committed == 0 else "full" if committed == target else "partial")
    if status == "overallocated":
        reasons.append("coverage_overallocated")
    return {"status": status, "target_shares": target, "committed_shares": committed,
            "reserved_shares": reserved, "available_shares": available, "reason_codes": sorted(set(reasons))}
