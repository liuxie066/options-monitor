from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import json
import time
import shutil
from tempfile import TemporaryDirectory
from typing import Any

from domain.domain.combo_identity import validate_combo_identity
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.events import CLOSE_EVENT_TYPES
from domain.domain.ledger.position_fields import apply_strategy_metadata_patch
from domain.domain.strategy_membership import resolve_option_strategy_membership
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import normalize_wheel_event
from src.application.ledger.combo_membership import resolve_combo_group_membership
from src.application.ledger.event_codec import stored_trade_event_to_ledger_event, valid_void_target_event_id
from src.application.ledger.read_only_evidence import open_trade_reconciliation_evidence_repo
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.repository_assigned_stock import _wheel_activation_row
from src.application.ledger.wheel_trade_companions import plan_wheel_assignment_companion, _wheel_branches_from_rows


def _identity(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _rows(reader: Any, conn: sqlite3.Connection, account: str) -> dict[str, Any]:
    # These reads deliberately require the installed schema; no migration or fallback.
    events = reader._read_trade_events(conn, strict=True)
    lots = reader._read_position_lots(conn, strict=True)
    wheels = [normalize_wheel_event({
        **dict(row), "payload": json.loads(row["payload_json"]),
    }) for row in conn.execute("SELECT * FROM wheel_events ORDER BY occurred_at_ms, event_id")]
    stocks = reader._read_json_query_from_conn(
        conn, "SELECT event_json FROM assigned_stock_events ORDER BY trade_time_ms, stock_event_id",
        (), column="event_json", strict=True,
    )
    identities = reader._read_json_query_from_conn(
        conn,
        "SELECT raw_json FROM strategy_group_identities WHERE account = ? ORDER BY group_id",
        (account,),
        column="raw_json",
        strict=True,
    )
    return {
        "trade_events": events,
        "account_position_lots": [r for r in lots if r["fields"].get("account") == account],
        "account_wheel_events": [r for r in wheels if r["account"] == account],
        "account_assigned_stock_events": [r for r in stocks if (r.get("account") or (r.get("raw_payload") or {}).get("account")) == account],
        "account_strategy_group_identities": identities,
    }


def _completed_combo_transition(
    rows: dict[str, Any],
    events: list[Any],
    fields: dict[str, Any],
    assignment: Any,
) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
    group_id = str(fields.get("strategy_group_id") or "").strip()
    identities = [
        item
        for item in rows["account_strategy_group_identities"]
        if str(item.get("group_id") or "").strip() == group_id
    ]
    if len(identities) != 1:
        raise ValueError("completed combo transition requires one exact group identity")
    identity = identities[0]
    validation = validate_combo_identity(identity)
    if validation.status != "valid" or validation.identity_hash != identity.get("identity_hash"):
        raise ValueError("completed combo transition requires a valid group identity")
    membership = resolve_combo_group_membership(
        group_id=group_id,
        account=assignment.contract_key.account,
        expected_symbol=assignment.contract_key.underlying_symbol,
        trade_events=rows["trade_events"],
        projected_position_lots=rows["account_position_lots"],
    )
    expected_lots = {
        str(identity.get("funding_put_record_id") or "").strip(),
        str(identity.get("participation_call_record_id") or "").strip(),
    }
    if (
        identity.get("strategy") != "combo_yield"
        or identity.get("account") != assignment.contract_key.account
        or identity.get("symbol") != assignment.contract_key.underlying_symbol
        or str(identity.get("funding_put_record_id") or "").strip()
        != assignment.target_lot_id
        or int(identity.get("original_contracts") or 0) != assignment.contracts
        or membership.fact.get("status") != "exact"
        or set(membership.global_current_lot_ids) != expected_lots
        or membership.global_live_lot_ids
    ):
        raise ValueError("completed combo transition requires an exact fully closed group")
    voided = {
        target
        for raw in rows["trade_events"]
        for target in [valid_void_target_event_id(raw)]
        if target
    }
    sibling_lot_id = str(identity.get("participation_call_record_id") or "").strip()
    allowed_later = {
        event.event_id
        for event in events
        if event.target_lot_id == sibling_lot_id
        and event.event_type in CLOSE_EVENT_TYPES
        and event.event_id not in voided
    }
    if not allowed_later:
        raise ValueError("completed combo transition requires a terminal sibling event")
    ordinary_fields = apply_strategy_metadata_patch(
        fields,
        {
            "strategy": None,
            "leg_role": None,
            "strategy_group_id": None,
            "strategy_snapshot": None,
        },
    )
    evidence = {
        "identity": identity,
        "membership_generation_hash": membership.generation_hash,
        "terminal_sibling_event_ids": sorted(allowed_later),
    }
    return ordinary_fields, allowed_later, evidence


def _plan(reader: Any, conn: sqlite3.Connection, path: Path, account: str, market: str,
          assignment_event_id: str, instant: int, allow_completed_combo_yield: bool) -> dict[str, Any]:
    rows = _rows(reader, conn, account)
    events = []
    for raw in rows["trade_events"]:
        event, diagnostics = stored_trade_event_to_ledger_event(raw)
        if event is None or any(d.severity == "error" for d in diagnostics):
            raise ValueError("recovery requires valid canonical trade facts")
        events.append(event)
    matches = [e for e in events if e.event_id == assignment_event_id]
    if len(matches) != 1:
        raise ValueError("assignment event must resolve uniquely")
    assignment = matches[0]
    key = assignment.contract_key
    if (assignment.event_type != "assignment" or key.account != account
            or symbol_market(key.underlying_symbol).lower() != market
            or assignment.event_time_ms <= 0 or assignment.event_time_ms > instant):
        raise ValueError("assignment identity, market, account or time mismatch")
    lots = [r for r in rows["account_position_lots"] if r["record_id"] == assignment.target_lot_id]
    if len(lots) != 1:
        raise ValueError("assignment source lot must resolve uniquely")
    fields = lots[0]["fields"]
    valid_combo_ids = {
        str(item.get("group_id") or "").strip()
        for item in rows["account_strategy_group_identities"]
    }
    membership = resolve_option_strategy_membership(
        key, assignment.position_side, fields, valid_combo_group_ids=valid_combo_ids,
    )
    planned_fields = fields
    allowed_later: set[str] = set()
    combo_transition: dict[str, Any] | None = None
    if membership.strategy == "csp_lc" and membership.leg_role == "funding_put":
        if not allow_completed_combo_yield:
            raise ValueError("recovery requires ordinary CSP/CC membership")
        planned_fields, allowed_later, combo_transition = _completed_combo_transition(
            rows, events, fields, assignment,
        )
    elif membership.issues or membership.strategy not in {"csp", "cc"}:
        raise ValueError("recovery requires ordinary CSP/CC membership")
    source_id = fields.get("source_event_id")
    if any(valid_void_target_event_id(raw) in {assignment_event_id, source_id}
           for raw in rows["trade_events"]):
        raise ValueError("assignment or source open is void")
    windows = [_wheel_activation_row(r) for r in conn.execute(
        "SELECT * FROM wheel_activation_windows WHERE account = ? AND market = ? ORDER BY generation",
        (account, market),
    )]
    eligible = [w for w in windows if w["activated_at_ms"] <= assignment.event_time_ms
                and (w["deactivated_at_ms"] is None or assignment.event_time_ms < w["deactivated_at_ms"])]
    if len(eligible) != 1:
        raise ValueError("assignment must belong to exactly one historical activation window")
    planned, reason = plan_wheel_assignment_companion(
        assignment, planned_fields, rows, eligible[0], recorded_at_ms=assignment.event_time_ms,
    )
    if planned is None:
        raise ValueError(f"assignment recovery blocked: {reason}")
    existing = [w for w in rows["account_wheel_events"] if w["event_id"] == planned["event_id"]]
    if existing:
        stored_window = existing[0]["payload"].get("activation_window")
        current_window = eligible[0]
        closure_fields = {"deactivated_at_ms", "deactivation_request_id", "deactivation_request_hash"}
        if stored_window != current_window:
            legitimate_close = (
                isinstance(stored_window, dict)
                and all(stored_window.get(k) is None for k in closure_fields)
                and all(current_window.get(k) is not None for k in closure_fields)
                and {k: v for k, v in stored_window.items() if k not in closure_fields}
                == {k: v for k, v in current_window.items() if k not in closure_fields}
            )
            if not legitimate_close:
                raise ValueError("existing Wheel activation window conflict")
            # Closing a window does not rewrite the original creation receipt.
            planned, reason = plan_wheel_assignment_companion(
                assignment, planned_fields, rows, stored_window, recorded_at_ms=assignment.event_time_ms,
            )
            if planned is None:
                raise ValueError(f"assignment recovery blocked: {reason}")
    if existing and existing[0]["payload_hash"] != planned["payload_hash"]:
        raise ValueError("existing Wheel event payload conflict")
    events_by_id = {event.event_id: event for event in events}
    historical_voids = {
        event.event_id
        for event in events
        for target in [events_by_id.get(event.target_event_id or "")]
        if event.event_type == "void"
        and target is not None
        and target.event_time_ms < assignment.event_time_ms
    }
    later = [e.event_id for e in events if e.contract_key.account == account
             and e.contract_key.underlying_symbol == key.underlying_symbol
             and e.event_id != assignment_event_id and e.event_id not in allowed_later
             and e.event_id not in historical_voids
             and e.event_time_ms >= assignment.event_time_ms]
    later.extend(str(s.get("stock_event_id") or s.get("event_id")) for s in rows["account_assigned_stock_events"]
                 if str(s.get("symbol") or (s.get("raw_payload") or {}).get("symbol") or "") == key.underlying_symbol
                 and int(s.get("trade_time_ms") or s.get("event_time_ms") or 0) >= assignment.event_time_ms)
    if later and not existing:
        raise ValueError("recovery cannot prove subsequent facts safe: " + ", ".join(sorted(set(later))))
    projected_rows = {**rows, "account_wheel_events": rows["account_wheel_events"] + ([] if existing else [planned])}
    branches = [b for b in _wheel_branches_from_rows(projected_rows, account=account, as_of_ms=instant)
                if b["start_event_id"] == planned["event_id"]]
    if len(branches) != 1:
        raise ValueError("recovery current projection readback failed")
    branch = branches[0]
    if reason and (branch["phase"] == "ready" or not branch["reason_codes"]):
        raise ValueError("incomplete assignment must remain blocked")
    relevant_events = [e.to_dict() for e in events if e.contract_key.account == account
                       and e.contract_key.underlying_symbol == key.underlying_symbol]
    fingerprint = {"ledger": _identity(path), "account": account, "market": market,
                   "assignment_event_id": assignment_event_id, "trade_events": relevant_events,
                   "source_lot": lots[0], "windows": windows,
                   "combo_transition": combo_transition,
                   "wheel_events": rows["account_wheel_events"],
                   "stock_events": rows["account_assigned_stock_events"], "planned_event": {k: v for k, v in planned.items() if k != "recorded_at_ms"}}
    return {"status": "already_present" if existing else "preview", "write_applied": False,
            "ledger": fingerprint["ledger"], "account": account, "market": market,
            "assignment_event_id": assignment_event_id, "event": existing[0] if existing else planned,
            "preview_hash": canonical_sha256(fingerprint), "branch": branch,
            "source_transition": "completed_combo_yield" if combo_transition else "ordinary_assignment"}


def recover_wheel_assignment(*, sqlite_path: str | Path, account: str, market: str,
                             assignment_event_id: str, expected_preview_hash: str | None = None,
                             apply: bool = False, confirm: bool = False,
                             allow_completed_combo_yield: bool = False) -> dict[str, Any]:
    """Append only the missing Wheel companion for one exact assignment."""
    path = Path(sqlite_path).expanduser().resolve()
    if not account or account != account.strip().lower() or market not in {"us", "hk"} or not assignment_event_id:
        raise ValueError("recovery requires lowercase account, market and exact assignment event ID")
    if apply and (not confirm or not expected_preview_hash):
        raise ValueError("recovery apply requires confirm and expected preview hash")
    if confirm and not apply:
        raise ValueError("recovery confirm requires apply")
    if not path.is_file():
        raise ValueError("existing ledger database is required")
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    if wal.exists() != shm.exists():
        raise ValueError("ledger WAL state is unreadable without side effects")
    reader = open_trade_reconciliation_evidence_repo(path)
    try:
        # SQLite read-only WAL connections may write SHM. Read a stable temporary
        # copy instead; never connect to the source until an authorized apply.
        def source_files():
            return {str(p): (p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                    for p in (path, wal, shm) if p.exists()}
        before = source_files()
        with TemporaryDirectory(prefix="om-wheel-preview-") as directory:
            snapshot = Path(directory) / path.name
            shutil.copyfile(path, snapshot)
            if wal.exists():
                shutil.copyfile(wal, Path(str(snapshot) + "-wal"))
            if source_files() != before:
                raise ValueError("ledger changed while copying preview; retry")
            with closing(sqlite3.connect(snapshot)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                preview = _plan(
                    reader, conn, path, account, market, assignment_event_id,
                    int(time.time() * 1000), allow_completed_combo_yield,
                )
        if not apply:
            return preview
        repo = SQLiteOptionPositionsRepository(path, initialize=False)
        with repo._writer_connection(begin_immediate=True) as conn:
            current = _plan(
                reader, conn, path, account, market, assignment_event_id,
                int(time.time() * 1000), allow_completed_combo_yield,
            )
            if current["ledger"] != preview["ledger"]:
                raise ValueError("ledger file identity changed")
            if current["status"] == "already_present":
                return current
            if current["preview_hash"] != expected_preview_hash:
                raise ValueError("recovery preview hash changed; preview again")
            current["event"]["recorded_at_ms"] = int(time.time() * 1000)
            if not repo.append_wheel_event_once(current["event"], conn=conn):
                raise ValueError("unexpected Wheel append result")
            readback = _plan(
                reader, conn, path, account, market, assignment_event_id,
                int(time.time() * 1000), allow_completed_combo_yield,
            )
            return {**readback, "status": "applied", "write_applied": True}
    except sqlite3.Error as exc:
        raise ValueError(f"ledger recovery unavailable: {exc}") from exc
