"""Deliberate historical/corrupt SQLite fixtures, never used by application code."""
import sqlite3
from pathlib import Path

from src.infrastructure.decision_history_sqlite import canonical_json, content_hash, history_path


def replace_history_payload(base: Path, payload: dict, *, account: str = "lx", market: str = "US", revision: int = 0) -> None:
    with sqlite3.connect(history_path(base)) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name='decisions_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER decisions_no_update")
        conn.execute("UPDATE decisions SET payload_json=?,payload_hash=? WHERE account=? AND market=? AND revision=?",
                     (canonical_json(payload), content_hash(payload), account, market, revision))
        conn.execute(trigger)


def delete_history_revision(base: Path, *, revision: int = 0) -> None:
    with sqlite3.connect(history_path(base)) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name='decisions_no_delete'").fetchone()[0]
        conn.execute("DROP TRIGGER decisions_no_delete")
        conn.execute("DELETE FROM decisions WHERE revision=?", (revision,))
        conn.execute(trigger)
