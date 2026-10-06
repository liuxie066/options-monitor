from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any, Callable, Iterator, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import effective_contracts_open
from domain.domain.trade_execution import execution_identity_from_input, legacy_open_execution_input_from_event
from domain.domain.strategy_membership import strategy_metadata_has_owner, POSITION_LOT_STRATEGY_PATCH_FIELDS, validate_attribution_decision
from domain.domain.ledger.events import lot_id_for_open_event
from domain.domain.wheel import lot_strategy_metadata_from_trade_events, effective_wheel_events, build_wheel_event
from .event_codec import valid_void_target_event_id
from .publisher import project_stored_trade_events_to_position_lots
from .repository import require_option_positions_event_read_repo
from .position_projection_migration import _store_identity, _read_only_connection, _repository
from .position_projection_runtime import run_position_projection_in_transaction
from .current_decision_projection import capture_trade_event_decision_projection_fence
from .writer import _finish_trade_event_decision_projection
from .read_only_evidence import open_trade_reconciliation_evidence_repo
from .repository_wheel_policy import effective_wheel_window
from .combo_membership import resolve_combo_group_membership, publish_combo_pair_identity


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
        repo.upsert_combo_pair_inference(pair, conn=conn)
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
