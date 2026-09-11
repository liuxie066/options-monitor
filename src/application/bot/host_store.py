from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from src.application.bot.contracts import (
    AppEvent,
    AppResult,
    ExecutionContract,
    contract_from_payload,
    contract_to_payload,
    new_id,
    utc_now_iso,
)
from src.application.bot.event_store import public_progress_event, incomplete_progress_response, safe_failure_cause
from src.infrastructure.private_storage import connect_private_sqlite, private_path


REPLY_DELIVERY_LEASE_SECONDS = 300
REPLY_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
PROGRESS_RESOLUTION_CONFLICT_NOTICE = '\n原事项的进度已变化，本次回答已保存，但未将原事项标记为完成。'


class BotHostStore:
    def __init__(self, path: str | Path) -> None:
        self.path = private_path(path)

    def session_turns(self, session_key: str) -> tuple[dict[str, Any], ...]:
        return self._session_json_list(session_key, "turns_json")

    def session_memory(self, session_key: str) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memory_json FROM bot_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        default = _default_session_memory()
        if row is None:
            return default
        try:
            payload = json.loads(str(row[0] or "{}"))
        except Exception:
            return default
        if not isinstance(payload, dict):
            return default
        version = _memory_integer(payload.get("version"), default=1, minimum=1)
        compacted_turn_count = _memory_integer(
            payload.get("compacted_turn_count"),
            default=0,
            minimum=0,
        )
        if version is None or compacted_turn_count is None:
            return default
        pinned_state = payload.get("pinned_state")
        episodes = payload.get("episodes")
        if pinned_state is not None and not isinstance(pinned_state, dict):
            return default
        if episodes is not None and not isinstance(episodes, list):
            return default
        return {
            "version": version,
            "compacted_turn_count": compacted_turn_count,
            "pinned_state": dict(pinned_state or {}),
            "episodes": [dict(item) for item in episodes or () if isinstance(item, dict)],
        }

    def update_session_memory(
        self,
        session_key: str,
        memory: dict[str, Any],
        *,
        expected_compacted_turn_count: int | None = None,
    ) -> bool:
        encoded_memory = json.dumps(memory, ensure_ascii=False, default=str, allow_nan=False)
        self._ensure_schema()
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_compacted_turn_count is not None:
                row = conn.execute(
                    "SELECT memory_json FROM bot_sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                current = _json_object(row)
                current_count = _memory_integer(
                    current.get("compacted_turn_count"),
                    default=0,
                    minimum=0,
                )
                expected_count = _memory_integer(
                    expected_compacted_turn_count,
                    default=0,
                    minimum=0,
                )
                if current_count is None or expected_count is None or current_count != expected_count:
                    return False
            conn.execute(
                """
                INSERT INTO bot_sessions (session_key, messages_json, turns_json, memory_json, updated_at)
                VALUES (?, '[]', '[]', ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    memory_json = excluded.memory_json,
                    updated_at = excluded.updated_at
                """,
                (session_key, encoded_memory, now),
            )
        return True

    def _session_json_list(self, session_key: str, column: str) -> tuple[dict[str, Any], ...]:
        if column not in {"messages_json", "turns_json"}:
            raise ValueError("unsupported session column")
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {column} FROM bot_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            return ()
        try:
            items = json.loads(str(row[0] or "[]"))
        except Exception:
            return ()
        return tuple(dict(item) for item in items if isinstance(item, dict))

    def acquire_session_run(self, session_key: str, run_id: str, *, ttl_seconds: int, deadline_monotonic: float | None = None) -> bool:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return False
        self._ensure_schema(deadline_monotonic=deadline_monotonic)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self._connect(deadline_monotonic=deadline_monotonic) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return False
            conn.execute(
                "DELETE FROM bot_session_runs WHERE session_key = ? AND expires_at <= ?",
                (session_key, now.isoformat()),
            )
            try:
                conn.execute(
                    "INSERT INTO bot_session_runs (session_key, run_id, expires_at) VALUES (?, ?, ?)",
                    (session_key, run_id, expires_at),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_session_run(self, session_key: str, run_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM bot_session_runs WHERE session_key = ? AND run_id = ?",
                (session_key, run_id),
            )

    def start_run(
        self,
        run_id: str,
        *,
        contract: ExecutionContract,
        session_key: str | None,
        resumed_from: str | None = None,
    ) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_runs (
                    run_id, request_id, contract_id, session_key, status, cancel_requested,
                    events_json, started_at, finished_at, response_json, contract_json,
                    resumed_from, resume_attempts, admission_state, lease_id, deadline_at
                ) VALUES (?, ?, ?, ?, 'running', 0, '[]', ?, NULL, NULL, ?, ?, 0, 'open', ?, ?)
                """,
                (
                    run_id,
                    contract.request_id,
                    contract.contract_id,
                    session_key,
                    utc_now_iso(),
                    json.dumps(contract_to_payload(contract), ensure_ascii=False, default=str),
                    resumed_from,
                    new_id("lease"),
                    (datetime.now(timezone.utc) + timedelta(seconds=max(0, (contract.deadline_monotonic or time.monotonic() + 180) - time.monotonic()))).isoformat(),
                ),
            )

    def append_event(self, event: AppEvent) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT events_json, finished_at FROM bot_runs WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            if row is None or row[1] is not None:
                return
            try:
                events = json.loads(str(row[0] or "[]"))
            except Exception:
                events = []
            events.append(
                {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "type": event.type,
                    "timestamp": event.timestamp,
                    "payload": event.payload,
                    "visible_ref": event.visible_ref,
                }
            )
            conn.execute(
                "UPDATE bot_runs SET events_json = ? WHERE run_id = ?",
                (json.dumps(events, ensure_ascii=False, default=str), event.run_id),
            )
            state = {
                "model_turn_started": "waiting_model",
                "tool_call": "waiting_tool",
                "tool_result": "running",
            }.get(event.type)
            if state:
                conn.execute("UPDATE bot_runs SET status = ? WHERE run_id = ?", (state, event.run_id))
            termination_reason = _termination_reason(event)
            if termination_reason:
                conn.execute(
                    "UPDATE bot_runs SET termination_reason = ? WHERE run_id = ?",
                    (termination_reason, event.run_id),
                )
            if event.type == "model_turn_completed":
                metrics = _json_object(
                    conn.execute("SELECT metrics_json FROM bot_runs WHERE run_id = ?", (event.run_id,)).fetchone()
                )
                metrics["model_turn_count"] = int(metrics.get("model_turn_count") or 0) + 1
                metrics["model_retry_count"] = max(
                    int(metrics.get("model_retry_count") or 0),
                    int(event.payload.get("model_retry_count") or 0),
                )
                metrics["usage"] = dict(event.payload.get("usage_total") or event.payload.get("usage") or {})
                conn.execute(
                    "UPDATE bot_runs SET metrics_json = ? WHERE run_id = ?",
                    (json.dumps(metrics, ensure_ascii=False, default=str), event.run_id),
                )
            elif event.type == "context_compacted":
                metrics = _json_object(
                    conn.execute("SELECT metrics_json FROM bot_runs WHERE run_id = ?", (event.run_id,)).fetchone()
                )
                metrics["usage"] = dict(event.payload.get("usage_total") or {})
                conn.execute(
                    "UPDATE bot_runs SET metrics_json = ? WHERE run_id = ?",
                    (json.dumps(metrics, ensure_ascii=False, default=str), event.run_id),
                )
            elif event.type == "run_metrics":
                metrics = _json_object(conn.execute("SELECT metrics_json FROM bot_runs WHERE run_id=?", (event.run_id,)).fetchone())
                metrics.update(event.payload)
                conn.execute("UPDATE bot_runs SET metrics_json=? WHERE run_id=?", (json.dumps(metrics, ensure_ascii=False), event.run_id))
            elif event.type == "tool_result":
                metrics = _json_object(
                    conn.execute("SELECT metrics_json FROM bot_runs WHERE run_id = ?", (event.run_id,)).fetchone()
                )
                metrics["tool_call_count"] = int(metrics.get("tool_call_count") or 0) + 1
                conn.execute(
                    "UPDATE bot_runs SET metrics_json = ? WHERE run_id = ?",
                    (json.dumps(metrics, ensure_ascii=False, default=str), event.run_id),
                )

    def finish_run(self, result: AppResult, *, progress_resolution: dict[str, Any] | None = None,
                   reply: dict[str, Any] | None = None, reply_builder: Any = None, progress_scope: Any = None,
                   deadline_monotonic: float | None = None) -> AppResult:
        self._ensure_schema(deadline_monotonic=deadline_monotonic)
        with self._connect(deadline_monotonic=deadline_monotonic) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            raw = conn.execute("SELECT * FROM bot_runs WHERE run_id=?", (result.run_id,)).fetchone()
            if raw is None:
                raise ValueError("run not found")
            row = dict(raw)
            if row['finished_at'] is not None:
                saved = json.loads(row['response_json'] or '{}')
                return replace(result, status=row['status'], ok=saved.get('ok') is True,
                    user_response=str(saved.get('user_response') or ''), error=saved.get('error'))
            if result.status == 'answered' and row['admission_state'] != 'commit':
                raise ValueError("answer has no committed admission")
            if result.status == 'answered' and deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise TimeoutError('answer persistence exceeded interaction deadline')
            if row['admission_state'] == 'cancel':
                result = replace(result, status='cancelled', ok=False, user_response='Bot 运行已取消。', error={'code':'CANCELLED'})
            progress = self._progress(row, result) if result.status in {'failed','cancelled','interrupted'} else {}
            if progress and result.status in {'failed', 'interrupted'}:
                result = replace(result, user_response=incomplete_progress_response(result.user_response, progress))
            if result.status == 'answered' and progress_resolution:
                original = conn.execute("SELECT * FROM bot_runs WHERE run_id=?", (progress_resolution['progress_ref'],)).fetchone()
                closed = False
                if original is not None:
                    previous = json.loads(original['progress_json'])
                    current_owner = self._progress(row).get('owner_scope')
                    if (previous.get('revision') == progress_resolution['expected_revision']
                            and previous.get('owner_scope') == current_owner and current_owner
                            and previous.get('goal') == progress_resolution['covered_goal']
                            and not previous.get('resolved_by') and row.get('resumed_from') == original['run_id']
                            and progress_scope is not None and progress_scope.owner_scope == current_owner
                            and set(previous.get('accounts', ())) <= progress_scope.allowed_accounts):
                        previous.update(revision=previous['revision'] + 1, resolved_by=result.run_id, resolved_at=utc_now_iso())
                        conn.execute("UPDATE bot_runs SET progress_json=? WHERE run_id=?",
                                     (json.dumps(previous, ensure_ascii=False), original['run_id']))
                        closed = True
                if not closed:
                    result = replace(result, user_response=result.user_response + PROGRESS_RESOLUTION_CONFLICT_NOTICE)
            response = {'status':result.status, 'ok':result.ok, 'user_response':result.user_response, 'error':result.error}
            events = json.loads(row['events_json'])
            for event in events:
                if event.get('type') == 'final_result':
                    event['payload'].update(status=result.status, ok=result.ok,
                                            error_code=str((result.error or {}).get('code') or ''))
            conn.execute("""UPDATE bot_runs SET status=?,finished_at=?,response_json=?,progress_json=?,events_json=?,
                admission_state=CASE WHEN admission_state='open' THEN 'discard' ELSE admission_state END
                WHERE run_id=?""", (result.status, utc_now_iso(), json.dumps(response, ensure_ascii=False),
                    json.dumps(progress, ensure_ascii=False), json.dumps(events, ensure_ascii=False), result.run_id))
            if result.status == 'answered':
                if reply_builder is not None:
                    reply = reply_builder(result)
                if reply:
                    self._enqueue_reply(conn, delivery_key=reply['delivery_key'], channel=reply['channel'],
                        payload=reply['payload'], session_key=row['session_key'], run_id=result.run_id)
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise TimeoutError('answer persistence exceeded interaction deadline')
        return result
    def request_cancel(self, run_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE bot_runs
                SET cancel_requested = 1, admission_state = 'cancel'
                WHERE run_id = ?
                  AND status IN ('running', 'waiting_model', 'waiting_tool')
                  AND admission_state = 'open'
                """,
                (run_id,),
            )
        return bool(cursor.rowcount)

    def claim_admission_decision(self, run_id: str, desired: str) -> str:
        if desired not in {"commit", "discard"}:
            raise ValueError("admission decision must be commit or discard")
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, admission_state FROM bot_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or str(row[0] or "") not in {
                "running",
                "waiting_model",
                "waiting_tool",
            }:
                raise RuntimeError("run unavailable for admission")
            state = str(row[1] or "")
            if state == "open":
                conn.execute(
                    "UPDATE bot_runs SET admission_state = ? WHERE run_id = ? AND admission_state = 'open'",
                    (desired, run_id),
                )
                state = desired
            if state not in {"commit", "discard", "cancel"}:
                raise RuntimeError("run admission state is invalid")
            return state

    def is_cancel_requested(self, run_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM bot_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return bool(row and row[0])

    def run_record(self, run_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM bot_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_runs(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bot_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def run_events(self, run_id: str, *, after_event_id: str | None = None) -> tuple[dict[str, Any], ...]:
        record = self.run_record(run_id)
        if record is None:
            return ()
        try:
            events = [dict(item) for item in json.loads(str(record.get("events_json") or "[]")) if isinstance(item, dict)]
        except Exception:
            return ()
        if not after_event_id:
            return tuple(events)
        for index, item in enumerate(events):
            if str(item.get("event_id") or "") == str(after_event_id):
                return tuple(events[index + 1 :])
        return tuple(events)

    def run_progress(self, run_id: str, *, after_event_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return tuple(
            progress
            for item in self.run_events(run_id, after_event_id=after_event_id)
            if (progress := public_progress_event(item)) is not None
        )

    def resume_source(self, run_id: str, *, max_attempts: int = 3) -> tuple[ExecutionContract, list[dict[str, Any]], str | None] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM bot_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            status = str(row["status"] or "")
            attempts = int(row["resume_attempts"] or 0)
            if status not in {"failed", "interrupted"} or attempts >= max(1, int(max_attempts)):
                return None
            try:
                contract_payload = json.loads(str(row["contract_json"] or "{}"))
                events = json.loads(str(row["events_json"] or "[]"))
                contract = contract_from_payload(contract_payload)
            except Exception:
                return None
            if not contract.contract_id or contract.policy.get("read_only") is not True:
                return None
            if str(row["admission_state"] or "") in {"commit", "cancel"}:
                return None
            if any(
                isinstance(item, dict)
                and isinstance(item.get("payload"), dict)
                and item["payload"].get("session_commit_outcome") == "unknown"
                for item in events
            ):
                return None
            conn.execute(
                "UPDATE bot_runs SET resume_attempts = resume_attempts + 1 WHERE run_id = ?",
                (run_id,),
            )
        return contract, [dict(item) for item in events if isinstance(item, dict)], row["session_key"]

    def mark_stale_runs_interrupted(self, *, older_than_seconds: int = 600) -> int:
        self._ensure_schema()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(older_than_seconds)))).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            stale = conn.execute("SELECT * FROM bot_runs WHERE status IN ('running','waiting_model','waiting_tool') AND started_at < ?", (cutoff,)).fetchall()
            cursor = conn.execute(
                """
                UPDATE bot_runs
                SET status = CASE WHEN admission_state = 'cancel' THEN 'cancelled' ELSE 'interrupted' END,
                    finished_at = ?,
                    termination_reason = CASE WHEN admission_state = 'cancel' THEN 'CANCELLED' ELSE 'host_restart_or_stale_run' END,
                    admission_state = CASE
                        WHEN admission_state = 'open' THEN 'discard'
                        ELSE admission_state
                    END
                WHERE status IN ('running', 'waiting_model', 'waiting_tool') AND started_at < ?
                """,
                (utc_now_iso(), cutoff),
            )
            changed = cursor.rowcount
            for row in stale:
                recovered = conn.execute("SELECT * FROM bot_runs WHERE run_id=?", (row['run_id'],)).fetchone()
                conn.execute("UPDATE bot_runs SET progress_json=? WHERE run_id=?", (json.dumps(self._progress(dict(recovered)), ensure_ascii=False), row['run_id']))
        return int(changed)

    def enqueue_reply(
        self,
        *,
        delivery_key: str,
        channel: str,
        payload: dict[str, Any],
        session_key: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            return self._enqueue_reply(conn, delivery_key=delivery_key, channel=channel, payload=payload, session_key=session_key, run_id=run_id)

    @staticmethod
    def _enqueue_reply(conn: sqlite3.Connection, *, delivery_key: str, channel: str, payload: dict[str, Any], session_key: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO bot_reply_outbox (
                delivery_key, channel, session_key, run_id, payload_json, status,
                attempt_count, next_attempt_at, last_error, created_at, updated_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?, NULL)
            ON CONFLICT(delivery_key) DO NOTHING
            """,
            (
                str(delivery_key),
                str(channel),
                session_key,
                run_id,
                json.dumps(payload, ensure_ascii=False, default=str),
                now,
                now,
                now,
            ),
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM bot_reply_outbox WHERE delivery_key = ?",
            (str(delivery_key),),
        ).fetchone()
        return dict(row) if row is not None else {}

    def reply_payload(self, delivery_key: str) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM bot_reply_outbox WHERE delivery_key=?", (delivery_key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def claim_reply(
        self,
        *,
        delivery_key: str | None = None,
        channel: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        now = before or utc_now_iso()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            capability_cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=REPLY_CAPABILITY_TTL_SECONDS)
            ).isoformat()
            conn.execute(
                """
                UPDATE bot_reply_outbox
                SET status = 'expired', payload_json = '{}', last_error = NULL, updated_at = ?
                WHERE status IN ('pending', 'retryable_failed') AND created_at <= ?
                """,
                (utc_now_iso(), capability_cutoff),
            )
            delivery_cutoff = (
                datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                - timedelta(seconds=REPLY_DELIVERY_LEASE_SECONDS)
            ).isoformat()
            conn.execute(
                """
                UPDATE bot_reply_outbox
                SET status = 'retryable_failed', next_attempt_at = ?,
                    last_error = 'delivery lease expired', updated_at = ?
                WHERE status = 'delivering' AND updated_at <= ?
                """,
                (now, now, delivery_cutoff),
            )
            if delivery_key:
                row = conn.execute(
                    """
                    SELECT * FROM bot_reply_outbox
                    WHERE delivery_key = ?
                      AND status IN ('pending', 'retryable_failed')
                      AND next_attempt_at <= ?
                    """,
                    (str(delivery_key), now),
                ).fetchone()
            elif channel:
                row = conn.execute(
                    """
                    SELECT * FROM bot_reply_outbox
                    WHERE channel = ?
                      AND status IN ('pending', 'retryable_failed')
                      AND next_attempt_at <= ?
                    ORDER BY created_at, delivery_key LIMIT 1
                    """,
                    (str(channel), now),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM bot_reply_outbox
                    WHERE status IN ('pending', 'retryable_failed') AND next_attempt_at <= ?
                    ORDER BY created_at, delivery_key LIMIT 1
                    """,
                    (now,),
                ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE bot_reply_outbox
                SET status = 'delivering', attempt_count = attempt_count + 1, updated_at = ?
                WHERE delivery_key = ?
                """,
                (utc_now_iso(), row["delivery_key"]),
            )
            claimed = dict(row)
            claimed["status"] = "delivering"
            claimed["attempt_count"] = int(row["attempt_count"] or 0) + 1
            return claimed

    def mark_reply_delivered(self, delivery_key: str) -> bool:
        self._ensure_schema()
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE bot_reply_outbox
                SET status = 'delivered', payload_json = '{}', delivered_at = ?, updated_at = ?, last_error = NULL
                WHERE delivery_key = ? AND status = 'delivering'
                """,
                (now, now, str(delivery_key)),
            )
        return bool(cursor.rowcount)

    def mark_reply_failed(
        self,
        delivery_key: str,
        *,
        error: str,
        retryable: bool,
        retry_after_seconds: int = 30,
    ) -> bool:
        self._ensure_schema()
        now = datetime.now(timezone.utc)
        next_attempt = now + timedelta(seconds=max(1, int(retry_after_seconds)))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE bot_reply_outbox
                SET status = ?, next_attempt_at = ?,
                    payload_json = CASE WHEN ? THEN payload_json ELSE '{}' END,
                    last_error = ?, updated_at = ?
                WHERE delivery_key = ? AND status = 'delivering'
                """,
                (
                    "retryable_failed" if retryable else "terminal_failed",
                    next_attempt.isoformat(),
                    1 if retryable else 0,
                    "retryable_delivery_error" if retryable else "terminal_delivery_error",
                    now.isoformat(),
                    str(delivery_key),
                ),
            )
        return bool(cursor.rowcount)

    def list_replies(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bot_reply_outbox ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def acquire_lane(self, lane: str, lease_id: str, *, limit: int, ttl_seconds: int, deadline_monotonic: float | None = None) -> bool:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return False
        self._ensure_schema(deadline_monotonic=deadline_monotonic)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect(deadline_monotonic=deadline_monotonic) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return False
            conn.execute("DELETE FROM bot_lane_leases WHERE expires_at <= ?", (now.isoformat(),))
            active = conn.execute(
                "SELECT COUNT(*) FROM bot_lane_leases WHERE lane = ?",
                (str(lane),),
            ).fetchone()
            if int(active[0] if active else 0) >= max(1, int(limit)):
                return False
            conn.execute(
                "INSERT INTO bot_lane_leases (lane, lease_id, expires_at) VALUES (?, ?, ?)",
                (str(lane), str(lease_id), expires_at),
            )
        return True

    def release_lane(self, lane: str, lease_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM bot_lane_leases WHERE lane = ? AND lease_id = ?",
                (str(lane), str(lease_id)),
            )

    def unfinished_progress(self, scope: Any, *, include_cancelled: bool = False, progress_ref: str | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute("""SELECT progress_json FROM bot_runs
                WHERE json_extract(progress_json, '$.owner_scope') = ?
                AND (? IS NULL OR run_id=?)
                AND (? IS NOT NULL OR json_extract(progress_json, '$.resolved_by') IS NULL)
                AND (? OR (status != 'cancelled' AND admission_state != 'cancel' AND cancel_requested = 0))
                ORDER BY started_at DESC,run_id LIMIT 50""",
                (scope.owner_scope, progress_ref, progress_ref, progress_ref, int(include_cancelled))).fetchall()
        result = []
        for row in rows:
            item = json.loads(row[0])
            if set(item.get('accounts', ())) <= scope.allowed_accounts:
                result.append({k:v for k,v in item.items() if k != 'owner_scope'})
                if len(result) == 8:
                    break
        return result

    @staticmethod
    def progress_query(payload: dict[str, Any]) -> dict[str, Any]:
        return {key:value for key,value in payload.items() if key not in {
            'config_key','config_path','authenticated_channel','authenticated_sender_id',
            'authenticated_conversation_id','authority_scope','cursor','limit','offset','report_now_ms'}}

    @staticmethod
    def _progress(row: dict[str, Any], result: AppResult | None = None) -> dict[str, Any]:
        from src.application.bot.memory import scope_from_contract
        from src.application.agent_tool_registry import pure_read_tool_names
        business_reads = pure_read_tool_names()
        try:
            contract = contract_from_payload(json.loads(row['contract_json']))
            owner = scope_from_contract(contract).owner_scope
        except (ValueError, KeyError, TypeError, OSError):
            owner = None
        events = json.loads(row.get('events_json') or '[]')
        refs, accounts = [], set()
        read_count = partial_count = failed_count = 0
        failure_counts = {"read": 0, "submission": 0, "internal": 0}
        failure_causes = []
        sources = []
        budget_reason = None
        for event in events:
            data = event.get('payload', {})
            if event.get('type') == 'agent_budget_fallback':
                budget_reason = data.get('reason')
            if event.get('type') in {'tool_result', 'memory_tool_result'} and data.get('ok') is False:
                tool_name = data.get('tool_name')
                category = ('internal' if event.get('type') == 'memory_tool_result' or tool_name == 'bot_memory'
                            else 'submission' if tool_name == 'submit_answer'
                            else 'read' if tool_name in business_reads else 'internal')
                failure_counts[category] += 1
                cause = safe_failure_cause({**data, "tool_name": "bot_memory"} if event.get('type') == 'memory_tool_result' else data, category)
                if cause.get('account'):
                    accounts.add(cause['account'].lower())
                if cause not in failure_causes and len(failure_causes) < 3:
                    failure_causes.append(cause)
            if event.get('type') == 'tool_result' and data.get('tool_name') != 'bot_memory':
                if data.get('ok') is False:
                    failed_count += 1
                elif data.get('ok') is True and data.get('content_hash'):
                    read_count += 1
                    partial_count += int(data.get('coverage', {}).get('status') != 'complete' or bool(data.get('missing_data') or data.get('warnings')))
                    source = data.get('source', {})
                    label = str(source.get('label') or '').strip()[:80] if isinstance(source, dict) else ''
                    if label and label not in sources:
                        sources.append(label)
            if event.get('type') == 'tool_result' and data.get('ok') is True and data.get('content_hash'):
                refs.append({'ref': data.get('ref'), 'tool_name': data.get('tool_name'), 'as_of': data.get('as_of'), 'content_hash': data['content_hash'], 'query': BotHostStore.progress_query(data.get('tool_input') or {}), 'coverage': data.get('coverage', {})})
                def collect_accounts(value: Any) -> None:
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key in {'account', 'account_name', 'account_scope'} and isinstance(child, str):
                                accounts.add(child.lower())
                            elif key == 'accounts' and isinstance(child, list):
                                accounts.update(item.lower() for item in child if isinstance(item, str))
                            else:
                                collect_accounts(child)
                    elif isinstance(value, list):
                        for child in value:
                            collect_accounts(child)
                for field in ('tool_input', 'coverage', 'value'):
                    collect_accounts(data.get(field))
        return {'progress_ref': row['run_id'], 'revision': 1, 'owner_scope': owner,
                'goal': json.loads(row['contract_json']).get('input', {}).get('user_message', '')[:2000],
                'accounts': sorted(accounts), 'evidence_refs': refs[-12:],
                'completed_checks': {'read_count': read_count, 'partial_count': partial_count, 'failed_count': failed_count, 'failed_read_count': failure_counts['read'], 'failed_submission_count': failure_counts['submission'], 'failed_internal_count': failure_counts['internal'], 'failure_causes': failure_causes, 'sources': sources[:4]},
                'termination_reason': ((result.error or {}).get('reason') or budget_reason or (result.error or {}).get('code')) if result else row.get('termination_reason'),
                'next_step': '重新读取当前证据，继续原问题；旧引用仅作导航。', 'resolved_by': None, 'resolved_at': None}

    def _connect(self, *, deadline_monotonic: float | None = None) -> sqlite3.Connection:
        # Allow short competing commits without resetting the interaction deadline.
        timeout = 1.0 if deadline_monotonic is None else max(0, min(1.0, deadline_monotonic - time.monotonic()))
        return connect_private_sqlite(self.path, timeout=timeout)

    def _ensure_schema(self, *, deadline_monotonic: float | None = None) -> None:
        from src.application.bot.migration import assert_bot_ready
        assert_bot_ready(self.path)
        with self._connect(deadline_monotonic=deadline_monotonic) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_sessions (
                    session_key TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL,
                    turns_json TEXT NOT NULL DEFAULT '[]',
                    memory_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_session_runs (
                    session_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    session_key TEXT,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    events_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    response_json TEXT,
                    contract_json TEXT NOT NULL DEFAULT '{}',
                    resumed_from TEXT,
                    resume_attempts INTEGER NOT NULL DEFAULT 0,
                    termination_reason TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    admission_state TEXT NOT NULL DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS bot_reply_outbox (
                    delivery_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    session_key TEXT,
                    run_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bot_lane_leases (
                    lane TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (lane, lease_id)
                );
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(bot_sessions)")}
            if "turns_json" not in columns:
                conn.execute("ALTER TABLE bot_sessions ADD COLUMN turns_json TEXT NOT NULL DEFAULT '[]'")
            if "memory_json" not in columns:
                conn.execute("ALTER TABLE bot_sessions ADD COLUMN memory_json TEXT NOT NULL DEFAULT '{}'")
            run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(bot_runs)")}
            for column, definition in (("lease_id", "TEXT"), ("deadline_at", "TEXT"), ("progress_json", "TEXT NOT NULL DEFAULT '{}'")):
                if column not in run_columns:
                    conn.execute(f"ALTER TABLE bot_runs ADD COLUMN {column} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS bot_runs_progress_owner ON bot_runs(json_extract(progress_json, '$.owner_scope'), started_at)")
            from src.application.bot.memory import ensure_schema
            ensure_schema(conn)
            if "contract_json" not in run_columns:
                conn.execute("ALTER TABLE bot_runs ADD COLUMN contract_json TEXT NOT NULL DEFAULT '{}'")
            if "resumed_from" not in run_columns:
                conn.execute("ALTER TABLE bot_runs ADD COLUMN resumed_from TEXT")
            if "resume_attempts" not in run_columns:
                conn.execute("ALTER TABLE bot_runs ADD COLUMN resume_attempts INTEGER NOT NULL DEFAULT 0")
            if "termination_reason" not in run_columns:
                conn.execute("ALTER TABLE bot_runs ADD COLUMN termination_reason TEXT")
            conn.execute(
                """
                UPDATE bot_reply_outbox
                SET payload_json = '{}', last_error = NULL
                WHERE status IN ('delivered', 'terminal_failed', 'expired')
                  AND payload_json != '{}'
                """
            )
            if "metrics_json" not in run_columns:
                conn.execute("ALTER TABLE bot_runs ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")
            if "admission_state" not in run_columns:
                conn.execute(
                    "ALTER TABLE bot_runs ADD COLUMN admission_state TEXT NOT NULL DEFAULT 'open'"
                )


def _termination_reason(event: AppEvent) -> str | None:
    if event.type == "agent_terminated":
        return str(event.payload.get("reason") or "completed")
    if event.type == "run_cancelled":
        return "cancelled"
    if event.type == "budget_exhausted":
        return "budget_exhausted"
    if event.type == "model_error":
        return str(event.payload.get("error_category") or "model_error")
    return None


def _json_object(row: tuple[Any, ...] | sqlite3.Row | None) -> dict[str, Any]:
    raw = row[0] if row else "{}"
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _default_session_memory() -> dict[str, Any]:
    return {"version": 1, "compacted_turn_count": 0, "pinned_state": {}, "episodes": []}


def _memory_integer(value: Any, *, default: int, minimum: int) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not isfinite(value) or not value.is_integer():
            return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized >= minimum else default


__all__ = ["BotHostStore"]
