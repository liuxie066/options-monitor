from __future__ import annotations

from .current_decision_assigned_stock import (
    _trade_event_contract,
    advance_assigned_stock_fact_for_trade_events,
    validate_assigned_stock_fact,
)

from .current_decision_common import (
    Any,
    CURRENT_DECISION_READ_SCHEMA,
    CurrentDecisionAccountFence,
    CurrentDecisionProjectionError,
    CurrentDecisionProjectionFence,
    Mapping,
    ProjectorImplementationUnavailable,
    SQLiteOptionPositionsRepository,
    Sequence,
    _integer,
    _position_lot_fields,
    _text,
    loaded_projector_implementation_fingerprint,
)

from .current_decision_payload import (
    _decode_projection_row_payload,
    _required_current_inputs,
    build_current_decision_projection,
    current_decision_projection_row,
)

from .current_decision_quality import (
    derive_lifecycle_quality_view,
    lifecycle_views_by_lot,
)

from .current_decision_runtime_support import (
    _decision_generations,
    _projection_bindings_clean,
    _projection_metadata_clean,
)

def capture_current_decision_projection_fence(
    repo: SQLiteOptionPositionsRepository,
    *,
    accounts: Sequence[str],
    conn: Any | None = None,
) -> CurrentDecisionProjectionFence:
    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_values = tuple(
        sorted({_text(value, field="account", lower=True) for value in accounts})
    )
    if not account_values:
        raise CurrentDecisionProjectionError("projection fence accounts are required")
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable as exc:
        raise CurrentDecisionProjectionError(
            "projector implementation is unavailable"
        ) from exc
    state = repo.read_current_decision_projection_fence_inputs(
        account_values,
        conn=conn,
    )
    source = state.get("source")
    if not isinstance(source, Mapping):
        raise CurrentDecisionProjectionError("position source state is missing")
    source_generation = _integer(
        source.get("source_generation"),
        field="position source generation",
    )
    account_states = state.get("accounts")
    if not isinstance(account_states, Mapping):
        raise CurrentDecisionProjectionError("projection fence state is invalid")
    captured: list[CurrentDecisionAccountFence] = []
    for account in account_values:
        raw = account_states.get(account)
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("projection fence account is missing")
        head = raw.get("head")
        generation = raw.get("generation")
        projection = raw.get("projection")
        lots_generation = (
            _integer(head.get("lots_generation"), field="lots_generation")
            if isinstance(head, Mapping)
            else 0
        )
        captured.append(
            CurrentDecisionAccountFence(
                account=account,
                position_lots_generation=lots_generation,
                decision_generations=_decision_generations(
                    generation if isinstance(generation, Mapping) else None
                ),
                projection_present=isinstance(projection, Mapping),
                clean_at_start=_projection_bindings_clean(
                    account=account,
                    source=source,
                    head=head if isinstance(head, Mapping) else None,
                    generation=(
                        generation if isinstance(generation, Mapping) else None
                    ),
                    projection=(
                        projection if isinstance(projection, Mapping) else None
                    ),
                    implementation_fingerprint=implementation,
                ),
            )
        )
    return CurrentDecisionProjectionFence(
        position_source_generation=source_generation,
        accounts=tuple(captured),
    )

def capture_trade_event_decision_projection_fence(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    account: str | None = None,
) -> CurrentDecisionProjectionFence | None:
    """Capture every existing account head before a global event mutation."""

    accounts = set(repo.list_position_projection_accounts(conn=conn))
    if account is not None:
        accounts.add(_text(account, field="account", lower=True))
    return (
        capture_current_decision_projection_fence(
            repo,
            accounts=tuple(accounts),
            conn=conn,
        )
        if accounts
        else None
    )

def read_current_assigned_stock_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    conn: Any,
) -> dict[str, Any]:
    state = repo.read_current_decision_storage_state(account, conn=conn)
    projection = state.get("projection")
    if not isinstance(projection, Mapping):
        raise CurrentDecisionProjectionError(
            "current decision projection is missing assigned-stock state"
        )
    return validate_assigned_stock_fact(
        _decode_projection_row_payload(projection)["assigned_stock"]
    )

def defer_current_decision_projection(
    fence: CurrentDecisionProjectionFence | None,
    *,
    reason: str = "explicit_rebuild_required",
) -> dict[str, Any] | None:
    if fence is None:
        return None
    reason_value = _text(reason, field="projection deferral reason", lower=True)
    return {
        "schema_version": "current_decision_projection_finalize.v1",
        "statuses": {
            item.account: (
                "not_initialized"
                if not item.projection_present
                else "preexisting_dirty"
                if not item.clean_at_start
                else reason_value
            )
            for item in fence.accounts
        },
        "published_accounts": [],
        "projection_dml_count": 0,
    }

def finalize_current_decision_projection(
    repo: SQLiteOptionPositionsRepository,
    *,
    fence: CurrentDecisionProjectionFence,
    updated_at_ms: int,
    conn: Any,
    case_mutations_by_account: Mapping[
        str,
        Sequence[tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]],
    ]
    | None = None,
    assigned_stock_after_by_account: Mapping[str, Mapping[str, Any]] | None = None,
    trade_event_mutations: Sequence[tuple[Any, bool]] = (),
) -> dict[str, Any]:
    """Publish once/account after all owning mutations; caller owns the transaction."""

    if conn is None:
        raise CurrentDecisionProjectionError("projection finalizer requires a transaction")
    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_fences = {item.account: item for item in fence.accounts}
    if not account_fences:
        raise CurrentDecisionProjectionError("projection fence is empty")
    mutations = dict(case_mutations_by_account or {})
    assigned_after = dict(assigned_stock_after_by_account or {})
    if (set(mutations) | set(assigned_after)) - set(account_fences):
        raise CurrentDecisionProjectionError("projection mutation account is outside fence")
    event_mutations_by_account: dict[str, list[tuple[Any, bool]]] = {}
    for event, created in trade_event_mutations:
        contract = _trade_event_contract(event)
        account = _text(contract.get("account"), field="event account", lower=True)
        if account in account_fences:
            event_mutations_by_account.setdefault(account, []).append(
                (event, bool(created))
            )
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable as exc:
        raise CurrentDecisionProjectionError(
            "projector implementation is unavailable"
        ) from exc
    final_state = repo.read_current_decision_projection_fence_inputs(
        sorted(account_fences),
        conn=conn,
    )
    source = final_state.get("source")
    if not isinstance(source, Mapping):
        raise CurrentDecisionProjectionError("final position source state is missing")
    final_source_generation = _integer(
        source.get("source_generation"),
        field="final position source generation",
    )
    global_change = final_source_generation != fence.position_source_generation
    raw_accounts = final_state.get("accounts")
    if not isinstance(raw_accounts, Mapping):
        raise CurrentDecisionProjectionError("final projection fence state is invalid")

    statuses: dict[str, str] = {}
    to_build: list[str] = []
    for account, begin in sorted(account_fences.items()):
        if not begin.projection_present:
            statuses[account] = "not_initialized"
            continue
        if not begin.clean_at_start:
            statuses[account] = "preexisting_dirty"
            continue
        final = raw_accounts.get(account)
        if not isinstance(final, Mapping):
            raise CurrentDecisionProjectionError("final projection account is missing")
        head = final.get("head")
        generation = final.get("generation")
        projection = final.get("projection")
        if not all(isinstance(value, Mapping) for value in (head, generation, projection)):
            raise CurrentDecisionProjectionError("clean projection disappeared")
        changed = (
            global_change
            or _integer(head.get("lots_generation"), field="lots_generation")
            != begin.position_lots_generation
            or _decision_generations(generation) != begin.decision_generations
        )
        if not changed:
            statuses[account] = "not_required"
            continue
        to_build.append(account)

    rows: dict[str, dict[str, Any]] = {}
    for account in to_build:
        inputs = repo.read_current_decision_projection_inputs(
            account,
            conn=conn,
            include_identities=False,
        )
        event_assigned_after = assigned_after.get(account)
        if event_assigned_after is None and event_mutations_by_account.get(account):
            projection = inputs.get("projection")
            if not isinstance(projection, Mapping):
                raise CurrentDecisionProjectionError(
                    "current decision projection disappeared"
                )
            event_assigned_after = advance_assigned_stock_fact_for_trade_events(
                _decode_projection_row_payload(projection)["assigned_stock"],
                event_mutations=event_mutations_by_account[account],
                current_position_lots=list(inputs.get("lots") or []),
            )
        projection = inputs.get("projection")
        if not isinstance(projection, Mapping):
            raise CurrentDecisionProjectionError(
                "current decision projection disappeared"
            )
        combo_assigned = validate_assigned_stock_fact(
            event_assigned_after
            if event_assigned_after is not None
            else _decode_projection_row_payload(projection)["assigned_stock"]
        )
        group_ids = {
            str(fields.get("strategy_group_id") or "").strip()
            for fields in _position_lot_fields(list(inputs.get("lots") or [])).values()
        } | {
            str(lot.get("strategy_group_id") or "").strip()
            for lot in combo_assigned["lots"]
        }
        inputs["identities"] = [
            identity
            for group_id in sorted(group_ids - {""})
            if (
                identity := repo.get_strategy_group_identity(group_id, conn=conn)
            )
            is not None
        ]
        payload = build_current_decision_projection(
            repo,
            account=account,
            updated_at_ms=updated_at_ms,
            conn=conn,
            current_inputs=inputs,
            case_mutations=mutations.get(account, ()),
            assigned_stock_after=event_assigned_after,
            implementation_fingerprint=implementation,
        )
        rows[account] = current_decision_projection_row(payload)

    for account in to_build:
        repo.upsert_current_decision_projection(rows[account], conn=conn)
        statuses[account] = "published"
    return {
        "schema_version": "current_decision_projection_finalize.v1",
        "statuses": statuses,
        "published_accounts": to_build,
        "projection_dml_count": len(to_build),
    }

def _decision_read_unavailable(
    account: str,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_DECISION_READ_SCHEMA,
        "status": status,
        "account": account,
        "reason": reason,
        "payload": None,
        "position_lots": [],
    }

def read_current_decision_projection(
    repo: Any,
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    account_value = _text(account, field="account", lower=True)
    instant = _integer(now_ms, field="now_ms", minimum=1)
    if not callable(getattr(repo, "read_current_decision_projection_inputs", None)):
        return _decision_read_unavailable(
            account_value,
            status="absent",
            reason="sqlite_repository_required",
        )
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason="projector_implementation_unavailable",
        )
    conn = repo._connect()
    try:
        conn.execute("BEGIN")
        inputs = repo.read_current_decision_projection_inputs(
            account_value,
            conn=conn,
            include_identities=False,
        )
        projection = inputs.get("projection")
        if projection is None:
            return _decision_read_unavailable(
                account_value,
                status="absent",
                reason="decision_projection_missing",
            )
        if not isinstance(projection, Mapping):
            raise CurrentDecisionProjectionError("decision projection row is invalid")
        source, head, generation, lots = _required_current_inputs(
            account=account_value,
            current_inputs=inputs,
            implementation_fingerprint=implementation,
        )
        if not _projection_metadata_clean(
            account=account_value,
            source=source,
            head=head,
            generation=generation,
            projection=projection,
            implementation_fingerprint=implementation,
        ):
            raise CurrentDecisionProjectionError("decision projection is dirty")
        payload = _decode_projection_row_payload(projection)
        lot_views, case_views = lifecycle_views_by_lot(
            payload["lifecycle"],
            current_position_lots=lots,
            now_ms=instant,
        )
        quality = derive_lifecycle_quality_view(
            payload["lifecycle_quality"],
            now_ms=instant,
        )
        for item in quality["operational_cases"]:
            item["reason_state"] = case_views[str(item["case_id"])]["reason_state"]
        return {
            "schema_version": CURRENT_DECISION_READ_SCHEMA,
            "status": "trusted",
            "account": account_value,
            "reason": None,
            "payload": payload,
            "position_lots": lots,
            "lot_count": len(lots),
            "lifecycle_by_lot": lot_views,
            "lifecycle_by_case": case_views,
            "lifecycle_quality": quality,
        }
    except CurrentDecisionProjectionError as exc:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason=str(exc),
        )
    except Exception:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason="current_decision_read_failed",
        )
    finally:
        conn.rollback()
        conn.close()

def verify_current_decision_projection(
    repo: Any,
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    result = read_current_decision_projection(
        repo,
        account=account,
        now_ms=now_ms,
    )
    valid = result["status"] == "trusted"
    return {
        "schema_version": "current_decision_projection_verification.v1",
        "account": result["account"],
        "status": "valid" if valid else result["status"],
        "mismatch_count": 0 if valid else 1,
        "mismatch_samples": []
        if valid
        else [{"reason": result["reason"]}],
    }

__all__ = [
    "CURRENT_ASSIGNED_STOCK_SCHEMA",
    "CURRENT_COMBO_GROUP_FACT_SCHEMA",
    "CURRENT_COMBO_SCHEMA",
    "CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA",
    "CURRENT_DECISION_PROJECTION_SCHEMA",
    "CURRENT_DECISION_READ_SCHEMA",
    "CURRENT_LIFECYCLE_QUALITY_SCHEMA",
    "LIFECYCLE_CASE_DECISION_FACT_SCHEMA",
    "CurrentDecisionAccountFence",
    "CurrentDecisionProjectionError",
    "CurrentDecisionProjectionFence",
    "advance_assigned_stock_fact_for_trade_events",
    "advance_lifecycle_case_decision_fact",
    "apply_current_decision_projection_migration",
    "build_initial_lifecycle_case_decision_fact",
    "build_current_decision_projection_migration_inventory",
    "build_current_combo_facts",
    "build_current_decision_projection",
    "build_current_decision_projection_payload",
    "build_lifecycle_case_decision_fact",
    "build_lifecycle_quality_fact",
    "capture_current_decision_projection_fence",
    "capture_trade_event_decision_projection_fence",
    "compact_assigned_stock_view",
    "current_decision_projection_row",
    "current_decision_projection_migration_status",
    "empty_assigned_stock_fact",
    "encode_current_decision_projection",
    "encode_lifecycle_case_decision_fact",
    "finalize_current_decision_projection",
    "preview_current_decision_projection_oracle",
    "defer_current_decision_projection",
    "read_current_assigned_stock_fact",
    "read_current_decision_projection",
    "read_lifecycle_case_decision_fact",
    "update_assigned_stock_fact",
    "validate_assigned_stock_fact",
    "validate_current_combo_facts",
    "validate_current_decision_projection_payload",
    "validate_lifecycle_case_decision_fact",
    "validate_lifecycle_quality_fact",
    "verify_current_decision_projection",
    "verify_current_decision_projection_migration",
    "write_lifecycle_case_decision_fact",
]
