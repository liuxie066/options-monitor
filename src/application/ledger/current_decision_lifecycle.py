from __future__ import annotations

from .current_decision_common import (
    Any,
    CurrentDecisionProjectionError,
    LIFECYCLE_CASE_DECISION_FACT_SCHEMA,
    Mapping,
    _CASE_FACT_KEYS,
    _decimal_text,
    _fact_hash,
    _integer,
    _integer_map,
    _lifecycle_case_current_generation_token,
    _normalize_anchor_facts,
    _optional_integer,
    _optional_text,
    _sha256,
    _text,
    _text_list,
    symbol_market,
)

def build_lifecycle_case_decision_fact(
    *,
    lifecycle_case: Mapping[str, Any],
    case_resolution: Mapping[str, Any],
    generation_token: Mapping[str, Any],
    read_model: Mapping[str, Any],
    evidence_revision: int,
    evidence_count: int,
    admission_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = dict(lifecycle_case)
    resolution = dict(case_resolution)
    token = dict(generation_token)
    model = dict(read_model)
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )
    case_id = str(case.get("case_id") or "").strip()
    account = str(case.get("account") or "").strip().lower()
    if not case_id or not account:
        raise CurrentDecisionProjectionError("lifecycle case id and account are required")
    if str(resolution.get("case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle resolution case mismatch")
    if str(token.get("case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle generation case mismatch")
    if str(model.get("lifecycle_case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle read model case mismatch")
    target = {
        str(key): int(value)
        for key, value in sorted(
            dict(case.get("target_contracts_by_lot") or {}).items()
        )
    }
    admission = dict(admission_head or {})
    status = str(case.get("status") or "").strip().lower()
    reason_state = str(summary.get("reason_state") or "").strip().lower()
    if not reason_state:
        reason_state = {
            "ledger_written": "resolved",
            "partially_resolved": "partially_resolved",
            "needs_review": "needs_review",
            "conflict": "conflict",
            "waiting_settlement_evidence": "cause_pending",
            "pending": "not_started",
        }.get(status, "not_started")
    fact = {
        "schema_version": LIFECYCLE_CASE_DECISION_FACT_SCHEMA,
        "case_id": case_id,
        "account": account,
        "market": str(
            case.get("market") or symbol_market(case.get("symbol")) or ""
        ).strip().upper(),
        "contract": {
            "broker": str(case.get("broker") or "").strip().lower(),
            "futu_account_id": str(case.get("futu_account_id") or "").strip()
            or None,
            "symbol": str(case.get("symbol") or "").strip().upper(),
            "option_type": str(case.get("option_type") or "").strip().lower(),
            "position_side": str(case.get("position_side") or "").strip().lower(),
            "strike": _decimal_text(case.get("strike"), field="strike", optional=True),
            "expiration_ymd": str(case.get("expiration_ymd") or "").strip(),
            "contract_key": str(case.get("contract_key") or "").strip(),
        },
        "target_contracts_by_lot": target,
        "status": status,
        "decision": {
            "decision_type": str(case.get("decision_type") or "").strip().lower()
            or None,
            "reason_state": reason_state,
            "close_reason": str(summary.get("close_reason") or "").strip().lower()
            or None,
            "reason_codes": sorted(
                {
                    str(item).strip()
                    for item in (
                        summary.get("lifecycle_reason_codes")
                        or case.get("reason_codes")
                        or []
                    )
                    if str(item).strip()
                }
            ),
            "resolution_revision": int(summary.get("resolution_revision") or 0),
            "state_fingerprint": str(summary.get("state_fingerprint") or "").strip()
            or None,
            "quality_trust_class": _lifecycle_quality_trust_class(case),
        },
        "resolution": {
            "status": str(resolution.get("status") or "").strip().lower(),
            "resolved_contracts_by_lot": dict(
                sorted(dict(model.get("resolved_contracts_by_lot") or {}).items())
            ),
            "remaining_contracts_by_lot": dict(
                sorted(dict(model.get("remaining_contracts_by_lot") or {}).items())
            ),
            "resolved_contracts_by_terminal_type": dict(
                sorted(
                    dict(
                        model.get("resolved_contracts_by_terminal_type") or {}
                    ).items()
                )
            ),
            "requested_reservations_by_lot": dict(
                sorted(
                    dict(
                        resolution.get("requested_reservations_by_lot") or {}
                    ).items()
                )
            ),
            "effective_reservations_by_lot": dict(
                sorted(
                    dict(
                        resolution.get("effective_reservations_by_lot") or {}
                    ).items()
                )
            ),
            "contested_reason_codes": sorted(
                {
                    str(item).strip()
                    for item in resolution.get("reason_codes") or []
                    if str(item).strip()
                }
            ),
            "anchor_facts": sorted(
                [dict(item) for item in resolution.get("anchor_facts") or []],
                key=lambda item: str(item.get("anchor_fact_id") or ""),
            ),
        },
        "timing": {
            "observation_start_ms": model.get("observation_start_ms"),
            "pending_until_ms": model.get("pending_until_ms"),
            "settlement_deadline_ms": model.get("pending_until_ms"),
            "timing_policy_hash": model.get("timing_policy_hash"),
        },
        "evidence": {
            "revision": int(evidence_revision),
            "count": int(evidence_count),
            "admitted_semantic_schema": admission.get("semantic_schema"),
            "admitted_semantic_fingerprint": admission.get("semantic_fingerprint"),
            "admitted_evidence_id": admission.get("evidence_id"),
            "admitted_evidence_count": 1 if admission else 0,
        },
        "generation": {
            "dependency_case_ids": sorted(
                str(item) for item in token.get("dependency_case_ids") or []
            ),
            "target_lot_ids": sorted(
                str(item) for item in token.get("target_lot_ids") or []
            ),
            "generation_token": "",
        },
    }
    fact["generation"]["generation_token"] = (
        _lifecycle_case_current_generation_token(fact)
    )
    fact["fact_sha256"] = _fact_hash(fact)
    return validate_lifecycle_case_decision_fact(fact)

def _lifecycle_quality_trust_class(case: Mapping[str, Any]) -> str:
    status = str(case.get("status") or "").strip().lower()
    decision_type = str(case.get("decision_type") or "").strip().lower()
    if status in {
        "external_adjustment_pending_review",
        "external_adjustment",
        "manual_review",
    } or decision_type in {
        "external_adjustment_pending_review",
        "external_adjustment",
        "manual_review",
    }:
        return "external_review"
    if (
        bool(case.get("legacy_evidence_gap"))
        or case.get("migration_evidence_complete") is False
        or str(case.get("quality_classification") or "").strip().lower()
        == "legacy_evidence_gap"
    ):
        return "legacy_gap"
    return "trusted"

def validate_lifecycle_case_decision_fact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _CASE_FACT_KEYS:
        raise CurrentDecisionProjectionError("lifecycle case fact shape is invalid")
    item = dict(payload)
    if item["schema_version"] != LIFECYCLE_CASE_DECISION_FACT_SCHEMA:
        raise CurrentDecisionProjectionError("lifecycle case fact schema is invalid")
    case_id = _text(item["case_id"], field="case_id")
    account = _text(item["account"], field="account", lower=True)
    _text(item["market"], field="market", upper=True)

    contract = item["contract"]
    contract_keys = {
        "broker",
        "futu_account_id",
        "symbol",
        "option_type",
        "position_side",
        "strike",
        "expiration_ymd",
        "contract_key",
    }
    if not isinstance(contract, Mapping) or set(contract) != contract_keys:
        raise CurrentDecisionProjectionError("lifecycle contract shape is invalid")
    _text(contract["broker"], field="contract.broker", lower=True)
    _optional_text(contract["futu_account_id"], field="contract.futu_account_id")
    _text(contract["symbol"], field="contract.symbol", upper=True)
    _text(contract["option_type"], field="contract.option_type", lower=True)
    _text(contract["position_side"], field="contract.position_side", lower=True)
    if contract["strike"] is not None:
        if _decimal_text(contract["strike"], field="contract.strike") != contract["strike"]:
            raise CurrentDecisionProjectionError("contract.strike is not canonical")
    _text(contract["expiration_ymd"], field="contract.expiration_ymd")
    _text(contract["contract_key"], field="contract.contract_key")

    target = _integer_map(
        item["target_contracts_by_lot"],
        field="target_contracts_by_lot",
        positive=True,
    )
    if not target:
        raise CurrentDecisionProjectionError("target_contracts_by_lot is required")
    _text(item["status"], field="status", lower=True)

    decision = item["decision"]
    decision_keys = {
        "decision_type",
        "reason_state",
        "close_reason",
        "reason_codes",
        "resolution_revision",
        "state_fingerprint",
        "quality_trust_class",
    }
    if not isinstance(decision, Mapping) or set(decision) != decision_keys:
        raise CurrentDecisionProjectionError("lifecycle decision shape is invalid")
    _optional_text(decision["decision_type"], field="decision_type", lower=True)
    _text(decision["reason_state"], field="reason_state", lower=True)
    _optional_text(decision["close_reason"], field="close_reason", lower=True)
    _text_list(decision["reason_codes"], field="reason_codes")
    _integer(decision["resolution_revision"], field="resolution_revision")
    _sha256(decision["state_fingerprint"], field="state_fingerprint", optional=True)
    if decision["quality_trust_class"] not in {
        "trusted",
        "legacy_gap",
        "external_review",
    }:
        raise CurrentDecisionProjectionError("quality_trust_class is invalid")

    resolution = item["resolution"]
    resolution_keys = {
        "status",
        "resolved_contracts_by_lot",
        "remaining_contracts_by_lot",
        "resolved_contracts_by_terminal_type",
        "requested_reservations_by_lot",
        "effective_reservations_by_lot",
        "contested_reason_codes",
        "anchor_facts",
    }
    if not isinstance(resolution, Mapping) or set(resolution) != resolution_keys:
        raise CurrentDecisionProjectionError("lifecycle resolution shape is invalid")
    _text(resolution["status"], field="resolution.status", lower=True)
    resolved = _integer_map(
        resolution["resolved_contracts_by_lot"],
        field="resolved_contracts_by_lot",
    )
    remaining = _integer_map(
        resolution["remaining_contracts_by_lot"],
        field="remaining_contracts_by_lot",
    )
    terminal = _integer_map(
        resolution["resolved_contracts_by_terminal_type"],
        field="resolved_contracts_by_terminal_type",
    )
    requested = _integer_map(
        resolution["requested_reservations_by_lot"],
        field="requested_reservations_by_lot",
        positive=True,
    )
    effective = _integer_map(
        resolution["effective_reservations_by_lot"],
        field="effective_reservations_by_lot",
        positive=True,
    )
    if set(resolved) != set(target) or set(remaining) != set(target):
        raise CurrentDecisionProjectionError("lifecycle quantity keys mismatch")
    if any(resolved[key] + remaining[key] != target[key] for key in target):
        raise CurrentDecisionProjectionError("lifecycle quantity total mismatch")
    if sum(terminal.values()) != sum(resolved.values()):
        raise CurrentDecisionProjectionError("terminal quantity total mismatch")
    if any(key not in target or value > remaining[key] for key, value in requested.items()):
        raise CurrentDecisionProjectionError("requested reservation exceeds remaining")
    if any(key not in requested or value > requested[key] for key, value in effective.items()):
        raise CurrentDecisionProjectionError("effective reservation exceeds requested")
    _text_list(
        resolution["contested_reason_codes"],
        field="contested_reason_codes",
    )
    _normalize_anchor_facts(resolution["anchor_facts"], case_id=case_id)

    timing = item["timing"]
    timing_keys = {
        "observation_start_ms",
        "pending_until_ms",
        "settlement_deadline_ms",
        "timing_policy_hash",
    }
    if not isinstance(timing, Mapping) or set(timing) != timing_keys:
        raise CurrentDecisionProjectionError("lifecycle timing shape is invalid")
    for field in (
        "observation_start_ms",
        "pending_until_ms",
        "settlement_deadline_ms",
    ):
        _optional_integer(timing[field], field=field, minimum=1)
    _sha256(timing["timing_policy_hash"], field="timing_policy_hash", optional=True)

    evidence = item["evidence"]
    evidence_keys = {
        "revision",
        "count",
        "admitted_semantic_schema",
        "admitted_semantic_fingerprint",
        "admitted_evidence_id",
        "admitted_evidence_count",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != evidence_keys:
        raise CurrentDecisionProjectionError("lifecycle evidence shape is invalid")
    _integer(evidence["revision"], field="evidence.revision")
    count = _integer(evidence["count"], field="evidence.count")
    admitted_count = _integer(
        evidence["admitted_evidence_count"],
        field="admitted_evidence_count",
    )
    admission_fields = (
        evidence["admitted_semantic_schema"],
        evidence["admitted_semantic_fingerprint"],
        evidence["admitted_evidence_id"],
    )
    if admitted_count not in {0, 1} or (admitted_count == 0) != all(
        value is None for value in admission_fields
    ):
        raise CurrentDecisionProjectionError("lifecycle admission shape is invalid")
    if admitted_count:
        _text(admission_fields[0], field="admitted_semantic_schema")
        _sha256(admission_fields[1], field="admitted_semantic_fingerprint")
        _text(admission_fields[2], field="admitted_evidence_id")
        if count < 1:
            raise CurrentDecisionProjectionError("admitted evidence is not counted")

    generation = item["generation"]
    generation_keys = {
        "dependency_case_ids",
        "target_lot_ids",
        "generation_token",
    }
    if not isinstance(generation, Mapping) or set(generation) != generation_keys:
        raise CurrentDecisionProjectionError("lifecycle generation shape is invalid")
    dependency_ids = _text_list(
        generation["dependency_case_ids"],
        field="dependency_case_ids",
    )
    if case_id not in dependency_ids:
        raise CurrentDecisionProjectionError("lifecycle generation omits case")
    target_ids = _text_list(generation["target_lot_ids"], field="target_lot_ids")
    if not set(target).issubset(target_ids):
        raise CurrentDecisionProjectionError("lifecycle generation target mismatch")
    supplied_generation_token = _sha256(
        generation["generation_token"],
        field="generation_token",
    )
    if supplied_generation_token != _lifecycle_case_current_generation_token(item):
        raise CurrentDecisionProjectionError(
            "lifecycle compact generation token mismatch"
        )
    supplied_hash = _sha256(item["fact_sha256"], field="fact_sha256")
    if supplied_hash != _fact_hash(item):
        raise CurrentDecisionProjectionError("lifecycle case fact hash mismatch")
    if account != item["account"]:
        raise CurrentDecisionProjectionError("lifecycle account is not canonical")
    return item

def _lifecycle_admission_from_fact_state(
    fact_state: Mapping[str, Any],
) -> dict[str, Any] | None:
    admission_fields = (
        fact_state.get("admitted_semantic_schema"),
        fact_state.get("admitted_semantic_fingerprint"),
        fact_state.get("admitted_evidence_id"),
    )
    admission = (
        {
            "semantic_schema": admission_fields[0],
            "semantic_fingerprint": admission_fields[1],
            "evidence_id": admission_fields[2],
        }
        if all(value is not None for value in admission_fields)
        else None
    )
    if admission is None and any(value is not None for value in admission_fields):
        raise CurrentDecisionProjectionError("lifecycle admission state is incomplete")
    return admission

def build_initial_lifecycle_case_decision_fact(
    *,
    lifecycle_case: Mapping[str, Any],
    fact_state: Mapping[str, Any],
    resolution: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = dict(lifecycle_case)
    case_id = str(case.get("case_id") or "").strip()
    target = dict(case.get("target_contracts_by_lot") or {})
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )
    resolved = dict(summary.get("resolved_contracts_by_lot") or {})
    if not resolved:
        resolved = {lot_id: 0 for lot_id in target}
    remaining = dict(summary.get("remaining_contracts_by_lot") or {})
    if not remaining:
        remaining = {
            lot_id: int(contracts) - int(resolved.get(lot_id, 0))
            for lot_id, contracts in target.items()
        }
    resolution_value = {
        "case_id": case_id,
        "status": "missing",
        "reason_codes": [],
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
        "anchor_facts": [],
        **dict(resolution or {}),
    }
    timing_value = dict(timing or {})
    return build_lifecycle_case_decision_fact(
        lifecycle_case=case,
        case_resolution=resolution_value,
        generation_token={
            "case_id": case_id,
            "dependency_case_ids": [case_id],
            "target_lot_ids": sorted(target),
        },
        read_model={
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": resolved,
            "remaining_contracts_by_lot": remaining,
            "resolved_contracts_by_terminal_type": dict(
                summary.get("resolved_contracts_by_terminal_type") or {}
            ),
            "observation_start_ms": timing_value.get(
                "observation_start_ms",
                case.get("observation_start_ms"),
            ),
            "pending_until_ms": timing_value.get(
                "pending_until_ms",
                case.get("pending_until_ms"),
            ),
            "timing_policy_hash": timing_value.get("timing_policy_hash"),
        },
        evidence_revision=int(fact_state.get("evidence_revision") or 0),
        evidence_count=int(fact_state.get("evidence_count") or 0),
        admission_head=_lifecycle_admission_from_fact_state(fact_state),
    )

def advance_lifecycle_case_decision_fact(
    prior_fact: Mapping[str, Any],
    *,
    lifecycle_case: Mapping[str, Any],
    fact_state: Mapping[str, Any],
    resolution: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prior = validate_lifecycle_case_decision_fact(prior_fact)
    case = dict(lifecycle_case)
    case_id = str(case.get("case_id") or "").strip()
    account = str(case.get("account") or "").strip().lower()
    if case_id != prior["case_id"] or account != prior["account"]:
        raise CurrentDecisionProjectionError("lifecycle prior fact binding changed")

    prior_resolution = dict(prior["resolution"])
    resolution_update = dict(resolution or {})
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )

    def quantity_map(field: str) -> dict[str, int]:
        value = resolution_update.get(field, summary.get(field))
        return (
            dict(value)
            if isinstance(value, Mapping)
            else dict(prior_resolution[field])
        )

    timing_update = dict(timing or {})
    prior_timing = dict(prior["timing"])
    observation_start_ms = timing_update.get(
        "observation_start_ms",
        prior_timing["observation_start_ms"],
    )
    pending_until_ms = timing_update.get(
        "pending_until_ms",
        prior_timing["pending_until_ms"],
    )
    return build_lifecycle_case_decision_fact(
        lifecycle_case=case,
        case_resolution={
            "case_id": case_id,
            "status": resolution_update.get("status", prior_resolution["status"]),
            "reason_codes": resolution_update.get(
                "reason_codes",
                prior_resolution["contested_reason_codes"],
            ),
            "requested_reservations_by_lot": resolution_update.get(
                "requested_reservations_by_lot",
                prior_resolution["requested_reservations_by_lot"],
            ),
            "effective_reservations_by_lot": resolution_update.get(
                "effective_reservations_by_lot",
                prior_resolution["effective_reservations_by_lot"],
            ),
            "anchor_facts": resolution_update.get(
                "anchor_facts",
                prior_resolution["anchor_facts"],
            ),
        },
        generation_token={
            "case_id": case_id,
            "dependency_case_ids": prior["generation"]["dependency_case_ids"],
            "target_lot_ids": prior["generation"]["target_lot_ids"],
        },
        read_model={
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": quantity_map(
                "resolved_contracts_by_lot"
            ),
            "remaining_contracts_by_lot": quantity_map(
                "remaining_contracts_by_lot"
            ),
            "resolved_contracts_by_terminal_type": quantity_map(
                "resolved_contracts_by_terminal_type"
            ),
            "observation_start_ms": observation_start_ms,
            "pending_until_ms": pending_until_ms,
            "timing_policy_hash": timing_update.get(
                "timing_policy_hash",
                prior_timing["timing_policy_hash"],
            ),
        },
        evidence_revision=int(fact_state.get("evidence_revision") or 0),
        evidence_count=int(fact_state.get("evidence_count") or 0),
        admission_head=_lifecycle_admission_from_fact_state(fact_state),
    )

_ASSIGNED_LOT_KEYS = frozenset(
    {
        "stock_lot_id",
        "source_assignment_event_id",
        "source_option_lot_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "assigned_at_ms",
        "shares_opened",
        "shares_remaining",
        "assignment_price",
        "remaining_cost_basis",
        "basis_policy",
        "strategy",
        "leg_role",
        "strategy_group_id",
        "yield_enhancement_mode",
        "source_option_leg_role",
        "sale_fact_count",
        "sale_fact_chain_sha256",
    }
)

_ASSIGNED_ALLOCATION_KEYS = frozenset(
    {
        "open_event_id",
        "stock_lot_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "shares",
        "start_at_ms",
        "end_at_ms",
        "allocation_status",
        "linkage_basis",
    }
)

_ASSIGNED_LINKAGE_BASES = frozenset({"stock_lot_id", "strategy_group"})

_ASSIGNED_REVIEW_KEYS = frozenset(
    {
        "status",
        "event_id",
        "stock_lot_id",
        "stock_event_id",
        "account",
        "broker",
        "symbol",
        "details_sha256",
    }
)
