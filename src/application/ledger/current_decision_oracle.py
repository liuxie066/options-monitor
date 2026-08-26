from __future__ import annotations

from .current_decision_assigned_stock import (
    compact_assigned_stock_view,
)

from .current_decision_common import (
    Any,
    CurrentDecisionProjectionError,
    Mapping,
    SQLiteOptionPositionsRepository,
    _GENERATION_FIELDS,
    _canonical_json_bytes,
    _integer,
    _sha256_bytes,
    _text,
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    derive_lifecycle_read_model,
    project_assigned_stock_lifecycle,
    resolve_lifecycle_account_rows,
    symbol_market,
)

from .current_decision_lifecycle import (
    build_lifecycle_case_decision_fact,
)

from .current_decision_payload import (
    build_current_decision_projection_payload,
)

from .current_decision_quality import (
    build_lifecycle_quality_fact,
)

def _oracle_assigned_stock_report(
    rows: Mapping[str, Any],
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    from src.application.ledger.event_codec import import_stored_trade_events
    from src.application.ledger.event_codec import valid_void_target_event_id
    from src.application.ledger.publisher import (
        project_stored_trade_events_to_position_lots,
    )

    event_rows = [
        dict(item)
        for item in rows.get("trade_events") or []
        if int(item.get("event_time_ms") or item.get("trade_time_ms") or 0)
        <= now_ms
        or valid_void_target_event_id(item) is not None
    ]
    events, diagnostics = import_stored_trade_events(event_rows)
    projected = project_stored_trade_events_to_position_lots(event_rows)
    del diagnostics
    current_fields_by_lot_id = {
        item.record_id: item.fields for item in projected.lots
    }
    return project_assigned_stock_lifecycle(
        [assigned_stock_trade_event_row(event) for event in events],
        assignment_option_rows=[
            assigned_stock_allocation_row(item)
            for item in projected.ledger_projection.allocations
        ],
        option_open_lots=[
            assigned_stock_position_lot_row(
                item,
                current_fields=current_fields_by_lot_id.get(item.lot_id),
                at_ms=now_ms,
            )
            for item in projected.ledger_projection.lots
        ],
        assigned_stock_events=[
            dict(item)
            for item in rows.get("account_assigned_stock_events") or []
            if isinstance(item, Mapping)
            and assigned_stock_event_time_ms(item) <= now_ms
        ],
        quote_snapshots=[],
        stock_holdings=None,
        account_norm=account,
        broker_norm=None,
        month=None,
        as_of_ms=now_ms,
    )

def _oracle_lifecycle_case_facts(
    rows: Mapping[str, Any],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    resolution = resolve_lifecycle_account_rows(rows)
    case_ids = [
        str(item.get("case_id") or "")
        for item in resolution.get("generation_tokens") or []
        if isinstance(item, Mapping)
    ]
    from src.application.ledger.queries import (
        lifecycle_case_coherent_facts_many_from_account_snapshot,
    )

    materialized = lifecycle_case_coherent_facts_many_from_account_snapshot(
        {**rows, "account_lifecycle_resolution": resolution},
        case_ids=case_ids,
    )
    revisions = rows.get("account_lifecycle_evidence_revisions")
    admissions = rows.get("account_lifecycle_settlement_admission_heads")
    if not isinstance(revisions, Mapping) or not isinstance(admissions, Mapping):
        raise CurrentDecisionProjectionError(
            "oracle lifecycle revision facts are unavailable"
        )
    case_facts: list[dict[str, Any]] = []
    for case_id in case_ids:
        facts = materialized[case_id]
        case_evidence = list(facts["case_evidence"])
        revision = revisions.get(case_id)
        if revision is None:
            revision_value, evidence_count = 0, len(case_evidence)
        elif isinstance(revision, Mapping):
            revision_value = _integer(
                revision.get("revision"), field="evidence revision"
            )
            raw_count = revision.get("evidence_count")
            evidence_count = (
                len(case_evidence)
                if raw_count is None
                else _integer(raw_count, field="evidence count")
            )
        else:
            raise CurrentDecisionProjectionError(
                "oracle lifecycle revision fact is invalid"
            )
        if evidence_count != len(case_evidence):
            raise CurrentDecisionProjectionError(
                "oracle lifecycle evidence count mismatch"
            )
        lifecycle_case = dict(facts["lifecycle_case"])
        case_resolution = dict(facts["case_resolution"])
        timing_policy = (
            dict(facts["timing_policy"])
            if isinstance(facts.get("timing_policy"), Mapping)
            else None
        )
        try:
            compact_model = derive_lifecycle_read_model(
                expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
                market=str(
                    lifecycle_case.get("market")
                    or symbol_market(lifecycle_case.get("symbol"))
                    or ""
                ),
                target_contracts_by_lot=dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                ),
                allocations=list(facts["case_allocations"]),
                void_event_ids=list(facts["effective_void_event_ids"]),
                accepted_option_close_contracts_by_lot=dict(
                    case_resolution.get("effective_reservations_by_lot") or {}
                ),
                now_ms=now_ms,
                observation_start_ms_override=(
                    int(lifecycle_case["observation_start_ms"])
                    if lifecycle_case.get("observation_start_ms") is not None
                    else None
                ),
                pending_until_ms_override=(
                    int(timing_policy["settlement_deadline_ms"])
                    if timing_policy is not None
                    and timing_policy.get("settlement_deadline_ms") is not None
                    else None
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise CurrentDecisionProjectionError(
                "oracle lifecycle read model is invalid"
            ) from exc
        read_model = {
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": compact_model.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": compact_model.remaining_contracts_by_lot,
            "resolved_contracts_by_terminal_type": (
                compact_model.resolved_contracts_by_terminal_type
            ),
            "observation_start_ms": compact_model.observation_start_ms,
            "pending_until_ms": compact_model.pending_until_ms,
            "timing_policy_hash": (
                _sha256_bytes(_canonical_json_bytes(timing_policy))
                if timing_policy is not None
                else None
            ),
        }
        admission = admissions.get(case_id)
        if admission is not None and not isinstance(admission, Mapping):
            raise CurrentDecisionProjectionError(
                "oracle lifecycle admission fact is invalid"
            )
        case_facts.append(
            build_lifecycle_case_decision_fact(
                lifecycle_case=lifecycle_case,
                case_resolution=case_resolution,
                generation_token=dict(facts["generation_token"]),
                read_model=read_model,
                evidence_revision=revision_value,
                evidence_count=evidence_count,
                admission_head=(dict(admission) if admission is not None else None),
            )
        )
    return case_facts

def _current_decision_projection_oracle(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    now_ms: int,
    assigned_stock_report: Mapping[str, Any] | None,
    conn: Any | None = None,
    allow_schema_cookie_mismatch: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build explicit O(history) migration facts without publishing them."""

    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_value = _text(account, field="account", lower=True)
    instant = _integer(now_ms, field="now_ms", minimum=1)
    owned = conn is None
    active_conn = conn or repo._connect()
    try:
        if owned:
            active_conn.execute("BEGIN")
        rows = repo.read_lifecycle_account_rows(
            account=account_value,
            conn=active_conn,
        )
        current_inputs = repo.read_current_decision_projection_inputs(
            account_value,
            conn=active_conn,
        )
    finally:
        if owned:
            active_conn.rollback()
            active_conn.close()
    if allow_schema_cookie_mismatch and isinstance(
        current_inputs.get("source"), Mapping
    ):
        current_inputs["schema_cookie"] = current_inputs["source"].get(
            "sqlite_schema_cookie"
        )
    if current_inputs.get("generation") is None:
        current_inputs["generation"] = {
            "account": account_value,
            **{field: 0 for field in _GENERATION_FIELDS},
            "updated_at_ms": instant,
        }

    case_facts = _oracle_lifecycle_case_facts(rows, now_ms=instant)

    assigned = compact_assigned_stock_view(
        assigned_stock_report
        if assigned_stock_report is not None
        else _oracle_assigned_stock_report(
            rows,
            account=account_value,
            now_ms=instant,
        ),
        account=account_value,
        current_position_lots=list(current_inputs.get("lots") or []),
    )
    quality = build_lifecycle_quality_fact(
        account=account_value,
        all_case_facts=case_facts,
        operational_case_facts=case_facts,
    )
    return (
        build_current_decision_projection_payload(
            account=account_value,
            current_inputs=current_inputs,
            case_facts=case_facts,
            assigned_stock=assigned,
            lifecycle_quality=quality,
            updated_at_ms=instant,
        ),
        case_facts,
    )

def preview_current_decision_projection_oracle(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    now_ms: int,
    assigned_stock_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the explicit O(history) comparison payload without publishing it."""

    return _current_decision_projection_oracle(
        repo,
        account=account,
        now_ms=now_ms,
        assigned_stock_report=assigned_stock_report,
    )[0]

_DECISION_MIGRATION_REQUIRED_TABLES = (
    "trade_events",
    "position_lots",
    "position_projection_source_state",
    "position_projection_heads",
    "assigned_stock_events",
    "trade_lifecycle_cases",
    "trade_lifecycle_evidence",
    "trade_lifecycle_evidence_revisions",
    "trade_lifecycle_settlement_admission_heads",
    "trade_lifecycle_allocations",
    "trade_lifecycle_source_consumptions",
    "trade_lifecycle_timing_policies",
    "strategy_group_identities",
    "current_decision_input_generations",
    "current_decision_projections",
    "trade_lifecycle_case_targets",
)

_DECISION_MIGRATION_REQUIRED_INDEXES = (
    "idx_position_lots_account_record",
    "idx_assigned_stock_events_account_time",
    "idx_trade_lifecycle_cases_account_status",
    "idx_trade_lifecycle_case_targets_account_lot",
    "idx_strategy_group_identities_account",
)

_DECISION_MIGRATION_REQUIRED_TRIGGERS = (
    "trg_current_decision_assigned_stock_account_insert_guard",
    "trg_current_decision_assigned_stock_account_update_guard",
    "trg_current_decision_assigned_stock_account_delete_guard",
    "trg_current_decision_lifecycle_case_fact_insert_guard",
    "trg_current_decision_lifecycle_case_fact_update_guard",
    "trg_current_decision_case_target_guard",
    "trg_current_decision_case_target_update_guard",
    *(
        f"trg_current_decision_{label}_{operation}"
        for label in (
            "lifecycle_case",
            "lifecycle_evidence",
            "lifecycle_allocation",
            "lifecycle_source_consumption",
            "lifecycle_timing",
            "combo_identity",
            "assigned_stock",
        )
        for operation in ("insert", "update", "delete")
    ),
)

_DECISION_MIGRATION_AUTHORITY_QUERIES = (
    (
        "trade_events",
        "SELECT event_id,account,event_json,trade_time_ms,created_at_ms,updated_at_ms "
        "FROM trade_events ORDER BY trade_time_ms,event_id",
    ),
    (
        "position_lots",
        "SELECT record_id,account,fields_json,source_event_id,expiration,strike,"
        "multiplier,updated_at_ms FROM position_lots ORDER BY record_id",
    ),
    (
        "position_projection_source_state",
        "SELECT singleton_id,source_generation,projector_schema,"
        "projector_implementation_fingerprint,checkpoint_mode,"
        "last_full_verified_source_generation FROM position_projection_source_state "
        "ORDER BY singleton_id",
    ),
    (
        "position_projection_heads",
        "SELECT account,lots_generation,built_source_generation,built_lots_generation,"
        "projection_fingerprint,lot_count,projector_schema,"
        "projector_implementation_fingerprint,status FROM position_projection_heads "
        "ORDER BY account",
    ),
    (
        "assigned_stock_events",
        "SELECT stock_event_id,event_json,trade_time_ms,created_at_ms,updated_at_ms "
        "FROM assigned_stock_events ORDER BY trade_time_ms,stock_event_id",
    ),
    (
        "trade_lifecycle_cases",
        "SELECT case_id,case_key,account,broker,symbol,option_type,position_side,"
        "strike,expiration_ymd,contract_key,status,decision_type,target_lot_ids_json,"
        "target_contracts_by_lot_json,observation_start_ms,pending_until_ms,"
        "created_at_ms,updated_at_ms,raw_json FROM trade_lifecycle_cases "
        "ORDER BY case_id",
    ),
    (
        "trade_lifecycle_evidence",
        "SELECT evidence_id,case_id,source_type,source_event_id,evidence_type,account,"
        "symbol,raw_json,created_at_ms FROM trade_lifecycle_evidence "
        "ORDER BY created_at_ms,evidence_id",
    ),
    (
        "trade_lifecycle_evidence_revisions",
        "SELECT lifecycle_case.case_id,coalesce(revision.revision,0) AS revision "
        "FROM trade_lifecycle_cases AS lifecycle_case "
        "LEFT JOIN trade_lifecycle_evidence_revisions AS revision "
        "ON revision.case_id=lifecycle_case.case_id ORDER BY lifecycle_case.case_id",
    ),
    (
        "trade_lifecycle_settlement_admission_heads",
        "SELECT * FROM trade_lifecycle_settlement_admission_heads ORDER BY case_id",
    ),
    (
        "trade_lifecycle_allocations",
        "SELECT * FROM trade_lifecycle_allocations ORDER BY allocation_id",
    ),
    (
        "trade_lifecycle_source_consumptions",
        "SELECT * FROM trade_lifecycle_source_consumptions ORDER BY source_key",
    ),
    (
        "trade_lifecycle_timing_policies",
        "SELECT * FROM trade_lifecycle_timing_policies ORDER BY case_id",
    ),
    (
        "strategy_group_identities",
        "SELECT * FROM strategy_group_identities ORDER BY group_id",
    ),
)
