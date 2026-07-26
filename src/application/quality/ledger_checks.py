from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.application.ledger.api import (
    compare_projection_lots,
    project_trade_event_log,
    trade_event_log,
)
from src.application.quality.model import check_result, dataset_status, evidence_ref
from src.application.trades.deal_identity import active_ledger_events, structured_deal_ids_from_ledger_event


def _account_from_event(event: dict[str, Any]) -> str:
    key = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
    raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    return str(event.get("account") or key.get("account") or raw.get("internal_account") or "").strip().lower()


def _account_from_lot(row: Any) -> str:
    payload = row.to_dict() if hasattr(row, "to_dict") else row
    fields = payload.get("fields") if isinstance(payload, dict) and isinstance(payload.get("fields"), dict) else {}
    return str(fields.get("account") or "").strip().lower()


def _economic_fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    key = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
    raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    return (
        str(event.get("event_type") or "").lower(),
        str(event.get("broker") or key.get("broker") or "").lower(),
        _account_from_event(event),
        str(event.get("symbol") or key.get("underlying_symbol") or "").upper(),
        str(event.get("option_type") or key.get("option_type") or "").lower(),
        str(event.get("position_side") or key.get("position_side") or "").lower(),
        str(event.get("side") or raw.get("side") or "").lower(),
        int(event.get("contracts") or 0),
        str(event.get("strike") or key.get("strike") or ""),
        str(event.get("expiration_ymd") or key.get("expiration_ymd") or ""),
        str(event.get("multiplier") or raw.get("multiplier") or ""),
        str(event.get("price") or ""),
    )


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
    deal_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in active_events:
        for deal_id in structured_deal_ids_from_ledger_event(event):
            deal_rows[deal_id].append(event)

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
            if len(scoped) <= 1:
                continue
            fingerprints = {_economic_fingerprint(item) for item in scoped}
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


__all__ = ["build_ledger_datasets"]
