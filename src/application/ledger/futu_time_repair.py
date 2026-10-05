"""Source-bound batch correction of historical Futu execution instants."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.performance.cash_conversion import cash_fx_daily_facts
from domain.domain.trade_execution import (
    execution_instant_milliseconds, futu_execution_time, canonical_trade_execution_content,
    structured_deal_keys_from_ledger_event, ledger_execution_event_set_is_complete,
)
from .cash_conversion_migration import _conversion_from_evidence
from .event_codec import stored_trade_event_to_ledger_event, valid_void_target_event_id
from .publisher import project_stored_trade_events_to_position_lots
from .trade_attribution import trade_attribution_facts_from_events
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

REQUEST_SCHEMA = "futu_trade_time_repair_request.v1"
PROVENANCE_SCHEMA = "futu_raw_trade_time_correction.v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _paths(repo: Any) -> tuple[Path, Path]:
    ledger = Path(repo.db_path).resolve(strict=True)
    inbox = ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3").resolve(strict=True)
    if not ledger.is_file() or not inbox.is_file() or ledger.samefile(inbox):
        raise ValueError("repair requires distinct existing ledger and authoritative inbox files")
    return ledger, inbox


def open_futu_time_repair_store(*, data_config: Path, runtime_root: str | None = None) -> tuple[Any, dict[str, Any]]:
    from .repository import SQLiteOptionPositionsRepository
    from .store_resolution import resolve_ledger_store
    store = resolve_ledger_store(data_config, runtime_root=runtime_root)
    repo = SQLiteOptionPositionsRepository(store.sqlite_path, initialize=False)
    return repo, store.to_dict()


def _read_connection(repo: Any) -> sqlite3.Connection:
    ledger, inbox = _paths(repo)
    conn = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ATTACH DATABASE ? AS repair_inbox", (inbox.as_uri() + "?mode=ro",))
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        return conn
    except BaseException:
        conn.close()
        raise


def _table_hashes(conn: sqlite3.Connection, schema: str) -> dict[str, str]:
    hashes = {}
    for row in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        values = [
            [value.hex() if isinstance(value, bytes) else value for value in record]
            for record in conn.execute(f"SELECT * FROM {schema}.{quoted}")
        ]
        hashes[name] = _hash(sorted(values, key=_json))
    return hashes


def _validate_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("unsupported Futu time repair request")
    if set(request) != {"schema_version", "batch_id", "reason", "prepared_at_ms", "targets"}:
        raise ValueError("repair request fields are incomplete or unknown")
    if not isinstance(request["batch_id"], str) or not request["batch_id"].strip() or not isinstance(request["reason"], str) or not request["reason"].strip():
        raise ValueError("batch_id and reason are required")
    if type(request["prepared_at_ms"]) is not int or request["prepared_at_ms"] <= 0:
        raise ValueError("prepared_at_ms must be a positive integer")
    targets = request["targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError("explicit targets are required")
    ids = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"event_id", "before_sha256", "after_trade_time_ms"}:
            raise ValueError("invalid target fields")
        eid = target["event_id"]
        digest = target["before_sha256"]
        if not isinstance(eid, str) or not eid or eid in ids:
            raise ValueError("target event IDs must be nonempty and unique")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("target requires before_sha256")
        if type(target["after_trade_time_ms"]) is not int or target["after_trade_time_ms"] <= 0:
            raise ValueError("target time must be a positive integer")
        ids.add(eid)


def _project_invariants(before: list[dict], after: list[dict]) -> dict[str, Any]:
    old = project_stored_trade_events_to_position_lots(before)
    new = project_stored_trade_events_to_position_lots(after)
    if old.diagnostics or new.diagnostics:
        raise ValueError("repair requires a diagnostic-free canonical projection")
    def without_times(items: Any, fields: set[str]) -> list[dict]:
        return sorted(({k: v for k, v in item.to_dict().items() if k not in fields} for item in items), key=_json)
    if without_times(old.ledger_projection.lots, {"opened_at_ms"}) != without_times(new.ledger_projection.lots, {"opened_at_ms"}):
        raise ValueError("repair changes non-time lot economics or membership")
    if without_times(old.ledger_projection.allocations, {"opened_at_ms", "closed_at_ms"}) != without_times(new.ledger_projection.allocations, {"opened_at_ms", "closed_at_ms"}):
        raise ValueError("repair changes allocation economics or membership")
    accounts = {e["contract_key"]["account"] for e in before if e.get("contract_key")}
    for account in accounts:
        facts_before = trade_attribution_facts_from_events(before, account=account)
        facts_after = trade_attribution_facts_from_events(after, account=account)
        # Only the execution instant is allowed to change in attribution facts.
        def proof(facts: list[dict]) -> list[dict]:
            rows = deepcopy(facts)
            for row in rows:
                row.pop("occurred_at_utc", None)
                row.pop("trade_time_ms", None)
                row.pop("event_time_ms", None)
                row.pop("input_hash", None)
            return sorted(rows, key=_json)
        if proof(facts_before) != proof(facts_after):
            raise ValueError("repair changes attribution identity or accepted decision proof")
    return {"position_lot_count": len(new.lots), "allocation_count": len(new.ledger_projection.allocations),
            "native_economics_unchanged": True, "attribution_proofs_unchanged": True,
            "projection_lots": [lot.to_dict() for lot in new.lots]}


def _proven_keys(event: dict[str, Any]) -> set[str]:
    raw = event.get("raw_payload") or {}
    account = str(event["contract_key"]["account"]).strip().lower()
    nested = raw.get("execution_input") or {}
    labels = [raw.get(k) for k in ("internal_account", "account", "account_label")]
    labels.append((nested.get("broker_account_ref") or {}).get("account_label"))
    if any(str(label).strip().lower() != account for label in labels if label not in (None, "")):
        raise ValueError("raw and normalized internal account identity conflict")
    keys = structured_deal_keys_from_ledger_event(event, include_legacy_execution_identity=True)
    source = {**event, "raw_payload": {k: v for k, v in raw.items() if k not in {"execution_input", "execution_id"}}}
    raw_keys = structured_deal_keys_from_ledger_event(source, include_legacy_execution_identity=True)
    if not any(k.startswith("execution:v1:") for k in raw_keys) or keys != raw_keys:
        raise ValueError("raw and normalized physical execution identity conflict")
    if raw.get("execution_id") not in (None, "") and raw["execution_id"] not in keys:
        raise ValueError("stored execution ID conflicts with source identity")
    return keys


def _prepare(conn: sqlite3.Connection, repo: Any, request: dict[str, Any], plan_inbox: Callable) -> dict[str, Any]:
    _validate_request(request)
    rows = [dict(row) for row in conn.execute("SELECT event_id,event_json,trade_time_ms FROM trade_events ORDER BY event_id")]
    by_id = {row["event_id"]: row for row in rows}
    payloads = {row["event_id"]: json.loads(row["event_json"]) for row in rows}
    voided = {valid_void_target_event_id(p) for p in payloads.values()}
    # Same connection is essential for a coherent read set and the EXCLUSIVE apply transaction.
    fx = PerformanceEvidenceSQLiteRepository(repo.db_path)._read_fx_rates_conn(conn)
    rates = cash_fx_daily_facts(fx)
    changes = []
    proposed = deepcopy(payloads)
    for target in sorted(request["targets"], key=lambda x: x["event_id"]):
        eid = target["event_id"]
        row = by_id.get(eid)
        if row is None or _text_hash(row["event_json"]) != target["before_sha256"]:
            raise ValueError(f"target snapshot changed: {eid}")
        before = payloads[eid]
        event, diagnostics = stored_trade_event_to_ledger_event(before)
        if event is None or any(d.severity == "error" for d in diagnostics):
            raise ValueError(f"invalid canonical event: {eid}")
        raw = before.get("raw_payload") or {}
        if eid in voided or event.source != "opend_push" or event.event_type not in {"open", "close", "expire_close"} or str(event.contract_key.broker).lower() not in {"futu", "富途"}:
            raise ValueError(f"unsupported or voided Futu event: {eid}")
        if "trade_time_correction_provenance" in raw or "opend_order_evidence" in raw:
            raise ValueError(f"existing correction or order-evidence repair required: {eid}")
        if row["trade_time_ms"] != event.event_time_ms:
            raise ValueError(f"SQL and JSON times conflict: {eid}")
        evidence = futu_execution_time(raw)
        if evidence["errors"]:
            raise ValueError(f"raw time evidence unavailable: {eid}: {evidence['errors']}")
        after_ms = execution_instant_milliseconds(evidence["occurred_at_utc"])
        if after_ms != target["after_trade_time_ms"] or after_ms == event.event_time_ms:
            raise ValueError(f"requested correction does not match raw source time: {eid}")
        content_errors = canonical_trade_execution_content(raw)["errors"]
        if any(str(error).startswith("invalid:") for error in content_errors):
            raise ValueError(f"conflicting raw execution evidence: {eid}")
        keys = _proven_keys(before)
        if not any(k.startswith("execution:v1:") for k in keys):
            raise ValueError(f"scoped execution identity unavailable: {eid}")
        after = deepcopy(before)
        after["event_time_ms"] = after_ms
        updated_raw = after["raw_payload"]
        if isinstance(updated_raw.get("execution_input"), dict):
            execution = updated_raw["execution_input"]
            if execution_instant_milliseconds(execution.get("occurred_at_utc")) != event.event_time_ms:
                raise ValueError(f"stored execution instant disagrees with ledger: {eid}")
            execution["occurred_at_utc"] = evidence["occurred_at_utc"]
            execution["source_timezone"] = evidence["source_timezone"]
        conversions = {}
        for fact in cash_facts_for_trade_event(replace(event, event_time_ms=after_ms)):
            if fact.amount is None:
                raise ValueError(f"cash fact amount unavailable: {eid}:{fact.fact_kind}")
            conversion = _conversion_from_evidence(cash_fact_id=fact.fact_id, amount=fact.amount,
                currency=fact.currency, effective_at_ms=fact.effective_at_ms,
                fx_rates=rates, migrated_at_ms=request["prepared_at_ms"])
            if conversion["status"] != "observed":
                raise ValueError(f"booking FX unavailable: {eid}:{fact.fact_kind}")
            conversions[fact.fact_kind] = conversion
        updated_raw["cash_conversions"] = conversions
        updated_raw["trade_time_correction_provenance"] = {
            "schema_version": PROVENANCE_SCHEMA, "batch_id": request["batch_id"],
            "before_trade_time_ms": event.event_time_ms, "after_trade_time_ms": after_ms,
            "expected_before_sha256": target["before_sha256"], "provider": "opend",
            "reason": request["reason"], "corrected_at_ms": request["prepared_at_ms"],
            "source_time": evidence["source_time"], "source_timezone": evidence["source_timezone"],
        }
        if structured_deal_keys_from_ledger_event(after, include_legacy_execution_identity=True) != keys:
            raise ValueError(f"execution identity changed: {eid}")
        changes.append({"event_id": eid, "before_json": row["event_json"], "after_json": _json(after),
                        "before_payload": before, "after_payload": after,
                        "before_trade_time_ms": event.event_time_ms, "after_trade_time_ms": after_ms})
        proposed[eid] = after
    for change in changes:
        keys = structured_deal_keys_from_ledger_event(change["before_payload"], include_legacy_execution_identity=True)
        related = [payload for eid, payload in payloads.items() if eid not in voided
                   and keys & structured_deal_keys_from_ledger_event(payload, include_legacy_execution_identity=True)]
        if not ledger_execution_event_set_is_complete(related):
            raise ValueError(f"incomplete split execution evidence: {change['event_id']}")
        for payload in related:
            eid = payload["event_id"]
            if proposed[eid].get("event_time_ms") != change["after_trade_time_ms"]:
                raise ValueError(f"incomplete split execution repair: {eid}")
    invariants = _project_invariants(list(payloads.values()), list(proposed.values()))
    patches = plan_inbox(conn, changes)
    plan = {"operation": "futu_trade_time_batch_repair", "mode": "dry_run", "request": request,
            "request_hash": _hash(request), "events": changes, "inbox": patches,
            "invariants": invariants, "read_set": {schema: _table_hashes(conn, schema) for schema in ("main", "repair_inbox")}}
    plan["input_hash"] = _hash(plan)
    return plan


def prepare_futu_time_repair(repo: Any, *, request: dict[str, Any], plan_inbox: Callable) -> dict[str, Any]:
    with closing(_read_connection(repo)) as conn:
        return _prepare(conn, repo, request, plan_inbox)
