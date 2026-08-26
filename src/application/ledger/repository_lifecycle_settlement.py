from __future__ import annotations

from .repository_schema import (
    Any,
    _json_object,
    _json_text,
    json,
    now_ms,
    sqlite3,
)

class LifecycleSettlementRepositoryMixin:
    def get_trade_lifecycle_settlement_admission_head(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT case_id, semantic_schema, semantic_fingerprint,
                       evidence_id, evidence_created_at_ms, updated_at_ms
                FROM trade_lifecycle_settlement_admission_heads
                WHERE case_id = ?
                """,
                (str(case_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_trade_lifecycle_settlement_admission_head(
        self,
        *,
        case_id: str,
        semantic_schema: str,
        semantic_fingerprint: str,
        evidence_id: str,
        evidence_created_at_ms: int,
        updated_at_ms: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = (
            str(case_id or "").strip(),
            str(semantic_schema or "").strip(),
            str(semantic_fingerprint or "").strip(),
            str(evidence_id or "").strip(),
        )
        if not all(values) or int(evidence_created_at_ms or 0) <= 0:
            raise ValueError("settlement admission head is incomplete")
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_settlement_admission_heads (
                  case_id, semantic_schema, semantic_fingerprint, evidence_id,
                  evidence_created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                  semantic_schema = excluded.semantic_schema,
                  semantic_fingerprint = excluded.semantic_fingerprint,
                  evidence_id = excluded.evidence_id,
                  evidence_created_at_ms = excluded.evidence_created_at_ms,
                  updated_at_ms = excluded.updated_at_ms
                """,
                (
                    *values,
                    int(evidence_created_at_ms),
                    int(updated_at_ms),
                ),
            )

    def insert_trade_lifecycle_source_consumption_once(
        self,
        claim: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(claim or {})
        source_key = str(payload.get("source_key") or "").strip()
        case_id = str(payload.get("case_id") or "").strip()
        evidence_id = str(payload.get("owner_evidence_id") or "").strip()
        role = str(payload.get("source_role") or "").strip().lower()
        payload_hash = str(
            payload.get("source_payload_hash") or ""
        ).strip()
        if (
            str(payload.get("schema_version") or "").strip()
            != "trade_lifecycle_source_consumption.v1"
            or not source_key
            or not case_id
            or not evidence_id
            or role not in {"option_anchor", "stock_settlement"}
            or not payload_hash
        ):
            raise ValueError("lifecycle source consumption claim is incomplete")
        raw_json = _json_text(payload)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(
                        "lifecycle_source_event_already_consumed"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_source_consumptions (
                  source_key, case_id, owner_evidence_id, source_role,
                  source_payload_hash, created_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    case_id,
                    evidence_id,
                    role,
                    payload_hash,
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def get_trade_lifecycle_source_consumption(
        self,
        source_key: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                WHERE source_key = ?
                """,
                (str(source_key or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_trade_lifecycle_source_consumptions(
        self,
        *,
        case_id: str | None = None,
        owner_evidence_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        if owner_evidence_id:
            clauses.append("owner_evidence_id = ?")
            params.append(str(owner_evidence_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                {where}
                ORDER BY created_at_ms ASC, source_key ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def insert_trade_lifecycle_allocation(
        self,
        allocation: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(allocation or {})
        required = (
            "allocation_id",
            "case_id",
            "evidence_id",
            "target_lot_id",
            "terminal_type",
            "canonical_terminal_event_id",
        )
        values = {field: str(payload.get(field) or "").strip() for field in required}
        if any(not value for value in values.values()):
            raise ValueError("lifecycle allocation is missing required identity")
        contracts = int(payload.get("contracts_allocated") or 0)
        if contracts <= 0 or contracts != float(payload.get("contracts_allocated")):
            raise ValueError("lifecycle allocation contracts must be a positive integer")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_allocations WHERE allocation_id = ?",
                (values["allocation_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(f"lifecycle allocation conflict for allocation_id={values['allocation_id']}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_allocations (
                  allocation_id, case_id, evidence_id, target_lot_id, terminal_type,
                  contracts_allocated, canonical_terminal_event_id, created_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["allocation_id"],
                    values["case_id"],
                    values["evidence_id"],
                    values["target_lot_id"],
                    values["terminal_type"].lower(),
                    contracts,
                    values["canonical_terminal_event_id"],
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def list_trade_lifecycle_allocations(
        self,
        *,
        case_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE case_id = ?" if case_id else ""
        params = (str(case_id).strip(),) if case_id else ()
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_allocations
                {where}
                ORDER BY created_at_ms ASC, allocation_id ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def insert_trade_lifecycle_timing_policy_once(
        self,
        policy: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(policy or {})
        payload.pop("pairing_until_ms", None)
        case_id = str(payload.get("case_id") or "").strip()
        if (
            not case_id
            or str(payload.get("policy_schema") or "").strip()
            != "lifecycle_timing_policy.v1"
        ):
            raise ValueError(
                "lifecycle timing policy requires case_id and v1 schema"
            )
        raw_json = _json_text(payload)
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                WHERE case_id = ?
                """,
                (case_id,),
            ).fetchone()
            if existing is not None:
                existing_payload = _json_object(existing["raw_json"])
                existing_payload.pop("pairing_until_ms", None)
                if _json_text(existing_payload) != raw_json:
                    raise ValueError(
                        "lifecycle timing policy immutable conflict "
                        f"for case_id={case_id}"
                    )
                return False
            columns = {
                str(row["name"])
                for row in active_conn.execute(
                    "PRAGMA table_info(trade_lifecycle_timing_policies)"
                ).fetchall()
            }
            names = [
                "case_id",
                "policy_schema",
                "market",
                "timezone",
                "settlement_style",
                "underlying_security_type",
                "last_trade_cutoff_ms",
                "last_trade_cutoff_source",
            ]
            values: list[Any] = [
                case_id,
                str(payload["policy_schema"]),
                str(payload.get("market") or "").strip().upper(),
                str(payload.get("timezone") or "").strip(),
                str(payload.get("settlement_style") or "").strip().lower(),
                str(
                    payload.get("underlying_security_type") or ""
                ).strip().lower(),
                int(payload.get("last_trade_cutoff_ms") or 0),
                str(payload.get("last_trade_cutoff_source") or "").strip(),
            ]
            if "pairing_until_ms" in columns:
                # Compatibility with databases initialized by the pre-v2 draft.
                names.append("pairing_until_ms")
                values.append(0)
            names.extend(
                [
                    "settlement_deadline_ms",
                    "trading_days_json",
                    "calendar_source",
                    "calendar_observed_at_ms",
                    "calendar_hash",
                    "created_at_ms",
                    "raw_json",
                ]
            )
            values.extend(
                [
                    int(payload.get("settlement_deadline_ms") or 0),
                    _json_text(payload.get("trading_days") or []),
                    str(payload.get("calendar_source") or "").strip(),
                    int(payload.get("calendar_observed_at_ms") or 0),
                    str(payload.get("calendar_hash") or "").strip(),
                    ts,
                    raw_json,
                ]
            )
            placeholders = ", ".join("?" for _ in names)
            active_conn.execute(
                f"""
                INSERT INTO trade_lifecycle_timing_policies (
                  {", ".join(names)}
                ) VALUES ({placeholders})
                """,
                values,
            )
        return True

    def get_trade_lifecycle_timing_policy(
        self,
        case_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                WHERE case_id = ?
                """,
                (str(case_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_trade_lifecycle_timing_policies(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                ORDER BY case_id ASC
                """
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]
