"""Explicit Wheel provenance plus current ledger facts; no contract/time inference."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from domain.domain.performance.period import PeriodWindow
from domain.domain.wheel import effective_wheel_events
from domain.domain.wheel.projection import project_wheel_intents
from src.application.ledger.api import open_trade_reconciliation_evidence_repo, project_trade_event_log
from src.application.performance.service import build_option_period_performance


def attach_trade_results(result: dict[str, Any], *, ledger_path, scope, account) -> None:
    rows = result["rows"]
    origins = []
    for row in rows:
        row["trade_results"] = []
        for candidate in row.pop("original_candidate_sources", []):
            source = candidate.get("source") or {}
            entry = {"action_id": candidate.get("action_id"), "symbol": candidate.get("symbol"),
                     "status": "unlinked", "label": "未建立关联", "trades": []}
            row["trade_results"].append(entry)
            if candidate.get("strategy_family") == "wheel" and source.get("final_candidate_id") and source.get("candidate_snapshot_hash"):
                origins.append((row, candidate, entry))
    if not origins:
        return
    try:
        snapshot = open_trade_reconciliation_evidence_repo(ledger_path).read_decision_history_evidence(account=account)
        events = snapshot["trade_events"]
        published = project_trade_event_log(events)
        open_events = {event.event_id: event for event in published.ledger_projection.effective_open_events}
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        effective, invalid = effective_wheel_events(snapshot["wheel_events"], as_of_ms=now_ms,
                                                   known_trade_event_ids=set(open_events), trade_events=events)
        # Existing reducer owns terminal allocation, economic corrections, actual fees and currency.
        period = PeriodWindow("history", "UTC", "1970-01-01", now.date().isoformat(), 0, now_ms + 1, 0, now_ms, "partial_current")
        repo = SimpleNamespace(list_trade_events=lambda: events, list_position_lots=lambda: [])
        facts = build_option_period_performance(repo, period=period, config_key="history", configured_accounts=[account],
                                               account=account, include_rows=True)["rows"]
        by_open: dict[str, list[dict[str, Any]]] = {}
        for fact in facts:
            by_open.setdefault(fact["open_event_id"], []).append(fact)
        for row, candidate, entry in origins:
            _link(entry, candidate, scope=scope, account=account, market=row["brief"]["market"],
                  events=effective, invalid=invalid, open_events=open_events, facts=by_open, now_ms=now_ms,
                  source_events={event["event_id"]: event for event in events})
    except Exception as exc:
        # Source failure is a gap, never a false "no trade" or a reason to run a write/refresh.
        for _row, _candidate, entry in origins:
            entry.update(status="unavailable", label="结果不可用", reason=type(exc).__name__, trades=[])
    if any(entry["status"] in {"unavailable", "conflict"} or
           any(outcome.get("missing") or outcome["label"] == "结果不可用" for trade in entry["trades"] for outcome in trade["outcomes"])
           for row in rows for entry in row["trade_results"]):
        result["status"] = "partial"


def _link(entry, candidate, *, scope, account, market, events, invalid, open_events, facts, now_ms, source_events):
    source = candidate["source"]
    branch = str(candidate.get("wheel_branch_id") or "")
    direction = candidate.get("option_type")
    if not branch or direction not in {"call", "put"}:
        entry.update(status="unavailable", label="结果不可用", reason="candidate_identity_incomplete")
        return
    if invalid.get((account, branch)):
        entry.update(status="conflict", label="无法确认关联", reason="wheel_event_conflict")
        return
    created = [event for event in events if event["account"] == account and event["wheel_branch_id"] == branch
               and event["event_type"] == f"wheel_{direction}_intent_created"
               and event["payload"].get("final_candidate_id") == source["final_candidate_id"]
               and event["payload"].get("snapshot_hash") == source["candidate_snapshot_hash"]]
    if not created:
        return
    states = {item["intent_id"]: item for item in project_wheel_intents(events, account=account,
        wheel_branch_id=branch, direction=direction, as_of_ms=now_ms, known_trade_event_ids=set(open_events))}
    intent_ids = {event["intent_id"] for event in created}
    if any(states.get(intent, {}).get("status") in {None, "conflict"} for intent in intent_ids):
        entry.update(status="conflict", label="无法确认关联", reason="intent_conflict")
        return
    consumed = [event for event in events if event["account"] == account and event["wheel_branch_id"] == branch
                and event["intent_id"] in intent_ids and event["event_type"] == f"wheel_{direction}_intent_consumed"]
    if not consumed:
        return
    by_fill: dict[str, list[dict[str, Any]]] = {}
    for event in consumed:
        by_fill.setdefault(event["source_trade_event_id"], []).append(event)
    for event_id, links in by_fill.items():
        opening = open_events.get(event_id)
        if opening is None:
            entry.update(status="unavailable", label="结果不可用", reason="effective_open_event_missing", trades=[])
            return
        raw = opening.raw_payload
        if (opening.contract_key.account != account or str(raw.get("futu_account_id") or "") != scope["futu_account_id"]
                or raw.get("trd_env") != scope["trade_env"] or str(raw.get("market") or "").upper() != market):
            entry.update(status="unavailable", label="结果不可用", reason="trade_scope_unproven", trades=[])
            return
        all_links = [event for event in events if event["source_trade_event_id"] == event_id
                     and event["event_type"] in {"wheel_put_intent_consumed", "wheel_call_intent_consumed"}]
        if ({event["event_id"] for event in all_links} != {event["event_id"] for event in links}
                or sum(event["payload"]["contracts"] for event in links) != opening.contracts
                or any(event["payload"].get("multiplier") != opening.multiplier for event in links)):
            entry.update(status="conflict", label="无法确认关联", reason="quantity_or_origin_conflict", trades=[])
            return
        current = facts.get(event_id, [])
        if not current:
            entry.update(status="unavailable", label="结果不可用", reason="ledger_result_missing", trades=[])
            return
        if sum(fact["contracts"] for fact in current) != opening.contracts:
            entry.update(status="unavailable", label="结果不可用", reason="ledger_quantity_incomplete", trades=[])
            return
        unfinished = any(fact["state"] != "terminated" for fact in current)
        outcomes = []
        for fact in current:
            view = {key: fact[key] for key in ("fact_id", "open_lot_id", "terminal_event_id", "state", "currency", "contracts", "multiplier", "missing")}
            terminal_source = (source_events.get(fact["terminal_event_id"], {}).get("raw_payload") or {})
            terminal_scope_ok = (str(terminal_source.get("futu_account_id") or "") == scope["futu_account_id"]
                                 and terminal_source.get("trd_env") == scope["trade_env"]
                                 and str(terminal_source.get("market") or "").upper() == market)
            if unfinished:
                view.update(label="未结束", option_net_cashflow=None)
            elif not terminal_scope_ok or fact["option_net_cashflow"] is None or not fact["currency"] or not fact["multiplier"]:
                view.update(label="结果不可用", option_net_cashflow=None)
                if not terminal_scope_ok:
                    view["missing"] = sorted(set(view["missing"]) | {"terminal_scope_unproven"})
            else:
                view.update(label="已实现期权净现金流", option_net_cashflow=fact["option_net_cashflow"],
                            opening_actual_fee=fact["opening_actual_fee"], terminal_actual_fee=fact["terminal_actual_fee"])
            outcomes.append(view)
        entry["trades"].append({"open_event_id": event_id, "contracts": opening.contracts, "outcomes": outcomes})
    entry.update(status="linked", label="已建立明确关联")
