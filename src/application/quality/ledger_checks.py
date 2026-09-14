from __future__ import annotations

from collections import defaultdict
from typing import Any

from domain.domain.trade_execution import ledger_execution_event_set_is_complete
from src.application.ledger.api import (
    compare_projection_lots,
    execution_identity_from_input,
    lifecycle_account_coherent_facts,
    proven_lifecycle_terminal_events,
    stock_claim_matches_lifecycle_terminal_events,
    project_trade_event_log,
    trade_event_log,
)
from src.application.quality.model import check_result, dataset_status, evidence_ref
from src.application.trades.deal_identity import (
    active_ledger_events,
    ledger_event_economic_fingerprint,
    structured_deal_ids_from_ledger_event,
    structured_deal_keys_from_ledger_event,
)


def _account_from_event(event: dict[str, Any]) -> str:
    key = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
    raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    return str(event.get("account") or key.get("account") or raw.get("internal_account") or "").strip().lower()


def _account_from_lot(row: Any) -> str:
    payload = row.to_dict() if hasattr(row, "to_dict") else row
    fields = payload.get("fields") if isinstance(payload, dict) and isinstance(payload.get("fields"), dict) else {}
    return str(fields.get("account") or "").strip().lower()


def _lifecycle_stock_event(event: dict[str, Any]) -> bool:
    raw = event.get("raw_payload") or {}
    return event.get("event_type") in {"assignment", "exercise"} and (
        raw.get("schema_version") == "lifecycle_terminal_event.v2" or bool(raw.get("allocation_id"))
    )


def _stock_source_identity(event: dict[str, Any]) -> str:
    settlement = (event.get("raw_payload") or {}).get("stock_settlement") or {}
    if not isinstance(settlement, dict):
        return ""
    parts = str(settlement.get("source_event_id") or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "futu" or not all(parts[1:]):
        return ""
    # A lifecycle source claim uses the existing REAL Futu source-key contract.
    # This key only locates the complete group; the claim proof admits the event.
    return execution_identity_from_input({
        "broker_account_ref": {"broker_id": "futu", "environment": "REAL", "external_account_id": parts[2]},
        "external_id_namespace": "futu.deal", "external_execution_id": parts[3],
    })


def _stock_source_groups(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        keys = structured_deal_keys_from_ledger_event(event, include_legacy_execution_identity=True)
        stock_key = _stock_source_identity(event)
        if stock_key:
            keys.add(stock_key)
        for key in keys:
            if key.startswith("execution:"):
                groups[key].append(event)
    return groups


def _proven_lifecycle_stock_event_ids(repo: Any, events: list[dict[str, Any]]) -> set[str]:
    """Exclude only complete stock-source groups proven by coherent ledger facts."""
    proven_ids: set[str] = set()
    # Physical groups span the whole log, including accounts outside the request.
    accounts = {_account_from_event(event) for event in events if _lifecycle_stock_event(event)} - {""}
    if not accounts:
        return proven_ids
    groups = _stock_source_groups(events)
    for account in sorted(accounts):
        try:
            facts = lifecycle_account_coherent_facts(repo, account=account)
        except Exception:
            # An unavailable historical reader cannot authorize an exemption.
            continue
        try:
            snapshot_groups = _stock_source_groups(active_ledger_events(facts["trade_events"]))
            claims = facts.get("account_lifecycle_source_consumptions") or []
            for case in facts.get("account_lifecycle_cases") or []:
                terminal = proven_lifecycle_terminal_events(case, facts=facts)
                if not terminal:
                    continue
                for claim in claims:
                    if (claim.get("case_id") != case["case_id"]
                            or claim.get("source_role") != "stock_settlement"
                            or not stock_claim_matches_lifecycle_terminal_events(claim, terminal, case=case)):
                        continue
                    group = [event for event in terminal if (
                        _lifecycle_stock_event(event)
                        and (event.get("raw_payload") or {}).get("evidence_id") == claim["owner_evidence_id"]
                    )]
                    if not group:
                        continue
                    key = _stock_source_identity(group[0])
                    # Reject missing/extra active effects, concurrent voids, and changed
                    # content, even if the earlier read and snapshot share event IDs.
                    expected = sorted(group, key=lambda row: row["event_id"])
                    if (key and sorted(groups.get(key, []), key=lambda row: row["event_id"]) == expected
                            and sorted(snapshot_groups.get(key, []), key=lambda row: row["event_id"]) == expected):
                        proven_ids.update(row["event_id"] for row in group)
        except (KeyError, TypeError, ValueError, OverflowError, RuntimeError):
            # Missing/invalid historical proof leaves these events explicitly untrusted.
            continue
    return proven_ids


def build_ledger_datasets(
    *,
    repo: Any,
    accounts: list[str],
    market: str,
    observed_at_utc: str,
) -> list[dict[str, Any]]:
    events = trade_event_log(repo)
    projection = project_trade_event_log(events)
    current_lots = repo.list_position_lots()
    active_events = active_ledger_events(events)
    proven_stock_ids = _proven_lifecycle_stock_event_ids(repo, active_events)
    deal_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities = [
        structured_deal_keys_from_ledger_event(event, include_legacy_execution_identity=True)
        for event in active_events
    ]
    canonical_by_alias: dict[str, set[str]] = defaultdict(set)
    for keys in identities:
        canonical = {key for key in keys if key.startswith("execution:")}
        for key in keys:
            canonical_by_alias[key].update(canonical)
    for event, keys in zip(active_events, identities):
        if _lifecycle_stock_event(event):
            if event.get("event_id") not in proven_stock_ids:
                deal_rows[f"unscoped:{_account_from_event(event)}:{event.get('event_id') or 'missing'}"].append(event)
            continue
        canonical = set().union(*(canonical_by_alias[key] for key in keys))
        if len(canonical) == 1:
            deal_rows[next(iter(canonical))].append(event)
        elif keys:
            # Ambiguous aliases remain separate; completion cannot prove a mixed execution.
            deal_rows[sorted(keys)[0]].append(event)
        else:
            deal_ids = structured_deal_ids_from_ledger_event(event)
            raw = event.get("raw_payload") or {}
            if not deal_ids and any(field in raw for field in ("broker_deal_completion", "execution_input")):
                deal_ids = {str(event.get("event_id") or "missing")}
            for deal_id in deal_ids:
                deal_rows[f"unscoped:{_account_from_event(event)}:{deal_id}"].append(event)

    out: list[dict[str, Any]] = []
    for account in accounts:
        projected_for_account = [row for row in projection.lots if _account_from_lot(row) == account]
        current_for_account = [row for row in current_lots if _account_from_lot(row) == account]
        account_diagnostics = [
            item
            for item in projection.diagnostics
            if not getattr(item, "details", None)
            or str((getattr(item, "details", {}) or {}).get("account") or "").strip().lower() in {"", account}
        ]
        comparison = compare_projection_lots(
            projected_lots=projected_for_account,
            current_lots=current_for_account,
            diagnostics=account_diagnostics,
        )
        mismatch_count = sum(
            count for key, count in comparison["summary"].items() if key != "matched"
        )
        replay_evidence = evidence_ref(
            kind="ledger-full-replay",
            observed_at_utc=observed_at_utc,
            value={
                "account": account,
                "event_count": sum(1 for item in events if _account_from_event(item) == account),
                "projected_lot_count": len(projected_for_account),
                "materialized_lot_count": len(current_for_account),
                "comparison_summary": comparison["summary"],
            },
            artifact_ref=f"om-evidence:ledger-replay:{account}",
        )
        replay_ok = mismatch_count == 0
        replay_check = check_result(
            check_id="OM-LED-001",
            status="pass" if replay_ok else "fail",
            scope={"account": account, "market": market},
            observed_at_utc=observed_at_utc,
            reason_code="LEDGER_REPLAY_MATCHED" if replay_ok else "LEDGER_REPLAY_MISMATCH",
            message=(
                "Full trade-event replay matches materialized position lots."
                if replay_ok
                else "Full trade-event replay does not match materialized position lots."
            ),
            observed={"mismatch_count": mismatch_count, "summary": comparison["summary"]},
            expected={"mismatch_count": 0},
            evidence_refs=[replay_evidence],
        )

        conflict_deal_ids: list[str] = []
        duplicate_deal_ids: list[str] = []
        for deal_id, rows in deal_rows.items():
            scoped = [item for item in rows if _account_from_event(item) == account]
            if not scoped:
                continue
            if not deal_id.startswith("unscoped:") and ledger_execution_event_set_is_complete(rows):
                continue
            fingerprints = {
                (*ledger_event_economic_fingerprint(item), str(item.get("contracts")))
                for item in rows
            }
            if len(fingerprints) > 1:
                conflict_deal_ids.append(deal_id)
            else:
                duplicate_deal_ids.append(deal_id)
        projection_error_count = sum(
            1
            for item in account_diagnostics
            if str(getattr(item, "severity", "")).lower() == "error"
        )
        has_conflict = bool(
            conflict_deal_ids or duplicate_deal_ids or projection_error_count
        )
        conservation_check = check_result(
            check_id="OM-LED-002",
            status="fail" if has_conflict else "pass",
            scope={"account": account, "market": market},
            observed_at_utc=observed_at_utc,
            reason_code="LEDGER_CONFLICT_DETECTED" if has_conflict else "LEDGER_IDENTITIES_CONSERVED",
            message=(
                "Duplicate broker identity, economic conflict, or projection conservation error detected."
                if has_conflict
                else "Broker identities and projected quantities are conserved."
            ),
            observed={
                "duplicate_broker_identity_count": len(duplicate_deal_ids),
                "economic_conflict_count": len(conflict_deal_ids),
                "projection_error_count": projection_error_count,
            },
            expected={
                "duplicate_broker_identity_count": 0,
                "economic_conflict_count": 0,
                "projection_error_count": 0,
            },
            evidence_refs=[replay_evidence],
        )
        failed_checks = [item for item in (replay_check, conservation_check) if item["status"] == "fail"]
        out.append(
            dataset_status(
                dataset_id="om.ledger_projection",
                scope={"account": account, "market": market},
                status="untrusted" if failed_checks else "trusted",
                as_of_utc=observed_at_utc,
                checks=[replay_check, conservation_check],
                evidence_refs=[replay_evidence],
                usable_for=[] if failed_checks else ["option_position_report", "lifecycle", "close_advice"],
                blocked_consumers=(
                    ["option_position_report", "lifecycle", "close_advice"] if failed_checks else []
                ),
                blocked_by=[item["check_id"] for item in failed_checks],
                reason_codes=[item["reason_code"] for item in failed_checks],
            )
        )
    return out


def build_current_ledger_dataset(
    *,
    current_projection: dict[str, Any],
    account: str,
    market: str,
    observed_at_utc: str,
) -> dict[str, Any]:
    trusted = current_projection.get("status") == "trusted"
    reason = str(current_projection.get("reason") or "current_projection_unavailable")
    payload = (
        current_projection.get("payload")
        if isinstance(current_projection.get("payload"), dict)
        else {}
    )
    binding = (
        payload.get("position_binding")
        if isinstance(payload.get("position_binding"), dict)
        else {}
    )
    check = check_result(
        check_id="OM-LED-001",
        status="pass" if trusted else "unknown",
        scope={"account": account, "market": market},
        observed_at_utc=observed_at_utc,
        reason_code=(
            "LEDGER_CURRENT_PROJECTION_TRUSTED"
            if trusted
            else "LEDGER_CURRENT_PROJECTION_UNAVAILABLE"
        ),
        message=(
            "Current ledger projection head, generations, rows, and fingerprint are trusted."
            if trusted
            else "Current ledger projection is unavailable; run the explicit integrity or repair workflow."
        ),
        observed={
            "projection_status": current_projection.get("status"),
            "reason": None if trusted else reason,
            "lot_count": current_projection.get("lot_count", 0),
            "position_source_generation": binding.get(
                "position_source_generation"
            ),
            "position_lots_generation": binding.get(
                "position_lots_generation"
            ),
            "position_lots_fingerprint": binding.get(
                "position_lots_fingerprint"
            ),
        },
        expected={"projection_status": "trusted"},
        evidence_refs=[],
    )
    return dataset_status(
        dataset_id="om.ledger_projection",
        scope={"account": account, "market": market},
        status="trusted" if trusted else "unavailable",
        as_of_utc=observed_at_utc,
        checks=[check],
        usable_for=(
            ["option_position_report", "lifecycle", "close_advice"]
            if trusted
            else []
        ),
        blocked_consumers=(
            []
            if trusted
            else ["option_position_report", "lifecycle", "close_advice"]
        ),
        blocked_by=[] if trusted else ["OM-LED-001"],
        reason_codes=[] if trusted else ["LEDGER_CURRENT_PROJECTION_UNAVAILABLE"],
        extensions={
            "validation_mode": "current_heads_and_rows",
            "full_replay_status_artifact": "quality/integrity_status.v1.json",
        },
    )


__all__ = ["build_current_ledger_dataset", "build_ledger_datasets"]
