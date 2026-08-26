from __future__ import annotations

from .current_decision_common import (
    ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA,
    Any,
    CURRENT_LIFECYCLE_QUALITY_SCHEMA,
    CurrentDecisionProjectionError,
    Iterable,
    LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
    Mapping,
    Sequence,
    _GENERATION_FIELDS,
    _OPERATIONAL_STATUSES,
    _fact_hash,
    _hash_without,
    _integer,
    _integer_map,
    _lifecycle_case_current_generation_token,
    _optional_integer,
    _position_lot_fields,
    _sha256,
    _text,
    allocation_id_for,
    arbitrate_lifecycle_case_resolutions,
    canonical_sha256,
    derive_lifecycle_read_model,
    terminal_event_id_for,
)

from .current_decision_lifecycle import (
    validate_lifecycle_case_decision_fact,
)

def arbitrate_lifecycle_case_facts(
    *,
    account: str,
    case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    facts_by_id: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    for raw in case_facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        case_id = str(fact["case_id"])
        if fact["account"] != account_value or case_id in facts_by_id:
            raise CurrentDecisionProjectionError("lifecycle case fact account or id mismatch")
        facts_by_id[case_id] = fact
        resolution = fact["resolution"]
        resolutions[case_id] = {
            "resolver_schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
            "case_id": case_id,
            "status": resolution["status"],
            "anchor_facts": resolution["anchor_facts"],
            "requested_reservations_by_lot": resolution[
                "requested_reservations_by_lot"
            ],
            "effective_reservations_by_lot": resolution[
                "effective_reservations_by_lot"
            ],
            "reason_codes": resolution["contested_reason_codes"],
        }
        resolutions[case_id]["resolution_hash"] = _hash_without(
            resolutions[case_id],
            "resolution_hash",
        )
    arbitration = arbitrate_lifecycle_case_resolutions(
        account=account_value,
        case_resolutions=resolutions,
    )
    effective_facts: list[dict[str, Any]] = []
    for resolution in arbitration["case_resolutions"]:
        case_id = str(resolution["case_id"])
        fact = dict(facts_by_id[case_id])
        fact_resolution = dict(fact["resolution"])
        fact_resolution.update(
            {
                "status": resolution["status"],
                "effective_reservations_by_lot": dict(
                    resolution.get("effective_reservations_by_lot") or {}
                ),
                "contested_reason_codes": list(
                    resolution.get("reason_codes") or []
                ),
            }
        )
        fact["resolution"] = fact_resolution
        fact["generation"] = dict(fact["generation"])
        fact["generation"]["generation_token"] = (
            _lifecycle_case_current_generation_token(fact)
        )
        fact["fact_sha256"] = _fact_hash(fact)
        effective_facts.append(validate_lifecycle_case_decision_fact(fact))
    result = {
        "schema_version": ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA,
        "account": account_value,
        "operational_cases": effective_facts,
        "contested_components": arbitration["contested_components"],
        "arbitration_hash": arbitration["arbitration_hash"],
    }
    result["operational_cases_hash"] = canonical_sha256(effective_facts)
    return result

def _synthetic_allocations(fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    case_id = str(fact["case_id"])
    resolved_by_lot = dict(fact["resolution"]["resolved_contracts_by_lot"])
    remaining_by_type = dict(
        fact["resolution"]["resolved_contracts_by_terminal_type"]
    )
    allocations: list[dict[str, Any]] = []
    index = 0
    for lot_id in sorted(resolved_by_lot):
        lot_remaining = int(resolved_by_lot[lot_id])
        for terminal_type in sorted(remaining_by_type):
            quantity = min(lot_remaining, int(remaining_by_type[terminal_type]))
            if quantity <= 0:
                continue
            index += 1
            evidence_id = f"current-decision:{case_id}:{index}"
            allocations.append(
                {
                    "allocation_id": allocation_id_for(
                        case_id=case_id,
                        evidence_id=evidence_id,
                        target_lot_id=lot_id,
                    ),
                    "case_id": case_id,
                    "evidence_id": evidence_id,
                    "target_lot_id": lot_id,
                    "terminal_type": terminal_type,
                    "contracts_allocated": quantity,
                    "canonical_terminal_event_id": terminal_event_id_for(
                        case_id=case_id,
                        evidence_id=evidence_id,
                        target_lot_id=lot_id,
                        terminal_type=terminal_type,
                        contracts_allocated=quantity,
                    ),
                }
            )
            lot_remaining -= quantity
            remaining_by_type[terminal_type] -= quantity
        if lot_remaining:
            raise CurrentDecisionProjectionError("lifecycle allocation matrix is incoherent")
    if any(remaining_by_type.values()):
        raise CurrentDecisionProjectionError("lifecycle terminal totals are incoherent")
    return allocations

def derive_lifecycle_case_current_view(
    case_fact: Mapping[str, Any],
    *,
    current_position_lots: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    fact = validate_lifecycle_case_decision_fact(case_fact)
    lots = _position_lot_fields(current_position_lots)
    remaining = fact["resolution"]["remaining_contracts_by_lot"]
    quantity_drift = any(
        lot_id not in lots
        or int(lots[lot_id].get("contracts_open") or 0) != expected
        for lot_id, expected in remaining.items()
    )
    conflicts = set(fact["resolution"]["contested_reason_codes"])
    if fact["resolution"]["status"] == "conflict" and not conflicts:
        conflicts.add("lifecycle_compact_resolution_conflict")
    timing = fact["timing"]
    model = derive_lifecycle_read_model(
        expiration_ymd=fact["contract"]["expiration_ymd"],
        market=fact["market"],
        target_contracts_by_lot=fact["target_contracts_by_lot"],
        allocations=_synthetic_allocations(fact),
        accepted_option_close_contracts_by_lot=fact["resolution"][
            "effective_reservations_by_lot"
        ],
        now_ms=_integer(now_ms, field="now_ms", minimum=1),
        conflict_reason_codes=tuple(sorted(conflicts)),
        quantity_drift=quantity_drift,
        observation_start_ms_override=timing["observation_start_ms"],
        pending_until_ms_override=(
            timing["settlement_deadline_ms"] or timing["pending_until_ms"]
        ),
    )
    persisted_status = str(fact["status"])
    persisted_reason = str(fact["decision"]["reason_state"])
    reason_state = (
        persisted_reason
        if persisted_status in {"needs_review", "conflict"}
        and persisted_reason in {"needs_review", "conflict"}
        else model.reason_state
    )
    close_reason = (
        fact["decision"]["close_reason"]
        if reason_state in {"needs_review", "conflict"}
        else model.close_reason
    )
    if model.lifecycle_state == "conflict":
        evidence_status = "conflict"
    elif fact["resolution"]["status"] in {"direct", "bridged"} and any(
        model.reserved_contracts_by_lot.values()
    ):
        evidence_status = "closure_observed_cause_pending"
    elif not fact["resolution"]["anchor_facts"] and not any(
        model.resolved_contracts_by_lot.values()
    ):
        evidence_status = "missing"
    elif any(model.remaining_contracts_by_lot.values()):
        evidence_status = "partial"
    else:
        evidence_status = "complete"
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_case_id": fact["case_id"],
        "lifecycle_state": model.lifecycle_state,
        "lifecycle_evidence_status": evidence_status,
        "lifecycle_reason_codes": sorted(
            {
                *model.lifecycle_reason_codes,
                *fact["decision"]["reason_codes"],
            }
        ),
        "observation_start_ms": model.observation_start_ms,
        "pending_until_ms": model.pending_until_ms,
        "timing_policy_hash": timing["timing_policy_hash"],
        "target_contracts_by_lot": fact["target_contracts_by_lot"],
        "resolved_contracts_by_lot": model.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": model.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            model.resolved_contracts_by_terminal_type
        ),
        "reserved_contracts_by_lot": model.reserved_contracts_by_lot,
        "closure_fact": model.closure_fact,
        "reason_state": reason_state,
        "close_reason": close_reason,
        "lifecycle_generation_token": fact["generation"]["generation_token"],
        "actionable": model.actionable
        and reason_state
        not in {"cause_pending", "partially_resolved", "needs_review", "conflict"},
    }

def lifecycle_views_by_lot(
    lifecycle: Mapping[str, Any],
    *,
    current_position_lots: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    case_views: dict[str, dict[str, Any]] = {}
    lot_views: dict[str, dict[str, Any]] = {}
    for raw in lifecycle.get("operational_cases") or []:
        view = derive_lifecycle_case_current_view(
            raw,
            current_position_lots=current_position_lots,
            now_ms=now_ms,
        )
        case_id = str(view["lifecycle_case_id"])
        case_views[case_id] = view
        for lot_id in sorted(view["target_contracts_by_lot"]):
            if lot_id not in lot_views:
                lot_views[lot_id] = dict(view)
                continue
            prior = lot_views[lot_id]
            lot_views[lot_id] = {
                **prior,
                "lifecycle_state": "conflict",
                "lifecycle_case_ids": sorted(
                    {
                        str(prior.get("lifecycle_case_id") or ""),
                        case_id,
                    }
                    - {""}
                ),
                "lifecycle_evidence_status": "conflict",
                "lifecycle_reason_codes": sorted(
                    {
                        *prior.get("lifecycle_reason_codes", []),
                        *view.get("lifecycle_reason_codes", []),
                        "reservation_target_overlap",
                    }
                ),
                "reason_state": "conflict",
                "actionable": False,
            }
    return lot_views, case_views

def _quality_detail(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = validate_lifecycle_case_decision_fact(fact)
    return {
        "case_id": item["case_id"],
        "market": item["market"],
        "status": item["status"],
        "trust_class": item["decision"]["quality_trust_class"],
        "evidence_count": item["evidence"]["count"],
        "settlement_deadline_ms": item["timing"]["settlement_deadline_ms"],
        "reason_state": item["decision"]["reason_state"],
        "timing_policy_hash": item["timing"]["timing_policy_hash"],
    }

def _quality_aggregate(
    case_facts: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for raw in case_facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        market = str(fact["market"])
        bucket = buckets.setdefault(
            market,
            {
                "market": market,
                "total_case_count": 0,
                "status_counts": {},
                "trust_class_counts": {},
            },
        )
        bucket["total_case_count"] += 1
        status = str(fact["status"])
        trust = str(fact["decision"]["quality_trust_class"])
        bucket["status_counts"][status] = bucket["status_counts"].get(status, 0) + 1
        bucket["trust_class_counts"][trust] = (
            bucket["trust_class_counts"].get(trust, 0) + 1
        )
    return [
        {
            **bucket,
            "status_counts": dict(sorted(bucket["status_counts"].items())),
            "trust_class_counts": dict(
                sorted(bucket["trust_class_counts"].items())
            ),
        }
        for _market, bucket in sorted(buckets.items())
    ]

def build_lifecycle_quality_fact(
    *,
    account: str,
    all_case_facts: Sequence[Mapping[str, Any]],
    operational_case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if any(
        not isinstance(item, Mapping)
        or str(item.get("account") or "").strip().lower() != account_value
        for item in (*all_case_facts, *operational_case_facts)
    ):
        raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
    details = sorted(
        (_quality_detail(item) for item in operational_case_facts),
        key=lambda item: item["case_id"],
    )
    result = {
        "schema_version": CURRENT_LIFECYCLE_QUALITY_SCHEMA,
        "account": account_value,
        "aggregate_by_market": _quality_aggregate(all_case_facts),
        "operational_cases": details,
    }
    result["aggregate_fingerprint"] = canonical_sha256(
        result["aggregate_by_market"]
    )
    result["detail_fingerprint"] = canonical_sha256(details)
    return validate_lifecycle_quality_fact(result)

def update_lifecycle_quality_fact(
    prior: Mapping[str, Any],
    *,
    case_mutations: Sequence[
        tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]
    ],
    operational_case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prior_value = validate_lifecycle_quality_fact(prior)
    counts: dict[str, dict[str, Any]] = {
        row["market"]: {
            "market": row["market"],
            "total_case_count": row["total_case_count"],
            "status_counts": dict(row["status_counts"]),
            "trust_class_counts": dict(row["trust_class_counts"]),
        }
        for row in prior_value["aggregate_by_market"]
    }

    def apply(fact: Mapping[str, Any], delta: int) -> None:
        item = validate_lifecycle_case_decision_fact(fact)
        if item["account"] != prior_value["account"]:
            raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
        market = str(item["market"])
        bucket = counts.setdefault(
            market,
            {
                "market": market,
                "total_case_count": 0,
                "status_counts": {},
                "trust_class_counts": {},
            },
        )
        bucket["total_case_count"] += delta
        for field, value in (
            ("status_counts", str(item["status"])),
            (
                "trust_class_counts",
                str(item["decision"]["quality_trust_class"]),
            ),
        ):
            bucket[field][value] = bucket[field].get(value, 0) + delta
            if bucket[field][value] == 0:
                del bucket[field][value]
            elif bucket[field][value] < 0:
                raise CurrentDecisionProjectionError("lifecycle quality count underflow")
        if bucket["total_case_count"] < 0:
            raise CurrentDecisionProjectionError("lifecycle quality total underflow")

    seen_cases: set[str] = set()
    for old, new in case_mutations:
        identities = {
            str(item.get("case_id") or "").strip()
            for item in (old, new)
            if isinstance(item, Mapping)
        }
        if len(identities) != 1:
            raise CurrentDecisionProjectionError("lifecycle quality mutation id mismatch")
        case_id = next(iter(identities))
        if case_id in seen_cases:
            raise CurrentDecisionProjectionError("duplicate lifecycle quality mutation")
        seen_cases.add(case_id)
        if old is not None:
            apply(old, -1)
        if new is not None:
            apply(new, 1)
    aggregate = [
        {
            **bucket,
            "status_counts": dict(sorted(bucket["status_counts"].items())),
            "trust_class_counts": dict(
                sorted(bucket["trust_class_counts"].items())
            ),
        }
        for market, bucket in sorted(counts.items())
        if bucket["total_case_count"]
    ]
    if any(
        not isinstance(item, Mapping)
        or str(item.get("account") or "").strip().lower()
        != prior_value["account"]
        for item in operational_case_facts
    ):
        raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
    details = sorted(
        (_quality_detail(item) for item in operational_case_facts),
        key=lambda item: item["case_id"],
    )
    result = {
        "schema_version": CURRENT_LIFECYCLE_QUALITY_SCHEMA,
        "account": prior_value["account"],
        "aggregate_by_market": aggregate,
        "operational_cases": details,
        "aggregate_fingerprint": canonical_sha256(aggregate),
        "detail_fingerprint": canonical_sha256(details),
    }
    return validate_lifecycle_quality_fact(result)

def validate_lifecycle_quality_fact(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "account",
        "aggregate_by_market",
        "operational_cases",
        "aggregate_fingerprint",
        "detail_fingerprint",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("lifecycle quality shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_LIFECYCLE_QUALITY_SCHEMA:
        raise CurrentDecisionProjectionError("lifecycle quality schema is invalid")
    _text(item["account"], field="quality account", lower=True)
    aggregates = item["aggregate_by_market"]
    if not isinstance(aggregates, list):
        raise CurrentDecisionProjectionError("quality aggregates must be a list")
    markets: list[str] = []
    for raw in aggregates:
        keys = {
            "market",
            "total_case_count",
            "status_counts",
            "trust_class_counts",
        }
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise CurrentDecisionProjectionError("quality aggregate shape is invalid")
        row = dict(raw)
        markets.append(_text(row["market"], field="quality market", upper=True))
        total = _integer(row["total_case_count"], field="total_case_count", minimum=1)
        status_counts = _integer_map(row["status_counts"], field="status_counts", positive=True)
        trust_counts = _integer_map(
            row["trust_class_counts"],
            field="trust_class_counts",
            positive=True,
        )
        if sum(status_counts.values()) != total or sum(trust_counts.values()) != total:
            raise CurrentDecisionProjectionError("quality aggregate total mismatch")
    if markets != sorted(set(markets)):
        raise CurrentDecisionProjectionError("quality aggregates are not canonical")
    details = item["operational_cases"]
    if not isinstance(details, list):
        raise CurrentDecisionProjectionError("quality details must be a list")
    detail_ids: list[str] = []
    for raw in details:
        keys = {
            "case_id",
            "market",
            "status",
            "trust_class",
            "evidence_count",
            "settlement_deadline_ms",
            "reason_state",
            "timing_policy_hash",
        }
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise CurrentDecisionProjectionError("quality detail shape is invalid")
        row = dict(raw)
        detail_ids.append(_text(row["case_id"], field="quality case_id"))
        _text(row["market"], field="quality detail market", upper=True)
        _text(row["status"], field="quality detail status", lower=True)
        if row["trust_class"] not in {"trusted", "legacy_gap", "external_review"}:
            raise CurrentDecisionProjectionError("quality detail trust class is invalid")
        _integer(row["evidence_count"], field="quality evidence_count")
        _optional_integer(
            row["settlement_deadline_ms"],
            field="quality settlement_deadline_ms",
            minimum=1,
        )
        _text(row["reason_state"], field="quality reason_state", lower=True)
        _sha256(
            row["timing_policy_hash"],
            field="quality timing_policy_hash",
            optional=True,
        )
    if detail_ids != sorted(set(detail_ids)):
        raise CurrentDecisionProjectionError("quality details are not canonical")
    if (
        _sha256(item["aggregate_fingerprint"], field="aggregate_fingerprint")
        != canonical_sha256(aggregates)
        or _sha256(item["detail_fingerprint"], field="detail_fingerprint")
        != canonical_sha256(details)
    ):
        raise CurrentDecisionProjectionError("lifecycle quality hash mismatch")
    return item

def derive_lifecycle_quality_view(
    quality: Mapping[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    stored = validate_lifecycle_quality_fact(quality)
    verdict_counts: dict[str, int] = {}
    blocked_counts: dict[str, int] = {}
    verdict_counts_by_market: dict[str, dict[str, int]] = {}
    blocked_counts_by_market: dict[str, dict[str, int]] = {}
    operational_trust: dict[str, dict[str, int]] = {}
    operational_status: dict[str, dict[str, int]] = {}
    details: list[dict[str, Any]] = []

    def add_counts(
        market: str,
        verdict: str,
        blocked: Sequence[str],
        count: int = 1,
    ) -> None:
        if count <= 0:
            return
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + count
        market_verdicts = verdict_counts_by_market.setdefault(market, {})
        market_verdicts[verdict] = market_verdicts.get(verdict, 0) + count
        market_blocked = blocked_counts_by_market.setdefault(market, {})
        for consumer in blocked:
            blocked_counts[consumer] = blocked_counts.get(consumer, 0) + count
            market_blocked[consumer] = market_blocked.get(consumer, 0) + count

    for item in stored["operational_cases"]:
        trust = item["trust_class"]
        market = item["market"]
        market_trust = operational_trust.setdefault(market, {})
        market_trust[trust] = market_trust.get(trust, 0) + 1
        status = item["status"]
        market_status = operational_status.setdefault(market, {})
        market_status[status] = market_status.get(status, 0) + 1
        deadline = item["settlement_deadline_ms"]
        if trust == "external_review":
            verdict = "unavailable"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        elif trust == "legacy_gap":
            verdict = "untrusted"
            blocked = ["option_performance"]
        elif item["status"] == "ledger_written":
            verdict = "trusted"
            blocked = []
        elif deadline is None:
            verdict = "unavailable"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        elif now_ms <= deadline and status != "conflict":
            verdict = "partial"
            blocked = []
        else:
            verdict = "untrusted"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        add_counts(market, verdict, blocked)
        details.append({**item, "dataset_status": verdict, "blocked_consumers": blocked})

    aggregate_markets: set[str] = set()
    terminal_classification = {
        "trusted": ("trusted", ()),
        "legacy_gap": ("untrusted", ("option_performance",)),
        "external_review": (
            "unavailable",
            ("close_advice", "lifecycle_report", "option_performance"),
        ),
    }
    for aggregate in stored["aggregate_by_market"]:
        market = aggregate["market"]
        aggregate_markets.add(market)
        terminal_total = 0
        for trust, total in aggregate["trust_class_counts"].items():
            if trust not in terminal_classification:
                raise CurrentDecisionProjectionError(
                    "quality aggregate trust class is invalid"
                )
            count = int(total) - operational_trust.get(market, {}).get(trust, 0)
            if count < 0:
                raise CurrentDecisionProjectionError(
                    "quality operational trust count exceeds aggregate"
                )
            terminal_total += count
            verdict, blocked = terminal_classification[trust]
            add_counts(market, verdict, blocked, count)
        status_terminal_total = 0
        for status, total in aggregate["status_counts"].items():
            count = int(total) - operational_status.get(market, {}).get(status, 0)
            if count < 0:
                raise CurrentDecisionProjectionError(
                    "quality operational status count exceeds aggregate"
                )
            if count and status in _OPERATIONAL_STATUSES:
                raise CurrentDecisionProjectionError(
                    "quality operational status is missing detail"
                )
            status_terminal_total += count
        if terminal_total != status_terminal_total:
            raise CurrentDecisionProjectionError(
                "quality terminal aggregate count mismatch"
            )
    if (set(operational_trust) | set(operational_status)) - aggregate_markets:
        raise CurrentDecisionProjectionError(
            "quality operational market is missing from aggregate"
        )
    return {
        **stored,
        "aggregate_by_market": [
            {
                **aggregate,
                "dataset_status_counts": dict(
                    sorted(
                        verdict_counts_by_market.get(
                            aggregate["market"],
                            {},
                        ).items()
                    )
                ),
                "blocked_consumer_counts": dict(
                    sorted(
                        blocked_counts_by_market.get(
                            aggregate["market"],
                            {},
                        ).items()
                    )
                ),
            }
            for aggregate in stored["aggregate_by_market"]
        ],
        "operational_cases": details,
        "operational_status_counts": dict(sorted(verdict_counts.items())),
        "blocked_consumer_counts": dict(sorted(blocked_counts.items())),
    }

_PROJECTION_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "normalized_account",
        "projector_implementation_fingerprint",
        "position_binding",
        "source_bindings",
        "lifecycle",
        "combo",
        "assigned_stock",
        "lifecycle_quality",
        "decision_state_fingerprint",
        "updated_at_ms",
    }
)

_POSITION_BINDING_KEYS = frozenset(
    {
        "projector_schema",
        "projector_implementation_fingerprint",
        "position_source_generation",
        "position_lots_generation",
        "position_lots_fingerprint",
        "lot_count",
        "active_lot_count",
    }
)

_SOURCE_BINDING_KEYS = frozenset(_GENERATION_FIELDS)

_LIFECYCLE_KEYS = frozenset(
    {
        "schema_version",
        "account",
        "operational_cases",
        "contested_components",
        "arbitration_hash",
        "operational_cases_hash",
    }
)

def validate_current_lifecycle_facts(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _LIFECYCLE_KEYS:
        raise CurrentDecisionProjectionError("current lifecycle shape is invalid")
    item = dict(payload)
    if item["schema_version"] != ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA:
        raise CurrentDecisionProjectionError("current lifecycle schema is invalid")
    account = _text(item["account"], field="lifecycle account", lower=True)
    facts = item["operational_cases"]
    if not isinstance(facts, list):
        raise CurrentDecisionProjectionError("operational lifecycle cases must be a list")
    case_ids: list[str] = []
    for raw in facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        if fact["account"] != account:
            raise CurrentDecisionProjectionError("operational lifecycle account mismatch")
        case_ids.append(str(fact["case_id"]))
    if case_ids != sorted(set(case_ids)):
        raise CurrentDecisionProjectionError("operational lifecycle cases are not canonical")
    rebuilt = arbitrate_lifecycle_case_facts(account=account, case_facts=facts)
    if item != rebuilt:
        raise CurrentDecisionProjectionError("current lifecycle arbitration mismatch")
    return item
