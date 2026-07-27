from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from domain.domain.decision_state_fingerprint import (
    DECISION_STATE_FINGERPRINT_SCHEMA,
    DECISION_STATE_SNAPSHOT_SCHEMA,
    build_decision_state_fingerprint,
    canonical_sha256,
)
from src.application.ledger.projection_verify import compare_projection_lots
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


def decision_state_snapshot(
    repo: Any,
    *,
    account: str,
    portfolio_scope_id: str,
    source_observed_at: str | None = None,
) -> dict[str, Any]:
    observed_at_override = (
        str(source_observed_at) if source_observed_at is not None else None
    )
    observed_at = observed_at_override or datetime.now(timezone.utc).isoformat()
    candidate = getattr(repo, "primary_repo", repo)
    read_rows = getattr(candidate, "read_decision_state_rows", None)
    if not callable(read_rows):
        return {
            "schema_version": DECISION_STATE_SNAPSHOT_SCHEMA,
            "fingerprint_schema_version": DECISION_STATE_FINGERPRINT_SCHEMA,
            "snapshot_status": "snapshot_unavailable",
            "actionable": False,
            "reason_codes": ["coherent_ledger_snapshot_unavailable"],
            "decision_state_fingerprint": None,
            "source_observed_at": observed_at,
        }
    try:
        rows = read_rows(account=account)
        # A ledger observation is complete only after the coherent read
        # transaction has returned.  Injected timestamps remain available for
        # deterministic tests and historical replay.
        observed_at = (
            observed_at_override or datetime.now(timezone.utc).isoformat()
        )
        events = list(rows["trade_events"])
        stored_lots = list(rows["stored_position_lots"])
        projection = project_stored_trade_events_to_position_lots(events)
        projected_lots = [item.to_dict() for item in projection.lots]
        comparison = compare_projection_lots(
            projected_lots=projected_lots,
            current_lots=stored_lots,
            diagnostics=projection.diagnostics,
        )
        error_count = sum(
            count
            for status, count in comparison["summary"].items()
            if status != "matched"
        )
        account_value = str(account or "").strip().lower()
        reprojected_account_lots = [
            row
            for row in projected_lots
            if str((row.get("fields") or {}).get("account") or "").strip().lower() == account_value
        ]
        fingerprint_payload = {
            "schema_version": DECISION_STATE_SNAPSHOT_SCHEMA,
            "normalized_account": account_value,
            "portfolio_scope_id": str(portfolio_scope_id or "").strip(),
            "event_fingerprint": canonical_sha256(events),
            "stored_position_lots_fingerprint": canonical_sha256(stored_lots),
            "reprojected_position_lots_fingerprint": canonical_sha256(projected_lots),
            "account_position_lots": rows["account_position_lots"],
            "account_reprojected_position_lots": reprojected_account_lots,
            "account_lifecycle_cases": rows["account_lifecycle_cases"],
            "account_lifecycle_evidence": rows["account_lifecycle_evidence"],
            "account_lifecycle_allocations": rows["account_lifecycle_allocations"],
            "account_assigned_stock_events": rows["account_assigned_stock_events"],
            "account_combo_identities": rows["account_combo_identities"],
        }
        fingerprint = build_decision_state_fingerprint(fingerprint_payload)
        trusted = error_count == 0
        return {
            **fingerprint_payload,
            "fingerprint_schema_version": DECISION_STATE_FINGERPRINT_SCHEMA,
            "snapshot_status": "trusted" if trusted else "projection_untrusted",
            "actionable": trusted,
            "reason_codes": [] if trusted else ["same_snapshot_projection_mismatch"],
            "decision_state_fingerprint": fingerprint,
            "source_observed_at": observed_at,
            "projection_comparison": comparison,
            "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
        }
    except Exception as exc:
        return {
            "schema_version": DECISION_STATE_SNAPSHOT_SCHEMA,
            "fingerprint_schema_version": DECISION_STATE_FINGERPRINT_SCHEMA,
            "snapshot_status": "snapshot_unavailable",
            "actionable": False,
            "reason_codes": ["coherent_ledger_snapshot_failed"],
            "decision_state_fingerprint": None,
            "source_observed_at": observed_at,
            "error": str(exc),
        }


__all__ = ["decision_state_snapshot"]
