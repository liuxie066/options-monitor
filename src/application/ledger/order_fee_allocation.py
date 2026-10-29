"""Explicit operator allocation of a documented actual two-leg order expense."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from domain.domain.ledger import fee_fact_for_event
from domain.domain.ledger.fees import FeeBasis
from domain.domain.money import canonical_decimal_text
from domain.domain.trade_execution import execution_instant_milliseconds, futu_execution_time
from domain.domain.trade_account_identity import extract_visible_account_fields
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
from .event_codec import stored_trade_event_to_ledger_event
from .order_fee_migration import (
    _Unit, _allocate, _apply_unit, _fill_pending_fee_fx, _option_event_with_fee, _trade_change,
)
from .order_fee_semantics import execution_trade_side, futu_order_namespace_issue
from .publisher import project_stored_trade_events_to_position_lots
from .repository import SQLiteOptionPositionsRepository, with_sqlite_repo_transaction
from .store_resolution import resolve_ledger_store


REQUEST_SCHEMA = "confirmed_order_fee_allocation.v1"
SOURCE = "operator_allocated_order_fee"
_REQUEST_KEYS = set("schema_version request_id actor authorization_ref reason method account futu_account_id order_id currency expected_total_fee targets evidence".split())
_EVIDENCE_KEYS = set("observed_at_ms account futu_account_id environment day order deals fee".split())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return _text_hash(_json(value))


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _object(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} fields are incomplete or unknown")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a nonempty canonical string")
    return value


def _number(value: Any, name: str, *, money: bool = False, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not number.is_finite() or number < 0 or positive and number == 0:
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if money:
        try:
            cents = number.quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"{name} is outside supported money precision") from exc
        if number != cents:
            raise ValueError(f"{name} must have cent precision")
    return number


def _validate_request(request: Any) -> None:
    _object(request, _REQUEST_KEYS, "allocation request")
    if request["schema_version"] != REQUEST_SCHEMA or request["method"] != "equal":
        raise ValueError("unsupported allocation schema or method")
    for key in ("request_id", "actor", "authorization_ref", "reason", "account", "futu_account_id", "order_id"):
        _text(request[key], key)
    from src.application.account_config import normalize_account_label
    if normalize_account_label(request["account"]) != request["account"]:
        raise ValueError("account must be canonical lowercase")
    physical = request["futu_account_id"]
    if not physical.isascii() or not physical.isdecimal() or str(int(physical)) != physical or int(physical) <= 0:
        raise ValueError("futu_account_id must be a lossless positive integer string")
    if request["currency"] not in {"HKD", "USD", "CNY"}:
        raise ValueError("allocation currency is invalid")
    total = _number(request["expected_total_fee"], "expected_total_fee", money=True, positive=True)
    if total / 2 != (total / 2).quantize(Decimal("0.01")):
        raise ValueError("equal fee shares must have exact cent precision")
    targets = request["targets"]
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError("exactly two explicit allocation targets are required")
    for target in targets:
        _object(target, {"event_id", "before_sha256"}, "allocation target")
        _text(target["event_id"], "event_id")
        digest = target["before_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(x not in "0123456789abcdef" for x in digest):
            raise ValueError("target requires a lowercase before_sha256")
    if len({t["event_id"] for t in targets}) != 2:
        raise ValueError("allocation target IDs must be unique")
    evidence = _object(request["evidence"], _EVIDENCE_KEYS, "broker evidence")
    if type(evidence["observed_at_ms"]) is not int or evidence["observed_at_ms"] <= 0:
        raise ValueError("evidence observed_at_ms must be a positive integer")
    if evidence["environment"] != "REAL" or any(evidence[k] != request[k] for k in ("account", "futu_account_id")):
        raise ValueError("broker evidence account/environment conflicts with request")
    if not isinstance(evidence["day"], str) or date.fromisoformat(evidence["day"]).isoformat() != evidence["day"]:
        raise ValueError("evidence day must be YYYY-MM-DD")
    order = _object(evidence["order"], set("order_id currency order_status dealt_qty combo_legs".split()), "order evidence")
    if order["order_id"] != request["order_id"] or order["currency"] != request["currency"] or order["order_status"] != "FILLED_ALL":
        raise ValueError("broker order scope or terminal status conflicts")
    quantity = _number(order["dealt_qty"], "order dealt_qty", positive=True)
    if quantity != quantity.to_integral_value():
        raise ValueError("combo order quantity must be whole")
    legs = order["combo_legs"]
    if not isinstance(legs, list) or len(legs) != 2:
        raise ValueError("exactly two structured combo legs are required")
    for leg in legs:
        _object(leg, {"code", "trd_side", "qty_ratio"}, "combo leg")
        _text(leg["code"], "combo leg code")
        if leg["trd_side"] not in {"BUY", "SELL", "BUY_BACK", "SELL_SHORT"} or _number(leg["qty_ratio"], "qty_ratio") != 1:
            raise ValueError("only unit-ratio combo legs with explicit trade sides are supported")
    if len({leg["code"] for leg in legs}) != 2:
        raise ValueError("combo leg codes must be unique")
    deals = evidence["deals"]
    if not isinstance(deals, list) or len(deals) != 2:
        raise ValueError("exactly two unsplit broker deals are required")
    for deal in deals:
        _object(deal, set("deal_id order_id code trd_side qty price create_time".split()), "deal evidence")
        _text(deal["deal_id"], "deal_id")
        if deal["order_id"] != request["order_id"]:
            raise ValueError("deal order conflicts with request")
        leg = next((leg for leg in legs if leg["code"] == deal["code"]), None)
        if leg is None or leg["trd_side"] != deal["trd_side"] or _number(deal["qty"], "deal qty", positive=True) != quantity:
            raise ValueError("broker combo/deal identity or quantity conflicts")
        _number(deal["price"], "deal price")
        if not isinstance(deal["create_time"], str) or deal["create_time"][:10] != evidence["day"]:
            raise ValueError("deal is outside the submitted evidence day")
    if len({deal["deal_id"] for deal in deals}) != 2 or {deal["code"] for deal in deals} != {leg["code"] for leg in legs}:
        raise ValueError("broker deal identities must map one to one to combo legs")
    fee = _object(evidence["fee"], {"order_id", "fee_amount", "fee_details"}, "fee evidence")
    if fee["order_id"] != request["order_id"] or _number(fee["fee_amount"], "broker order fee", money=True) != total:
        raise ValueError("broker total fee conflicts with request")
    details = fee["fee_details"]
    if not isinstance(details, list) or not details:
        raise ValueError("broker fee details are required")
    amounts = []
    for item in details:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("fee detail must be [name, amount]")
        _text(item[0], "fee detail name")
        amounts.append(_number(item[1], "fee detail amount", money=True))
    if sum(amounts) != total:
        raise ValueError("broker fee details do not conserve order total")


def open_order_fee_allocation_store(*, data_config: Path, runtime_root: str | None = None) -> tuple[Any, dict[str, Any]]:
    store = resolve_ledger_store(data_config, runtime_root=runtime_root)
    return SQLiteOptionPositionsRepository(store.sqlite_path, initialize=False), store.to_dict()


@contextmanager
def _read(repo: Any) -> Iterator[sqlite3.Connection]:
    path = Path(repo.db_path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM trade_events ORDER BY event_id")]


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row["event_json"])
    if not isinstance(payload, dict) or payload.get("event_id") != row["event_id"]:
        raise ValueError("stored event identity or JSON is invalid")
    if payload.get("raw_payload") is not None and not isinstance(payload["raw_payload"], dict):
        raise ValueError("stored raw payload must be an object")
    return payload


def _group(rows: list[dict[str, Any]], request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payloads = {row["event_id"]: _payload(row) for row in rows}
    voided = {p.get("target_event_id") for p in payloads.values() if p.get("event_type") == "void"}
    group = {}
    for eid, payload in payloads.items():
        raw, key = payload.get("raw_payload") or {}, payload.get("contract_key") or {}
        if (eid not in voided and str(raw.get("futu_account_id") or "") == request["futu_account_id"]
                and raw.get("order_id") == request["order_id"]):
            if key.get("account") != request["account"]:
                raise ValueError("broker order spans conflicting account labels")
            group[eid] = payload
    if set(group) != {target["event_id"] for target in request["targets"]}:
        raise ValueError("targets must equal the complete active order group")
    return group


def _prior(conn: sqlite3.Connection, rows: list[dict[str, Any]], request: dict[str, Any], expected: str | None) -> dict[str, Any] | None:
    group = _group(rows, request)
    found = []
    for row in rows:
        p = (_payload(row).get("raw_payload") or {}).get("fee_provenance")
        if isinstance(p, dict) and p.get("source") == SOURCE and p.get("request_id") == request["request_id"]:
            if row["event_id"] not in group or p.get("request_hash") != _hash(request):
                raise ValueError("allocation request_id conflicts with a prior request")
            found.append((row, p))
    if not found:
        return None
    if len(found) != 2:
        raise ValueError("partial prior allocation state")
    hashes = {p.get("input_hash") for _, p in found}
    if len(hashes) != 1 or not next(iter(hashes)) or expected is not None and expected not in hashes:
        raise ValueError("prior allocation input hash conflicts")
    unit_identity = "manual-order-fee:" + request["request_id"] + ":" + _hash(request)
    for row, _ in found:
        before = next(t["before_sha256"] for t in request["targets"] if t["event_id"] == row["event_id"])
        audit = conn.execute(
            "SELECT 1 FROM broker_fee_enrichment_audit WHERE event_kind='option_trade' AND event_id=? "
            "AND before_event_sha256=? AND after_event_sha256=? AND write_unit_identity_sha256=?",
            (row["event_id"], before, _text_hash(row["event_json"]), _text_hash(unit_identity)),
        ).fetchone()
        if audit is None:
            raise ValueError("prior allocation audit/readback drift")
    return {"mode": "no_op", "input_hash": next(iter(hashes)), "request_hash": _hash(request),
            "write_applied": False, "durable_readback": True, "event_count": 2}


def _prepare(conn: sqlite3.Connection, repo: Any, request: dict[str, Any]) -> tuple[dict[str, Any], _Unit]:
    rows = _rows(conn)
    group = _group(rows, request)
    lots = [dict(row) for row in conn.execute("SELECT * FROM position_lots ORDER BY lot_id")]
    projection = project_stored_trade_events_to_position_lots([json.loads(row["event_json"]) for row in rows])
    expected_lots = sorted((lot.to_dict() for lot in projection.lots), key=lambda x: x["lot_id"])
    stored_lots = [{"lot_id": row["lot_id"], "fields": json.loads(row["fields_json"])} for row in lots]
    if projection.diagnostics or stored_lots != expected_lots:
        raise ValueError("stored position projection is not current before allocation")
    fx_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='performance_fx_rate_facts'").fetchone()
    fx_rates = PerformanceEvidenceSQLiteRepository(repo.db_path).read_fx_rates(conn=conn).fx_rates if fx_exists else ()
    fx_payload = {"fx_rate_facts": fx_rates}
    schema = [list(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    input_hash = _hash({"store": str(Path(repo.db_path).resolve()), "request": request,
                        "events": rows, "lots": lots, "schema": schema,
                        "fx": [rate.normalized_payload() for rate in fx_rates]})
    amounts = _allocate(_number(request["expected_total_fee"], "total"), (1, 1))
    allocations = [{"event_id": eid, "amount": canonical_decimal_text(amount)}
                   for eid, amount in zip(sorted(group), amounts, strict=True)]
    changes = []
    matched_deals: set[str] = set()
    for allocation in allocations:
        eid = allocation["event_id"]
        row = next(row for row in rows if row["event_id"] == eid)
        target = next(t for t in request["targets"] if t["event_id"] == eid)
        if _text_hash(row["event_json"]) != target["before_sha256"]:
            raise ValueError("target event changed since supplied evidence")
        event, diagnostics = stored_trade_event_to_ledger_event(group[eid])
        if event is None or diagnostics or event.event_type not in {"open", "close"} or event.asset_type != "option":
            raise ValueError("allocation requires canonical option open/close events")
        raw = event.raw_payload
        visible_accounts = extract_visible_account_fields(raw)
        source = raw.get("_trade_intake_source")
        if source is not None and (not isinstance(source, dict)
                or source.get("account", request["account"]) != request["account"]
                or str(source.get("futu_account_id", request["futu_account_id"])) != request["futu_account_id"]):
            raise ValueError("stored intake account scope conflicts")
        if (event.contract_key.broker != "富途" or event.currency != request["currency"]
                or futu_order_namespace_issue(raw) or raw.get("trd_env", "REAL") != "REAL"
                or any(value != request["futu_account_id"] for key, value in visible_accounts.items() if key != "account")
                or visible_accounts.get("account", request["account"]) not in {request["account"], request["futu_account_id"]}
                or row.get("account") != request["account"]
                or raw.get("internal_account", request["account"]) != request["account"]):
            raise ValueError("target broker/account/currency/environment conflicts")
        completion = raw.get("broker_deal_completion")
        if completion and (not isinstance(completion, dict) or completion.get("split_count") != 1
                           or completion.get("split_index") != 1 or completion.get("expected_contracts") != event.contracts):
            raise ValueError("split broker executions are not supported")
        deal_ids = {str(raw[k]) for k in ("source_deal_id", "deal_id") if raw.get(k) not in (None, "")}
        if len(deal_ids) != 1:
            raise ValueError("exact source deal identity is required")
        deal = next((d for d in request["evidence"]["deals"] if d["deal_id"] in deal_ids), None)
        if deal is None or deal["deal_id"] in matched_deals:
            raise ValueError("each target must consume a distinct broker deal")
        matched_deals.add(deal["deal_id"])
        instant = futu_execution_time(deal)
        if (instant["errors"] or execution_instant_milliseconds(instant["occurred_at_utc"]) != event.event_time_ms
                or request["evidence"]["observed_at_ms"] < event.event_time_ms
                or str(raw.get("code") or "") != deal["code"]
                or _number(deal["qty"], "deal qty") != event.contracts
                or _number(deal["price"], "deal price") != event.price
                or ("buy" if deal["trd_side"] in {"BUY", "BUY_BACK"} else "sell") != execution_trade_side(event)
                or raw.get("trd_side", deal["trd_side"]) != deal["trd_side"]):
            raise ValueError("broker deal economics or execution identity conflicts")
        before = fee_fact_for_event(event)
        if before.basis == FeeBasis.ACTUAL:
            raise ValueError("existing actual fee cannot be replaced by manual allocation")
        updated = _option_event_with_fee(event, basis=FeeBasis.ACTUAL,
            amount=Decimal(allocation["amount"]), applied_at_ms=request["evidence"]["observed_at_ms"],
            provenance={"source": SOURCE, "reason": "user_confirmed_equal_allocation",
                "allocation_basis": "user_confirmed_equal", "schema_version": REQUEST_SCHEMA,
                "request_id": request["request_id"], "request_hash": _hash(request), "input_hash": input_hash,
                "actor": request["actor"], "authorization_ref": request["authorization_ref"],
                "operator_reason": request["reason"], "method": "equal",
                "broker_evidence_source": "operator_supplied_opend_snapshot",
                "broker_evidence": request["evidence"], "broker_evidence_sha256": _hash(request["evidence"]),
                "broker_order_fee_total": canonical_decimal_text(request["expected_total_fee"]),
                "currency": request["currency"], "allocations": allocations,
                "original_targets": request["targets"]})
        change = _trade_change(event, updated, before_json=row["event_json"],
                               before_basis=before.basis.value, after_basis="actual")
        changes.append(_fill_pending_fee_fx(change, fx_payload=fx_payload,
                                           applied_at_ms=request["evidence"]["observed_at_ms"]))
    if matched_deals != {deal["deal_id"] for deal in request["evidence"]["deals"]}:
        raise ValueError("targets must consume the complete broker deal set")
    unit = _Unit(identity="manual-order-fee:" + request["request_id"] + ":" + _hash(request), changes=tuple(changes))
    preview = {"schema_version": REQUEST_SCHEMA, "mode": "dry_run", "input_hash": input_hash,
        "request_hash": _hash(request), "write_applied": False, "event_count": 2,
        "evidence_validation": "operator_submitted_consistency_only", "broker_leg_fees_observed": False,
        "currency": request["currency"], "order_fee_total": canonical_decimal_text(request["expected_total_fee"]),
        "allocations": allocations, "events": [{"event_id": c.event_id, "before_sha256": _text_hash(c.before_json),
            "after_sha256": _text_hash(c.after_json), "before": json.loads(c.before_json), "after": json.loads(c.after_json)} for c in changes]}
    return preview, unit


def prepare_confirmed_order_fee_allocation(repo: Any, *, request: dict[str, Any]) -> dict[str, Any]:
    _validate_request(request)
    with _read(repo) as conn:
        prior = _prior(conn, _rows(conn), request, None)
        return prior if prior else _prepare(conn, repo, request)[0]


def _backup(repo: Any, backup_dir: str | Path) -> dict[str, Any]:
    directory = Path(backup_dir).expanduser()
    if not directory.parent.is_dir():
        raise ValueError("backup parent directory must already exist")
    directory.mkdir(mode=0o700)
    path = directory / "ledger.sqlite3"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dest = sqlite3.connect(path)
    try:
        with _read(repo) as source:
            source.backup(dest)
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("allocation backup integrity check failed")
    finally:
        dest.close()
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"path": str(path.resolve()), "sha256": digest, "integrity_check": "ok"}


def _readback(repo: Any, request: dict[str, Any], expected: str) -> dict[str, Any] | None:
    with _read(repo) as conn:
        return _prior(conn, _rows(conn), request, expected)


def apply_confirmed_order_fee_allocation(repo: Any, *, request: dict[str, Any],
        expected_input_hash: str, backup_dir: str | Path) -> dict[str, Any]:
    _validate_request(request)
    if not expected_input_hash or not backup_dir:
        raise ValueError("apply requires expected_input_hash and a new backup_dir")
    backup = None
    admitted = False
    try:
        def commit(sqlite_repo: Any, conn: sqlite3.Connection) -> dict[str, Any]:
            nonlocal backup, admitted
            prior = _prior(conn, _rows(conn), request, expected_input_hash)
            if prior:
                return prior
            preview, unit = _prepare(conn, sqlite_repo, request)
            if preview["input_hash"] != expected_input_hash:
                raise ValueError("allocation preview input hash changed; create a fresh preview")
            backup = _backup(sqlite_repo, backup_dir)
            admitted = True
            result = _apply_unit(sqlite_repo, conn, unit, applied_at_ms=int(time.time() * 1000), resolve_pending_fee_fx=False)
            return {**preview, "mode": "committed", "write_applied": True, "backup": backup, "result": result}
        result = with_sqlite_repo_transaction(repo, commit, require_projection_publication=True)
    except Exception as exc:
        if not admitted:
            raise
        try:
            proof = _readback(repo, request, expected_input_hash)
            if proof:
                return {**proof, "mode": "committed", "write_applied": True, "backup": backup}
            with _read(repo) as conn:
                rows = {row["event_id"]: row for row in _rows(conn)}
                unchanged = all(_text_hash(rows[t["event_id"]]["event_json"]) == t["before_sha256"] for t in request["targets"])
            if unchanged:
                return {"mode": "not_applied", "write_applied": False, "durable_readback": True,
                        "backup": backup, "error_type": type(exc).__name__, "error": str(exc)}
        except Exception:
            pass
        return {"mode": "commit_outcome_unknown", "write_applied": None, "durable_readback": False,
                "backup": backup, "error_type": type(exc).__name__, "error": str(exc)}
    try:
        proof = _readback(repo, request, expected_input_hash)
        if proof is None:
            raise ValueError("committed allocation is absent from independent readback")
    except Exception as exc:
        return {**result, "mode": "committed_readback_unavailable", "durable_readback": False,
                "error_type": type(exc).__name__, "error": str(exc)}
    return {**result, "durable_readback": True}


__all__ = ["open_order_fee_allocation_store", "prepare_confirmed_order_fee_allocation", "apply_confirmed_order_fee_allocation"]
