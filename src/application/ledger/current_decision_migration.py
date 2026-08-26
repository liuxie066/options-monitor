from __future__ import annotations

from .current_decision_common import (
    Any,
    CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
    Callable,
    CurrentDecisionProjectionError,
    Mapping,
    Path,
    Sequence,
    _canonical_json_bytes,
    _ensure_current_decision_projection_schema,
    _integer,
    _normalized_lifecycle_case_targets,
    _position_migration,
    _projection_schema_cookie,
    canonical_sha256,
    hashlib,
    json,
    time,
)

from .current_decision_oracle import (
    _DECISION_MIGRATION_AUTHORITY_QUERIES,
    _DECISION_MIGRATION_REQUIRED_INDEXES,
    _DECISION_MIGRATION_REQUIRED_TABLES,
    _DECISION_MIGRATION_REQUIRED_TRIGGERS,
    _current_decision_projection_oracle,
)

from .current_decision_payload import (
    _decode_projection_row_payload,
    current_decision_projection_row,
    encode_lifecycle_case_decision_fact,
    write_lifecycle_case_decision_fact,
)

from .current_decision_runtime_support import (
    _projection_metadata_clean,
)

def _migration_rows_fingerprint(
    conn: Any,
    queries: Sequence[tuple[str, str]],
) -> tuple[str, dict[str, int], int]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    payload_bytes = 0
    for name, query in queries:
        if not _position_migration._table_exists(conn, name):
            counts[name] = 0
            digest.update(_canonical_json_bytes({"table": name, "missing": True}))
            continue
        count = 0
        digest.update(_canonical_json_bytes({"table": name}))
        for row in conn.execute(query):
            payload = _canonical_json_bytes(dict(row))
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
            payload_bytes += len(payload)
        counts[name] = count
    return digest.hexdigest(), counts, payload_bytes

def _migration_source_inventory(conn: Any) -> dict[str, Any]:
    accounts: set[str] = set()
    reasons: list[str] = []
    assigned_accounts: dict[str, str] = {}
    assigned_null_count = 0
    cases: dict[str, dict[str, Any]] = {}
    targets: dict[str, tuple[tuple[str, str, str, int | None], ...]] = {}

    for row in conn.execute(
        "SELECT account FROM position_projection_heads ORDER BY account"
    ):
        account = str(row["account"] or "").strip()
        if not account or account != account.lower():
            reasons.append("position_head_account_invalid")
        else:
            accounts.add(account)

    for row in conn.execute(
        "SELECT case_id,account,raw_json FROM trade_lifecycle_cases ORDER BY case_id"
    ):
        case_id = str(row["case_id"] or "").strip()
        account = str(row["account"] or "").strip()
        try:
            payload = json.loads(str(row["raw_json"] or ""))
            if not isinstance(payload, dict):
                raise ValueError
            if (
                not case_id
                or not account
                or account != account.lower()
                or str(payload.get("case_id") or "").strip() != case_id
                or str(payload.get("account") or "").strip() != account
            ):
                raise ValueError
            normalized = _normalized_lifecycle_case_targets(
                payload,
                case_id=case_id,
                account=account,
            )[2]
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.append(f"lifecycle_case_invalid:{case_id or 'unknown'}")
            continue
        accounts.add(account)
        cases[case_id] = payload
        targets[case_id] = normalized

    for row in conn.execute(
        "SELECT stock_event_id,account,event_json FROM assigned_stock_events "
        "ORDER BY stock_event_id"
    ):
        stock_event_id = str(row["stock_event_id"] or "").strip()
        try:
            payload = json.loads(str(row["event_json"] or ""))
            if not isinstance(payload, dict):
                raise ValueError
            account = str(payload.get("account") or "").strip()
            if not account or account != account.lower():
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.append(
                f"assigned_stock_event_invalid:{stock_event_id or 'unknown'}"
            )
            continue
        stored = row["account"]
        if stored is None:
            assigned_null_count += 1
        elif str(stored) != account:
            reasons.append(f"assigned_stock_account_conflict:{stock_event_id}")
        accounts.add(account)
        assigned_accounts[stock_event_id] = account

    for table in (
        "trade_events",
        "position_lots",
        "strategy_group_identities",
        "current_decision_input_generations",
        "current_decision_projections",
    ):
        for row in conn.execute(f"SELECT account FROM {table} ORDER BY account"):
            account = str(row["account"] or "").strip()
            if not account or account != account.lower():
                reasons.append(f"{table}_account_invalid")
            else:
                accounts.add(account)

    evidence_counts = {
        str(row["case_id"]): int(row["evidence_count"] or 0)
        for row in conn.execute(
            "SELECT lifecycle_case.case_id,count(evidence.evidence_id) AS evidence_count "
            "FROM trade_lifecycle_cases AS lifecycle_case "
            "LEFT JOIN trade_lifecycle_evidence AS evidence "
            "ON evidence.case_id=lifecycle_case.case_id "
            "GROUP BY lifecycle_case.case_id ORDER BY lifecycle_case.case_id"
        )
    }
    return {
        "accounts": tuple(sorted(accounts)),
        "cases": cases,
        "targets": targets,
        "assigned_accounts": assigned_accounts,
        "assigned_null_count": assigned_null_count,
        "evidence_counts": evidence_counts,
        "reasons": reasons,
    }

def _migration_state_summary(
    conn: Any,
    *,
    accounts: Sequence[str],
    payloads: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Any]],
    targets: Mapping[str, Sequence[tuple[str, str, str, int | None]]],
    assigned_accounts: Mapping[str, str],
    evidence_counts: Mapping[str, int],
    implementation: str,
) -> dict[str, Any]:
    indexes = _position_migration._object_names(conn, "index")
    triggers = _position_migration._object_names(conn, "trigger")
    source = conn.execute(
        "SELECT * FROM position_projection_source_state WHERE singleton_id=1"
    ).fetchone()
    missing_indexes = sorted(set(_DECISION_MIGRATION_REQUIRED_INDEXES) - indexes)
    missing_triggers = sorted(set(_DECISION_MIGRATION_REQUIRED_TRIGGERS) - triggers)
    assigned_mismatch = sum(
        1
        for row in conn.execute(
            "SELECT stock_event_id,account FROM assigned_stock_events "
            "ORDER BY stock_event_id"
        )
        if row["account"] != assigned_accounts.get(str(row["stock_event_id"]))
    )
    target_mismatch = 0
    for case_id, expected in targets.items():
        actual = tuple(
            (str(row["case_id"]), str(row["account"]), str(row["target_lot_id"]), row["target_contracts"])
            for row in conn.execute(
                "SELECT case_id,account,target_lot_id,target_contracts "
                "FROM trade_lifecycle_case_targets WHERE case_id=? "
                "ORDER BY target_lot_id",
                (case_id,),
            )
        )
        target_mismatch += actual != tuple(expected)

    fact_mismatch = 0
    for case_id, fact in facts.items():
        encoded, fingerprint = encode_lifecycle_case_decision_fact(fact)
        row = conn.execute(
            "SELECT decision_fact_json,decision_fact_sha256 "
            "FROM trade_lifecycle_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        fact_mismatch += row is None or (
            row["decision_fact_json"], row["decision_fact_sha256"]
        ) != (encoded, fingerprint)

    evidence_count_mismatch = 0
    for case_id, expected in evidence_counts.items():
        row = conn.execute(
            "SELECT evidence_count FROM trade_lifecycle_evidence_revisions "
            "WHERE case_id=?",
            (case_id,),
        ).fetchone()
        evidence_count_mismatch += row is None or row["evidence_count"] != expected

    projection_missing = projection_dirty = projection_mismatch = 0
    for account in accounts:
        storage = conn.execute(
            "SELECT * FROM current_decision_input_generations WHERE account=?",
            (account,),
        ).fetchone()
        projection = conn.execute(
            "SELECT * FROM current_decision_projections WHERE account=?",
            (account,),
        ).fetchone()
        if projection is None:
            projection_missing += 1
            continue
        inputs = conn.execute(
            "SELECT * FROM position_projection_heads WHERE account=?",
            (account,),
        ).fetchone()
        if not _projection_metadata_clean(
            account=account,
            source=dict(source) if source is not None else None,
            head=dict(inputs) if inputs is not None else None,
            generation=dict(storage) if storage is not None else None,
            projection=dict(projection),
            implementation_fingerprint=implementation,
        ):
            projection_dirty += 1
            continue
        if projection["decision_state_fingerprint"] != payloads[account][
            "decision_state_fingerprint"
        ]:
            projection_mismatch += 1

    return {
        "assigned_account_mismatch_count": assigned_mismatch,
        "case_target_mismatch_count": target_mismatch,
        "case_fact_mismatch_count": fact_mismatch,
        "evidence_count_mismatch_count": evidence_count_mismatch,
        "generation_missing_count": sum(
            conn.execute(
                "SELECT 1 FROM current_decision_input_generations WHERE account=?",
                (account,),
            ).fetchone()
            is None
            for account in accounts
        ),
        "projection_missing_count": projection_missing,
        "projection_dirty_count": projection_dirty,
        "projection_mismatch_count": projection_mismatch,
        "missing_indexes": missing_indexes,
        "missing_triggers": missing_triggers,
        "position_schema_cookie_mismatch": (
            source is None
            or source["sqlite_schema_cookie"] != _projection_schema_cookie(conn)
        ),
    }

def _migration_state_clean(summary: Mapping[str, Any]) -> bool:
    return not any(
        (
            int(summary[key])
            for key in (
                "assigned_account_mismatch_count",
                "case_target_mismatch_count",
                "case_fact_mismatch_count",
                "evidence_count_mismatch_count",
                "generation_missing_count",
                "projection_missing_count",
                "projection_dirty_count",
                "projection_mismatch_count",
            )
        )
    ) and not any(
        (
            summary["missing_indexes"],
            summary["missing_triggers"],
            summary["position_schema_cookie_mismatch"],
        )
    )

def _current_decision_migration_inventory_from_conn(
    path: Path,
    conn: Any,
    *,
    now_ms: int,
    implementation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    tables = _position_migration._object_names(conn, "table")
    missing_tables = sorted(set(_DECISION_MIGRATION_REQUIRED_TABLES) - tables)
    authority_queries = [
        item
        for item in _DECISION_MIGRATION_AUTHORITY_QUERIES
        if item[0] in tables
    ]
    authority_fingerprint, source_counts, source_bytes = (
        _migration_rows_fingerprint(conn, authority_queries)
    )
    public: dict[str, Any] = {
        "store_identity": _position_migration._store_identity(path),
        "loaded_projector_implementation_fingerprint": implementation,
        "now_ms": now_ms,
        "authority_fingerprint": authority_fingerprint,
        "source_counts": source_counts,
        "source_payload_bytes": source_bytes,
        "accounts": [],
        "repair": {},
    }
    if missing_tables:
        reasons = ["required_tables_missing"]
        public.update(
            readiness="not_ready",
            readiness_reasons=reasons,
            missing_tables=missing_tables,
            inventory_fingerprint=canonical_sha256(
                {
                    "store_identity": public["store_identity"],
                    "implementation": implementation,
                    "now_ms": now_ms,
                    "authority_fingerprint": authority_fingerprint,
                }
            ),
        )
        return public, {}

    sources = _migration_source_inventory(conn)
    reasons = list(sources["reasons"])
    repo = _position_migration._repository(path)
    payloads: dict[str, dict[str, Any]] = {}
    facts: dict[str, dict[str, Any]] = {}
    account_rows: list[dict[str, Any]] = []
    for account in sources["accounts"]:
        try:
            payload, account_facts = _current_decision_projection_oracle(
                repo,
                account=account,
                now_ms=now_ms,
                assigned_stock_report=None,
                conn=conn,
                allow_schema_cookie_mismatch=True,
            )
        except Exception as exc:
            reasons.append(f"oracle_unavailable:{account}:{type(exc).__name__}")
            continue
        payloads[account] = payload
        for fact in account_facts:
            case_id = str(fact["case_id"])
            if case_id in facts:
                raise CurrentDecisionProjectionError(
                    "migration oracle returned duplicate lifecycle case"
                )
            facts[case_id] = fact
        head = conn.execute(
            "SELECT built_source_generation,built_lots_generation,"
            "projection_fingerprint,lot_count,status FROM position_projection_heads "
            "WHERE account=?",
            (account,),
        ).fetchone()
        account_rows.append(
            {
                "account": account,
                "position_head": dict(head) if head is not None else None,
                "oracle_decision_state_fingerprint": payload[
                    "decision_state_fingerprint"
                ],
                "oracle_case_fact_count": len(account_facts),
            }
        )
    if len(payloads) != len(sources["accounts"]):
        reasons.append("oracle_inventory_incomplete")
    if set(facts) != set(sources["cases"]):
        reasons.append("lifecycle_case_fact_inventory_incomplete")
    state = (
        _migration_state_summary(
            conn,
            accounts=sources["accounts"],
            payloads=payloads,
            facts=facts,
            targets=sources["targets"],
            assigned_accounts=sources["assigned_accounts"],
            evidence_counts=sources["evidence_counts"],
            implementation=implementation,
        )
        if not reasons
        else {}
    )
    public.update(
        accounts=account_rows,
        repair=state,
        missing_tables=[],
        assigned_stock_legacy_account_count=sources["assigned_null_count"],
        mixed_version_guard_status=(
            "active"
            if "trg_current_decision_assigned_stock_account_insert_guard"
            in _position_migration._object_names(conn, "trigger")
            else "missing"
        ),
        readiness="ready" if not reasons else "not_ready",
        readiness_reasons=sorted(set(reasons)),
        inventory_fingerprint=canonical_sha256(
            {
                "store_identity": public["store_identity"],
                "implementation": implementation,
                "now_ms": now_ms,
                "authority_fingerprint": authority_fingerprint,
            }
        ),
    )
    return public, {
        "sources": sources,
        "payloads": payloads,
        "facts": facts,
        "state": state,
    }

def build_current_decision_projection_migration_inventory(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    path = _position_migration._store_path(sqlite_path)
    instant = _integer(
        int(time.time() * 1000) if now_ms is None else now_ms,
        field="now_ms",
        minimum=1,
    )
    implementation, timing = _position_migration._loaded_implementation()
    before = _position_migration._file_sizes(path)
    with _position_migration._read_only_connection(path) as conn:
        inventory, _details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=instant,
            implementation=implementation,
        )
    _position_migration._assert_read_only_persistent_sizes(
        before,
        _position_migration._file_sizes(path),
        operation="current-decision inventory",
    )
    return _position_migration._manifest(
        {
            "schema_version": CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "inventory",
            "read_only": True,
            "loaded_projector_fingerprint_timing": timing,
            **inventory,
        }
    )

def verify_current_decision_projection_migration(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    path = _position_migration._store_path(sqlite_path)
    instant = _integer(
        int(time.time() * 1000) if now_ms is None else now_ms,
        field="now_ms",
        minimum=1,
    )
    implementation, _timing = _position_migration._loaded_implementation()
    before = _position_migration._file_sizes(path)
    with _position_migration._read_only_connection(path) as conn:
        inventory, details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=instant,
            implementation=implementation,
        )
        comparisons: list[dict[str, Any]] = []
        samples: list[dict[str, Any]] = []
        if inventory["readiness"] == "ready":
            for account, expected in details["payloads"].items():
                row = conn.execute(
                    "SELECT * FROM current_decision_projections WHERE account=?",
                    (account,),
                ).fetchone()
                if row is None:
                    status = "proposed"
                else:
                    try:
                        actual = _decode_projection_row_payload(dict(row))
                        status = (
                            "matched"
                            if actual["decision_state_fingerprint"]
                            == expected["decision_state_fingerprint"]
                            else "mismatch"
                        )
                    except CurrentDecisionProjectionError:
                        status = "mismatch"
                comparisons.append({"account": account, "status": status})
                if status == "mismatch" and len(samples) < 10:
                    samples.append(
                        {"account": account, "reason": "stored_projection_mismatch"}
                    )
    _position_migration._assert_read_only_persistent_sizes(
        before,
        _position_migration._file_sizes(path),
        operation="current-decision verify",
    )
    status = (
        "not_ready"
        if inventory["readiness"] != "ready"
        else "mismatch"
        if samples
        else "valid"
    )
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_verify.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "verify",
            "read_only": True,
            "status": status,
            "inventory_fingerprint": inventory["inventory_fingerprint"],
            "comparison_count": len(comparisons),
            "comparisons": comparisons,
            "mismatch_count": len(samples),
            "mismatch_samples": samples,
            "readiness_reasons": inventory["readiness_reasons"],
        }
    )

def apply_current_decision_projection_migration(
    sqlite_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    supplied = _position_migration._validate_manifest(
        manifest,
        schema=CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
    )
    path = _position_migration._store_path(sqlite_path)
    implementation, _timing = _position_migration._loaded_implementation()
    now_ms = _integer(supplied.get("now_ms"), field="manifest now_ms", minimum=1)
    conn = _position_migration._write_connection(path)
    repo = _position_migration._repository(path)
    before = _position_migration._file_sizes(path)
    write_applied = False
    counts: dict[str, int] = {}
    final_state: dict[str, Any] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        current, details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=now_ms,
            implementation=implementation,
        )
        if current["store_identity"] != supplied.get("store_identity"):
            raise ValueError("migration manifest store identity mismatch")
        if implementation != supplied.get(
            "loaded_projector_implementation_fingerprint"
        ):
            raise ValueError("migration manifest projector implementation mismatch")
        if current["authority_fingerprint"] != supplied.get(
            "authority_fingerprint"
        ):
            raise ValueError("migration manifest is stale")
        if current["readiness"] != "ready":
            raise ValueError("migration inventory is not ready")
        _position_migration._fail(failure_hook, "after_manifest_recheck")
        if _migration_state_clean(details["state"]):
            final_state = details["state"]
            conn.rollback()
        else:
            _ensure_current_decision_projection_schema(conn)
            _position_migration._fail(failure_hook, "after_schema")
            accounts = tuple(details["sources"]["accounts"])
            if accounts:
                placeholders = ",".join("?" for _account in accounts)
                conn.execute(
                    f"DELETE FROM current_decision_input_generations "
                    f"WHERE account IN ({placeholders})",
                    accounts,
                )
                conn.executemany(
                    "INSERT INTO current_decision_input_generations ("
                    "account,generation,case_generation,evidence_generation,"
                    "allocation_generation,source_consumption_generation,"
                    "timing_generation,combo_identity_generation,"
                    "assigned_stock_generation,updated_at_ms"
                    ") VALUES (?,0,0,0,0,0,0,0,0,?)",
                    [(account, now_ms) for account in accounts],
                )
            for trigger in (
                "trg_current_decision_assigned_stock_account_update_guard",
                "trg_current_decision_assigned_stock_account_delete_guard",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            assigned_updates = 0
            for stock_event_id, account in details["sources"][
                "assigned_accounts"
            ].items():
                cursor = conn.execute(
                    "UPDATE assigned_stock_events SET account=? "
                    "WHERE stock_event_id=? AND account IS NOT ?",
                    (account, stock_event_id, account),
                )
                assigned_updates += int(cursor.rowcount or 0)
            _ensure_current_decision_projection_schema(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assigned_stock_events_account_time "
                "ON assigned_stock_events(account,trade_time_ms,stock_event_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_cases_account_status "
                "ON trade_lifecycle_cases(account,status,updated_at_ms DESC,case_id DESC) "
                "WHERE status IN ('pending','waiting_settlement_evidence',"
                "'needs_review','partially_resolved','conflict')"
            )
            conn.execute(
                "UPDATE position_projection_source_state SET sqlite_schema_cookie=?,"
                "updated_at_ms=? WHERE singleton_id=1",
                (_projection_schema_cookie(conn), now_ms),
            )
            evidence_updates = 0
            for case_id, expected_count in details["sources"][
                "evidence_counts"
            ].items():
                row = conn.execute(
                    "SELECT revision,evidence_count "
                    "FROM trade_lifecycle_evidence_revisions WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO trade_lifecycle_evidence_revisions "
                        "(case_id,revision,evidence_count) VALUES (?,0,?)",
                        (case_id, expected_count),
                    )
                    evidence_updates += 1
                elif row["evidence_count"] != expected_count:
                    conn.execute(
                        "UPDATE trade_lifecycle_evidence_revisions "
                        "SET evidence_count=? WHERE case_id=?",
                        (expected_count, case_id),
                    )
                    evidence_updates += 1
            target_updates = 0
            for case_id, expected in details["sources"]["targets"].items():
                actual = tuple(
                    (str(row["case_id"]), str(row["account"]), str(row["target_lot_id"]), row["target_contracts"])
                    for row in conn.execute(
                        "SELECT case_id,account,target_lot_id,target_contracts "
                        "FROM trade_lifecycle_case_targets WHERE case_id=? "
                        "ORDER BY target_lot_id",
                        (case_id,),
                    )
                )
                if actual == tuple(expected):
                    continue
                conn.execute(
                    "DELETE FROM trade_lifecycle_case_targets WHERE case_id=?",
                    (case_id,),
                )
                conn.executemany(
                    "INSERT INTO trade_lifecycle_case_targets "
                    "(case_id,account,target_lot_id,target_contracts) "
                    "VALUES (?,?,?,?)",
                    expected,
                )
                target_updates += 1
            fact_updates = 0
            for fact in details["facts"].values():
                fact_updates += write_lifecycle_case_decision_fact(
                    repo,
                    fact=fact,
                    conn=conn,
                )
            _position_migration._fail(failure_hook, "after_backfill")
            final_payloads: dict[str, dict[str, Any]] = {}
            final_facts: dict[str, dict[str, Any]] = {}
            for account in accounts:
                payload, account_facts = _current_decision_projection_oracle(
                    repo,
                    account=account,
                    now_ms=now_ms,
                    assigned_stock_report=None,
                    conn=conn,
                )
                final_payloads[account] = payload
                final_facts.update(
                    (str(fact["case_id"]), fact) for fact in account_facts
                )
            _position_migration._fail(failure_hook, "before_projection")
            projection_updates = sum(
                repo.upsert_current_decision_projection(
                    current_decision_projection_row(payload),
                    conn=conn,
                )
                for payload in final_payloads.values()
            )
            final_state = _migration_state_summary(
                conn,
                accounts=accounts,
                payloads=final_payloads,
                facts=final_facts,
                targets=details["sources"]["targets"],
                assigned_accounts=details["sources"]["assigned_accounts"],
                evidence_counts=details["sources"]["evidence_counts"],
                implementation=implementation,
            )
            if not _migration_state_clean(final_state):
                raise RuntimeError("current decision migration verification failed")
            counts = {
                "assigned_accounts_backfilled": assigned_updates,
                "evidence_counts_backfilled": evidence_updates,
                "case_targets_rebuilt": target_updates,
                "case_facts_written": fact_updates,
                "projections_written": projection_updates,
            }
            _position_migration._fail(failure_hook, "before_commit")
            conn.commit()
            write_applied = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _position_migration.secure_sqlite_artifacts(path)
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_apply.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "apply",
            "write_applied": write_applied,
            "store_identity": _position_migration._store_identity(path),
            "source_manifest_hash": supplied["manifest_hash"],
            "counts": counts,
            "state": final_state,
            "sqlite_bytes": {
                "before": before,
                "after": _position_migration._file_sizes(path),
            },
        }
    )

def current_decision_projection_migration_status(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    inventory = build_current_decision_projection_migration_inventory(
        sqlite_path,
        now_ms=now_ms,
    )
    repair = dict(inventory.get("repair") or {})
    account_count = len(inventory.get("accounts") or [])
    missing = int(repair.get("projection_missing_count") or 0)
    dirty = int(repair.get("projection_dirty_count") or 0)
    mismatch = int(repair.get("projection_mismatch_count") or 0)
    if inventory["readiness"] != "ready":
        status = "dirty"
    elif account_count == 0 or missing == account_count:
        status = "absent"
    elif dirty:
        status = "dirty"
    elif mismatch or missing:
        status = "mismatch"
    else:
        status = "clean"
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_status.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "status",
            "read_only": True,
            "status": status,
            "readiness": "ready" if status == "clean" else "not_ready",
            "readiness_reasons": inventory["readiness_reasons"],
            "account_count": account_count,
            "repair": repair,
            "shadow_status": "eligible" if status == "clean" else "not_ready",
            "performance_status": "formal_artifact_required",
            "inventory_manifest_hash": inventory["manifest_hash"],
            "store_identity": inventory["store_identity"],
            "mixed_version_guard_status": inventory.get(
                "mixed_version_guard_status", "missing"
            ),
        }
    )
