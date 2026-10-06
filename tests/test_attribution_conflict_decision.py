from copy import deepcopy
import time

import pytest

from domain.domain.wheel import effective_wheel_events
from src.application.ledger.api import read_trade_attribution_snapshot, record_trade_attribution_conflict, with_sqlite_repo_transaction
from src.application.trades import attribution
from test_trade_attribution_meituan import _meituan_repo


def _scope(tmp_path, monkeypatch):
    repo, config, _ = _meituan_repo(tmp_path)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", lambda **_: {"status": "available", "reason_codes": []})
    return repo, config


def _view(repo, config):
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    return attribution.build_trade_attribution_view(rows, config=config, account="lx", market="hk",
        now_ms=int(time.time() * 1000), combo_evidence={"complete": True, "exposures": []})


def _call(view):
    return next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")


def _args(config, fact, target, request, conflicts=()):
    return dict(account="lx", market="hk", config=config, execution_key=fact["execution_key"],
        candidate_id=target, expected_input_hash=fact["input_hash"], request_id=request, actor="fixture:operator",
        combo_evidence={"complete": True, "exposures": []}, capacity_observation={}, combo_mode="confirm",
        manual=True, conflict_event_ids=conflicts)


def _conflict(repo, view, *, suffix="", keys=()):
    fact = _call(view)
    branch = view["wheel_model"]["wheel_branches"][0]
    return with_sqlite_repo_transaction(repo, lambda active, conn: record_trade_attribution_conflict(active,
        account="lx", execution_key=fact["execution_key"], branch=branch,
        candidate_ids=[*fact["candidate_ids"], *( ["test:" + suffix] if suffix else [])], input_hash=fact["input_hash"],
        now_ms=int(time.time() * 1000), conn=conn, execution_keys=keys))


def _statuses(repo, at=None, *, trade_events=None):
    events = repo.list_trade_events() if trade_events is None else trade_events
    result = {}
    effective_wheel_events(repo.list_wheel_events(account="lx"), as_of_ms=at or int(time.time() * 1000),
        known_trade_event_ids={row["event_id"] for row in events}, trade_events=events, conflict_statuses=result)
    return result


@pytest.mark.parametrize("decision", ["keep", "move", "ordinary"])
def test_conflict_decision_is_atomic_and_request_bound(tmp_path, monkeypatch, decision):
    repo, config = _scope(tmp_path, monkeypatch)
    view = _view(repo, config)
    call = _call(view)
    first = call["candidate_ids"][0]
    attribution.apply_trade_attribution(repo, **_args(config, call, first, "initial"))
    assert not attribution.apply_trade_attribution(repo, **_args(config, call, first, "already-linked"))["write_applied"]
    view = _view(repo, config)
    conflict = _conflict(repo, view)
    call = _call(_view(repo, config))
    target = first if decision == "keep" else call["candidate_ids"][1] if decision == "move" else "ordinary"
    args = _args(config, call, target, "decision", (conflict,))
    before = repo.list_trade_events()
    preview = attribution.apply_trade_attribution(repo, **args, apply_changes=False)
    assert not preview["write_applied"] and repo.list_trade_events() == before
    result = attribution.apply_trade_attribution(repo, **args)
    assert result["write_applied"] and _statuses(repo)[conflict]["resolved"]
    assert result["status"] == ("ordinary" if decision == "ordinary" else "linked")
    assert len(repo.list_trade_events()) == len(before) + 1
    assert attribution.apply_trade_attribution(repo, **args)["write_applied"] is False
    assert repo.list_trade_events()[:len(before)] == before
    with pytest.raises(ValueError, match="request identity conflicts"):
        attribution.apply_trade_attribution(repo, **{**args, "actor": "other"})
    proof = next(row for row in repo.list_trade_events() if row["event_id"] in result["proof_event_ids"])
    assert not _statuses(repo, proof["event_time_ms"] - 1)[conflict]["resolved"]
    broken = deepcopy(repo.list_trade_events())
    broken = [row for row in broken if row["event_id"] != proof["event_id"]]
    assert not _statuses(repo, trade_events=broken)[conflict]["resolved"]
    for field, value in (("adjust_target_source_event_id", "wrong-source"), ("attribution_decision", None),
                         ("attribution_policy_version", "wrong-policy")):
        broken = deepcopy(repo.list_trade_events())
        next(row for row in broken if row["event_id"] == proof["event_id"])["raw_payload"][field] = value
        assert not _statuses(repo, trade_events=broken)[conflict]["resolved"]


@pytest.mark.parametrize("decision", ["keep", "move", "ordinary"])
def test_conflict_decision_with_trusted_current_projection_preserves_unallocated_review(tmp_path, monkeypatch, decision):
    from src.application.ledger.assigned_stock_projection import project_assigned_stock_lifecycle_from_rows
    from src.application.ledger.current_decision_projection import (
        build_current_decision_projection, compact_assigned_stock_view,
        current_decision_projection_row, read_current_decision_projection,
    )

    repo, config = _scope(tmp_path, monkeypatch)
    call = _call(_view(repo, config))
    old_target = call["candidate_ids"][0]
    attribution.apply_trade_attribution(repo, **_args(config, call, old_target, "initial"))
    now = int(time.time() * 1000)
    with repo._connect() as conn:
        conn.execute("""INSERT OR IGNORE INTO current_decision_input_generations (
            account, generation, case_generation, evidence_generation, allocation_generation,
            source_consumption_generation, timing_generation, combo_identity_generation,
            assigned_stock_generation, updated_at_ms
        ) VALUES ('lx', 0, 0, 0, 0, 0, 0, 0, 0, 1)""")
    assigned = compact_assigned_stock_view(project_assigned_stock_lifecycle_from_rows(
        repo.read_lifecycle_account_rows(account="lx"), account="lx", as_of_ms=now),
        account="lx", current_position_lots=repo.list_position_lots(), as_of_ms=now)
    assert assigned["covered_call_allocations"] == []
    assert any(row["status"] == "covered_call_unallocated" and row["event_id"] == call["open_event_id"]
               for row in assigned["review_facts"])
    repo.upsert_current_decision_projection(current_decision_projection_row(build_current_decision_projection(
        repo, account="lx", updated_at_ms=now, assigned_stock_after=assigned, all_quality_case_facts=[])))
    assert read_current_decision_projection(repo, account="lx", now_ms=now)["status"] == "trusted"

    conflict = _conflict(repo, _view(repo, config))
    call = _call(_view(repo, config))
    target = "ordinary" if decision == "ordinary" else old_target if decision == "keep" else next(
        candidate for candidate in call["candidate_ids"] if candidate != old_target)
    result = attribution.apply_trade_attribution(repo, **_args(config, call, target, "trusted", (conflict,)))
    assert result["write_applied"] and _statuses(repo)[conflict]["resolved"]
    after = read_current_decision_projection(repo, account="lx", now_ms=int(time.time() * 1000))
    assert after["status"] == "trusted"
    assert after["payload"]["assigned_stock"]["covered_call_allocations"] == []
    assert after["payload"]["assigned_stock"]["review_facts"] == assigned["review_facts"]


@pytest.mark.parametrize("cancel_after_write", [False, True])
def test_cancel_before_commit_rolls_back_the_entire_decision(tmp_path, monkeypatch, cancel_after_write):
    from threading import Event
    repo, config = _scope(tmp_path, monkeypatch)
    conflict = _conflict(repo, _view(repo, config))
    call = _call(_view(repo, config))
    stop = Event()
    if cancel_after_write:
        writer = attribution.write_trade_attribution_decision
        def cancel(*args, **kwargs):
            result = writer(*args, **kwargs)
            stop.set()
            return result
        monkeypatch.setattr(attribution, "write_trade_attribution_decision", cancel)
    else:
        stop.set()
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots())
    with pytest.raises(ValueError, match="cancelled"):
        attribution.apply_trade_attribution(repo, **_args(config, call, "ordinary", "cancel", (conflict,)), stop_event=stop)
    assert (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()) == before


def test_incomplete_conflict_and_stale_input_leave_no_partial_effect(tmp_path, monkeypatch):
    repo, config = _scope(tmp_path, monkeypatch)
    conflict = _conflict(repo, _view(repo, config), keys=("missing-execution",))
    call = _call(_view(repo, config))
    args = _args(config, call, "ordinary", "incomplete", (conflict,))
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots())
    with pytest.raises(ValueError, match="complete member"):
        attribution.apply_trade_attribution(repo, **args)
    assert (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()) == before
    with pytest.raises(ValueError, match="evidence changed"):
        attribution.apply_trade_attribution(repo, **{**args, "expected_input_hash": "stale"})


def test_final_capacity_failure_rolls_back_proof_resolution_and_projection(tmp_path, monkeypatch):
    repo, config = _scope(tmp_path, monkeypatch)
    conflict = _conflict(repo, _view(repo, config))
    call = _call(_view(repo, config))
    args = _args(config, call, call["candidate_ids"][0], "rollback", (conflict,))
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots())
    original = attribution.write_trade_attribution_decision
    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(attribution, "trade_attribution_capacity_check", lambda **_: {"status": "unavailable", "reason_codes": ["stale"]})
        return result
    monkeypatch.setattr(attribution, "write_trade_attribution_decision", fail_after_write)
    with pytest.raises(ValueError, match="capacity changed"):
        attribution.apply_trade_attribution(repo, **args)
    assert (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()) == before


def test_resolves_only_selected_conflict_and_future_void_does_not_change_history(tmp_path, monkeypatch):
    from domain.domain.ledger import TradeEvent
    repo, config = _scope(tmp_path, monkeypatch)
    first = _conflict(repo, _view(repo, config), suffix="first")
    second = _conflict(repo, _view(repo, config), suffix="second")
    call = _call(_view(repo, config))
    result = attribution.apply_trade_attribution(repo, **_args(config, call, "ordinary", "one-only", (first,)))
    assert _statuses(repo)[first]["resolved"] and not _statuses(repo)[second]["resolved"]
    events = repo.list_trade_events()
    proof = next(row for row in events if row["event_id"] in result["proof_event_ids"])
    at = proof["event_time_ms"]
    void = TradeEvent.from_dict({**proof, "event_id": "void-proof", "event_type": "void",
        "event_time_ms": at + 100, "target_lot_id": None, "target_event_id": proof["event_id"], "raw_payload": {}}).to_dict()
    assert _statuses(repo, at, trade_events=[*events, void])[first]["resolved"]
    assert not _statuses(repo, at + 100, trade_events=[*events, void])[first]["resolved"]


def test_old_invalid_resolution_does_not_block_a_new_complete_proof(tmp_path, monkeypatch):
    from domain.domain.wheel import build_wheel_event
    repo, config = _scope(tmp_path, monkeypatch)
    conflict_id = _conflict(repo, _view(repo, config))
    conflict = next(row for row in repo.list_wheel_events(account="lx") if row["event_id"] == conflict_id)
    bad = build_wheel_event(event_id="old-incomplete-resolution", account="lx",
        lot_id=conflict.get("stock_lot_id"), wheel_branch_id=conflict["wheel_branch_id"],
        event_type="wheel_attribution_conflict_resolved", occurred_at_ms=conflict["occurred_at_ms"],
        recorded_at_ms=conflict["occurred_at_ms"], payload={**conflict["payload"],
            "conflict_event_id": conflict_id, "resolution_evidence_event_id": "absent-proof"})
    with_sqlite_repo_transaction(repo, lambda active, conn: active.append_wheel_event_once(bad, conn=conn))
    assert not _statuses(repo)[conflict_id]["resolved"]
    call = _call(_view(repo, config))
    attribution.apply_trade_attribution(repo, **_args(config, call, call["candidate_ids"][0], "complete", (conflict_id,)))
    assert _statuses(repo)[conflict_id] == {"resolved": True,
        "invalid_resolution_event_ids": ["old-incomplete-resolution"], "execution_keys": [call["execution_key"]]}


@pytest.mark.parametrize("second_target", ["ordinary", "wheel"])
def test_complete_two_member_decision_uses_one_publication(tmp_path, monkeypatch, second_target):
    from dataclasses import replace
    from src.application.ledger.writer import persist_trade_event_object
    from domain.domain.ledger import TradeEvent
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger import trade_attribution as writer
    repo, config = _scope(tmp_path, monkeypatch)
    source = next(row for row in repo.list_trade_events() if row["event_type"] == "open" and row["option_type"] == "call")
    raw = {"side": "sell", "execution_input": deepcopy(source["raw_payload"]["execution_input"])}
    raw["execution_input"]["external_execution_id"] = "second-fill"
    raw["execution_id"] = execution_identity_from_input(raw["execution_input"])
    raw.pop("external_event_key", None)
    persist_trade_event_object(repo, replace(TradeEvent.from_dict(source), event_id="second-fill", raw_payload=raw))
    view = _view(repo, config)
    calls = [row for row in view["rows"] if row["contract_key"]["option_type"] == "call"]
    conflict = _conflict(repo, view, keys=tuple(row["execution_key"] for row in calls))
    call = _call(_view(repo, config))
    args = _args(config, call, "ordinary", "both", (conflict,))
    before = repo.list_trade_events()
    with pytest.raises(ValueError, match="complete member"):
        attribution.apply_trade_attribution(repo, **args)
    publication = writer.run_position_projection_in_transaction
    counts = []
    def publish(*a, **kw):
        counts.append(len(a[1]))
        return publication(*a, **kw)
    monkeypatch.setattr(writer, "run_position_projection_in_transaction", publish)
    result = attribution.apply_trade_attribution(repo, **args, member_decisions=tuple(
        {"execution_key": row["execution_key"], "candidate_id": row["candidate_ids"][0]
         if index == 1 and second_target == "wheel" else "ordinary"} for index, row in enumerate(calls)))
    assert counts == [2]
    assert len(repo.list_trade_events()) == len(before) + 2 and _statuses(repo)[conflict]["resolved"]
    resolution = next(row for row in repo.list_wheel_events(account="lx") if row["event_id"] in result["resolution_event_ids"])
    assert len(resolution["payload"]["resolution_evidence_event_ids"]) == 2
    incomplete = [row for row in repo.list_trade_events() if row["event_id"] != result["proof_event_ids"][0]]
    assert not _statuses(repo, trade_events=incomplete)[conflict]["resolved"]


@pytest.mark.parametrize("overallocated", [False, True])
def test_batch_allocation_checks_final_capacity_after_other_member_is_decided(tmp_path, monkeypatch, overallocated):
    from dataclasses import replace
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from domain.domain.ledger import TradeEvent
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.writer import persist_trade_event_object
    repo, config, _ = _meituan_repo(tmp_path, call_contracts=3)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", lambda **_: {"status": "available", "reason_codes": []})
    source = next(row for row in repo.list_trade_events() if row["event_type"] == "open" and row["option_type"] == "call")
    raw = {"side": "sell", "execution_input": deepcopy(source["raw_payload"]["execution_input"])}
    raw["execution_input"].update(external_execution_id="other-fill", quantity="1")
    raw["execution_id"] = execution_identity_from_input(raw["execution_input"])
    persist_trade_event_object(repo, replace(TradeEvent.from_dict(source), event_id="other-fill", contracts=1, raw_payload=raw))
    current = _view(repo, config)
    calls = [row for row in current["rows"] if row["contract_key"]["option_type"] == "call"]
    conflict = _conflict(repo, current, keys=tuple(row["execution_key"] for row in calls))
    calls = [row for row in _view(repo, config)["rows"] if row["contract_key"]["option_type"] == "call"]
    call = next(row for row in calls if row["contracts"] == 3)
    branches = sorted(item["wheel_branch_id"] for item in call["candidates"] if item["strategy"] == "wheel")[:3]
    if overallocated:
        branches[1] = branches[0]
    target = "wheel-multi:" + canonical_sha256(sorted(branches))[:24]
    args = _args(config, call, target, "batch-allocation", (conflict,))
    args["member_decisions"] = tuple({"execution_key": row["execution_key"],
        "candidate_id": target if row == call else "ordinary",
        "wheel_branch_ids": branches if row == call else []} for row in calls)
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots())
    if overallocated:
        with pytest.raises(ValueError, match="final Wheel capacity"):
            attribution.apply_trade_attribution(repo, **args)
        assert (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()) == before
    else:
        result = attribution.apply_trade_attribution(repo, **args)
        assert len(result["proof_event_ids"]) == 2 and _statuses(repo)[conflict]["resolved"]
        assert sum(row["contracts"] for row in result["wheel_call_allocations"]) == 3


@pytest.mark.parametrize("adoption", ["legacy", "shared"])
@pytest.mark.parametrize("target", ["ordinary", "keep"])
def test_combo_can_only_be_released_as_a_complete_decision(tmp_path, monkeypatch, adoption, target):
    from dataclasses import replace
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.ledger.combo_membership import resolve_combo_group_membership, validate_combo_group_membership
    from test_combo_reconciliation_application import _call_open, _put_open, _reconcile, BASE_TIME_MS
    from test_wheel_workflows import _wheel_repo
    repo, branch_id = _wheel_repo(tmp_path)
    for event in (_call_open(), _put_open()):
        raw = deepcopy(event.raw_payload)
        raw["execution_input"].update(external_id_namespace="futu.deal", external_execution_id=event.event_id)
        raw["execution_id"] = execution_identity_from_input(raw["execution_input"])
        persist_trade_event_object(repo, replace(event, raw_payload=raw))
    pair = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3000)["inferences"][0]
    from src.application.ledger.current_decision_projection import (
        build_current_decision_projection, current_decision_projection_row,
        read_current_decision_projection, compact_assigned_stock_view,
    )
    from src.application.ledger.assigned_stock_projection import project_assigned_stock_lifecycle_from_rows
    with repo._connect() as conn:
        conn.execute("""INSERT OR IGNORE INTO current_decision_input_generations (
            account, generation, case_generation, evidence_generation, allocation_generation,
            source_consumption_generation, timing_generation, combo_identity_generation,
            assigned_stock_generation, updated_at_ms
        ) VALUES ('lx', 0, 0, 0, 0, 0, 0, 0, 0, 1)""")
    assigned = compact_assigned_stock_view(project_assigned_stock_lifecycle_from_rows(
        repo.read_lifecycle_account_rows(account="lx"), account="lx", as_of_ms=BASE_TIME_MS + 3500),
        account="lx", current_position_lots=repo.list_position_lots(), as_of_ms=BASE_TIME_MS + 3500)
    repo.upsert_current_decision_projection(current_decision_projection_row(build_current_decision_projection(
        repo, account="lx", updated_at_ms=BASE_TIME_MS + 3500,
        assigned_stock_after=assigned, all_quality_case_facts=[])))
    config = {"market": "us", "accounts": ["lx"], "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
        "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}}}
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", lambda **_: {"status": "available", "reason_codes": []})
    monkeypatch.setattr(time, "time", lambda: (BASE_TIME_MS + 5000) / 1000)
    def view():
        return attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
            config=config, account="lx", market="us", now_ms=BASE_TIME_MS + 5000,
            combo_evidence={"complete": True, "exposures": []})
    current = view()
    put = next(row for row in current["rows"] if row["lot_id"] == "put-lot")
    if adoption == "legacy":
        _legacy_combo_fixture(repo, pair, BASE_TIME_MS + 4000)
    else:
        attribution.apply_trade_attribution(repo, **{**_args(config, put, "combo:" + pair["strategy_group_id"], "new-pair"), "market": "us"})
    monkeypatch.setattr(time, "time", lambda: (BASE_TIME_MS + 6000) / 1000)
    current = view()
    put = next(row for row in current["rows"] if row["lot_id"] == "put-lot")
    conflict = with_sqlite_repo_transaction(repo, lambda active, conn: record_trade_attribution_conflict(active,
        account="lx", execution_key=put["execution_key"], branch=next(row for row in current["wheel_model"]["wheel_branches"] if row["wheel_branch_id"] == branch_id),
        candidate_ids=["combo:" + pair["strategy_group_id"]], input_hash=put["input_hash"],
        now_ms=BASE_TIME_MS + 5500, conn=conn))
    # Both current view and apply must observe the real conflict before filtering it.
    def view():
        return attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
            config=config, account="lx", market="us", now_ms=BASE_TIME_MS + 6000,
            combo_evidence={"complete": True, "exposures": []})
    current = view()
    put = next(row for row in current["rows"] if row["lot_id"] == "put-lot")
    candidate = "ordinary" if target == "ordinary" else "combo:" + pair["strategy_group_id"]
    args = {**_args(config, put, candidate, "release-pair", (conflict,)), "market": "us"}
    before = repo.list_trade_events()
    with pytest.raises(ValueError, match="complete member"):
        attribution.apply_trade_attribution(repo, **args)
    assert repo.list_trade_events() == before
    result = attribution.apply_trade_attribution(repo, **args, member_decisions=tuple(
        {"execution_key": row["execution_key"], "candidate_id": candidate} for row in current["rows"] if row["strategy_group_id"] == pair["strategy_group_id"]))
    assert result["write_applied"]
    membership = resolve_combo_group_membership(group_id=pair["strategy_group_id"], account="lx",
        trade_events=repo.list_trade_events(), projected_position_lots=repo.list_position_lots())
    assert membership.fact["status"] == ("released" if target == "ordinary" else "exact")
    assert membership.global_historical_lot_ids == ("call-lot", "put-lot")
    assert validate_combo_group_membership(membership.fact).status == "valid"
    assert len(repo.list_strategy_group_identities(account="lx")) == 1
    # A later non-strategy adjustment must not hide either member's proof.
    persist_trade_event_object(repo, replace(_put_open(), event_id="later-adjustment", event_type="adjust",
        event_time_ms=BASE_TIME_MS + 6500, lot_id=None, target_lot_id="put-lot", contracts=0, price=0,
        raw_payload={"adjust_target_source_event_id": "put-open", "patch": {"last_action_at": BASE_TIME_MS + 6500}}))
    projected = read_current_decision_projection(repo, account="lx", now_ms=BASE_TIME_MS + 6500)
    groups = projected["payload"]["combo"]["current_groups"]
    assert len(groups) == (0 if target == "ordinary" else 1)
    if groups:
        assert groups[0]["status"] == "active_combo"
    if target == "keep":
        from src.application.ledger.combo_membership import resolve_combo_assignment_proof
        assignment = replace(_put_open(), event_id="later-assignment", event_type="assignment",
            event_time_ms=BASE_TIME_MS + 7000, lot_id=None, target_lot_id="put-lot", raw_payload={"side": "buy"})
        assert resolve_combo_assignment_proof(assignment=assignment, group_id=pair["strategy_group_id"],
            trade_events=repo.list_trade_events(), identities=repo.list_strategy_group_identities(account="lx")) == ("csp_lc", None)
        # Every consumer must reject a complete manifest whose prior state never existed.
        from src.application.ledger.combo_membership import _controlled_pair_adoption
        invalid = deepcopy(repo.list_trade_events())
        for row in invalid:
            if row["event_id"] in result["proof_event_ids"]:
                row["raw_payload"]["attribution_decision"]["members"][0]["before"]["strategy_snapshot"] = {"never": "existed"}
        assert resolve_combo_group_membership(group_id=pair["strategy_group_id"], account="lx",
            trade_events=invalid, projected_position_lots=repo.list_position_lots()).fact["status"] == "conflict"
        assert resolve_combo_assignment_proof(assignment=assignment, group_id=pair["strategy_group_id"],
            trade_events=invalid, identities=repo.list_strategy_group_identities(account="lx"))[0] is None
        binding = next(row for row in membership.fact["member_bindings_for_current_account"] if row["record_id"] == "put-lot")
        assert _controlled_pair_adoption(invalid, binding=binding, opening=_put_open(),
            group_id=pair["strategy_group_id"], role="funding_put", instant=assignment.event_time_ms,
            voided_ids=set()) is None
        from domain.domain.ledger import TradeEvent
        proof = next(row for row in repo.list_trade_events() if row["event_id"] == result["proof_event_ids"][0])
        future_void = replace(TradeEvent.from_dict(proof), event_id="future-proof-void", event_type="void",
            event_time_ms=BASE_TIME_MS + 8000, target_lot_id=None, target_event_id=proof["event_id"], raw_payload={}).to_dict()
        history = [*repo.list_trade_events(), future_void]
        assert resolve_combo_assignment_proof(assignment=assignment, group_id=pair["strategy_group_id"],
            trade_events=history, identities=repo.list_strategy_group_identities(account="lx")) == ("csp_lc", None)
        assert resolve_combo_assignment_proof(assignment=replace(assignment, event_time_ms=BASE_TIME_MS + 9000),
            group_id=pair["strategy_group_id"], trade_events=history,
            identities=repo.list_strategy_group_identities(account="lx"))[0] is None


def _legacy_combo_fixture(repo, pair, instant):
    """Historical pre-manifest facts, not a second confirmation workflow."""
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.combo_membership import publish_combo_pair_identity
    from src.application.ledger.position_projection_runtime import run_position_projection_in_transaction
    from src.application.ledger.current_decision_projection import capture_trade_event_decision_projection_fence
    from src.application.ledger.writer_decision import _finish_trade_event_decision_projection
    events = {event["event_id"]: event for event in repo.list_trade_events()}
    proof_ids = {}
    proofs = []
    for prefix, role in (("put", "funding_put"), ("call", "participation_call")):
        source = events[pair[prefix + "_open_event_id"]]
        lot = pair[prefix + "_record_id"]
        event_id = "combo-adopt:v1:" + canonical_sha256({"inference_id": pair["inference_id"], "role": role})
        proof_ids[prefix] = event_id
        proofs.append(TradeEvent(event_id=event_id, event_type="adjust", event_time_ms=instant,
            contract_key=ContractKey.from_values(**source["contract_key"]), contracts=0, price=0,
            currency=source["currency"], multiplier=source["multiplier"], target_lot_id=lot,
            source="post_trade_combo_reconciliation", raw_payload={"source": "post_trade_combo_reconciliation",
                "source_type": "combo_pair_inference", "mode": "post_trade_combo_adoption",
                "inference_id": pair["inference_id"], "record_id": lot, "target_lot_id": lot,
                "adjust_target_source_event_id": source["event_id"], "idempotency_key": event_id,
                "patch": {"strategy": "combo_yield", "strategy_group_id": pair["strategy_group_id"],
                    "leg_role": role, "last_action_at": instant}}))
    with repo._writer_connection(begin_immediate=True) as conn:
        fence = capture_trade_event_decision_projection_fence(repo, conn=conn)
        runtime = run_position_projection_in_transaction(repo, proofs, conn=conn, mode="forced_full")
        identity, _ = publish_combo_pair_identity(repo, conn=conn, inference=pair)
        repo.transition_combo_pair_inference(inference_id=pair["inference_id"], expected_statuses=[pair["status"]],
            new_status="user_confirmed", expected_input_hash=pair["input_snapshot_hash"], decision_fields={
                "decision_at_ms": instant, "decision_by": "historical-fixture", "decision_reason": "user_confirmed_exact_pair",
                "strategy_group_id": pair["strategy_group_id"], "identity_hash": identity["identity_hash"],
                "put_adoption_event_id": proof_ids["put"], "call_adoption_event_id": proof_ids["call"]}, conn=conn)
        _finish_trade_event_decision_projection(repo, conn=conn, fence=fence, events=proofs, created_flags=runtime.created_flags)


@pytest.mark.parametrize("target_kind,next_kind", [("wheel", "wheel"), ("ordinary", "ordinary"), ("ordinary", "wheel")])
def test_voided_prior_adjustment_rejects_keep_proof_and_request_recovery(tmp_path, monkeypatch, target_kind, next_kind):
    from dataclasses import replace
    from domain.domain.ledger import TradeEvent
    from domain.domain.wheel import lot_strategy_metadata_from_trade_events
    from src.application.ledger.interventions import persist_manual_void_event
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.ledger.api import assert_trade_attribution_unclaimed, trade_attribution_facts_from_events

    repo, config = _scope(tmp_path, monkeypatch)
    now = int(time.time() * 1000) + 100
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    call = _call(_view(repo, config))
    target = call["candidate_ids"][0] if target_kind == "wheel" else "ordinary"
    initial_args = _args(config, call, target, "initial")
    initial = attribution.apply_trade_attribution(repo, **initial_args)
    next_target = call["candidate_ids"][0] if next_kind == "wheel" else "ordinary"
    opening = next(row for row in repo.list_trade_events() if row["event_id"] == call["open_event_id"])
    now += 10
    persist_trade_event_object(repo, replace(TradeEvent.from_dict(opening), event_id="prior-snapshot",
        event_type="adjust", event_time_ms=now, lot_id=None, target_lot_id=call["lot_id"],
        contracts=0, price=0, fees=0, source="om option-positions",
        raw_payload={"adjust_target_source_event_id": call["open_event_id"],
                     "patch": {"strategy_snapshot": {"audit_note": "example"}}}))
    now += 10
    conflict = _conflict(repo, _view(repo, config), suffix="rejected-proof")
    now += 10
    args = _args(config, _call(_view(repo, config)), next_target, "keep", (conflict,))
    result = attribution.apply_trade_attribution(repo, **args)
    assert _statuses(repo)[conflict]["resolved"]
    before_void = _call({"rows": trade_attribution_facts_from_events(repo.list_trade_events(), account="lx")})
    assert before_void["status"] == ("linked" if next_kind == "wheel" else "ordinary")
    if next_kind == "wheel":
        with pytest.raises(ValueError, match="already claimed"):
            assert_trade_attribution_unclaimed(repo.list_trade_events(), [call["lot_id"]])
    # A non-strategy fact does not revoke a valid durable decision.
    now += 10
    persist_trade_event_object(repo, replace(TradeEvent.from_dict(opening), event_id="later-audit",
        event_type="adjust", event_time_ms=now, lot_id=None, target_lot_id=call["lot_id"],
        contracts=0, price=0, fees=0, source="om option-positions",
        raw_payload={"adjust_target_source_event_id": call["open_event_id"], "patch": {"last_action_at": now}}))
    assert not attribution.apply_trade_attribution(repo, **args)["write_applied"]
    now += 10
    assert persist_manual_void_event(repo, target_event_id="prior-snapshot", void_reason="correct snapshot",
                                     as_of_ms=now).created
    events = repo.list_trade_events()
    accepted = set()
    current = lot_strategy_metadata_from_trade_events(events, accepted_proof_event_ids=accepted)
    assert set(initial["proof_event_ids"]) <= accepted
    assert not accepted.intersection(result["proof_event_ids"])
    assert current == lot_strategy_metadata_from_trade_events(
        [row for row in events if row["event_id"] not in result["proof_event_ids"]])
    assert not _statuses(repo)[conflict]["resolved"]
    with pytest.raises(ValueError, match="not accepted by replay"):
        attribution.apply_trade_attribution(repo, **args)
    fact = _call({"rows": trade_attribution_facts_from_events(events, account="lx")})
    assert "test:rejected-proof" not in fact["acknowledged_candidate_ids"]
    if target_kind == "ordinary":
        assert fact["status"] == "ordinary" and fact["origin"] == "manual"
        retry = attribution.apply_trade_attribution(repo, **initial_args)
        assert retry["status"] == "ordinary" and not retry["write_applied"]
        assert retry["proof_event_ids"] == initial["proof_event_ids"]
        with pytest.raises(ValueError, match="manually excluded"):
            assert_trade_attribution_unclaimed(events, [call["lot_id"]])


def test_assignment_companion_keeps_historical_internal_transition_after_future_proof_void(tmp_path, monkeypatch):
    from dataclasses import replace
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.event_codec import valid_void_target_event_id
    from src.application.ledger.wheel_trade_companions import plan_wheel_assignment_companion

    repo, config = _scope(tmp_path, monkeypatch)
    now = int(time.time() * 1000) + 100
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    call = _call(_view(repo, config))
    result = attribution.apply_trade_attribution(repo, **_args(config, call, call["candidate_ids"][0], "initial"))
    rows = repo.read_lifecycle_account_rows(account="lx")
    opening = next(TradeEvent.from_dict(row) for row in rows["trade_events"] if row["event_id"] == call["open_event_id"])
    assignment = replace(opening, event_id="historical-call-assignment", event_type="assignment",
        event_time_ms=now + 10, lot_id=None, target_lot_id=call["lot_id"], raw_payload={"side": "buy",
            "stock_settlement": {"side": "sell", "shares": opening.contracts * opening.multiplier,
                "price": float(opening.contract_key.strike), "fees": 0, "currency": opening.currency,
                "fee_provenance": {"basis": "actual", "source": "test"}}})
    fields = repo.get_position_lot_fields(call["lot_id"])
    window = repo.get_current_wheel_activation_window(market="hk", account="lx")
    before = plan_wheel_assignment_companion(assignment, fields, rows, window, recorded_at_ms=now + 20)
    assert before[1] is None
    assert before[0]["payload"]["parent_branch_id"]
    assert before[0]["payload"]["initial_lifecycle_status"] == "pending_decision"
    proof = next(row for row in rows["trade_events"] if row["event_id"] in result["proof_event_ids"])
    void = replace(TradeEvent.from_dict(proof), event_id="future-proof-void", event_type="void",
        event_time_ms=now + 100, target_lot_id=None, target_event_id=proof["event_id"], raw_payload={}).to_dict()
    assert valid_void_target_event_id(void) == proof["event_id"]
    after = plan_wheel_assignment_companion(assignment, fields,
        {**rows, "trade_events": [*rows["trade_events"], void]}, window, recorded_at_ms=now + 20)
    assert after == before


@pytest.mark.parametrize("legacy", [False, True])
def test_full_history_companion_projection_ignores_future_strategy_proof_and_void(tmp_path, monkeypatch, legacy):
    from dataclasses import replace
    from domain.domain.ledger import TradeEvent
    from domain.domain.wheel import build_legacy_wheel_event, build_wheel_event
    from src.application.ledger.wheel_assignment_recovery import _wheel_branches_from_rows

    repo, config = _scope(tmp_path, monkeypatch)
    now = int(time.time() * 1000) + 100
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    call = _call(_view(repo, config))
    target = call["candidate_ids"][0]
    attribution.apply_trade_attribution(repo, **_args(config, call, target, "initial"))
    now += 10
    conflict = _conflict(repo, _view(repo, config))
    now += 10
    result = attribution.apply_trade_attribution(repo, **_args(config, _call(_view(repo, config)), target, "keep", (conflict,)))
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    proof = next(row for row in rows["trade_events"] if row["event_id"] in result["proof_event_ids"])
    at = proof["event_time_ms"]
    branch_id = next(row for row in rows["account_wheel_events"] if row["event_id"] == conflict)["wheel_branch_id"]
    if legacy:
        creation = next(row for row in rows["account_wheel_events"] if row["wheel_branch_id"] == branch_id
                        and row["event_type"] == "wheel_branch_created")
        rows["account_wheel_events"] = [row for row in rows["account_wheel_events"] if row != creation]
        rows["account_wheel_events"].append(build_legacy_wheel_event(event_id="legacy-start",
            account="lx", lot_id=creation["stock_lot_id"],
            event_type="wheel_started", occurred_at_ms=creation["occurred_at_ms"],
            recorded_at_ms=creation["recorded_at_ms"], source_trade_event_id=creation["source_trade_event_id"],
            payload={"request_id": "legacy-fixture"}))
    def projected(history):
        return next(row for row in _wheel_branches_from_rows(history, account="lx", as_of_ms=at)
                    if row["wheel_branch_id"] == branch_id)
    before = projected(rows)
    assert before["integrity_status"] == "trusted"
    void = replace(TradeEvent.from_dict(proof), event_id="future-proof-void", event_type="void",
        event_time_ms=at + 100, target_lot_id=None, target_event_id=proof["event_id"], raw_payload={}).to_dict()
    after = projected({**rows, "trade_events": [*rows["trade_events"], void]})
    assert after == before
    now += 200
    later_conflict = _conflict(repo, _view(repo, config), suffix="release")
    now += 10
    attribution.apply_trade_attribution(repo, **_args(config, _call(_view(repo, config)), target, "later-keep", (later_conflict,)))
    after = projected({**rows, "trade_events": repo.list_trade_events()})
    assert after == before


def test_writer_rejects_same_time_keep_sorted_before_its_prior_decision(tmp_path, monkeypatch):
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from domain.domain.strategy_membership import POSITION_LOT_STRATEGY_PATCH_FIELDS
    from domain.domain.wheel import lot_strategy_metadata_from_trade_events
    from src.application.ledger.trade_attribution import write_trade_attribution_decision

    repo, config = _scope(tmp_path, monkeypatch)
    now = int(time.time() * 1000) + 100
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    call = _call(_view(repo, config))
    target = call["candidate_ids"][0]
    initial = attribution.apply_trade_attribution(repo, **_args(config, call, target, "initial"))
    request = next(str(index) for index in range(100) if "trade-attribution:" + canonical_sha256({
        "account": "lx", "request_id": str(index), "open_event_id": call["open_event_id"]}) < initial["proof_event_ids"][0])
    before = repo.list_trade_events()
    metadata = lot_strategy_metadata_from_trade_events(before)[call["lot_id"]]
    with pytest.raises(ValueError, match="proof readback was not accepted"):
        with_sqlite_repo_transaction(repo, lambda active, conn: write_trade_attribution_decision(active,
            conn=conn, account="lx", request_id=request, actor="fixture:operator", input_hash=call["input_hash"],
            plans=[{"fact": call, "patch": {key: metadata.get(key) for key in POSITION_LOT_STRATEGY_PATCH_FIELDS},
                    "action": "wheel", "candidate_id": target}], conflicts=[], branch_generations={}, now_ms=now,
            manual=True))
    assert repo.list_trade_events() == before
