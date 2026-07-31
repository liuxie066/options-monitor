from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.lifecycle_allocation import resolve_allocations
from domain.domain.option_lifecycle import build_lifecycle_case
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.notification_outbox import (
    build_notification_intent,
    canonical_payload_hash,
    canonical_state_fingerprint,
)
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
    with_sqlite_repo_transaction,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)


MIGRATION_SCHEMA = "lifecycle_cutover_manifest.v1"
MIGRATION_RECEIPT_SCHEMA = "lifecycle_cutover_receipt.v1"
EXPLICIT_MAPPING_SCHEMA = "lifecycle_explicit_mapping.v1"


def build_lifecycle_migration_inventory(
    repo: Any,
    *,
    explicit_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    explicit_by_case = _explicit_mappings_by_case(explicit_mapping)
    cases = [
        dict(item)
        for item in sqlite_repo.list_trade_lifecycle_cases()
        if isinstance(item, dict)
    ]
    evidence = [
        dict(item)
        for item in sqlite_repo.list_trade_lifecycle_evidence()
        if isinstance(item, dict)
    ]
    allocations = [
        dict(item)
        for item in sqlite_repo.list_trade_lifecycle_allocations()
        if isinstance(item, dict)
    ]
    events = [
        dict(item)
        for item in sqlite_repo.list_trade_events()
        if isinstance(item, dict)
    ]
    events_by_id = {
        str(item.get("event_id") or "").strip(): item
        for item in events
        if str(item.get("event_id") or "").strip()
    }
    lot_fields_by_id = {
        str(item.get("record_id") or "").strip(): dict(
            item.get("fields") or {}
        )
        for item in sqlite_repo.list_position_lots()
        if isinstance(item, dict)
        and str(item.get("record_id") or "").strip()
        and isinstance(item.get("fields"), dict)
    }
    claims = [
        dict(item)
        for item in (
            sqlite_repo.list_trade_lifecycle_source_consumptions()
        )
        if isinstance(item, dict)
    ]
    notifications = [
        dict(item)
        for item in (
            sqlite_repo.list_trade_lifecycle_notifications()
        )
        if isinstance(item, dict)
    ]
    timing_policies = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in sqlite_repo.list_trade_lifecycle_timing_policies()
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }
    void_ids = sorted(
        {
            target
            for item in events
            for target in [valid_void_target_event_id(item)]
            if target
        }
    )
    rows: list[dict[str, Any]] = []
    known_case_ids = {
        str(item.get("case_id") or "").strip()
        for item in cases
    }
    unknown_mapped_cases = sorted(
        set(explicit_by_case) - known_case_ids
    )
    if unknown_mapped_cases:
        raise ValueError(
            "explicit lifecycle mapping references unknown cases: "
            + ",".join(unknown_mapped_cases)
        )
    for lifecycle_case in sorted(
        cases,
        key=lambda item: str(item.get("case_id") or ""),
    ):
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        explicit_row = explicit_by_case.get(case_id)
        if explicit_row is not None:
            rows.append(
                _build_explicit_mapped_lifecycle_row(
                    lifecycle_case=lifecycle_case,
                    mapping=explicit_row,
                    all_cases=cases,
                    all_evidence=evidence,
                    all_allocations=allocations,
                    all_events=events,
                    events_by_id=events_by_id,
                    lot_fields_by_id=lot_fields_by_id,
                    all_claims=claims,
                    all_notifications=notifications,
                    timing_policies=timing_policies,
                    void_ids=void_ids,
                )
            )
            continue
        case_evidence = [
            item
            for item in evidence
            if str(item.get("case_id") or "").strip() == case_id
        ]
        case_allocations = [
            item
            for item in allocations
            if str(item.get("case_id") or "").strip() == case_id
        ]
        case_claims = [
            item
            for item in claims
            if str(item.get("case_id") or "").strip() == case_id
        ]
        case_notifications = [
            item
            for item in notifications
            if str(item.get("case_id") or "").strip() == case_id
        ]
        schema_version = str(
            lifecycle_case.get("schema_version") or ""
        ).strip()
        target = dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        )
        review_reasons: set[str] = set()
        resolution_payload: dict[str, Any] = {}
        legacy_upgrade: dict[str, Any] | None = None
        if not target:
            review_reasons.add("target_manifest_missing")
        else:
            try:
                resolution = resolve_allocations(
                    target,
                    case_allocations,
                    void_event_ids=void_ids,
                )
                resolution_payload = {
                    "status": resolution.status,
                    "resolved_contracts_by_lot": (
                        resolution.resolved_contracts_by_lot
                    ),
                    "remaining_contracts_by_lot": (
                        resolution.remaining_contracts_by_lot
                    ),
                    "resolved_contracts_by_terminal_type": (
                        resolution.resolved_contracts_by_terminal_type
                    ),
                    "reason_codes": list(resolution.reason_codes),
                }
                if resolution.status != "ok":
                    review_reasons.update(resolution.reason_codes)
                for lot_id, expected_remaining in (
                    resolution.remaining_contracts_by_lot.items()
                ):
                    fields = lot_fields_by_id.get(lot_id)
                    if fields is None:
                        review_reasons.add(
                            "target_lot_projection_missing"
                        )
                        continue
                    try:
                        actual_remaining = int(
                            fields.get("contracts_open") or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        review_reasons.add(
                            "target_lot_projection_invalid"
                        )
                        continue
                    if actual_remaining != expected_remaining:
                        review_reasons.add(
                            "target_lot_quantity_drift"
                        )
            except ValueError as exc:
                review_reasons.add(
                    "target_or_allocation_invalid:" + str(exc)
                )
        _validate_case_broker_identity(
            lifecycle_case,
            case_evidence,
            review_reasons=review_reasons,
        )
        _validate_case_allocations(
            case_allocations,
            events_by_id=events_by_id,
            review_reasons=review_reasons,
        )
        _validate_existing_case_claims(
            case_claims,
            case_evidence,
            review_reasons=review_reasons,
        )
        futu_account_ids = sorted(
            {
                str(item.get("futu_account_id") or "").strip()
                for item in case_evidence
                if str(item.get("futu_account_id") or "").strip()
            }
        )
        if schema_version == "lifecycle_case.v2":
            if not isinstance(
                timing_policies.get(case_id),
                dict,
            ):
                review_reasons.add(
                    "lifecycle_timing_policy_missing"
                )
        else:
            legacy_upgrade = _plan_legacy_case_upgrade(
                lifecycle_case=lifecycle_case,
                evidence=case_evidence,
                allocations=case_allocations,
                target_contracts_by_lot=target,
                timing_policy=timing_policies.get(case_id),
                futu_account_ids=futu_account_ids,
                review_reasons=review_reasons,
            )
        planned_claims: list[dict[str, Any]] = []
        existing_claim_keys = {
            str(item.get("source_key") or "").strip()
            for item in case_claims
        }
        for item in case_evidence:
            role = _migration_source_role(item)
            source_key = str(
                item.get("source_event_id") or ""
            ).strip()
            if role is None:
                continue
            if not _is_canonical_futu_key(source_key):
                review_reasons.add(
                    "canonical_broker_source_identity_missing"
                )
                continue
            if source_key in existing_claim_keys:
                continue
            try:
                planned_claims.append(
                    build_source_consumption_claim(
                        source_key=source_key,
                        case_id=case_id,
                        owner_evidence_id=str(
                            item.get("evidence_id") or ""
                        ),
                        source_role=role,
                        economic_payload={
                            **(
                                dict(item.get("raw") or {})
                                if isinstance(
                                    item.get("raw"),
                                    dict,
                                )
                                else {}
                            ),
                            **item,
                        },
                    )
                )
            except ValueError as exc:
                review_reasons.add(
                    "source_claim_unprovable:" + str(exc)
                )
        state = {
            "case": lifecycle_case,
            "evidence": sorted(
                case_evidence,
                key=lambda item: str(
                    item.get("evidence_id") or ""
                ),
            ),
            "allocations": sorted(
                case_allocations,
                key=lambda item: str(
                    item.get("allocation_id") or ""
                ),
            ),
            "effective_void_event_ids": void_ids,
            "claims": sorted(
                case_claims,
                key=lambda item: str(
                    item.get("source_key") or ""
                ),
            ),
            "notifications": sorted(
                case_notifications,
                key=lambda item: str(
                    item.get("outbox_id") or ""
                ),
            ),
            "timing_policy": timing_policies.get(case_id),
        }
        rows.append(
            {
                "target_key": f"lifecycle:{case_id}",
                "kind": "lifecycle_case",
                "selected": False,
                "mapping_status": (
                    "exact" if not review_reasons else "needs_review"
                ),
                "review_reason_codes": sorted(review_reasons),
                "case_id": case_id,
                "account": lifecycle_case.get("account"),
                "futu_account_ids": futu_account_ids,
                "contract_key": lifecycle_case.get("contract_key"),
                "target_contracts_by_lot": target,
                "resolution": resolution_payload,
                "evidence_ids": sorted(
                    str(item.get("evidence_id") or "")
                    for item in case_evidence
                ),
                "allocation_ids": sorted(
                    str(item.get("allocation_id") or "")
                    for item in case_allocations
                ),
                "terminal_event_ids": sorted(
                    str(
                        item.get("canonical_terminal_event_id")
                        or ""
                    )
                    for item in case_allocations
                ),
                "effective_void_event_ids": void_ids,
                "existing_source_claims": case_claims,
                "planned_source_claims": planned_claims,
                "receipt_states": [
                    {
                        "outbox_id": item.get("outbox_id"),
                        "transition_type": item.get(
                            "transition_type"
                        ),
                        "status": item.get("status"),
                    }
                    for item in case_notifications
                ],
                "timing_policy_bound": case_id in timing_policies,
                "timing_policy": timing_policies.get(case_id),
                "planned_futu_account_binding": (
                    futu_account_ids[0]
                    if schema_version == "lifecycle_case.v2"
                    and len(futu_account_ids) == 1
                    and not str(
                        lifecycle_case.get(
                            "futu_account_id"
                        )
                        or ""
                    ).strip()
                    else None
                ),
                "legacy_upgrade": legacy_upgrade,
                "notification_case_id": (
                    dict(legacy_upgrade or {})
                    .get("canonical_case", {})
                    .get("case_id")
                    if legacy_upgrade
                    else case_id
                ),
                "suppress_option_leg_closed": True,
                "seed_final_intent": False,
                "inventory_state_hash": canonical_payload_hash(
                    state
                ),
            }
        )
    _mark_source_claim_ambiguities(
        rows,
        existing_claims=claims,
    )
    rows.extend(
        _normal_close_inventory_rows(
            events,
            notifications,
            void_event_ids=set(void_ids),
        )
    )
    manifest_body = {
        "schema_version": MIGRATION_SCHEMA,
        "rows": sorted(
            rows,
            key=lambda item: str(item.get("target_key") or ""),
        ),
    }
    return {
        **manifest_body,
        "manifest_hash": canonical_payload_hash(manifest_body),
        "row_count": len(rows),
        "exact_count": sum(
            1
            for item in rows
            if item.get("mapping_status") == "exact"
        ),
        "review_count": sum(
            1
            for item in rows
            if item.get("mapping_status") != "exact"
        ),
    }


def _explicit_mappings_by_case(
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    mapping = dict(payload or {})
    if (
        str(mapping.get("schema_version") or "").strip()
        != EXPLICIT_MAPPING_SCHEMA
    ):
        raise ValueError(
            "explicit lifecycle mapping schema is invalid"
        )
    raw_rows = mapping.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError(
            "explicit lifecycle mapping rows must be a list"
        )
    by_case: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError(
                "explicit lifecycle mapping row must be an object"
            )
        row = dict(raw)
        case_id = str(
            row.get("legacy_case_id") or ""
        ).strip()
        disposition = str(
            row.get("disposition") or ""
        ).strip().lower()
        if (
            not case_id
            or disposition
            not in {"terminal_frozen", "bridge_to_v2"}
        ):
            raise ValueError(
                "explicit lifecycle mapping identity is invalid"
            )
        if case_id in by_case:
            raise ValueError(
                "explicit lifecycle mapping case is duplicated: "
                + case_id
            )
        row["legacy_case_id"] = case_id
        row["disposition"] = disposition
        by_case[case_id] = row
    return by_case


def _build_explicit_mapped_lifecycle_row(
    *,
    lifecycle_case: dict[str, Any],
    mapping: dict[str, Any],
    all_cases: list[dict[str, Any]],
    all_evidence: list[dict[str, Any]],
    all_allocations: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    lot_fields_by_id: dict[str, dict[str, Any]],
    all_claims: list[dict[str, Any]],
    all_notifications: list[dict[str, Any]],
    timing_policies: dict[str, dict[str, Any]],
    void_ids: list[str],
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    disposition = str(
        mapping.get("disposition") or ""
    ).strip().lower()
    review_reasons: set[str] = set()
    contract = _explicit_contract(
        mapping.get("canonical_contract"),
        review_reasons=review_reasons,
    )
    target = _explicit_target_manifest(
        mapping.get("target_contracts_by_lot"),
        review_reasons=review_reasons,
    )
    _validate_explicit_case_contract(
        lifecycle_case,
        contract=contract,
        mismatch_exceptions=(
            mapping.get("legacy_case_exceptions")
        ),
        review_reasons=review_reasons,
    )

    evidence_by_id = {
        str(item.get("evidence_id") or "").strip(): item
        for item in all_evidence
        if str(item.get("evidence_id") or "").strip()
    }
    case_allocations = [
        item
        for item in all_allocations
        if str(item.get("case_id") or "").strip() == case_id
    ]
    (
        mapped_evidence,
        planned_bindings,
        planned_claims,
    ) = _plan_explicit_evidence_sources(
        lifecycle_case=lifecycle_case,
        mapping=mapping,
        contract=contract,
        target_contracts_by_lot=target,
        evidence_by_id=evidence_by_id,
        existing_claims=all_claims,
        review_reasons=review_reasons,
    )

    legacy_upgrade: dict[str, Any] | None = None
    resolution_payload: dict[str, Any] = {}
    terminal_event_ids: list[str] = []
    canonical_case_id: str | None = None
    explicit_case_ids = [case_id]
    if disposition == "terminal_frozen":
        terminal_event_ids, resolution_payload = (
            _validate_explicit_terminal_frozen(
                lifecycle_case=lifecycle_case,
                mapping=mapping,
                contract=contract,
                target_contracts_by_lot=target,
                mapped_evidence=mapped_evidence,
                case_allocations=case_allocations,
                events_by_id=events_by_id,
                lot_fields_by_id=lot_fields_by_id,
                void_ids=void_ids,
                review_reasons=review_reasons,
            )
        )
    elif disposition == "bridge_to_v2":
        canonical_case_id, legacy_upgrade = (
            _plan_explicit_legacy_bridge(
                lifecycle_case=lifecycle_case,
                mapping=mapping,
                contract=contract,
                target_contracts_by_lot=target,
                all_cases=all_cases,
                all_allocations=all_allocations,
                all_events=all_events,
                lot_fields_by_id=lot_fields_by_id,
                review_reasons=review_reasons,
            )
        )
        if canonical_case_id:
            explicit_case_ids.append(canonical_case_id)

    evidence_ids = sorted(
        str(item.get("evidence_id") or "")
        for item in mapped_evidence
    )
    target_lot_ids = sorted(target)
    source_keys = sorted(
        str(item.get("source_key") or "")
        for item in planned_claims
    )
    source_keys.extend(
        sorted(
            {
                str(item.get("source_key") or "")
                for item in all_claims
                if str(item.get("owner_evidence_id") or "")
                in evidence_ids
            }
            - set(source_keys)
        )
    )
    state = _explicit_inventory_state(
        mapping=mapping,
        case_ids=explicit_case_ids,
        evidence_ids=evidence_ids,
        terminal_event_ids=terminal_event_ids,
        target_lot_ids=target_lot_ids,
        source_keys=source_keys,
        all_cases=all_cases,
        all_evidence=all_evidence,
        all_allocations=all_allocations,
        all_events=all_events,
        lot_fields_by_id=lot_fields_by_id,
        all_claims=all_claims,
        all_notifications=all_notifications,
        timing_policies=timing_policies,
        void_ids=void_ids,
    )
    notification_case_id = canonical_case_id or case_id
    return {
        "target_key": f"lifecycle:{case_id}",
        "kind": "lifecycle_case",
        "selected": False,
        "mapping_status": (
            "exact" if not review_reasons else "needs_review"
        ),
        "review_reason_codes": sorted(review_reasons),
        "case_id": case_id,
        "account": contract.get("account"),
        "futu_account_ids": sorted(
            {
                binding[1]
                for item in mapping.get("evidence_sources") or []
                if isinstance(item, dict)
                for binding in [
                    _canonical_futu_binding(
                        str(item.get("source_key") or "")
                    )
                ]
                if binding is not None
            }
        ),
        "contract_key": lifecycle_case.get("contract_key"),
        "target_contracts_by_lot": target,
        "resolution": resolution_payload,
        "evidence_ids": evidence_ids,
        "allocation_ids": sorted(
            str(item.get("allocation_id") or "")
            for item in case_allocations
        ),
        "terminal_event_ids": terminal_event_ids,
        "effective_void_event_ids": void_ids,
        "existing_source_claims": [
            item
            for item in all_claims
            if str(item.get("source_key") or "") in source_keys
        ],
        "planned_source_claims": planned_claims,
        "planned_evidence_bindings": planned_bindings,
        "receipt_states": [
            {
                "outbox_id": item.get("outbox_id"),
                "transition_type": item.get("transition_type"),
                "status": item.get("status"),
            }
            for item in all_notifications
            if str(item.get("case_id") or "")
            in explicit_case_ids
        ],
        "timing_policy_bound": (
            bool(canonical_case_id)
            and canonical_case_id in timing_policies
        ),
        "timing_policy": (
            dict(legacy_upgrade or {}).get("timing_policy")
            if legacy_upgrade
            else None
        ),
        "planned_futu_account_binding": None,
        "legacy_upgrade": legacy_upgrade,
        "legacy_terminal_frozen": (
            disposition == "terminal_frozen"
        ),
        "explicit_mapping": mapping,
        "explicit_state_case_ids": explicit_case_ids,
        "explicit_state_evidence_ids": evidence_ids,
        "explicit_state_terminal_event_ids": terminal_event_ids,
        "explicit_state_target_lot_ids": target_lot_ids,
        "explicit_state_source_keys": source_keys,
        "notification_case_id": notification_case_id,
        "suppress_option_leg_closed": True,
        "seed_final_intent": False,
        "inventory_state_hash": canonical_payload_hash(state),
    }


def _explicit_contract(
    raw: Any,
    *,
    review_reasons: set[str],
) -> dict[str, Any]:
    contract = dict(raw or {}) if isinstance(raw, dict) else {}
    required = (
        "account",
        "broker",
        "symbol",
        "option_type",
        "position_side",
        "strike",
        "expiration_ymd",
        "currency",
        "multiplier",
    )
    if any(contract.get(key) in (None, "") for key in required):
        review_reasons.add("explicit_contract_mapping_incomplete")
        return contract
    account = str(contract.get("account") or "").strip().lower()
    symbol = canonical_symbol(contract.get("symbol"))
    option_type = str(
        contract.get("option_type") or ""
    ).strip().lower()
    position_side = str(
        contract.get("position_side") or ""
    ).strip().lower()
    currency = str(contract.get("currency") or "").strip().upper()
    try:
        strike = _canonical_decimal(contract.get("strike"))
        multiplier = _canonical_decimal(contract.get("multiplier"))
    except ValueError:
        review_reasons.add("explicit_contract_mapping_invalid")
        return contract
    if (
        not account
        or not symbol
        or option_type not in {"put", "call"}
        or position_side not in {"short", "long"}
        or currency not in {"USD", "HKD", "CNY"}
        or Decimal(multiplier) <= 0
    ):
        review_reasons.add("explicit_contract_mapping_invalid")
        return contract
    return {
        **contract,
        "account": account,
        "broker": str(contract.get("broker") or "").strip(),
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "strike": strike,
        "expiration_ymd": str(
            contract.get("expiration_ymd") or ""
        ).strip(),
        "currency": currency,
        "multiplier": multiplier,
    }


def _explicit_target_manifest(
    raw: Any,
    *,
    review_reasons: set[str],
) -> dict[str, int]:
    if not isinstance(raw, dict) or not raw:
        review_reasons.add("explicit_target_mapping_missing")
        return {}
    normalized: dict[str, int] = {}
    for lot_id_raw, contracts_raw in raw.items():
        lot_id = str(lot_id_raw or "").strip()
        try:
            contracts = int(contracts_raw)
        except (TypeError, ValueError, OverflowError):
            review_reasons.add("explicit_target_mapping_invalid")
            return {}
        if not lot_id or contracts <= 0:
            review_reasons.add("explicit_target_mapping_invalid")
            return {}
        normalized[lot_id] = contracts
    return normalized


def _validate_explicit_case_contract(
    lifecycle_case: dict[str, Any],
    *,
    contract: dict[str, Any],
    mismatch_exceptions: Any = None,
    review_reasons: set[str],
) -> None:
    if not contract:
        return
    text_fields = {
        "account": lambda value: str(value or "").strip().lower(),
        "broker": lambda value: str(value or "").strip(),
        "symbol": lambda value: canonical_symbol(value) or "",
        "option_type": lambda value: str(value or "").strip().lower(),
        "position_side": lambda value: str(value or "").strip().lower(),
        "expiration_ymd": lambda value: str(value or "").strip(),
    }
    for key, normalize in text_fields.items():
        observed = normalize(lifecycle_case.get(key))
        canonical = normalize(contract.get(key))
        if observed != canonical and not _explicit_case_exception_matches(
            mismatch_exceptions,
            field=key,
            observed=observed,
            canonical=canonical,
        ):
            review_reasons.add("explicit_case_contract_mismatch")
    try:
        observed_strike = _canonical_decimal(
            lifecycle_case.get("strike")
        )
        canonical_strike = _canonical_decimal(
            contract.get("strike")
        )
        if (
            observed_strike != canonical_strike
            and not _explicit_case_exception_matches(
                mismatch_exceptions,
                field="strike",
                observed=observed_strike,
                canonical=canonical_strike,
            )
        ):
            review_reasons.add("explicit_case_contract_mismatch")
        observed_multiplier = _canonical_decimal(
            lifecycle_case.get("multiplier")
        )
        canonical_multiplier = _canonical_decimal(
            contract.get("multiplier")
        )
        if (
            observed_multiplier != canonical_multiplier
            and not _explicit_case_exception_matches(
                mismatch_exceptions,
                field="multiplier",
                observed=observed_multiplier,
                canonical=canonical_multiplier,
            )
        ):
            review_reasons.add("explicit_case_contract_mismatch")
    except ValueError:
        review_reasons.add("explicit_case_contract_mismatch")


def _explicit_case_exception_matches(
    raw: Any,
    *,
    field: str,
    observed: str,
    canonical: str,
) -> bool:
    exceptions = dict(raw or {}) if isinstance(raw, dict) else {}
    item = (
        dict(exceptions.get(field) or {})
        if isinstance(exceptions.get(field), dict)
        else {}
    )
    return (
        field in {"multiplier"}
        and str(item.get("legacy_value") or "").strip()
        == observed
        and str(item.get("canonical_value") or "").strip()
        == canonical
        and bool(str(item.get("reason") or "").strip())
    )


def _plan_explicit_evidence_sources(
    *,
    lifecycle_case: dict[str, Any],
    mapping: dict[str, Any],
    contract: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
    evidence_by_id: dict[str, dict[str, Any]],
    existing_claims: list[dict[str, Any]],
    review_reasons: set[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    raw_sources = mapping.get("evidence_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        review_reasons.add("explicit_evidence_mapping_missing")
        return [], [], []
    case_id = str(lifecycle_case.get("case_id") or "")
    mapped: list[dict[str, Any]] = []
    bindings: list[dict[str, str]] = []
    claims: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()
    seen_source_keys: set[str] = set()
    existing_by_key = {
        str(item.get("source_key") or ""): item
        for item in existing_claims
    }
    case_option_deal = (
        dict(
            dict(lifecycle_case.get("raw") or {}).get(
                "option_deal"
            )
            or {}
        )
        if isinstance(lifecycle_case.get("raw"), dict)
        else {}
    )
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            review_reasons.add("explicit_evidence_mapping_invalid")
            continue
        source = dict(raw_source)
        evidence_id = str(
            source.get("evidence_id") or ""
        ).strip()
        source_key = str(
            source.get("source_key") or ""
        ).strip()
        source_role = str(
            source.get("source_role") or ""
        ).strip().lower()
        evidence = evidence_by_id.get(evidence_id)
        if (
            not evidence_id
            or evidence_id in seen_evidence_ids
            or source_key in seen_source_keys
            or source_role
            not in {"option_anchor", "stock_settlement"}
            or evidence is None
        ):
            review_reasons.add("explicit_evidence_mapping_invalid")
            continue
        seen_evidence_ids.add(evidence_id)
        seen_source_keys.add(source_key)
        mapped.append(evidence)
        expected_type = {
            "option_anchor": "option_zero_price_close",
            "stock_settlement": "stock_settlement_leg",
        }[source_role]
        if (
            str(evidence.get("evidence_type") or "")
            .strip()
            .lower()
            != expected_type
        ):
            review_reasons.add("explicit_evidence_role_mismatch")
        derived = _legacy_evidence_source_identity(evidence)
        binding = _canonical_futu_binding(source_key)
        if (
            derived is None
            or binding is None
            or source_key != derived["source_key"]
            or binding
            != (
                str(contract.get("account") or ""),
                derived["futu_account_id"],
            )
        ):
            review_reasons.add(
                "explicit_broker_source_identity_mismatch"
            )
        current_case_id = str(evidence.get("case_id") or "").strip()
        if current_case_id not in {"", case_id}:
            review_reasons.add(
                "explicit_evidence_case_owner_conflict"
            )
        elif not current_case_id:
            bindings.append(
                {
                    "evidence_id": evidence_id,
                    "case_id": case_id,
                }
            )
        if (
            source_role == "option_anchor"
            and not _option_anchor_matches_legacy_case(
                evidence,
                case_option_deal=case_option_deal,
                lifecycle_case=lifecycle_case,
            )
        ):
            review_reasons.add(
                "explicit_option_anchor_case_mismatch"
            )
        economic_payload = _explicit_claim_payload(
            evidence,
            source_key=source_key,
            source_role=source_role,
            contract=contract,
            target_contracts_by_lot=target_contracts_by_lot,
        )
        try:
            claim = build_source_consumption_claim(
                source_key=source_key,
                case_id=case_id,
                owner_evidence_id=evidence_id,
                source_role=source_role,
                economic_payload=economic_payload,
            )
        except ValueError as exc:
            review_reasons.add(
                "source_claim_unprovable:" + str(exc)
            )
            continue
        existing = existing_by_key.get(source_key)
        if existing is None:
            claims.append(claim)
        elif not _same_source_claim_owner(existing, claim):
            review_reasons.add(
                "source_claim_existing_owner_conflict"
            )
    return mapped, bindings, claims


def _option_anchor_matches_legacy_case(
    evidence: dict[str, Any],
    *,
    case_option_deal: dict[str, Any],
    lifecycle_case: dict[str, Any],
) -> bool:
    if not case_option_deal:
        return False
    case_identity = _legacy_evidence_source_identity(
        {
            "account": lifecycle_case.get("account"),
            "source_event_id": case_option_deal.get("deal_id"),
            "raw": case_option_deal,
        }
    )
    evidence_identity = _legacy_evidence_source_identity(evidence)
    raw = (
        dict(evidence.get("raw") or {})
        if isinstance(evidence.get("raw"), dict)
        else {}
    )
    try:
        return (
            case_identity is not None
            and case_identity == evidence_identity
            and canonical_symbol(raw.get("symbol"))
            == canonical_symbol(lifecycle_case.get("symbol"))
            and str(raw.get("option_type") or "").strip().lower()
            == str(lifecycle_case.get("option_type") or "")
            .strip()
            .lower()
            and str(raw.get("expiration_ymd") or "").strip()
            == str(
                lifecycle_case.get("expiration_ymd") or ""
            ).strip()
            and _canonical_decimal(raw.get("strike"))
            == _canonical_decimal(lifecycle_case.get("strike"))
            and int(raw.get("contracts") or 0)
            == int(lifecycle_case.get("contracts") or 0)
            and _canonical_decimal(raw.get("price")) == "0"
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _legacy_evidence_source_identity(
    evidence: dict[str, Any],
) -> dict[str, str] | None:
    raw = (
        dict(evidence.get("raw") or {})
        if isinstance(evidence.get("raw"), dict)
        else {}
    )
    raw_payload = (
        dict(raw.get("raw_payload") or {})
        if isinstance(raw.get("raw_payload"), dict)
        else {}
    )
    visible = (
        dict(raw.get("visible_account_fields") or {})
        if isinstance(raw.get("visible_account_fields"), dict)
        else {}
    )
    accounts = {
        str(value or "").strip().lower()
        for value in (
            evidence.get("account"),
            raw.get("internal_account"),
        )
        if str(value or "").strip()
    }
    futu_ids = {
        str(value or "").strip()
        for value in (
            evidence.get("futu_account_id"),
            raw.get("futu_account_id"),
            raw_payload.get("futu_account_id"),
            raw_payload.get("trd_acc_id"),
            visible.get("futu_account_id"),
            visible.get("trd_acc_id"),
        )
        if str(value or "").strip()
    }
    deal_ids = {
        str(value or "").strip()
        for value in (
            raw.get("deal_id"),
            raw_payload.get("deal_id"),
            _source_key_deal_id(evidence.get("source_event_id")),
        )
        if str(value or "").strip()
    }
    if (
        len(accounts) != 1
        or len(futu_ids) != 1
        or len(deal_ids) != 1
    ):
        return None
    account = next(iter(accounts))
    futu_account_id = next(iter(futu_ids))
    deal_id = next(iter(deal_ids))
    return {
        "account": account,
        "futu_account_id": futu_account_id,
        "deal_id": deal_id,
        "source_key": (
            f"futu:{account}:{futu_account_id}:{deal_id}"
        ),
    }


def _source_key_deal_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _is_canonical_futu_key(text):
        return text.split(":", 3)[3]
    return text


def _explicit_claim_payload(
    evidence: dict[str, Any],
    *,
    source_key: str,
    source_role: str,
    contract: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
) -> dict[str, Any]:
    raw = (
        dict(evidence.get("raw") or {})
        if isinstance(evidence.get("raw"), dict)
        else {}
    )
    binding = _canonical_futu_binding(source_key) or ("", "")
    contracts = sum(target_contracts_by_lot.values())
    payload = {
        **raw,
        **evidence,
        "account": binding[0],
        "internal_account": binding[0],
        "futu_account_id": binding[1],
        "symbol": contract.get("symbol"),
        "option_type": contract.get("option_type"),
        "position_side": contract.get("position_side"),
        "strike": contract.get("strike"),
        "expiration_ymd": contract.get("expiration_ymd"),
        "multiplier": contract.get("multiplier"),
        "currency": contract.get("currency"),
        "contracts": contracts,
    }
    if source_role == "stock_settlement":
        payload["shares"] = (
            evidence.get("stock_qty")
            or evidence.get("shares")
            or raw.get("contracts")
        )
        payload["price"] = (
            evidence.get("stock_price")
            if evidence.get("stock_price") is not None
            else raw.get("price")
        )
    return payload


def _validate_explicit_terminal_frozen(
    *,
    lifecycle_case: dict[str, Any],
    mapping: dict[str, Any],
    contract: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
    mapped_evidence: list[dict[str, Any]],
    case_allocations: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    lot_fields_by_id: dict[str, dict[str, Any]],
    void_ids: list[str],
    review_reasons: set[str],
) -> tuple[list[str], dict[str, Any]]:
    if (
        str(lifecycle_case.get("status") or "")
        .strip()
        .lower()
        != "ledger_written"
    ):
        review_reasons.add(
            "explicit_terminal_case_not_ledger_written"
        )
    terminal_type = str(
        mapping.get("terminal_type") or ""
    ).strip().lower()
    if terminal_type not in {
        "close",
        "expire_close",
        "assignment",
        "exercise",
    }:
        review_reasons.add("explicit_terminal_type_invalid")
    if (
        str(lifecycle_case.get("decision_type") or "")
        .strip()
        .lower()
        != terminal_type
    ):
        review_reasons.add(
            "explicit_terminal_decision_mismatch"
        )
    terminal_event_ids = sorted(
        {
            str(item or "").strip()
            for item in mapping.get("terminal_event_ids") or []
            if str(item or "").strip()
        }
    )
    if not terminal_event_ids:
        review_reasons.add("explicit_terminal_event_missing")
    if any(item in void_ids for item in terminal_event_ids):
        review_reasons.add("explicit_terminal_event_voided")
    if case_allocations:
        _validate_case_allocations(
            case_allocations,
            events_by_id=events_by_id,
            review_reasons=review_reasons,
        )
        allocation_event_ids = {
            str(
                item.get("canonical_terminal_event_id") or ""
            ).strip()
            for item in case_allocations
            if str(
                item.get("canonical_terminal_event_id") or ""
            ).strip()
        }
        if not allocation_event_ids.issubset(
            set(terminal_event_ids)
        ):
            review_reasons.add(
                "explicit_terminal_allocation_event_mismatch"
            )
        try:
            allocation_resolution = resolve_allocations(
                target_contracts_by_lot,
                case_allocations,
                void_event_ids=void_ids,
            )
        except ValueError:
            allocation_resolution = None
        if (
            allocation_resolution is None
            or allocation_resolution.status != "ok"
        ):
            review_reasons.add(
                "explicit_terminal_allocation_invalid"
            )
    resolved_by_lot = {
        lot_id: 0 for lot_id in target_contracts_by_lot
    }
    case_id = str(lifecycle_case.get("case_id") or "")
    adopted_event_ids = {
        str(item or "").strip()
        for item in lifecycle_case.get("adopted_event_ids") or []
        if str(item or "").strip()
    }
    for event_id in terminal_event_ids:
        event = events_by_id.get(event_id)
        if event is None:
            review_reasons.add("explicit_terminal_event_missing")
            continue
        lot_id = str(event.get("target_lot_id") or "").strip()
        raw_payload = (
            dict(event.get("raw_payload") or {})
            if isinstance(event.get("raw_payload"), dict)
            else {}
        )
        try:
            contracts = int(event.get("contracts") or 0)
        except (TypeError, ValueError, OverflowError):
            contracts = 0
        if (
            str(event.get("event_type") or "").strip().lower()
            != terminal_type
            or lot_id not in resolved_by_lot
            or contracts <= 0
            or (
                str(raw_payload.get("case_id") or "").strip()
                != case_id
                and event_id not in adopted_event_ids
            )
            or not _event_matches_explicit_contract(
                event,
                contract=contract,
            )
        ):
            review_reasons.add(
                "explicit_terminal_event_contract_mismatch"
            )
            continue
        resolved_by_lot[lot_id] += contracts
    if resolved_by_lot != target_contracts_by_lot:
        review_reasons.add(
            "explicit_terminal_quantity_mismatch"
        )
    for lot_id in target_contracts_by_lot:
        fields = lot_fields_by_id.get(lot_id)
        if fields is None:
            review_reasons.add("explicit_target_lot_missing")
            continue
        try:
            open_contracts = int(fields.get("contracts_open") or 0)
            closed_contracts = int(
                fields.get("contracts_closed") or 0
            )
        except (TypeError, ValueError, OverflowError):
            review_reasons.add(
                "explicit_target_lot_projection_invalid"
            )
            continue
        if (
            open_contracts != 0
            or closed_contracts
            < target_contracts_by_lot[lot_id]
            or not _lot_matches_explicit_contract(
                fields,
                contract=contract,
            )
        ):
            review_reasons.add(
                "explicit_target_lot_projection_mismatch"
            )
    _validate_terminal_evidence_roles(
        terminal_type=terminal_type,
        lifecycle_case=lifecycle_case,
        mapping=mapping,
        contract=contract,
        target_contracts_by_lot=target_contracts_by_lot,
        evidence=mapped_evidence,
        review_reasons=review_reasons,
    )
    return terminal_event_ids, {
        "status": (
            "ok"
            if (
                terminal_event_ids
                and resolved_by_lot == target_contracts_by_lot
            )
            else "invalid"
        ),
        "resolved_contracts_by_lot": resolved_by_lot,
        "remaining_contracts_by_lot": {
            lot_id: max(
                0,
                expected - resolved_by_lot.get(lot_id, 0),
            )
            for lot_id, expected in target_contracts_by_lot.items()
        },
        "resolved_contracts_by_terminal_type": {
            terminal_type: sum(resolved_by_lot.values())
        }
        if terminal_type
        else {},
        "reason_codes": [],
    }


def _validate_terminal_evidence_roles(
    *,
    terminal_type: str,
    lifecycle_case: dict[str, Any],
    mapping: dict[str, Any],
    contract: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
    evidence: list[dict[str, Any]],
    review_reasons: set[str],
) -> None:
    option_rows = [
        item
        for item in evidence
        if str(item.get("evidence_type") or "").strip().lower()
        == "option_zero_price_close"
    ]
    stock_rows = [
        item
        for item in evidence
        if str(item.get("evidence_type") or "").strip().lower()
        == "stock_settlement_leg"
    ]
    if len(option_rows) != 1:
        review_reasons.add("explicit_option_anchor_count_invalid")
    requires_stock = terminal_type in {"assignment", "exercise"}
    if (
        (requires_stock and len(stock_rows) != 1)
        or (not requires_stock and stock_rows)
    ):
        review_reasons.add(
            "explicit_stock_settlement_count_invalid"
        )
        return
    if not requires_stock or len(option_rows) != 1:
        return
    stock = stock_rows[0]
    option = option_rows[0]
    raw_stock = (
        dict(stock.get("raw") or {})
        if isinstance(stock.get("raw"), dict)
        else {}
    )
    stock_symbol = canonical_symbol(
        stock.get("symbol") or raw_stock.get("symbol")
    )
    expected_side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get(
        (
            terminal_type,
            str(contract.get("option_type") or ""),
            str(contract.get("position_side") or ""),
        )
    )
    try:
        stock_qty = int(
            stock.get("stock_qty")
            or stock.get("shares")
            or raw_stock.get("contracts")
            or 0
        )
        expected_qty = int(
            Decimal(str(contract.get("multiplier")))
            * sum(target_contracts_by_lot.values())
        )
        stock_price = _canonical_decimal(
            stock.get("stock_price")
            if stock.get("stock_price") is not None
            else raw_stock.get("price")
        )
        option_time = int(
            option.get("trade_time_ms")
            or option.get("event_time_ms")
            or dict(option.get("raw") or {}).get(
                "trade_time_ms"
            )
            or 0
        )
        stock_time = int(
            stock.get("trade_time_ms")
            or stock.get("event_time_ms")
            or raw_stock.get("trade_time_ms")
            or 0
        )
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        OverflowError,
    ):
        review_reasons.add(
            "explicit_stock_settlement_economics_invalid"
        )
        return
    side = str(
        stock.get("side") or raw_stock.get("side") or ""
    ).strip().lower()
    if (
        not expected_side
        or side != expected_side
        or stock_symbol != contract.get("symbol")
        or stock_qty != expected_qty
        or stock_price
        != _canonical_decimal(contract.get("strike"))
    ):
        review_reasons.add(
            "explicit_stock_settlement_economics_mismatch"
        )
    window = (
        dict(mapping.get("settlement_window") or {})
        if isinstance(mapping.get("settlement_window"), dict)
        else {}
    )
    try:
        start_ms = int(window.get("start_ms") or 0)
        end_ms = int(window.get("end_ms") or 0)
    except (TypeError, ValueError, OverflowError):
        start_ms = 0
        end_ms = 0
    if (
        not str(window.get("source") or "").strip()
        or start_ms <= 0
        or end_ms < start_ms
        or option_time <= 0
        or stock_time < max(start_ms, option_time)
        or stock_time > end_ms
    ):
        review_reasons.add(
            "explicit_stock_settlement_window_mismatch"
        )


def _plan_explicit_legacy_bridge(
    *,
    lifecycle_case: dict[str, Any],
    mapping: dict[str, Any],
    contract: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
    all_cases: list[dict[str, Any]],
    all_allocations: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    lot_fields_by_id: dict[str, dict[str, Any]],
    review_reasons: set[str],
) -> tuple[str | None, dict[str, Any] | None]:
    legacy_id = str(lifecycle_case.get("case_id") or "")
    if (
        str(lifecycle_case.get("status") or "")
        .strip()
        .lower()
        not in {"waiting", "waiting_settlement_evidence"}
    ):
        review_reasons.add("explicit_bridge_legacy_status_invalid")
    canonical_case_id = str(
        mapping.get("canonical_case_id") or ""
    ).strip()
    canonical_case = next(
        (
            item
            for item in all_cases
            if str(item.get("case_id") or "").strip()
            == canonical_case_id
        ),
        None,
    )
    if (
        not canonical_case_id
        or canonical_case is None
        or canonical_case_id == legacy_id
        or str(canonical_case.get("schema_version") or "")
        != "lifecycle_case.v2"
    ):
        review_reasons.add("explicit_bridge_target_invalid")
        return canonical_case_id or None, None
    _validate_explicit_case_contract(
        canonical_case,
        contract=contract,
        review_reasons=review_reasons,
    )
    canonical_target = _explicit_target_manifest(
        canonical_case.get("target_contracts_by_lot"),
        review_reasons=review_reasons,
    )
    if canonical_target != target_contracts_by_lot:
        review_reasons.add("explicit_bridge_target_manifest_mismatch")
    case_ids = {legacy_id, canonical_case_id}
    if any(
        str(item.get("case_id") or "") in case_ids
        for item in all_allocations
    ):
        review_reasons.add("explicit_bridge_has_terminal_allocation")
    if any(
        str(dict(item.get("raw_payload") or {}).get("case_id") or "")
        in case_ids
        and str(item.get("event_type") or "").strip().lower()
        in {"close", "expire_close", "assignment", "exercise"}
        for item in all_events
        if isinstance(item.get("raw_payload"), dict)
    ):
        review_reasons.add("explicit_bridge_has_terminal_event")
    for lot_id, expected in target_contracts_by_lot.items():
        fields = lot_fields_by_id.get(lot_id)
        try:
            open_contracts = int(
                dict(fields or {}).get("contracts_open") or 0
            )
        except (TypeError, ValueError, OverflowError):
            open_contracts = 0
        if (
            fields is None
            or open_contracts < expected
            or not _lot_matches_explicit_contract(
                fields,
                contract=contract,
            )
        ):
            review_reasons.add(
                "explicit_bridge_target_lot_mismatch"
            )
    source_bindings = {
        binding[1]
        for item in mapping.get("evidence_sources") or []
        if isinstance(item, dict)
        for binding in [
            _canonical_futu_binding(
                str(item.get("source_key") or "")
            )
        ]
        if binding is not None
    }
    if len(source_bindings) != 1:
        review_reasons.add(
            "explicit_bridge_futu_account_ambiguous"
        )
        futu_account_id = ""
    else:
        futu_account_id = next(iter(source_bindings))
    existing_futu = str(
        canonical_case.get("futu_account_id") or ""
    ).strip()
    if existing_futu and existing_futu != futu_account_id:
        review_reasons.add(
            "explicit_bridge_futu_account_conflict"
        )
    policy = (
        dict(mapping.get("timing_policy") or {})
        if isinstance(mapping.get("timing_policy"), dict)
        else {}
    )
    try:
        cutoff_ms = int(policy.get("last_trade_cutoff_ms") or 0)
        deadline_ms = int(
            policy.get("settlement_deadline_ms") or 0
        )
    except (TypeError, ValueError, OverflowError):
        cutoff_ms = 0
        deadline_ms = 0
    if (
        str(policy.get("policy_schema") or "")
        != "lifecycle_timing_policy.v1"
        or str(policy.get("case_id") or "") != canonical_case_id
        or str(policy.get("market") or "").strip().upper()
        != str(canonical_case.get("market") or "").strip().upper()
        or cutoff_ms <= 0
        or deadline_ms <= 0
        or not str(policy.get("calendar_hash") or "").strip()
    ):
        review_reasons.add("explicit_bridge_timing_policy_invalid")
    canonical_payload = {
        **canonical_case,
        "futu_account_id": existing_futu or futu_account_id,
    }
    bridge_evidence = [
        {
            "schema_version": "migration_bridge_evidence.v1",
            "evidence_id": "migration_bridge_"
            + canonical_payload_hash(
                {
                    "legacy_case_id": legacy_id,
                    "canonical_case_id": canonical_case_id,
                    "referenced_legacy_evidence_id": (
                        item.get("evidence_id")
                    ),
                }
            )[:24],
            "case_id": canonical_case_id,
            "source_type": "lifecycle_migration",
            "source_event_id": None,
            "evidence_type": "migration_bridge",
            "account": contract.get("account"),
            "symbol": contract.get("symbol"),
            "referenced_legacy_case_id": legacy_id,
            "referenced_legacy_evidence_id": str(
                item.get("evidence_id") or ""
            ),
            "allocating": False,
        }
        for item in mapping.get("evidence_sources") or []
        if isinstance(item, dict)
        and str(item.get("evidence_id") or "").strip()
    ]
    return canonical_case_id, {
        "schema_version": "legacy_lifecycle_upgrade.v1",
        "legacy_case_id": legacy_id,
        "canonical_case": canonical_payload,
        "timing_policy": policy,
        "bridge_evidence": bridge_evidence,
        "reuse_existing_canonical_case": True,
    }


def _event_matches_explicit_contract(
    event: dict[str, Any],
    *,
    contract: dict[str, Any],
) -> bool:
    event_contract = (
        dict(event.get("contract_key") or {})
        if isinstance(event.get("contract_key"), dict)
        else {}
    )
    return (
        str(event_contract.get("account") or "").strip().lower()
        == str(contract.get("account") or "")
        and str(event_contract.get("broker") or "").strip()
        == str(contract.get("broker") or "")
        and canonical_symbol(
            event_contract.get("underlying_symbol")
            or event_contract.get("symbol")
        )
        == contract.get("symbol")
        and str(event_contract.get("option_type") or "")
        .strip()
        .lower()
        == contract.get("option_type")
        and str(event_contract.get("position_side") or "")
        .strip()
        .lower()
        == contract.get("position_side")
        and str(event_contract.get("expiration_ymd") or "").strip()
        == contract.get("expiration_ymd")
        and _decimal_equal(
            event_contract.get("strike"),
            contract.get("strike"),
        )
        and str(event.get("currency") or "").strip().upper()
        == contract.get("currency")
        and _decimal_equal(
            event.get("multiplier"),
            contract.get("multiplier"),
        )
    )


def _lot_matches_explicit_contract(
    fields: dict[str, Any],
    *,
    contract: dict[str, Any],
) -> bool:
    return (
        str(fields.get("account") or "").strip().lower()
        == str(contract.get("account") or "")
        and str(fields.get("broker") or "").strip()
        == str(contract.get("broker") or "")
        and canonical_symbol(fields.get("symbol"))
        == contract.get("symbol")
        and str(fields.get("option_type") or "").strip().lower()
        == contract.get("option_type")
        and str(
            fields.get("position_side")
            or fields.get("side")
            or ""
        )
        .strip()
        .lower()
        == contract.get("position_side")
        and str(fields.get("expiration_ymd") or "").strip()
        == contract.get("expiration_ymd")
        and _decimal_equal(
            fields.get("strike"),
            contract.get("strike"),
        )
        and str(fields.get("currency") or "").strip().upper()
        == contract.get("currency")
        and _decimal_equal(
            fields.get("multiplier"),
            contract.get("multiplier"),
        )
    )


def _canonical_decimal(value: Any) -> str:
    if value in (None, "") or isinstance(value, bool):
        raise ValueError("decimal value is missing")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("decimal value is invalid") from exc
    if not number.is_finite():
        raise ValueError("decimal value is invalid")
    normalized = number.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _decimal_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_decimal(left) == _canonical_decimal(right)
    except ValueError:
        return False


def _explicit_inventory_state(
    *,
    mapping: dict[str, Any],
    case_ids: list[str],
    evidence_ids: list[str],
    terminal_event_ids: list[str],
    target_lot_ids: list[str],
    source_keys: list[str],
    all_cases: list[dict[str, Any]],
    all_evidence: list[dict[str, Any]],
    all_allocations: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    lot_fields_by_id: dict[str, dict[str, Any]],
    all_claims: list[dict[str, Any]],
    all_notifications: list[dict[str, Any]],
    timing_policies: dict[str, dict[str, Any]],
    void_ids: list[str],
) -> dict[str, Any]:
    case_id_set = set(case_ids)
    evidence_id_set = set(evidence_ids)
    event_id_set = set(terminal_event_ids)
    source_key_set = set(source_keys)
    return {
        "explicit_mapping": mapping,
        "cases": sorted(
            [
                item
                for item in all_cases
                if str(item.get("case_id") or "") in case_id_set
            ],
            key=lambda item: str(item.get("case_id") or ""),
        ),
        "evidence": sorted(
            [
                item
                for item in all_evidence
                if str(item.get("evidence_id") or "")
                in evidence_id_set
            ],
            key=lambda item: str(item.get("evidence_id") or ""),
        ),
        "allocations": sorted(
            [
                item
                for item in all_allocations
                if str(item.get("case_id") or "") in case_id_set
            ],
            key=lambda item: str(item.get("allocation_id") or ""),
        ),
        "terminal_events": sorted(
            [
                item
                for item in all_events
                if str(item.get("event_id") or "") in event_id_set
            ],
            key=lambda item: str(item.get("event_id") or ""),
        ),
        "target_lots": {
            lot_id: lot_fields_by_id.get(lot_id)
            for lot_id in sorted(target_lot_ids)
        },
        "effective_void_event_ids": void_ids,
        "claims": sorted(
            [
                item
                for item in all_claims
                if (
                    str(item.get("case_id") or "") in case_id_set
                    or str(item.get("source_key") or "")
                    in source_key_set
                )
            ],
            key=lambda item: str(item.get("source_key") or ""),
        ),
        "notifications": sorted(
            [
                item
                for item in all_notifications
                if str(item.get("case_id") or "") in case_id_set
            ],
            key=lambda item: str(item.get("outbox_id") or ""),
        ),
        "timing_policies": {
            case_id: timing_policies.get(case_id)
            for case_id in sorted(case_id_set)
        },
    }


def _plan_legacy_case_upgrade(
    *,
    lifecycle_case: dict[str, Any],
    evidence: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    target_contracts_by_lot: dict[str, Any],
    timing_policy: dict[str, Any] | None,
    futu_account_ids: list[str],
    review_reasons: set[str],
) -> dict[str, Any] | None:
    if not target_contracts_by_lot:
        review_reasons.add(
            "legacy_case_requires_explicit_target_mapping"
        )
        return None
    if len(futu_account_ids) != 1:
        review_reasons.add(
            "legacy_case_requires_explicit_futu_mapping"
        )
        return None
    if allocations:
        review_reasons.add(
            "legacy_terminal_case_requires_frozen_receipt_only"
        )
        return None
    if not isinstance(timing_policy, dict):
        review_reasons.add(
            "lifecycle_timing_policy_missing"
        )
        return None
    symbol = str(
        lifecycle_case.get("symbol") or ""
    ).strip().upper()
    market = str(
        lifecycle_case.get("market")
        or symbol_market(symbol)
        or ""
    ).strip().upper()
    required = {
        "account": lifecycle_case.get("account"),
        "broker": lifecycle_case.get("broker"),
        "contract_key": lifecycle_case.get("contract_key"),
        "position_side": lifecycle_case.get("position_side"),
        "expiration_ymd": lifecycle_case.get("expiration_ymd"),
        "symbol": symbol,
        "option_type": lifecycle_case.get("option_type"),
        "strike": lifecycle_case.get("strike"),
        "market": market,
    }
    if any(value in (None, "") for value in required.values()):
        review_reasons.add(
            "legacy_case_contract_mapping_incomplete"
        )
        return None
    try:
        canonical_case = {
            **build_lifecycle_case(
                account=str(required["account"]),
                broker=str(required["broker"]),
                contract_key=str(required["contract_key"]),
                position_side=str(required["position_side"]),
                expiration_ymd=str(required["expiration_ymd"]),
                market=market,
                target_contracts_by_lot=(
                    target_contracts_by_lot
                ),
                futu_account_id=futu_account_ids[0],
            ),
            "market": market,
            "symbol": symbol,
            "option_type": str(
                required["option_type"]
            ).strip().lower(),
            "strike": float(required["strike"]),
            "currency": lifecycle_case.get("currency"),
            "multiplier": float(
                lifecycle_case.get("multiplier") or 100
            ),
        }
    except (TypeError, ValueError, OverflowError) as exc:
        review_reasons.add(
            "legacy_case_mapping_invalid:" + str(exc)
        )
        return None
    canonical_case_id = str(
        canonical_case.get("case_id") or ""
    )
    canonical_policy = {
        **timing_policy,
        "case_id": canonical_case_id,
    }
    bridge_evidence = [
        {
            "schema_version": (
                "migration_bridge_evidence.v1"
            ),
            "evidence_id": "migration_bridge_"
            + canonical_payload_hash(
                {
                    "legacy_case_id": lifecycle_case.get(
                        "case_id"
                    ),
                    "canonical_case_id": canonical_case_id,
                    "referenced_legacy_evidence_id": (
                        item.get("evidence_id")
                    ),
                }
            )[:24],
            "case_id": canonical_case_id,
            "source_type": "lifecycle_migration",
            "source_event_id": None,
            "evidence_type": "migration_bridge",
            "account": canonical_case.get("account"),
            "symbol": canonical_case.get("symbol"),
            "referenced_legacy_case_id": str(
                lifecycle_case.get("case_id") or ""
            ),
            "referenced_legacy_evidence_id": str(
                item.get("evidence_id") or ""
            ),
            "allocating": False,
        }
        for item in evidence
        if str(item.get("evidence_id") or "").strip()
    ]
    return {
        "schema_version": "legacy_lifecycle_upgrade.v1",
        "legacy_case_id": str(
            lifecycle_case.get("case_id") or ""
        ),
        "canonical_case": canonical_case,
        "timing_policy": canonical_policy,
        "bridge_evidence": bridge_evidence,
    }


def apply_lifecycle_migration_manifest(
    repo: Any,
    *,
    manifest: dict[str, Any],
    apply_changes: bool = False,
) -> dict[str, Any]:
    payload = dict(manifest or {})
    if str(payload.get("schema_version") or "") != MIGRATION_SCHEMA:
        raise ValueError("lifecycle migration manifest schema is invalid")
    rows = [
        dict(item)
        for item in payload.get("rows") or []
        if isinstance(item, dict)
    ]
    body = {
        "schema_version": MIGRATION_SCHEMA,
        "rows": rows,
    }
    manifest_hash = canonical_payload_hash(body)
    if str(payload.get("manifest_hash") or "") != manifest_hash:
        raise ValueError("lifecycle migration manifest hash mismatch")
    selected = [item for item in rows if bool(item.get("selected"))]
    for item in selected:
        if str(item.get("mapping_status") or "") != "exact":
            raise ValueError(
                "migration_needs_review rows cannot be applied: "
                f"{item.get('target_key')}"
            )
    if not apply_changes:
        return {
            "schema_version": "lifecycle_migration_apply_result.v1",
            "status": "dry_run",
            "manifest_hash": manifest_hash,
            "selected_count": len(selected),
            "would_apply_target_keys": [
                item.get("target_key") for item in selected
            ],
            "applied_count": 0,
            "existing_count": 0,
        }
    results = [
        _apply_manifest_row(
            repo,
            row=item,
            manifest_hash=manifest_hash,
        )
        for item in selected
    ]
    return {
        "schema_version": "lifecycle_migration_apply_result.v1",
        "status": "applied",
        "manifest_hash": manifest_hash,
        "selected_count": len(selected),
        "applied_count": sum(
            1 for item in results if item["receipt_created"]
        ),
        "existing_count": sum(
            1 for item in results if not item["receipt_created"]
        ),
        "results": results,
    }


def select_lifecycle_migration_targets(
    manifest: dict[str, Any],
    *,
    target_keys: list[str],
) -> dict[str, Any]:
    selected = {
        str(item or "").strip()
        for item in target_keys
        if str(item or "").strip()
    }
    payload = dict(manifest or {})
    rows = [
        {
            **dict(item),
            "selected": (
                str(item.get("target_key") or "").strip()
                in selected
            ),
        }
        for item in payload.get("rows") or []
        if isinstance(item, dict)
    ]
    known = {
        str(item.get("target_key") or "").strip()
        for item in rows
    }
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError(
            "unknown lifecycle migration targets: "
            + ",".join(unknown)
        )
    body = {
        "schema_version": MIGRATION_SCHEMA,
        "rows": rows,
    }
    return {
        **body,
        "manifest_hash": canonical_payload_hash(body),
        "row_count": len(rows),
        "selected_count": len(selected),
        "exact_count": sum(
            1
            for item in rows
            if item.get("mapping_status") == "exact"
        ),
        "review_count": sum(
            1
            for item in rows
            if item.get("mapping_status") != "exact"
        ),
    }


def _apply_manifest_row(
    repo: Any,
    *,
    row: dict[str, Any],
    manifest_hash: str,
) -> dict[str, Any]:
    target_key = str(row.get("target_key") or "").strip()
    row_hash = canonical_payload_hash(
        {
            key: value
            for key, value in row.items()
            if key != "selected"
        }
    )

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "lifecycle migration requires SQLite transaction authority"
            )
        existing_receipt = next(
            (
                item
                for item in (
                    sqlite_repo.list_trade_lifecycle_migration_receipts(
                        conn=conn
                    )
                )
                if str(item.get("target_key") or "").strip()
                == target_key
            ),
            None,
        )
        if isinstance(existing_receipt, dict):
            if str(
                existing_receipt.get("row_hash") or ""
            ) != row_hash:
                raise ValueError(
                    "lifecycle migration receipt row conflict"
                )
            return {
                "target_key": target_key,
                "receipt_created": False,
                "source_claims_created": [],
                "evidence_bindings_created": [],
                "timing_policy_created": False,
                "outbox_rows_created": [],
            }
        current_state_hash = _current_inventory_state_hash(
            sqlite_repo,
            row=row,
            conn=conn,
        )
        if (
            str(row.get("inventory_state_hash") or "")
            != current_state_hash
        ):
            raise ValueError(
                f"lifecycle migration source drift: {target_key}"
            )
        legacy_upgrade = (
            dict(row.get("legacy_upgrade") or {})
            if isinstance(row.get("legacy_upgrade"), dict)
            else {}
        )
        canonical_case = (
            dict(legacy_upgrade.get("canonical_case") or {})
            if isinstance(
                legacy_upgrade.get("canonical_case"),
                dict,
            )
            else {}
        )
        canonical_case_id = str(
            canonical_case.get("case_id") or ""
        ).strip()
        case_created = False
        bridge_created: list[bool] = []
        legacy_superseded = False
        if legacy_upgrade:
            if not canonical_case_id:
                raise ValueError(
                    "legacy migration canonical case is missing"
                )
            if bool(
                legacy_upgrade.get(
                    "reuse_existing_canonical_case"
                )
            ):
                if not isinstance(
                    sqlite_repo.get_trade_lifecycle_case(
                        canonical_case_id,
                        conn=conn,
                    ),
                    dict,
                ):
                    raise ValueError(
                        "legacy migration canonical case is missing"
                    )
            else:
                case_created = (
                    sqlite_repo.insert_trade_lifecycle_case_once(
                        canonical_case,
                        conn=conn,
                    )
                )
            sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                case_id=canonical_case_id,
                futu_account_id=str(
                    canonical_case.get("futu_account_id")
                    or ""
                ),
                conn=conn,
            )
            bridge_created = [
                sqlite_repo.insert_trade_lifecycle_evidence_once(
                    dict(item),
                    conn=conn,
                )
                for item in legacy_upgrade.get(
                    "bridge_evidence"
                )
                or []
                if isinstance(item, dict)
            ]
            legacy_superseded = (
                sqlite_repo.supersede_trade_lifecycle_case_once(
                    case_id=str(row.get("case_id") or ""),
                    superseded_by_case_id=canonical_case_id,
                    conn=conn,
                )
            )
        planned_binding = str(
            row.get("planned_futu_account_binding") or ""
        ).strip()
        if planned_binding:
            sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                case_id=str(row.get("case_id") or ""),
                futu_account_id=planned_binding,
                conn=conn,
            )
        evidence_bindings_created = [
            sqlite_repo.bind_trade_lifecycle_evidence_case_once(
                evidence_id=str(item.get("evidence_id") or ""),
                case_id=str(item.get("case_id") or ""),
                conn=conn,
            )
            for item in row.get("planned_evidence_bindings") or []
            if isinstance(item, dict)
        ]
        claims_created = [
            sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                dict(item),
                conn=conn,
            )
            for item in row.get("planned_source_claims") or []
            if isinstance(item, dict)
        ]
        timing_created = False
        effective_timing_policy = (
            dict(legacy_upgrade.get("timing_policy") or {})
            if legacy_upgrade
            and isinstance(
                legacy_upgrade.get("timing_policy"),
                dict,
            )
            else (
                dict(row.get("timing_policy") or {})
                if isinstance(row.get("timing_policy"), dict)
                else {}
            )
        )
        effective_timing_case_id = (
            canonical_case_id
            if legacy_upgrade
            else str(row.get("case_id") or "")
        )
        if (
            row.get("kind") == "lifecycle_case"
            and effective_timing_policy
            and (
                legacy_upgrade
                or not bool(row.get("timing_policy_bound"))
            )
        ):
            timing_created = (
                sqlite_repo.insert_trade_lifecycle_timing_policy_once(
                    effective_timing_policy,
                    conn=conn,
                )
            )
        if (
            row.get("kind") == "lifecycle_case"
            and not bool(row.get("legacy_terminal_frozen"))
            and not isinstance(
                sqlite_repo.get_trade_lifecycle_timing_policy(
                    effective_timing_case_id,
                    conn=conn,
                ),
                dict,
            )
        ):
            raise ValueError(
                "lifecycle migration timing binding missing"
            )
        outbox_created: list[bool] = []
        if bool(row.get("suppress_option_leg_closed", True)):
            outbox_created.append(
                sqlite_repo.insert_trade_lifecycle_notification_once(
                    _suppression_intent(row),
                    conn=conn,
                )
            )
        if bool(row.get("seed_final_intent")):
            outbox_created.append(
                sqlite_repo.insert_trade_lifecycle_notification_once(
                    _final_intent(row),
                    conn=conn,
                )
            )
        receipt = {
            "schema_version": MIGRATION_RECEIPT_SCHEMA,
            "migration_schema": MIGRATION_SCHEMA,
            "target_key": target_key,
            "manifest_hash": manifest_hash,
            "row_hash": row_hash,
            "source_claim_count": len(
                row.get("planned_source_claims") or []
            ),
            "suppression_requested": bool(
                row.get("suppress_option_leg_closed", True)
            ),
            "final_intent_requested": bool(
                row.get("seed_final_intent")
            ),
            "canonical_case_id": canonical_case_id or None,
            "legacy_superseded": bool(legacy_upgrade),
            "explicit_mapping_disposition": (
                dict(row.get("explicit_mapping") or {}).get(
                    "disposition"
                )
                if isinstance(
                    row.get("explicit_mapping"),
                    dict,
                )
                else None
            ),
        }
        receipt_created = (
            sqlite_repo.insert_trade_lifecycle_migration_receipt_once(
                receipt,
                conn=conn,
            )
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "target_key": target_key,
            "receipt_created": receipt_created,
            "source_claims_created": claims_created,
            "evidence_bindings_created": evidence_bindings_created,
            "canonical_case_created": case_created,
            "bridge_evidence_created": bridge_created,
            "legacy_superseded": legacy_superseded,
            "timing_policy_created": timing_created,
            "outbox_rows_created": outbox_created,
        }

    return with_sqlite_repo_transaction(repo, _run)


def _suppression_intent(row: dict[str, Any]) -> dict[str, Any]:
    target_key = str(row.get("target_key") or "")
    case_id = str(
        row.get("notification_case_id")
        or row.get("case_id")
        or target_key
    )
    transition_key = (
        str(row.get("transition_key") or "").strip()
        or (
            f"lifecycle:{case_id}:option_leg_closed"
            if row.get("kind") == "lifecycle_case"
            else f"{target_key}:resolution_confirmed"
        )
    )
    transition_type = (
        "option_leg_closed"
        if row.get("kind") == "lifecycle_case"
        else "resolution_confirmed"
    )
    fingerprint = canonical_state_fingerprint(
        {
            "migration_target": target_key,
            "inventory_state_hash": row.get(
                "inventory_state_hash"
            ),
            "transition_type": transition_type,
        }
    )
    payload = {
        "schema_version": "migration_notification_suppression.v1",
        "case_id": case_id,
        "account": row.get("account"),
        "transition_type": transition_type,
        "migration_target": target_key,
        "broker_deal_key": row.get("broker_deal_key"),
        "state_fingerprint": fingerprint,
    }
    return build_notification_intent(
        case_id=case_id,
        transition_type=transition_type,
        resolution_revision=max(
            1,
            int(row.get("resolution_revision") or 0),
        ),
        transition_key=transition_key,
        state_fingerprint=fingerprint,
        payload=payload,
        status="suppressed",
    )


def _final_intent(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("kind") != "lifecycle_case":
        raise ValueError(
            "only lifecycle cases may seed final migration intent"
        )
    resolution = dict(row.get("resolution") or {})
    if any(
        int(value or 0) > 0
        for value in (
            resolution.get("remaining_contracts_by_lot") or {}
        ).values()
    ):
        raise ValueError(
            "unresolved lifecycle case cannot seed final intent"
        )
    case_id = str(row.get("case_id") or "")
    revision = max(1, int(row.get("resolution_revision") or 1))
    fingerprint = canonical_state_fingerprint(
        {
            "migration_target": row.get("target_key"),
            "inventory_state_hash": row.get(
                "inventory_state_hash"
            ),
            "transition_type": "resolution_confirmed",
        }
    )
    payload = {
        "schema_version": "migration_final_notification.v1",
        "case_id": case_id,
        "account": row.get("account"),
        "transition_type": "resolution_confirmed",
        "resolution_revision": revision,
        "state_fingerprint": fingerprint,
        "resolved_contracts_by_terminal_type": (
            resolution.get("resolved_contracts_by_terminal_type")
            or {}
        ),
    }
    return build_notification_intent(
        case_id=case_id,
        transition_type="resolution_confirmed",
        resolution_revision=revision,
        transition_key=(
            f"lifecycle:{case_id}:resolution_confirmed"
        ),
        state_fingerprint=fingerprint,
        payload=payload,
    )


def _normal_close_inventory_rows(
    events: list[dict[str, Any]],
    notifications: list[dict[str, Any]],
    *,
    void_event_ids: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    review: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event_type") or "").strip().lower() != "close":
            continue
        event_id = str(event.get("event_id") or "").strip()
        if event_id in void_event_ids:
            continue
        raw = (
            dict(event.get("raw_payload") or {})
            if isinstance(event.get("raw_payload"), dict)
            else {}
        )
        account = _unique_normal_close_account(event, raw)
        futu_account_id = str(
            raw.get("futu_account_id") or ""
        ).strip()
        deal_id = str(raw.get("source_deal_id") or "").strip()
        if (
            not futu_account_id
            and not deal_id
            and _is_internal_non_broker_close(event, raw)
        ):
            continue
        if not account or not futu_account_id or not deal_id:
            review.append(event)
            continue
        key = f"futu:{account}:{futu_account_id}:{deal_id}"
        grouped.setdefault(key, []).append(event)
    rows: list[dict[str, Any]] = []
    for broker_key, group in sorted(grouped.items()):
        account = broker_key.split(":", 3)[1]
        target_key = f"close:{broker_key}"
        transition_key = f"{target_key}:resolution_confirmed"
        current = [
            item
            for item in notifications
            if (
                str(item.get("transition_key") or "")
                == transition_key
                or str(item.get("case_id") or "") == target_key
            )
        ]
        state = {
            "broker_deal_key": broker_key,
            "events": sorted(
                group,
                key=lambda item: str(item.get("event_id") or ""),
            ),
            "notifications": current,
        }
        rows.append(
            {
                "target_key": target_key,
                "kind": "normal_close",
                "selected": False,
                "mapping_status": "exact",
                "review_reason_codes": [],
                "notification_case_id": target_key,
                "account": account,
                "broker_deal_key": broker_key,
                "transition_key": transition_key,
                "event_ids": sorted(
                    str(item.get("event_id") or "")
                    for item in group
                ),
                "split_event_count": len(group),
                "existing_receipt_states": [
                    {
                        "outbox_id": item.get("outbox_id"),
                        "status": item.get("status"),
                    }
                    for item in current
                ],
                "planned_source_claims": [],
                "suppress_option_leg_closed": True,
                "seed_final_intent": False,
                "resolution_revision": 1,
                "inventory_state_hash": canonical_payload_hash(
                    state
                ),
            }
        )
    for event in review:
        event_id = str(event.get("event_id") or "")
        rows.append(
            {
                "target_key": f"normal-close-review:{event_id}",
                "kind": "normal_close",
                "selected": False,
                "mapping_status": "needs_review",
                "review_reason_codes": [
                    "canonical_broker_deal_key_missing"
                ],
                "event_ids": [event_id],
                "planned_source_claims": [],
                "suppress_option_leg_closed": False,
                "seed_final_intent": False,
                "inventory_state_hash": canonical_payload_hash(
                    {"event": event}
                ),
            }
        )
    return rows


def _unique_normal_close_account(
    event: dict[str, Any],
    raw: dict[str, Any],
) -> str:
    contract_key = (
        dict(event.get("contract_key") or {})
        if isinstance(event.get("contract_key"), dict)
        else {}
    )
    candidates = {
        value
        for item in (
            event.get("account"),
            contract_key.get("account"),
            raw.get("close_target_account"),
            raw.get("internal_account"),
        )
        for value in [str(item or "").strip().lower()]
        if value
    }
    if len(candidates) != 1:
        return ""
    return next(iter(candidates))


def _is_internal_non_broker_close(
    event: dict[str, Any],
    raw: dict[str, Any],
) -> bool:
    source_type = str(
        raw.get("source_type")
        or event.get("source_type")
        or ""
    ).strip().lower()
    return source_type in {
        "manual_trade_event",
        "system_trade_event",
    }


def _mark_source_claim_ambiguities(
    rows: list[dict[str, Any]],
    *,
    existing_claims: list[dict[str, Any]],
) -> None:
    existing_by_key = {
        str(item.get("source_key") or "").strip(): item
        for item in existing_claims
        if str(item.get("source_key") or "").strip()
    }
    planned_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for claim in row.get("planned_source_claims") or []:
            if not isinstance(claim, dict):
                continue
            source_key = str(claim.get("source_key") or "").strip()
            if source_key:
                planned_by_key.setdefault(source_key, []).append(row)
            existing = existing_by_key.get(source_key)
            if existing is not None and not _same_source_claim_owner(
                existing,
                claim,
            ):
                _mark_inventory_review(
                    row,
                    "source_claim_existing_owner_conflict",
                )
    for claim_rows in planned_by_key.values():
        if len(claim_rows) <= 1:
            continue
        for row in claim_rows:
            _mark_inventory_review(
                row,
                "source_claim_owner_ambiguous",
            )


def _same_source_claim_owner(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    return all(
        str(left.get(key) or "").strip()
        == str(right.get(key) or "").strip()
        for key in (
            "source_key",
            "case_id",
            "owner_evidence_id",
            "source_role",
            "source_payload_hash",
        )
    )


def _mark_inventory_review(
    row: dict[str, Any],
    reason_code: str,
) -> None:
    reasons = {
        str(item or "").strip()
        for item in row.get("review_reason_codes") or []
        if str(item or "").strip()
    }
    reasons.add(str(reason_code))
    row["review_reason_codes"] = sorted(reasons)
    row["mapping_status"] = "needs_review"


def _validate_case_broker_identity(
    lifecycle_case: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    review_reasons: set[str],
) -> None:
    consumable = [
        item
        for item in evidence
        if _migration_source_role(item) is not None
    ]
    account = str(
        lifecycle_case.get("account") or ""
    ).strip().lower()
    futu_account_ids: set[str] = set()
    for item in consumable:
        source_key = str(
            item.get("source_event_id") or ""
        ).strip()
        binding = _canonical_futu_binding(source_key)
        evidence_account = str(
            item.get("account") or ""
        ).strip().lower()
        futu_account_id = str(
            item.get("futu_account_id") or ""
        ).strip()
        if (
            binding is None
            or binding != (account, futu_account_id)
            or evidence_account != account
        ):
            review_reasons.add("broker_account_binding_mismatch")
            continue
        futu_account_ids.add(futu_account_id)
        if not _evidence_contract_matches_case(
            item,
            lifecycle_case=lifecycle_case,
            source_role=str(_migration_source_role(item) or ""),
        ):
            review_reasons.add("broker_contract_identity_mismatch")
    if len(futu_account_ids) > 1:
        review_reasons.add("futu_account_identity_ambiguous")


def _validate_case_allocations(
    allocations: list[dict[str, Any]],
    *,
    events_by_id: dict[str, dict[str, Any]],
    review_reasons: set[str],
) -> None:
    for allocation in allocations:
        event_id = str(
            allocation.get("canonical_terminal_event_id") or ""
        ).strip()
        event = events_by_id.get(event_id)
        if event is None:
            review_reasons.add("terminal_event_missing")
            continue
        try:
            allocation_contracts = int(
                allocation.get("contracts_allocated") or 0
            )
            event_contracts = int(event.get("contracts") or 0)
        except (TypeError, ValueError, OverflowError):
            review_reasons.add("terminal_allocation_invalid")
            continue
        if (
            str(event.get("event_type") or "").strip().lower()
            != str(
                allocation.get("terminal_type") or ""
            ).strip().lower()
            or str(event.get("target_lot_id") or "").strip()
            != str(
                allocation.get("target_lot_id") or ""
            ).strip()
            or allocation_contracts <= 0
            or event_contracts != allocation_contracts
        ):
            review_reasons.add("terminal_allocation_event_mismatch")


def _validate_existing_case_claims(
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    review_reasons: set[str],
) -> None:
    evidence_by_id = {
        str(item.get("evidence_id") or "").strip(): item
        for item in evidence
        if str(item.get("evidence_id") or "").strip()
    }
    for claim in claims:
        owner_id = str(
            claim.get("owner_evidence_id") or ""
        ).strip()
        owner = evidence_by_id.get(owner_id)
        if (
            owner is None
            or str(claim.get("case_id") or "").strip()
            != str(owner.get("case_id") or "").strip()
            or str(claim.get("source_key") or "").strip()
            != str(owner.get("source_event_id") or "").strip()
            or str(claim.get("source_role") or "").strip()
            != str(_migration_source_role(owner) or "")
            or not isinstance(claim.get("source_payload"), dict)
            or str(claim.get("source_payload_hash") or "").strip()
            != canonical_payload_hash(claim["source_payload"])
        ):
            review_reasons.add("existing_source_claim_owner_invalid")


def _canonical_futu_binding(
    source_key: str,
) -> tuple[str, str] | None:
    parts = str(source_key or "").split(":", 3)
    if (
        len(parts) != 4
        or parts[0].lower() != "futu"
        or not all(parts[1:])
    ):
        return None
    return parts[1].lower(), parts[2]


def _evidence_contract_matches_case(
    evidence: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    source_role: str,
) -> bool:
    keys = ["symbol"]
    if source_role == "option_anchor":
        keys.extend(
            (
                "option_type",
                "position_side",
                "expiration_ymd",
            )
        )
    for key in keys:
        evidence_value = str(
            evidence.get(key) or ""
        ).strip().lower()
        case_value = str(
            lifecycle_case.get(key) or ""
        ).strip().lower()
        if not evidence_value or not case_value or evidence_value != case_value:
            return False
    if source_role != "option_anchor":
        return True
    try:
        evidence_strike = Decimal(str(evidence.get("strike")))
        case_strike = Decimal(str(lifecycle_case.get("strike")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        evidence_strike.is_finite()
        and case_strike.is_finite()
        and evidence_strike == case_strike
    )


def _current_inventory_state_hash(
    sqlite_repo: Any,
    *,
    row: dict[str, Any],
    conn: Any,
) -> str:
    if row.get("kind") == "normal_close":
        event_ids = {
            str(item)
            for item in row.get("event_ids") or []
        }
        events = [
            item
            for item in sqlite_repo.list_trade_events(conn=conn)
            if str(item.get("event_id") or "") in event_ids
        ]
        notifications = [
            item
            for item in (
                sqlite_repo.list_trade_lifecycle_notifications(
                    conn=conn
                )
            )
            if (
                str(item.get("transition_key") or "")
                == str(row.get("transition_key") or "")
                or str(item.get("case_id") or "")
                == str(row.get("notification_case_id") or "")
            )
        ]
        if row.get("mapping_status") != "exact":
            return canonical_payload_hash(
                {"event": events[0] if len(events) == 1 else None}
            )
        return canonical_payload_hash(
            {
                "broker_deal_key": row.get("broker_deal_key"),
                "events": sorted(
                    events,
                    key=lambda item: str(
                        item.get("event_id") or ""
                    ),
                ),
                "notifications": notifications,
            }
        )
    if isinstance(row.get("explicit_mapping"), dict):
        all_cases = [
            dict(item)
            for item in sqlite_repo.list_trade_lifecycle_cases(
                conn=conn
            )
            if isinstance(item, dict)
        ]
        all_evidence = [
            dict(item)
            for item in sqlite_repo.list_trade_lifecycle_evidence(
                conn=conn
            )
            if isinstance(item, dict)
        ]
        all_allocations = [
            dict(item)
            for item in sqlite_repo.list_trade_lifecycle_allocations(
                conn=conn
            )
            if isinstance(item, dict)
        ]
        all_events = [
            dict(item)
            for item in sqlite_repo.list_trade_events(conn=conn)
            if isinstance(item, dict)
        ]
        lots = {
            str(item.get("record_id") or ""): dict(
                item.get("fields") or {}
            )
            for item in sqlite_repo.list_position_lots(conn=conn)
            if isinstance(item, dict)
            and str(item.get("record_id") or "")
            and isinstance(item.get("fields"), dict)
        }
        all_claims = [
            dict(item)
            for item in (
                sqlite_repo.list_trade_lifecycle_source_consumptions(
                    conn=conn
                )
            )
            if isinstance(item, dict)
        ]
        all_notifications = [
            dict(item)
            for item in (
                sqlite_repo.list_trade_lifecycle_notifications(
                    conn=conn
                )
            )
            if isinstance(item, dict)
        ]
        timing_policies = {
            str(item.get("case_id") or ""): dict(item)
            for item in (
                sqlite_repo.list_trade_lifecycle_timing_policies(
                    conn=conn
                )
            )
            if isinstance(item, dict)
            and str(item.get("case_id") or "")
        }
        void_ids = sorted(
            {
                target
                for item in all_events
                for target in [valid_void_target_event_id(item)]
                if target
            }
        )
        return canonical_payload_hash(
            _explicit_inventory_state(
                mapping=dict(row["explicit_mapping"]),
                case_ids=[
                    str(item)
                    for item in row.get(
                        "explicit_state_case_ids"
                    )
                    or []
                ],
                evidence_ids=[
                    str(item)
                    for item in row.get(
                        "explicit_state_evidence_ids"
                    )
                    or []
                ],
                terminal_event_ids=[
                    str(item)
                    for item in row.get(
                        "explicit_state_terminal_event_ids"
                    )
                    or []
                ],
                target_lot_ids=[
                    str(item)
                    for item in row.get(
                        "explicit_state_target_lot_ids"
                    )
                    or []
                ],
                source_keys=[
                    str(item)
                    for item in row.get(
                        "explicit_state_source_keys"
                    )
                    or []
                ],
                all_cases=all_cases,
                all_evidence=all_evidence,
                all_allocations=all_allocations,
                all_events=all_events,
                lot_fields_by_id=lots,
                all_claims=all_claims,
                all_notifications=all_notifications,
                timing_policies=timing_policies,
                void_ids=void_ids,
            )
        )
    case_id = str(row.get("case_id") or "")
    lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
        case_id,
        conn=conn,
    )
    evidence = sqlite_repo.list_trade_lifecycle_evidence(
        case_id=case_id,
        conn=conn,
    )
    allocations = sqlite_repo.list_trade_lifecycle_allocations(
        case_id=case_id,
        conn=conn,
    )
    events = sqlite_repo.list_trade_events(conn=conn)
    void_ids = sorted(
        {
            target
            for item in events
            for target in [valid_void_target_event_id(item)]
            if target
        }
    )
    claims = sqlite_repo.list_trade_lifecycle_source_consumptions(
        case_id=case_id,
        conn=conn,
    )
    notifications = sqlite_repo.list_trade_lifecycle_notifications(
        case_id=case_id,
        conn=conn,
    )
    return canonical_payload_hash(
        {
            "case": lifecycle_case,
            "evidence": sorted(
                evidence,
                key=lambda item: str(
                    item.get("evidence_id") or ""
                ),
            ),
            "allocations": sorted(
                allocations,
                key=lambda item: str(
                    item.get("allocation_id") or ""
                ),
            ),
            "effective_void_event_ids": void_ids,
            "claims": sorted(
                claims,
                key=lambda item: str(
                    item.get("source_key") or ""
                ),
            ),
            "notifications": sorted(
                notifications,
                key=lambda item: str(
                    item.get("outbox_id") or ""
                ),
            ),
            "timing_policy": (
                sqlite_repo.get_trade_lifecycle_timing_policy(
                    case_id,
                    conn=conn,
                )
            ),
        }
    )


def _migration_source_role(
    evidence: dict[str, Any],
) -> str | None:
    evidence_type = str(
        evidence.get("evidence_type") or ""
    ).strip().lower()
    if evidence_type == "option_zero_price_close":
        return "option_anchor"
    if evidence_type == "stock_settlement_leg":
        return "stock_settlement"
    return None


def _is_canonical_futu_key(value: str) -> bool:
    parts = str(value or "").split(":", 3)
    return (
        len(parts) == 4
        and parts[0].lower() == "futu"
        and all(parts[1:])
    )


__all__ = [
    "EXPLICIT_MAPPING_SCHEMA",
    "MIGRATION_RECEIPT_SCHEMA",
    "MIGRATION_SCHEMA",
    "apply_lifecycle_migration_manifest",
    "build_lifecycle_migration_inventory",
    "select_lifecycle_migration_targets",
]
