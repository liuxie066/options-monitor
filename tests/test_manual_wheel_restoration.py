"""Restore a complete original manual decision without inventing recommendation history."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import TradeEvent
from src.application.futu_portfolio_context import build_futu_position_snapshot
from src.application.ledger import api, trade_attribution as owner
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from test_attribution_conflict_decision import _conflict, _statuses
from test_trade_attribution_full_branches import _ingest, EVIDENCE, NOW
from test_trade_attribution_meituan import _meituan_repo, _ms


def _capacity(repo, now=NOW):
    quantities = defaultdict(int)
    for fact in api.read_trade_attribution_facts(repo, account="lx"):
        if fact["contracts_open"]:
            contract = fact["contract_key"]
            quantities[(contract["expiration_ymd"], contract["strike"])] -= fact["contracts_open"]
    positions = [{"code": "HK.03690", "sec_type": "STOCK", "qty": 2500, "can_sell_qty": 0}]
    for (expiry, strike), quantity in quantities.items():
        positions.append({"code": f"HK.03690{expiry.replace('-', '')[2:]}C{int(float(strike) * 1000):08d}",
            "stock_owner": "HK.03690", "sec_type": "OPTION", "qty": quantity, "option_type": "CALL",
            "option_strike_price": strike, "strike_time": expiry, "multiplier": 500})
    snapshot = build_futu_position_snapshot(rows=positions,
        broker_account_ref={"broker_id": "futu", "external_account_id": "1001", "environment": "REAL",
            "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["HK"], asset_types=["stock", "option"], completeness="complete",
        observed_at_utc=datetime.fromtimestamp(now / 1000, timezone.utc).isoformat())
    return {"portfolio": {"capacity_authority": {"status": "available", "logical_account": "lx",
        "futu_account_id": "1001", "trd_env": "REAL", "market": "hk"}, "position_snapshot_input": snapshot}}


def _view(repo, config, *, complete=False, observation=None, now=NOW):
    return api.build_trade_attribution_view(api.read_trade_attribution_snapshot(repo, account="lx", market="hk"),
        config=config, account="lx", market="hk", now_ms=now, combo_evidence={"complete": complete, "exposures": []},
        capacity_observation=_capacity(repo, now) if observation is None else observation, combo_mode="confirm")


@pytest.fixture
def restoration_scope(tmp_path, monkeypatch):
    initial = _ms("2026-09-28T00:00:00Z")
    monkeypatch.setattr("time.time", lambda: initial / 1000)
    repo, config, _ = _meituan_repo(tmp_path, call_contracts=3)
    view = _view(repo, config, complete=True, now=initial)
    multi = next(row for row in view["rows"] if row["contracts_open"] == 3)
    branches = sorted(candidate["wheel_branch_id"] for candidate in multi["candidates"])
    selected = tuple(branches[:3])
    api.apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=multi["execution_key"], candidate_id="wheel-multi:" + canonical_sha256(sorted(selected))[:24],
        expected_input_hash=multi["input_hash"], request_id="original:multi", actor="test:operator",
        combo_evidence=EVIDENCE, capacity_observation=_capacity(repo, initial), combo_mode="confirm", manual=True,
        wheel_branch_ids=selected)
    for name, stamp, branch in (("prior-single", "2026-09-29T00:00:00Z", branches[3]),
                                ("new-later", "2026-10-06T07:34:03Z", branches[4])):
        instant = _ms(stamp) + 1000
        monkeypatch.setattr("time.time", lambda instant=instant: instant / 1000)
        execution, _ = _ingest(repo, config, tmp_path, deal_id=name, occurred_at=stamp)
        row = next(row for row in _view(repo, config, complete=True, now=instant)["rows"]
                   if row["execution_key"] == execution)
        api.apply_trade_attribution(repo, account="lx", market="hk", config=config,
            execution_key=execution, candidate_id="wheel:" + branch, expected_input_hash=row["input_hash"],
            request_id="original:" + name, actor="test:operator", combo_evidence=EVIDENCE,
            capacity_observation=_capacity(repo, instant), combo_mode="confirm", manual=True)
    monkeypatch.setattr("time.time", lambda: NOW / 1000)
    original = [row for row in api.read_trade_attribution_facts(repo, account="lx") if row["contracts_open"]]
    assert len(original) == 3 and all(row["origin"] == "manual" for row in original)
    conflict = _conflict(repo, _view(repo, config, complete=True), keys=tuple(row["execution_key"] for row in original))
    return repo, config, original, conflict


def _args(scope, *, observation=None):
    repo, config, original, conflict = scope
    view = _view(repo, config, observation=observation)
    members = []
    for fact in original:
        allocations = deepcopy(fact.get("wheel_call_allocations") or [])
        branches = [row["wheel_branch_id"] for row in allocations for _ in range(row["contracts"])]
        item = {"execution_key": fact["execution_key"], "candidate_id":
            "wheel-multi:" + canonical_sha256(sorted(branches))[:24] if branches else "wheel:" + fact["wheel_branch_id"]}
        if allocations:
            item.update(wheel_branch_ids=branches, wheel_call_allocations=allocations)
        members.append(item)
    primary_key = next(row["execution_key"] for row in original if row["contracts"] == 3)
    primary = next(row for row in view["rows"] if row["execution_key"] == primary_key)
    target = next(item for item in members if item["execution_key"] == primary["execution_key"])
    assert not primary["evidence_complete"]
    return dict(account="lx", market="hk", config=config, execution_key=primary["execution_key"],
        candidate_id=target["candidate_id"], expected_input_hash=primary["input_hash"],
        request_id="restore:complete", actor="test:operator", manual=True, combo_mode="confirm",
        combo_evidence={"complete": False, "exposures": []},
        capacity_observation=_capacity(repo) if observation is None else observation,
        conflict_event_ids=(conflict,), member_decisions=tuple(members))


def _state(repo):
    return deepcopy((repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()))


def _legacy_proofs(repo):
    # Isolated historical fixtures reproduce the retired writers' exact known schemas.
    for proof in repo.list_trade_events():
        raw = proof.get("raw_payload") or {}
        if raw.get("attribution_request_id") not in {"original:multi", "original:prior-single"}:
            continue
        patch = raw["patch"]
        legacy = {"actor": raw["actor"], "target_lot_id": proof["target_lot_id"],
            "adjust_target_source_event_id": raw["adjust_target_source_event_id"],
            "wheel_linkage_request_id": raw["attribution_request_id"], "input_snapshot_hash": "a" * 64,
            "patch": patch}
        if patch.get("wheel_call_allocations"):
            legacy.update(schema_version="wheel_call_allocations_confirmed.v1", attribution_origin="manual",
                attribution_request_id=raw["attribution_request_id"], attribution_policy_version=owner.ATTRIBUTION_POLICY_VERSION,
                attribution_candidate_id=raw["attribution_candidate_id"], attribution_candidate_ids=raw["attribution_candidate_ids"])
        else:
            legacy.update(schema_version="wheel_call_linkage_confirmed.v1", batch_generation_hash="b" * 64,
                linkage_candidate_id="legacy:candidate", source="wheel_linkage",
                source_wheel_branch_id=patch["source_wheel_branch_id"], source_stock_lot_id=patch["source_stock_lot_id"])
        _replace_raw(repo, proof, legacy)


def _replace_raw(repo, proof, raw):
    import json
    proof = {**proof, "source": "wheel_linkage", "raw_payload": raw}
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("UPDATE trade_events SET event_json=? WHERE event_id=?", (json.dumps(proof), proof["event_id"]))


@pytest.mark.parametrize("legacy", [False, True])
def test_complete_three_member_restore_preserves_original_allocations_and_economics(restoration_scope, legacy):
    repo, config, original, conflict = restoration_scope
    if legacy:
        _legacy_proofs(repo)
    args = _args(restoration_scope)
    before = _state(repo)
    preview = api.apply_trade_attribution(repo, **args, apply_changes=False)
    assert not preview["write_applied"] and preview["evidence_complete"] is False
    assert _state(repo) == before
    applied = api.apply_trade_attribution(repo, **args)
    assert applied["write_applied"] and len(applied["proof_event_ids"]) == 3
    assert _statuses(repo)[conflict]["resolved"]
    after = repo.list_trade_events()
    assert after[:len(before[0])] == before[0] and len(after) == len(before[0]) + 3
    assert all(row["event_type"] == "adjust" and row["contracts"] == float(row["price"]) == float(row["fees"]) == 0
               for row in after[len(before[0]):])
    assert len([row for row in repo.list_wheel_events(account="lx") if row["event_type"] == "wheel_attribution_conflict_resolved"]) == 1
    current = {row["execution_key"]: row for row in api.read_trade_attribution_facts(repo, account="lx")}
    for fact in original:
        assert all(current[fact["execution_key"]].get(key) == fact.get(key) for key in
                   ("contracts", "contracts_open", "price", "open_event_id", "wheel_branch_id", "wheel_call_allocations"))
    branches = _view(repo, config)["wheel_model"]["wheel_branches"]
    assert len(branches) == 5 and all(branch["active_option_committed_shares"] == 500 for branch in branches)
    stable = _state(repo)
    assert not api.apply_trade_attribution(SQLiteOptionPositionsRepository(repo.db_path), **args)["write_applied"]
    assert _state(repo) == stable


@pytest.mark.parametrize("bad", ["missing_member", "extra_member", "stock", "omit_allocations", "move", "automatic", "hash"])
def test_history_independent_restore_requires_exact_complete_manual_batch(restoration_scope, bad):
    args = _args(restoration_scope)
    members = [deepcopy(item) for item in args["member_decisions"]]
    multi = next(item for item in members if item.get("wheel_call_allocations"))
    single = next(item for item in members if not item.get("wheel_call_allocations"))
    if bad == "missing_member":
        members.remove(single)
    elif bad == "extra_member":
        members.append({"execution_key": "absent", "candidate_id": "ordinary"})
    elif bad == "stock":
        multi["wheel_call_allocations"][0]["stock_lot_id"] = "different:stock"
    elif bad == "omit_allocations":
        multi.pop("wheel_call_allocations")
    elif bad == "move":
        single["candidate_id"] = "ordinary"
    elif bad == "automatic":
        args["manual"] = False
    else:
        args["expected_input_hash"] = "stale"
    args["member_decisions"] = tuple(members)
    before = _state(restoration_scope[0])
    with pytest.raises(ValueError):
        api.apply_trade_attribution(restoration_scope[0], **args)
    assert _state(restoration_scope[0]) == before


@pytest.mark.parametrize("bad", ["absent", "stale", "partial", "physical", "quantity", "stock_shortage", "config"])
def test_original_manual_proof_does_not_replace_current_capacity(restoration_scope, bad):
    repo = restoration_scope[0]
    observation = _capacity(repo)
    snapshot = observation["portfolio"]["position_snapshot_input"]
    if bad == "absent":
        observation = {}
    elif bad == "stale":
        snapshot["observed_at_utc"] = "2020-01-01T00:00:00+00:00"
    elif bad == "partial":
        snapshot["completeness"] = "partial"
    elif bad == "physical":
        snapshot["broker_account_ref"]["external_account_id"] = "different"
    elif bad == "quantity":
        snapshot["rows"][1]["quantity"] = "999"
    elif bad == "stock_shortage":
        snapshot["rows"][0]["quantity"] = "0"
    args = _args(restoration_scope, observation=observation)
    if bad == "config":
        args["config"] = deepcopy(args["config"])
        args["config"]["account_settings"]["lx"]["futu"]["account_id"] = "other"
    before = _state(repo)
    with pytest.raises(ValueError):
        api.apply_trade_attribution(repo, **args)
    assert _state(repo) == before


@pytest.mark.parametrize("bad", ["superseded", "void", "close", "legacy_request", "legacy_policy", "legacy_source"])
def test_last_effective_proof_and_fully_open_source_are_required(restoration_scope, bad):
    repo, _config, original, _conflict_id = restoration_scope
    if bad.startswith("legacy"):
        _legacy_proofs(repo)
    multi = next(fact for fact in original if fact["contracts"] == 3)
    proof = next(row for row in repo.list_trade_events() if row.get("target_lot_id") == multi["lot_id"])
    if bad == "superseded":
        # Even an identical unconfirmed later patch cannot borrow an older manual origin.
        persist_trade_event_object(repo, replace(TradeEvent.from_dict(proof), event_id="later:plain-adjust",
            event_time_ms=NOW - 1, source="test", raw_payload={"patch": deepcopy(proof["raw_payload"]["patch"])}))
    elif bad == "void":
        persist_trade_event_object(repo, replace(TradeEvent.from_dict(proof), event_id="void:original",
            event_type="void", event_time_ms=NOW - 1, target_lot_id=None, target_event_id=proof["event_id"], raw_payload={}))
    elif bad == "close":
        persist_trade_event_object(repo, replace(TradeEvent.from_dict(proof), event_id="close:one",
            event_type="close", contracts=1, event_time_ms=NOW - 1, source="test", raw_payload={"side": "buy"}))
    else:
        raw = deepcopy(proof["raw_payload"])
        raw["attribution_request_id" if bad == "legacy_request" else "attribution_policy_version"] = "wrong"
        if bad == "legacy_source":
            raw["adjust_target_source_event_id"] = "wrong-opening"
        _replace_raw(repo, proof, raw)
    args = _args(restoration_scope)
    before = _state(repo)
    with pytest.raises(ValueError):
        api.apply_trade_attribution(repo, **args)
    assert _state(repo) == before


@pytest.mark.parametrize("failure", ["cancel", "expire"])
def test_restore_rolls_back_all_members_when_final_validation_fails(restoration_scope, monkeypatch, failure):
    repo = restoration_scope[0]
    args = _args(restoration_scope)
    before = _state(repo)
    writer = owner.write_trade_attribution_decision
    stop = Event()
    def change_after_write(*a, **kw):
        result = writer(*a, **kw)
        if failure == "cancel":
            stop.set()
        else:
            monkeypatch.setattr("time.time", lambda: (NOW + 61000) / 1000)
        return result
    monkeypatch.setattr(owner, "write_trade_attribution_decision", change_after_write)
    with pytest.raises(ValueError, match="cancelled|capacity changed"):
        api.apply_trade_attribution(repo, **args, stop_event=stop)
    assert _state(repo) == before
