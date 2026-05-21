from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def make_audit_id(prefix: str = "audit") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{uuid4().hex[:10]}"


def write_control(
    *,
    apply: bool = False,
    confirm: bool = False,
    yes: bool = False,
    high_risk: bool = False,
) -> dict[str, bool]:
    explicitly_confirmed = bool(confirm or yes)
    if high_risk:
        write_requested = explicitly_confirmed
        confirmation_required = bool(apply and not explicitly_confirmed)
    else:
        write_requested = bool(apply or explicitly_confirmed)
        confirmation_required = False
    return {
        "dry_run": not write_requested,
        "write_requested": write_requested,
        "explicitly_confirmed": explicitly_confirmed,
        "confirmation_required": confirmation_required,
        "yes": bool(yes),
    }


def write_contract_payload(
    *,
    dry_run: bool,
    write_applied: bool,
    backup_path: str | Path | None = None,
    audit_id: str | None = None,
    rollback_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "dry_run": bool(dry_run),
        "write_applied": bool(write_applied),
        "backup_path": str(backup_path) if backup_path else None,
        "audit_id": audit_id or make_audit_id(),
        "rollback_hint": rollback_hint,
    }


def attach_write_contract(
    payload: dict[str, Any],
    *,
    dry_run: bool,
    write_applied: bool,
    backup_path: str | Path | None = None,
    audit_id: str | None = None,
    rollback_hint: str | None = None,
) -> dict[str, Any]:
    out = dict(payload)
    out.update(
        write_contract_payload(
            dry_run=dry_run,
            write_applied=write_applied,
            backup_path=backup_path,
            audit_id=audit_id,
            rollback_hint=rollback_hint,
        )
    )
    return out
