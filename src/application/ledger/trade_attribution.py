from __future__ import annotations

from contextlib import nullcontext
import time
from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import build_open_adjustment_patch_contract, effective_contracts_open
from domain.domain.trade_execution import execution_identity_from_input
from domain.domain.strategy_membership import strategy_metadata_has_owner
from domain.domain.wheel import lot_strategy_metadata_from_trade_events
from .event_codec import valid_void_target_event_id
from .publisher import project_stored_trade_events_to_position_lots
from .repository import require_option_positions_event_read_repo, with_sqlite_repo_transaction
from .position_projection_migration import _store_identity, _read_only_connection, _repository
from .position_projection_runtime import run_position_projection_in_transaction
from .current_decision_projection import capture_trade_event_decision_projection_fence
from .writer import _finish_trade_event_decision_projection
from .read_only_evidence import open_trade_reconciliation_evidence_repo


ATTRIBUTION_POLICY_VERSION = "trade_attribution.v1"
_POLICY_SCOPE = ("broker", "physical_account_id", "environment", "account", "market")


def read_trade_attribution_policy(repo: Any, *, scope: Mapping[str, Any], conn: Any = None) -> dict[str, Any] | None:
    with nullcontext(conn) if conn is not None else _read_only_connection(repo.db_path.resolve()) as active:
        if not active.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone():
            return None
        row = active.execute("SELECT * FROM trade_attribution_policy_enablings WHERE "
            + " AND ".join(f"{key} = ?" for key in _POLICY_SCOPE) + " AND policy_version = ?",
            (*[scope.get(key) for key in _POLICY_SCOPE], ATTRIBUTION_POLICY_VERSION)).fetchone()
        return dict(row) if row else None


def enable_trade_attribution_policy(repo: Any, *, scope: Mapping[str, Any], effective_from_ms: int,
                                    actor: str, request_id: str, now_ms: int,
                                    apply_changes: bool = False) -> dict[str, Any]:
    request = {key: scope.get(key) for key in _POLICY_SCOPE}
    request.update(policy_version=ATTRIBUTION_POLICY_VERSION, effective_from_ms=effective_from_ms,
                   actor=actor, request_id=request_id)
    if (any(not isinstance(value, str) or not value or value.strip() != value for key, value in request.items()
            if key != "effective_from_ms") or request["broker"] != request["broker"].lower()
            or request["account"] != request["account"].lower() or request["market"] not in {"us", "hk"}
            or request["environment"] not in {"REAL", "SIMULATE"}
            or type(effective_from_ms) is not int or type(now_ms) is not int or now_ms <= 0):
        raise ValueError("invalid attribution policy enabling identity")
    request_hash = canonical_sha256(request)

    def run(active_repo: Any, conn: Any) -> dict[str, Any]:
        existing = read_trade_attribution_policy(active_repo, scope=request, conn=conn)
        if existing:
            if existing["request_id"] != request_id or existing["request_hash"] != request_hash:
                raise ValueError("attribution policy is already enabled with another request")
            return {**existing, "write_applied": False}
        written_at = max(now_ms, int(time.time() * 1000)) if apply_changes else now_ms
        if effective_from_ms < written_at:
            raise ValueError("attribution policy cannot be enabled retroactively")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone():
            raise ValueError("controlled trade attribution schema migration is required")
        result = {**request, "created_at_ms": written_at, "request_hash": request_hash}
        if apply_changes:
            columns = list(result)
            conn.execute("INSERT INTO trade_attribution_policy_enablings (" + ",".join(columns)
                         + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(result.values()))
            if effective_from_ms < int(time.time() * 1000):
                raise ValueError("attribution policy enabling missed its effective time")
        return {**result, "write_applied": apply_changes}

    if apply_changes:
        return with_sqlite_repo_transaction(repo, run)
    with _read_only_connection(repo.db_path.resolve()) as conn:
        conn.execute("BEGIN")
        return run(repo, conn)


def ledger_resource_identity(repo: Any) -> dict[str, Any]:
    candidate = require_option_positions_event_read_repo(repo)
    return _store_identity(candidate.db_path.resolve())


def read_trade_attribution_snapshot(repo: Any, *, account: str, market: str, conn: Any = None) -> dict[str, Any]:
    """All competing facts are read together; callers may not paginate this snapshot."""
    with nullcontext(conn) if conn is not None else _read_only_connection(repo.db_path.resolve()) as active:
        if conn is None:
            from .repository_schema import initialize_ledger_connection
            initialize_ledger_connection(active)
            active.execute("BEGIN")
        reader = _repository(repo.db_path.resolve())
        rows = reader.read_lifecycle_account_rows(account=account, conn=active)
        rows["account_combo_inferences"] = reader.list_combo_pair_inferences(account=account, conn=active)
        rows["wheel_activation_window"] = reader.get_current_wheel_activation_window(market=market, account=account, conn=active)
        if active.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone():
            rows["attribution_policy_enablings"] = [dict(row) for row in active.execute(
                "SELECT * FROM trade_attribution_policy_enablings WHERE account = ? AND market = ? AND policy_version = ?",
                (account, market, ATTRIBUTION_POLICY_VERSION))]
        else:
            rows["attribution_policy_enablings"] = []
        return rows


def _effective_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    voided = {target for row in events if (target := valid_void_target_event_id(row))}
    return sorted((dict(row) for row in events if row.get("event_id") not in voided
                   and row.get("event_type") != "void"),
                  key=lambda row: (int(row.get("event_time_ms") or 0), str(row.get("event_id") or "")))


def assert_trade_attribution_unclaimed(events: Sequence[Mapping[str, Any]], lot_ids: Sequence[str]) -> None:
    effective = _effective_events(events)
    metadata = lot_strategy_metadata_from_trade_events(effective)
    for lot_id in lot_ids:
        current = metadata.get(lot_id) or {}
        if strategy_metadata_has_owner(current):
            raise ValueError("trade attribution already claimed: " + lot_id)
        if _manual_ordinary_decision(effective, lot_id):
            raise ValueError("trade attribution manually excluded: " + lot_id)


def _manual_ordinary_decision(events: Sequence[Mapping[str, Any]], lot_id: str) -> dict[str, Any] | None:
    for event in reversed(events):
        raw = event.get("raw_payload") or {}
        if (event.get("event_type") == "adjust" and event.get("target_lot_id") == lot_id
                and event.get("source") == "trade_attribution"
                and raw.get("attribution_origin") == "manual" and raw.get("attribution_action") == "ordinary"
                and raw.get("actor") and raw.get("attribution_request_id")):
            return dict(event)
    return None


def trade_attribution_facts_from_events(events: Sequence[Mapping[str, Any]], *, account: str) -> list[dict[str, Any]]:
    effective = _effective_events(events)
    metadata = lot_strategy_metadata_from_trade_events(effective)
    projection = project_stored_trade_events_to_position_lots(list(events))
    lots = {lot.lot_id: dict(lot.fields) for lot in projection.lots}
    out = []
    for event in effective:
        contract = event.get("contract_key") or {}
        if event.get("event_type") != "open" or contract.get("account") != account:
            continue
        lot_id = str(event.get("lot_id") or "")
        if lot_id not in lots:
            continue
        raw = event.get("raw_payload") or {}
        execution = raw.get("execution_input") or {}
        execution_key = execution_identity_from_input(execution)
        ref = execution.get("broker_account_ref") or {}
        fields = lots[lot_id]
        membership = metadata.get(lot_id) or {}
        ordinary = _manual_ordinary_decision(effective, lot_id)
        related = [row for row in effective if row.get("lot_id") == lot_id or row.get("target_lot_id") == lot_id]
        linked = bool(membership.get("source_wheel_branch_id") or membership.get("source_stock_lot_id")
                      or membership.get("strategy_group_id"))
        decisions = [row for row in related if row.get("event_type") == "adjust"
            and row.get("source") in {"wheel_linkage", "post_trade_combo_reconciliation"}
            and (row.get("raw_payload") or {}).get("attribution_origin") in {"manual", "rule", "intent"}
            and (row.get("raw_payload") or {}).get("attribution_request_id")]
        origin = (decisions[-1]["raw_payload"]["attribution_origin"] if decisions else "inherited") if linked else None
        reasons = []
        if not execution_key or execution.get("errors") or ref.get("account_label") not in (None, "", account):
            reasons.append("execution_identity_unproven")
        if effective_contracts_open(fields) != int(event.get("contracts") or 0):
            reasons.append("execution_not_fully_open")
        if any(row.get("event_type") in {"close", "expire_close", "assignment", "exercise"} for row in related):
            reasons.append("execution_has_successors")
        if projection.diagnostics:
            reasons.append("ledger_projection_diagnostics")
        out.append({
            "schema_version": ATTRIBUTION_POLICY_VERSION, "execution_key": execution_key or None,
            "open_event_id": event["event_id"], "lot_id": lot_id, "account": account,
            "broker_account_ref": {key: ref.get(key) for key in ("broker_id", "external_account_id", "environment")},
            "contract_key": dict(contract), "contracts": int(event.get("contracts") or 0),
            "contracts_open": effective_contracts_open(fields), "position_side": fields.get("position_side"), "price": event.get("price"),
            "multiplier": event.get("multiplier"), "currency": event.get("currency"),
            "event_time_ms": event.get("event_time_ms"),
            "status": "linked" if linked else "ordinary" if ordinary else "pending",
            "strategy": membership.get("strategy") if linked else None,
            "wheel_branch_id": membership.get("source_wheel_branch_id") or membership.get("source_stock_lot_id"),
            "strategy_group_id": membership.get("strategy_group_id"),
            "origin": "manual" if ordinary else origin,
            "acknowledged_candidate_ids": list((decisions[-1]["raw_payload"].get("attribution_candidate_ids") or [])) if decisions and origin == "manual" else [],
            "reason_codes": reasons, "candidate_ids": [],
            "input_hash": canonical_sha256(related), "policy_version": ATTRIBUTION_POLICY_VERSION,
            "ledger_event_ids": [row["event_id"] for row in related if row.get("event_type") == "adjust"],
            "ordinary_previewable": not reasons and not linked,
        })
    return sorted(out, key=lambda row: (str(row["execution_key"] or ""), row["open_event_id"]))


def read_trade_attribution_facts(repo: Any, *, account: str) -> list[dict[str, Any]]:
    candidate = require_option_positions_event_read_repo(repo)
    evidence = open_trade_reconciliation_evidence_repo(candidate.db_path).read_trade_receipt_evidence()
    facts = trade_attribution_facts_from_events(evidence["trade_events"], account=account)
    stored = {row["lot_id"]: row["fields"] for row in evidence["position_lots"]}
    for fact in facts:
        fields = stored.get(fact["lot_id"])
        if fields is None or effective_contracts_open(fields) != fact["contracts_open"]:
            fact["reason_codes"].append("ledger_projection_mismatch")
            fact["ordinary_previewable"] = False
    return facts


def record_trade_attribution_conflict(repo: Any, *, account: str, execution_key: str,
    branch: Mapping[str, Any], candidate_ids: Sequence[str], input_hash: str, now_ms: int, conn: Any,
    execution_keys: Sequence[str] = ()) -> str:
    from domain.domain.wheel import build_wheel_event

    if conn is None or not conn.in_transaction or not execution_key or not candidate_ids:
        raise ValueError("attribution conflict requires a transaction and competing evidence")
    branch_id = branch["wheel_branch_id"]
    keys = sorted(set([execution_key, *execution_keys]))
    request_id = canonical_sha256({"account": account, "execution_keys": keys,
                                  "branch_id": branch_id, "candidates": sorted(set(candidate_ids)), "policy_version": ATTRIBUTION_POLICY_VERSION})
    event_id = "wheel-attribution-conflict:" + request_id
    existing = [row for row in repo.list_wheel_events(account=account, conn=conn) if row["event_id"] == event_id]
    if existing:
        return event_id
    event = build_wheel_event(event_id=event_id, account=account, lot_id=branch.get("stock_lot_id"),
        wheel_branch_id=branch_id, event_type="wheel_attribution_conflict", occurred_at_ms=now_ms, recorded_at_ms=now_ms,
        payload={"actor": "trade_intake:attribution", "request_id": request_id, "execution_keys": keys,
                 "policy_version": ATTRIBUTION_POLICY_VERSION, "reason": "late_attribution_conflict",
                 "candidate_ids": sorted(set(candidate_ids)), "branch_generation_hash": branch["batch_generation_hash"],
                 "input_hash": input_hash})
    repo.append_wheel_event_once(event, conn=conn)
    return event_id


def record_trade_ordinary_attribution(
    repo: Any, *, account: str, execution_key: str, expected_input_hash: str,
    request_id: str, actor: str, now_ms: int, apply_changes: bool = False,
) -> dict[str, Any]:
    if not all((account, execution_key, expected_input_hash, request_id, actor)) or now_ms <= 0:
        raise ValueError("ordinary attribution requires complete identity")

    def run(active: Any, conn: Any) -> dict[str, Any]:
        if conn is None:
            raise TypeError("attribution requires SQLite transaction authority")
        events = active.list_trade_events(conn=conn)
        matching = [row for row in trade_attribution_facts_from_events(events, account=account)
                    if row["execution_key"] == execution_key]
        if len(matching) != 1:
            raise ValueError("attribution execution must resolve to exactly one open lot")
        fact = matching[0]
        effective = _effective_events(events)
        decision = _manual_ordinary_decision(effective, fact["lot_id"])
        requests = [row for row in effective if (row.get("raw_payload") or {}).get("attribution_request_id") == request_id]
        if requests and (len(requests) != 1 or not decision or requests[0]["event_id"] != decision["event_id"]
                         or requests[0]["raw_payload"].get("actor") != actor):
            raise ValueError("attribution request identity conflicts")
        if decision:
            return {**fact, "write_applied": False, "status": "ordinary", "origin": "manual"}
        if fact["input_hash"] != expected_input_hash or not fact["ordinary_previewable"]:
            raise ValueError("attribution facts changed or incomplete; create a new preview")
        assert_trade_attribution_unclaimed(events, [fact["lot_id"]])
        if not apply_changes:
            return {**fact, "write_applied": False}
        fields = active.get_position_lot_fields(fact["lot_id"], conn=conn)
        strategy = "sell_put" if fact["contract_key"].get("option_type") == "put" and fields.get("position_side") == "short" else "covered_call" if fields.get("position_side") == "short" else "unassigned"
        patch = build_open_adjustment_patch_contract(fields, strategy=strategy, as_of_ms=now_ms)
        event = TradeEvent(
            event_id="trade-attribution:" + canonical_sha256({"account": account, "request_id": request_id}),
            event_type="adjust", event_time_ms=now_ms, contract_key=ContractKey.from_values(**fact["contract_key"]),
            contracts=0, price=0, currency=fact["currency"], multiplier=fact["multiplier"],
            source="trade_attribution", target_lot_id=fact["lot_id"],
            raw_payload={"attribution_origin": "manual", "attribution_action": "ordinary", "actor": actor,
                         "attribution_request_id": request_id, "attribution_policy_version": ATTRIBUTION_POLICY_VERSION,
                         "execution_key": execution_key, "adjust_target_source_event_id": fact["open_event_id"],
                         "patch": patch.to_dict()},
        )
        fence = capture_trade_event_decision_projection_fence(active, conn=conn)
        runtime = run_position_projection_in_transaction(active, [event], conn=conn, mode="forced_full")
        _finish_trade_event_decision_projection(active, conn=conn, fence=fence, events=[event], created_flags=runtime.created_flags)
        result = next(row for row in trade_attribution_facts_from_events(active.list_trade_events(conn=conn), account=account)
                      if row["execution_key"] == execution_key)
        if result["status"] != "ordinary" or result["origin"] != "manual":
            raise ValueError("ordinary attribution readback failed")
        return {**result, "write_applied": True}

    return with_sqlite_repo_transaction(repo, run, require_projection_publication=True)
