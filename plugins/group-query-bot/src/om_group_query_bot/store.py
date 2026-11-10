"""Seven-day preflight delivery metadata, with no chat content or business input."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .feishu import VerifiedGroupEvent

CONTENT_TTL_HOURS = 24
METADATA_TTL_DAYS = 7
CONVERSATION_DB_NAME = "conversation.sqlite3"


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("an aware UTC clock is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class InputIdentityConflict(RuntimeError):
    """The same in-conversation input ID arrived with different facts."""


class DeliveryStore:
    def __init__(self, data_dir: Path, *, db_name: str = "preflight.sqlite3"):
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = data_dir / db_name
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                message_key TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                event_id TEXT,
                scope_sha256 TEXT NOT NULL,
                delivery_uuid TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('claimed','sending','acknowledged','failed','outcome_unknown')),
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS input_bindings (
                scope_sha256 TEXT NOT NULL,
                input_id TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                PRIMARY KEY (scope_sha256, input_id)
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, event: VerifiedGroupEvent, instance_id: str, now: datetime) -> tuple[dict, bool]:
        scope = event.scope_sha256(instance_id)
        key = hashlib.sha256((scope + ":" + event.message_id).encode()).hexdigest()
        with self.connect() as db:
            db.execute("DELETE FROM deliveries WHERE expires_at_utc <= ?", (utc_text(now),))
            result = db.execute("""INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?)""",
                                (key, event.message_id, event.event_id, scope, str(uuid.uuid4()),
                                 utc_text(now), utc_text(now + timedelta(days=7))))
            row = db.execute("SELECT * FROM deliveries WHERE message_key = ?", (key,)).fetchone()
            return dict(row), result.rowcount == 1

    def mark_sending(self, key: str) -> None:
        with self.connect() as db:
            result = db.execute("UPDATE deliveries SET state = 'sending' WHERE message_key = ? AND state IN ('claimed','sending')", (key,))
            if result.rowcount != 1:
                raise RuntimeError("delivery_not_claimed")

    def finish(self, key: str, state: str) -> None:
        if state not in {"acknowledged", "failed", "outcome_unknown"}:
            raise ValueError("invalid_delivery_state")
        with self.connect() as db:
            result = db.execute("UPDATE deliveries SET state = ? WHERE message_key = ? AND state IN ('claimed','sending')", (state, key))
            if result.rowcount != 1:
                raise RuntimeError("delivery_already_finished")

    def cleanup(self, now: datetime) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM deliveries WHERE expires_at_utc <= ?", (utc_text(now),))
            db.execute("DELETE FROM input_bindings WHERE expires_at_utc <= ?", (utc_text(now),))

    def bind_input(self, scope_sha256: str, input_id: str, input_sha256: str,
                   now: datetime) -> bool:
        """Bind an input identity; a different digest for the same ID is fatal."""
        with self.connect() as db:
            db.execute("DELETE FROM input_bindings WHERE expires_at_utc <= ?", (utc_text(now),))
            row = db.execute("""SELECT input_sha256 FROM input_bindings
                                WHERE scope_sha256 = ? AND input_id = ?""",
                             (scope_sha256, input_id)).fetchone()
            if row is not None:
                if row["input_sha256"] != input_sha256:
                    raise InputIdentityConflict(
                        "input_id was already confirmed with different facts")
                return False
            db.execute("""INSERT INTO input_bindings VALUES (?, ?, ?, ?, ?)""",
                       (scope_sha256, input_id, input_sha256, utc_text(now),
                        utc_text(now + timedelta(days=1))))
            return True

    def counts(self, now: datetime) -> dict[str, int]:
        with self.connect() as db:
            return {row["state"]: row["n"] for row in db.execute(
                "SELECT state, COUNT(*) AS n FROM deliveries WHERE expires_at_utc > ? GROUP BY state",
                (utc_text(now),))}


def derived_expiry(now: datetime, *source_expiries: datetime | None) -> datetime:
    """Content may never outlive its first 24h or any source expiry."""
    expiry = now + timedelta(hours=CONTENT_TTL_HOURS)
    for source in source_expiries:
        if source is not None and source < expiry:
            expiry = source
    return expiry


class ConversationStore:
    """Bounded bot-side conversation, input, outbox and audit storage."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = data_dir / CONVERSATION_DB_NAME
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                turn_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                scope_sha256 TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','tool_result')),
                content TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_expires_at_utc TEXT NOT NULL,
                PRIMARY KEY (turn_id, seq)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS messages_scope
                          ON messages(scope_sha256, created_at_utc, expires_at_utc)""")
            db.execute("""CREATE TABLE IF NOT EXISTS scenario_inputs (
                scope_sha256 TEXT NOT NULL,
                input_id TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','confirmed')),
                parameters_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_expires_at_utc TEXT NOT NULL,
                PRIMARY KEY (scope_sha256, input_id)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS scenario_inputs_digest
                          ON scenario_inputs(scope_sha256, operation, input_sha256, expires_at_utc)""")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_payloads (
                delivery_key TEXT PRIMARY KEY,
                scope_sha256 TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_expires_at_utc TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS audit (
                audit_id TEXT PRIMARY KEY,
                scope_sha256 TEXT NOT NULL,
                turn_id TEXT,
                status TEXT NOT NULL,
                reason_codes TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def append_message(self, *, scope_sha256: str, turn_id: str, seq: int, role: str,
                       content: str, now: datetime, source_id: str,
                       source_expiry: datetime) -> str:
        """Insert once; a replayed turn keeps the original expiry."""
        expires = derived_expiry(now, source_expiry)
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, int(seq), scope_sha256, role, content, utc_text(now),
                 utc_text(expires), source_id, utc_text(source_expiry)))
            row = db.execute("""SELECT expires_at_utc FROM messages
                                WHERE turn_id = ? AND seq = ?""", (turn_id, int(seq))).fetchone()
            return str(row["expires_at_utc"])

    def context_turns(self, scope_sha256: str, now: datetime, *, max_turns: int,
                      max_tokens: int, estimate) -> list[dict]:
        """Return complete, unexpired turns newest-last within both limits."""
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM messages WHERE scope_sha256 = ? AND expires_at_utc > ?
                   ORDER BY created_at_utc, turn_id, seq""",
                (scope_sha256, utc_text(now))).fetchall()
        turns: list[dict] = []
        by_id: dict[str, dict] = {}
        for row in rows:
            turn = by_id.get(row["turn_id"])
            if turn is None:
                turn = {"turn_id": row["turn_id"], "messages": []}
                by_id[row["turn_id"]] = turn
                turns.append(turn)
            turn["messages"].append({"role": row["role"], "content": row["content"]})
        complete = [turn for turn in turns
                    if turn["messages"][0]["role"] == "user"
                    and any(item["role"] == "assistant" for item in turn["messages"])]
        selected: list[dict] = []
        used = 0
        for turn in reversed(complete):
            size = estimate(turn["messages"])
            if selected and (len(selected) >= max_turns or used + size > max_tokens):
                break
            if not selected and size > max_tokens:
                break
            selected.append(turn)
            used += size
        selected.reverse()
        return selected

    def live_input_expiry(self, *, scope_sha256: str, operation: str,
                          now: datetime) -> datetime | None:
        """Earliest expiry among this scope+operation's live input versions."""
        with self.connect() as db:
            row = db.execute(
                """SELECT MIN(expires_at_utc) AS expires FROM scenario_inputs
                   WHERE scope_sha256 = ? AND operation = ? AND expires_at_utc > ?""",
                (scope_sha256, operation, utc_text(now))).fetchone()
        value = row["expires"] if row is not None else None
        return datetime.fromisoformat(str(value)) if value else None

    def save_pending_input(self, *, scope_sha256: str, operation: str,
                           parameters: dict, now: datetime, source_id: str,
                           source_expiry: datetime) -> str:
        pending_id = "pending-" + uuid.uuid4().hex
        payload = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        prior = self.live_input_expiry(
            scope_sha256=scope_sha256, operation=operation, now=now)
        expires = derived_expiry(now, source_expiry, prior)
        with self.connect() as db:
            db.execute("""DELETE FROM scenario_inputs WHERE scope_sha256 = ?
                          AND operation = ? AND status = 'pending'""",
                       (scope_sha256, operation))
            db.execute("""INSERT INTO scenario_inputs VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                       (scope_sha256, pending_id, hashlib.sha256(payload.encode()).hexdigest(),
                        operation, payload, utc_text(now), utc_text(expires),
                        source_id, utc_text(source_expiry)))
        return pending_id

    def pending_inputs(self, scope_sha256: str, now: datetime) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT operation, parameters_json FROM scenario_inputs
                   WHERE scope_sha256 = ? AND status = 'pending' AND expires_at_utc > ?
                   ORDER BY created_at_utc DESC""",
                (scope_sha256, utc_text(now))).fetchall()
        return [{"operation": row["operation"],
                 "parameters": json.loads(row["parameters_json"])} for row in rows]

    def resolve_confirmed_input(self, *, scope_sha256: str, operation: str,
                                input_sha256: str, now: datetime) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT input_id FROM scenario_inputs
                   WHERE scope_sha256 = ? AND operation = ? AND input_sha256 = ?
                     AND status = 'confirmed' AND expires_at_utc > ?
                   ORDER BY created_at_utc DESC LIMIT 1""",
                (scope_sha256, operation, input_sha256, utc_text(now))).fetchone()
            return str(row["input_id"]) if row is not None else None

    def save_confirmed_input(self, *, scope_sha256: str, input_id: str,
                             input_sha256: str, operation: str, parameters: dict,
                             now: datetime, source_id: str,
                             source_expiry: datetime) -> str:
        payload = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        prior = self.live_input_expiry(
            scope_sha256=scope_sha256, operation=operation, now=now)
        expires = derived_expiry(now, source_expiry, prior)
        with self.connect() as db:
            row = db.execute(
                """SELECT input_sha256, expires_at_utc FROM scenario_inputs
                   WHERE scope_sha256 = ? AND input_id = ?""",
                (scope_sha256, input_id)).fetchone()
            if row is not None:
                if row["input_sha256"] != input_sha256:
                    raise InputIdentityConflict(
                        "input_id was already confirmed with different facts")
                return str(row["expires_at_utc"])
            db.execute("""DELETE FROM scenario_inputs WHERE scope_sha256 = ?
                          AND operation = ? AND status = 'pending'""",
                       (scope_sha256, operation))
            db.execute("""INSERT INTO scenario_inputs VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)""",
                       (scope_sha256, input_id, input_sha256, operation, payload,
                        utc_text(now), utc_text(expires), source_id, utc_text(source_expiry)))
        return utc_text(expires)

    def input_expiry(self, scope_sha256: str, input_id: str, now: datetime) -> datetime | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT expires_at_utc FROM scenario_inputs
                   WHERE scope_sha256 = ? AND input_id = ? AND expires_at_utc > ?""",
                (scope_sha256, input_id, utc_text(now))).fetchone()
        return datetime.fromisoformat(str(row["expires_at_utc"])) if row is not None else None

    def save_outbox(self, *, delivery_key: str, scope_sha256: str, body: str,
                    now: datetime, source_id: str, source_expiry: datetime) -> str:
        expires = derived_expiry(now, source_expiry)
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO outbox_payloads VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (delivery_key, scope_sha256, body, utc_text(now), utc_text(expires),
                 source_id, utc_text(source_expiry)))
            row = db.execute("""SELECT expires_at_utc FROM outbox_payloads
                                WHERE delivery_key = ?""", (delivery_key,)).fetchone()
            return str(row["expires_at_utc"])

    def outbox_body(self, delivery_key: str, now: datetime) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT body FROM outbox_payloads
                   WHERE delivery_key = ? AND expires_at_utc > ?""",
                (delivery_key, utc_text(now))).fetchone()
        return str(row["body"]) if row is not None else None

    def record_audit(self, *, scope_sha256: str, turn_id: str | None, status: str,
                     reason_codes: tuple[str, ...], now: datetime) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, scope_sha256, turn_id, status,
                 json.dumps(list(reason_codes), separators=(",", ":")),
                 utc_text(now), utc_text(now + timedelta(hours=CONTENT_TTL_HOURS))))

    def cleanup(self, now: datetime) -> dict[str, int]:
        """Delete expired bodies first; every statement is independently repeatable."""
        stamp = utc_text(now)
        counts: dict[str, int] = {}
        with self.connect() as db:
            for table in ("messages", "scenario_inputs", "outbox_payloads", "audit"):
                cursor = db.execute(f"DELETE FROM {table} WHERE expires_at_utc <= ?", (stamp,))
                counts[table] = cursor.rowcount
        return counts

    def counts(self, now: datetime) -> dict[str, int]:
        stamp = utc_text(now)
        with self.connect() as db:
            return {
                table: int(db.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE expires_at_utc > ?",
                    (stamp,)).fetchone()["n"])
                for table in ("messages", "scenario_inputs", "outbox_payloads", "audit")
            }
