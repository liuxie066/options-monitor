from __future__ import annotations

from typing import Any, Mapping
import json
import time
from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, resolve_futu_account_ids
from src.application.ledger.api import (
    ledger_resource_identity, open_trade_reconciliation_evidence_repo,
    read_trade_attribution_facts, resolve_ledger_store, resolve_position_data_config_path,
    ledger_store_write_guard, open_wheel_activation_repository, enable_trade_attribution_policy,
    preview_trade_attribution_migration, apply_trade_attribution_migration,
    read_trade_attribution_snapshot, trade_attribution_facts_from_events,
    combo_attribution_candidates_from_rows, ATTRIBUTION_POLICY_VERSION,
    with_sqlite_repo_transaction, adopt_post_trade_combo_pair,
    read_trade_attribution_policy, record_trade_attribution_conflict,
)
from src.application.futu_quote_routing import runtime_config_market
from src.application.write_contract import write_control
from src.application.trades.account_mapping import combo_reconciliation_mode_for_account
from domain.domain.strategy_membership import resolve_trade_attribution
from domain.domain.combo_reconciliation import delivered_combo_exposures_for_lot
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel.intents import resolve_wheel_fill_intent
from domain.domain.wheel import effective_wheel_events
from domain.domain.ledger.position_fields import effective_contracts_open
from src.application.wheel.config import resolve_wheel_config, evaluate_wheel_activation_readiness
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from src.application.wheel.capacity import trade_attribution_capacity_check
from src.application.daily_decision_brief_repository import read_combo_candidate_exposures
from src.application.wheel.workflows import confirm_wheel_call_linkage, confirm_wheel_linkage


def trade_attribution_enabled_for_execution(repo: Any, *, execution: Mapping[str, Any], account: str,
                                            market: str, event_time_ms: int) -> bool:
    if not getattr(repo, "db_path", None):
        return False
    ref = execution.get("broker_account_ref") or {}
    if not all(ref.get(key) for key in ("broker_id", "external_account_id", "environment")):
        return False
    policy = read_trade_attribution_policy(repo, scope={"broker": ref["broker_id"], "physical_account_id": ref["external_account_id"],
        "environment": ref["environment"], "account": account, "market": market.lower()})
    return bool(policy and int(event_time_ms) >= policy["effective_from_ms"])


def attribution_result_payload(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {key: fact.get(key) for key in ("schema_version", "execution_key", "open_event_id", "lot_id", "account",
        "status", "strategy", "wheel_branch_id", "strategy_group_id", "origin", "reason_codes", "candidate_ids",
        "input_hash", "policy_version", "evaluated_at_ms", "ledger_event_ids", "coverage", "direction")}


def read_attribution_combo_evidence(rows: Mapping[str, Any], *, account: str, runtime_root: Path,
                                    now_ms: int) -> dict[str, Any]:
    preview = combo_attribution_candidates_from_rows(rows, account=account, runtime_environment="",
        exposures=[], effective_now_ms=now_ms, include_claimed=True)
    scopes = {(item["market"], item["market_date"]) for item in preview["lot_facts"]}
    exposures, reads = {}, []
    for market, market_date in sorted(scopes):
        result = read_combo_candidate_exposures(base=runtime_root, account=account, market=market,
                                                market_trading_date=market_date)
        reads.append({"market": market, "market_date": market_date, "complete": (
            result.get("available") is True and result.get("complete") is True
            and result.get("delivery_available") is True and result.get("reason") in {None, "ok"}
            and not result.get("invalid_revisions"))})
        for exposure in result.get("exposures") or []:
            key = exposure.get("candidate_exposure_id")
            if key in exposures and exposures[key] != exposure:
                reads[-1]["complete"] = False
            if key:
                exposures[key] = dict(exposure)
    return {"complete": all(item["complete"] for item in reads), "reads": reads,
            "exposures": [exposures[key] for key in sorted(exposures)]}


def _branch_account_ref(branch: Mapping[str, Any], rows: Mapping[str, Any]) -> dict[str, Any] | None:
    events = {row["event_id"]: row for row in rows["trade_events"]}
    opens = {row.get("lot_id"): row for row in events.values() if row.get("event_type") == "open"}
    starts = {row["event_id"]: row for row in rows["account_wheel_events"]}
    start = starts.get(branch.get("start_event_id"), {})
    source = events.get(branch.get("source_assignment_event_id") or start.get("source_trade_event_id"), {})
    # Settlement events may inherit physical identity from their exact source lot.
    references = []
    for event in (source, opens.get(source.get("target_lot_id"), {})):
        ref = ((event.get("raw_payload") or {}).get("execution_input") or {}).get("broker_account_ref") or {}
        if all(ref.get(key) for key in ("broker_id", "external_account_id", "environment")):
            references.append({key: ref[key] for key in ("broker_id", "external_account_id", "environment")})
    return references[0] if references and all(ref == references[0] for ref in references) else None


def build_trade_attribution_view(
    rows: Mapping[str, Any], *, config: Mapping[str, Any], account: str, market: str, now_ms: int,
    combo_evidence: Mapping[str, Any], capacity_observation: Mapping[str, Any] | None = None,
    combo_mode: str = "confirm",
) -> dict[str, Any]:
    """Collect every competitor before selecting any execution; this function never writes."""
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
    readiness = evaluate_wheel_activation_readiness(wheel_config.get("activation_descriptor"), rows.get("wheel_activation_window"))
    model = build_wheel_read_model_from_rows(rows, account=account, as_of_ms=now_ms, market=market,
                                            monitoring_readiness=readiness)
    capacity_model = build_wheel_read_model_from_rows(rows, account=account, as_of_ms=now_ms)
    branches = model["wheel_branches"]
    wheel_events, wheel_errors = effective_wheel_events(rows["account_wheel_events"], as_of_ms=now_ms, trade_events=rows["trade_events"],
        known_trade_event_ids={event["event_id"] for event in rows["trade_events"]})
    blocked_executions = {execution for event in wheel_events if event["event_type"] == "wheel_attribution_conflict"
        and "strategy_attribution_conflict" in wheel_errors.get((account, event["wheel_branch_id"]), set())
        for execution in event["payload"]["execution_keys"]}
    exposures = list(combo_evidence.get("exposures") or [])
    combos = combo_attribution_candidates_from_rows(rows, account=account, runtime_environment="",
        exposures=exposures, effective_now_ms=now_ms, include_claimed=True)
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
        if combo_mode != "auto":
            reasons.append("combo_confirmation_required")
        if (pair["evidence_grade"] != "exact_delivered_candidate" or pair.get("alternative_inference_ids")
                or pair["status"] != "proposal_ready"):
            reasons.append("combo_not_unique_delivered_pair")
        if any(lot not in by_lot or by_lot[lot]["reason_codes"] or by_lot[lot].get("origin") == "manual" for lot in members):
            reasons.append("combo_member_unavailable")
        if members[0] in by_lot:
            reasons.extend(trade_attribution_capacity_check(fact=by_lot[members[0]], facts=account_facts,
                wheel_read_model=capacity_model, observation=capacity_observation or {}, now_ms=now_ms)["reason_codes"])
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
                    "eligible": False, "reason_codes": ["combo_counterpart_missing_or_asymmetric"], "member_lot_ids": [lot]})
        if fact["position_side"] != "short" or fact["contracts_open"] <= 0:
            continue
        contract = fact["contract_key"]
        # ponytail: reuse the historical projector per fill; cache by event time if account history becomes large.
        historical = build_wheel_read_model_from_rows(rows, account=account, as_of_ms=fact["event_time_ms"], market=market)
        history = {row["wheel_branch_id"]: row for row in historical["wheel_branches"]}
        for branch in branches:
            if (branch["symbol"] != contract["underlying_symbol"] or branch["direction"] != contract["option_type"]
                    or branch["lifecycle_status"] != "active"):
                continue
            branch_id = branch["wheel_branch_id"]
            reasons = []
            prior = history.get(branch_id)
            if prior is None or prior["lifecycle_status"] != "active":
                continue
            if prior["integrity_status"] != "trusted":
                reasons.append("wheel_branch_not_trusted_at_fill")
            if branch["integrity_status"] != "trusted" or not readiness["ready"]:
                reasons.append("wheel_branch_not_ready")
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
            check = trade_attribution_capacity_check(fact=fact, facts=account_facts, wheel_read_model=capacity_model,
                observation=capacity_observation or {}, now_ms=now_ms, consumed_reservation=consumed_reservation)
            reasons.extend(check["reason_codes"])
            candidates[lot].append({"candidate_id": "wheel:" + branch_id, "strategy": "wheel", "eligible": not reasons,
                "reason_codes": sorted(set(reasons)), "wheel_branch_id": branch_id, "member_lot_ids": [lot],
                "branch_generation_hash": branch["batch_generation_hash"], "available_shares": available,
                "competition_available_shares": None if available is None else available - (fact["contracts_open"] * multiplier if fact["wheel_branch_id"] == branch_id else 0),
                "intent_id": intent["intent_id"] if intent else None, "consumed_reservation": consumed_reservation,
                "intent_remaining_contracts": intent["remaining_contracts"] if intent else None,
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
            reasons.update(trade_attribution_capacity_check(fact=fact, facts=account_facts, wheel_read_model=capacity_model,
                observation=capacity_observation or {}, now_ms=now_ms, consumed_reservation=candidate["consumed_reservation"])["reason_codes"])
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
            if candidate["strategy"] == "wheel":
                key = candidate["candidate_id"]
                demands[key] = demands.get(key, 0) + fact["contracts"] * int(fact["multiplier"])
    portfolio = (capacity_observation or {}).get("portfolio") or {}
    snapshot = portfolio.get("position_snapshot_input") or {}
    capacity_semantic = {
        "authority": {key: value for key, value in (portfolio.get("capacity_authority") or {}).items() if key != "source_observed_at"},
        "positions": sorted(({key: row.get(key) for key in ("instrument_ref", "position_side", "quantity")}
                             for row in snapshot.get("rows") or []), key=canonical_sha256),
        "scope": snapshot.get("scope"), "completeness": snapshot.get("completeness"), "errors": snapshot.get("errors"),
        "cash": portfolio.get("cash_by_currency"), "cash_reliable": portfolio.get("cash_balance_reliable"),
        "fx_rates": (portfolio.get("exchange_rates") or {}).get("rates"), "fx_status": portfolio.get("exchange_rate_status"),
    }
    results = []
    for fact in facts:
        proposals = sorted(candidates[fact["lot_id"]], key=lambda row: row["candidate_id"])
        for proposal in proposals:
            if (proposal["strategy"] == "wheel" and proposal["available_shares"] is not None
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
        semantic = {"fact_hash": fact["input_hash"], "candidates": proposals, "policy": wheel_config.get("policy_hash"),
                    "complete": complete, "enabled": enabled, "capacity": capacity_semantic,
                    "account_facts": sorted((item["open_event_id"], item["input_hash"]) for item in account_facts),
                    "wheel_evidence": sorted((event["event_id"], event["payload_hash"]) for event in wheel_events)}
        results.append({**fact, "status": resolution.status, "candidate_ids": list(resolution.candidate_ids),
            "candidates": proposals, "reason_codes": sorted(set(fact["reason_codes"]) | set(resolution.reason_codes)),
            "selected_candidate_id": resolution.selected_candidate_id if enabled else None, "rules_enabled": enabled,
            "input_hash": canonical_sha256(semantic), "evaluated_at_ms": now_ms, "evidence_complete": complete,
            "coverage": next((branch.get("coverage") for branch in branches if branch["wheel_branch_id"] == fact["wheel_branch_id"]), None),
            "direction": fact["contract_key"]["option_type"]})
    return {"rows": results, "wheel_model": capacity_model, "combo_evidence": dict(combo_evidence)}


def apply_trade_attribution(
    repo: Any, *, account: str, market: str, config: Mapping[str, Any], execution_key: str,
    candidate_id: str, expected_input_hash: str, request_id: str, actor: str,
    combo_evidence: Mapping[str, Any], capacity_observation: Mapping[str, Any], combo_mode: str,
    stop_event: Any = None, manual: bool = False, apply_changes: bool = True,
) -> dict[str, Any]:
    """Re-arbitrate under the ledger transaction, then reuse the existing narrow writers."""
    if not all((execution_key, candidate_id, expected_input_hash, request_id, actor)):
        raise ValueError("attribution requires complete request identity")

    def run(active: Any, conn: Any) -> dict[str, Any]:
        rows = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
        existing = [row for row in trade_attribution_facts_from_events(rows["trade_events"], account=account)
                    if row["execution_key"] == execution_key]
        if len(existing) != 1:
            raise ValueError("attribution execution is not unique")
        fact = existing[0]
        existing_target = ("wheel:" + fact["wheel_branch_id"] if fact["wheel_branch_id"] else
                           "combo:" + fact["strategy_group_id"] if fact["strategy_group_id"] else None)
        if existing_target:
            if candidate_id != existing_target:
                raise ValueError("attribution target conflicts with durable membership")
            return {**fact, "write_applied": False}
        if fact["status"] == "ordinary" and fact["origin"] == "manual":
            raise ValueError("manual ordinary decision cannot be overwritten")
        if stop_event is not None and stop_event.is_set():
            raise ValueError("attribution cancelled")
        instant = int(time.time() * 1000)
        view = build_trade_attribution_view(rows, config=config, account=account, market=market, now_ms=instant,
            combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode)
        current = next(row for row in view["rows"] if row["execution_key"] == execution_key)
        if current["input_hash"] != expected_input_hash:
            raise ValueError("attribution evidence changed; create a new preview")
        chosen = next((row for row in current["candidates"] if row["candidate_id"] == candidate_id), None)
        if chosen is None or not current["evidence_complete"]:
            raise ValueError("attribution candidate evidence is incomplete")
        allowed_manual_reasons = {"combo_confirmation_required", "combo_not_unique_delivered_pair"}
        if (not manual and current["selected_candidate_id"] != candidate_id
                or set(chosen["reason_codes"]) - (allowed_manual_reasons if manual else set())):
            raise ValueError("attribution candidate is not admissible")
        members = [row for row in view["rows"] if row["lot_id"] in chosen["member_lot_ids"]]
        if len(members) != len(chosen["member_lot_ids"]) or any(
                row["reason_codes"] and not row["ordinary_previewable"] for row in members):
            raise ValueError("attribution member identity or dependencies changed")
        if not manual and any(row["selected_candidate_id"] != candidate_id for row in members):
            raise ValueError("attribution members have competing strategies")
        if not apply_changes:
            return {**current, "write_applied": False}
        attribution_metadata = {"attribution_origin": "manual" if manual else "intent" if chosen.get("intent_id") else "rule",
            "attribution_request_id": request_id, "attribution_policy_version": ATTRIBUTION_POLICY_VERSION,
            "attribution_candidate_id": candidate_id, "actor": actor,
            "attribution_candidate_ids": current["candidate_ids"]}
        if chosen["strategy"] == "wheel":
            for member in sorted(members, key=lambda row: (row["event_time_ms"], row["open_event_id"])):
                member_rows = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
                member_model = build_wheel_read_model_from_rows(member_rows, account=account, as_of_ms=instant, market=market)
                direction = member["contract_key"]["option_type"]
                linkage = next((row for row in member_model["linkage_candidates"]
                    if row["option_record_id"] == member["lot_id"] and row["wheel_branch_id"] == chosen["wheel_branch_id"]), None)
                if linkage is None:
                    raise ValueError("Wheel linkage no longer admissible")
                capacity = {"account": account, "symbol": member["contract_key"]["underlying_symbol"],
                            "status": "available", "capacity_identity_hash": current["input_hash"]}
                common = dict(account=account, linkage_candidate_id=linkage["linkage_candidate_id"],
                    expected_input_hash=linkage["input_snapshot_hash"], expected_batch_generation_hash=linkage["batch_generation_hash"],
                    request_id=request_id + ":" + member["open_event_id"], actor=actor, market=market, apply_changes=True, as_of_ms=instant, conn=conn,
                    attribution_metadata=attribution_metadata)
                if direction == "call":
                    confirm_wheel_call_linkage(active, call_lot_id=member["lot_id"], lot_id=linkage["stock_lot_id"],
                                               coverage_fact=capacity, **common)
                else:
                    confirm_wheel_linkage(active, option_lot_id=member["lot_id"], wheel_branch_id=chosen["wheel_branch_id"],
                                          direction=direction, capacity_fact=capacity, **common)
        else:
            pair = chosen["inference"]
            active.upsert_combo_pair_inference(pair, conn=conn)
            adopt_post_trade_combo_pair(repo=active, inference_id=pair["inference_id"], expected_input_hash=pair["input_snapshot_hash"],
                actor=actor, apply_changes=True, effective_now_ms=instant, require_unique_auto_match=not manual,
                exposures=combo_evidence.get("exposures") or [], conn=conn, attribution_metadata=attribution_metadata)
        # The target was already included in occupancy before the adjustment. Validate
        # freshness again immediately before the enclosing transaction can commit.
        capacity_facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
        for member in members:
            if member["position_side"] != "short":
                continue
            check = trade_attribution_capacity_check(fact=member, facts=capacity_facts, wheel_read_model=view["wheel_model"],
                observation=capacity_observation, now_ms=int(time.time() * 1000), consumed_reservation=chosen.get("consumed_reservation"))
            if check["status"] != "available":
                raise ValueError("attribution capacity changed before commit")
        if stop_event is not None and stop_event.is_set():
            raise ValueError("attribution cancelled before commit")
        after = trade_attribution_facts_from_events(active.list_trade_events(conn=conn), account=account)
        result = next(row for row in after if row["execution_key"] == execution_key)
        target = "wheel:" + result["wheel_branch_id"] if result["wheel_branch_id"] else "combo:" + str(result["strategy_group_id"])
        if result["status"] != "linked" or target != candidate_id:
            raise ValueError("attribution durable readback failed")
        return {**result, "write_applied": True}

    return with_sqlite_repo_transaction(repo, run, require_projection_publication=True)


def reconcile_trade_attribution_account(
    repo: Any, *, config: Mapping[str, Any], account: str, market: str, runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursor: str = "", stop_event: Any = None,
) -> dict[str, Any]:
    from src.application.wheel.capacity import observe_trade_attribution_capacity
    from src.application.trades.inbox import cache_trade_attribution_result

    rows = read_trade_attribution_snapshot(repo, account=account, market=market)
    if not rows["attribution_policy_enablings"]:
        return {"status": "disabled", "checked": 0, "next_cursor": ""}
    facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
    selected = sorted((row for row in facts if row["execution_key"] and row["execution_key"] > cursor
        and str(symbol_market(row["contract_key"]["underlying_symbol"]) or "").lower() == market),
        key=lambda row: row["execution_key"])[:100]
    if not selected or stop_event is not None and stop_event.is_set():
        return {"status": "idle", "checked": 0, "next_cursor": ""}
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root, now_ms=int(time.time() * 1000))
    observation = observe_trade_attribution_capacity(config=dict(config), account=account, stop_event=stop_event)
    result = {"checked": 0, "linked": 0, "conflicts": 0, "cache_updates": 0, "errors": [], "next_cursor": cursor}
    for selected_fact in selected:
        if stop_event is not None and stop_event.is_set():
            break
        # Reuse one provider observation, but refresh all local competitors after each transaction.
        rows = read_trade_attribution_snapshot(repo, account=account, market=market)
        view = build_trade_attribution_view(rows, config=config, account=account, market=market, now_ms=int(time.time() * 1000),
            combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode)
        current = next((row for row in view["rows"] if row["execution_key"] == selected_fact["execution_key"]), None)
        if current is None:
            continue
        try:
            if current["selected_candidate_id"]:
                request_id = "trade-attribution:" + canonical_sha256({"policy": ATTRIBUTION_POLICY_VERSION,
                    "execution": current["execution_key"], "candidate": current["selected_candidate_id"]})
                applied = apply_trade_attribution(repo, account=account, market=market, config=config,
                    execution_key=current["execution_key"], candidate_id=current["selected_candidate_id"],
                    expected_input_hash=current["input_hash"], request_id=request_id, actor="trade_intake:attribution_rule",
                    combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode, stop_event=stop_event)
                committed = read_trade_attribution_snapshot(repo, account=account, market=market)
                after_view = build_trade_attribution_view(committed, config=config, account=account, market=market,
                    now_ms=int(time.time() * 1000), combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode)
                current = next(row for row in after_view["rows"] if row["execution_key"] == current["execution_key"])
                result["linked"] += int(applied["write_applied"])
            elif current["status"] == "conflict" and current["rules_enabled"]:
                def record_conflicts(active: Any, conn: Any) -> list[str]:
                    fresh = read_trade_attribution_snapshot(active, account=account, market=market, conn=conn)
                    check = build_trade_attribution_view(fresh, config=config, account=account, market=market,
                        now_ms=int(time.time() * 1000), combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode)
                    fact = next(row for row in check["rows"] if row["execution_key"] == current["execution_key"])
                    if fact["input_hash"] != current["input_hash"] or fact["status"] != "conflict":
                        raise ValueError("conflict evidence changed")
                    ids = list(fact["candidate_ids"])
                    if fact["wheel_branch_id"]:
                        ids.append("wheel:" + fact["wheel_branch_id"])
                    if fact["strategy_group_id"]:
                        ids.append("combo:" + fact["strategy_group_id"])
                    return [record_trade_attribution_conflict(active, account=account, execution_key=fact["execution_key"],
                        branch=branch, candidate_ids=ids, input_hash=fact["input_hash"], now_ms=int(time.time() * 1000), conn=conn,
                        execution_keys=[row["execution_key"] for row in check["rows"] if row["execution_key"]
                            and (row["wheel_branch_id"] == branch["wheel_branch_id"] or "wheel:" + branch["wheel_branch_id"] in row["candidate_ids"])])
                        for branch in check["wheel_model"]["wheel_branches"] if "wheel:" + branch["wheel_branch_id"] in ids]
                events = with_sqlite_repo_transaction(repo, record_conflicts)
                current["ledger_event_ids"] = sorted(set(current["ledger_event_ids"]) | set(events))
                result["conflicts"] += 1
            result["cache_updates"] += cache_trade_attribution_result(inbox_path,
                execution_key=current["execution_key"], result=attribution_result_payload(current))
        except Exception as exc:
            result["errors"].append({"execution_key": current["execution_key"], "error": type(exc).__name__})
        result["checked"] += 1
        result["next_cursor"] = current["execution_key"]
    if len(selected) < 100 and result["checked"] == len(selected):
        result["next_cursor"] = ""
    return result


def attribution_runtime(*, config_key: str | None, config_path: str | None, account: str):
    if not config_key and not config_path:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="请先指定交易市场。")
    path, config = load_runtime_config(config_key=config_key, config_path=config_path)
    account = str(account or "").strip().lower()
    if not account or account not in config.get("accounts", []):
        raise AgentToolError(code="PERMISSION_DENIED", message="账户不在当前配置范围内。")
    data_config = resolve_position_data_config_path(base=repo_base(), cfg=config, config_path=path)
    store = resolve_ledger_store(data_config, config_path=path)
    repo = open_trade_reconciliation_evidence_repo(store.sqlite_path)
    settings = infer_futu_portfolio_settings(config, account=account)
    mapping = {"account": account, "physical_account_ids": sorted(resolve_futu_account_ids(config, account=account)),
               "environment": str(settings.get("trd_env") or "").upper()}
    authority = {"config_path": str(path.resolve()), "runtime_root": str(store.runtime_root.resolve()),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": canonical_sha256(mapping)}
    return repo, config, authority, mapping


def trade_attribution_read(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    repo, config, authority, _mapping = attribution_runtime(
        config_key=payload.get("config_key"), config_path=payload.get("config_path"), account=payload.get("account"))
    account = str(payload["account"]).strip().lower()
    market = runtime_config_market(config).lower()
    snapshot = read_trade_attribution_snapshot(repo, account=account, market=market)
    now = int(time.time() * 1000)
    evidence = read_attribution_combo_evidence(snapshot, account=account, runtime_root=Path(authority["runtime_root"]), now_ms=now)
    view = build_trade_attribution_view(snapshot, config=config, account=account, market=market, now_ms=now, combo_evidence=evidence,
        combo_mode=combo_reconciliation_mode_for_account(config, account=account))
    rows = view["rows"]
    execution = str(payload.get("execution_key") or "").strip()
    status = str(payload.get("status") or "").strip()
    cursor = str(payload.get("cursor") or "")
    if status and status not in {"linked", "ordinary", "pending", "conflict", "not_applicable"}:
        raise AgentToolError(code="INPUT_ERROR", message="无效的归属状态。")
    rows = [row for row in rows if (not execution or row["execution_key"] == execution)
            and (not status or row["status"] == status) and row["open_event_id"] > cursor]
    rows.sort(key=lambda row: row["open_event_id"])
    limit = max(1, min(int(payload.get("limit") or 50), 100))
    page = rows[:limit]
    return {"account": account, "rows": page, "returned_count": len(page),
            "next_cursor": page[-1]["open_event_id"] if len(rows) > limit else None,
            "evidence_scope": "canonical_ledger_and_local_candidates", "evidence_complete": all(row["evidence_complete"] for row in page),
            "capacity_observed": False}, [], {}


def run_attribution_admin(args: Any, *, config: dict[str, Any], config_path: Path,
                          runtime_root: Path) -> dict[str, Any]:
    if any(getattr(args, key, None) for key in (
        "mode", "once", "deal_json", "execution_file", "inbox_id", "retry_failed", "reconcile_state",
        "compensate_receipts", "deal_id", "host", "port", "state_path", "audit_path", "status_path")):
        raise ValueError("attribution administration cannot be combined with listener/replay options")
    if args.dry_run and (args.apply or args.confirm or args.yes):
        raise ValueError("dry-run cannot be combined with write flags")
    control = write_control(apply=args.apply, confirm=args.confirm, yes=args.yes, high_risk=True)
    if control["confirmation_required"]:
        raise ValueError("attribution administration requires --apply with --confirm or --yes")
    data_config = resolve_position_data_config_path(base=repo_base(), cfg=config, config_path=config_path,
                                                   data_config=args.data_config)
    store = resolve_ledger_store(data_config, config_path=config_path, runtime_root=runtime_root)
    if args.apply:
        guard = ledger_store_write_guard(data_config, config_path=config_path, runtime_root=runtime_root)
        if not guard.get("ok"):
            raise ValueError("ledger write scope guard failed: " + str(guard.get("errors")))
    if args.action == "attribution-migrate":
        if not args.apply:
            return preview_trade_attribution_migration(store.sqlite_path)
        if not args.manifest or not args.backup_path:
            raise ValueError("migration apply requires --manifest and --backup-path")
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("ok") is True and "result" in manifest:
            manifest = manifest["result"]
        return apply_trade_attribution_migration(store.sqlite_path, manifest=manifest,
            backup_path=args.backup_path, writers_stopped=args.writers_stopped)
    if args.action != "attribution-enable":
        raise ValueError("unknown attribution administration action")
    account = str(args.account or "").strip().lower()
    if account not in config.get("accounts", []):
        raise ValueError("attribution enabling requires a configured account")
    physical = resolve_futu_account_ids(config, account=account)
    settings = infer_futu_portfolio_settings(config, account=account)
    if len(physical) != 1 or not args.actor or not args.request_id or args.effective_from_ms is None:
        raise ValueError("enabling requires unique physical account, --actor, --request-id and --effective-from-ms")
    scope = {"broker": "futu", "physical_account_id": physical[0], "account": account,
             "environment": str(settings.get("trd_env") or "").upper(),
             "market": runtime_config_market(config).lower()}
    repo = (open_wheel_activation_repository(store.sqlite_path) if args.apply
            else open_trade_reconciliation_evidence_repo(store.sqlite_path))
    return enable_trade_attribution_policy(repo, scope=scope, effective_from_ms=args.effective_from_ms,
        actor=args.actor, request_id=args.request_id, now_ms=int(time.time() * 1000), apply_changes=args.apply)
