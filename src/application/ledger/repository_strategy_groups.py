from __future__ import annotations

from .repository_schema import (
    Any,
    Sequence,
    _assert_same_combo_pair_inference_identity,
    _combo_pair_inference_sql_values,
    _json_object,
    _json_text,
    _normalize_combo_pair_inference_payload,
    json,
    now_ms,
    sqlite3,
)

class StrategyGroupRepositoryMixin:
    def insert_strategy_group_identity(
        self,
        identity: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(identity or {})
        group_id = str(payload.get("group_id") or "").strip()
        identity_hash = str(payload.get("identity_hash") or "").strip()
        if not group_id or not identity_hash:
            raise ValueError("strategy group identity requires group_id and identity_hash")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT identity_hash FROM strategy_group_identities WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["identity_hash"] or "") != identity_hash:
                    raise ValueError(f"strategy group identity conflict for group_id={group_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO strategy_group_identities (
                  group_id, schema_version, strategy, account, symbol,
                  funding_put_record_id, funding_put_open_event_id, funding_put_contract_key,
                  participation_call_record_id, participation_call_open_event_id,
                  participation_call_contract_key, original_contracts, created_at_ms,
                  identity_hash, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    str(payload.get("schema_version") or "").strip(),
                    str(payload.get("strategy") or "").strip().lower(),
                    str(payload.get("account") or "").strip().lower(),
                    str(payload.get("symbol") or "").strip().upper(),
                    str(payload.get("funding_put_record_id") or "").strip(),
                    str(payload.get("funding_put_open_event_id") or "").strip(),
                    _json_text(payload.get("funding_put_contract_key")),
                    str(payload.get("participation_call_record_id") or "").strip(),
                    str(payload.get("participation_call_open_event_id") or "").strip(),
                    _json_text(payload.get("participation_call_contract_key")),
                    int(payload.get("original_contracts") or 0),
                    int(payload.get("created_at_ms") or now_ms()),
                    identity_hash,
                    raw_json,
                ),
            )
        return True

    def get_strategy_group_identity(
        self,
        group_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM strategy_group_identities WHERE group_id = ?",
                (str(group_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_strategy_group_identities(
        self,
        *,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE account = ?" if account else ""
        params = (str(account).strip().lower(),) if account else ()
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json FROM strategy_group_identities
                {where}
                ORDER BY account ASC, symbol ASC, group_id ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def upsert_combo_pair_inference(
        self,
        inference: dict[str, Any],
        *,
        reactivate_stale: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = _normalize_combo_pair_inference_payload(inference)
        inference_id = str(payload["inference_id"])
        with self._optional_conn(conn, commit=True) as active_conn:
            existing_row = active_conn.execute(
                """
                SELECT raw_json, status, created_at_ms
                FROM combo_pair_inferences
                WHERE inference_id = ?
                """,
                (inference_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _json_object(existing_row["raw_json"])
                _assert_same_combo_pair_inference_identity(existing, payload)
                existing_status = str(existing_row["status"] or "").strip().lower()
                reactivating = (
                    bool(reactivate_stale)
                    and existing_status == "expired_unresolved"
                    and str(existing.get("decision_reason") or "").strip()
                    == "facts_drifted_or_leg_claimed"
                )
                if (
                    existing_status not in {"proposal_ready", "ambiguous"}
                    and not reactivating
                ):
                    return False
                created_at_ms = int(existing_row["created_at_ms"])
            else:
                reactivating = False
                created_at_ms = int(payload.get("created_at_ms") or now_ms())
            updated_at_ms = int(now_ms())
            payload["created_at_ms"] = created_at_ms
            payload["updated_at_ms"] = updated_at_ms
            raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            values = _combo_pair_inference_sql_values(
                payload,
                raw_json=raw_json,
            )
            if existing_row is None:
                active_conn.execute(
                    """
                    INSERT INTO combo_pair_inferences (
                      inference_id, schema_version, algorithm_version,
                      account, symbol, market, market_date,
                      put_record_id, put_open_event_id,
                      call_record_id, call_open_event_id,
                      evidence_grade,
                      candidate_occurrence_ids_json,
                      candidate_exposure_ids_json,
                      input_snapshot_hash, status, proposal_expires_at_ms,
                      evidence_json, alternatives_json, strategy_group_id,
                      identity_hash, put_adoption_event_id, call_adoption_event_id,
                      put_void_event_id, call_void_event_id,
                      decision_at_ms, decision_by, decision_reason,
                      created_at_ms, updated_at_ms, raw_json
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
                return True
            active_conn.execute(
                """
                UPDATE combo_pair_inferences
                SET algorithm_version = ?, evidence_grade = ?,
                    candidate_occurrence_ids_json = ?,
                    candidate_exposure_ids_json = ?,
                    input_snapshot_hash = ?, status = ?,
                    proposal_expires_at_ms = ?, evidence_json = ?,
                    alternatives_json = ?, strategy_group_id = ?,
                    decision_at_ms = NULL, decision_by = NULL,
                    decision_reason = NULL,
                    updated_at_ms = ?, raw_json = ?
                WHERE inference_id = ?
                  AND (
                    status IN ('proposal_ready', 'ambiguous')
                    OR (
                      ? = 1
                      AND status = 'expired_unresolved'
                      AND decision_reason = 'facts_drifted_or_leg_claimed'
                    )
                  )
                """,
                (
                    str(payload["algorithm_version"]),
                    str(payload["evidence_grade"]),
                    _json_text(payload["candidate_occurrence_ids"]),
                    _json_text(payload["candidate_exposure_ids"]),
                    str(payload["input_snapshot_hash"]),
                    str(payload["status"]),
                    int(payload["proposal_expires_at_ms"]),
                    _json_text(payload["evidence"]),
                    _json_text(payload["alternative_inference_ids"]),
                    str(payload["strategy_group_id"]),
                    updated_at_ms,
                    raw_json,
                    inference_id,
                    int(reactivating),
                ),
            )
        return False

    def get_combo_pair_inference(
        self,
        inference_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM combo_pair_inferences
                WHERE inference_id = ?
                """,
                (str(inference_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def transition_combo_pair_inference(
        self,
        *,
        inference_id: str,
        expected_statuses: Sequence[str],
        new_status: str,
        expected_input_hash: str | None = None,
        decision_fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        inference_value = str(inference_id or "").strip()
        expected = sorted(
            {str(item or "").strip().lower() for item in expected_statuses}
            - {""}
        )
        status_value = str(new_status or "").strip().lower()
        allowed_statuses = {
            "proposal_ready",
            "ambiguous",
            "user_confirmed",
            "user_rejected",
            "expired_unresolved",
            "superseded",
        }
        if not inference_value or not expected or status_value not in allowed_statuses:
            raise ValueError("combo inference transition is incomplete")
        allowed_fields = {
            "decision_at_ms",
            "decision_by",
            "decision_reason",
            "strategy_group_id",
            "identity_hash",
            "put_adoption_event_id",
            "call_adoption_event_id",
            "put_void_event_id",
            "call_void_event_id",
        }
        updates = dict(decision_fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported combo inference decision fields: " + ",".join(invalid)
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json, status, input_snapshot_hash FROM combo_pair_inferences WHERE inference_id = ?",
                (inference_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"combo inference not found: {inference_value}")
            current_status = str(row["status"] or "").strip().lower()
            if current_status not in expected:
                raise ValueError(
                    f"combo inference status compare-and-set failed: {current_status}"
                )
            if (
                expected_input_hash is not None
                and str(row["input_snapshot_hash"] or "").strip()
                != str(expected_input_hash or "").strip()
            ):
                raise ValueError("combo inference input hash compare-and-set failed")
            payload = _json_object(row["raw_json"])
            payload.update(updates)
            payload["status"] = status_value
            updated_at_ms = int(updates.get("decision_at_ms") or now_ms())
            payload["updated_at_ms"] = updated_at_ms
            cursor = active_conn.execute(
                """
                UPDATE combo_pair_inferences
                SET status = ?, decision_at_ms = ?, decision_by = ?,
                    decision_reason = ?, strategy_group_id = ?, identity_hash = ?,
                    put_adoption_event_id = ?, call_adoption_event_id = ?,
                    put_void_event_id = ?, call_void_event_id = ?,
                    updated_at_ms = ?, raw_json = ?
                WHERE inference_id = ? AND status = ?
                """,
                (
                    status_value,
                    payload.get("decision_at_ms"),
                    payload.get("decision_by"),
                    payload.get("decision_reason"),
                    payload.get("strategy_group_id"),
                    payload.get("identity_hash"),
                    payload.get("put_adoption_event_id"),
                    payload.get("call_adoption_event_id"),
                    payload.get("put_void_event_id"),
                    payload.get("call_void_event_id"),
                    updated_at_ms,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    inference_value,
                    current_status,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("combo inference status compare-and-set failed")
        return payload

    def list_combo_pair_inferences(
        self,
        *,
        account: str | None = None,
        status: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if account:
            clauses.append("account = ?")
            values.append(str(account).strip().lower())
        if status:
            clauses.append("status = ?")
            values.append(str(status).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM combo_pair_inferences
                {where}
                ORDER BY account ASC, market_date DESC, symbol ASC,
                         updated_at_ms DESC, inference_id ASC
                """,
                values,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def expire_combo_pair_inferences(
        self,
        *,
        effective_now_ms: int,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        cutoff = int(effective_now_ms)
        if cutoff <= 0:
            raise ValueError("effective_now_ms must be > 0")
        clauses = [
            "status IN ('proposal_ready', 'ambiguous')",
            "proposal_expires_at_ms < ?",
        ]
        values: list[Any] = [cutoff]
        if account:
            clauses.append("account = ?")
            values.append(str(account).strip().lower())
        with self._optional_conn(conn, commit=True) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT inference_id, raw_json
                FROM combo_pair_inferences
                WHERE {' AND '.join(clauses)}
                ORDER BY inference_id ASC
                """,
                values,
            ).fetchall()
            updated = 0
            for row in rows:
                payload = _json_object(row["raw_json"])
                payload["status"] = "expired_unresolved"
                payload["updated_at_ms"] = cutoff
                payload["decision_at_ms"] = cutoff
                payload["decision_reason"] = "proposal_expired"
                active_conn.execute(
                    """
                    UPDATE combo_pair_inferences
                    SET status = 'expired_unresolved', decision_at_ms = ?,
                        decision_reason = 'proposal_expired', updated_at_ms = ?,
                        raw_json = ?
                    WHERE inference_id = ?
                      AND status IN ('proposal_ready', 'ambiguous')
                    """,
                    (
                        cutoff,
                        cutoff,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        str(row["inference_id"]),
                    ),
                )
                updated += 1
        return updated

    def expire_stale_combo_pair_inferences(
        self,
        *,
        account: str,
        active_inference_ids: Sequence[str],
        effective_now_ms: int,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("account is required to expire stale combo inferences")
        changed_at_ms = int(effective_now_ms)
        if changed_at_ms <= 0:
            raise ValueError("effective_now_ms must be > 0")
        active_ids = {
            str(item).strip() for item in active_inference_ids if str(item).strip()
        }
        with self._optional_conn(conn, commit=True) as active_conn:
            rows = active_conn.execute(
                """
                SELECT inference_id, raw_json
                FROM combo_pair_inferences
                WHERE account = ?
                  AND status IN ('proposal_ready', 'ambiguous')
                ORDER BY inference_id ASC
                """,
                (account_value,),
            ).fetchall()
            updated = 0
            for row in rows:
                inference_id = str(row["inference_id"])
                if inference_id in active_ids:
                    continue
                payload = _json_object(row["raw_json"])
                payload["status"] = "expired_unresolved"
                payload["updated_at_ms"] = changed_at_ms
                payload["decision_at_ms"] = changed_at_ms
                payload["decision_reason"] = "facts_drifted_or_leg_claimed"
                cursor = active_conn.execute(
                    """
                    UPDATE combo_pair_inferences
                    SET status = 'expired_unresolved', decision_at_ms = ?,
                        decision_reason = 'facts_drifted_or_leg_claimed',
                        updated_at_ms = ?, raw_json = ?
                    WHERE inference_id = ?
                      AND status IN ('proposal_ready', 'ambiguous')
                    """,
                    (
                        changed_at_ms,
                        changed_at_ms,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        inference_id,
                    ),
                )
                updated += int(cursor.rowcount or 0)
        return updated
