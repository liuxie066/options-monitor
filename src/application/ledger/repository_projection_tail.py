from __future__ import annotations

from .repository_schema import (
    Any,
    POSITION_PROJECTION_SCHEMA,
    PositionLotDiff,
    PositionLotRecord,
    PositionProjectionAccountSnapshot,
    Sequence,
    _canonical_existing_fields_json,
    _position_lot_storage_values,
    _position_projection_column_contract,
    _position_projection_column_contract_is_closed,
    _projection_schema_cookie,
    _storage_scalar_matches,
    json,
    now_ms,
    ordered_position_lots_fingerprint,
    position_lot_row_to_record,
    sqlite3,
)

class PositionProjectionTailRepositoryMixin:
    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        return self.apply_position_lot_diff(records, conn=conn).lot_count

    def apply_position_lot_diff(
        self,
        records: Sequence[PositionLotRecord],
        *,
        remove_missing: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> PositionLotDiff:
        desired: dict[
            str,
            tuple[str, str, str, str | None, int | None, float | None, float | None],
        ] = {}
        for record in records:
            values = _position_lot_storage_values(record)
            record_id = values[0]
            if record_id in desired:
                raise ValueError(f"duplicate position lot record_id: {record_id}")
            desired[record_id] = values

        added = 0
        changed = 0
        removed = 0
        unchanged = 0
        all_accounts = {values[1] for values in desired.values()}
        touched_accounts: set[str] = set()
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            if remove_missing:
                current_rows = active_conn.execute(
                    """
                    SELECT record_id, account, fields_json, source_event_id,
                           expiration, strike, multiplier
                    FROM position_lots
                    ORDER BY record_id ASC
                    """
                ).fetchall()
                prior_lot_count = len(current_rows)
            else:
                record_ids = tuple(desired)
                if record_ids:
                    placeholders = ",".join("?" for _item in record_ids)
                    current_rows = active_conn.execute(
                        f"""
                        SELECT record_id, account, fields_json, source_event_id,
                               expiration, strike, multiplier
                        FROM position_lots
                        WHERE record_id IN ({placeholders})
                        ORDER BY record_id ASC
                        """,
                        record_ids,
                    ).fetchall()
                else:
                    current_rows = []
                head_rows = active_conn.execute(
                    """
                    SELECT account, lot_count
                    FROM position_projection_heads
                    ORDER BY account ASC
                    """
                ).fetchall()
                all_accounts.update(str(row["account"]) for row in head_rows)
                prior_lot_count = sum(int(row["lot_count"] or 0) for row in head_rows)
            current_by_id = {str(row["record_id"]): row for row in current_rows}

            for record_id, row in current_by_id.items():
                old_account = str(row["account"] or "").strip()
                if not old_account:
                    raw_fields = json.loads(str(row["fields_json"]) or "{}")
                    old_account = str(raw_fields.get("account") if isinstance(raw_fields, dict) else "").strip()
                if old_account:
                    all_accounts.add(old_account)
                if record_id in desired or not remove_missing:
                    continue
                active_conn.execute(
                    "DELETE FROM position_lots WHERE record_id = ?",
                    (record_id,),
                )
                removed += 1
                if old_account:
                    touched_accounts.add(old_account)

            for record_id, values in desired.items():
                (
                    _record_id,
                    account,
                    fields_json,
                    source_event_id,
                    expiration_ms,
                    strike,
                    multiplier,
                ) = values
                current = current_by_id.get(record_id)
                if current is None:
                    active_conn.execute(
                        """
                        INSERT INTO position_lots (
                          record_id, account, fields_json, source_event_id,
                          expiration, strike, multiplier, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (*values, ts),
                    )
                    added += 1
                    touched_accounts.add(account)
                    continue

                raw_current_fields_json = str(current["fields_json"] or "{}")
                current_fields_json = (
                    raw_current_fields_json
                    if raw_current_fields_json == fields_json
                    else _canonical_existing_fields_json(raw_current_fields_json)
                )
                public_changed = current_fields_json != fields_json or current["source_event_id"] != source_event_id
                scalar_conflict = any(
                    current[column] is not None and not _storage_scalar_matches(current[column], desired_value)
                    for column, desired_value in (
                        ("expiration", expiration_ms),
                        ("strike", strike),
                        ("multiplier", multiplier),
                    )
                )
                if not public_changed and not scalar_conflict:
                    # Explicit migration owns historical sidecar backfill. Existing
                    # public bytes remain unchanged even if a legacy scalar is null.
                    unchanged += 1
                    continue

                old_fields = json.loads(str(current["fields_json"]) or "{}")
                old_account = str(
                    current["account"] or (old_fields.get("account") if isinstance(old_fields, dict) else "") or ""
                ).strip()
                active_conn.execute(
                    """
                    UPDATE position_lots
                    SET account = ?, fields_json = ?, source_event_id = ?,
                        expiration = ?, strike = ?, multiplier = ?, updated_at_ms = ?
                    WHERE record_id = ?
                    """,
                    (
                        account,
                        fields_json,
                        source_event_id,
                        expiration_ms,
                        strike,
                        multiplier,
                        ts,
                        record_id,
                    ),
                )
                changed += 1
                touched_accounts.add(account)
                if old_account:
                    touched_accounts.add(old_account)

        final_lot_count = len(desired) if remove_missing else prior_lot_count + added
        unchanged_count = (
            unchanged
            if remove_missing
            else max(0, final_lot_count - added - changed)
        )
        return PositionLotDiff(
            added=added,
            changed=changed,
            removed=removed,
            unchanged=unchanged_count,
            accounts=tuple(sorted(all_accounts)),
            touched_accounts=tuple(sorted(touched_accounts)),
        )

    def position_projection_column_contract(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        with self._optional_conn(conn) as active_conn:
            return _position_projection_column_contract(active_conn)

    def position_projection_schema_cookie(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        with self._optional_conn(conn) as active_conn:
            return _projection_schema_cookie(active_conn)

    def position_projection_indexes_ready(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        required = {
            "idx_trade_events_trade_time",
            "idx_trade_events_account_time",
            "idx_position_lots_account_expiration",
            "idx_position_lots_account_record",
        }
        with self._optional_conn(conn) as active_conn:
            present = {
                str(row["name"])
                for row in active_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
        return required.issubset(present)

    def position_projection_normalized_columns_ready(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        with self._optional_conn(conn) as active_conn:
            event_problem = active_conn.execute(
                """
                SELECT 1
                FROM trade_events
                WHERE account IS NULL
                   OR account = ''
                   OR account != lower(account)
                   OR account != coalesce(
                        nullif(trim(CAST(json_extract(
                          event_json, '$.contract_key.account'
                        ) AS TEXT)), ''),
                        trim(CAST(json_extract(event_json, '$.account') AS TEXT))
                      )
                LIMIT 1
                """
            ).fetchone()
            lot_problem = active_conn.execute(
                """
                SELECT 1
                FROM position_lots
                WHERE account IS NULL
                   OR account = ''
                   OR account != lower(account)
                   OR account != trim(CAST(
                        json_extract(fields_json, '$.account') AS TEXT
                      ))
                   OR (
                        json_extract(fields_json, '$.option_type') IN ('put', 'call')
                        AND (expiration IS NULL OR strike IS NULL OR multiplier IS NULL)
                   )
                LIMIT 1
                """
            ).fetchone()
        return event_problem is None and lot_problem is None

    def list_position_projection_accounts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT account FROM position_projection_heads
                ORDER BY account ASC
                """
            ).fetchall()
        return tuple(str(row["account"]) for row in rows if str(row["account"] or "").strip())

    def position_projection_account_snapshot(
        self,
        account: str,
        *,
        include_records: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> PositionProjectionAccountSnapshot:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("position projection account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            cursor = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE account = ?
                ORDER BY record_id ASC
                """,
                (account_value,),
            )
            retained: list[dict[str, Any]] = []
            lot_count = 0

            def _ordered_rows():
                nonlocal lot_count
                for row in cursor:
                    record = position_lot_row_to_record(row)
                    lot_count += 1
                    if include_records:
                        retained.append(record)
                    yield record

            fingerprint = ordered_position_lots_fingerprint(_ordered_rows())
        return PositionProjectionAccountSnapshot(
            account=account_value,
            fingerprint=fingerprint,
            lot_count=lot_count,
            records=tuple(retained),
        )

    def publish_full_position_projection_heads(
        self,
        *,
        implementation_fingerprint: str,
        known_accounts: Sequence[str],
        changed_accounts: Sequence[str],
        full_verified: bool = True,
        publish_source_implementation: bool = True,
        readiness_prevalidated: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, str | None]:
        fingerprint = str(implementation_fingerprint or "").strip()
        if len(fingerprint) != 64:
            raise ValueError("projector implementation fingerprint is required")
        with self._optional_conn(conn, commit=True) as active_conn:
            source = active_conn.execute(
                """
                SELECT source_generation, sqlite_schema_cookie
                FROM position_projection_source_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if source is None:
                raise RuntimeError("position projection source state is missing")
            source_generation = int(source["source_generation"])
            schema_cookie = _projection_schema_cookie(active_conn)
            ready = bool(readiness_prevalidated) or _position_projection_column_contract_is_closed(
                active_conn
            )
            reason: str | None = None
            if readiness_prevalidated:
                pass
            elif not ready:
                reason = "column_contract_open"
            elif not self.position_projection_indexes_ready(conn=active_conn):
                ready = False
                reason = "normalized_indexes_missing"
            elif not self.position_projection_normalized_columns_ready(conn=active_conn):
                ready = False
                reason = "normalized_columns_incomplete"

            accounts = set(self.list_position_projection_accounts(conn=active_conn))
            accounts.update(str(item or "").strip() for item in known_accounts)
            accounts.update(str(item or "").strip() for item in changed_accounts)
            accounts = {account for account in accounts if account and account == account.lower()}
            ts = int(now_ms())
            total = 0
            changed = {str(item or "").strip() for item in changed_accounts}
            for account in sorted(accounts):
                head = active_conn.execute(
                    """
                    SELECT lots_generation, built_lots_generation,
                           projection_fingerprint, lot_count, status,
                           projector_schema, projector_implementation_fingerprint
                    FROM position_projection_heads
                    WHERE account = ?
                    """,
                    (account,),
                ).fetchone()
                can_reuse = (
                    account not in changed
                    and head is not None
                    and str(head["status"] or "") == "trusted"
                    and str(head["projector_schema"] or "") == POSITION_PROJECTION_SCHEMA
                    and str(head["projector_implementation_fingerprint"] or "") == fingerprint
                    and head["built_lots_generation"] is not None
                    and int(head["lots_generation"]) == int(head["built_lots_generation"])
                    and bool(str(head["projection_fingerprint"] or ""))
                )
                if can_reuse:
                    account_fingerprint = str(head["projection_fingerprint"])
                    lot_count = int(head["lot_count"])
                else:
                    snapshot = self.position_projection_account_snapshot(
                        account,
                        conn=active_conn,
                    )
                    account_fingerprint = snapshot.fingerprint
                    lot_count = snapshot.lot_count
                total += lot_count
                lots_generation = int(head["lots_generation"] or 0) if head else 0
                active_conn.execute(
                    """
                    INSERT INTO position_projection_heads (
                      account, lots_generation, built_source_generation,
                      built_lots_generation, projection_fingerprint, lot_count,
                      projector_schema, projector_implementation_fingerprint,
                      status, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account) DO UPDATE SET
                      built_source_generation = excluded.built_source_generation,
                      built_lots_generation = excluded.built_lots_generation,
                      projection_fingerprint = excluded.projection_fingerprint,
                      lot_count = excluded.lot_count,
                      projector_schema = excluded.projector_schema,
                      projector_implementation_fingerprint =
                        excluded.projector_implementation_fingerprint,
                      status = excluded.status,
                      updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        account,
                        lots_generation,
                        source_generation,
                        lots_generation,
                        account_fingerprint,
                        lot_count,
                        POSITION_PROJECTION_SCHEMA,
                        fingerprint,
                        "trusted" if ready else "untrusted",
                        ts,
                    ),
                )
            active_conn.execute(
                """
                UPDATE position_projection_source_state
                SET projector_schema = ?,
                    projector_implementation_fingerprint = CASE
                      WHEN ? THEN ?
                      ELSE projector_implementation_fingerprint
                    END,
                    sqlite_schema_cookie = CASE
                      WHEN ? THEN ?
                      ELSE sqlite_schema_cookie
                    END,
                    last_full_verified_source_generation = CASE
                      WHEN ? THEN ?
                      ELSE last_full_verified_source_generation
                    END,
                    updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (
                    POSITION_PROJECTION_SCHEMA,
                    1 if publish_source_implementation else 0,
                    fingerprint,
                    1 if publish_source_implementation else 0,
                    schema_cookie,
                    1 if full_verified else 0,
                    source_generation,
                    ts,
                ),
            )
        return total, ready, reason

    def read_position_projection_account_metadata(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("position projection account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            source = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
            head = active_conn.execute(
                "SELECT * FROM position_projection_heads WHERE account = ?",
                (account_value,),
            ).fetchone()
            cookie = _projection_schema_cookie(active_conn)
        return {
            "source": dict(source) if source is not None else None,
            "head": dict(head) if head is not None else None,
            "schema_cookie": cookie,
        }

    def read_position_projection_source_state(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("position projection source state is missing")
        return dict(row)

    def set_position_projection_checkpoint_mode(
        self,
        mode: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        value = str(mode or "").strip().lower()
        if value not in {"disabled", "enabled", "untrusted"}:
            raise ValueError("checkpoint mode must be disabled, enabled, or untrusted")
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute(
                """
                UPDATE position_projection_source_state
                SET checkpoint_mode = ?, updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (value, int(now_ms())),
            )

    def list_position_projection_checkpoints(
        self,
        *,
        trusted_only: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE trust_status = 'trusted'" if trusted_only else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT * FROM position_projection_checkpoints
                {where}
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def read_newest_trusted_position_projection_checkpoint(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT * FROM position_projection_checkpoints
                WHERE trust_status = 'trusted'
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_position_projection_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        columns = (
            "checkpoint_id",
            "projector_schema",
            "projector_implementation_fingerprint",
            "prefix_event_count",
            "prefix_end_trade_time_ms",
            "prefix_end_event_id",
            "prefix_chain_sha256",
            "source_generation",
            "sqlite_schema_cookie",
            "accumulator_json",
            "accumulator_sha256",
            "diagnostic_count",
            "diagnostic_sha256",
            "state_bytes",
            "trust_status",
            "verification_kind",
            "parent_checkpoint_id",
            "created_at_ms",
            "verified_at_ms",
            "invalidated_at_ms",
            "invalidation_reason",
        )
        if set(checkpoint) != set(columns):
            raise ValueError("position projection checkpoint fields differ from v1 schema")
        placeholders = ",".join("?" for _item in columns)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT * FROM position_projection_checkpoints WHERE checkpoint_id = ?",
                (checkpoint["checkpoint_id"],),
            ).fetchone()
            if existing is not None:
                if checkpoint["verification_kind"] == "full_oracle":
                    mutable_columns = tuple(
                        column for column in columns if column != "checkpoint_id"
                    )
                    active_conn.execute(
                        f"""
                        UPDATE position_projection_checkpoints
                        SET {','.join(f'{column} = ?' for column in mutable_columns)}
                        WHERE checkpoint_id = ?
                        """,
                        (
                            *(checkpoint[column] for column in mutable_columns),
                            checkpoint["checkpoint_id"],
                        ),
                    )
                    return
                immutable = set(columns) - {
                    "verification_kind",
                    "parent_checkpoint_id",
                    "created_at_ms",
                    "verified_at_ms",
                    "trust_status",
                    "invalidated_at_ms",
                    "invalidation_reason",
                }
                if any(existing[column] != checkpoint[column] for column in immutable):
                    raise ValueError("checkpoint id conflicts with immutable payload")
                return
            active_conn.execute(
                f"""
                INSERT INTO position_projection_checkpoints ({','.join(columns)})
                VALUES ({placeholders})
                """,
                tuple(checkpoint[column] for column in columns),
            )

    def invalidate_position_projection_checkpoints(
        self,
        *,
        reason: str,
        checkpoint_ids: Sequence[str] = (),
        mark_mode_untrusted: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        reason_value = str(reason or "").strip()
        if not reason_value:
            raise ValueError("checkpoint invalidation reason is required")
        normalized = tuple(
            dict.fromkeys(str(item or "").strip() for item in checkpoint_ids)
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            ts = int(now_ms())
            if normalized:
                placeholders = ",".join("?" for _item in normalized)
                cursor = active_conn.execute(
                    f"""
                    UPDATE position_projection_checkpoints
                    SET trust_status = 'invalid', invalidated_at_ms = ?,
                        invalidation_reason = ?
                    WHERE trust_status = 'trusted'
                      AND checkpoint_id IN ({placeholders})
                    """,
                    (ts, reason_value, *normalized),
                )
            else:
                cursor = active_conn.execute(
                    """
                    UPDATE position_projection_checkpoints
                    SET trust_status = 'invalid', invalidated_at_ms = ?,
                        invalidation_reason = ?
                    WHERE trust_status = 'trusted'
                    """,
                    (ts, reason_value),
                )
            if mark_mode_untrusted:
                active_conn.execute(
                    """
                    UPDATE position_projection_source_state
                    SET checkpoint_mode = 'untrusted', updated_at_ms = ?
                    WHERE singleton_id = 1
                    """,
                    (ts,),
                )
        return int(cursor.rowcount)

    def prune_position_projection_checkpoints(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        with self._optional_conn(conn, commit=True) as active_conn:
            invalid = active_conn.execute(
                "SELECT checkpoint_id FROM position_projection_checkpoints WHERE trust_status = 'invalid'"
            ).fetchall()
            trusted = active_conn.execute(
                """
                SELECT checkpoint_id, verification_kind
                FROM position_projection_checkpoints
                WHERE trust_status = 'trusted'
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                """
            ).fetchall()
            keep = {str(row["checkpoint_id"]) for row in trusted[:2]}
            full = next(
                (
                    str(row["checkpoint_id"])
                    for row in trusted
                    if str(row["verification_kind"]) == "full_oracle"
                ),
                None,
            )
            if full is not None:
                keep.add(full)
            removable = [*invalid, *trusted]
            removed = tuple(
                sorted(
                    str(row["checkpoint_id"])
                    for row in removable
                    if str(row["checkpoint_id"]) not in keep
                )
            )
            if removed:
                placeholders = ",".join("?" for _item in removed)
                active_conn.execute(
                    f"DELETE FROM position_projection_checkpoints WHERE checkpoint_id IN ({placeholders})",
                    removed,
                )
        return removed

    def list_active_position_lots(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("position projection account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE account = ?
                  AND json_extract(fields_json, '$.status') = 'open'
                ORDER BY expiration ASC, record_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def get_position_lots_by_ids(
        self,
        record_ids: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in record_ids))
        if not normalized or any(not item for item in normalized):
            return []
        placeholders = ",".join("?" for _item in normalized)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE record_id IN ({placeholders})
                ORDER BY record_id ASC
                """,
                normalized,
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                ORDER BY record_id DESC
                """
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def get_position_lot_fields(
        self,
        record_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE record_id = ?
                """,
                (str(record_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"position lot not found: {record_id}")
        return position_lot_row_to_record(row)["fields"]

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.list_position_lots()

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.get_position_lot_fields(record_id)
