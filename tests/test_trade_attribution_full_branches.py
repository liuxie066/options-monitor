"""Full Wheel branches must not compete with the only unclaimed Call target."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.trade_execution import execution_identity_from_input
from src.application.futu_portfolio_context import build_futu_position_snapshot
from src.application.ledger.api import read_trade_attribution_facts, read_trade_attribution_snapshot
from src.application.trades import attribution
from src.application.trades.auto_intake import _process_payload
from src.application.wheel.capacity import trade_attribution_capacity_check
from test_trade_attribution_meituan import _call_payload, _meituan_repo, _ms

EVIDENCE = {"complete": True, "exposures": []}
NOW = _ms("2026-10-06T07:34:10Z")


def _ingest(repo, config, tmp_path, *, deal_id, occurred_at, contracts=1):
    payload = _call_payload(deal_id=deal_id, contracts=contracts)
    payload["occurred_at_utc"] = occurred_at
    if deal_id.startswith("new-"):
        payload["instrument_ref"].update(strike="82.5", expiration_ymd="2026-12-30")
    result = _process_payload(payload, repo=repo, state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl", account_mapping={"1001": "lx"}, futu_account_ids=["1001"],
        apply_changes=True, host="127.0.0.1", port=11111, allow_external_lookup=False,
        config=config, runtime_root=tmp_path)
    assert result["status"] == "applied", result
    return execution_identity_from_input(payload), result


def _view(repo, config, *, now_ms=NOW, evidence=EVIDENCE, observation=None):
    return attribution.build_trade_attribution_view(
        read_trade_attribution_snapshot(repo, account="lx", market="hk"),
        config=config, account="lx", market="hk", now_ms=now_ms, combo_evidence=evidence,
        capacity_observation=observation or {}, combo_mode="confirm")


def _observation(repo, *, now_ms=NOW):
    positions = [{"code": "HK.03690", "sec_type": "STOCK", "qty": 2500, "can_sell_qty": 0}]
    for fact in read_trade_attribution_facts(repo, account="lx"):
        if fact["contracts_open"] <= 0:
            continue
        contract = fact["contract_key"]
        expiry = contract["expiration_ymd"]
        strike = contract["strike"]
        positions.append({"code": f"HK.03690{expiry.replace('-', '')[2:]}C{int(float(strike) * 1000):08d}",
            "stock_owner": "HK.03690", "sec_type": "OPTION", "qty": -fact["contracts_open"],
            "option_type": "CALL", "option_strike_price": strike, "strike_time": expiry, "multiplier": 500})
    snapshot = build_futu_position_snapshot(rows=positions,
        broker_account_ref={"broker_id": "futu", "external_account_id": "1001", "environment": "REAL",
            "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["HK"], asset_types=["stock", "option"], completeness="complete",
        observed_at_utc=datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat())
    return {"portfolio": {"capacity_authority": {"status": "available", "logical_account": "lx",
        "futu_account_id": "1001", "trd_env": "REAL", "market": "hk"}, "position_snapshot_input": snapshot}}


def _occupied_scope(tmp_path, monkeypatch, *, covered=4):
    # Reuse real ingress, five Put assignments and the existing multi-allocation writer.
    monkeypatch.setattr(attribution.time, "time", lambda: _ms("2026-09-28T00:00:00Z") / 1000)
    repo, config, _ = _meituan_repo(tmp_path, call_contracts=3)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
        lambda **_: {"status": "available", "reason_codes": []})
    view = _view(repo, config, now_ms=_ms("2026-09-28T00:00:00Z"))
    call = next(row for row in view["rows"] if row["contracts_open"] == 3)
    branches = sorted(candidate["wheel_branch_id"] for candidate in call["candidates"])
    chosen = tuple(branches[:3])
    attribution.apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=call["execution_key"], candidate_id="wheel-multi:" + canonical_sha256(sorted(chosen))[:24],
        expected_input_hash=call["input_hash"], request_id="fixture:three", actor="fixture:operator",
        combo_evidence=EVIDENCE, capacity_observation={}, combo_mode="confirm", manual=True,
        wheel_branch_ids=chosen)
    if covered == 4:
        monkeypatch.setattr(attribution.time, "time", lambda: _ms("2026-09-29T00:00:10Z") / 1000)
        execution, _ = _ingest(repo, config, tmp_path, deal_id="prior-single",
            occurred_at="2026-09-29T00:00:00Z")
        view = _view(repo, config, now_ms=_ms("2026-09-29T00:00:10Z"))
        call = next(row for row in view["rows"] if row["execution_key"] == execution)
        attribution.apply_trade_attribution(repo, account="lx", market="hk", config=config,
            execution_key=execution, candidate_id="wheel:" + branches[3], expected_input_hash=call["input_hash"],
            request_id="fixture:single", actor="fixture:operator", combo_evidence=EVIDENCE,
            capacity_observation={}, combo_mode="confirm", manual=True)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker, physical_account_id, environment, account, market, policy_version,
             effective_from_ms, created_at_ms, actor, request_id, request_hash)
            VALUES ('futu', '1001', 'REAL', 'lx', 'hk', 'trade_attribution.v2', ?, ?,
                    'fixture', 'fixture:cutover', ?)""", (_ms("2026-10-01T00:00:00Z"),
                    _ms("2026-10-01T00:00:00Z"), "a" * 64))
    monkeypatch.setattr(attribution.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
    execution, receipt = _ingest(repo, config, tmp_path, deal_id="new-call",
        occurred_at="2026-10-06T07:34:03Z")
    return repo, config, branches, execution, receipt


def test_only_unclaimed_branch_is_selected_and_existing_links_do_not_conflict(tmp_path, monkeypatch):
    repo, config, branches, execution, receipt = _occupied_scope(tmp_path, monkeypatch)
    view = _view(repo, config, observation=_observation(repo))
    assert [branch["coverage"]["available_shares"] for branch in
            sorted(view["wheel_model"]["wheel_branches"], key=lambda row: row["wheel_branch_id"])] == [0, 0, 0, 0, 500]
    target = next(row for row in view["rows"] if row["execution_key"] == execution)
    assert target["candidate_ids"] == ["wheel:" + branches[-1]]
    assert target["selected_candidate_id"] == "wheel:" + branches[-1]
    assert target["status"] == "pending" and target["reason_codes"] == ["awaiting_ledger_commit"]
    assert all(row["status"] == "linked" for row in view["rows"]
               if row["contracts_open"] > 0 and row["execution_key"] != execution)
    # Ingress does not observe live broker capacity; the receipt must remain pending.
    assert receipt["attribution_result"]["status"] == "pending"
    assert receipt["attribution_result"]["candidate_ids"] == ["wheel:" + branches[-1]]


def test_reconcile_links_last_branch_once_and_preserves_economics(tmp_path, monkeypatch):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch)
    observation = _observation(repo)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **kw: deepcopy(EVIDENCE))
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **_: observation)
    before = repo.list_trade_events()
    opens = [row for row in before if row["event_type"] == "open"]
    kwargs = dict(config=config, account="lx", market="hk", runtime_root=tmp_path,
        inbox_path=tmp_path / "inbox.sqlite3", combo_mode="confirm")
    result = attribution.reconcile_trade_attribution_account(repo, **kwargs)
    assert result["linked"] == 1 and result["conflicts"] == 0 and result["errors"] == [], result
    linked = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["execution_key"] == execution)
    assert linked["status"] == "linked" and linked["wheel_branch_id"] == branches[-1]
    assert linked["origin"] == "rule"
    assert len(repo.list_trade_events()) == len(before) + 1
    assert [row for row in repo.list_trade_events() if row["event_type"] == "open"] == opens
    view = _view(repo, config, observation=observation)
    assert all(branch["coverage"]["status"] == "full" for branch in view["wheel_model"]["wheel_branches"])
    retry = attribution.reconcile_trade_attribution_account(repo, **kwargs)
    assert retry["linked"] == 0 and retry["conflicts"] == 0 and retry["errors"] == [], retry
    assert len(repo.list_trade_events()) == len(before) + 1


def test_two_unclaimed_branches_still_require_manual_choice(tmp_path, monkeypatch):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch, covered=3)
    target = next(row for row in _view(repo, config, observation=_observation(repo))["rows"]
                  if row["execution_key"] == execution)
    assert target["candidate_ids"] == ["wheel:" + branch for branch in branches[3:]]
    assert target["selected_candidate_id"] is None and target["reason_codes"] == ["multiple_strategy_candidates"]


@pytest.mark.parametrize("missing", ["capacity", "stale_capacity", "combo"])
def test_unique_remaining_branch_cannot_bypass_incomplete_or_stale_evidence(tmp_path, monkeypatch, missing):
    repo, config, _, execution, _ = _occupied_scope(tmp_path, monkeypatch)
    observation = {} if missing == "capacity" else _observation(repo, now_ms=NOW - 61000 if missing == "stale_capacity" else NOW)
    evidence = {"complete": False, "exposures": []} if missing == "combo" else EVIDENCE
    target = next(row for row in _view(repo, config, observation=observation, evidence=evidence)["rows"]
                  if row["execution_key"] == execution)
    assert target["status"] == "pending" and target["selected_candidate_id"] is None


def test_two_pending_calls_against_last_branch_remain_blocked(tmp_path, monkeypatch):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch)
    second, _ = _ingest(repo, config, tmp_path, deal_id="new-second", occurred_at="2026-10-06T07:34:04Z")
    view = _view(repo, config, observation=_observation(repo))
    for row in view["rows"]:
        if row["execution_key"] in {execution, second}:
            assert row["candidate_ids"] == ["wheel:" + branches[-1]]
            assert row["selected_candidate_id"] is None
            assert "competing_fills_exceed_capacity" in row["reason_codes"]


@pytest.mark.parametrize("uncertainty", [
    "historical_not_full", "historical_unknown", "historical_untrusted", "current_unknown", "current_untrusted",
    "reservation_only", "partial", "overallocated", "invalid_intent", "matching_intent",
])
def test_full_branch_exclusion_requires_unambiguous_owner_evidence(tmp_path, monkeypatch, uncertainty):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch)
    branch_id = branches[0]
    current_owner = attribution.build_wheel_read_model_with_capacity_from_rows
    historical_owner = attribution.build_wheel_read_model_from_rows
    intent_owner = attribution.resolve_wheel_fill_intent

    def change(model, *, historical):
        model = deepcopy(model)
        if historical != uncertainty.startswith("historical_"):
            return model
        branch = next(row for row in model["wheel_branches"] if row["wheel_branch_id"] == branch_id)
        coverage = branch["coverage"]
        if uncertainty.endswith("untrusted"):
            branch["integrity_status"] = "conflict"
            branch["reason_codes"] = ["wheel_option_units_invalid"]
        elif uncertainty.endswith("unknown"):
            coverage["status"] = "unavailable"
        elif uncertainty == "historical_not_full":
            branch.update(active_option_committed_shares=0, active_option_lot_ids=[])
            coverage.update(status="none", committed_shares=0, available_shares=500)
        elif uncertainty == "reservation_only":
            branch.update(active_option_committed_shares=0, active_option_lot_ids=[], active_intent_reserved_shares=500)
            coverage.update(status="none", committed_shares=0, reserved_shares=500, available_shares=0)
        elif uncertainty == "partial":
            branch["active_option_committed_shares"] = 250
            coverage.update(status="partial", committed_shares=250, available_shares=250)
        elif uncertainty == "overallocated":
            branch["active_option_committed_shares"] = 1000
            coverage.update(status="overallocated", committed_shares=1000, available_shares=0)
        return model

    def current(*args, **kwargs):
        return tuple(change(model, historical=False) for model in current_owner(*args, **kwargs))

    def historical(*args, **kwargs):
        model = historical_owner(*args, **kwargs)
        # Change only the target fill-time view, not the history of prior linked calls.
        return change(model, historical=True) if kwargs["as_of_ms"] == NOW - 7000 else model

    def intent(branch, fill, *args, **kwargs):
        if branch["wheel_branch_id"] == branch_id and fill["event_time_ms"] == NOW - 7000:
            if uncertainty == "invalid_intent":
                return {"intent": None, "reserved_contracts_to_consume": 0,
                        "reason_codes": ["multiple_or_invalid_wheel_intents"]}
            if uncertainty == "matching_intent":
                return {"intent": {"intent_id": "fixture:matching", "remaining_contracts": 1},
                        "reserved_contracts_to_consume": 1, "reason_codes": []}
        return intent_owner(branch, fill, *args, **kwargs)

    monkeypatch.setattr(attribution, "build_wheel_read_model_with_capacity_from_rows", current)
    monkeypatch.setattr(attribution, "build_wheel_read_model_from_rows", historical)
    monkeypatch.setattr(attribution, "resolve_wheel_fill_intent", intent)
    target = next(row for row in _view(repo, config, observation=_observation(repo))["rows"]
                  if row["execution_key"] == execution)
    assert "wheel:" + branch_id in target["candidate_ids"]
    assert target["status"] == "pending" and target["selected_candidate_id"] is None


def test_new_competing_fill_invalidates_previous_unique_target_preview(tmp_path, monkeypatch):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch)
    observation = _observation(repo)
    preview = next(row for row in _view(repo, config, observation=observation)["rows"]
                   if row["execution_key"] == execution)
    assert preview["selected_candidate_id"] == "wheel:" + branches[-1]
    _ingest(repo, config, tmp_path, deal_id="new-concurrent", occurred_at="2026-10-06T07:34:04Z")
    before = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    with pytest.raises(ValueError, match="evidence changed"):
        attribution.apply_trade_attribution(repo, account="lx", market="hk", config=config,
            execution_key=execution, candidate_id=preview["selected_candidate_id"],
            expected_input_hash=preview["input_hash"], request_id="fixture:old-preview", actor="fixture:rule",
            combo_evidence=EVIDENCE, capacity_observation=observation, combo_mode="confirm")
    assert read_trade_attribution_snapshot(repo, account="lx", market="hk") == before
