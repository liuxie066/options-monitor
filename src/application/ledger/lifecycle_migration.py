from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.lifecycle_allocation import resolve_allocations
from domain.domain.option_lifecycle import build_lifecycle_case
from domain.domain.symbol_identity import symbol_market
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


def build_lifecycle_migration_inventory(repo: Any) -> dict[str, Any]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
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
    for lifecycle_case in sorted(
        cases,
        key=lambda item: str(item.get("case_id") or ""),
    ):
        case_id = str(lifecycle_case.get("case_id") or "").strip()
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
    rows.extend(_normal_close_inventory_rows(events, notifications))
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
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    review: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event_type") or "").strip().lower() != "close":
            continue
        raw = (
            dict(event.get("raw_payload") or {})
            if isinstance(event.get("raw_payload"), dict)
            else {}
        )
        account = str(event.get("account") or "").strip().lower()
        futu_account_id = str(
            raw.get("futu_account_id") or ""
        ).strip()
        deal_id = str(raw.get("source_deal_id") or "").strip()
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
    "MIGRATION_RECEIPT_SCHEMA",
    "MIGRATION_SCHEMA",
    "apply_lifecycle_migration_manifest",
    "build_lifecycle_migration_inventory",
    "select_lifecycle_migration_targets",
]
