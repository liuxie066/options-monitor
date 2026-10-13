from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any, Callable, Iterator, Mapping, Sequence
import time

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import effective_contracts_open
from domain.domain.trade_execution import execution_identity_from_input, legacy_open_execution_input_from_event
from domain.domain.strategy_membership import strategy_metadata_has_owner, POSITION_LOT_STRATEGY_PATCH_FIELDS, validate_attribution_decision
from domain.domain.ledger.events import lot_id_for_open_event
from domain.domain.wheel import lot_strategy_metadata_from_trade_events, effective_wheel_events, build_wheel_event
from .event_codec import valid_void_target_event_id
from .publisher import project_stored_trade_events_to_position_lots
from .repository import require_option_positions_event_read_repo, with_sqlite_repo_transaction
from .position_projection_migration import _store_identity, _read_only_connection, _repository
from .position_projection_runtime import run_position_projection_in_transaction
from .current_decision_projection import capture_trade_event_decision_projection_fence
from .writer import _finish_trade_event_decision_projection
from .read_only_evidence import open_trade_reconciliation_evidence_repo
from .repository_wheel_policy import effective_wheel_window
from .combo_membership import resolve_combo_group_membership, publish_combo_pair_identity
from .combo_reconciliation import combo_attribution_candidates_from_rows
from src.application.account_config import combo_reconciliation_mode_for_account
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, resolve_futu_account_ids
from domain.domain.strategy_membership import (resolve_trade_attribution, resolve_option_strategy_membership,
    trade_attribution_conflict_execution_keys)
from domain.domain.combo_reconciliation import delivered_combo_exposures_for_lot
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel.intents import resolve_wheel_fill_intent, plan_wheel_call_intent_consume, plan_wheel_put_intent_consume
from domain.domain.wheel import project_wheel_linkage_candidates
from domain.domain.ledger.position_fields import build_open_adjustment_patch_contract
from domain.domain.wheel_call_allocation import parse_wheel_call_allocations
from domain.domain.trade_contract_identity import contract_share_quantity
from domain.domain import risk_capacity
from src.application.portfolio_context_service import evaluate_account_cash_snapshot, cash_snapshot_is_usable
from src.infrastructure.exchange_rates import capacity_currency_converter
from .assigned_stock_projection import build_wheel_read_model_from_rows, build_wheel_read_model_with_capacity_from_rows


ATTRIBUTION_POLICY_VERSION = "trade_attribution.v2"
_POLICY_SCOPE = ("broker", "physical_account_id", "environment", "account", "market")


def read_trade_attribution_policy(repo: Any, *, scope: Mapping[str, Any], conn: Any = None) -> dict[str, Any] | None:
    with nullcontext(conn) if conn is not None else _read_only_connection(repo.db_path.resolve()) as active:
        if not active.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone():
            return None
        row = active.execute("SELECT * FROM trade_attribution_policy_enablings WHERE "
            + " AND ".join(f"{key} = ?" for key in _POLICY_SCOPE) + " AND policy_version = ?",
            (*[scope.get(key) for key in _POLICY_SCOPE], ATTRIBUTION_POLICY_VERSION)).fetchone()
        return dict(row) if row else None


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
        if rows["wheel_activation_window"] is None:
            windows = reader.list_wheel_activation_windows(market=market, account=account, conn=active)
            if windows:
                bindings = reader.list_wheel_policy_bindings(market=market, account=account, conn=active)
                rows["wheel_activation_window"] = effective_wheel_window(windows[-1], bindings)
        if active.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone():
            rows["attribution_policy_enablings"] = [dict(row) for row in active.execute(
                "SELECT * FROM trade_attribution_policy_enablings WHERE account = ? AND market = ? AND policy_version = ?",
                (account, market, ATTRIBUTION_POLICY_VERSION))]
        else:
            rows["attribution_policy_enablings"] = []
        return rows


@contextmanager
def open_trade_attribution_snapshot_reader(
    repo: Any, *, account: str, market: str,
) -> Iterator[Callable[..., dict[str, Any] | None]]:
    """Observe committed changes on one read-only connection for one batch.

    None means unchanged since this reader's last full observation. The caller
    owns snapshot rows; this reader retains only a connection-local data_version.
    No transaction spans calls and no version is a policy or time-validity lease.
    """
    path = repo.db_path.resolve()
    identity = _store_identity(path)

    def check_identity() -> None:
        if repo.db_path.resolve() != path or _store_identity(path) != identity:
            raise ValueError("trade attribution ledger identity changed during observation")

    with _read_only_connection(path) as observer:
        version: int | None = None

        def read_if_changed(*, force: bool = False) -> dict[str, Any] | None:
            nonlocal version
            check_identity()
            row = observer.execute("PRAGMA data_version").fetchone()
            if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
                raise ValueError("trade attribution ledger data_version is unavailable")
            sampled_version = row[0]
            check_identity()
            if not force and version == sampled_version:
                return None
            rows = read_trade_attribution_snapshot(repo, account=account, market=market)
            check_identity()
            # Remember the BEFORE-read version: a commit during materialization
            # must cause another observation, even if this snapshot includes it.
            version = sampled_version
            return rows

        yield read_if_changed


def _effective_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    voided = {target for row in events if (target := valid_void_target_event_id(row))}
    return sorted((dict(row) for row in events if row.get("event_id") not in voided
                   and row.get("event_type") != "void"),
                  key=lambda row: (int(row.get("event_time_ms") or 0), str(row.get("event_id") or "")))


def assert_trade_attribution_unclaimed(events: Sequence[Mapping[str, Any]], lot_ids: Sequence[str]) -> None:
    effective = _effective_events(events)
    accepted: set[str] = set()
    metadata = lot_strategy_metadata_from_trade_events(effective, accepted_proof_event_ids=accepted)
    for lot_id in lot_ids:
        current = metadata.get(lot_id) or {}
        if strategy_metadata_has_owner(current):
            raise ValueError("trade attribution already claimed: " + lot_id)
        if _manual_ordinary_decision(effective, lot_id, metadata=metadata, accepted=accepted):
            raise ValueError("trade attribution manually excluded: " + lot_id)


def _manual_ordinary_decision(events: Sequence[Mapping[str, Any]], lot_id: str, *,
    metadata: Mapping[str, Mapping[str, Any]], accepted: set[str],
) -> dict[str, Any] | None:
    for event in reversed(events):
        raw = event.get("raw_payload") or {}
        if (event.get("event_type") == "adjust" and event.get("target_lot_id") == lot_id
                and any(key in (raw.get("patch") or {}) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS)):
            if "attribution_decision" in raw and event["event_id"] not in accepted:
                continue
            if (event.get("source") == "trade_attribution"
                and raw.get("attribution_origin") == "manual" and raw.get("attribution_action") == "ordinary"
                and raw.get("actor") and raw.get("attribution_request_id")):
                if raw.get("attribution_decision"):
                    current = metadata.get(lot_id, {})
                    if any(current.get(key) != raw["patch"].get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS):
                        return None
                return dict(event)
            return None
    return None


def _legacy_manual_wheel_call_confirmation(
    related: Sequence[Mapping[str, Any]], *, opening: Mapping[str, Any],
    lot_id: str, membership: Mapping[str, Any],
) -> bool:
    """Recognize the original durable confirmation, never infer it from a link alone."""
    latest = next((row for row in reversed(related) if row.get("event_type") == "adjust"
        and any(key in ((row.get("raw_payload") or {}).get("patch") or {})
                for key in POSITION_LOT_STRATEGY_PATCH_FIELDS)), None)
    if latest is None:
        return False
    raw = latest.get("raw_payload") or {}
    patch = raw.get("patch") or {}
    branch = patch.get("source_wheel_branch_id")
    if (latest.get("source") != "wheel_linkage"
            or raw.get("schema_version") != "wheel_call_linkage_confirmed.v1"
            or "attribution_decision" in raw
            or not all(raw.get(key) for key in ("actor", "wheel_linkage_request_id",
                "input_snapshot_hash", "batch_generation_hash", "linkage_candidate_id"))
            or latest.get("target_lot_id") != lot_id or raw.get("target_lot_id") != lot_id
            or raw.get("adjust_target_source_event_id") != opening.get("event_id")
            or not branch or patch.get("source_stock_lot_id") != branch
            or raw.get("source_wheel_branch_id") != branch or raw.get("source_stock_lot_id") != branch
            or patch.get("strategy") != "wheel" or patch.get("leg_role") != "wheel_call"
            or any(membership.get(key) != patch.get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS)
            or latest.get("event_time_ms", 0) < opening.get("event_time_ms", 0)):
        return False
    try:
        proof, fill = TradeEvent.from_dict(latest), TradeEvent.from_dict(opening)
        return (proof.contract_key == fill.contract_key and proof.currency == fill.currency
            and proof.multiplier == fill.multiplier and proof.contracts == 0
            and proof.price == 0 and proof.fees == 0)
    except (TypeError, ValueError, KeyError):
        return False


def trade_attribution_facts_from_events(events: Sequence[Mapping[str, Any]], *, account: str) -> list[dict[str, Any]]:
    effective = _effective_events(events)
    accepted: set[str] = set()
    metadata = lot_strategy_metadata_from_trade_events(effective, accepted_proof_event_ids=accepted)
    projection = project_stored_trade_events_to_position_lots(list(events))
    lots = {lot.lot_id: dict(lot.fields) for lot in projection.lots}
    by_open_event: dict[str, list[str]] = {}
    for lot_id, fields in lots.items():
        open_event_id = str(fields.get("open_event_id") or fields.get("source_event_id") or "")
        if open_event_id:
            by_open_event.setdefault(open_event_id, []).append(lot_id)
    out = []
    for event in effective:
        contract = event.get("contract_key") or {}
        if event.get("event_type") != "open" or contract.get("account") != account:
            continue
        mapped_lots = by_open_event.get(str(event.get("event_id") or ""), [])
        if len(mapped_lots) != 1:
            continue
        lot_id = mapped_lots[0]
        if event.get("lot_id") and event["lot_id"] != lot_id:
            continue
        raw = event.get("raw_payload") or {}
        execution = raw.get("execution_input") or legacy_open_execution_input_from_event(event)
        execution_key = execution_identity_from_input(execution)
        ref = execution.get("broker_account_ref") or {}
        fields = lots[lot_id]
        membership = metadata.get(lot_id) or {}
        ordinary = _manual_ordinary_decision(effective, lot_id, metadata=metadata, accepted=accepted)
        related = [row for row in effective if row.get("event_id") == event["event_id"]
                   or row.get("lot_id") == lot_id or row.get("target_lot_id") == lot_id]
        allocations = membership.get("wheel_call_allocations") or []
        linked = bool(allocations or membership.get("source_wheel_branch_id") or membership.get("source_stock_lot_id")
                      or membership.get("strategy_group_id"))
        decisions = [row for row in related if row.get("event_type") == "adjust"
            and row.get("source") in {"trade_attribution", "wheel_linkage", "post_trade_combo_reconciliation"}
            and (row.get("raw_payload") or {}).get("attribution_origin") in {"manual", "rule", "intent"}
            and (row.get("raw_payload") or {}).get("attribution_request_id")
            and ("attribution_decision" not in (row.get("raw_payload") or {}) or row["event_id"] in accepted)]
        origin = (decisions[-1]["raw_payload"]["attribution_origin"] if decisions else
            "manual" if _legacy_manual_wheel_call_confirmation(related, opening=event,
                lot_id=lot_id, membership=membership) else "inherited") if linked else None
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
            "contract_key": TradeEvent.from_dict(event).contract_key.to_dict(), "contracts": int(event.get("contracts") or 0),
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
            **({"wheel_call_allocations": allocations} if allocations else {}),
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
    execution_keys: Sequence[str] = (), diagnostic: Mapping[str, Any] | None = None) -> str:
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
                 "input_hash": input_hash, **({"diagnostic": dict(diagnostic)} if diagnostic is not None else {})})
    repo.append_wheel_event_once(event, conn=conn)
    return event_id


def read_trade_attribution_decision(rows: Mapping[str, Any], *, account: str, request_id: str,
    request_content: Mapping[str, Any], now_ms: int) -> dict[str, Any] | None:
    prior = [event for event in rows["trade_events"] if (event.get("raw_payload") or {}).get("attribution_request_id") == request_id]
    if not prior:
        return None
    if any((event.get("raw_payload") or {}).get("attribution_request") != request_content
           or (event.get("contract_key") or {}).get("account") != account for event in prior):
        raise ValueError("attribution request identity conflicts")
    valid = _effective_events([row for row in rows["trade_events"] if int(row.get("event_time_ms") or 0) <= now_ms])
    valid_ids = {row["event_id"] for row in valid}
    if any(event["event_id"] not in valid_ids for event in prior):
        raise ValueError("attribution decision proof is no longer effective")
    decision = prior[0]["raw_payload"].get("attribution_decision")
    if not isinstance(decision, Mapping):
        raise ValueError("attribution request has no complete decision proof")
    accepted: set[str] = set()
    current = lot_strategy_metadata_from_trade_events(valid, accepted_proof_event_ids=accepted)
    if any(row["event_id"] not in accepted for row in prior):
        raise ValueError("attribution decision proof was not accepted by replay")
    if {member["proof_event_id"] for member in decision["members"]} != {row["event_id"] for row in prior}:
        raise ValueError("attribution request proof set differs")
    for event in prior:
        patch = event["raw_payload"]["patch"]
        if any(current.get(event["target_lot_id"], {}).get(key) != patch.get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS):
            raise ValueError("attribution durable decision is no longer effective")
    statuses: dict[str, Any] = {}
    effective_wheel_events(rows["account_wheel_events"], as_of_ms=now_ms, trade_events=valid,
        known_trade_event_ids=valid_ids, conflict_statuses=statuses)
    if any(not statuses.get(key, {}).get("resolved") for key in request_content["conflict_event_ids"]):
        raise ValueError("attribution conflict decision is no longer effective")
    return {"decision": decision, "proof_event_ids": sorted(event["event_id"] for event in prior)}


def write_trade_attribution_decision(
    repo: Any, *, conn: Any, account: str, request_id: str, actor: str,
    input_hash: str, plans: Sequence[Mapping[str, Any]], conflicts: Sequence[Mapping[str, Any]],
    branch_generations: Mapping[str, str], now_ms: int, manual: bool,
    wheel_events: Sequence[Mapping[str, Any]] = (),
    request_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish every member and conflict proof together under the caller's lock."""
    if conn is None or not conn.in_transaction or not plans or conflicts and not manual:
        raise ValueError("attribution decision requires transaction authority")
    before_events = repo.list_trade_events(conn=conn)
    metadata = lot_strategy_metadata_from_trade_events(before_events)
    members = []
    for plan in plans:
        fact = plan["fact"]
        patch = plan["patch"]
        members.append({key: fact[key] for key in ("execution_key", "open_event_id", "lot_id")} | {
            "proof_event_id": "trade-attribution:" + canonical_sha256({"account": account,
                "request_id": request_id, "open_event_id": fact["open_event_id"]}),
            "before": {key: metadata.get(fact["lot_id"], {}).get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS},
            "after": {key: patch.get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS},
        })
    decision = {"schema_version": "attribution_decision.v1", "request_id": request_id,
        "actor": actor, "account": account, "input_hash": input_hash, "manual": manual,
        "policy_version": ATTRIBUTION_POLICY_VERSION, "branch_generations": dict(branch_generations),
        "conflict_event_ids": sorted(row["event_id"] for row in conflicts), "members": members}
    events = []
    for plan, member in zip(plans, members):
        fact = plan["fact"]
        patch = {**plan["patch"], **member["after"]}
        event = TradeEvent(event_id=member["proof_event_id"], event_type="adjust", event_time_ms=now_ms,
            contract_key=ContractKey.from_values(**fact["contract_key"]), contracts=0, price=0,
            currency=fact["currency"], multiplier=fact["multiplier"],
            source={"wheel": "wheel_linkage", "combo": "post_trade_combo_reconciliation"}.get(plan["action"], "trade_attribution"),
            target_lot_id=fact["lot_id"], raw_payload={
                "patch": patch, "adjust_target_source_event_id": fact["open_event_id"],
                "actor": actor, "attribution_request_id": request_id,
                "attribution_policy_version": ATTRIBUTION_POLICY_VERSION,
                "attribution_origin": "manual" if manual else plan.get("origin", "rule"),
                "attribution_action": plan["action"], "attribution_candidate_id": plan.get("candidate_id"),
                "attribution_request": dict(request_content or {}),
                "attribution_candidate_ids": sorted(set(plan.get("candidate_ids", [])) |
                    {candidate for conflict in conflicts for candidate in conflict["payload"]["candidate_ids"]}),
                "attribution_decision": decision,
            })
        events.append(event)
    active = _effective_events([*before_events, *(event.to_dict() for event in events)])
    validate_attribution_decision(decision, events=active,
        opening_lot_ids={row["event_id"]: lot_id_for_open_event(row) for row in active if row["event_type"] == "open"},
        as_of_ms=now_ms)
    resolutions = []
    for conflict in conflicts:
        keys = set(conflict["payload"]["execution_keys"])
        covered = [member for member in members if member["execution_key"] in keys]
        if {member["execution_key"] for member in covered} != keys:
            raise ValueError("attribution decision does not cover the conflict")
        proof_ids = [member["proof_event_id"] for member in covered]
        resolutions.append(build_wheel_event(
            event_id="wheel-attribution-resolved:" + canonical_sha256({"account": account,
                "request_id": request_id, "conflict_event_id": conflict["event_id"]}),
            account=account, lot_id=conflict.get("stock_lot_id"), wheel_branch_id=conflict.get("wheel_branch_id"),
            event_type="wheel_attribution_conflict_resolved", occurred_at_ms=now_ms, recorded_at_ms=now_ms,
            payload={"actor": actor, "request_id": request_id, "conflict_event_id": conflict["event_id"],
                "input_hash": input_hash, "branch_generation_hash": branch_generations[conflict["wheel_branch_id"]],
                "resolution_evidence_event_id": proof_ids[0], "resolution_evidence_event_ids": proof_ids}))
    prior_statuses: dict[str, Any] = {}
    effective_wheel_events(repo.list_wheel_events(account=account, conn=conn), as_of_ms=now_ms,
        trade_events=before_events, known_trade_event_ids={row["event_id"] for row in before_events},
        conflict_statuses=prior_statuses)
    fence = capture_trade_event_decision_projection_fence(repo, conn=conn)
    # All strategy proofs are visible before the single projection publication.
    for event in [*wheel_events, *resolutions]:
        repo.append_wheel_event_once(event, conn=conn)
    runtime = run_position_projection_in_transaction(repo, events, conn=conn, mode="forced_full")
    for inference_id, pair in {plan["inference"]["inference_id"]: plan["inference"]
                               for plan in plans if plan.get("inference")}.items():
        pair = dict(pair)
        revalidated_expired = pair.pop("revalidated_expired", False) is True
        repo.upsert_combo_pair_inference(pair, conn=conn, reactivate_expired=manual and revalidated_expired)
        identity, _membership = publish_combo_pair_identity(repo, conn=conn, inference=pair)
        existing_pair = repo.get_combo_pair_inference(inference_id, conn=conn)
        if existing_pair["status"] == "user_confirmed":
            continue
        proof_by_lot = {member["lot_id"]: member["proof_event_id"] for member in members}
        repo.transition_combo_pair_inference(inference_id=inference_id,
            expected_statuses=[pair["status"]], new_status="user_confirmed",
            expected_input_hash=pair["input_snapshot_hash"], decision_fields={
                "decision_at_ms": now_ms, "decision_by": actor, "decision_reason": "user_confirmed_exact_pair",
                "strategy_group_id": pair["strategy_group_id"], "identity_hash": identity["identity_hash"],
                "put_adoption_event_id": proof_by_lot[pair["put_record_id"]],
                "call_adoption_event_id": proof_by_lot[pair["call_record_id"]]}, conn=conn)
    for group in {member[side].get("strategy_group_id") for member in members for side in ("before", "after")} - {None, ""}:
        membership = resolve_combo_group_membership(group_id=group, account=account,
            trade_events=repo.list_trade_events(conn=conn), projected_position_lots=repo.list_position_lots(conn=conn))
        if membership.fact["status"] not in {"exact", "released"}:
            raise ValueError("attribution final Combo membership is invalid")
    _finish_trade_event_decision_projection(repo, conn=conn, fence=fence, events=events,
        created_flags=runtime.created_flags)
    stored = repo.list_trade_events(conn=conn)
    accepted: set[str] = set()
    current = lot_strategy_metadata_from_trade_events(stored, accepted_proof_event_ids=accepted)
    if any(member["proof_event_id"] not in accepted for member in members):
        raise ValueError("attribution proof readback was not accepted by replay")
    for member in members:
        if any(current.get(member["lot_id"], {}).get(key) != value for key, value in member["after"].items()):
            raise ValueError("attribution membership readback failed")
    after_statuses: dict[str, Any] = {}
    effective_wheel_events(repo.list_wheel_events(account=account, conn=conn), as_of_ms=now_ms,
        trade_events=stored, known_trade_event_ids={row["event_id"] for row in stored},
        conflict_statuses=after_statuses)
    selected = set(decision["conflict_event_ids"])
    if any(not after_statuses.get(key, {}).get("resolved") for key in selected):
        raise ValueError("attribution conflict resolution readback failed")
    if any(after_statuses.get(key) != value for key, value in prior_statuses.items() if key not in selected):
        raise ValueError("attribution decision changed another conflict")
    return {"decision": decision, "proof_event_ids": [event.event_id for event in events],
        "resolution_event_ids": [row["event_id"] for row in resolutions], "write_applied": any(runtime.created_flags)}


def _multi_wheel_call_attribution(
    *, view: Mapping[str, Any], current: Mapping[str, Any], branch_ids: tuple[str, ...],
    decided_execution_keys: set[str],
) -> dict[str, Any]:
    if (current["status"] not in {"pending", "conflict"} or current["position_side"] != "short"
            or current["contract_key"]["option_type"] != "call" or not current["evidence_complete"]
            or current["reason_codes"] and set(current["reason_codes"]) != {"multiple_strategy_candidates"}):
        raise ValueError("Wheel Call allocation requires one complete, unclaimed short Call fill")
    if len(branch_ids) != current["contracts"] or not branch_ids:
        raise ValueError("Wheel Call allocation must account for every contract")
    if current["contracts_open"] != current["contracts"]:
        raise ValueError("Wheel Call allocation requires the original fill to be fully open")
    multiplier = int(current["multiplier"])
    if multiplier <= 0 or multiplier != current["multiplier"]:
        raise ValueError("Wheel Call multiplier is invalid")
    branch_by_id = {row["wheel_branch_id"]: row for row in view["wheel_model"]["wheel_branches"]}
    allocations = []
    for branch_id in sorted(set(branch_ids)):
        count = branch_ids.count(branch_id)
        candidate = next((row for row in current["candidates"]
                          if row["candidate_id"] == "wheel:" + branch_id), None)
        branch = branch_by_id.get(branch_id)
        if candidate is None or branch is None or branch["direction"] != "call" or candidate.get("intent_id"):
            raise ValueError("Wheel Call allocation branch is unavailable")
        if set(candidate["reason_codes"]) - {"wheel_branch_capacity_exceeded", "competing_fills_exceed_capacity"}:
            raise ValueError("Wheel Call allocation branch evidence is incomplete")
        competing = [row for row in view["rows"] if row["lot_id"] != current["lot_id"]
                     and row["execution_key"] not in decided_execution_keys
                     and row["status"] in {"pending", "conflict"}
                     and not (row.get("wheel_branch_id") or row.get("wheel_call_allocations")
                              or row.get("strategy_group_id"))
                     and any(item["candidate_id"] == "wheel:" + branch_id for item in row["candidates"])]
        own = sum(row["contracts"] for row in current.get("wheel_call_allocations") or [] if row["wheel_branch_id"] == branch_id)
        own += current["contracts_open"] if current.get("wheel_branch_id") == branch_id else 0
        if (competing or candidate["competition_available_shares"] is None
                or len(decided_execution_keys) == 1
                and count * multiplier > candidate["competition_available_shares"] + own * multiplier):
            raise ValueError("Wheel Call allocation exceeds unclaimed branch capacity")
        allocations.append({"stock_lot_id": branch["stock_lot_id"], "wheel_branch_id": branch_id,
                            "contracts": count})
    normalized = parse_wheel_call_allocations(allocations)
    if sum(row[2] for row in normalized) != current["contracts"]:
        raise ValueError("Wheel Call allocation quantity does not match the fill")
    candidate_id = "wheel-multi:" + canonical_sha256(sorted(branch_ids))[:24]
    preview = {**current, "candidate_id": candidate_id, "wheel_call_allocations": allocations,
               "write_applied": False}
    return preview



def _branch_account_ref(branch: Mapping[str, Any], rows: Mapping[str, Any]) -> dict[str, Any] | None:
    events = {row["event_id"]: row for row in rows["trade_events"]}
    opens = {lot_id_for_open_event(row): row
             for row in events.values() if row.get("event_type") == "open"}
    starts = {row["event_id"]: row for row in rows["account_wheel_events"]}
    start = starts.get(branch.get("start_event_id"), {})
    source = events.get(branch.get("source_assignment_event_id") or start.get("source_trade_event_id"), {})
    # Settlement events may inherit physical identity from their exact source lot.
    references = []
    for event in (source, opens.get(source.get("target_lot_id"), {})):
        raw = event.get("raw_payload") or {}
        ref = (raw.get("execution_input") or {}).get("broker_account_ref") or {}
        if not ref and event.get("event_type") == "open":
            account_id = str(raw.get("futu_account_id") or "").strip()
            deal_id = str(raw.get("source_deal_id") or raw.get("deal_id") or "").strip()
            environment = str(raw.get("trd_env") or "").strip().upper()
            if (account_id and deal_id and environment in {"REAL", "SIMULATE"}
                    and event.get("broker") in {"futu", "富途"}
                    and event.get("event_id") == f"futu:{event.get('account')}:{account_id}:{deal_id}"):
                ref = {"broker_id": "futu", "external_account_id": account_id, "environment": environment}
        if all(ref.get(key) for key in ("broker_id", "external_account_id", "environment")):
            references.append({key: ref[key] for key in ("broker_id", "external_account_id", "environment")})
    return references[0] if references and all(ref == references[0] for ref in references) else None



def build_trade_attribution_view(
    rows: Mapping[str, Any], *, config: Mapping[str, Any], account: str, market: str, now_ms: int,
    combo_evidence: Mapping[str, Any], capacity_observation: Mapping[str, Any] | None = None,
    combo_mode: str = "confirm", revalidate_expired: bool = False,
) -> dict[str, Any]:
    """Collect every competitor before selecting any execution; this function never writes."""
    # The Wheel package initializes its application workflows; load config after the ledger facade.
    from src.application.wheel.config import resolve_wheel_config, evaluate_wheel_activation_readiness
    if combo_mode not in {"off", "observe", "confirm", "auto"}:
        raise ValueError("invalid Combo attribution mode")
    account_facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
    facts = [row for row in account_facts
             if str(symbol_market(row["contract_key"]["underlying_symbol"]) or "").lower() == market]
    physical_ids = resolve_futu_account_ids(config, account=account)
    settings = infer_futu_portfolio_settings(config, account=account)
    for fact in facts:
        ref = fact["broker_account_ref"]
        if (len(physical_ids) != 1 or ref.get("external_account_id") not in physical_ids
                or ref.get("environment") != str(settings.get("trd_env") or "").upper()
                or ref.get("broker_id") != "futu"):
            fact["reason_codes"].append("configured_physical_account_mismatch")
            fact["ordinary_previewable"] = False
    stored = {row["record_id"]: row["fields"] for row in rows["stored_position_lots"]}
    wheel_config = resolve_wheel_config(config, account, market=market)
    readiness = evaluate_wheel_activation_readiness(
        wheel_config.get("activation_descriptor"), rows.get("wheel_activation_window"),
        account_configured=wheel_config["account_configured"],
    )
    intent_history_readiness = (readiness if wheel_config["account_configured"] else
        evaluate_wheel_activation_readiness(wheel_config.get("activation_descriptor"),
            rows.get("wheel_activation_window"), account_configured=True))
    late_intent_allowed = (readiness["reason_code"] in {"closed_window", "account_not_configured"}
        and intent_history_readiness["reason_code"] in {None, "closed_window"}
        and not intent_history_readiness.get("policy_drift"))
    model, capacity_model = build_wheel_read_model_with_capacity_from_rows(
        rows, account=account, as_of_ms=now_ms, market=market, monitoring_readiness=readiness,
    )
    branches = model["wheel_branches"]
    conflict_statuses = {}
    wheel_events, _wheel_errors = effective_wheel_events(rows["account_wheel_events"], as_of_ms=now_ms, trade_events=rows["trade_events"],
        conflict_statuses=conflict_statuses,
        known_trade_event_ids={event["event_id"] for event in rows["trade_events"]})
    blocked_executions = {execution for event in wheel_events if event["event_type"] == "wheel_attribution_conflict"
        and not conflict_statuses[event["event_id"]]["resolved"]
        for execution in event["payload"]["execution_keys"]}
    exposures = list(combo_evidence.get("exposures") or [])
    combos = combo_attribution_candidates_from_rows(rows, account=account, runtime_environment="",
        exposures=exposures, effective_now_ms=now_ms, include_claimed=True, revalidate_expired=revalidate_expired)
    combo_lots = {row["record_id"]: row for row in combos["lot_facts"]}
    known_proposals = {row["inference_id"] for row in rows["account_combo_inferences"]
                       if row.get("status") in {"proposal_ready", "ambiguous", "user_confirmed"}}
    candidates: dict[str, list[dict[str, Any]]] = {row["lot_id"]: [] for row in facts}
    by_lot = {row["lot_id"]: row for row in facts}
    for pair in combos["inferences"]:
        if combo_mode == "off" and pair["inference_id"] not in known_proposals and pair["evidence_grade"] != "exact_delivered_candidate":
            continue
        members = [pair["put_record_id"], pair["call_record_id"]]
        reasons = []
        if (pair["evidence_grade"] != "exact_delivered_candidate" or pair.get("alternative_inference_ids")
                or pair["status"] != "proposal_ready"):
            reasons.append("combo_expired_pair_not_unique" if pair.get("revalidated_expired")
                           else "combo_not_unique_delivered_pair")
        if pair.get("revalidated_expired"):
            reasons.append("combo_confirmation_required")
        if any(lot not in by_lot or by_lot[lot]["reason_codes"] or by_lot[lot].get("origin") == "manual" for lot in members):
            reasons.append("combo_member_unavailable")
        if members[0] in by_lot:
            reasons.extend(trade_attribution_capacity_check(config=config, fact=by_lot[members[0]], facts=account_facts,
                wheel_read_model=capacity_model, observation=capacity_observation or {}, now_ms=now_ms,
                check_account_capacity=False)["reason_codes"])
        for lot in members:
            if lot in candidates:
                candidates[lot].append({"candidate_id": "combo:" + pair["strategy_group_id"], "strategy": "combo_yield",
                    "eligible": not reasons, "reason_codes": reasons[:], "member_lot_ids": members,
                    "strategy_group_id": pair["strategy_group_id"], "inference": pair})
    for fact in facts:
        lot = fact["lot_id"]
        if effective_contracts_open(stored.get(lot) or {}) != fact["contracts_open"]:
            fact["reason_codes"].append("ledger_projection_mismatch")
        covered_exposures = {exposure for candidate in candidates[lot]
                             for exposure in candidate["inference"]["candidate_exposure_ids"]}
        covered_exposures.update(combos["rejected_exposure_ids_by_lot"].get(lot, []))
        for exposure in delivered_combo_exposures_for_lot(combo_lots.get(lot) or {}, exposures):
            if exposure not in covered_exposures:
                candidates[lot].append({"candidate_id": "combo-exposure:" + exposure, "strategy": "combo_yield",
                    "eligible": False, "reason_codes": ["combo_proposal_expired"
                        if exposure in combos["expired_exposure_ids_by_lot"].get(lot, [])
                        else "combo_counterpart_missing_or_asymmetric"], "member_lot_ids": [lot]})
        if fact["position_side"] != "short" or fact["contracts_open"] <= 0:
            continue
        contract = fact["contract_key"]
        # ponytail: reuse the historical projector per fill; cache by event time if account history becomes large.
        matching_branches = [branch for branch in branches
            if branch["symbol"] == contract["underlying_symbol"] and branch["direction"] == contract["option_type"]
            and branch["lifecycle_status"] == "active"]
        if not matching_branches:
            continue
        historical = build_wheel_read_model_from_rows(rows, account=account, as_of_ms=fact["event_time_ms"], market=market)
        history = {row["wheel_branch_id"]: row for row in historical["wheel_branches"]}
        for branch in matching_branches:
            branch_id = branch["wheel_branch_id"]
            reasons = []
            prior = history.get(branch_id)
            if prior is None or prior["lifecycle_status"] != "active":
                continue
            if prior["integrity_status"] != "trusted":
                reasons.append("wheel_branch_not_trusted_at_fill")
            branch_ref = _branch_account_ref(branch, rows)
            if branch_ref is not None and branch_ref != fact["broker_account_ref"]:
                continue
            if branch_ref is None:
                reasons.append("wheel_physical_account_unproven")
            rejected = [event for event in wheel_events
                if event["event_type"] == f"wheel_{branch['direction']}_linkage_rejected"
                and (event.get("wheel_branch_id") or event.get("stock_lot_id")) == branch_id
                and fact["open_event_id"] in {(event.get("payload") or {}).get(key)
                    for key in ("call_open_event_id", "option_open_event_id", "put_open_event_id")}]
            if rejected:
                continue
            fill = next(event for event in rows["trade_events"] if event["event_id"] == fact["open_event_id"])
            intent_check = resolve_wheel_fill_intent(branch, fill, rows["account_wheel_events"], now_ms=now_ms,
                known_trade_event_ids={event["event_id"] for event in rows["trade_events"]})
            reasons.extend(intent_check["reason_codes"])
            intent = intent_check["intent"]
            if branch["integrity_status"] != "trusted" or (not readiness["ready"]
                    and not (late_intent_allowed and intent is not None)):
                reasons.append("wheel_branch_not_ready")
            consumed_reservation = ({"wheel_branch_id": branch_id, "intent_id": intent["intent_id"],
                "contracts": intent_check["reserved_contracts_to_consume"]} if intent else None)
            try:
                multiplier = int(fact["multiplier"])
                if multiplier <= 0 or multiplier != fact["multiplier"]:
                    raise ValueError("invalid multiplier")
                available = (int(branch["shares_remaining"]) - int(branch.get("active_option_committed_shares") or 0)
                             - int(branch.get("active_intent_reserved_shares") or 0)) if branch["direction"] == "call" else (
                    int(branch["remaining_contracts"]) - int(branch.get("active_option_committed_contracts") or 0)
                    - int(branch.get("active_intent_reserved_contracts") or 0)) * multiplier
                available += intent_check["reserved_contracts_to_consume"] * multiplier
                if fact["wheel_branch_id"] == branch_id:
                    available += fact["contracts_open"] * multiplier
                if fact["contracts"] * multiplier > available:
                    reasons.append("wheel_branch_capacity_exceeded")
            except (KeyError, TypeError, ValueError):
                available = None
                reasons.append("wheel_branch_capacity_unavailable")
            # Trusted coverage at both times proves this unrelated branch is
            # already occupied, not an alternative target or competing demand.
            # Keep unknown evidence, intents and prior single/multi ownership.
            if (branch["direction"] == "call" and available == 0 and intent is None
                    and reasons == ["wheel_branch_capacity_exceeded"]
                    and fact["wheel_branch_id"] != branch_id
                    and not any(item["wheel_branch_id"] == branch_id
                                for item in fact.get("wheel_call_allocations") or [])
                    and all(projection.get("coverage", {}).get("status") == "full"
                            and lot not in projection.get("active_option_lot_ids", [])
                            for projection in (prior, branch))):
                continue
            check = trade_attribution_capacity_check(config=config, fact=fact, facts=account_facts, wheel_read_model=capacity_model,
                observation=capacity_observation or {}, now_ms=now_ms, consumed_reservation=consumed_reservation)
            reasons.extend(check["reason_codes"])
            candidates[lot].append({"candidate_id": "wheel:" + branch_id, "strategy": "wheel", "eligible": not reasons,
                "reason_codes": sorted(set(reasons)), "wheel_branch_id": branch_id, "member_lot_ids": [lot],
                "branch_generation_hash": branch["batch_generation_hash"], "available_shares": available,
                "competition_available_shares": None if available is None else available - (fact["contracts_open"] * multiplier if fact["wheel_branch_id"] == branch_id else 0),
                "intent_id": intent["intent_id"] if intent else None, "consumed_reservation": consumed_reservation,
                "intent_remaining_contracts": intent["remaining_contracts"] if intent else None,
                "capacity_evidence_complete": check.get("evidence_complete") is True,
                "capacity_reason_codes": check["reason_codes"]})
    # Same-intent executions transfer reservation together in one ledger transaction.
    intent_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for fact in facts:
        if fact["status"] != "pending" or fact["reason_codes"]:
            continue
        for candidate in candidates[fact["lot_id"]]:
            if candidate.get("intent_id"):
                intent_groups.setdefault((candidate["wheel_branch_id"], candidate["intent_id"]), []).append((fact, candidate))
    for group in intent_groups.values():
        total = sum(fact["contracts"] for fact, _ in group)
        credit = sum(candidate["consumed_reservation"]["contracts"] for _, candidate in group)
        member_ids = sorted(fact["lot_id"] for fact, _ in group)
        for fact, candidate in group:
            reasons = set(candidate["reason_codes"]) - set(candidate.pop("capacity_reason_codes"))
            reasons.discard("wheel_branch_capacity_exceeded")
            if total > candidate["intent_remaining_contracts"]:
                reasons.add("competing_fills_exceed_intent_remainder")
            if candidate["available_shares"] is not None:
                extra_credit = (credit - candidate["consumed_reservation"]["contracts"]) * int(fact["multiplier"])
                candidate["available_shares"] += extra_credit
                candidate["competition_available_shares"] += extra_credit
                if total * int(fact["multiplier"]) > candidate["available_shares"]:
                    reasons.add("wheel_branch_capacity_exceeded")
            candidate["consumed_reservation"]["contracts"] = credit
            candidate["member_lot_ids"] = member_ids
            check = trade_attribution_capacity_check(config=config, fact=fact, facts=account_facts, wheel_read_model=capacity_model,
                observation=capacity_observation or {}, now_ms=now_ms, consumed_reservation=candidate["consumed_reservation"])
            reasons.update(check["reason_codes"])
            candidate["capacity_evidence_complete"] = check.get("evidence_complete") is True
            candidate["reason_codes"] = sorted(reasons)
            candidate["eligible"] = not reasons
    for proposals in candidates.values():
        for proposal in proposals:
            proposal.pop("capacity_reason_codes", None)
    demands: dict[str, int] = {}
    for fact in facts:
        if fact["status"] != "pending":
            continue
        for candidate in candidates[fact["lot_id"]]:
            if candidate["strategy"] == "wheel" and candidate["capacity_evidence_complete"]:
                key = candidate["candidate_id"]
                demands[key] = demands.get(key, 0) + fact["contracts"] * int(fact["multiplier"])
    portfolio = (capacity_observation or {}).get("portfolio") or {}
    snapshot = portfolio.get("position_snapshot_input") or {}
    from src.application.portfolio_context_service import cash_snapshot_evidence
    capacity_semantic = {
        "authority": {key: value for key, value in (portfolio.get("capacity_authority") or {}).items() if key != "source_observed_at"},
        "positions": sorted(({key: row.get(key) for key in ("instrument_ref", "position_side", "quantity")}
                             for row in snapshot.get("rows") or []), key=canonical_sha256),
        "scope": snapshot.get("scope"), "completeness": snapshot.get("completeness"), "errors": snapshot.get("errors"),
        # Re-observation clocks may change; freshness is rechecked before commit.
        "cash_evidence": {key: value for key, value in cash_snapshot_evidence(portfolio).items()
                          if key not in {"cash_source_observed_at", "capacity_identity_hash", "capacity_authority"}},
        "fx_rates": (portfolio.get("exchange_rates") or {}).get("rates"), "fx_status": portfolio.get("exchange_rate_status"),
    }
    results = []
    for fact in facts:
        proposals = sorted(candidates[fact["lot_id"]], key=lambda row: row["candidate_id"])
        for proposal in proposals:
            if (proposal["strategy"] == "wheel" and proposal["capacity_evidence_complete"]
                    and proposal["available_shares"] is not None
                    and demands.get(proposal["candidate_id"], 0) > proposal["competition_available_shares"]):
                proposal["reason_codes"] = sorted(set(proposal["reason_codes"]) | {"competing_fills_exceed_capacity"})
                proposal["eligible"] = False
        existing = {**fact, "candidate_id": "wheel:" + fact["wheel_branch_id"] if fact["wheel_branch_id"]
                    else "combo:" + fact["strategy_group_id"] if fact["strategy_group_id"] else None}
        if fact["execution_key"] in blocked_executions:
            existing["status"] = "conflict"
        complete = combo_evidence.get("complete") is True
        if "reads" in combo_evidence:
            lot_scope = combo_lots.get(fact["lot_id"], {})
            scoped_reads = [read for read in combo_evidence["reads"]
                            if (read.get("market"), read.get("market_date")) ==
                            (lot_scope.get("market"), lot_scope.get("market_date"))]
            complete = len(scoped_reads) == 1 and scoped_reads[0].get("complete") is True
        complete = complete and not fact["reason_codes"]
        resolution = resolve_trade_attribution(candidates=tuple(proposals), evidence_complete=complete,
            existing=existing, applicable=fact["contracts_open"] > 0)
        ref = fact["broker_account_ref"]
        enabled = any(row["broker"] == ref.get("broker_id") and row["physical_account_id"] == ref.get("external_account_id")
            and row["environment"] == ref.get("environment") and row["account"] == account and row["market"] == market
            and row["policy_version"] == ATTRIBUTION_POLICY_VERSION and row["effective_from_ms"] <= fact["event_time_ms"]
            for row in rows.get("attribution_policy_enablings") or [])
        capacity_evidence = capacity_semantic
        if not any(proposal["strategy"] == "wheel" for proposal in proposals):
            # Only position evidence can change an existing Combo membership decision.
            capacity_evidence = {key: capacity_semantic[key] for key in (
                "authority", "scope", "completeness", "errors")}
            capacity_evidence["positions"] = [row for row in capacity_semantic["positions"]
                if (row.get("instrument_ref") or {}).get("asset_type") == "option"]
            capacity_evidence["scope"] = {**(snapshot.get("scope") or {}),
                "asset_types": [asset for asset in (snapshot.get("scope") or {}).get("asset_types", [])
                                if asset == "option"]}
        semantic = {"fact_hash": fact["input_hash"], "candidates": proposals, "policy": wheel_config.get("policy_hash"),
                    "complete": complete, "enabled": enabled, "capacity": capacity_evidence,
                    "account_facts": sorted((item["open_event_id"], item["input_hash"]) for item in account_facts),
                    "combo_inferences": sorted(({key: pair.get(key) for key in (
                        "inference_id", "status", "input_snapshot_hash", "put_lot_snapshot", "call_lot_snapshot",
                        "proposal_expires_at_ms", "strategy_group_id")} for pair in rows.get("account_combo_inferences") or []),
                        key=lambda pair: pair["inference_id"]),
                    "wheel_evidence": sorted((event["event_id"], event["payload_hash"]) for event in wheel_events)}
        results.append({**fact, "status": resolution.status, "candidate_ids": list(resolution.candidate_ids),
            "candidates": proposals, "reason_codes": sorted(set(fact["reason_codes"]) | set(resolution.reason_codes)),
            "selected_candidate_id": resolution.selected_candidate_id if enabled else None, "rules_enabled": enabled,
            "input_hash": canonical_sha256(semantic), "evaluated_at_ms": now_ms, "evidence_complete": complete,
            "capacity_evidence": {
                "semantic_sha256": canonical_sha256(capacity_evidence),
                "authority": dict(portfolio.get("capacity_authority") or {}),
                "snapshot": {key: snapshot.get(key) for key in
                    ("snapshot_id", "source_id", "observed_at_utc", "completeness")},
            },
            "coverage": next((branch.get("coverage") for branch in branches if branch["wheel_branch_id"] == fact["wheel_branch_id"]), None),
            "direction": fact["contract_key"]["option_type"]})
    return {"rows": results, "wheel_model": capacity_model, "combo_evidence": dict(combo_evidence),
            "conflict_statuses": conflict_statuses}




def apply_trade_attribution(
    repo: Any, *, account: str, market: str, config: Mapping[str, Any], execution_key: str,
    candidate_id: str, expected_input_hash: str, request_id: str, actor: str,
    combo_evidence: Mapping[str, Any], capacity_observation: Mapping[str, Any], combo_mode: str,
    stop_event: Any = None, manual: bool = False, apply_changes: bool = True,
    wheel_branch_ids: tuple[str, ...] = (), conflict_event_ids: tuple[str, ...] = (),
    member_decisions: tuple[Mapping[str, Any], ...] = (),
    before_commit: Any = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One arbitration and patch path for preview, single and complete decisions."""

    if not all((execution_key, candidate_id, expected_input_hash, request_id, actor)):
        raise ValueError("attribution requires complete request identity")
    if (not isinstance(manual, bool) or any(not isinstance(value, str) or not value.strip()
                                           for value in conflict_event_ids)):
        raise ValueError("attribution decision mode or conflict identity is invalid")
    if conflict_event_ids and not manual:
        raise ValueError("conflict resolution requires a manual decision")
    requested = list(member_decisions) or [{"execution_key": execution_key, "candidate_id": candidate_id,
                                          "wheel_branch_ids": list(wheel_branch_ids)}]
    if any(not isinstance(item, Mapping) or set(item) - {"execution_key", "candidate_id", "wheel_branch_ids", "wheel_call_allocations"}
           or any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("execution_key", "candidate_id"))
           or not isinstance(item.get("wheel_branch_ids", []), (list, tuple))
           or any(not isinstance(value, str) or not value.strip() for value in item.get("wheel_branch_ids", []))
           for item in requested):
        raise ValueError("attribution member decision is invalid")
    requested = [{"execution_key": item["execution_key"], "candidate_id": item["candidate_id"],
                  "wheel_branch_ids": sorted(item.get("wheel_branch_ids") or []),
                  **({"wheel_call_allocations": [dict(stock_lot_id=stock, wheel_branch_id=branch, contracts=count)
                       for stock, branch, count in parse_wheel_call_allocations(item["wheel_call_allocations"])]}
                     if "wheel_call_allocations" in item else {})} for item in requested]
    primary = [item for item in requested if item["execution_key"] == execution_key]
    if len(primary) != 1 or primary[0]["candidate_id"] != candidate_id:
        raise ValueError("attribution primary member differs from the decision")
    if wheel_branch_ids and sorted(wheel_branch_ids) != primary[0]["wheel_branch_ids"]:
        raise ValueError("attribution primary allocation differs from the decision")
    if (len({item["execution_key"] for item in requested}) != len(requested)
            or len(set(conflict_event_ids)) != len(conflict_event_ids)):
        raise ValueError("attribution decision contains duplicate members or conflicts")
    request_content = {"account": account, "market": market, "actor": actor, "manual": manual,
        "members": sorted(requested, key=lambda item: item["execution_key"]),
        "conflict_event_ids": sorted(conflict_event_ids), "input_hash": expected_input_hash}
    if reference is not None:
        request_content["reference"] = dict(reference)

    def run(active: Any, conn: Any) -> dict[str, Any]:
        rows = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
        facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
        by_key = {row["execution_key"]: row for row in facts if row["execution_key"]}
        if execution_key not in by_key or len(by_key) != len([row for row in facts if row["execution_key"]]):
            raise ValueError("attribution execution is not unique")
        instant = int(time.time() * 1000)
        prior = read_trade_attribution_decision(rows, account=account, request_id=request_id,
            request_content=request_content, now_ms=instant)
        if prior is not None:
            return {**by_key[execution_key], **prior, "write_applied": False}
        if manual and any(item["candidate_id"].startswith("combo:") for item in requested):
            mode = combo_reconciliation_mode_for_account(config, account=account)
            if mode not in {"confirm", "auto"}:
                raise ValueError(f"Combo confirmation is disabled for account {account}: effective mode={mode}")
        if stop_event is not None and stop_event.is_set():
            raise ValueError("attribution cancelled")
        view = build_trade_attribution_view(rows, config=config, account=account, market=market, now_ms=instant,
            combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode,
            revalidate_expired=manual)
        current = next(row for row in view["rows"] if row["execution_key"] == execution_key)
        if reference is None and not conflict_event_ids and not member_decisions and current["status"] == "linked" and not by_key[execution_key]["reason_codes"]:
            fact = by_key[execution_key]
            existing_target = ("wheel:" + fact["wheel_branch_id"] if fact["wheel_branch_id"] else
                               "combo:" + fact["strategy_group_id"] if fact["strategy_group_id"] else None)
            if fact.get("wheel_call_allocations"):
                existing_branches = sorted(row["wheel_branch_id"] for row in fact["wheel_call_allocations"]
                                           for _ in range(row["contracts"]))
                existing_target = "wheel-multi:" + canonical_sha256(existing_branches)[:24]
                if not manual or sorted(wheel_branch_ids) != existing_branches:
                    raise ValueError("Wheel Call allocation conflicts with durable membership")
            if candidate_id != existing_target:
                raise ValueError("attribution target conflicts with durable membership")
            return {**fact, "write_applied": False}
        if current["input_hash"] != expected_input_hash:
            raise ValueError("attribution evidence changed; use trade_attribution_read prepare_confirmation=true input_hash to create a new preview")
        if reference is not None:
            if reference.get("kind") == "combo":
                matches = [item for item in current["candidates"] if item["candidate_id"] == candidate_id
                    and (item.get("inference") or {}).get("inference_id") == reference.get("inference_id")]
            elif reference.get("kind") == "wheel":
                model = view["wheel_model"]
                proposals = (model["linkage_candidates"] if reference.get("direction") == "call" else
                    project_wheel_linkage_candidates(model["wheel_branches"], rows["account_position_lots"], rows["account_wheel_events"]))
                matches = [item for item in proposals
                    if item.get("call_record_id", item.get("option_record_id")) == current["lot_id"]
                    and item.get("linkage_candidate_id") == reference.get("linkage_candidate_id")
                    and item.get("batch_generation_hash") == reference.get("batch_generation_hash")
                    and "wheel:" + str(item.get("wheel_branch_id") or item.get("stock_lot_id")) == candidate_id]
            else:
                raise ValueError("unknown attribution reference")
            if len(matches) != 1:
                raise ValueError("attribution reference is stale or unavailable")
        conflicts = [row for row in rows["account_wheel_events"] if row["event_id"] in conflict_event_ids]
        if (len(conflicts) != len(conflict_event_ids) or any(row["event_type"] != "wheel_attribution_conflict"
                or row["account"] != account or view["conflict_statuses"].get(row["event_id"], {}).get("resolved") for row in conflicts)):
            raise ValueError("attribution conflict target is unavailable")
        generations = {branch["wheel_branch_id"]: branch["batch_generation_hash"] for branch in view["wheel_model"]["wheel_branches"]}
        if any(row["wheel_branch_id"] not in generations for row in conflicts):
            raise ValueError("attribution conflict branch generation is unavailable")
        if conflicts:
            # Exclude only selected conflict records, after binding the real snapshot.
            selected = set(conflict_event_ids)
            evaluation_rows = {**rows, "account_wheel_events": [row for row in rows["account_wheel_events"]
                if row["event_id"] not in selected and (row.get("payload") or {}).get("conflict_event_id") not in selected]}
            view = build_trade_attribution_view(evaluation_rows, config=config, account=account, market=market,
                now_ms=instant, combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode,
                revalidate_expired=manual)
        else:
            evaluation_rows = rows
        evaluated = {row["execution_key"]: row for row in view["rows"]}
        decisions = {item["execution_key"]: dict(item) for item in requested}
        if not conflicts and len(decisions) == 1:
            chosen = next((item for item in evaluated[execution_key]["candidates"] if item["candidate_id"] == candidate_id), None)
            if chosen:
                decisions = {row["execution_key"]: {"execution_key": row["execution_key"], "candidate_id": candidate_id}
                             for row in view["rows"] if row["lot_id"] in chosen["member_lot_ids"]}
                if wheel_branch_ids:
                    decisions[execution_key]["wheel_branch_ids"] = list(wheel_branch_ids)
        required = {key for row in conflicts for key in row["payload"]["execution_keys"]} if conflicts else set(decisions)
        expanded = set()
        while required - expanded:
            key = next(iter(required - expanded))
            expanded.add(key)
            fact = by_key.get(key)
            if fact is None or key not in decisions:
                continue
            item = decisions[key]
            if fact["strategy_group_id"]:
                required.update(row["execution_key"] for row in facts if row["strategy_group_id"] == fact["strategy_group_id"])
            chosen = next((value for value in evaluated[key]["candidates"] if value["candidate_id"] == item["candidate_id"]), None)
            if chosen and chosen["strategy"] == "combo_yield":
                required.update(row["execution_key"] for row in facts if row["lot_id"] in chosen["member_lot_ids"])
        if required != decisions.keys():
            raise ValueError("attribution requires an explicit complete member decision: " + str(sorted(required)))
        plans, intent_events = [], []
        branches = {row["wheel_branch_id"]: row for row in view["wheel_model"]["wheel_branches"]}
        for key, item in sorted(decisions.items()):
            fact, proposed = by_key[key], evaluated[key]
            target = item["candidate_id"]
            if fact["reason_codes"] or not proposed["evidence_complete"]:
                raise ValueError("attribution member identity or dependencies changed")
            if not conflicts:
                assert_trade_attribution_unclaimed(rows["trade_events"], [fact["lot_id"]])
            fields = active.get_position_lot_fields(fact["lot_id"], conn=conn)
            choice = next((value for value in proposed["candidates"] if value["candidate_id"] == target), None)
            allocations = item.get("wheel_branch_ids") or []
            if target == "ordinary":
                if not manual:
                    raise ValueError("ordinary attribution requires a manual decision")
                kwargs = {"strategy": resolve_option_strategy_membership(fact["contract_key"], fact["position_side"], {}).strategy}
                action = "ordinary"
            elif allocations:
                if not manual or target != "wheel-multi:" + canonical_sha256(sorted(allocations))[:24]:
                    raise ValueError("Wheel Call allocation identity is invalid")
                multi = _multi_wheel_call_attribution(view=view,
                    current={**proposed, "status": "pending", "reason_codes": []}, branch_ids=tuple(allocations),
                    decided_execution_keys=set(decisions))
                if "wheel_call_allocations" in item and parse_wheel_call_allocations(item["wheel_call_allocations"]) != parse_wheel_call_allocations(multi["wheel_call_allocations"]):
                    raise ValueError("Wheel allocation stock identity differs from the decision")
                kwargs = {"strategy": "wheel", "leg_role": "wheel_call", "wheel_call_allocations": multi["wheel_call_allocations"]}
                action = "wheel"
            else:
                allowed = {"combo_confirmation_required", "combo_not_unique_delivered_pair"} if manual else set()
                if conflicts:
                    allowed |= {"combo_member_unavailable", "wheel_branch_capacity_exceeded", "competing_fills_exceed_capacity"}
                if (choice is None or set(choice["reason_codes"]) - allowed
                        or not manual and proposed["selected_candidate_id"] != target):
                    raise ValueError("attribution candidate is not admissible")
                action = "wheel" if choice["strategy"] == "wheel" else "combo"
                if action == "wheel":
                    branch = branches[choice["wheel_branch_id"]]
                    direction = fact["contract_key"]["option_type"]
                    kwargs = {"strategy": "wheel", "leg_role": "wheel_" + direction,
                        "source_wheel_branch_id": branch["wheel_branch_id"],
                        "source_lot_id": branch.get("stock_lot_id") if direction == "call" else None}
                    fill = next(row for row in rows["trade_events"] if row["event_id"] == fact["open_event_id"])
                    intent = resolve_wheel_fill_intent(branch, fill, [*evaluation_rows["account_wheel_events"], *intent_events],
                        now_ms=instant, known_trade_event_ids={row["event_id"] for row in rows["trade_events"]})
                    if intent["reason_codes"]:
                        raise ValueError("Wheel intent is unavailable")
                    if intent["intent"]:
                        planner = plan_wheel_call_intent_consume if direction == "call" else plan_wheel_put_intent_consume
                        payload = intent["intent"].get("payload") or {}
                        intent_events.append(planner(branch, intent["intent"], fill,
                            {"account": account, "symbol": branch["symbol"], "status": "available",
                             "shares_available_for_cover": fact["contracts"] * fact["multiplier"],
                             **{field: payload.get(field) for field in ("capacity_identity_hash", "cash_reservation_currency", "cash_reservation_amount")}},
                            recorded_at_ms=instant))
                else:
                    pair = choice["inference"]
                    kwargs = {"strategy": "combo_yield", "strategy_group_id": pair["strategy_group_id"],
                        "leg_role": "funding_put" if fact["lot_id"] == pair["put_record_id"] else "participation_call"}
            patch = build_open_adjustment_patch_contract(fields, as_of_ms=instant, **kwargs).to_dict()
            plans.append({"fact": fact, "patch": patch, "action": action, "candidate_id": target,
                "candidate_ids": proposed["candidate_ids"], "origin": "intent" if choice and choice.get("intent_id") else "rule",
                "inference": choice.get("inference") if choice else None})
        if before_commit is not None:
            before_commit()
        if not apply_changes:
            conn.execute("SAVEPOINT attribution_preview")
        result = write_trade_attribution_decision(active, conn=conn, account=account, request_id=request_id,
            actor=actor, input_hash=expected_input_hash, plans=plans, conflicts=conflicts,
            branch_generations=generations, now_ms=instant, manual=manual, wheel_events=intent_events,
            request_content=request_content)
        after_rows = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
        after_model = build_wheel_read_model_from_rows(after_rows, account=account, as_of_ms=instant)
        after_facts = trade_attribution_facts_from_events(after_rows["trade_events"], account=account)
        targets = {plan["patch"].get("source_wheel_branch_id") for plan in plans}
        targets.update(value["wheel_branch_id"] for plan in plans for value in plan["patch"].get("wheel_call_allocations") or [])
        for branch in after_model["wheel_branches"]:
            if branch["wheel_branch_id"] in targets and branch["integrity_status"] != "trusted":
                raise ValueError("attribution final Wheel capacity or membership is invalid")
            if branch["wheel_branch_id"] in targets:
                unit = "shares" if branch["direction"] == "call" else "contracts"
                available = branch.get("shares_remaining" if unit == "shares" else "remaining_contracts")
                committed = branch.get("active_option_committed_" + unit)
                reserved = branch.get("active_intent_reserved_" + unit)
                if available is None or committed is None or committed + (reserved or 0) > available:
                    raise ValueError("attribution final Wheel capacity exceeded")
        for plan in plans:
            if plan["action"] != "ordinary" and plan["fact"]["position_side"] == "short":
                check = trade_attribution_capacity_check(config=config, fact=plan["fact"], facts=after_facts, wheel_read_model=after_model,
                    observation=capacity_observation, now_ms=int(time.time() * 1000),
                    check_account_capacity=plan["action"] == "wheel")
                if check["status"] != "available":
                    raise ValueError("attribution capacity changed before commit")
        if stop_event is not None and stop_event.is_set():
            raise ValueError("attribution cancelled before commit")
        if before_commit is not None:
            before_commit()
        after = next(row for row in after_facts
                     if row["execution_key"] == execution_key)
        if not apply_changes:
            conn.execute("ROLLBACK TO attribution_preview")
            conn.execute("RELEASE attribution_preview")
            return {**current, **result, "write_applied": False, "members": plans,
                "request_content": request_content,
                "conflict_event_ids": list(conflict_event_ids), "branch_generations": generations,
                **({"wheel_call_allocations": plans[0]["patch"]["wheel_call_allocations"]}
                   if plans[0]["patch"].get("wheel_call_allocations") else {})}
        return {**after, **result}

    return with_sqlite_repo_transaction(repo, run, require_projection_publication=True)


def trade_attribution_capacity_check(
    *, fact: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
    wheel_read_model: Mapping[str, Any], observation: Mapping[str, Any], now_ms: int,
    config: Mapping[str, Any],
    consumed_reservation: Mapping[str, Any] | None = None,
    check_account_capacity: bool = True,
) -> dict[str, Any]:
    """Verify booked positions; Wheel additionally requires account coverage."""
    from datetime import datetime, timezone
    from domain.domain.position_snapshot import position_snapshot_scope_errors
    from domain.domain.symbol_identity import symbol_market

    reasons: set[str] = set()
    portfolio = observation.get("portfolio") or {}
    snapshot = portfolio.get("position_snapshot_input") or {}
    ref = fact.get("broker_account_ref") or {}
    market = str(symbol_market(fact["contract_key"]["underlying_symbol"]) or "").lower()
    authority = portfolio.get("capacity_authority") or {}
    if (observation.get("error") or authority.get("status") != "available"
            or authority.get("logical_account") != fact["account"]
            or authority.get("futu_account_id") != ref.get("external_account_id")
            or authority.get("trd_env") != ref.get("environment") or authority.get("market") != market):
        reasons.add("capacity_authority_unavailable")
    pooled_cash = fact["contract_key"]["option_type"] == "put"
    markets = {"us", "hk"} if pooled_cash else {market}
    for required_market in markets:
        for asset in (("stock", "option") if check_account_capacity else ("option",)):
            reasons.update(position_snapshot_scope_errors(snapshot, account_label=fact["account"],
                environment=str(ref.get("environment") or ""), market=required_market, asset_type=asset,
                external_account_id=ref.get("external_account_id"),
                now_utc=datetime.fromtimestamp(now_ms / 1000, timezone.utc), max_age_seconds=60))
    if reasons:
        return {"status": "unavailable", "reason_codes": sorted(reasons), "evidence_complete": False}

    cash_usable = False
    if pooled_cash and check_account_capacity:
        try:
            cash_snapshot = evaluate_account_cash_snapshot(
                portfolio, config=config, account=fact["account"],
                evaluated_at=datetime.fromtimestamp(now_ms / 1000, timezone.utc),
            )
            cash_usable = cash_snapshot_is_usable({"cash_snapshot": cash_snapshot})
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # Preserve reconciliation/reservation failures ahead of cash errors.
            cash_usable = None
    return risk_capacity.evaluate_booked_option_capacity(
        fact=fact, facts=facts, snapshot=snapshot, wheel_read_model=wheel_read_model,
        cash_usable=cash_usable, cash_by_currency=portfolio.get("cash_by_currency"),
        convert_currency=capacity_currency_converter(portfolio.get("exchange_rates") or {}),
        fx_status=portfolio.get("exchange_rate_status"),
        consumed_reservation=consumed_reservation, check_account_capacity=check_account_capacity,
    )


def record_trade_attribution_conflicts(
    repo: Any, *, account: str, market: str, config: Mapping[str, Any],
    execution_key: str, expected_input_hash: str, combo_evidence: Mapping[str, Any],
    capacity_observation: Mapping[str, Any], combo_mode: str, stop_event: Any = None,
) -> list[str]:
    """Revalidate and record all affected conflicts in one ledger transaction.

    The application supplies authorized, frozen external observations. Ledger
    facts, member selection, idempotent writes and final expiry/cancellation
    checks stay under this owner; callers never receive a connection callback.
    """
    def record_conflicts(active: Any, conn: Any) -> list[str]:
        fresh = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
        check = build_trade_attribution_view(fresh, config=config, account=account, market=market,
            now_ms=int(time.time() * 1000), combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode)
        fact = next(row for row in check["rows"] if row["execution_key"] == execution_key)
        if (fact["input_hash"] != expected_input_hash or fact["status"] != "conflict"
                or not fact["rules_enabled"] or "unresolved_attribution_conflict" in fact["reason_codes"]):
            raise ValueError("conflict evidence changed")
        ids = list(fact["candidate_ids"])
        if fact["wheel_branch_id"]:
            ids.append("wheel:" + fact["wheel_branch_id"])
        if fact["strategy_group_id"]:
            ids.append("combo:" + fact["strategy_group_id"])
        claimed_branches = {allocation["wheel_branch_id"] for allocation in fact.get("wheel_call_allocations") or []}
        if fact["wheel_branch_id"]:
            claimed_branches.add(fact["wheel_branch_id"])
        affected_branches = (claimed_branches if "late_capacity_conflict" in fact["reason_codes"] else
            {item["wheel_branch_id"] for item in fact["candidates"] if item["strategy"] == "wheel"} | claimed_branches)
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("attribution conflict recording cancelled")
        events = [record_trade_attribution_conflict(active, account=account, execution_key=fact["execution_key"],
            branch=branch, candidate_ids=ids, input_hash=fact["input_hash"], now_ms=int(time.time() * 1000), conn=conn,
            execution_keys=trade_attribution_conflict_execution_keys(check["rows"], branch["wheel_branch_id"], fact,
                account_facts=trade_attribution_facts_from_events(fresh["trade_events"], account=account)),
            diagnostic={"reason_codes": fact["reason_codes"], "evaluated_at_ms": fact["evaluated_at_ms"],
                "candidate_reason_codes": {item["candidate_id"]: item["reason_codes"]
                    for item in fact["candidates"]}, "capacity_evidence": fact["capacity_evidence"]})
            for branch in check["wheel_model"]["wheel_branches"] if branch["wheel_branch_id"] in affected_branches]
        # Validate the source snapshot again, excluding our provisional
        # conflict events; cancellation/expiry rolls back the whole write.
        final_view = build_trade_attribution_view(fresh, config=config, account=account, market=market,
            now_ms=int(time.time() * 1000), combo_evidence=combo_evidence,
            capacity_observation=capacity_observation, combo_mode=combo_mode)
        final_fact = next(row for row in final_view["rows"] if row["execution_key"] == fact["execution_key"])
        if final_fact["input_hash"] != fact["input_hash"] or final_fact["status"] != "conflict":
            raise ValueError("conflict evidence changed before commit")
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("attribution conflict recording cancelled")
        return events

    return with_sqlite_repo_transaction(repo, record_conflicts)
