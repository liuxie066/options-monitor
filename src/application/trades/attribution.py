from __future__ import annotations

from typing import Any, Mapping
from copy import deepcopy
import json
import time
from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, resolve_futu_account_ids
from src.application.ledger.api import (
    lot_id_for_open_event, write_trade_attribution_decision, read_trade_attribution_decision,
    ledger_resource_identity, open_trade_reconciliation_evidence_repo,
    resolve_ledger_store, resolve_position_data_config_path,
    ledger_store_write_guard,
    preview_trade_attribution_migration, apply_trade_attribution_migration,
    read_trade_attribution_snapshot, open_trade_attribution_snapshot_reader, trade_attribution_facts_from_events,
    encode_evidence_cursor, decode_evidence_cursor, TradeEventPaginationError,
    combo_attribution_candidates_from_rows, ATTRIBUTION_POLICY_VERSION,
    with_sqlite_repo_transaction, record_trade_attribution_conflict,
)
from src.application.futu_quote_routing import runtime_config_market
from src.application.write_contract import write_control
from src.application.trades.account_mapping import combo_reconciliation_mode_for_account
from domain.domain.strategy_membership import resolve_trade_attribution, resolve_option_strategy_membership
from domain.domain.combo_reconciliation import delivered_combo_exposures_for_lot
from domain.domain.symbol_identity import resolve_symbol_identity, symbol_market
from domain.domain.wheel.intents import resolve_wheel_fill_intent, plan_wheel_call_intent_consume, plan_wheel_put_intent_consume
from domain.domain.wheel import effective_wheel_events, project_wheel_linkage_candidates
from domain.domain.ledger.position_fields import effective_contracts_open
from domain.domain.ledger.position_fields import build_open_adjustment_patch_contract
from domain.domain.wheel_call_allocation import parse_wheel_call_allocations
from src.application.ledger.api import (assert_trade_attribution_unclaimed)
from src.application.wheel.config import resolve_wheel_config, evaluate_wheel_activation_readiness
from src.application.wheel.read_model import (
    build_wheel_read_model_from_rows,
    build_wheel_read_model_with_capacity_from_rows,
)
from src.application.wheel.capacity import trade_attribution_capacity_check
from src.application.daily_decision_brief_repository import DailyBriefReadScope, read_combo_candidate_exposures


def attribution_result_payload(fact: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: fact.get(key) for key in ("schema_version", "execution_key", "open_event_id", "lot_id", "account",
        "status", "strategy", "wheel_branch_id", "strategy_group_id", "origin", "reason_codes", "candidate_ids",
        "input_hash", "policy_version", "evaluated_at_ms", "ledger_event_ids", "coverage", "direction",
        "rules_enabled", "evidence_complete", "selected_candidate_id")}
    if fact.get("wheel_call_allocations"):
        payload["wheel_call_allocations"] = fact["wheel_call_allocations"]
    return payload


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


def attribution_focus_open_event_id(rows: Mapping[str, Any], *, account: str, execution_key: str) -> str | None:
    """Resolve only a unique execution; retain all ledger facts for competition."""
    if not execution_key:
        return None
    matches = [row["open_event_id"] for row in trade_attribution_facts_from_events(rows["trade_events"], account=account)
               if row["execution_key"] == execution_key]
    return matches[0] if len(matches) == 1 else None


def read_attribution_combo_evidence(rows: Mapping[str, Any], *, account: str, runtime_root: Path,
                                    now_ms: int, focus_open_event_id: str | None = None) -> dict[str, Any]:
    preview = combo_attribution_candidates_from_rows(rows, account=account, runtime_environment="",
        exposures=[], effective_now_ms=now_ms, include_claimed=True)
    scopes = {(item["market"], item["market_date"]) for item in preview["lot_facts"]
              if focus_open_event_id is None or item["open_event_id"] == focus_open_event_id}
    exposures, reads = {}, []
    read_scopes = {}
    for market, market_date in sorted(scopes):
        if market not in read_scopes:
            read_scopes[market] = DailyBriefReadScope(base=runtime_root, account=account, market=market)
        result = read_combo_candidate_exposures(base=runtime_root, account=account, market=market,
                                                market_trading_date=market_date, read_scope=read_scopes[market])
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
        if (pair["evidence_grade"] != "exact_delivered_candidate" or pair.get("alternative_inference_ids")
                or pair["status"] != "proposal_ready"):
            reasons.append("combo_not_unique_delivered_pair")
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
                    "eligible": False, "reason_codes": ["combo_counterpart_missing_or_asymmetric"], "member_lot_ids": [lot]})
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
            reasons.update(trade_attribution_capacity_check(config=config, fact=fact, facts=account_facts, wheel_read_model=capacity_model,
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
            combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode)
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
                now_ms=instant, combo_evidence=combo_evidence, capacity_observation=capacity_observation, combo_mode=combo_mode)
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


def read_trade_attribution_context(repo: Any, *, config: Mapping[str, Any], account: str,
                                   runtime_root: Path, execution_key: str = "",
                                   snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from src.application.wheel.capacity import observe_trade_attribution_capacity
    observation = observe_trade_attribution_capacity(config=dict(config), account=account, runtime_root=runtime_root)
    market = runtime_config_market(config).lower()
    rows = snapshot if snapshot is not None else read_trade_attribution_snapshot(repo, account=account, market=market)
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root,
        now_ms=int(time.time() * 1000),
        focus_open_event_id=attribution_focus_open_event_id(rows, account=account, execution_key=execution_key))
    return {"config": config, "market": market, "combo_evidence": evidence,
        "capacity_observation": observation, "combo_mode": combo_reconciliation_mode_for_account(config, account=account)}


def apply_referenced_trade_attribution(repo: Any, *, account: str, config: Mapping[str, Any],
    runtime_root: Path, expected_input_hash: str, request_id: str, actor: str, apply_changes: bool,
    option_lot_id: str | None = None, wheel_branch_id: str | None = None, stock_lot_id: str | None = None,
    direction: str | None = None, linkage_candidate_id: str | None = None,
    expected_batch_generation_hash: str | None = None, inference_id: str | None = None,
) -> dict[str, Any]:
    """Adapt existing lot/inference references; admission and writing have one owner.

    expected_input_hash comes from trade_attribution_read(prepare_confirmation=True).
    Historical linkage/proposal hashes cannot authorize a new attribution decision.
    """
    context = {"config": config, "market": runtime_config_market(config).lower(),
        "combo_mode": combo_reconciliation_mode_for_account(config, account=account),
        "combo_evidence": {}, "capacity_observation": {}}
    rows = read_trade_attribution_snapshot(repo, account=account, market=context["market"])
    if inference_id:
        pairs = [row for row in rows["account_combo_inferences"] if row["inference_id"] == inference_id]
        if len(pairs) != 1:
            raise ValueError("combo inference is unavailable")
        pair = pairs[0]
        option_lot_id = pair["put_record_id"]
        candidate_id = "combo:" + pair["strategy_group_id"]
        reference = {"kind": "combo", "inference_id": inference_id}
    else:
        if stock_lot_id:
            model = build_wheel_read_model_from_rows(rows, account=account, market=context["market"],
                as_of_ms=int(time.time() * 1000))
            branches = [row for row in model["wheel_branches"] if row.get("stock_lot_id") == stock_lot_id]
            if len(branches) != 1:
                raise ValueError("Wheel stock lot has no unique branch")
            wheel_branch_id = branches[0]["wheel_branch_id"]
        candidate_id = "wheel:" + str(wheel_branch_id or "")
        reference = {"kind": "wheel", "direction": direction, "linkage_candidate_id": linkage_candidate_id,
            "batch_generation_hash": expected_batch_generation_hash}
    facts = [row for row in trade_attribution_facts_from_events(rows["trade_events"], account=account)
             if row["lot_id"] == option_lot_id]
    if len(facts) != 1 or not facts[0]["execution_key"]:
        raise ValueError("attribution source execution identity is unavailable")
    if not any((row.get("raw_payload") or {}).get("attribution_request_id") == request_id for row in rows["trade_events"]):
        context = read_trade_attribution_context(repo, config=config, account=account, runtime_root=runtime_root)
    result = apply_trade_attribution(repo, account=account, execution_key=facts[0]["execution_key"],
        candidate_id=candidate_id, expected_input_hash=expected_input_hash, request_id=request_id,
        actor=actor, apply_changes=apply_changes, manual=True, reference=reference, **context)
    return {**result, "attribution_status": result["status"], "dry_run": not apply_changes,
        "request_id": request_id, "option_record_id": option_lot_id,
        "event_id": result["proof_event_ids"][0],
        "status": (("dry_run" if not apply_changes else "adopted" if result["write_applied"] else "already_confirmed")
                   if inference_id else ("planned" if not apply_changes else "confirmed" if result["write_applied"] else "idempotent"))}


def reconcile_trade_attribution_account(
    repo: Any, *, config: Mapping[str, Any], account: str, market: str, runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursor: str = "", stop_event: Any = None,
) -> dict[str, Any]:
    with open_trade_attribution_snapshot_reader(repo, account=account, market=market) as read_if_changed:
        return _reconcile_trade_attribution_batch(repo, config=config, account=account, market=market,
            runtime_root=runtime_root, inbox_path=inbox_path, combo_mode=combo_mode, cursor=cursor,
            stop_event=stop_event, read_if_changed=read_if_changed)


def _reconcile_trade_attribution_batch(
    repo: Any, *, config: Mapping[str, Any], account: str, market: str, runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursor: str, stop_event: Any, read_if_changed: Any,
) -> dict[str, Any]:
    from src.application.wheel.capacity import observe_trade_attribution_capacity
    from src.application.trades.inbox import cache_trade_attribution_result

    rows = read_if_changed()
    if rows is None:
        raise RuntimeError("initial trade attribution snapshot is unavailable")
    facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
    selected = sorted((row for row in facts if row["execution_key"] and row["execution_key"] > cursor
        and str(symbol_market(row["contract_key"]["underlying_symbol"]) or "").lower() == market),
        key=lambda row: row["execution_key"])[:100]
    if not selected or stop_event is not None and stop_event.is_set():
        return {"status": "idle", "checked": 0, "next_cursor": ""}
    result = {"checked": 0, "linked": 0, "conflicts": 0, "cache_updates": 0, "errors": [], "next_cursor": cursor}
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root, now_ms=int(time.time() * 1000))
    if stop_event is not None and stop_event.is_set():
        return result
    observation = observe_trade_attribution_capacity(config=dict(config), account=account, runtime_root=runtime_root, stop_event=stop_event)
    if stop_event is not None and stop_event.is_set():
        return result
    view = None
    for selected_fact in selected:
        if stop_event is not None and stop_event.is_set():
            break
        # Every committed change (including in-place fees) invalidates the
        # observation. After attempted effects, force a fresh snapshot as well.
        fresh = read_if_changed(force=evidence is None)
        if stop_event is not None and stop_event.is_set():
            break
        if fresh is not None and (fresh != rows or evidence is None):
            rows = fresh
            evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root,
                now_ms=int(time.time() * 1000))
            view = None
        if stop_event is not None and stop_event.is_set():
            break
        if view is None:
            view = build_trade_attribution_view(rows, config=config, account=account, market=market,
                now_ms=int(time.time() * 1000), combo_evidence=evidence,
                capacity_observation=observation, combo_mode=combo_mode)
        current = next((row for row in view["rows"] if row["execution_key"] == selected_fact["execution_key"]), None)
        if current is None:
            continue
        current = deepcopy(current)
        if stop_event is not None and stop_event.is_set():
            break
        action_attempted = False
        try:
            if current["selected_candidate_id"]:
                action_attempted = True
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
                action_attempted = True
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
        finally:
            # Writers recheck snapshot/time/hash in their own transaction. Even a
            # failed attempt may have changed facts; never reuse its old view.
            if action_attempted:
                view = None
                evidence = None
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
    prepare_confirmation = payload.get("prepare_confirmation", False)
    if not isinstance(prepare_confirmation, bool):
        raise AgentToolError(code="INPUT_ERROR", message="prepare_confirmation 必须为布尔值。")
    repo, config, authority, _mapping = attribution_runtime(
        config_key=payload.get("config_key"), config_path=payload.get("config_path"), account=payload.get("account"))
    account = str(payload["account"]).strip().lower()
    market = runtime_config_market(config).lower()
    cursor_key = canonical_sha256({"tool": "trade_attribution_read", "account": account, "market": market,
                                   "runtime_root": authority["runtime_root"], "config_path": authority.get("config_path"),
                                   "prepare_confirmation": prepare_confirmation})
    execution = str(payload.get("execution_key") or "").strip()
    raw_symbol = str(payload.get("symbol") or "").strip()
    identity = resolve_symbol_identity(raw_symbol) if raw_symbol else None
    if raw_symbol and identity is None:
        raise AgentToolError(code="INPUT_ERROR", message="无法识别该标的。")
    if identity and identity.market.lower() != market:
        raise AgentToolError(code="INPUT_ERROR", message="标的与所选市场不一致。")
    status = str(payload.get("status") or "").strip()
    if status and status not in {"linked", "ordinary", "pending", "conflict", "not_applicable"}:
        raise AgentToolError(code="INPUT_ERROR", message="无效的归属状态。")
    filters = {"execution_key": execution, "symbol": identity.canonical if identity else "", "status": status}
    cursor = ""
    if payload.get("cursor"):
        try:
            state = decode_evidence_cursor(str(payload["cursor"]), cursor_key)
            if (state.get("tool") != "trade_attribution_read" or not isinstance(state.get("filters"), dict)
                    or set(state["filters"]) != set(filters)
                    or not all(isinstance(value, str) for value in state["filters"].values())
                    or not isinstance(state.get("last_open_event_id"), str)):
                raise TradeEventPaginationError("invalid attribution cursor")
            if any(value and value != state["filters"].get(key) for key, value in filters.items()):
                raise TradeEventPaginationError("attribution cursor filters changed")
            filters = state["filters"]
            cursor = state["last_open_event_id"]
        except TradeEventPaginationError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=f"归属分页 cursor 无效或已过期，请重新查询：{exc}") from exc
    snapshot = read_trade_attribution_snapshot(repo, account=account, market=market)
    context = (read_trade_attribution_context(repo, config=config, account=account,
        runtime_root=Path(authority["runtime_root"]), execution_key=filters["execution_key"], snapshot=snapshot)
        if prepare_confirmation else None)
    now = int(time.time() * 1000)
    evidence = context["combo_evidence"] if context else read_attribution_combo_evidence(
        snapshot, account=account, runtime_root=Path(authority["runtime_root"]), now_ms=now,
        focus_open_event_id=attribution_focus_open_event_id(snapshot, account=account, execution_key=filters["execution_key"]))
    view = build_trade_attribution_view(snapshot, config=config, account=account, market=market, now_ms=now, combo_evidence=evidence,
        capacity_observation=context["capacity_observation"] if context else None,
        combo_mode=combo_reconciliation_mode_for_account(config, account=account))
    rows = view["rows"]
    rows = [row for row in rows if (not filters["execution_key"] or row["execution_key"] == filters["execution_key"])
            and (not filters["symbol"] or row["contract_key"]["underlying_symbol"] == filters["symbol"])
            and (not filters["status"] or row["status"] == filters["status"]) and row["open_event_id"] > cursor]
    rows.sort(key=lambda row: row["open_event_id"])
    limit = max(1, min(int(payload.get("limit") or 50), 100))
    page = rows[:limit]
    return {"account": account, "market": market, "rows": page, "returned_count": len(page),
            "next_cursor": encode_evidence_cursor({"tool": "trade_attribution_read", "filters": filters,
                "last_open_event_id": page[-1]["open_event_id"]}, cursor_key) if len(rows) > limit else None,
            "evidence_scope": "canonical_ledger_and_local_candidates", "evidence_complete": all(row["evidence_complete"] for row in page),
            "capacity_observed": bool(context and context["capacity_observation"].get("portfolio")),
            "prepare_confirmation": prepare_confirmation}, [], {}


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
        account = str(args.account or "").strip().lower()
        physical = resolve_futu_account_ids(config, account=account) if account in config.get("accounts", []) else []
        settings = infer_futu_portfolio_settings(config, account=account) if physical else {}
        if len(physical) != 1:
            raise ValueError("attribution migration requires one configured account and physical source")
        scope = {"broker": "futu", "physical_account_id": physical[0], "account": account,
                 "environment": str(settings.get("trd_env") or "").upper(),
                 "market": runtime_config_market(config).lower()}
        if not args.apply:
            if args.effective_from_ms is None:
                raise ValueError("attribution migration preview requires --effective-from-ms")
            return preview_trade_attribution_migration(store.sqlite_path, scope=scope,
                effective_from_ms=args.effective_from_ms)
        if not args.manifest or not args.backup_path:
            raise ValueError("migration apply requires --manifest and --backup-path")
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("ok") is True and "result" in manifest:
            manifest = manifest["result"]
        if not isinstance(manifest, dict) or manifest.get("cutover_scope") != scope:
            raise ValueError("migration manifest source differs from current configuration")
        return apply_trade_attribution_migration(store.sqlite_path, manifest=manifest,
            backup_path=args.backup_path, writers_stopped=args.writers_stopped)
    raise ValueError("unknown attribution administration action")


def confirm_wheel_call_linkage(repo: Any, *, account: str, call_lot_id: str, lot_id: str,
    linkage_candidate_id: str, expected_input_hash: str, expected_batch_generation_hash: str,
    request_id: str, actor: str, config: Mapping[str, Any], runtime_root: Path,
    apply_changes: bool = False,
) -> dict[str, Any]:
    return apply_referenced_trade_attribution(repo, account=account, option_lot_id=call_lot_id,
        stock_lot_id=lot_id, direction="call", linkage_candidate_id=linkage_candidate_id,
        expected_input_hash=expected_input_hash, expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id, actor=actor, config=config, runtime_root=runtime_root, apply_changes=apply_changes)



def confirm_wheel_linkage(repo: Any, *, account: str, option_lot_id: str, wheel_branch_id: str,
    direction: str, linkage_candidate_id: str, expected_input_hash: str, expected_batch_generation_hash: str,
    request_id: str, actor: str, config: Mapping[str, Any], runtime_root: Path,
    apply_changes: bool = False,
) -> dict[str, Any]:
    return apply_referenced_trade_attribution(repo, account=account, option_lot_id=option_lot_id,
        wheel_branch_id=wheel_branch_id, direction=direction, linkage_candidate_id=linkage_candidate_id,
        expected_input_hash=expected_input_hash, expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id, actor=actor, config=config, runtime_root=runtime_root, apply_changes=apply_changes)
