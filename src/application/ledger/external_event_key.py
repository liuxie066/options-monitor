from __future__ import annotations

from typing import Any, Iterable, Mapping

from domain.domain.trade_execution import (
    conflicting_execution_associations,
    execution_economic_content,
    execution_identity_from_input,
)


def broker_execution_identity(deal: Any) -> str:
    return execution_identity_from_input(getattr(deal, "execution_input", None))


def read_execution_event_candidates(repo: Any, execution_id: str) -> tuple[Any, Any]:
    from .repository_core import RepositoryCoreMixin

    candidate = getattr(repo, "primary_repo", repo)
    results = []
    for name in ("list_trade_events", "list_assigned_stock_events"):
        targeted = (
            getattr(candidate, f"{name}_for_execution", None)
            if isinstance(candidate, RepositoryCoreMixin) else None
        )
        if callable(targeted):
            results.append(targeted(execution_id))
        else:
            read = getattr(candidate, name, None)
            results.append(read() if callable(read) else None)
    return results[0], results[1]


def broker_deal_completion_payload(
    *,
    source_deal_id: str | None,
    expected_contracts: int,
    split_count: int,
    split_index: int,
    allocated_contracts: int,
) -> dict[str, Any]:
    return {
        "source_deal_id": source_deal_id or None,
        "expected_contracts": expected_contracts,
        "split_count": split_count,
        "split_index": split_index,
        "allocated_contracts": allocated_contracts,
    }


def require_same_execution(stored: dict[str, Any], incoming: dict[str, Any]) -> None:
    if execution_identity_from_input(stored) != execution_identity_from_input(incoming):
        raise ValueError("trade_execution_identity_conflict")
    before = execution_economic_content(stored)
    after = execution_economic_content(incoming)
    if before["errors"] or after["errors"]:
        raise ValueError("legacy_execution_evidence_required")
    if before["economic"] != after["economic"] or conflicting_execution_associations(before, after):
        raise ValueError("trade_execution_economic_conflict")


def applied_execution_association_conflicts(
    repo: Any,
    execution_id: str,
    content: Mapping[str, Any],
    *,
    applied_events: Iterable[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Compare source associations with the event allocation and durable order binding."""
    if not execution_id:
        return []
    if applied_events is None:
        rows: list[Mapping[str, Any]] = []
        for result in read_execution_event_candidates(repo, execution_id):
            if isinstance(result, Iterable):
                rows.extend(result)
    else:
        rows = list(applied_events)
    incoming = content.get("associations") or {}
    conflicts: set[str] = set()
    for event in rows:
        if not isinstance(event, Mapping):
            continue
        raw = event if event.get("stock_event_id") else event.get("raw_payload") or {}
        if not isinstance(raw, Mapping):
            continue
        execution = raw.get("execution_input") or {}
        stored_id = execution_identity_from_input(execution)
        if stored_id != execution_id and not (applied_events is not None and not stored_id):
            continue
        effect = str(event.get("event_type") or "").lower()
        if effect in {"close", "expire_close", "assignment", "exercise", "sale"}:
            effect = "close"
        if effect in {"open", "close"} and incoming.get("position_effect") not in (None, effect):
            conflicts.add("position_effect")
        associations = {
            "external_order_id": raw.get("order_id") or execution.get("external_order_id"),
            "external_order_namespace": raw.get("external_order_namespace") or execution.get("external_order_namespace"),
        }
        conflicts.update(conflicting_execution_associations(
            {"associations": associations}, content,
        ))
    return sorted(conflicts)


def ensure_execution_writer_guard(conn: Any) -> None:
    """Activated execution state is writable only by a writer that understands it."""
    for table in ("trade_events", "assigned_stock_events", "position_lots"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_execution_writer_v1_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN
                  SELECT CASE WHEN om_execution_writer_v1() != 1
                    THEN RAISE(ABORT, 'trade execution writer v1 required') END;
                END
            """)


def broker_external_event_key(deal: Any) -> str:
    execution = getattr(deal, "execution_input", None) or {}
    execution_id = execution_identity_from_input(execution)
    ref = execution.get("broker_account_ref") or {}
    legacy_futu = (
        ref.get("broker_id") == "futu"
        and ref.get("environment") == "REAL"
        and execution.get("external_id_namespace") == "futu.deal"
    )
    if execution_id and not legacy_futu:
        raw = getattr(deal, "raw_payload", None) or {}
        target = str(raw.get("target_lot_id") or raw.get("record_id") or "").strip()
        if target and getattr(deal, "position_effect", None) == "close":
            return f"{execution_id}:close:{target}"
        return execution_id
    deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    account = str(getattr(deal, "internal_account", "") or "").strip().lower()
    futu_account_id = str(getattr(deal, "futu_account_id", "") or "").strip()
    if deal_id and account and futu_account_id:
        return f"futu:{account}:{futu_account_id}:{deal_id}"
    return ""


def futu_compatibility_source_key(
    *,
    account: Any,
    futu_account_id: Any,
    source_deal_id: Any,
    execution_input: Any = None,
) -> str:
    """Keep Futu lifecycle reference grammar while isolating new ID namespaces."""
    account = str(account or "").strip().lower()
    physical = str(futu_account_id or "").strip()
    source_id = str(source_deal_id or "").strip()
    execution = execution_input if isinstance(execution_input, dict) else {}
    execution_id = execution_identity_from_input(execution)
    ref = execution.get("broker_account_ref") or {}
    if execution_id and not (
        ref.get("broker_id") == "futu"
        and ref.get("environment") == "REAL"
        and execution.get("external_id_namespace") == "futu.deal"
    ):
        source_id = execution_id
    if account and physical and source_id:
        return f"futu:{account}:{physical}:{source_id}"
    return ""
