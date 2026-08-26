from __future__ import annotations

from .current_decision_assigned_stock import (
    validate_assigned_stock_fact,
)

from .current_decision_combo import (
    build_current_combo_facts,
    validate_current_combo_facts,
)

from .current_decision_common import (
    Any,
    CURRENT_DECISION_PROJECTION_SCHEMA,
    CurrentDecisionProjectionError,
    Mapping,
    POSITION_PROJECTION_SCHEMA,
    ProjectorImplementationUnavailable,
    SQLiteOptionPositionsRepository,
    Sequence,
    _GENERATION_FIELDS,
    _OPERATIONAL_STATUSES,
    _canonical_json_bytes,
    _integer,
    _position_lot_fields,
    _sha256,
    _sha256_bytes,
    _text,
    canonical_sha256,
    json,
    loaded_projector_implementation_fingerprint,
)

from .current_decision_lifecycle import (
    validate_lifecycle_case_decision_fact,
)

from .current_decision_quality import (
    _POSITION_BINDING_KEYS,
    _PROJECTION_PAYLOAD_KEYS,
    _SOURCE_BINDING_KEYS,
    arbitrate_lifecycle_case_facts,
    build_lifecycle_quality_fact,
    update_lifecycle_quality_fact,
    validate_current_lifecycle_facts,
    validate_lifecycle_quality_fact,
)

def _decision_state_fingerprint(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"decision_state_fingerprint", "updated_at_ms"}
        }
    )

def validate_current_decision_projection_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _PROJECTION_PAYLOAD_KEYS:
        raise CurrentDecisionProjectionError("current decision payload shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_DECISION_PROJECTION_SCHEMA:
        raise CurrentDecisionProjectionError("current decision schema is invalid")
    account = _text(item["normalized_account"], field="account", lower=True)
    implementation = _sha256(
        item["projector_implementation_fingerprint"],
        field="projector implementation fingerprint",
    )

    binding = item["position_binding"]
    if not isinstance(binding, Mapping) or set(binding) != _POSITION_BINDING_KEYS:
        raise CurrentDecisionProjectionError("position binding shape is invalid")
    if binding["projector_schema"] != POSITION_PROJECTION_SCHEMA:
        raise CurrentDecisionProjectionError("position binding schema is invalid")
    if (
        _sha256(
            binding["projector_implementation_fingerprint"],
            field="position implementation fingerprint",
        )
        != implementation
    ):
        raise CurrentDecisionProjectionError("position implementation mismatch")
    for field in (
        "position_source_generation",
        "position_lots_generation",
        "lot_count",
        "active_lot_count",
    ):
        _integer(binding[field], field=field)
    if int(binding["active_lot_count"]) > int(binding["lot_count"]):
        raise CurrentDecisionProjectionError("active lot count exceeds lot count")
    _sha256(
        binding["position_lots_fingerprint"],
        field="position lots fingerprint",
    )

    sources = item["source_bindings"]
    if not isinstance(sources, Mapping) or set(sources) != _SOURCE_BINDING_KEYS:
        raise CurrentDecisionProjectionError("decision source binding shape is invalid")
    for field in _GENERATION_FIELDS:
        _integer(sources[field], field=f"source_bindings.{field}")

    lifecycle = validate_current_lifecycle_facts(item["lifecycle"])
    combo = validate_current_combo_facts(item["combo"])
    assigned = validate_assigned_stock_fact(item["assigned_stock"])
    quality = validate_lifecycle_quality_fact(item["lifecycle_quality"])
    if lifecycle["account"] != account or assigned["account"] != account:
        raise CurrentDecisionProjectionError("current decision account mismatch")
    if quality["account"] != account or any(
        group["account"] != account for group in combo["current_groups"]
    ):
        raise CurrentDecisionProjectionError("current decision nested account mismatch")
    _integer(item["updated_at_ms"], field="updated_at_ms", minimum=1)
    if (
        _sha256(
            item["decision_state_fingerprint"],
            field="decision_state_fingerprint",
        )
        != _decision_state_fingerprint(item)
    ):
        raise CurrentDecisionProjectionError("decision state fingerprint mismatch")
    return item

def encode_current_decision_projection(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    item = validate_current_decision_projection_payload(payload)
    payload_bytes = _canonical_json_bytes(item)
    return payload_bytes.decode("utf-8"), _sha256_bytes(payload_bytes)

def current_decision_projection_row(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    item = validate_current_decision_projection_payload(payload)
    payload_json, payload_sha256 = encode_current_decision_projection(item)
    binding = item["position_binding"]
    sources = item["source_bindings"]
    return {
        "account": item["normalized_account"],
        "projection_schema": item["schema_version"],
        "projector_implementation_fingerprint": item[
            "projector_implementation_fingerprint"
        ],
        "built_position_source_generation": binding[
            "position_source_generation"
        ],
        "built_position_lots_generation": binding["position_lots_generation"],
        "position_lots_fingerprint": binding["position_lots_fingerprint"],
        "built_decision_input_generation": sources["generation"],
        "built_case_generation": sources["case_generation"],
        "built_evidence_generation": sources["evidence_generation"],
        "built_allocation_generation": sources["allocation_generation"],
        "built_source_consumption_generation": sources[
            "source_consumption_generation"
        ],
        "built_timing_generation": sources["timing_generation"],
        "built_combo_identity_generation": sources[
            "combo_identity_generation"
        ],
        "built_assigned_stock_generation": sources[
            "assigned_stock_generation"
        ],
        "decision_state_fingerprint": item["decision_state_fingerprint"],
        "payload_sha256": payload_sha256,
        "payload_json": payload_json,
        "updated_at_ms": item["updated_at_ms"],
    }

def encode_lifecycle_case_decision_fact(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    item = validate_lifecycle_case_decision_fact(payload)
    return _canonical_json_bytes(item).decode("utf-8"), str(item["fact_sha256"])

def read_lifecycle_case_decision_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    case_id: str,
    conn: Any,
) -> dict[str, Any] | None:
    row = repo.get_current_decision_lifecycle_fact_state(case_id, conn=conn)
    if row is None or row.get("decision_fact_json") is None:
        return None
    return _stored_case_fact(row, account=str(row["account"]))

def write_lifecycle_case_decision_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    fact: Mapping[str, Any],
    conn: Any,
) -> bool:
    item = validate_lifecycle_case_decision_fact(fact)
    fact_json, fact_hash = encode_lifecycle_case_decision_fact(item)
    return repo.update_trade_lifecycle_case_decision_fact(
        case_id=str(item["case_id"]),
        account=str(item["account"]),
        status=str(item["status"]),
        decision_fact_json=fact_json,
        decision_fact_sha256=fact_hash,
        conn=conn,
    )

def _decode_projection_row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(row)
    payload_json = stored.get("payload_json")
    if not isinstance(payload_json, str):
        raise CurrentDecisionProjectionError("stored decision payload must be text")
    try:
        decoded = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError("stored decision payload is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise CurrentDecisionProjectionError("stored decision payload must be an object")
    payload = validate_current_decision_projection_payload(decoded)
    canonical_json, payload_sha256 = encode_current_decision_projection(payload)
    if canonical_json != payload_json or stored.get("payload_sha256") != payload_sha256:
        raise CurrentDecisionProjectionError("stored decision payload bytes mismatch")
    expected = current_decision_projection_row(payload)
    for field, value in expected.items():
        if field == "payload_json":
            continue
        if stored.get(field) != value:
            raise CurrentDecisionProjectionError(
                f"stored decision projection field mismatch: {field}"
            )
    return payload

def _required_current_inputs(
    *,
    account: str,
    current_inputs: Mapping[str, Any],
    implementation_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    account_value = _text(account, field="account", lower=True)
    inputs = dict(current_inputs)
    source = inputs.get("source")
    head = inputs.get("head")
    generation = inputs.get("generation")
    lots = inputs.get("lots")
    if not isinstance(source, Mapping) or not isinstance(head, Mapping):
        raise CurrentDecisionProjectionError("trusted position projection is missing")
    if not isinstance(generation, Mapping):
        raise CurrentDecisionProjectionError("decision input generation is missing")
    if not isinstance(lots, list) or any(not isinstance(item, Mapping) for item in lots):
        raise CurrentDecisionProjectionError("current position lots are invalid")
    source_row, head_row, generation_row = dict(source), dict(head), dict(generation)
    implementation = _sha256(
        implementation_fingerprint,
        field="projector implementation fingerprint",
    )
    checks = (
        source_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
        head_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
        source_row.get("projector_implementation_fingerprint") == implementation,
        head_row.get("projector_implementation_fingerprint") == implementation,
        head_row.get("status") == "trusted",
        head_row.get("built_source_generation") == source_row.get("source_generation"),
        head_row.get("built_lots_generation") == head_row.get("lots_generation"),
        source_row.get("sqlite_schema_cookie") == inputs.get("schema_cookie"),
        head_row.get("projection_fingerprint") == inputs.get("lots_fingerprint"),
        head_row.get("lot_count") == inputs.get("lot_count"),
        generation_row.get("account") == account_value,
    )
    if not all(checks):
        raise CurrentDecisionProjectionError("current projection inputs are not trusted")
    for field in (
        "source_generation",
        "lots_generation",
        "built_source_generation",
        "built_lots_generation",
        "lot_count",
    ):
        row = source_row if field == "source_generation" else head_row
        _integer(row.get(field), field=field)
    for field in _GENERATION_FIELDS:
        _integer(generation_row.get(field), field=field)
    _sha256(inputs.get("lots_fingerprint"), field="position lots fingerprint")
    return source_row, head_row, generation_row, [dict(item) for item in lots]

def _active_lot_ids(current_position_lots: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        record_id
        for record_id, fields in _position_lot_fields(current_position_lots).items()
        if str(fields.get("status") or "").strip().lower() == "open"
        and int(fields.get("contracts_open") or 0) > 0
    }

def _referenced_case_fact(
    fact: Mapping[str, Any],
    *,
    referenced_lot_ids: set[str],
) -> bool:
    return (
        str(fact["status"]) in _OPERATIONAL_STATUSES
        or bool(set(fact["target_contracts_by_lot"]) & referenced_lot_ids)
        or bool(fact["resolution"]["requested_reservations_by_lot"])
        or bool(fact["resolution"]["effective_reservations_by_lot"])
    )

def build_current_decision_projection_payload(
    *,
    account: str,
    current_inputs: Mapping[str, Any],
    case_facts: Sequence[Mapping[str, Any]],
    assigned_stock: Mapping[str, Any],
    lifecycle_quality: Mapping[str, Any],
    updated_at_ms: int,
    implementation_fingerprint: str | None = None,
) -> dict[str, Any]:
    account_value = _text(account, field="account", lower=True)
    implementation = implementation_fingerprint
    if implementation is None:
        try:
            implementation = loaded_projector_implementation_fingerprint()
        except ProjectorImplementationUnavailable as exc:
            raise CurrentDecisionProjectionError(
                "projector implementation is unavailable"
            ) from exc
    source, head, generation, lots = _required_current_inputs(
        account=account_value,
        current_inputs=current_inputs,
        implementation_fingerprint=str(implementation),
    )
    assigned = validate_assigned_stock_fact(assigned_stock)
    if assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("assigned stock account mismatch")
    active_lot_ids = _active_lot_ids(lots)
    referenced_lot_ids = {
        *active_lot_ids,
        *(
            str(item["source_option_lot_id"])
            for item in assigned["lots"]
            if item["source_option_lot_id"] is not None
        ),
    }
    validated_case_facts = [
        validate_lifecycle_case_decision_fact(item) for item in case_facts
    ]
    selected = [
        item
        for item in validated_case_facts
        if _referenced_case_fact(item, referenced_lot_ids=referenced_lot_ids)
    ]
    lifecycle = arbitrate_lifecycle_case_facts(
        account=account_value,
        case_facts=selected,
    )
    quality = update_lifecycle_quality_fact(
        lifecycle_quality,
        case_mutations=(),
        operational_case_facts=lifecycle["operational_cases"],
    )
    combo = build_current_combo_facts(
        account=account_value,
        current_position_lots=lots,
        identities=list(current_inputs.get("identities") or []),
        assigned_stock=assigned,
    )
    payload = {
        "schema_version": CURRENT_DECISION_PROJECTION_SCHEMA,
        "normalized_account": account_value,
        "projector_implementation_fingerprint": str(implementation),
        "position_binding": {
            "projector_schema": POSITION_PROJECTION_SCHEMA,
            "projector_implementation_fingerprint": str(implementation),
            "position_source_generation": int(source["source_generation"]),
            "position_lots_generation": int(head["lots_generation"]),
            "position_lots_fingerprint": str(current_inputs["lots_fingerprint"]),
            "lot_count": int(current_inputs["lot_count"]),
            "active_lot_count": len(active_lot_ids),
        },
        "source_bindings": {
            field: int(generation[field]) for field in _GENERATION_FIELDS
        },
        "lifecycle": lifecycle,
        "combo": combo,
        "assigned_stock": assigned,
        "lifecycle_quality": quality,
        "updated_at_ms": _integer(
            updated_at_ms,
            field="updated_at_ms",
            minimum=1,
        ),
    }
    payload["decision_state_fingerprint"] = _decision_state_fingerprint(payload)
    return validate_current_decision_projection_payload(payload)

def _stored_case_fact(row: Mapping[str, Any], *, account: str) -> dict[str, Any]:
    stored = dict(row)
    raw_json = stored.get("decision_fact_json")
    raw_hash = stored.get("decision_fact_sha256")
    if not isinstance(raw_json, str) or not isinstance(raw_hash, str):
        raise CurrentDecisionProjectionError("lifecycle decision fact is missing")
    try:
        decoded = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError("lifecycle decision fact JSON is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise CurrentDecisionProjectionError("lifecycle decision fact must be an object")
    fact = validate_lifecycle_case_decision_fact(decoded)
    canonical_json, fact_hash = encode_lifecycle_case_decision_fact(fact)
    if canonical_json != raw_json or fact_hash != raw_hash:
        raise CurrentDecisionProjectionError("lifecycle decision fact bytes mismatch")
    if (
        fact["case_id"] != stored.get("case_id")
        or fact["account"] != account
        or fact["account"] != stored.get("account")
        or fact["status"] != stored.get("status")
    ):
        raise CurrentDecisionProjectionError("lifecycle decision fact row mismatch")
    revision = stored.get("evidence_revision")
    count = stored.get("evidence_count")
    if revision is None and count is None:
        revision, count = 0, 0
    if fact["evidence"]["revision"] != revision or fact["evidence"]["count"] != count:
        raise CurrentDecisionProjectionError("lifecycle evidence revision mismatch")
    admission = (
        stored.get("admitted_semantic_schema"),
        stored.get("admitted_semantic_fingerprint"),
        stored.get("admitted_evidence_id"),
    )
    embedded = (
        fact["evidence"]["admitted_semantic_schema"],
        fact["evidence"]["admitted_semantic_fingerprint"],
        fact["evidence"]["admitted_evidence_id"],
    )
    if admission != embedded:
        raise CurrentDecisionProjectionError("lifecycle admission binding mismatch")
    return fact

def _prior_referenced_lot_ids(payload: Mapping[str, Any] | None) -> set[str]:
    if payload is None:
        return set()
    item = validate_current_decision_projection_payload(payload)
    return {
        *(
            lot_id
            for fact in item["lifecycle"]["operational_cases"]
            for lot_id in fact["target_contracts_by_lot"]
        ),
        *(
            str(lot["source_option_lot_id"])
            for lot in item["assigned_stock"]["lots"]
            if lot["source_option_lot_id"] is not None
        ),
        *(
            str(binding["record_id"])
            for group in item["combo"]["current_groups"]
            for binding in group["active_member_bindings"]
        ),
    }

def build_current_decision_projection(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    updated_at_ms: int,
    conn: Any | None = None,
    current_inputs: Mapping[str, Any] | None = None,
    case_mutations: Sequence[
        tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]
    ] = (),
    assigned_stock_after: Mapping[str, Any] | None = None,
    all_quality_case_facts: Sequence[Mapping[str, Any]] | None = None,
    implementation_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build from bounded current rows; lifetime readers are intentionally absent."""

    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_value = _text(account, field="account", lower=True)
    inputs = dict(
        current_inputs
        if current_inputs is not None
        else repo.read_current_decision_projection_inputs(account_value, conn=conn)
    )
    prior_payload = (
        _decode_projection_row_payload(inputs["projection"])
        if isinstance(inputs.get("projection"), Mapping)
        else None
    )
    assigned = (
        validate_assigned_stock_fact(assigned_stock_after)
        if assigned_stock_after is not None
        else (
            validate_assigned_stock_fact(prior_payload["assigned_stock"])
            if prior_payload is not None
            else None
        )
    )
    if assigned is None or assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("trusted assigned-stock fact is required")

    mutation_rows: list[
        tuple[dict[str, Any] | None, dict[str, Any] | None]
    ] = []
    target_ids = {
        *_position_lot_fields(list(inputs.get("lots") or [])).keys(),
        *_prior_referenced_lot_ids(prior_payload),
        *(
            str(item["source_option_lot_id"])
            for item in assigned["lots"]
            if item["source_option_lot_id"] is not None
        ),
    }
    mutation_case_ids: set[str] = set()
    for old_raw, new_raw in case_mutations:
        old = (
            validate_lifecycle_case_decision_fact(old_raw)
            if old_raw is not None
            else None
        )
        new = (
            validate_lifecycle_case_decision_fact(new_raw)
            if new_raw is not None
            else None
        )
        case_ids = {
            str(item["case_id"]) for item in (old, new) if item is not None
        }
        if len(case_ids) != 1 or any(
            item is not None and item["account"] != account_value
            for item in (old, new)
        ):
            raise CurrentDecisionProjectionError("case mutation binding is invalid")
        case_id = next(iter(case_ids))
        if case_id in mutation_case_ids:
            raise CurrentDecisionProjectionError("duplicate case mutation")
        mutation_case_ids.add(case_id)
        for item in (old, new):
            if item is not None:
                target_ids.update(item["target_contracts_by_lot"])
        mutation_rows.append((old, new))

    facts_by_id = {
        fact["case_id"]: fact
        for fact in (
            _stored_case_fact(row, account=account_value)
            for row in repo.list_current_decision_lifecycle_fact_rows(
                account=account_value,
                target_lot_ids=sorted(target_ids),
                conn=conn,
            )
        )
    }
    for old, new in mutation_rows:
        case_id = str((new or old)["case_id"])  # type: ignore[index]
        if new is None:
            facts_by_id.pop(case_id, None)
        else:
            facts_by_id[case_id] = new

    facts = [facts_by_id[case_id] for case_id in sorted(facts_by_id)]
    if all_quality_case_facts is not None:
        quality = build_lifecycle_quality_fact(
            account=account_value,
            all_case_facts=all_quality_case_facts,
            operational_case_facts=facts,
        )
    elif prior_payload is not None:
        quality = update_lifecycle_quality_fact(
            prior_payload["lifecycle_quality"],
            case_mutations=mutation_rows,
            operational_case_facts=facts,
        )
    else:
        raise CurrentDecisionProjectionError("trusted lifecycle quality fact is required")
    return build_current_decision_projection_payload(
        account=account_value,
        current_inputs=inputs,
        case_facts=facts,
        assigned_stock=assigned,
        lifecycle_quality=quality,
        updated_at_ms=updated_at_ms,
        implementation_fingerprint=implementation_fingerprint,
    )
