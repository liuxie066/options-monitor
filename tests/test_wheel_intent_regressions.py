"""Temporal intent matching and effective cancellation receipts on isolated SQLite."""
from copy import deepcopy
import socket
import subprocess

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.portfolio_scope import portfolio_scope_id
from domain.domain.trade_execution import execution_identity_from_input
from domain.domain.wheel import build_wheel_event, project_wheel_intents
from domain.domain.wheel.intents import (
    plan_wheel_call_intent_consume, plan_wheel_put_intent_consume, resolve_wheel_fill_intent,
)
from src.application.ledger import api, wheel_capacity, wheel_commands
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel import build_wheel_read_model
from tests.test_wheel_intent_policy_binding import _cash
from tests.test_wheel_ledger_commands import _cancel_request, _intent_scope
from tests.test_wheel_workflows import _trusted_multiplier_payload


@pytest.fixture(autouse=True)
def _no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Wheel regressions must not contact external services or launch processes")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _refresh_request(repo, request, *, instant, request_id):
    request = deepcopy(request)
    branch = build_wheel_read_model(repo, "lx", instant, market="us")["wheel_branches"][0]
    assert branch["integrity_status"] == "trusted" and branch["phase"] == "ready"
    request.update(as_of_ms=instant, request_id=request_id,
                   expected_batch_generation_hash=branch["batch_generation_hash"],
                   expected_snapshot_hash=f"snapshot-{request_id}", broker_order_id=f"order-{request_id}")
    snapshot = request["candidate_snapshot"]
    candidate = snapshot["batches"][0]["final_candidate"]
    candidate["batch_generation_hash"] = branch["batch_generation_hash"]
    snapshot["batches"] = [{**branch, "final_candidate": candidate}]
    snapshot["snapshot_hash"] = request["expected_snapshot_hash"]
    decision = api.decision_state_snapshot(repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"),
        source_observed_at=request["capacity_fact"]["source_observed_at"], current_decision_now_ms=instant)
    request["capacity_fact"]["decision_state_fingerprint"] = decision["decision_state_fingerprint"]
    if request["direction"] == "put":
        candidate.pop("capacity_identity_hash", None)
        allocation = api.revalidate_selected_wheel_put_candidate_from_rows(
            account="lx", portfolio_context=_cash(), position_lots=repo.list_position_lots(),
            lifecycle_rows=repo.read_lifecycle_account_rows(account="lx"), broker="futu", as_of_ms=instant,
            fx_snapshot={}, final_candidate=candidate, decision_snapshot=decision)
        identity = allocation["capacity_identity_hash"]
    else:
        identity = f"capacity-{request_id}"
    candidate["capacity_identity_hash"] = identity
    request["capacity_fact"]["capacity_identity_hash"] = identity
    return request


def _scope(tmp_path, monkeypatch, direction):
    repo, request = _intent_scope(tmp_path, monkeypatch)(direction)
    # Use the real Put allocation/validation too; only external observations are fixtures.
    monkeypatch.setattr(wheel_commands, "revalidate_selected_wheel_put_candidate_from_rows",
                        wheel_capacity.revalidate_selected_wheel_put_candidate_from_rows)
    return repo, _refresh_request(repo, request, instant=5_000, request_id="A")


def _summaries(repo, request, instant):
    rows = repo.read_lifecycle_account_rows(account="lx")
    return project_wheel_intents(rows["account_wheel_events"], account="lx",
        wheel_branch_id=request["wheel_branch_id"], direction=request["direction"], as_of_ms=instant,
        known_trade_event_ids={event["event_id"] for event in rows["trade_events"]})


@pytest.mark.parametrize("direction,entry", [("call", "generic"), ("call", "call_specific"), ("put", "generic")])
@pytest.mark.parametrize("now_ms", [8_000, 11_000])
def test_cancel_retry_requires_effective_original_cancellation(tmp_path, monkeypatch, direction, now_ms, entry):
    repo, request = _scope(tmp_path, monkeypatch, direction)
    created = api.create_wheel_intent(repo, **request, apply_changes=True)
    cancel = _cancel_request(repo, created, request)
    command = api.cancel_wheel_intent
    if entry == "call_specific":
        command = api.cancel_wheel_call_intent
        cancel = {key: value for key, value in cancel.items()
                  if key not in {"wheel_branch_id", "direction", "capacity_fact"}}
        cancel["lot_id"] = request["wheel_branch_id"]
    result = command(repo, **cancel, apply_changes=True)
    replay = command(repo, **cancel, apply_changes=True)
    assert result["write_applied"] and not replay["write_applied"]
    assert replay["status"] == "idempotent" and replay["event_id"] == result["event_id"]
    assert _summaries(repo, request, now_ms)[0]["status"] == "cancelled"
    assert command(repo, **{**cancel, "as_of_ms": now_ms}, apply_changes=True)["status"] == "idempotent"
    cancellation = next(event for event in repo.list_wheel_events(account="lx")
                        if event["event_id"] == result["event_id"])
    void = build_wheel_event(event_id="void-cancellation", account="lx",
        wheel_branch_id=cancellation["wheel_branch_id"], lot_id=cancellation.get("stock_lot_id"),
        event_type="wheel_event_voided", occurred_at_ms=7_000, recorded_at_ms=7_000,
        payload={"target_wheel_event_id": result["event_id"]})
    with repo._writer_connection(begin_immediate=True) as conn:
        assert repo.append_wheel_event_once(void, conn=conn)
    # A future void cannot invalidate an earlier retry's effective result.
    assert command(repo, **{**cancel, "as_of_ms": 6_500}, apply_changes=True)["status"] == "idempotent"
    summary = _summaries(repo, request, now_ms)[0]
    assert summary["status"] == ("active" if now_ms == 8_000 else "expired")
    branch = build_wheel_read_model(repo, "lx", now_ms, market="us")["wheel_branches"][0]
    assert branch["integrity_status"] == "trusted"
    assert branch["active_intent_reserved_shares"] == (100 if now_ms == 8_000 else 0)
    before = repo.read_lifecycle_account_rows(account="lx")
    for apply in (False, True):
        with pytest.raises(ValueError, match="cancellation.*no longer effective.*new request"):
            command(repo, **{**cancel, "as_of_ms": now_ms}, apply_changes=apply)
        assert repo.read_lifecycle_account_rows(account="lx") == before
    if now_ms == 8_000:
        renewed = {**cancel, "as_of_ms": now_ms, "request_id": "new-cancellation",
                   "expected_batch_generation_hash": branch["batch_generation_hash"]}
        fresh = command(repo, **renewed, apply_changes=True)
        assert fresh["write_applied"] and fresh["event_id"] != result["event_id"]
        assert len(repo.list_wheel_events(account="lx")) == len(before["account_wheel_events"]) + 1
        assert _summaries(repo, request, now_ms)[0]["status"] == "cancelled"
        # Another effective cancellation does not revive the voided request's identity.
        after = repo.read_lifecycle_account_rows(account="lx")
        with pytest.raises(ValueError, match="cancellation.*no longer effective.*new request"):
            command(repo, **{**cancel, "as_of_ms": now_ms}, apply_changes=True)
        assert repo.read_lifecycle_account_rows(account="lx") == after


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("now_ms", [8_500, 12_000])
def test_cancel_recreate_matches_persisted_fill_in_public_attribution(tmp_path, monkeypatch, direction, now_ms):
    repo, first = _scope(tmp_path, monkeypatch, direction)
    created_a = api.create_wheel_intent(repo, **first, apply_changes=True)
    api.cancel_wheel_intent(repo, **_cancel_request(repo, created_a, first), apply_changes=True)
    fresh = _refresh_request(repo, first, instant=7_000, request_id="B")
    assert fresh["expected_batch_generation_hash"] != first["expected_batch_generation_hash"]
    if direction == "call":
        assert fresh["capacity_fact"]["capacity_identity_hash"] != first["capacity_fact"]["capacity_identity_hash"]
    created_b = api.create_wheel_intent(repo, **fresh, apply_changes=True)
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": "B-fill",
                 "broker_account_ref": {"broker_id": "futu", "external_account_id": "123", "environment": "REAL"}}
    fill = TradeEvent(event_id="B-fill", event_type="open", event_time_ms=8_000,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
            option_type=direction, strike=100, expiration_ymd="2026-09-18"),
        contracts=1, price=1, currency="USD", source="test", multiplier=100, lot_id="B-fill-lot",
        raw_payload=_trusted_multiplier_payload("B-fill", side="sell", order_id="order-B",
            external_order_namespace="futu.order", futu_account_id="123",
            execution_input=execution, execution_id=execution_identity_from_input(execution)))
    persist_trade_event_objects_atomically(repo, [fill])
    rows = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    branch = build_wheel_read_model(repo, "lx", now_ms, market="us")["wheel_branches"][0]
    result = resolve_wheel_fill_intent(branch, fill, rows["account_wheel_events"], now_ms=now_ms,
                                     known_trade_event_ids={event["event_id"] for event in rows["trade_events"]})
    assert result["reason_codes"] == []
    assert result["intent"]["intent_id"] == created_b["intent_id"]
    assert result["reserved_contracts_to_consume"] == (1 if now_ms < 10_000 else 0)
    view = api.build_trade_attribution_view(rows, config={"wheel": {"accounts": ["lx"]}},
        account="lx", market="us", now_ms=now_ms, combo_evidence={"complete": True, "exposures": []})
    fact = next(item for item in view["rows"] if item["open_event_id"] == fill.event_id)
    candidate = next(item for item in fact["candidates"] if item["candidate_id"] == "wheel:" + branch["wheel_branch_id"])
    assert candidate["intent_id"] == created_b["intent_id"]
    assert "multiple_or_invalid_wheel_intents" not in fact["reason_codes"]
    assert "wheel_intent_fill_mismatch_or_consumed" not in fact["reason_codes"]
    planner = plan_wheel_call_intent_consume if direction == "call" else plan_wheel_put_intent_consume
    bound_capacity = {**fresh["capacity_fact"], **{key: result["intent"]["payload"].get(key)
                      for key in ("cash_reservation_currency", "cash_reservation_amount")}}
    consumed = planner(branch, result["intent"], fill, bound_capacity, recorded_at_ms=now_ms)
    with repo._writer_connection(begin_immediate=True) as conn:
        assert repo.append_wheel_event_once(consumed, conn=conn)
    statuses = {item["intent_id"]: item["status"] for item in _summaries(repo, fresh, now_ms)}
    assert statuses == {created_a["intent_id"]: "cancelled", created_b["intent_id"]: "consumed"}


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("case,expected,reason", [
    ("cancelled", "B", None), ("expired", "B", None),
    ("cancel_at_fill", "B", None),
    ("overlap", None, "multiple_or_invalid_wheel_intents"),
    ("late_cancel", None, "multiple_or_invalid_wheel_intents"),
    ("late_old_fill", "A", None),
    ("wrong_order", None, "wheel_intent_fill_mismatch_or_consumed"),
    ("consumed", None, "wheel_intent_fill_mismatch_or_consumed"),
    ("over_quantity", None, "wheel_intent_fill_mismatch_or_consumed"),
    ("unknown_history", None, "multiple_or_invalid_wheel_intents"),
    ("only_cancelled", None, "wheel_intent_fill_mismatch_or_consumed"),
])
def test_fill_time_selection_preserves_fail_closed_guards(direction, case, expected, reason):
    branch = {"account": "lx", "symbol": "NVDA", "wheel_branch_id": "branch", "stock_lot_id": "branch",
              "direction": direction, "market": "us", "lifecycle_status": "active", "integrity_status": "trusted"}

    def event(intent, kind, instant, **payload):
        return build_wheel_event(event_id=f"{intent}-{kind}-{instant}", account="lx", lot_id="branch",
            wheel_branch_id="branch", event_type=f"wheel_{direction}_intent_{kind}",
            occurred_at_ms=instant, recorded_at_ms=instant, intent_id=intent, payload=payload,
            source_trade_event_id="already-booked" if kind == "consumed" else None)

    def creation(intent, instant, expires):
        return event(intent, "created", instant, contracts=1, multiplier=100, expires_at_ms=expires,
            expiration_ymd="2026-09-18", strike=100, broker_order_id=f"order-{intent}",
            capacity_identity_hash=f"capacity-{intent}", cash_reservation_currency="USD",
            cash_reservation_amount=10_000)

    history = [creation("A", 5_000, 6_000 if case == "expired" else 10_000)]
    if case not in {"overlap", "expired"}:
        history.append(event("A", "cancelled", 9_000 if case == "late_cancel" else
                             8_000 if case == "cancel_at_fill" else 6_000))
    if case != "only_cancelled":
        history.append(creation("B", 7_000, 10_000))
    if case == "unknown_history":
        history.append(creation("A", 5_100, 10_000))
    if case == "consumed":
        history.append(event("B", "consumed", 7_500, contracts=1))
    fill = {"event_id": "fill", "event_type": "open", "account": "lx", "symbol": "NVDA",
            "option_type": direction, "position_side": "short", "event_time_ms": 5_500 if case == "late_old_fill" else 8_000,
            "expiration_ymd": "2026-09-18", "strike": 100, "multiplier": 100,
            "contracts": 2 if case == "over_quantity" else 1,
            "raw_payload": {"order_id": "order-A" if case in {"late_old_fill", "wrong_order"} else "order-B"}}
    result = resolve_wheel_fill_intent(branch, fill, history, now_ms=9_500,
                                     known_trade_event_ids={"fill", "already-booked"})
    assert result["reason_codes"] == ([reason] if reason else [])
    assert (result["intent"] or {}).get("intent_id") == expected
    assert result["reserved_contracts_to_consume"] == (1 if expected == "B" else 0)


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("foreign", ["account", "branch"])
def test_foreign_void_does_not_invalidate_cancellation_retry(tmp_path, monkeypatch, direction, foreign):
    repo, request = _scope(tmp_path, monkeypatch, direction)
    created = api.create_wheel_intent(repo, **request, apply_changes=True)
    cancel = _cancel_request(repo, created, request)
    cancelled = api.cancel_wheel_intent(repo, **cancel, apply_changes=True)
    void = build_wheel_event(event_id="foreign-void", account="sy" if foreign == "account" else "lx",
        lot_id=None, wheel_branch_id="foreign-branch" if foreign == "branch" else request["wheel_branch_id"],
        event_type="wheel_event_voided", occurred_at_ms=7_000, recorded_at_ms=7_000,
        payload={"target_wheel_event_id": cancelled["event_id"]})
    with repo._writer_connection(begin_immediate=True) as conn:
        assert repo.append_wheel_event_once(void, conn=conn)
    before = repo.read_lifecycle_account_rows(account="lx")
    replay = api.cancel_wheel_intent(repo, **{**cancel, "as_of_ms": 8_000}, apply_changes=True)
    assert replay["status"] == "idempotent" and not replay["write_applied"]
    assert replay["event_id"] == cancelled["event_id"]
    assert _summaries(repo, request, 8_000)[0]["status"] == "cancelled"
    assert repo.read_lifecycle_account_rows(account="lx") == before
