from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def resolve_execution_inbox_path(repo: Any, requested_path: str | Path) -> Path:
    """Keep execution claims beside their Ledger; retain old Inbox facts for migration."""
    candidate = getattr(repo, "primary_repo", repo)
    db_path = getattr(candidate, "db_path", None)
    requested = Path(requested_path)
    if db_path is None:
        return requested
    ledger = Path(db_path).resolve()
    authoritative = ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3")
    legacy_paths = dict.fromkeys((requested.resolve(), ledger.with_suffix(".trade_intake_inbox.sqlite3")))
    for legacy in legacy_paths:
        if legacy == authoritative or not legacy.exists():
            continue
        try:
            with closing(sqlite3.connect(f"{legacy.as_uri()}?mode=ro", uri=True)) as conn:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trade_inbox'"
                ).fetchone()
                if has_table and conn.execute("SELECT 1 FROM trade_inbox LIMIT 1").fetchone():
                    raise ValueError(f"legacy_inbox_migration_required: {legacy}")
        except sqlite3.Error as exc:
            raise ValueError(f"legacy_inbox_migration_required: {legacy}") from exc
    return authoritative
