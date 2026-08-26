from __future__ import annotations

from .repository_schema import (
    Any,
    Sequence,
    _json_object,
    sqlite3,
)

class DecisionReadRepositoryMixin:
    def assert_foreign_keys_clean(self, *, conn: sqlite3.Connection | None = None) -> None:
        with self._optional_conn(conn) as active_conn:
            violations = active_conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign key check failed: {len(violations)} violation(s)")

    def _read_account_decision_state_rows(
        self,
        *,
        account: str,
        conn: sqlite3.Connection,
        shared_trade_events: Sequence[dict[str, Any]] | None = None,
        shared_position_lots: Sequence[dict[str, Any]] | None = None,
        shared_assigned_stock_events: Sequence[dict[str, Any]] | None = None,
        shared_wheel_events: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("decision state snapshot requires account")
        events = (
            list(shared_trade_events)
            if shared_trade_events is not None
            else self.list_trade_events(conn=conn)
        )
        lots = (
            list(shared_position_lots)
            if shared_position_lots is not None
            else self.list_position_lots(conn=conn)
        )
        cases = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_cases
                WHERE account = ?
                ORDER BY updated_at_ms DESC, case_id DESC
                """,
                (account_value,),
            ).fetchall()
        ]
        evidence: list[dict[str, Any]] = []
        evidence_received_at_ms_by_id: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT lifecycle_evidence.raw_json,
                   lifecycle_evidence.created_at_ms
            FROM trade_lifecycle_evidence AS lifecycle_evidence
            JOIN trade_lifecycle_cases AS lifecycle_case
              ON lifecycle_case.case_id = lifecycle_evidence.case_id
            WHERE lifecycle_case.account = ?
            ORDER BY lifecycle_evidence.created_at_ms ASC,
                     lifecycle_evidence.evidence_id ASC
            """,
            (account_value,),
        ).fetchall():
            payload = _json_object(row["raw_json"])
            evidence.append(payload)
            evidence_id = str(payload.get("evidence_id") or "").strip()
            if evidence_id:
                evidence_received_at_ms_by_id[evidence_id] = int(
                    row["created_at_ms"]
                )
        allocations = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT allocation.raw_json
                FROM trade_lifecycle_allocations AS allocation
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = allocation.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY allocation.created_at_ms ASC,
                         allocation.allocation_id ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        source_claims = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT source_claim.raw_json
                FROM trade_lifecycle_source_consumptions AS source_claim
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = source_claim.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY source_claim.created_at_ms ASC,
                         source_claim.source_key ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        timing_policies = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT timing.raw_json
                FROM trade_lifecycle_timing_policies AS timing
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = timing.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY timing.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        evidence_revisions = {
            str(row["case_id"]): {
                "revision": int(row["revision"]),
                "evidence_count": (
                    int(row["evidence_count"])
                    if row["evidence_count"] is not None
                    else None
                ),
            }
            for row in conn.execute(
                """
                SELECT revision.case_id, revision.revision,
                       revision.evidence_count
                FROM trade_lifecycle_evidence_revisions AS revision
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = revision.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY revision.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        }
        admission_heads = {
            str(row["case_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT admission.case_id, admission.semantic_schema,
                       admission.semantic_fingerprint, admission.evidence_id,
                       admission.evidence_created_at_ms,
                       admission.updated_at_ms
                FROM trade_lifecycle_settlement_admission_heads AS admission
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = admission.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY admission.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        }
        assigned_stock_events = (
            list(shared_assigned_stock_events)
            if shared_assigned_stock_events is not None
            else [
                _json_object(row["event_json"])
                for row in conn.execute(
                    """
                    SELECT event_json
                    FROM assigned_stock_events
                    ORDER BY trade_time_ms ASC, stock_event_id ASC
                    """
                ).fetchall()
            ]
        )
        wheel_events = (
            list(shared_wheel_events)
            if shared_wheel_events is not None
            else self.list_wheel_events(conn=conn)
        )
        identities = self.list_strategy_group_identities(
            account=account_value,
            conn=conn,
        )
        return {
            "account": account_value,
            "trade_events": events,
            "stored_position_lots": lots,
            "account_position_lots": [
                row
                for row in lots
                if str(
                    (row.get("fields") or {}).get("account") or ""
                ).strip().lower()
                == account_value
            ],
            "account_lifecycle_cases": cases,
            "account_lifecycle_evidence": evidence,
            "account_lifecycle_evidence_received_at_ms_by_id": (
                evidence_received_at_ms_by_id
            ),
            "account_lifecycle_allocations": allocations,
            "account_lifecycle_source_consumptions": source_claims,
            "account_lifecycle_timing_policies": timing_policies,
            "account_lifecycle_evidence_revisions": evidence_revisions,
            "account_lifecycle_settlement_admission_heads": admission_heads,
            "account_assigned_stock_events": [
                row
                for row in assigned_stock_events
                if str(
                    row.get("account")
                    or (row.get("raw_payload") or {}).get("account")
                    or ""
                ).strip().lower()
                == account_value
            ],
            "account_wheel_events": [
                row
                for row in wheel_events
                if str(row.get("account") or "").strip().lower() == account_value
            ],
            "account_combo_identities": identities,
        }

    def read_lifecycle_account_rows(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("lifecycle account reader requires account")
        if conn is not None:
            return self._read_account_decision_state_rows(
                account=account_value,
                conn=conn,
            )
        active_conn = self._connect()
        try:
            active_conn.execute("BEGIN")
            rows = self._read_account_decision_state_rows(
                account=account_value,
                conn=active_conn,
            )
            active_conn.commit()
        except Exception:
            active_conn.rollback()
            raise
        finally:
            active_conn.close()
        return rows

    def read_lifecycle_case_rows(
        self,
        *,
        case_id: str,
    ) -> dict[str, Any]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle case reader requires case_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            lifecycle_case = self.get_trade_lifecycle_case(
                case_value,
                conn=conn,
            )
            if lifecycle_case is None:
                raise ValueError(f"lifecycle case not found: {case_value}")
            rows = self._read_account_decision_state_rows(
                account=str(lifecycle_case.get("account") or ""),
                conn=conn,
            )
            rows["requested_lifecycle_case_id"] = case_value
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return rows

    def read_decision_state_rows(self, *, account: str) -> dict[str, Any]:
        return self.read_lifecycle_account_rows(account=account)

    def read_decision_state_rows_many(
        self,
        *,
        accounts: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Read multiple account decision states from one SQLite snapshot."""

        account_values = sorted(
            {
                str(account or "").strip().lower()
                for account in accounts
                if str(account or "").strip()
            }
        )
        if not account_values:
            raise ValueError("decision state batch requires accounts")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            events = self.list_trade_events(conn=conn)
            lots = self.list_position_lots(conn=conn)
            assigned_stock_events = [
                _json_object(row["event_json"])
                for row in conn.execute(
                    """
                    SELECT event_json
                    FROM assigned_stock_events
                    ORDER BY trade_time_ms ASC, stock_event_id ASC
                    """
                ).fetchall()
            ]
            wheel_events = self.list_wheel_events(conn=conn)
            rows = {
                account: self._read_account_decision_state_rows(
                    account=account,
                    conn=conn,
                    shared_trade_events=events,
                    shared_position_lots=lots,
                    shared_assigned_stock_events=assigned_stock_events,
                    shared_wheel_events=wheel_events,
                )
                for account in account_values
            }
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
