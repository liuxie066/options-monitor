from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.lifecycle_allocation import resolve_allocations
from src.application.ledger.api import lifecycle_reconciliation_facts
from src.application.trades.close_reason_evidence import canonical_hash
from src.application.trades.lifecycle_reconciliation import (
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)


MANUAL_RESOLUTION_SCHEMA = "manual_lifecycle_resolution.v1"
MANUAL_REASONS = {
    "assignment": "assignment",
    "exercise": "exercise",
    "expiration-no-settlement": "expire_close",
    "trade-close": "close",
}


def resolve_lifecycle_manually(
    repo: Any,
    *,
    case_id: str,
    expected_revision: int,
    reason: str,
    broker_ref: str,
    note: str,
    void_terminal_event_id: str | None = None,
    apply_changes: bool = False,
    now_ms: int,
    wheel_start_enabled: bool = False,
) -> dict[str, Any]:
    """Preview or atomically apply one evidence-backed manual resolution."""

    case_value = str(case_id or "").strip()
    reason_value = str(reason or "").strip().lower()
    terminal_type = MANUAL_REASONS.get(reason_value)
    broker_ref_value = str(broker_ref or "").strip()
    note_value = str(note or "").strip()
    void_target = str(void_terminal_event_id or "").strip()
    if not case_value:
        raise ValueError("manual lifecycle resolution requires exact case id")
    if terminal_type is None:
        raise ValueError("manual lifecycle reason is invalid")
    if int(expected_revision) < 0:
        raise ValueError("expected lifecycle revision is invalid")
    if not broker_ref_value and not note_value:
        raise ValueError(
            "manual lifecycle resolution requires broker reference or note"
        )

    facts = lifecycle_reconciliation_facts(
        repo,
        case_id=case_value,
    )
    lifecycle_case = next(iter(facts["cases"]), None)
    if not isinstance(lifecycle_case, dict):
        raise ValueError(f"lifecycle case not found: {case_value}")
    current_summary = (
        dict(lifecycle_case.get("derived_summary") or {})
        if isinstance(lifecycle_case.get("derived_summary"), dict)
        else {}
    )
    current_revision = int(
        current_summary.get("resolution_revision") or 0
    )

    seed = {
        "schema_version": MANUAL_RESOLUTION_SCHEMA,
        "case_id": case_value,
        "expected_revision": int(expected_revision),
        "reason": reason_value,
        "broker_ref": broker_ref_value,
        "note": note_value,
        "void_terminal_event_id": void_target or None,
    }
    evidence_id = "manual_lifecycle_" + canonical_hash(seed)
    if _manual_resolution_already_applied(
        facts,
        evidence_id=evidence_id,
        void_target=void_target,
    ):
        return {
            "schema_version": "manual_lifecycle_resolution_result.v1",
            "status": "idempotent",
            "apply_changes": bool(apply_changes),
            "case_id": case_value,
            "expected_revision": int(expected_revision),
            "current_revision": current_revision,
            "evidence_id": evidence_id,
            "void_terminal_event_id": void_target or None,
            "read_model": lifecycle_case_read_model(
                repo,
                case_id=case_value,
                now_ms=now_ms,
            ),
        }
    if current_revision != int(expected_revision):
        raise ValueError(
            "lifecycle resolution revision compare-and-set failed: "
            f"expected={int(expected_revision)} actual={current_revision}"
        )

    allocations = [
        dict(item)
        for item in facts["allocations"]
        if isinstance(item, dict)
    ]
    effective_void_ids = {
        str(item)
        for item in facts.get("effective_void_event_ids") or ()
        if str(item or "").strip()
    }
    correction_void_event: TradeEvent | None = None
    if void_target:
        correction_void_event = _build_correction_void_event(
            repo,
            lifecycle_case=lifecycle_case,
            allocations=allocations,
            effective_void_ids=effective_void_ids,
            target_event_id=void_target,
            evidence_id=evidence_id,
            broker_ref=broker_ref_value,
            note=note_value,
            event_time_ms=int(now_ms),
        )
        effective_void_ids.add(void_target)

    resolution = resolve_allocations(
        dict(lifecycle_case.get("target_contracts_by_lot") or {}),
        allocations,
        void_event_ids=effective_void_ids,
    )
    if resolution.status != "ok":
        raise ValueError(
            "lifecycle allocations conflict: "
            + ",".join(resolution.reason_codes)
        )
    remaining = int(resolution.remaining_contracts)
    if remaining <= 0:
        raise ValueError(
            "lifecycle case has no unresolved contracts; use correct with "
            "an effective terminal event"
        )

    evidence = _manual_evidence(
        lifecycle_case=lifecycle_case,
        evidence_rows=_all_lifecycle_evidence(repo),
        evidence_id=evidence_id,
        terminal_type=terminal_type,
        contracts=remaining,
        broker_ref=broker_ref_value,
        note=note_value,
        expected_revision=int(expected_revision),
        now_ms=int(now_ms),
    )
    reconciliation = reconcile_lifecycle_evidence(
        repo,
        evidence=evidence,
        case_id=case_value,
        apply_changes=bool(apply_changes),
        now_ms=int(now_ms),
        expected_resolution_revision=int(expected_revision),
        correction_void_events=(
            (correction_void_event,)
            if correction_void_event is not None
            else ()
        ),
        notification_transition_type=(
            "resolution_corrected"
            if correction_void_event is not None
            else None
        ),
        wheel_start_enabled=wheel_start_enabled,
    )
    next_revision = (
        int(
            (reconciliation.ledger_result or {}).get(
                "resolution_revision"
            )
            or current_revision
        )
        if apply_changes
        else current_revision + 1
    )
    return {
        "schema_version": "manual_lifecycle_resolution_result.v1",
        "status": reconciliation.status,
        "apply_changes": bool(apply_changes),
        "case_id": case_value,
        "reason": reason_value,
        "terminal_type": terminal_type,
        "broker_ref": broker_ref_value or None,
        "note": note_value or None,
        "expected_revision": int(expected_revision),
        "current_revision": current_revision,
        "next_revision": next_revision,
        "evidence": evidence,
        "void_event": (
            correction_void_event.to_dict()
            if correction_void_event is not None
            else None
        ),
        "allocation_plan": [
            dict(item) for item in reconciliation.allocation_plan
        ],
        "terminal_event_ids": sorted(
            str(
                item.get("canonical_terminal_event_id")
                or ""
            )
            for item in reconciliation.allocation_plan
            if str(
                item.get("canonical_terminal_event_id")
                or ""
            ).strip()
        ),
        "projection_diff": {
            "before_remaining_contracts_by_lot": (
                resolution.remaining_contracts_by_lot
            ),
            "after_remaining_contracts_by_lot": (
                {
                    lot_id: 0
                    for lot_id in resolution.remaining_contracts_by_lot
                }
                if reconciliation.status
                in {"dry_run", "applied", "idempotent"}
                else resolution.remaining_contracts_by_lot
            ),
        },
        "outbox_transition": (
            {
                "transition_type": "resolution_corrected",
                "transition_key": (
                    f"lifecycle:{case_value}:"
                    f"resolution_corrected:{next_revision}"
                ),
                "resolution_revision": next_revision,
            }
            if correction_void_event is not None
            else {
                "transition_type": "resolution_confirmed",
                "transition_key": (
                    f"lifecycle:{case_value}:resolution_confirmed"
                ),
                "resolution_revision": next_revision,
            }
        ),
        "reconciliation": reconciliation.to_dict(),
    }


def _manual_resolution_already_applied(
    facts: dict[str, Any],
    *,
    evidence_id: str,
    void_target: str,
) -> bool:
    evidence_exists = any(
        str(item.get("evidence_id") or "").strip() == evidence_id
        for item in facts.get("evidence") or ()
        if isinstance(item, dict)
    )
    allocation_exists = any(
        str(item.get("evidence_id") or "").strip() == evidence_id
        for item in facts.get("allocations") or ()
        if isinstance(item, dict)
    )
    if not evidence_exists or not allocation_exists:
        return False
    if not void_target:
        return True
    return void_target in {
        str(item)
        for item in facts.get("effective_void_event_ids") or ()
    }


def _build_correction_void_event(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    allocations: list[dict[str, Any]],
    effective_void_ids: set[str],
    target_event_id: str,
    evidence_id: str,
    broker_ref: str,
    note: str,
    event_time_ms: int,
) -> TradeEvent:
    matching = [
        item
        for item in allocations
        if str(
            item.get("canonical_terminal_event_id") or ""
        ).strip()
        == target_event_id
    ]
    if len(matching) != 1 or target_event_id in effective_void_ids:
        raise ValueError(
            "void target must be one effective allocation event in this case"
        )
    target_event = next(
        (
            item
            for item in _list_trade_events(repo)
            if str(item.get("event_id") or "").strip()
            == target_event_id
        ),
        None,
    )
    if not isinstance(target_event, dict):
        raise ValueError(
            "lifecycle correction terminal event was not found"
        )
    contract_key = ContractKey.from_values(
        broker=lifecycle_case.get("broker")
        or target_event.get("broker"),
        account=lifecycle_case.get("account")
        or target_event.get("account"),
        underlying_symbol=lifecycle_case.get("symbol")
        or target_event.get("symbol"),
        option_type=lifecycle_case.get("option_type")
        or target_event.get("option_type"),
        position_side=lifecycle_case.get("position_side")
        or target_event.get("position_side")
        or target_event.get("side"),
        strike=lifecycle_case.get("strike")
        or target_event.get("strike"),
        expiration_ymd=lifecycle_case.get("expiration_ymd")
        or target_event.get("expiration_ymd"),
    )
    void_id = "lifecycle_correction_void_" + canonical_hash(
        {
            "case_id": lifecycle_case.get("case_id"),
            "target_event_id": target_event_id,
            "evidence_id": evidence_id,
        }
    )
    return TradeEvent(
        event_id=void_id,
        event_type="void",
        event_time_ms=int(event_time_ms),
        contract_key=contract_key,
        contracts=0,
        price=0,
        currency=str(target_event.get("currency") or ""),
        source="manual_lifecycle_correction",
        multiplier=float(
            lifecycle_case.get("multiplier")
            or target_event.get("multiplier")
            or 100
        ),
        target_event_id=target_event_id,
        raw_payload={
            "schema_version": "lifecycle_correction_void.v1",
            "source": "om option lifecycle correct",
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "evidence_id": evidence_id,
            "void_target_event_id": target_event_id,
            "broker_ref": broker_ref or None,
            "note": note or None,
        },
    )


def _manual_evidence(
    *,
    lifecycle_case: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    evidence_id: str,
    terminal_type: str,
    contracts: int,
    broker_ref: str,
    note: str,
    expected_revision: int,
    now_ms: int,
) -> dict[str, Any]:
    base = {
        "schema_version": MANUAL_RESOLUTION_SCHEMA,
        "evidence_id": evidence_id,
        "case_id": lifecycle_case.get("case_id"),
        "source_type": "manual_lifecycle_resolution",
        "source_event_id": f"manual:{evidence_id}",
        "evidence_type": terminal_type,
        "terminal_type": terminal_type,
        "account": lifecycle_case.get("account"),
        "symbol": lifecycle_case.get("symbol"),
        "option_type": lifecycle_case.get("option_type"),
        "position_side": lifecycle_case.get("position_side"),
        "strike": lifecycle_case.get("strike"),
        "expiration_ymd": lifecycle_case.get("expiration_ymd"),
        "contracts": int(contracts),
        "currency": lifecycle_case.get("currency"),
        "broker_ref": broker_ref or None,
        "operator_note": note or None,
        "expected_resolution_revision": int(expected_revision),
    }
    if terminal_type == "expire_close":
        observation = _complete_observation_for_ref(
            evidence_rows,
            broker_ref=broker_ref,
        )
        return {
            **base,
            "event_time_ms": int(
                observation.get("observed_at_ms") or now_ms
            ),
            "observation_id": observation.get("observation_id"),
            "observation_hash": canonical_hash(observation),
            "observation": observation,
        }
    broker_evidence = _broker_evidence_for_ref(
        evidence_rows,
        broker_ref=broker_ref,
    )
    event_time_ms = int(
        broker_evidence.get("trade_time_ms")
        or broker_evidence.get("event_time_ms")
        or 0
    )
    if event_time_ms <= 0:
        raise ValueError(
            "broker reference does not provide execution time"
        )
    canonical_ref = str(
        broker_evidence.get("source_event_id") or ""
    ).strip()
    if terminal_type in {"assignment", "exercise"}:
        raw = (
            dict(broker_evidence.get("raw") or {})
            if isinstance(broker_evidence.get("raw"), dict)
            else {}
        )
        shares = abs(
            int(
                broker_evidence.get("stock_qty")
                or raw.get("contracts")
                or raw.get("qty")
                or 0
            )
        )
        return {
            **base,
            "event_time_ms": event_time_ms,
            "stock_settlement": {
                "source_event_id": canonical_ref,
                "futu_account_id": (
                    broker_evidence.get("futu_account_id")
                    or raw.get("futu_account_id")
                ),
                "symbol": broker_evidence.get("symbol")
                or raw.get("symbol"),
                "side": broker_evidence.get("side")
                or raw.get("side"),
                "shares": shares,
                "price": broker_evidence.get("stock_price")
                or raw.get("price"),
                "event_time_ms": event_time_ms,
                "order_id": broker_evidence.get("order_id")
                or raw.get("order_id"),
                "clearing_date": (
                    broker_evidence.get("clearing_date")
                    or raw.get("clearing_date")
                ),
            },
            "source_evidence_ids": [
                str(
                    broker_evidence.get("evidence_id") or ""
                ).strip()
            ],
        }
    price = float(
        broker_evidence.get("price")
        or (
            broker_evidence.get("raw") or {}
        ).get("price")
        or 0
    )
    if price <= 0:
        raise ValueError(
            "trade-close requires positive-price broker evidence"
        )
    return {
        **base,
        "event_time_ms": event_time_ms,
        "price": price,
        "broker_close": {
            "source_event_id": canonical_ref,
            "futu_account_id": broker_evidence.get(
                "futu_account_id"
            ),
            "side": broker_evidence.get("side"),
            "order_id": broker_evidence.get("order_id"),
            "clearing_date": broker_evidence.get(
                "clearing_date"
            ),
        },
        "source_evidence_ids": [
            str(broker_evidence.get("evidence_id") or "").strip()
        ],
    }


def _complete_observation_for_ref(
    evidence_rows: Iterable[dict[str, Any]],
    *,
    broker_ref: str,
) -> dict[str, Any]:
    matches: dict[str, dict[str, Any]] = {}
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            continue
        candidates = [evidence]
        for key in ("observation", "raw"):
            if isinstance(evidence.get(key), dict):
                candidates.append(dict(evidence[key]))
        for candidate in candidates:
            observation_id = str(
                candidate.get("observation_id") or ""
            ).strip()
            if (
                candidate.get("schema_version")
                in {
                    "broker_settlement_observation.v1",
                    "broker_settlement_observation.v2",
                }
                and bool(candidate.get("complete"))
                and broker_ref
                in {
                    observation_id,
                    str(evidence.get("evidence_id") or "").strip(),
                    str(evidence.get("source_event_id") or "").strip(),
                }
            ):
                matches[observation_id] = candidate
    if len(matches) != 1:
        raise ValueError(
            "expiration-no-settlement requires one complete frozen "
            "broker observation matching broker-ref"
        )
    return next(iter(matches.values()))


def _broker_evidence_for_ref(
    evidence_rows: Iterable[dict[str, Any]],
    *,
    broker_ref: str,
) -> dict[str, Any]:
    ref = str(broker_ref or "").strip()
    if not ref:
        raise ValueError(
            "assignment, exercise and trade-close require broker-ref"
        )
    matches: dict[str, dict[str, Any]] = {}
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            continue
        source_event_id = str(
            evidence.get("source_event_id") or ""
        ).strip()
        deal_id = source_event_id.rsplit(":", 1)[-1]
        if ref not in {
            source_event_id,
            deal_id,
            str(evidence.get("evidence_id") or "").strip(),
        }:
            continue
        if str(evidence.get("evidence_type") or "").strip().lower() not in {
            "stock_settlement_leg",
            "normal_close_deal",
            "trade_close",
            "close",
        }:
            continue
        matches[
            str(evidence.get("evidence_id") or source_event_id)
        ] = dict(evidence)
        continue
    for evidence in evidence_rows:
        observation = (
            dict(evidence.get("observation") or {})
            if isinstance(evidence.get("observation"), dict)
            else {}
        )
        relevant = (
            dict(observation.get("relevant_rows") or {})
            if isinstance(
                observation.get("relevant_rows"),
                dict,
            )
            else {}
        )
        for raw_row in relevant.get("history_deals") or []:
            if not isinstance(raw_row, dict):
                continue
            deal_id = str(
                raw_row.get("deal_id")
                or raw_row.get("dealID")
                or raw_row.get("source_deal_id")
                or raw_row.get("id")
                or ""
            ).strip()
            if ref != deal_id:
                continue
            account = str(
                observation.get("account")
                or evidence.get("account")
                or ""
            ).strip().lower()
            futu_account_id = str(
                observation.get("futu_account_id") or ""
            ).strip()
            if not account or not futu_account_id or not deal_id:
                continue
            source_event_id = (
                f"futu:{account}:{futu_account_id}:{deal_id}"
            )
            matches[
                f"{evidence.get('evidence_id')}:{deal_id}"
            ] = {
                "evidence_id": str(
                    evidence.get("evidence_id") or ""
                ),
                "source_event_id": source_event_id,
                "futu_account_id": futu_account_id,
                "symbol": (
                    raw_row.get("symbol")
                    or raw_row.get("underlying_symbol")
                    or raw_row.get("code")
                ),
                "side": raw_row.get("side")
                or raw_row.get("trd_side"),
                "stock_qty": (
                    raw_row.get("stock_qty")
                    or raw_row.get("dealt_qty")
                    or raw_row.get("qty")
                    or raw_row.get("quantity")
                ),
                "stock_price": raw_row.get("price")
                or raw_row.get("dealt_avg_price"),
                "trade_time_ms": _trade_time_ms(
                    raw_row.get("trade_time_ms")
                    or raw_row.get("event_time_ms")
                    or raw_row.get("create_time")
                    or raw_row.get("updated_time")
                ),
                "order_id": raw_row.get("order_id"),
                "clearing_date": (
                    raw_row.get("clearing_date")
                    or raw_row.get("settlement_date")
                ),
                "raw": dict(raw_row),
            }
    if len(matches) != 1:
        raise ValueError(
            "broker-ref must match exactly one persisted broker evidence row"
        )
    matched = next(iter(matches.values()))
    source_event_id = str(
        matched.get("source_event_id") or ""
    ).strip()
    if not source_event_id.startswith("futu:"):
        raise ValueError(
            "broker evidence lacks canonical Futu source identity"
        )
    return matched


def _trade_time_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        numeric = int(value)
        return (
            numeric
            if numeric > 10_000_000_000
            else numeric * 1000
        )
    raw = str(value).strip()
    if raw.isdigit():
        return _trade_time_ms(int(raw))
    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    return int(parsed.timestamp() * 1000)


def _list_trade_events(repo: Any) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    rows = candidate.list_trade_events()
    return [
        dict(item) for item in rows if isinstance(item, dict)
    ]


def _all_lifecycle_evidence(repo: Any) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    rows = candidate.list_trade_lifecycle_evidence()
    return [
        dict(item) for item in rows if isinstance(item, dict)
    ]


__all__ = [
    "MANUAL_REASONS",
    "MANUAL_RESOLUTION_SCHEMA",
    "resolve_lifecycle_manually",
]
