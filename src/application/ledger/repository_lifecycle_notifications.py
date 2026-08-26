from __future__ import annotations

from .repository_schema import (
    Any,
    Sequence,
    _json_object,
    _json_text,
    _notification_delivery_batch_row,
    _notification_outbox_row,
    hashlib,
    now_ms,
    sqlite3,
)

class LifecycleNotificationRepositoryMixin:
    def insert_trade_lifecycle_notification_once(
        self,
        intent: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(intent.get("payload") or {})
        payload_json = _json_text(payload)
        payload_hash = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        supplied_hash = str(intent.get("payload_hash") or "").strip()
        if supplied_hash and supplied_hash != payload_hash:
            raise ValueError("notification outbox payload hash mismatch")
        outbox_id = str(intent.get("outbox_id") or "").strip()
        case_id = str(intent.get("case_id") or "").strip()
        transition_type = str(
            intent.get("transition_type") or ""
        ).strip().lower()
        revision = int(intent.get("resolution_revision") or 0)
        delivery_revision = int(intent.get("delivery_revision") or 0)
        transition_key = str(intent.get("transition_key") or "").strip()
        state_fingerprint = str(
            intent.get("state_fingerprint") or ""
        ).strip()
        status = str(intent.get("status") or "pending").strip().lower()
        if (
            not outbox_id
            or not case_id
            or not transition_type
            or revision <= 0
            or delivery_revision < 0
            or not transition_key
            or not state_fingerprint
            or status not in {"pending", "suppressed"}
        ):
            raise ValueError("notification outbox intent is incomplete")
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE transition_key = ? AND delivery_revision = ?
                """,
                (transition_key, delivery_revision),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["outbox_id"] or "") != outbox_id
                    or str(existing["case_id"] or "") != case_id
                    or str(existing["transition_type"] or "")
                    != transition_type
                    or int(existing["resolution_revision"] or 0)
                    != revision
                    or str(existing["state_fingerprint"] or "")
                    != state_fingerprint
                    or str(existing["payload_hash"] or "") != payload_hash
                    or str(existing["payload_json"] or "") != payload_json
                ):
                    raise ValueError(
                        "notification outbox immutable intent conflict"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_notification_outbox (
                  outbox_id, case_id, transition_type, resolution_revision,
                  delivery_revision, transition_key, state_fingerprint,
                  status, payload_json, payload_hash, attempt_count,
                  next_attempt_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    outbox_id,
                    case_id,
                    transition_type,
                    revision,
                    delivery_revision,
                    transition_key,
                    state_fingerprint,
                    status,
                    payload_json,
                    payload_hash,
                    ts if status == "pending" else None,
                    ts,
                    ts,
                ),
            )
        return True

    def insert_trade_lifecycle_migration_receipt_once(
        self,
        receipt: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(receipt or {})
        target_key = str(payload.get("target_key") or "").strip()
        migration_schema = str(
            payload.get("migration_schema") or ""
        ).strip()
        manifest_hash = str(
            payload.get("manifest_hash") or ""
        ).strip()
        row_hash = str(payload.get("row_hash") or "").strip()
        if (
            not target_key
            or not migration_schema
            or not manifest_hash
            or not row_hash
        ):
            raise ValueError(
                "lifecycle migration receipt identity is incomplete"
            )
        raw_json = _json_text(payload)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT row_hash
                FROM trade_lifecycle_migration_receipts
                WHERE target_key = ?
                """,
                (target_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["row_hash"] or "") != row_hash:
                    raise ValueError(
                        "lifecycle migration receipt row conflict"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_migration_receipts (
                  target_key, migration_schema, manifest_hash,
                  row_hash, applied_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    target_key,
                    migration_schema,
                    manifest_hash,
                    row_hash,
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def list_trade_lifecycle_migration_receipts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_migration_receipts
                ORDER BY target_key ASC
                """
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def get_trade_lifecycle_notification(
        self,
        outbox_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE outbox_id = ?
                """,
                (str(outbox_id or "").strip(),),
            ).fetchone()
        return _notification_outbox_row(row) if row is not None else None

    def get_trade_lifecycle_notification_by_transition(
        self,
        *,
        transition_key: str,
        delivery_revision: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        transition_key_value = str(transition_key or "").strip()
        delivery_revision_value = int(delivery_revision)
        if not transition_key_value or delivery_revision_value < 0:
            raise ValueError("notification transition identity is incomplete")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE transition_key = ? AND delivery_revision = ?
                """,
                (transition_key_value, delivery_revision_value),
            ).fetchone()
        return _notification_outbox_row(row) if row is not None else None

    def list_trade_lifecycle_notifications(
        self,
        *,
        status: str | None = None,
        case_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_outbox
                {where}
                ORDER BY created_at_ms ASC, outbox_id ASC
                """,
                params,
            ).fetchall()
        return [_notification_outbox_row(row) for row in rows]

    def compare_and_set_trade_lifecycle_notification(
        self,
        *,
        outbox_id: str,
        expected_status: str,
        new_status: str,
        claim_id: str | None = None,
        expected_claim_id: str | None = None,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        allowed_fields = {
            "provider_message_id",
            "claim_id",
            "claimed_at_ms",
            "send_started_at_ms",
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "provider_receipt_json",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification outbox fields: "
                + ",".join(invalid)
            )
        if claim_id is not None:
            updates["claim_id"] = claim_id
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            value = updates[key]
            if key == "provider_receipt_json" and isinstance(value, dict):
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        clauses = ["outbox_id = ?", "status = ?"]
        values.extend(
            (
                str(outbox_id or "").strip(),
                str(expected_status or "").strip().lower(),
            )
        )
        if expected_claim_id is not None:
            clauses.append("claim_id = ?")
            values.append(str(expected_claim_id))
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET {', '.join(assignments)}
                WHERE {' AND '.join(clauses)}
                """,
                values,
            )
        return int(cursor.rowcount or 0) == 1

    def insert_trade_lifecycle_notification_batch_once(
        self,
        batch: dict[str, Any],
        *,
        member_outbox_ids: Sequence[str],
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(batch.get("payload") or {})
        payload_json = _json_text(payload)
        payload_hash = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        supplied_hash = str(batch.get("payload_hash") or "").strip()
        if supplied_hash and supplied_hash != payload_hash:
            raise ValueError(
                "notification delivery batch payload hash mismatch"
            )
        batch_id = str(batch.get("batch_id") or "").strip()
        route_fingerprint = str(
            batch.get("route_fingerprint") or ""
        ).strip()
        provider = str(batch.get("provider") or "").strip().lower()
        channel = str(batch.get("channel") or "").strip().lower()
        target_fingerprint = str(
            batch.get("target_fingerprint") or ""
        ).strip()
        renderer_version = str(
            batch.get("renderer_version") or ""
        ).strip()
        status = str(batch.get("status") or "pending").strip().lower()
        member_ids = tuple(
            str(value or "").strip() for value in member_outbox_ids
        )
        if not member_ids or any(not value for value in member_ids):
            raise ValueError(
                "notification delivery batch members are incomplete"
            )
        if len(set(member_ids)) != len(member_ids):
            raise ValueError(
                "notification delivery batch members must be unique"
            )
        payload_members_raw = payload.get("members")
        payload_members = (
            list(payload_members_raw)
            if isinstance(payload_members_raw, list)
            else []
        )
        payload_route = (
            dict(payload.get("route") or {})
            if isinstance(payload.get("route"), dict)
            else {}
        )
        payload_member_ids = tuple(
            str(item.get("outbox_id") or "").strip()
            for item in payload_members
            if isinstance(item, dict)
        )
        member_count = int(batch.get("member_count") or 0)
        first_created = int(
            batch.get("first_intent_created_at_ms") or 0
        )
        last_created = int(
            batch.get("last_intent_created_at_ms") or 0
        )
        created_at = int(batch.get("created_at_ms") or 0)
        attempts = int(batch.get("attempt_count") or 0)
        next_attempt = batch.get("next_attempt_at_ms")
        if (
            not batch_id
            or not route_fingerprint
            or not provider
            or not channel
            or not target_fingerprint
            or not renderer_version
            or status != "pending"
            or member_count != len(member_ids)
            or first_created <= 0
            or last_created < first_created
            or created_at <= 0
            or attempts < 0
            or str(payload.get("batch_id") or "").strip() != batch_id
            or str(payload.get("schema_version") or "").strip()
            != renderer_version
            or payload_member_ids != member_ids
            or len(payload_members) != len(member_ids)
            or str(payload_route.get("provider") or "").strip().lower()
            != provider
            or str(payload_route.get("channel") or "").strip().lower()
            != channel
            or str(
                payload_route.get("target_fingerprint") or ""
            ).strip()
            != target_fingerprint
            or str(
                payload_route.get("route_fingerprint") or ""
            ).strip()
            != route_fingerprint
        ):
            raise ValueError(
                "notification delivery batch is incomplete"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if existing is not None:
                stored = _notification_delivery_batch_row(existing)
                immutable_conflict = any(
                    stored[key] != value
                    for key, value in {
                        "route_fingerprint": route_fingerprint,
                        "provider": provider,
                        "channel": channel,
                        "target_fingerprint": target_fingerprint,
                        "renderer_version": renderer_version,
                        "payload_hash": payload_hash,
                        "member_count": member_count,
                        "first_intent_created_at_ms": first_created,
                        "last_intent_created_at_ms": last_created,
                    }.items()
                )
                if immutable_conflict or stored["payload"] != payload:
                    raise ValueError(
                        "notification delivery batch immutable conflict"
                    )
                bound = active_conn.execute(
                    """
                    SELECT outbox_id
                    FROM trade_lifecycle_notification_outbox
                    WHERE delivery_batch_id = ?
                    ORDER BY created_at_ms ASC, outbox_id ASC
                    """,
                    (batch_id,),
                ).fetchall()
                if {str(row["outbox_id"]) for row in bound} != set(
                    member_ids
                ):
                    raise ValueError(
                        "notification delivery batch membership conflict"
                    )
                return False
            placeholders = ",".join("?" for _ in member_ids)
            member_rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE outbox_id IN ({placeholders})
                """,
                member_ids,
            ).fetchall()
            if len(member_rows) != len(member_ids):
                raise ValueError(
                    "notification delivery batch member not found"
                )
            members_by_id = {
                str(row["outbox_id"]): _notification_outbox_row(row)
                for row in member_rows
            }
            for envelope in payload_members:
                if not isinstance(envelope, dict):
                    raise ValueError(
                        "notification delivery batch member payload is invalid"
                    )
                outbox_id = str(
                    envelope.get("outbox_id") or ""
                ).strip()
                row = members_by_id.get(outbox_id)
                if not isinstance(row, dict):
                    raise ValueError(
                        "notification delivery batch member not found"
                    )
                if row["delivery_batch_id"] is not None or str(
                    row["status"] or ""
                ) not in {"pending", "explicit_failed"}:
                    raise ValueError(
                        "notification delivery batch member is not bindable"
                    )
                expected_envelope = {
                    "outbox_id": str(row["outbox_id"]),
                    "case_id": str(row["case_id"]),
                    "transition_type": str(row["transition_type"]),
                    "resolution_revision": int(
                        row["resolution_revision"]
                    ),
                    "delivery_revision": int(
                        row.get("delivery_revision") or 0
                    ),
                    "transition_key": str(row["transition_key"]),
                    "state_fingerprint": str(
                        row["state_fingerprint"]
                    ),
                    "payload_hash": str(row["payload_hash"]),
                    "created_at_ms": int(row["created_at_ms"]),
                    "payload": dict(row.get("payload") or {}),
                }
                if envelope != expected_envelope:
                    raise ValueError(
                        "notification delivery batch member payload mismatch"
                    )
            actual_created = [
                int(row["created_at_ms"])
                for row in members_by_id.values()
            ]
            if (
                min(actual_created) != first_created
                or max(actual_created) != last_created
            ):
                raise ValueError(
                    "notification delivery batch member time range mismatch"
                )
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_notification_delivery_batches (
                  batch_id, route_fingerprint, provider, channel,
                  target_fingerprint, renderer_version, status,
                  payload_json, payload_hash, member_count,
                  first_intent_created_at_ms,
                  last_intent_created_at_ms, attempt_count,
                  next_attempt_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    route_fingerprint,
                    provider,
                    channel,
                    target_fingerprint,
                    renderer_version,
                    status,
                    payload_json,
                    payload_hash,
                    member_count,
                    first_created,
                    last_created,
                    attempts,
                    next_attempt,
                    created_at,
                    created_at,
                ),
            )
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET delivery_batch_id = ?, status = 'batched',
                    claim_id = NULL, claimed_at_ms = NULL,
                    send_started_at_ms = NULL,
                    next_attempt_at_ms = NULL,
                    updated_at_ms = ?
                WHERE outbox_id IN ({placeholders})
                  AND delivery_batch_id IS NULL
                  AND status IN ('pending', 'explicit_failed')
                """,
                (batch_id, created_at, *member_ids),
            )
            if int(cursor.rowcount or 0) != len(member_ids):
                raise ValueError(
                    "notification delivery batch binding lost"
                )
        return True

    def get_trade_lifecycle_notification_batch(
        self,
        batch_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                WHERE batch_id = ?
                """,
                (str(batch_id or "").strip(),),
            ).fetchone()
        return (
            _notification_delivery_batch_row(row)
            if row is not None
            else None
        )

    def list_trade_lifecycle_notification_batches(
        self,
        *,
        status: str | None = None,
        route_fingerprint: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if route_fingerprint:
            clauses.append("route_fingerprint = ?")
            params.append(str(route_fingerprint).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                {where}
                ORDER BY created_at_ms ASC, batch_id ASC
                """,
                params,
            ).fetchall()
        return [_notification_delivery_batch_row(row) for row in rows]

    def list_trade_lifecycle_notification_batch_members(
        self,
        batch_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE delivery_batch_id = ?
                ORDER BY created_at_ms ASC, outbox_id ASC
                """,
                (str(batch_id or "").strip(),),
            ).fetchall()
        return [_notification_outbox_row(row) for row in rows]

    def compare_and_set_trade_lifecycle_notification_batch(
        self,
        *,
        batch_id: str,
        expected_status: str,
        new_status: str,
        claim_id: str | None = None,
        expected_claim_id: str | None = None,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        allowed_fields = {
            "provider_message_id",
            "claim_id",
            "claimed_at_ms",
            "send_started_at_ms",
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "provider_receipt_json",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification delivery batch fields: "
                + ",".join(invalid)
            )
        if claim_id is not None:
            updates["claim_id"] = claim_id
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            value = updates[key]
            if key == "provider_receipt_json" and isinstance(value, dict):
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        clauses = ["batch_id = ?", "status = ?"]
        values.extend(
            (
                str(batch_id or "").strip(),
                str(expected_status or "").strip().lower(),
            )
        )
        if expected_claim_id is not None:
            clauses.append("claim_id = ?")
            values.append(str(expected_claim_id))
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_delivery_batches
                SET {', '.join(assignments)}
                WHERE {' AND '.join(clauses)}
                """,
                values,
            )
        return int(cursor.rowcount or 0) == 1

    def update_trade_lifecycle_notification_batch_members(
        self,
        *,
        batch_id: str,
        expected_statuses: Sequence[str],
        new_status: str,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        statuses = tuple(
            str(value or "").strip().lower()
            for value in expected_statuses
            if str(value or "").strip()
        )
        if not statuses:
            raise ValueError(
                "notification batch member expected status is required"
            )
        allowed_fields = {
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification batch member fields: "
                + ",".join(invalid)
            )
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            assignments.append(f"{key} = ?")
            values.append(updates[key])
        placeholders = ",".join("?" for _ in statuses)
        values.append(str(batch_id or "").strip())
        values.extend(statuses)
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET {', '.join(assignments)}
                WHERE delivery_batch_id = ?
                  AND status IN ({placeholders})
                """,
                values,
            )
        return int(cursor.rowcount or 0)
