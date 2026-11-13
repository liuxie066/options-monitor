from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

from domain.domain.decision_state_fingerprint import canonical_json_bytes

import pytest

from domain.domain.ledger import TradeEvent
from domain.domain.trade_execution import execution_identity_from_input
from src.application.agent_tools.positions import TRADE_ATTRIBUTION_READ_TOOL
from src.application.daily_decision_brief_service import _pending_attribution_for_brief
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades import attribution
from test_combo_reconciliation_application import _call_open, _put_open
from test_combo_reconciliation_domain import _exposure
from test_trade_attribution_combo_boundary import _args, _read, _scope


def _legacy_confirmation(repo, pair, now, *, damage=None):
    # This supported historical path predates attribution_decision/acknowledgments.
    proof_ids = {}
    for leg, role, opening in (("put", "funding_put", _put_open()),
                               ("call", "participation_call", _call_open())):
        proof_id = "combo-adopt:v1:" + sha256(canonical_json_bytes(
            {"inference_id": pair["inference_id"], "role": role})).hexdigest()
        raw = {"source": "post_trade_combo_reconciliation", "source_type": "combo_pair_inference",
            "mode": "post_trade_combo_adoption", "inference_id": pair["inference_id"],
            "record_id": opening.lot_id, "target_lot_id": opening.lot_id,
            "adjust_target_source_event_id": opening.event_id, "idempotency_key": proof_id,
            "patch": {"strategy": "combo_yield", "strategy_group_id": pair["strategy_group_id"],
                      "leg_role": role, "last_action_at": now}}
        if leg == "put" and damage == "source":
            raw["source"] = "test"
        if leg == "put" and damage == "opening":
            raw["adjust_target_source_event_id"] = "fixture:wrong-opening"
        persist_trade_event_object(repo, TradeEvent(event_id=proof_id, event_type="adjust",
            event_time_ms=now, contract_key=opening.contract_key, contracts=0, price=0,
            currency=opening.currency, multiplier=opening.multiplier, source="post_trade_combo_reconciliation",
            target_lot_id=opening.lot_id, raw_payload=raw))
        proof_ids[leg + "_adoption_event_id"] = proof_id
    repo.transition_combo_pair_inference(inference_id=pair["inference_id"],
        expected_statuses=[pair["status"]], new_status="user_confirmed", decision_fields={
            **proof_ids, "decision_at_ms": now, "decision_by": "test",
            "decision_reason": "user_confirmed_exact_pair"})
    return proof_ids


def _confirmed_scope(tmp_path, monkeypatch, *, contracts=1, legacy=False, legacy_damage=None):
    repo, config, pair, observation, evidence, now = _scope(tmp_path, monkeypatch, contracts=contracts)
    if legacy:
        _legacy_confirmation(repo, pair, now, damage=legacy_damage)
    else:
        attribution.apply_referenced_trade_attribution(
            repo, **_args(repo, config, pair, _read()), apply_changes=True)
    monkeypatch.setattr(attribution.time, "time", lambda: (now + 10_000) / 1000)

    forbidden_calls = []

    def unexpected_observation(**kwargs):
        forbidden_calls.append(kwargs)
        raise AssertionError("a local attribution read must not fetch broker capacity")

    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", unexpected_observation)
    monkeypatch.setattr("src.application.daily_decision_brief_service.resolve_position_ledger_sqlite_path",
                        lambda **kwargs: repo.db_path)
    return repo, config, pair, evidence, now, forbidden_calls


def _close(repo, pair, leg, contracts, now):
    opening = _put_open() if leg == "put" else _call_open()
    event = TradeEvent(event_id=f"fixture:close-{leg}", event_type="close", event_time_ms=now + 1,
        contract_key=opening.contract_key, contracts=contracts, price=1, currency=opening.currency,
        multiplier=opening.multiplier, source="test", target_lot_id=pair[f"{leg}_record_id"])
    persist_trade_event_object(repo, event)
    return event


def _rows(forbidden_calls):
    data, warnings, _ = TRADE_ATTRIBUTION_READ_TOOL.call({"account": "lx"})
    assert forbidden_calls == []
    assert warnings == []
    assert not data["capacity_observed"]
    return data["rows"]


def _by_leg(rows, leg):
    return next(row for row in rows if row["contract_key"]["option_type"] == leg)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("contracts,put_closed,call_closed", [
    (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1),
    (2, 0, 1), (2, 1, 0), (2, 1, 1), (2, 2, 1), (2, 1, 2),
])
def test_confirmed_combo_keeps_ownership_and_explains_remaining_members(
        tmp_path, monkeypatch, contracts, put_closed, call_closed, legacy):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(
        tmp_path, monkeypatch, contracts=contracts, legacy=legacy)
    if put_closed:
        _close(repo, pair, "put", put_closed, now)
    if call_closed:
        _close(repo, pair, "call", call_closed, now)
    before_events = repo.list_trade_events()
    before_inferences = deepcopy(repo.list_combo_pair_inferences(account="lx"))
    before_notifications = repo.list_trade_lifecycle_notifications()
    before_batches = repo.list_trade_lifecycle_notification_batches()

    rows = _rows(forbidden_calls)
    assert {row["status"] for row in rows} == {"linked"}, rows
    for row in rows:
        assert row["status"] == "linked", row
        assert row["origin"] == ("inherited" if legacy else "manual")
        assert row["strategy_group_id"] == pair["strategy_group_id"]
        assert "existing_attribution_preserved" in row["reason_codes"]
        members = {member["leg_role"]: member for member in row["combo_members"]}
        assert set(members) == {"funding_put", "participation_call"}
        for leg, role, closed, opening in (
                ("put", "funding_put", put_closed, _put_open()),
                ("call", "participation_call", call_closed, _call_open())):
            member = members[role]
            assert member["lot_id"] == pair[f"{leg}_record_id"]
            assert member["open_event_id"] == pair[f"{leg}_open_event_id"]
            assert member["contract_key"] == opening.contract_key.to_dict()
            assert member["position_side"] == ("short" if leg == "put" else "long")
            assert member["contracts"] == contracts
            assert member["contracts_open"] == contracts - closed

    pending, error = _pending_attribution_for_brief(
        base=tmp_path, config=config, account="lx", market="US", now_ms=now + 10_000)
    assert error is None
    assert pending == []
    assert _rows(forbidden_calls) == rows
    assert repo.list_trade_events() == before_events
    assert repo.list_combo_pair_inferences(account="lx") == before_inferences
    assert repo.list_trade_lifecycle_notifications() == before_notifications
    assert repo.list_trade_lifecycle_notification_batches() == before_batches


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("new_contracts,partially_closed,projection_drift", [
    (1, False, False), (2, False, False), (2, True, False), (1, False, True), (1, False, "missing"),
])
def test_new_counterpart_still_competes_for_the_original_delivered_exposure(
        tmp_path, monkeypatch, new_contracts, partially_closed, projection_drift, legacy):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(tmp_path, monkeypatch, legacy=legacy)
    _close(repo, pair, "call", 1, now)
    opening = _call_open()
    raw = deepcopy(opening.raw_payload)
    raw["execution_input"]["external_execution_id"] = "fixture:new-call"
    raw["execution_id"] = execution_identity_from_input(raw["execution_input"])
    newcomer = replace(opening, event_id="fixture:new-call", lot_id="fixture:new-call-lot",
        contracts=new_contracts, event_time_ms=now + 2, raw_payload=raw)
    persist_trade_event_object(repo, newcomer)
    if partially_closed:
        persist_trade_event_object(repo, TradeEvent(event_id="fixture:partial-new-call", event_type="close",
            event_time_ms=now + 3, contract_key=newcomer.contract_key, contracts=1, price=1,
            currency=newcomer.currency, multiplier=newcomer.multiplier, source="test",
            target_lot_id=newcomer.lot_id))

    if projection_drift:
        assert _by_leg(_rows(forbidden_calls), "put")["status"] == "conflict"
        snapshot = read_trade_attribution_snapshot(repo, account="lx", market="us")
        stored = next(lot for lot in snapshot["stored_position_lots"] if lot["record_id"] == newcomer.lot_id)
        if projection_drift == "missing":
            snapshot["stored_position_lots"].remove(stored)
        else:
            stored["fields"]["contracts_open"] = 0
        monkeypatch.setattr(attribution, "read_trade_attribution_snapshot", lambda *a, **kw: deepcopy(snapshot))
        monkeypatch.setattr("src.application.ledger.api.read_trade_attribution_snapshot",
                            lambda *a, **kw: deepcopy(snapshot))
        new_row = next(row for row in _rows(forbidden_calls) if row["lot_id"] == newcomer.lot_id)
        assert new_row["contracts_open"] == new_contracts
        assert "ledger_projection_mismatch" in new_row["reason_codes"]
        assert not new_row["evidence_complete"]

    put = _by_leg(_rows(forbidden_calls), "put")
    assert put["status"] == "conflict", put
    assert "late_competing_evidence" in put["reason_codes"]
    assert put["candidate_ids"]
    if new_contracts == 1 and not projection_drift:
        assert any(newcomer.lot_id in candidate["member_lot_ids"] for candidate in put["candidates"])
    else:
        assert any(candidate["candidate_id"].startswith("combo-exposure:") for candidate in put["candidates"])
    pending, error = _pending_attribution_for_brief(
        base=tmp_path, config=config, account="lx", market="US", now_ms=now + 10_000)
    assert error is None
    assert any(row["execution_key"] == put["execution_key"] and row["status"] == "conflict" for row in pending)
    assert forbidden_calls == []


def test_another_delivered_exposure_is_not_covered_by_the_original_confirmation(tmp_path, monkeypatch):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(tmp_path, monkeypatch)
    _close(repo, pair, "call", 1, now)
    other = _exposure(call_strike=120, delivery_confirmed=True)
    other["candidate_exposure_id"] = "fixture:another-exposure"
    evidence["exposures"].append(other)
    put = _by_leg(_rows(forbidden_calls), "put")
    assert put["status"] == "conflict", put
    assert "combo-exposure:fixture:another-exposure" in put["candidate_ids"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("damage", ["inference_member", "missing_member", "quantity_drift", "unconfirmed"])
def test_unproven_confirmation_or_member_snapshot_does_not_cover_exposure(tmp_path, monkeypatch, damage, legacy):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(tmp_path, monkeypatch, legacy=legacy)
    _close(repo, pair, "call", 1, now)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    if damage == "inference_member":
        rows["account_combo_inferences"][0]["call_open_event_id"] = "fixture:wrong-opening"
    elif damage == "missing_member":
        rows["stored_position_lots"] = [lot for lot in rows["stored_position_lots"]
                                        if lot["record_id"] != pair["call_record_id"]]
    elif damage == "quantity_drift":
        lot = next(lot for lot in rows["stored_position_lots"] if lot["record_id"] == pair["put_record_id"])
        lot["fields"]["contracts_open"] = 0
    else:
        rows["account_combo_inferences"][0]["status"] = "proposal_ready"
    view = attribution.build_trade_attribution_view(rows, config=config, account="lx", market="us",
        now_ms=now + 10_000, combo_evidence=evidence)
    put = _by_leg(view["rows"], "put")
    if damage == "quantity_drift":
        # Existing durable attribution survives incomplete data; the mismatch
        # must stay explicit and must not produce verified member quantities.
        assert "ledger_projection_mismatch" in put["reason_codes"]
        assert not put["evidence_complete"]
    else:
        assert put["status"] == "conflict", put
    assert not put.get("combo_members")


def test_voided_closure_restores_the_existing_pair_without_reconfirmation(tmp_path, monkeypatch):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(tmp_path, monkeypatch)
    closed = _close(repo, pair, "call", 1, now)
    persist_trade_event_object(repo, TradeEvent(event_id="fixture:void-close", event_type="void",
        event_time_ms=now + 2, contract_key=closed.contract_key, contracts=0, price=0,
        currency=closed.currency, multiplier=closed.multiplier, source="test", target_event_id=closed.event_id))
    rows = _rows(forbidden_calls)
    assert all(row["status"] == "linked" and row["contracts_open"] == 1 for row in rows)
    assert all(member["contracts_open"] == 1 for member in rows[0]["combo_members"])


@pytest.mark.parametrize("legacy", [False, True])
def test_voided_confirmation_cannot_supply_confirmed_member_evidence(tmp_path, monkeypatch, legacy):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(tmp_path, monkeypatch, legacy=legacy)
    put_before = _by_leg(_rows(forbidden_calls), "put")
    _close(repo, pair, "call", 1, now)
    opening = _put_open()
    persist_trade_event_object(repo, TradeEvent(event_id="fixture:void-confirmation", event_type="void",
        event_time_ms=now + 2, contract_key=opening.contract_key, contracts=0, price=0,
        currency=opening.currency, multiplier=opening.multiplier, source="test",
        target_event_id=put_before["ledger_event_ids"][0]))
    put = _by_leg(_rows(forbidden_calls), "put")
    assert put["status"] in {"pending", "conflict"}, put
    assert not put.get("combo_members")


@pytest.mark.parametrize("damage", ["source", "opening"])
def test_unproven_legacy_adoption_cannot_supply_confirmed_member_evidence(tmp_path, monkeypatch, damage):
    repo, config, pair, evidence, now, forbidden_calls = _confirmed_scope(
        tmp_path, monkeypatch, legacy=True, legacy_damage=damage)
    _close(repo, pair, "call", 1, now)
    put = _by_leg(_rows(forbidden_calls), "put")
    assert put["status"] == "conflict", put
    assert "late_competing_evidence" in put["reason_codes"]
    assert not put.get("combo_members")
