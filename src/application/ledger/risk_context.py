from __future__ import annotations

from typing import Any

from src.application.ledger.migration import shadow_replay_position_lot_snapshot


def summarize_ledger_shadow_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        result = shadow_replay_position_lot_snapshot(records, source="risk_context_shadow")
    except Exception as exc:
        return {
            "status": "blocked",
            "read_model": "ledger_shadow",
            "reason": "shadow_replay_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "fail_closed": True,
            "source_record_count": len(records),
        }

    import_diagnostics = [item.to_dict() for item in result.import_diagnostics]
    projection_diagnostics = [item.to_dict() for item in result.projection.diagnostics]
    reconciliation = result.reconciliation.to_dict() if result.reconciliation is not None else None
    reconciliation_issues = list((reconciliation or {}).get("issues") or [])
    import_degraded = any(item.get("severity") == "error" for item in import_diagnostics)
    blocked = bool(
        any(item.get("severity") == "error" for item in projection_diagnostics)
        or any(item.get("severity") == "error" for item in reconciliation_issues)
    )
    views = [item.to_dict() for item in result.projection.views]
    return {
        "status": "blocked" if blocked else ("degraded" if import_degraded else "ok"),
        "read_model": "ledger_shadow",
        "reason": (
            "ledger_invariants_failed"
            if blocked
            else ("ledger_import_degraded" if import_degraded else "ledger_shadow_ok")
        ),
        "fail_closed": blocked,
        "source_record_count": result.source_record_count,
        "imported_event_count": result.imported_event_count,
        "lot_count": len(result.projection.lots),
        "open_lot_count": sum(1 for item in result.projection.lots if item.contracts_open > 0),
        "view_count": len(views),
        "views": views,
        "import_diagnostics": import_diagnostics[:20],
        "projection_diagnostics": projection_diagnostics[:20],
        "reconciliation": reconciliation,
    }
