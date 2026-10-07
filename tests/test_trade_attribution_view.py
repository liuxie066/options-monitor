from cash_evidence_helpers import cash_portfolio, cash_config
import pytest
from copy import deepcopy
from datetime import datetime, timezone

from domain.domain.trade_execution import execution_identity_from_input
from src.application.trades.attribution import build_trade_attribution_view
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.wheel.capacity import trade_attribution_capacity_check


def _call_scope(tmp_path, monkeypatch, contracts=1):
    from test_wheel_workflows import _wheel_repo, _open_unlinked_call
    from src.application.wheel.config import resolve_wheel_activation_descriptor
    repo, branch_id = _wheel_repo(tmp_path, contracts=contracts)
    _open_unlinked_call(repo)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    for event in rows["trade_events"]:
        if event["event_type"] == "open":
            event["raw_payload"]["execution_input"] = {
                "external_id_namespace": "futu.deal", "external_execution_id": event["event_id"],
                "broker_account_ref": ref,
            }
    cfg = {"market": "us", "_resolved": {"market": "us"}, "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}, "wheel": {"accounts": ["lx"],
        "activation_by_account": {"lx": {"generation": 1, "activated_at_ms": 500, "deactivated_at_ms": None}}}}
    descriptor = resolve_wheel_activation_descriptor(cfg, market="us", account="lx")
    rows["wheel_activation_window"] = {**descriptor, "policy_sha256": descriptor["policy_hash"]}
    rows["attribution_policy_enablings"] = [{"broker": "futu", "physical_account_id": "1001", "environment": "REAL",
        "account": "lx", "market": "us", "policy_version": "trade_attribution.v2", "effective_from_ms": 2500}]
    # Capacity owner has separate full snapshot checks below; this isolates global competition.
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check",
                        lambda **kwargs: {"status": "available", "reason_codes": []})
    return rows, cfg, branch_id


def test_global_wheel_rule_and_all_competing_executions(tmp_path, monkeypatch):
    rows, config, branch = _call_scope(tmp_path, monkeypatch)
    args = dict(config=config, account="lx", market="us", now_ms=4000,
                combo_evidence={"complete": True, "exposures": []})
    result = build_trade_attribution_view(rows, **args)
    call = next(row for row in result["rows"] if row["position_side"] == "short" and row["contract_key"]["option_type"] == "call")
    assert call["selected_candidate_id"] == "wheel:" + branch
    assert call["status"] == "pending"  # Only the committed writer can publish linked.
    later = build_trade_attribution_view(rows, **{**args, "now_ms": 4001})
    assert [row["input_hash"] for row in later["rows"]] == [row["input_hash"] for row in result["rows"]]
    second = deepcopy(next(row for row in rows["trade_events"] if row["event_id"] == "unlinked-call-open-1"))
    second.update(event_id="second-fill", lot_id="second-lot")
    second["raw_payload"]["execution_input"]["external_execution_id"] = "second"
    rows["trade_events"].append(second)
    second_lot = deepcopy(next(row for row in rows["stored_position_lots"] if row["record_id"] == "unlinked-call-lot-1"))
    second_lot["record_id"] = "second-lot"
    second_lot["fields"].update(lot_id="second-lot", open_event_id="second-fill")
    rows["stored_position_lots"].append(second_lot)
    result = build_trade_attribution_view(rows, **args)
    calls = [row for row in result["rows"] if row["contract_key"]["option_type"] == "call"]
    assert len(calls) == 2
    assert all(row["selected_candidate_id"] is None for row in calls)
    assert all("competing_fills_exceed_capacity" in row["reason_codes"] for row in calls)


def test_unrelated_fills_skip_historical_wheel_projection(tmp_path, monkeypatch):
    import src.application.trades.attribution as attribution

    rows, config, _branch = _call_scope(tmp_path, monkeypatch)
    original = attribution.build_wheel_read_model_from_rows
    projected_at = []
    def tracked(*args, **kwargs):
        projected_at.append(kwargs["as_of_ms"])
        return original(*args, **kwargs)
    monkeypatch.setattr(attribution, "build_wheel_read_model_from_rows", tracked)
    build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=4000,
                                 combo_evidence={"complete": True, "exposures": []})
    put_times = {event["event_time_ms"] for event in rows["trade_events"]
                 if event["event_type"] == "open" and event["option_type"] == "put"}
    assert put_times and not put_times.intersection(projected_at)



def test_v1_window_fill_stays_manual_after_v2_cutover(tmp_path, monkeypatch):
    rows, config, branch = _call_scope(tmp_path, monkeypatch)
    scope = {"broker": "futu", "physical_account_id": "1001", "environment": "REAL",
             "account": "lx", "market": "us"}
    rows["attribution_policy_enablings"] = [
        {**scope, "policy_version": "trade_attribution.v1", "effective_from_ms": 2000},
        {**scope, "policy_version": "trade_attribution.v2", "effective_from_ms": 3500},
    ]
    args = dict(config=config, account="lx", market="us", now_ms=5000,
                combo_evidence={"complete": True, "exposures": []})
    before = build_trade_attribution_view(rows, **args)
    call = next(row for row in before["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["event_time_ms"] == 3000
    assert call["candidate_ids"] == ["wheel:" + branch]
    assert call["rules_enabled"] is False and call["selected_candidate_id"] is None
    for event in rows["trade_events"]:
        if event["event_id"] == "unlinked-call-open-1":
            event["event_time_ms"] = 4000
    after = build_trade_attribution_view(rows, **args)
    call = next(row for row in after["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["rules_enabled"] is True and call["selected_candidate_id"] == "wheel:" + branch


def test_future_branch_does_not_own_past_fill_and_missing_evidence_stays_pending(tmp_path, monkeypatch):
    rows, config, _branch = _call_scope(tmp_path, monkeypatch)
    for event in rows["trade_events"]:
        if event["event_id"] == "unlinked-call-open-1":
            event["event_time_ms"] = 1500
    args = dict(config=config, account="lx", market="us", now_ms=4000)
    result = build_trade_attribution_view(rows, combo_evidence={"complete": True}, **args)
    call = next(row for row in result["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["status"] == "ordinary" and call["candidate_ids"] == []
    result = build_trade_attribution_view(rows, combo_evidence={"complete": False}, **args)
    call = next(row for row in result["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["status"] == "pending"


def test_capacity_counts_booked_calls_once_and_refuses_mismatch_or_stale():
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    observed = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()
    snapshot = build_futu_position_snapshot(rows=[
        {"code": "US.NVDA", "sec_type": "STOCK", "qty": 100, "can_sell_qty": 0},
        {"code": "US.NVDA261218C00100000", "stock_owner": "US.NVDA", "sec_type": "OPTION",
         "qty": -1, "option_type": "CALL", "option_strike_price": 100, "strike_time": "2026-12-18", "multiplier": 100},
    ], broker_account_ref={**ref, "account_label": "lx", "broker_account_id": "futu:REAL:1001"}, markets=["US"], asset_types=["stock", "option"],
        observed_at_utc=observed, completeness="complete")
    observation = {"portfolio": {"capacity_authority": {"status": "available", "logical_account": "lx",
        "futu_account_id": "1001", "trd_env": "REAL", "market": "us"}, "position_snapshot_input": snapshot}}
    fact = {"account": "lx", "broker_account_ref": ref, "contracts_open": 1, "position_side": "short", "multiplier": 100, "currency": "USD",
            "contract_key": {"underlying_symbol": "NVDA", "option_type": "call", "strike": "100", "expiration_ymd": "2026-12-18"}}
    args = dict(config=cash_config(account_id="1001"), fact=fact, facts=[fact], observation=observation, wheel_read_model={"wheel_branches": []})
    result = trade_attribution_capacity_check(**args, now_ms=now)
    assert result["status"] == "available", result
    unrelated = deepcopy(fact)
    unrelated["contract_key"] = {**fact["contract_key"], "underlying_symbol": "PDD"}
    unrelated["broker_account_ref"] = {"broker_id": None, "external_account_id": None, "environment": None}
    assert trade_attribution_capacity_check(**{**args, "facts": [fact, unrelated]}, now_ms=now)["status"] == "available"
    stale = trade_attribution_capacity_check(**args, now_ms=now + 60001)
    assert "snapshot_observed_at_utc_stale_or_future" in stale["reason_codes"]
    mismatched = trade_attribution_capacity_check(**{**args, "facts": [fact, fact]}, now_ms=now)
    assert "broker_ledger_positions_mismatch" in mismatched["reason_codes"]
    assert "account_stock_capacity_exceeded" in mismatched["reason_codes"]
    from test_wheel_strategy import _started_event, _assignment_trade, _assigned_stock
    from domain.domain.wheel import build_legacy_wheel_event, project_wheel_branches
    intent = build_legacy_wheel_event(event_id="invalid-units", account="lx", lot_id="assigned-stock-assign-put",
        event_type="wheel_call_intent_created",
        occurred_at_ms=2100, recorded_at_ms=2101, intent_id="invalid-units",
        payload={"contracts": 1, "multiplier": "100.5", "expires_at_ms": now + 9000})
    branches = project_wheel_branches(
        [_started_event(), intent], [_assignment_trade()], [], _assigned_stock(), now)
    unknown = trade_attribution_capacity_check(**{**args, "wheel_read_model": {"wheel_branches": branches}}, now_ms=now)
    assert "capacity_basis_unavailable" in unknown["reason_codes"]
    for conflict_type in ("creation", "consumption"):
        created = build_legacy_wheel_event(event_id="created", account="lx", lot_id="assigned-stock-assign-put",
            event_type="wheel_call_intent_created",
            occurred_at_ms=2100, recorded_at_ms=2100, intent_id="conflicted",
            payload={"contracts": 1, "multiplier": 100, "expires_at_ms": now + 9000})
        conflicting = build_legacy_wheel_event(event_id="conflicting", account="lx", lot_id="assigned-stock-assign-put",
            event_type="wheel_call_intent_created" if conflict_type == "creation" else "wheel_call_intent_consumed",
            occurred_at_ms=2200, recorded_at_ms=2200, intent_id="conflicted", source_trade_event_id="unknown-fill",
            payload={"contracts": 1, "multiplier": 100, "expires_at_ms": now + 9000})
        branch = project_wheel_branches(
            [_started_event(), created, conflicting], [_assignment_trade()], [], _assigned_stock(), now)[0]
        assert not branch["active_intent_ids"] and branch["active_intent_reserved_shares"] is None
        assert "intent_" + conflict_type + "_conflict" in branch["reason_codes"]
        check = lambda value: trade_attribution_capacity_check(**{**args, "wheel_read_model": {"wheel_branches": [value]}}, now_ms=now)
        assert "capacity_basis_unavailable" in check(branch)["reason_codes"]
        assert check({**branch, "symbol": "AAPL"})["status"] == "available"
        assert check({**branch, "lifecycle_status": "ended"})["status"] == "available"


def _writable_call_scope(tmp_path, monkeypatch, contracts=1, fill_time=3000):
    import pytest
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.trades.attribution import apply_trade_attribution
    from src.application.wheel.config import resolve_wheel_activation_descriptor

    source = tmp_path / "source"
    source.mkdir()
    rows, config, _ = _call_scope(source, monkeypatch, contracts=contracts)
    repo = SQLiteOptionPositionsRepository(tmp_path / "writer.sqlite3")
    descriptor = resolve_wheel_activation_descriptor(config, market="us", account="lx")
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 500)
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(market="us", account="lx", expected_current_generation=0,
            policy_hash=descriptor["policy_hash"], request_id="window", request_hash="b" * 64, conn=conn)
    with repo._writer_connection(begin_immediate=True) as conn:
        from src.application.ledger.trade_attribution import ATTRIBUTION_POLICY_VERSION
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker, physical_account_id, environment, account, market, policy_version,
             effective_from_ms, created_at_ms, actor, request_id, request_hash)
            VALUES ('futu', '1001', 'REAL', 'lx', 'us', ?, 2500, 2000, 'fixture', 'cutover-v2', ?)""",
            (ATTRIBUTION_POLICY_VERSION, "a" * 64))
    from domain.domain.trade_execution import execution_identity_from_input
    for event in rows["trade_events"]:
        if event["event_id"] == "unlinked-call-open-1":
            event["event_time_ms"] = fill_time
        execution = event["raw_payload"].get("execution_input")
        if execution:
            event["raw_payload"]["execution_id"] = execution_identity_from_input(execution)
        persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(event)])
    monkeypatch.setattr("time.time", lambda: 4)
    return repo, config


def _call_capacity_observation(contracts=1, now_ms=4000):
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    snapshot = build_futu_position_snapshot(rows=[
        {"code": "US.NVDA", "sec_type": "STOCK", "qty": contracts * 100, "can_sell_qty": 0},
        {"code": "US.NVDA260821C00110000", "stock_owner": "US.NVDA", "sec_type": "OPTION",
         "qty": -contracts, "option_type": "CALL", "option_strike_price": 110,
         "strike_time": "2026-08-21", "multiplier": 100},
    ], broker_account_ref={"broker_id": "futu", "external_account_id": "1001", "environment": "REAL",
        "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["US"], asset_types=["stock", "option"], completeness="complete",
        observed_at_utc=datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat())
    return {"portfolio": {"capacity_authority": {"status": "available", "logical_account": "lx",
        "futu_account_id": "1001", "trd_env": "REAL", "market": "us"}, "position_snapshot_input": snapshot}}


@pytest.mark.parametrize("change", ["refresh", "cash", "authority", "cash_ttl"])
def test_public_confirmation_read_hash_confirms_and_default_read_stays_local(tmp_path, monkeypatch, change):
    from src.application.trades import attribution
    from src.application.agent_tools.positions import TRADE_ATTRIBUTION_READ_TOOL
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
    monkeypatch.setattr(attribution, "attribution_runtime", lambda **_: (repo, config,
        {"runtime_root": str(tmp_path), "config_path": str(tmp_path / "config.json")}, {}))
    evidence = {"complete": True, "exposures": []}
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **kw: evidence)
    observations = []
    def observe(**kwargs):
        observations.append(kwargs)
        observed_at = 3000 + len(observations) * 100
        observation = _call_capacity_observation(now_ms=observed_at)
        observation["portfolio"] = cash_portfolio({**observation["portfolio"],
            "cash_source_observed_at": datetime.fromtimestamp(observed_at / 1000, timezone.utc).isoformat(),
            "cash_by_currency": {"USD": 10000}}, account_id="1001")
        if len(observations) > 1:
            if change == "cash":
                observation["portfolio"]["cash_by_currency"]["USD"] = 9999
            elif change == "authority":
                observation["portfolio"]["capacity_authority"]["futu_account_id"] = "other"
            elif change == "cash_ttl":
                observation["portfolio"]["cash_snapshot"]["max_age_sec"] = 1
        return observation
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", observe)
    local, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call({"account": "lx"})
    assert observations == [] and local["capacity_observed"] is False
    prepared, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call({"account": "lx", "prepare_confirmation": True})
    assert len(observations) == 1 and prepared["capacity_observed"] is True
    assert observations[0]["runtime_root"] == tmp_path
    assert observations[0]["config"] == config
    call = next(row for row in prepared["rows"] if row["contract_key"]["option_type"] == "call")
    from src.application.wheel.read_model import build_wheel_read_model_from_rows
    model = build_wheel_read_model_from_rows(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", market="us", as_of_ms=4000)
    linkage = next(row for row in model["linkage_candidates"] if row["call_record_id"] == call["lot_id"])
    args = dict(account="lx", config=config, runtime_root=tmp_path, expected_input_hash=call["input_hash"],
        request_id="public-confirmation", actor="operator", option_lot_id=call["lot_id"],
        wheel_branch_id=linkage["wheel_branch_id"], direction="call",
        linkage_candidate_id=linkage["linkage_candidate_id"],
        expected_batch_generation_hash=linkage["batch_generation_hash"])
    before = repo.list_trade_events()
    if change != "refresh":
        with pytest.raises(ValueError, match="attribution evidence changed"):
            attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)
        assert repo.list_trade_events() == before
        return
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=False)["status"] == "planned"
    assert repo.list_trade_events() == before
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)["status"] == "confirmed"
    assert len(repo.list_trade_events()) == len(before) + 1



def test_public_call_confirmation_rejects_insufficient_broker_shares_without_writes(tmp_path, monkeypatch):
    from src.application.trades import attribution
    from src.application.agent_tools.positions import TRADE_ATTRIBUTION_READ_TOOL
    from src.application.wheel.read_model import build_wheel_read_model_from_rows

    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
    monkeypatch.setattr(attribution, "attribution_runtime", lambda **_: (repo, config,
        {"runtime_root": str(tmp_path), "config_path": str(tmp_path / "config.json")}, {}))
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence",
        lambda *a, **kw: {"complete": True, "exposures": []})
    good = _call_capacity_observation()
    bad = deepcopy(good)
    stock = next(row for row in bad["portfolio"]["position_snapshot_input"]["rows"]
                 if row["instrument_ref"]["asset_type"] == "stock")
    stock["quantity"] = "0"
    observations = iter((good, bad, bad, bad))
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity",
        lambda **_: next(observations))

    prepared, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call(
        {"account": "lx", "prepare_confirmation": True})
    call = next(row for row in prepared["rows"]
                if row["contract_key"]["option_type"] == "call")
    model = build_wheel_read_model_from_rows(
        read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", market="us", as_of_ms=4000)
    linkage = next(row for row in model["linkage_candidates"]
                   if row["call_record_id"] == call["lot_id"])
    args = dict(account="lx", config=config, runtime_root=tmp_path,
        expected_input_hash=call["input_hash"], request_id="insufficient-call",
        actor="operator", option_lot_id=call["lot_id"],
        wheel_branch_id=linkage["wheel_branch_id"], direction="call",
        linkage_candidate_id=linkage["linkage_candidate_id"],
        expected_batch_generation_hash=linkage["batch_generation_hash"])
    before_trade = repo.list_trade_events()
    before_wheel = repo.list_wheel_events(account="lx")
    with pytest.raises(ValueError, match="attribution evidence changed"):
        attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=False)
    with pytest.raises(ValueError, match="attribution evidence changed"):
        attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)
    refreshed, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call(
        {"account": "lx", "prepare_confirmation": True})
    refreshed_call = next(row for row in refreshed["rows"]
                          if row["contract_key"]["option_type"] == "call")
    assert refreshed_call["selected_candidate_id"] is None
    assert repo.list_trade_events() == before_trade
    assert repo.list_wheel_events(account="lx") == before_wheel

def test_confirmation_read_capacity_failure_stays_unavailable(tmp_path, monkeypatch):
    from src.application.trades import attribution
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
    monkeypatch.setattr(attribution, "attribution_runtime", lambda **_: (repo, config, {"runtime_root": str(tmp_path)}, {}))
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **kw: {"complete": True, "exposures": []})
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **_: {"error": "provider_timeout"})
    before = repo.list_trade_events()
    result, _, _ = attribution.trade_attribution_read({"account": "lx", "prepare_confirmation": True})
    call = next(row for row in result["rows"] if row["contract_key"]["option_type"] == "call")
    assert result["capacity_observed"] is False and call["selected_candidate_id"] is None
    assert "capacity_authority_unavailable" in call["candidates"][0]["reason_codes"]
    assert repo.list_trade_events() == before


def test_final_capacity_rejects_snapshot_that_expires_during_write(tmp_path, monkeypatch):
    from src.application.trades import attribution
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
    observation = _call_capacity_observation()
    evidence = {"complete": True, "exposures": []}
    view = build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=4000, combo_evidence=evidence,
        capacity_observation=observation)
    fact = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    original = attribution.write_trade_attribution_decision
    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr("time.time", lambda: 65)
        return result
    monkeypatch.setattr(attribution, "write_trade_attribution_decision", expire)
    before = read_trade_attribution_snapshot(repo, account="lx", market="us")
    with pytest.raises(ValueError, match="capacity changed before commit"):
        attribution.apply_trade_attribution(repo, account="lx", market="us", config=config,
            execution_key=fact["execution_key"], candidate_id=fact["selected_candidate_id"],
            expected_input_hash=fact["input_hash"], request_id="expires-in-writer", actor="rule",
            combo_evidence=evidence, capacity_observation=observation, combo_mode="confirm")
    after = read_trade_attribution_snapshot(repo, account="lx", market="us")
    assert after["trade_events"] == before["trade_events"]
    assert after["account_wheel_events"] == before["account_wheel_events"]
    assert after["stored_position_lots"] == before["stored_position_lots"]


def test_global_writer_commits_once_and_rolls_back_on_precommit_cancellation(tmp_path, monkeypatch):
    import pytest
    from src.application.trades.attribution import apply_trade_attribution
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    evidence = {"complete": True, "exposures": []}
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=4000, combo_evidence=evidence)
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["selected_candidate_id"]
    args = dict(account="lx", market="us", config=config, execution_key=call["execution_key"],
        candidate_id=call["selected_candidate_id"], expected_input_hash=call["input_hash"], request_id="rule:call", actor="trade_intake:rule",
        combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")

    class CancelBeforeCommit:
        checks = 0
        def is_set(self):
            self.checks += 1
            return self.checks >= 2

    before = repo.list_trade_events()
    with pytest.raises(ValueError, match="cancelled before commit"):
        apply_trade_attribution(repo, **args, stop_event=CancelBeforeCommit())
    assert repo.list_trade_events() == before
    first = apply_trade_attribution(repo, **args)
    assert first["status"] == "linked" and first["write_applied"]
    second = apply_trade_attribution(repo, **{**args, "capacity_observation": {"error": "timeout"}})
    assert second["status"] == "linked" and not second["write_applied"]
    assert len(repo.list_trade_events()) == len(before) + 1
    assert [row for row in repo.list_trade_events() if row["event_type"] == "open"] == [row for row in before if row["event_type"] == "open"]


@pytest.mark.parametrize("decision", ["wheel", "ordinary"])
def test_attribution_accepts_historical_contract_key_with_position_key(tmp_path, monkeypatch, decision):
    from src.application.trades.attribution import apply_trade_attribution

    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("""UPDATE trade_events
            SET event_json = json_set(event_json, '$.contract_key.position_key', 'legacy-position')
            WHERE event_id = 'unlinked-call-open-1'""")
    stored_open = next(row for row in repo.list_trade_events() if row["event_id"] == "unlinked-call-open-1")
    assert stored_open["contract_key"]["position_key"] == "legacy-position"

    evidence = {"complete": True, "exposures": []}
    view = build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=4000, combo_evidence=evidence)
    call = next(row for row in view["rows"] if row["open_event_id"] == stored_open["event_id"])
    args = dict(account="lx", market="us", config=config, execution_key=call["execution_key"],
        candidate_id=call["selected_candidate_id"] if decision == "wheel" else "ordinary",
        manual=decision == "ordinary", expected_input_hash=call["input_hash"],
        request_id="legacy-contract-key", actor="fixture:operator", combo_evidence=evidence,
        capacity_observation={}, combo_mode="confirm")
    before = repo.list_trade_events()
    assert not apply_trade_attribution(repo, **args, apply_changes=False)["write_applied"]
    assert repo.list_trade_events() == before
    result = apply_trade_attribution(repo, **args)
    assert result["write_applied"] and result["status"] == ("linked" if decision == "wheel" else "ordinary")
    assert not apply_trade_attribution(repo, **args)["write_applied"]
    assert len(repo.list_trade_events()) == len(before) + 1
    proof = next(row for row in repo.list_trade_events() if row["event_id"] in result["proof_event_ids"])
    assert "position_key" not in proof["contract_key"]
    assert next(row for row in repo.list_trade_events() if row["event_id"] == stored_open["event_id"]) == stored_open
    from domain.domain.wheel import lot_strategy_metadata_from_trade_events
    changed = deepcopy(repo.list_trade_events())
    next(row for row in changed if row["event_id"] == proof["event_id"])["contract_key"]["strike"] = "111"
    accepted = set()
    lot_strategy_metadata_from_trade_events(changed, accepted_proof_event_ids=accepted)
    assert proof["event_id"] not in accepted


def test_view_and_writer_keep_other_market_capacity_obligations(tmp_path, monkeypatch):
    from dataclasses import replace
    from domain.domain.ledger import ContractKey
    from test_trades_combo_reconciliation import _open_events
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.trades.attribution import apply_trade_attribution
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    hk = replace(_open_events()[1], event_id="hk-put", lot_id="hk-put", event_time_ms=3000,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="0700.HK",
            option_type="put", strike=200, expiration_ymd="2026-12-18"), currency="HKD", multiplier=100)
    execution = {**hk.raw_payload["execution_input"], "external_id_namespace": "futu.deal", "external_execution_id": "hk-put"}
    persist_trade_event_object(repo, replace(hk, raw_payload={**hk.raw_payload, "execution_input": execution,
        "execution_id": execution_identity_from_input(execution)}))
    observed = []

    def capacity(**kwargs):
        observed.append({fact["contract_key"]["underlying_symbol"] for fact in kwargs["facts"]})
        return {"status": "available", "reason_codes": []}

    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check", capacity)
    evidence = {"complete": True, "exposures": []}
    view = build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=4000, combo_evidence=evidence)
    assert all(row["contract_key"]["underlying_symbol"] == "NVDA" for row in view["rows"])
    assert observed and all("0700.HK" in symbols for symbols in observed)
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    observed.clear()
    result = apply_trade_attribution(repo, account="lx", market="us", config=config,
        execution_key=call["execution_key"], candidate_id=call["selected_candidate_id"],
        expected_input_hash=call["input_hash"], request_id="rule:cross-market", actor="trade_intake:rule",
        combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
    assert result["write_applied"]
    assert observed and all("0700.HK" in symbols for symbols in observed)


@pytest.mark.parametrize("now_ms", [7000, 12000])
def test_two_booked_intent_fills_are_linked_and_consumed_atomically(tmp_path, monkeypatch, now_ms):
    import pytest
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.trades.attribution import apply_trade_attribution
    from test_wheel_workflows import _wheel_repo, _create_call_intent
    repo, config = _writable_call_scope(tmp_path, monkeypatch, contracts=2, fill_time=5000)
    seed = tmp_path / "intent-seed"
    seed.mkdir()
    source, lot_id = _wheel_repo(seed, contracts=2)
    _create_call_intent(source, lot_id, contracts=2)
    with repo._writer_connection(begin_immediate=True) as conn:
        for event in source.list_wheel_events(account="lx"):
            if event["event_type"] == "wheel_call_intent_created":
                repo.append_wheel_event_once(event, conn=conn)
    second = deepcopy(next(row for row in repo.list_trade_events() if row["event_id"] == "unlinked-call-open-1"))
    second.update(event_id="second-fill", lot_id="second-lot", event_time_ms=6000)
    second["raw_payload"]["execution_input"]["external_execution_id"] = "second-fill"
    from domain.domain.trade_execution import execution_identity_from_input
    second["raw_payload"]["execution_id"] = execution_identity_from_input(second["raw_payload"]["execution_input"])
    persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(second)])
    monkeypatch.setattr("time.time", lambda: now_ms / 1000)
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check", trade_attribution_capacity_check)
    observation = _call_capacity_observation(contracts=2, now_ms=now_ms)
    evidence = {"complete": True, "exposures": []}
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=now_ms,
        combo_evidence=evidence, capacity_observation=observation)
    calls = [row for row in view["rows"] if row["contract_key"]["option_type"] == "call"]
    assert len(calls) == 2 and all(row["selected_candidate_id"] for row in calls), calls
    assert all(len(row["candidates"][0]["member_lot_ids"]) == 2 for row in calls)
    args = dict(account="lx", market="us", config=config, execution_key=calls[0]["execution_key"],
        candidate_id=calls[0]["selected_candidate_id"], expected_input_hash=calls[0]["input_hash"], request_id="two-fills",
        actor="trade_intake:rule", combo_evidence=evidence, capacity_observation=observation, combo_mode="confirm")
    class CancelBeforeCommit:
        checks = 0
        def is_set(self):
            self.checks += 1
            return self.checks >= 2
    before = repo.list_trade_events()
    with pytest.raises(ValueError, match="cancelled before commit"):
        apply_trade_attribution(repo, **args, stop_event=CancelBeforeCommit())
    assert repo.list_trade_events() == before
    assert not [row for row in repo.list_wheel_events(account="lx") if row["event_type"] == "wheel_call_intent_consumed"]
    assert apply_trade_attribution(repo, **args)["origin"] == "intent"
    consumes = [row for row in repo.list_wheel_events(account="lx") if row["event_type"] == "wheel_call_intent_consumed"]
    assert len(consumes) == 2 and sum(row["payload"]["contracts"] for row in consumes) == 2
    assert {row["event_schema_version"] for row in consumes} == {"wheel_event.v2"}
    assert len(repo.list_trade_events()) == len(before) + 2


def test_late_exact_intent_fill_after_wheel_close_is_linked_once(tmp_path, monkeypatch):
    from test_wheel_workflows import _wheel_repo, _create_call_intent
    from src.application.trades.attribution import apply_trade_attribution

    repo, config = _writable_call_scope(tmp_path, monkeypatch, fill_time=5_000)
    seed = tmp_path / "intent-seed"
    seed.mkdir()
    source, lot_id = _wheel_repo(seed)
    _create_call_intent(source, lot_id)
    with repo._writer_connection(begin_immediate=True) as conn:
        for event in source.list_wheel_events(account="lx"):
            if event["event_type"] == "wheel_call_intent_created":
                repo.append_wheel_event_once(event, conn=conn)
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 4_500)
    with repo._writer_connection(begin_immediate=True) as conn:
        closed = repo.close_wheel_activation_window(market="us", account="lx", expected_current_generation=1,
            policy_hash=repo.get_current_wheel_activation_window(market="us", account="lx", conn=conn)["policy_hash"],
            request_id="close", request_hash="c" * 64, conn=conn)
    config["wheel"]["activation_by_account"]["lx"]["deactivated_at_ms"] = closed["window"]["deactivated_at_ms"]
    monkeypatch.setattr("time.time", lambda: 6)
    evidence = {"complete": True, "exposures": []}
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=6_000, combo_evidence=evidence)
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["selected_candidate_id"] and call["candidates"][0]["intent_id"]
    args = dict(account="lx", market="us", config=config, execution_key=call["execution_key"],
        candidate_id=call["selected_candidate_id"], expected_input_hash=call["input_hash"],
        request_id="late-intent", actor="trade_intake:rule", combo_evidence=evidence,
        capacity_observation={}, combo_mode="confirm")
    first = apply_trade_attribution(repo, **args)
    second = apply_trade_attribution(repo, **args)
    assert first["status"] == second["status"] == "linked"
    assert first["write_applied"] and not second["write_applied"]
    assert len([event for event in repo.list_wheel_events(account="lx")
                if event["event_type"] == "wheel_call_intent_consumed"]) == 1


@pytest.mark.parametrize("gate, with_intent, eligible", [
    ("account_removed", True, True),
    ("account_removed", False, False),
    ("closed", False, False),
    ("closed_order_mismatch", True, False),
    ("closed_policy_drift", True, False),
    ("account_removed_boundary_mismatch", True, False),
    ("missing_window", True, False),
])
def test_late_fill_needs_exact_intent_and_valid_historical_gate(tmp_path, monkeypatch, gate, with_intent, eligible):
    from test_wheel_workflows import _wheel_repo, _create_call_intent

    rows, config, branch_id = _call_scope(tmp_path, monkeypatch)
    next(event for event in rows["trade_events"] if event["event_id"] == "unlinked-call-open-1")["event_time_ms"] = 5_000
    if with_intent:
        seed = tmp_path / "intent-seed"
        seed.mkdir()
        source, lot_id = _wheel_repo(seed)
        _create_call_intent(source, lot_id, broker_order_id="different-order" if gate == "closed_order_mismatch" else None)
        rows["account_wheel_events"].extend(event for event in source.list_wheel_events(account="lx")
            if event["event_type"] == "wheel_call_intent_created")
    if gate.startswith("account_removed"):
        config["wheel"]["accounts"] = []
    if gate.startswith("closed"):
        config["wheel"]["activation_by_account"]["lx"]["deactivated_at_ms"] = 4_500
        rows["wheel_activation_window"]["deactivated_at_ms"] = 4_500
    if gate == "closed_policy_drift":
        rows["wheel_activation_window"]["policy_sha256"] = "e" * 64
    elif gate == "account_removed_boundary_mismatch":
        rows["wheel_activation_window"]["generation"] = 2
    elif gate == "missing_window":
        rows["wheel_activation_window"] = None
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=6_000,
                                        combo_evidence={"complete": True, "exposures": []})
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    assert bool(call["selected_candidate_id"]) is eligible, (gate, call["reason_codes"])
    if eligible:
        assert call["selected_candidate_id"] == "wheel:" + branch_id
        assert call["candidates"][0]["intent_id"]
    else:
        assert "wheel_branch_not_ready" in call["reason_codes"]
        if gate == "closed_order_mismatch":
            assert "wheel_intent_fill_mismatch_or_consumed" in call["reason_codes"]


def test_two_independent_writers_cannot_claim_different_memberships(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from src.application.ledger.api import read_trade_attribution_facts
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.attribution import apply_trade_attribution
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    evidence = {"complete": True}
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=4000, combo_evidence=evidence)
    target = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    fact = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["lot_id"] == target["lot_id"])
    second_repo = SQLiteOptionPositionsRepository(repo.db_path)
    barrier = Barrier(2)
    def claim(ordinary):
        barrier.wait(timeout=5)
        try:
            if ordinary:
                return apply_trade_attribution(second_repo, account="lx", execution_key=fact["execution_key"],
                    expected_input_hash=target["input_hash"], request_id="ordinary-race", actor="operator", manual=True,
                    market="us", config=config, candidate_id="ordinary", combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
            return apply_trade_attribution(repo, account="lx", market="us", config=config, execution_key=target["execution_key"],
                candidate_id=target["selected_candidate_id"], expected_input_hash=target["input_hash"], request_id="wheel-race",
                actor="rule", combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
        except ValueError:
            return {"rejected": True}
    before = len(repo.list_trade_events())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [True, False]))
    assert sum(bool(row.get("rejected")) for row in results) == 1
    assert len(repo.list_trade_events()) == before + 1
    final = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["lot_id"] == fact["lot_id"])
    assert final["status"] in {"ordinary", "linked"}


def test_conflict_remains_durable_when_candidate_reader_and_inbox_are_unavailable(tmp_path, monkeypatch):
    from src.application.ledger.api import with_sqlite_repo_transaction, record_trade_attribution_conflict
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=4000, combo_evidence={"complete": True})
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    branch = view["wheel_model"]["wheel_branches"][0]
    def persist(active, conn):
        return record_trade_attribution_conflict(active, account="lx", execution_key=call["execution_key"], branch=branch,
            candidate_ids=["wheel:" + branch["wheel_branch_id"], "combo:late-pair"], input_hash=call["input_hash"], now_ms=4000, conn=conn)
    first = with_sqlite_repo_transaction(repo, persist)
    assert with_sqlite_repo_transaction(repo, persist) == first
    fresh = read_trade_attribution_snapshot(repo, account="lx", market="us")
    after = build_trade_attribution_view(fresh, config=config, account="lx", market="us", now_ms=5000,
        combo_evidence={"complete": False}, capacity_observation={"error": "unavailable"})
    fact = next(row for row in after["rows"] if row["execution_key"] == call["execution_key"])
    assert fact["status"] == "conflict" and fact["selected_candidate_id"] is None
    assert "strategy_attribution_conflict" in after["wheel_model"]["wheel_branches"][0]["reason_codes"]
    assert len([event for event in repo.list_wheel_events(account="lx") if event["event_type"] == "wheel_attribution_conflict"]) == 1


def test_put_capacity_reuses_fx_pool_and_rejects_wrong_contract_units(monkeypatch):
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    from src.infrastructure import exchange_rates as fx
    instant = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(fx, "_utc_now", lambda: instant)
    now = int(instant.timestamp() * 1000)
    observed = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    snapshot = build_futu_position_snapshot(rows=[
        {"code": "US.NVDA261218P00025000", "stock_owner": "US.NVDA", "sec_type": "OPTION", "qty": -1,
         "option_type": "PUT", "option_strike_price": 25, "strike_time": "2026-12-18", "multiplier": 100},
    ], broker_account_ref={**ref, "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["US", "HK"], asset_types=["stock", "option"], observed_at_utc=observed, completeness="complete")
    portfolio = cash_portfolio({"cash_source_observed_at": observed, "capacity_authority": {"status": "available", "logical_account": "lx", "futu_account_id": "1001",
        "trd_env": "REAL", "market": "us"}, "position_snapshot_input": snapshot, "cash_balance_reliable": True,
        "cash_by_currency": {"USD": 0, "HKD": 20000}, "exchange_rates": {"pairs": {
            pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": observed, "observed_at_utc": observed}
            for pair, rate in (("USDCNY", 7), ("HKDCNY", 0.9))}},
        "exchange_rate_status": "ready"}, account_id="1001")
    fact = {"account": "lx", "broker_account_ref": ref, "contracts_open": 1, "position_side": "short", "multiplier": 100,
            "currency": "USD", "contract_key": {"underlying_symbol": "NVDA", "option_type": "put", "strike": "25.0", "expiration_ymd": "2026-12-18"}}
    args = dict(config=cash_config(account_id="1001"), fact=fact, facts=[fact], observation={"portfolio": portfolio}, wheel_read_model={"wheel_branches": []}, now_ms=now)
    assert trade_attribution_capacity_check(**args)["status"] == "available"
    verified_fx = portfolio["exchange_rates"]
    portfolio["exchange_rates"] = {"rates": {"USDCNY": 7, "HKDCNY": 0.9}}
    assert "account_cash_capacity_exceeded" in trade_attribution_capacity_check(**args)["reason_codes"]
    portfolio["exchange_rates"] = verified_fx
    original = deepcopy(portfolio)
    short_policy = {**args["config"], "runtime": {"portfolio_context_ttl_sec": 1}}
    # Position evidence is still fresh; only the cash policy expires at commit.
    expired = trade_attribution_capacity_check(**{**args, "config": short_policy, "now_ms": now + 2000})
    assert expired["reason_codes"] == ["cash_capacity_unavailable"]
    assert portfolio == original
    snapshot = portfolio["position_snapshot_input"]
    # An HK obligation consumes the same FX pool as the new US Put.
    hk = deepcopy(fact)
    hk["currency"] = "HKD"
    hk["contract_key"] = {**fact["contract_key"], "underlying_symbol": "0700.HK", "strike": "200"}
    hk_row = deepcopy(snapshot["rows"][0])
    hk_row["instrument_ref"].update(symbol="0700.HK", market="HK", strike="200", currency="HKD")
    snapshot["rows"].append(hk_row)
    args["facts"].append(hk)
    assert "account_cash_capacity_exceeded" in trade_attribution_capacity_check(**args)["reason_codes"]
    portfolio["cash_by_currency"]["HKD"] = 50000
    assert trade_attribution_capacity_check(**args)["status"] == "available"
    snapshot["rows"].pop()
    args["facts"].pop()
    portfolio["cash_by_currency"]["HKD"] = 20000
    snapshot["scope"]["markets"] = ["US"]
    assert "snapshot_scope_mismatch" in trade_attribution_capacity_check(**args)["reason_codes"]
    snapshot["scope"]["markets"] = ["US", "HK"]
    args["wheel_read_model"]["wheel_branches"] = [{"lifecycle_status": "active", "direction": "put",
        "symbol": "0700.HK", "active_intent_ids": ["hk-intent"], "integrity_status": "trusted",
        "active_intent_reservations": [{"intent_id": "hk-intent", "remaining_contracts": 1,
            "cash_reservation_amount": 20000, "currency": "HKD"}]}]
    assert "account_cash_capacity_exceeded" in trade_attribution_capacity_check(**args)["reason_codes"]
    args["wheel_read_model"]["wheel_branches"][0].update(integrity_status="conflict", active_intent_ids=[], active_intent_reservations=[])
    assert "capacity_basis_unavailable" in trade_attribution_capacity_check(**args)["reason_codes"]
    args["wheel_read_model"]["wheel_branches"] = []
    portfolio["exchange_rate_status"] = "unavailable_stale"
    # Eligibility comes from the original quote, not a cached status label.
    assert trade_attribution_capacity_check(**args)["status"] == "available"
    for pair in portfolio["exchange_rates"]["pairs"].values():
        pair.update(quote_at_utc="2026-09-28T06:00:00+00:00", observed_at_utc="2026-09-28T06:00:00+00:00")
    assert "account_cash_capacity_exceeded" in trade_attribution_capacity_check(**args)["reason_codes"]
    portfolio["cash_by_currency"]["USD"] = 2500
    assert trade_attribution_capacity_check(**args)["status"] == "available"
    fact["multiplier"] = 50
    assert "broker_ledger_positions_mismatch" in trade_attribution_capacity_check(**args)["reason_codes"]


def _blocked_capacity_worker(connection, config, account, runtime_root):
    import time
    time.sleep(60)


@pytest.mark.parametrize("cancel", [False, True])
def test_provider_budget_terminates_its_worker(monkeypatch, tmp_path, cancel):
    import multiprocessing
    import time
    from threading import Event, Timer
    from src.application.wheel import capacity
    existing = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(capacity, "_attribution_capacity_worker", _blocked_capacity_worker)
    stop = Event()
    if cancel:
        timer = Timer(0.15, stop.set)
        timer.start()
    else:
        clock = time.monotonic
        start = clock()
        monkeypatch.setattr(time, "monotonic", lambda: clock() + (11 if clock() - start > 0.15 else 0))
    result = capacity.observe_trade_attribution_capacity(config={}, account="lx", runtime_root=tmp_path, stop_event=stop)
    assert result["error"] == ("cancelled" if cancel else "provider_timeout")
    assert {child.pid for child in multiprocessing.active_children()} <= existing
    if cancel:
        timer.join()


def test_unavailable_historical_brief_does_not_poison_current_fill(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_trades_combo_reconciliation import _open_events, BASE_TIME_MS
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.ledger.api import combo_attribution_candidates_from_rows
    from src.application.trades.attribution import read_attribution_combo_evidence
    repo = SQLiteOptionPositionsRepository(tmp_path / "history.sqlite3")
    current = _open_events()[1]
    for event in (replace(current, event_id="old-open", lot_id="old-lot", event_time_ms=BASE_TIME_MS - 864000000), current):
        execution = {**event.raw_payload["execution_input"], "external_id_namespace": "futu.deal", "external_execution_id": event.event_id}
        persist_trade_event_object(repo, replace(event, raw_payload={**event.raw_payload, "execution_input": execution, "execution_id": execution_identity_from_input(execution)}))
    persist_trade_event_object(repo, TradeEvent(event_id="old-close", event_type="close", event_time_ms=BASE_TIME_MS - 863999000,
        contract_key=current.contract_key, contracts=1, price=1, currency="USD", multiplier=100, source="test",
        target_lot_id="old-lot"))
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    lots = combo_attribution_candidates_from_rows(rows, account="lx", runtime_environment="", exposures=[],
        effective_now_ms=BASE_TIME_MS + 3000, include_claimed=True)["lot_facts"]
    dates = {lot["market_date"] for lot in lots}
    assert len(dates) == 2
    monkeypatch.setattr("src.application.trades.attribution.read_combo_candidate_exposures", lambda **kw: {
        "available": kw["market_trading_date"] == max(dates), "complete": True, "delivery_available": True, "exposures": []})
    evidence = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path, now_ms=BASE_TIME_MS + 3000)
    assert not evidence["complete"]
    config = {"market": "us", "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=BASE_TIME_MS + 3000, combo_evidence=evidence)
    current_fact = next(fact for fact in view["rows"] if fact["lot_id"] == "put-lot")
    assert current_fact["evidence_complete"] and current_fact["status"] == "ordinary", current_fact


@pytest.mark.parametrize("later_contracts", [1, 2])
def test_rejected_combo_does_not_reappear_as_placeholder_but_new_pair_competes(tmp_path, monkeypatch, later_contracts):
    from dataclasses import replace
    from test_trades_combo_reconciliation import _open_events, _exposure, BASE_TIME_MS
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.ledger.api import combo_attribution_candidates_from_rows
    repo = SQLiteOptionPositionsRepository(tmp_path / "rejected.sqlite3")
    for event in _open_events():
        execution = {**event.raw_payload["execution_input"], "external_id_namespace": "futu.deal", "external_execution_id": event.event_id}
        persist_trade_event_object(repo, replace(event, raw_payload={**event.raw_payload, "execution_input": execution, "execution_id": execution_identity_from_input(execution)}))
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    pair = combo_attribution_candidates_from_rows(rows, account="lx", runtime_environment="", exposures=[_exposure()],
        effective_now_ms=BASE_TIME_MS + 3000, include_claimed=True)["inferences"][0]
    rejected = {**pair, "status": "user_rejected"}
    rows["account_combo_inferences"] = [rejected]
    config = {"market": "us", "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    kwargs = dict(config=config, account="lx", market="us", now_ms=BASE_TIME_MS + 3000,
        combo_evidence={"complete": True, "exposures": [_exposure()]})
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check", lambda **_: {"reason_codes": []})
    view = build_trade_attribution_view(rows, **kwargs)
    assert all(not fact["candidates"] for fact in view["rows"]), view
    call = _open_events()[0]
    execution = {**call.raw_payload["execution_input"], "external_id_namespace": "futu.deal", "external_execution_id": "new-call"}
    persist_trade_event_object(repo, replace(call, event_id="new-call", lot_id="new-call-lot", contracts=later_contracts,
        raw_payload={**call.raw_payload, "execution_input": execution, "execution_id": execution_identity_from_input(execution)}))
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    rows["account_combo_inferences"] = [rejected]
    view = build_trade_attribution_view(rows, **kwargs)
    put = next(fact for fact in view["rows"] if fact["lot_id"] == "put-lot")
    assert len(put["candidates"]) == 1
    if later_contracts == 1:
        assert put["candidates"][0]["member_lot_ids"] == ["put-lot", "new-call-lot"]
    else:
        assert put["candidates"][0]["candidate_id"] == "combo-exposure:exposure-1"

    if later_contracts == 2:
        from domain.domain.ledger import TradeEvent
        persist_trade_event_object(repo, TradeEvent(event_id="partial-close", event_type="close",
            event_time_ms=BASE_TIME_MS + 2500, contract_key=call.contract_key, contracts=1, price=1,
            currency="USD", multiplier=100, source="test", target_lot_id="new-call-lot"))
        rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
        rows["account_combo_inferences"] = [rejected]
        view = build_trade_attribution_view(rows, **kwargs)
        put = next(fact for fact in view["rows"] if fact["lot_id"] == "put-lot")
        assert put["status"] == "pending"
        assert [candidate["candidate_id"] for candidate in put["candidates"]] == ["combo-exposure:exposure-1"]
        assert not combo_attribution_candidates_from_rows(rows, account="lx", runtime_environment="",
            exposures=[_exposure()], effective_now_ms=BASE_TIME_MS + 3000, include_claimed=True)["inferences"]


@pytest.mark.parametrize("entrypoint", ["writer", "manual_confirm"])
def test_put_intent_consumption_releases_cash_before_final_capacity_check(tmp_path, monkeypatch, entrypoint):
    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.wheel import build_wheel_event, project_wheel_linkage_candidates
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.trades import attribution
    from src.application.trades.attribution import apply_trade_attribution
    from src.application.wheel import build_wheel_read_model
    from src.application.wheel.config import resolve_wheel_activation_descriptor
    from test_wheel_intent_policy_binding import _environment

    seed = tmp_path / "seed"
    seed.mkdir()
    source, _, _, _, _ = _environment(seed, "put", monkeypatch)
    repo = SQLiteOptionPositionsRepository(tmp_path / "put.sqlite3")
    config = {"market": "us", "_resolved": {"market": "us"}, "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
        "wheel": {"accounts": ["lx"], "activation_by_account": {"lx": {
            "generation": 1, "activated_at_ms": 500, "deactivated_at_ms": None}}}}
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    descriptor = resolve_wheel_activation_descriptor(config, market="us", account="lx")
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 500)
    for event in source.list_trade_events():
        if event["event_id"] == "cc-open":
            with repo._writer_connection(begin_immediate=True) as conn:
                repo.open_wheel_activation_window(market="us", account="lx", expected_current_generation=0,
                    policy_hash=descriptor["policy_hash"], request_id="window", request_hash="b" * 64, conn=conn)
        if event["event_type"] == "open":
            execution = {"external_id_namespace": "futu.deal", "external_execution_id": event["event_id"],
                "broker_account_ref": ref}
            event["raw_payload"].update(execution_input=execution, execution_id=execution_identity_from_input(execution))
        persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(event)])
    branch = build_wheel_read_model(repo, "lx", 5000, market="us")["wheel_branches"][0]
    assert branch["direction"] == "put" and branch["lifecycle_status"] == "active"
    intent = build_wheel_event(event_id="put-intent", account="lx", lot_id=None, wheel_branch_id=branch["wheel_branch_id"],
        event_type="wheel_put_intent_created", occurred_at_ms=5000, recorded_at_ms=5000, intent_id="put-intent",
        payload={"market": "us", "symbol": "NVDA", "contracts": 1, "multiplier": 100, "strike": 100,
            "expiration_ymd": "2026-09-18", "expires_at_ms": 10000, "capacity_identity_hash": "put-cash",
            "cash_reservation_amount": 10000, "cash_reservation_currency": "USD"})
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(intent, conn=conn)
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker, physical_account_id, environment, account, market, policy_version,
             effective_from_ms, created_at_ms, actor, request_id, request_hash)
            VALUES ('futu', '1001', 'REAL', 'lx', 'us', 'trade_attribution.v2', 4500, 4500, 'fixture', 'cutover', ?)""",
            ("a" * 64,))
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": "put-fill", "broker_account_ref": ref}
    persist_trade_event_objects_atomically(repo, [TradeEvent(event_id="put-fill", event_type="open", event_time_ms=6000,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA", option_type="put",
            strike=100, expiration_ymd="2026-09-18"), contracts=1, price=1, currency="USD", multiplier=100,
        source="test", lot_id="put-fill", raw_payload={"side": "sell", "execution_input": execution,
            "execution_id": execution_identity_from_input(execution), "multiplier_source": "payload"})])
    monkeypatch.setattr("time.time", lambda: 7)
    snapshot = build_futu_position_snapshot(rows=[{
        "code": "US.NVDA260918P00100000", "stock_owner": "US.NVDA", "sec_type": "OPTION", "qty": -1,
        "option_type": "PUT", "option_strike_price": 100, "strike_time": "2026-09-18", "multiplier": 100,
    }], broker_account_ref={**ref, "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["US", "HK"], asset_types=["stock", "option"], completeness="complete",
        observed_at_utc=datetime.fromtimestamp(7, timezone.utc).isoformat())
    observation = {"portfolio": cash_portfolio({"cash_source_observed_at": datetime.fromtimestamp(7, timezone.utc).isoformat(),
        "capacity_authority": {"status": "available", "logical_account": "lx",
        "futu_account_id": "1001", "trd_env": "REAL", "market": "us"}, "position_snapshot_input": snapshot,
        "cash_by_currency": {"USD": 10000}}, account_id="1001")}
    evidence = {"complete": True, "exposures": []}
    view = build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=7000, combo_evidence=evidence, capacity_observation=observation)
    fact = next(row for row in view["rows"] if row["lot_id"] == "put-fill")
    assert fact["selected_candidate_id"] == "wheel:" + branch["wheel_branch_id"], fact
    # The old final check counts both this booked Put and its unconsumed cash reservation.
    before_check = trade_attribution_capacity_check(config=config, fact=fact, facts=view["rows"], wheel_read_model=view["wheel_model"],
        observation=observation, now_ms=7000)
    assert before_check["reason_codes"] == ["account_cash_capacity_exceeded"]
    args = dict(account="lx", market="us", config=config, execution_key=fact["execution_key"],
        candidate_id=fact["selected_candidate_id"], expected_input_hash=fact["input_hash"], request_id="put-consume",
        actor="trade_intake:rule", combo_evidence=evidence, capacity_observation=observation, combo_mode="confirm")
    if entrypoint == "manual_confirm":
        rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
        candidates = project_wheel_linkage_candidates(view["wheel_model"]["wheel_branches"],
            rows["account_position_lots"], rows["account_wheel_events"])
        candidate = next(row for row in candidates if row["option_record_id"] == fact["lot_id"]
            and row["wheel_branch_id"] == branch["wheel_branch_id"])
        context = dict(config=config, market="us", combo_evidence=evidence,
            capacity_observation=observation, combo_mode="confirm")
        monkeypatch.setattr(attribution, "read_trade_attribution_context", lambda *a, **kw: context)
        confirm_args = dict(account="lx", option_lot_id=fact["lot_id"], wheel_branch_id=branch["wheel_branch_id"],
            direction="put", linkage_candidate_id=candidate["linkage_candidate_id"],
            expected_input_hash=fact["input_hash"], expected_batch_generation_hash=candidate["batch_generation_hash"],
            request_id="put-consume", actor="trade_intake:rule", config=config, runtime_root=tmp_path)
        invoke = lambda apply: attribution.confirm_wheel_linkage(repo, **confirm_args, apply_changes=apply)
    else:
        invoke = lambda apply: apply_trade_attribution(repo, **args, apply_changes=apply)
    before = repo.list_trade_events()
    before_wheel = repo.list_wheel_events(account="lx")
    if entrypoint == "manual_confirm":
        insufficient = deepcopy(observation)
        insufficient["portfolio"]["cash_by_currency"]["USD"] = 9000
        context["capacity_observation"] = insufficient
        for apply in (False, True):
            with pytest.raises(ValueError, match="attribution evidence changed"):
                invoke(apply)
        assert repo.list_trade_events() == before
        assert repo.list_wheel_events(account="lx") == before_wheel
        context["capacity_observation"] = observation
    preview = invoke(False)
    assert not preview["write_applied"]
    assert repo.list_trade_events() == before
    assert repo.list_wheel_events(account="lx") == before_wheel
    result = invoke(True)
    assert result["write_applied"] and result["origin"] == ("manual" if entrypoint == "manual_confirm" else "intent")
    assert not invoke(True)["write_applied"]
    consumed = [event for event in repo.list_wheel_events(account="lx") if event["event_type"] == "wheel_put_intent_consumed"]
    assert len(consumed) == 1 and consumed[0]["payload"]["cash_reservation_amount"] == 10000
    after = build_wheel_read_model(repo, "lx", 7000, market="us")["wheel_branches"][0]
    assert after["active_intent_reserved_contracts"] == 0
    assert len(repo.list_trade_events()) == len(before) + 1
    if entrypoint == "manual_confirm":
        assert result["status"] == "confirmed" and result["proof_event_ids"] == preview["proof_event_ids"]
        proof = next(row for row in repo.list_trade_events() if row["event_id"] in result["proof_event_ids"])
        patch = proof["raw_payload"]["patch"]
        assert patch["strategy"] == "wheel" and patch["leg_role"] == "wheel_put"
        assert patch["source_wheel_branch_id"] == branch["wheel_branch_id"]
        fields = repo.get_position_lot_fields(fact["lot_id"])
        assert all(key not in fields for key in ("strategy", "leg_role", "source_wheel_branch_id"))
        from src.application.ledger.api import read_trade_attribution_facts
        linked = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["lot_id"] == fact["lot_id"])
        assert linked["status"] == "linked" and linked["wheel_branch_id"] == branch["wheel_branch_id"]
        assert fact["lot_id"] in after["active_option_lot_ids"]


@pytest.mark.parametrize("cached", [True, False])
def test_capacity_worker_uses_shared_cash_reader_without_writes(tmp_path, monkeypatch, cached):
    import json
    import src.application.wheel.capacity as capacity
    from cash_evidence_helpers import cash_config, cash_portfolio

    portfolio = cash_portfolio({"cash_by_currency": {"USD": 500}, "exchange_rates": {"stale": True}})
    state = tmp_path / "output_accounts/lx/state"
    state.mkdir(parents=True)
    if cached:
        (state / "portfolio_context.json").write_text(json.dumps(portfolio))
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    calls = []
    def fetch(**kw):
        calls.append(kw)
        assert kw["write_cache"] is False
        assert kw["include_options"] is True
        assert kw["exchange_rate_cache_path"] == tmp_path / "output_shared/state/rate_cache.json"
        assert kw["exchange_rate_observation"] is None
        return portfolio
    def fx(**kw):
        assert kw == {"cache_path": tmp_path / "output_shared/state/rate_cache.json", "write_cache": False}
        raise RuntimeError("FX unavailable")
    monkeypatch.setattr(capacity, "fetch_futu_portfolio_context", fetch)
    monkeypatch.setattr(capacity, "current_exchange_rate_snapshot", fx)
    class Connection:
        def send(self, value):
            self.result = value
        def close(self):
            self.closed = True
    connection = Connection()
    capacity._attribution_capacity_worker(connection, cash_config(), "lx", tmp_path)
    assert connection.closed
    assert connection.result["portfolio"]["cash_snapshot"]["status"] == "fresh"
    assert connection.result["portfolio"]["exchange_rates"] is None
    assert len(calls) == (0 if cached else 1)
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_attribution_pair_keeps_complete_view_and_historical_instants(tmp_path, monkeypatch):
    import src.application.trades.attribution as attribution
    from src.application.wheel import read_model

    rows, config, _branch = _call_scope(tmp_path, monkeypatch)
    args = dict(config=config, account='lx', market='us', now_ms=4000,
                combo_evidence={'complete': True, 'exposures': []})
    paired = attribution.build_wheel_read_model_with_capacity_from_rows
    def independent(rows, *, account, as_of_ms, market, monitoring_readiness=None):
        return (read_model.build_wheel_read_model_from_rows(
            rows, account=account, as_of_ms=as_of_ms, market=market, monitoring_readiness=monitoring_readiness),
            read_model.build_wheel_read_model_from_rows(rows, account=account, as_of_ms=as_of_ms))
    monkeypatch.setattr(attribution, 'build_wheel_read_model_with_capacity_from_rows', independent)
    expected = attribution.build_trade_attribution_view(rows, **args)
    monkeypatch.setattr(attribution, 'build_wheel_read_model_with_capacity_from_rows', paired)
    base = read_model._build_wheel_read_model_base
    instants = []
    def counted(*a, **kw):
        instants.append(kw['as_of_ms'])
        return base(*a, **kw)
    monkeypatch.setattr(read_model, '_build_wheel_read_model_base', counted)
    assert attribution.build_trade_attribution_view(rows, **args) == expected
    assert instants.count(4000) == 1
    assert instants.count(3000) == 1  # Matching call fill still needs its historical branch.
    put_times = {event['event_time_ms'] for event in rows['trade_events']
                 if event['event_type'] == 'open' and event['option_type'] == 'put'}
    assert not put_times.intersection(instants)
