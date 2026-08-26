from __future__ import annotations

from .repository_schema import (
    Any,
    Mapping,
    Sequence,
    _json_object,
    _json_text,
    _lifecycle_case_immutable_payload,
    _normalized_lifecycle_case_targets,
    json,
    now_ms,
    read_current_decision_projection_inputs_from_conn,
    sqlite3,
)

class LifecycleCaseRepositoryMixin:
    def upsert_trade_lifecycle_case(
        self,
        case: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(case or {})
        case_id = str(payload.get("case_id") or "").strip()
        case_key = str(payload.get("case_key") or "").strip()
        if not case_id or not case_key:
            raise ValueError("trade lifecycle case requires case_id and case_key")
        account = str(payload.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("trade lifecycle case requires lowercase account")
        payload["account"] = account
        target_lot_ids, target_contracts, target_rows = _normalized_lifecycle_case_targets(
            payload,
            case_id=case_id,
            account=account,
        )
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json, created_at_ms FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            created_at_ms = int(existing["created_at_ms"]) if existing is not None else ts
            changed = existing is None or str(existing["raw_json"] or "") != raw_json
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                  case_id, case_key, account, broker, symbol, option_type, position_side,
                  strike, expiration_ymd, contract_key, status, decision_type,
                  target_lot_ids_json, target_contracts_by_lot_json,
                  observation_start_ms, pending_until_ms, created_at_ms, updated_at_ms,
                  raw_json
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(case_id) DO UPDATE SET
                  case_key = excluded.case_key,
                  account = excluded.account,
                  broker = excluded.broker,
                  symbol = excluded.symbol,
                  option_type = excluded.option_type,
                  position_side = excluded.position_side,
                  strike = excluded.strike,
                  expiration_ymd = excluded.expiration_ymd,
                  contract_key = excluded.contract_key,
                  status = excluded.status,
                  decision_type = excluded.decision_type,
                  target_lot_ids_json = excluded.target_lot_ids_json,
                  target_contracts_by_lot_json = excluded.target_contracts_by_lot_json,
                  observation_start_ms = excluded.observation_start_ms,
                  pending_until_ms = excluded.pending_until_ms,
                  updated_at_ms = excluded.updated_at_ms,
                  raw_json = excluded.raw_json
                """,
                (
                    case_id,
                    case_key,
                    account,
                    str(payload.get("broker") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper(),
                    (str(payload.get("option_type") or "").strip().lower() or None),
                    (str(payload.get("position_side") or "").strip().lower() or None),
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    (str(payload.get("expiration_ymd") or "").strip() or None),
                    (str(payload.get("contract_key") or "").strip() or None),
                    str(payload.get("status") or "pending").strip().lower(),
                    (str(payload.get("decision_type") or "").strip().lower() or None),
                    json.dumps(list(target_lot_ids), ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        target_contracts,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    (int(payload["observation_start_ms"]) if payload.get("observation_start_ms") is not None else None),
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    created_at_ms,
                    ts,
                    raw_json,
                ),
            )
            if changed:
                active_conn.execute(
                    "DELETE FROM trade_lifecycle_case_targets WHERE case_id = ?",
                    (case_id,),
                )
                active_conn.executemany(
                    """
                    INSERT INTO trade_lifecycle_case_targets (
                      case_id, account, target_lot_id, target_contracts
                    ) VALUES (?, ?, ?, ?)
                    """,
                    target_rows,
                )
        return changed

    def get_trade_lifecycle_case(
        self, case_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (str(case_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["raw_json"]) or "{}")
        return dict(payload) if isinstance(payload, dict) else None

    def get_trade_lifecycle_case_by_key(
        self,
        case_key: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_key = ?",
                (str(case_key or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["raw_json"]) or "{}")
        return dict(payload) if isinstance(payload, dict) else None

    def list_trade_lifecycle_cases(
        self,
        *,
        status: str | None = None,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if account:
            clauses.append("account = ?")
            params.append(str(account).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_cases
                {where}
                ORDER BY updated_at_ms DESC, case_id DESC
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["raw_json"]) or "{}")
            if isinstance(payload, dict):
                out.append(dict(payload))
        return out

    def list_trade_lifecycle_case_targets_for_lots(
        self,
        *,
        account: str,
        target_lot_ids: Sequence[str],
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("lifecycle case target account must be lowercase")
        lot_ids = tuple(dict.fromkeys(str(value or "").strip() for value in target_lot_ids))
        if any(not value for value in lot_ids):
            raise ValueError("lifecycle case target lot id is required")
        if not lot_ids:
            return []
        placeholders = ",".join("?" for _value in lot_ids)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT case_id, account, target_lot_id, target_contracts
                FROM trade_lifecycle_case_targets
                WHERE account = ? AND target_lot_id IN ({placeholders})
                ORDER BY target_lot_id ASC, case_id ASC
                """,
                (account_value, *lot_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_current_decision_storage_state(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("current decision account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            generation = active_conn.execute(
                """
                SELECT *
                FROM current_decision_input_generations
                WHERE account = ?
                """,
                (account_value,),
            ).fetchone()
            projection = active_conn.execute(
                """
                SELECT *
                FROM current_decision_projections
                WHERE account = ?
                """,
                (account_value,),
            ).fetchone()
        return {
            "generation": dict(generation) if generation is not None else None,
            "projection": dict(projection) if projection is not None else None,
        }

    def read_current_decision_projection_inputs(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
        include_identities: bool = True,
    ) -> dict[str, Any]:
        """Read one account's bounded current-state inputs from one snapshot."""
        with self._optional_conn(conn) as active_conn:
            return read_current_decision_projection_inputs_from_conn(
                active_conn,
                account,
                include_identities=include_identities,
            )

    def read_current_decision_projection_fence_inputs(
        self,
        accounts: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Read only the bounded metadata needed by the publication fence."""

        account_values = tuple(
            sorted({str(value or "").strip() for value in accounts})
        )
        if not account_values or any(
            not value or value != value.lower() for value in account_values
        ):
            raise ValueError("current decision fence accounts must be lowercase")
        placeholders = ",".join("?" for _value in account_values)
        with self._optional_conn(conn) as active_conn:
            source = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
            heads = active_conn.execute(
                f"SELECT * FROM position_projection_heads WHERE account IN ({placeholders})",
                account_values,
            ).fetchall()
            generations = active_conn.execute(
                f"""
                SELECT * FROM current_decision_input_generations
                WHERE account IN ({placeholders})
                """,
                account_values,
            ).fetchall()
            projections = active_conn.execute(
                f"""
                SELECT account, projection_schema,
                  projector_implementation_fingerprint,
                  built_position_source_generation,
                  built_position_lots_generation, position_lots_fingerprint,
                  built_decision_input_generation, built_case_generation,
                  built_evidence_generation, built_allocation_generation,
                  built_source_consumption_generation, built_timing_generation,
                  built_combo_identity_generation, built_assigned_stock_generation
                FROM current_decision_projections
                WHERE account IN ({placeholders})
                """,
                account_values,
            ).fetchall()
        heads_by_account = {str(row["account"]): dict(row) for row in heads}
        generations_by_account = {
            str(row["account"]): dict(row) for row in generations
        }
        projections_by_account = {
            str(row["account"]): dict(row) for row in projections
        }
        return {
            "source": dict(source) if source is not None else None,
            "accounts": {
                account: {
                    "head": heads_by_account.get(account),
                    "generation": generations_by_account.get(account),
                    "projection": projections_by_account.get(account),
                }
                for account in account_values
            },
        }

    def list_current_decision_lifecycle_fact_rows(
        self,
        *,
        account: str,
        target_lot_ids: Sequence[str] = (),
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Read operational or currently referenced compact lifecycle facts."""

        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("current decision account must be lowercase")
        lot_ids = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in target_lot_ids
                if str(value or "").strip()
            )
        )
        select_sql = """
            SELECT lifecycle_case.case_id, lifecycle_case.account,
                   lifecycle_case.status, lifecycle_case.decision_fact_json,
                   lifecycle_case.decision_fact_sha256,
                   evidence_revision.revision AS evidence_revision,
                   evidence_revision.evidence_count AS evidence_count,
                   admission.semantic_schema AS admitted_semantic_schema,
                   admission.semantic_fingerprint AS admitted_semantic_fingerprint,
                   admission.evidence_id AS admitted_evidence_id
            FROM trade_lifecycle_cases AS lifecycle_case
            LEFT JOIN trade_lifecycle_evidence_revisions AS evidence_revision
              ON evidence_revision.case_id = lifecycle_case.case_id
            LEFT JOIN trade_lifecycle_settlement_admission_heads AS admission
              ON admission.case_id = lifecycle_case.case_id
        """
        rows_by_case: dict[str, sqlite3.Row] = {}
        with self._optional_conn(conn) as active_conn:
            for row in active_conn.execute(
                select_sql
                + """
                WHERE lifecycle_case.account = ?
                  AND lifecycle_case.status IN (
                    'pending', 'waiting_settlement_evidence', 'needs_review',
                    'partially_resolved', 'conflict'
                  )
                ORDER BY lifecycle_case.case_id ASC
                """,
                (account_value,),
            ).fetchall():
                rows_by_case[str(row["case_id"])] = row
            if lot_ids:
                placeholders = ",".join("?" for _value in lot_ids)
                for row in active_conn.execute(
                    select_sql
                    + f"""
                    JOIN trade_lifecycle_case_targets AS target
                      ON target.case_id = lifecycle_case.case_id
                    WHERE target.account = ?
                      AND target.target_lot_id IN ({placeholders})
                    ORDER BY lifecycle_case.case_id ASC
                    """,
                    (account_value, *lot_ids),
                ).fetchall():
                    rows_by_case[str(row["case_id"])] = row
        return [dict(rows_by_case[key]) for key in sorted(rows_by_case)]

    def get_current_decision_lifecycle_fact_state(
        self,
        case_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_id_value = str(case_id or "").strip()
        if not case_id_value:
            raise ValueError("current decision lifecycle case id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT lifecycle_case.case_id, lifecycle_case.account,
                       lifecycle_case.status,
                       lifecycle_case.decision_fact_json,
                       lifecycle_case.decision_fact_sha256,
                       COALESCE(evidence_revision.revision, 0)
                         AS evidence_revision,
                       COALESCE(evidence_revision.evidence_count, 0)
                         AS evidence_count,
                       admission.semantic_schema AS admitted_semantic_schema,
                       admission.semantic_fingerprint
                         AS admitted_semantic_fingerprint,
                       admission.evidence_id AS admitted_evidence_id
                FROM trade_lifecycle_cases AS lifecycle_case
                LEFT JOIN trade_lifecycle_evidence_revisions
                  AS evidence_revision
                  ON evidence_revision.case_id = lifecycle_case.case_id
                LEFT JOIN trade_lifecycle_settlement_admission_heads
                  AS admission
                  ON admission.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.case_id = ?
                """,
                (case_id_value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_trade_lifecycle_case_decision_fact(
        self,
        *,
        case_id: str,
        account: str,
        status: str,
        decision_fact_json: str,
        decision_fact_sha256: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        account_value = str(account or "").strip()
        status_value = str(status or "").strip().lower()
        if (
            not case_id_value
            or not account_value
            or account_value != account_value.lower()
            or not status_value
        ):
            raise ValueError("current decision lifecycle fact binding is invalid")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                """
                SELECT account, status, decision_fact_json,
                       decision_fact_sha256
                FROM trade_lifecycle_cases
                WHERE case_id = ?
                """,
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle case not found: {case_id_value}")
            if row["account"] != account_value or row["status"] != status_value:
                raise ValueError("current decision lifecycle fact binding changed")
            if (
                row["decision_fact_json"] == decision_fact_json
                and row["decision_fact_sha256"] == decision_fact_sha256
            ):
                return False
            cursor = active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET decision_fact_json = ?, decision_fact_sha256 = ?
                WHERE case_id = ? AND account = ? AND status = ?
                """,
                (
                    decision_fact_json,
                    decision_fact_sha256,
                    case_id_value,
                    account_value,
                    status_value,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("current decision lifecycle fact write lost ownership")
        return True

    def upsert_current_decision_projection(
        self,
        row: Mapping[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        values = dict(row or {})
        columns = (
            "account",
            "projection_schema",
            "projector_implementation_fingerprint",
            "built_position_source_generation",
            "built_position_lots_generation",
            "position_lots_fingerprint",
            "built_decision_input_generation",
            "built_case_generation",
            "built_evidence_generation",
            "built_allocation_generation",
            "built_source_consumption_generation",
            "built_timing_generation",
            "built_combo_identity_generation",
            "built_assigned_stock_generation",
            "decision_state_fingerprint",
            "payload_sha256",
            "payload_json",
            "updated_at_ms",
        )
        if set(values) != set(columns):
            raise ValueError("current decision projection row shape is invalid")
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                """
                INSERT INTO current_decision_projections (
                  account, projection_schema,
                  projector_implementation_fingerprint,
                  built_position_source_generation,
                  built_position_lots_generation, position_lots_fingerprint,
                  built_decision_input_generation, built_case_generation,
                  built_evidence_generation, built_allocation_generation,
                  built_source_consumption_generation, built_timing_generation,
                  built_combo_identity_generation,
                  built_assigned_stock_generation, decision_state_fingerprint,
                  payload_sha256, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                  projection_schema = excluded.projection_schema,
                  projector_implementation_fingerprint =
                    excluded.projector_implementation_fingerprint,
                  built_position_source_generation =
                    excluded.built_position_source_generation,
                  built_position_lots_generation =
                    excluded.built_position_lots_generation,
                  position_lots_fingerprint = excluded.position_lots_fingerprint,
                  built_decision_input_generation =
                    excluded.built_decision_input_generation,
                  built_case_generation = excluded.built_case_generation,
                  built_evidence_generation = excluded.built_evidence_generation,
                  built_allocation_generation = excluded.built_allocation_generation,
                  built_source_consumption_generation =
                    excluded.built_source_consumption_generation,
                  built_timing_generation = excluded.built_timing_generation,
                  built_combo_identity_generation =
                    excluded.built_combo_identity_generation,
                  built_assigned_stock_generation =
                    excluded.built_assigned_stock_generation,
                  decision_state_fingerprint = excluded.decision_state_fingerprint,
                  payload_sha256 = excluded.payload_sha256,
                  payload_json = excluded.payload_json,
                  updated_at_ms = excluded.updated_at_ms
                WHERE current_decision_projections.payload_sha256
                      IS NOT excluded.payload_sha256
                   OR current_decision_projections.payload_json
                      IS NOT excluded.payload_json
                """,
                tuple(values[column] for column in columns),
            )
        return int(cursor.rowcount or 0) == 1

    def list_trade_lifecycle_due_candidates(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Return compact case/timing/evidence invalidation facts only."""

        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("due lifecycle candidate account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT
                  lifecycle_case.raw_json AS case_raw_json,
                  lifecycle_case.updated_at_ms AS case_updated_at_ms,
                  timing.raw_json AS timing_raw_json,
                  COALESCE(evidence_revision.revision, 0)
                    AS evidence_revision
                FROM trade_lifecycle_cases AS lifecycle_case
                LEFT JOIN trade_lifecycle_timing_policies AS timing
                  ON timing.case_id = lifecycle_case.case_id
                LEFT JOIN trade_lifecycle_evidence_revisions
                  AS evidence_revision
                  ON evidence_revision.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.account = ?
                  AND lifecycle_case.status NOT IN (
                    'ledger_written', 'conflict', 'superseded'
                  )
                ORDER BY lifecycle_case.updated_at_ms DESC,
                         lifecycle_case.case_id DESC
                """,
                (account_value,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            lifecycle_case = _json_object(row["case_raw_json"])
            timing_policy = (
                _json_object(row["timing_raw_json"])
                if row["timing_raw_json"] is not None
                else None
            )
            output.append(
                {
                    "lifecycle_case": lifecycle_case,
                    "case_updated_at_ms": int(
                        row["case_updated_at_ms"] or 0
                    ),
                    "timing_policy": timing_policy,
                    "evidence_revision": int(
                        row["evidence_revision"] or 0
                    ),
                }
            )
        return output

    def get_trade_lifecycle_delivery_status_revision(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT revision
                FROM trade_lifecycle_status_revisions
                WHERE scope = 'delivery'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("lifecycle delivery status revision is missing")
        return int(row["revision"] or 0)

    def upsert_trade_lifecycle_evidence(
        self,
        evidence: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(evidence or {})
        evidence_id = str(payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("trade lifecycle evidence requires evidence_id")
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") == raw_json:
                    return False
                raise ValueError(
                    "trade lifecycle evidence is immutable for "
                    f"evidence_id={evidence_id}"
                )
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                  evidence_id, case_id, source_type, source_event_id, evidence_type,
                  account, symbol, raw_json, created_at_ms
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    evidence_id,
                    (str(payload.get("case_id") or "").strip() or None),
                    str(payload.get("source_type") or "").strip(),
                    (str(payload.get("source_event_id") or "").strip() or None),
                    str(payload.get("evidence_type") or "").strip(),
                    (str(payload.get("account") or "").strip().lower() or None),
                    (str(payload.get("symbol") or "").strip().upper() or None),
                    raw_json,
                    ts,
                ),
            )
        return True

    def list_trade_lifecycle_evidence(
        self,
        *,
        case_id: str | None = None,
        account: str | None = None,
        symbol: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        if account:
            clauses.append("account = ?")
            params.append(str(account).strip().lower())
        if symbol:
            clauses.append("symbol = ?")
            params.append(str(symbol).strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_evidence
                {where}
                ORDER BY created_at_ms ASC, evidence_id ASC
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["raw_json"]) or "{}")
            if isinstance(payload, dict):
                out.append(dict(payload))
        return out

    def insert_trade_lifecycle_case_once(
        self,
        case: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(case or {})
        case_id = str(payload.get("case_id") or "").strip()
        case_key = str(payload.get("case_key") or "").strip()
        account = str(payload.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("lifecycle_case.v2 requires lowercase account")
        payload["account"] = account
        _target_lot_ids, target_contracts, target_rows = _normalized_lifecycle_case_targets(
            payload,
            case_id=case_id,
            account=account,
        )
        if not case_id or not case_key or not target_contracts or len(target_rows) != len(target_contracts):
            raise ValueError("lifecycle_case.v2 requires case id, key, account and target manifest")
        immutable = _lifecycle_case_immutable_payload(payload)
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ? OR case_key = ?",
                (case_id, case_key),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(str(existing["raw_json"]) or "{}")
                if (
                    not isinstance(existing_payload, dict)
                    or _lifecycle_case_immutable_payload(existing_payload) != immutable
                ):
                    raise ValueError(f"lifecycle case immutable conflict for case_id={case_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                  case_id, case_key, account, broker, symbol, option_type, position_side,
                  strike, expiration_ymd, contract_key, status, decision_type,
                  target_lot_ids_json, target_contracts_by_lot_json, observation_start_ms,
                  pending_until_ms, created_at_ms, updated_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    case_key,
                    account,
                    str(payload.get("broker") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper(),
                    str(payload.get("option_type") or "").strip().lower() or None,
                    str(payload.get("position_side") or "").strip().lower() or None,
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    str(payload.get("expiration_ymd") or "").strip() or None,
                    _json_text(payload.get("contract_key")),
                    str(payload.get("status") or "waiting_settlement_evidence").strip().lower(),
                    str(payload.get("decision_type") or "").strip().lower() or None,
                    json.dumps(sorted(target_contracts), ensure_ascii=False),
                    json.dumps(target_contracts, ensure_ascii=False, sort_keys=True),
                    int(payload["observation_start_ms"]) if payload.get("observation_start_ms") is not None else None,
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    ts,
                    ts,
                    raw_json,
                ),
            )
            active_conn.executemany(
                """
                INSERT INTO trade_lifecycle_case_targets (
                  case_id, account, target_lot_id, target_contracts
                ) VALUES (?, ?, ?, ?)
                """,
                target_rows,
            )
        return True

    def update_trade_lifecycle_case_derived_status(
        self,
        *,
        case_id: str,
        status: str,
        derived_summary: dict[str, Any],
        expected_state_fingerprint: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        status_value = str(status or "").strip().lower()
        if not case_id_value or not status_value:
            raise ValueError("case id and derived status are required")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json, status FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle case not found: {case_id_value}")
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(f"lifecycle case JSON invalid: {case_id_value}")
            if expected_state_fingerprint is not None:
                current_summary = (
                    dict(payload.get("derived_summary") or {})
                    if isinstance(payload.get("derived_summary"), dict)
                    else {}
                )
                if (
                    str(current_summary.get("state_fingerprint") or "").strip()
                    != str(expected_state_fingerprint or "").strip()
                ):
                    raise ValueError("lifecycle case state fingerprint compare-and-set failed")
            updated = {
                **payload,
                "status": status_value,
                "derived_summary": dict(derived_summary or {}),
            }
            updated_json = json.dumps(updated, ensure_ascii=False, sort_keys=True)
            if str(row["status"] or "") == status_value and str(row["raw_json"] or "") == updated_json:
                return False
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET status = ?, updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (status_value, int(now_ms()), updated_json, case_id_value),
            )
        return True

    def bind_trade_lifecycle_case_futu_account_once(
        self,
        *,
        case_id: str,
        futu_account_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        account_id_value = str(futu_account_id or "").strip()
        if not case_id_value or not account_id_value:
            raise ValueError(
                "lifecycle case and Futu account identity are required"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"lifecycle case not found: {case_id_value}"
                )
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(
                    f"lifecycle case JSON invalid: {case_id_value}"
                )
            existing = str(
                payload.get("futu_account_id") or ""
            ).strip()
            if existing:
                if existing != account_id_value:
                    raise ValueError(
                        "lifecycle case Futu account immutable conflict"
                    )
                return False
            payload["futu_account_id"] = account_id_value
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (
                    int(now_ms()),
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    case_id_value,
                ),
            )
        return True

    def supersede_trade_lifecycle_case_once(
        self,
        *,
        case_id: str,
        superseded_by_case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        successor_id = str(superseded_by_case_id or "").strip()
        if (
            not case_id_value
            or not successor_id
            or case_id_value == successor_id
        ):
            raise ValueError(
                "legacy lifecycle supersession identity is invalid"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"lifecycle case not found: {case_id_value}"
                )
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(
                    f"lifecycle case JSON invalid: {case_id_value}"
                )
            existing_successor = str(
                payload.get("superseded_by_case_id") or ""
            ).strip()
            existing_status = str(
                payload.get("status") or ""
            ).strip().lower()
            if existing_successor:
                if (
                    existing_successor != successor_id
                    or existing_status != "superseded"
                ):
                    raise ValueError(
                        "legacy lifecycle supersession conflict"
                    )
                return False
            payload["status"] = "superseded"
            payload["superseded_by_case_id"] = successor_id
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET status = ?, updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (
                    "superseded",
                    int(now_ms()),
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    case_id_value,
                ),
            )
        return True

    def insert_trade_lifecycle_evidence_once(
        self,
        evidence: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(evidence or {})
        evidence_id = str(payload.get("evidence_id") or "").strip()
        source_type = str(payload.get("source_type") or "").strip()
        evidence_type = str(payload.get("evidence_type") or "").strip()
        if not evidence_id or not source_type or not evidence_type:
            raise ValueError("lifecycle evidence requires evidence_id, source_type and evidence_type")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(f"lifecycle evidence immutable conflict for evidence_id={evidence_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                  evidence_id, case_id, source_type, source_event_id, evidence_type,
                  account, symbol, raw_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    str(payload.get("case_id") or "").strip() or None,
                    source_type,
                    str(payload.get("source_event_id") or "").strip() or None,
                    evidence_type,
                    str(payload.get("account") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper() or None,
                    raw_json,
                    int(now_ms()),
                ),
            )
        return True

    def bind_trade_lifecycle_evidence_case_once(
        self,
        *,
        evidence_id: str,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        evidence_id_value = str(evidence_id or "").strip()
        case_id_value = str(case_id or "").strip()
        if not evidence_id_value or not case_id_value:
            raise ValueError("evidence_id and case_id are required")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT case_id, raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle evidence not found: {evidence_id_value}")
            existing_case = str(row["case_id"] or "").strip()
            if existing_case:
                if existing_case != case_id_value:
                    raise ValueError(f"lifecycle evidence already bound to another case: {evidence_id_value}")
                return False
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(f"lifecycle evidence JSON invalid: {evidence_id_value}")
            payload["case_id"] = case_id_value
            active_conn.execute(
                """
                UPDATE trade_lifecycle_evidence
                SET case_id = ?, raw_json = ?
                WHERE evidence_id = ? AND case_id IS NULL
                """,
                (
                    case_id_value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    evidence_id_value,
                ),
            )
        return True

    def get_trade_lifecycle_evidence(
        self,
        evidence_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (str(evidence_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def get_latest_trade_lifecycle_settlement_evidence(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("settlement evidence case_id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT rowid, raw_json, created_at_ms
                FROM trade_lifecycle_evidence
                WHERE case_id = ?
                  AND source_type = 'broker_settlement_observation'
                  AND json_type(raw_json, '$.observation') = 'object'
                ORDER BY created_at_ms DESC, rowid DESC
                LIMIT 1
                """,
                (case_value,),
            ).fetchone()
        if row is None:
            return None
        payload = _json_object(row["raw_json"])
        return {
            **payload,
            "_created_at_ms": int(row["created_at_ms"] or 0),
            "_rowid": int(row["rowid"] or 0),
        }
