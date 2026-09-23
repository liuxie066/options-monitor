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
        "account": "lx", "market": "us", "policy_version": "trade_attribution.v1", "effective_from_ms": 2500}]
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
    args = dict(fact=fact, facts=[fact], observation=observation, wheel_read_model={"wheel_branches": []})
    result = trade_attribution_capacity_check(**args, now_ms=now)
    assert result["status"] == "available", result
    stale = trade_attribution_capacity_check(**args, now_ms=now + 60001)
    assert "snapshot_observed_at_utc_stale_or_future" in stale["reason_codes"]
    mismatched = trade_attribution_capacity_check(**{**args, "facts": [fact, fact]}, now_ms=now)
    assert "broker_ledger_positions_mismatch" in mismatched["reason_codes"]
    assert "account_stock_capacity_exceeded" in mismatched["reason_codes"]
    from test_wheel_strategy import _started_event, _assignment_trade, _assigned_stock
    from domain.domain.wheel import build_wheel_event, project_wheel_lifecycles, WHEEL_EVENT_SCHEMA_V1
    intent = build_wheel_event(event_id="invalid-units", account="lx", lot_id="assigned-stock-assign-put",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1, event_type="wheel_call_intent_created",
        occurred_at_ms=2100, recorded_at_ms=2101, intent_id="invalid-units",
        payload={"contracts": 1, "multiplier": "100.5", "expires_at_ms": now + 9000})
    from src.application.wheel.read_model import _branch_from_legacy_batch
    branches = [_branch_from_legacy_batch(branch) for branch in project_wheel_lifecycles(
        [_started_event(), intent], [_assignment_trade()], [], _assigned_stock(), now)]
    unknown = trade_attribution_capacity_check(**{**args, "wheel_read_model": {"wheel_branches": branches}}, now_ms=now)
    assert "capacity_basis_unavailable" in unknown["reason_codes"]
    for conflict_type in ("creation", "consumption"):
        created = build_wheel_event(event_id="created", account="lx", lot_id="assigned-stock-assign-put",
            event_schema_version=WHEEL_EVENT_SCHEMA_V1, event_type="wheel_call_intent_created",
            occurred_at_ms=2100, recorded_at_ms=2100, intent_id="conflicted",
            payload={"contracts": 1, "multiplier": 100, "expires_at_ms": now + 9000})
        conflicting = build_wheel_event(event_id="conflicting", account="lx", lot_id="assigned-stock-assign-put",
            event_schema_version=WHEEL_EVENT_SCHEMA_V1,
            event_type="wheel_call_intent_created" if conflict_type == "creation" else "wheel_call_intent_consumed",
            occurred_at_ms=2200, recorded_at_ms=2200, intent_id="conflicted", source_trade_event_id="unknown-fill",
            payload={"contracts": 1, "multiplier": 100, "expires_at_ms": now + 9000})
        branch = _branch_from_legacy_batch(project_wheel_lifecycles(
            [_started_event(), created, conflicting], [_assignment_trade()], [], _assigned_stock(), now)[0])
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
    from src.application.ledger.api import enable_trade_attribution_policy
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
    monkeypatch.setattr("time.time", lambda: 2)
    enable_trade_attribution_policy(repo, scope={"broker": "futu", "physical_account_id": "1001", "environment": "REAL",
        "account": "lx", "market": "us"}, effective_from_ms=2500, actor="test", request_id="enable", now_ms=2000, apply_changes=True)
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
    evidence = {"complete": True, "exposures": []}
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=now_ms, combo_evidence=evidence)
    calls = [row for row in view["rows"] if row["contract_key"]["option_type"] == "call"]
    assert len(calls) == 2 and all(row["selected_candidate_id"] for row in calls), calls
    assert all(len(row["candidates"][0]["member_lot_ids"]) == 2 for row in calls)
    args = dict(account="lx", market="us", config=config, execution_key=calls[0]["execution_key"],
        candidate_id=calls[0]["selected_candidate_id"], expected_input_hash=calls[0]["input_hash"], request_id="two-fills",
        actor="trade_intake:rule", combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
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
    assert len(repo.list_trade_events()) == len(before) + 2


def test_two_independent_writers_cannot_claim_different_memberships(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from src.application.ledger.api import read_trade_attribution_facts, record_trade_ordinary_attribution
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
                return record_trade_ordinary_attribution(second_repo, account="lx", execution_key=fact["execution_key"],
                    expected_input_hash=fact["input_hash"], request_id="ordinary-race", actor="operator", now_ms=4000, apply_changes=True)
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


def test_put_capacity_reuses_fx_pool_and_rejects_wrong_contract_units():
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    observed = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    snapshot = build_futu_position_snapshot(rows=[
        {"code": "US.NVDA261218P00025000", "stock_owner": "US.NVDA", "sec_type": "OPTION", "qty": -1,
         "option_type": "PUT", "option_strike_price": 25, "strike_time": "2026-12-18", "multiplier": 100},
    ], broker_account_ref={**ref, "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["US", "HK"], asset_types=["stock", "option"], observed_at_utc=observed, completeness="complete")
    portfolio = {"capacity_authority": {"status": "available", "logical_account": "lx", "futu_account_id": "1001",
        "trd_env": "REAL", "market": "us"}, "position_snapshot_input": snapshot, "cash_balance_reliable": True,
        "cash_by_currency": {"USD": 0, "HKD": 20000}, "exchange_rates": {"rates": {"USDCNY": 7, "HKDCNY": 0.9}},
        "exchange_rate_status": "ready"}
    fact = {"account": "lx", "broker_account_ref": ref, "contracts_open": 1, "position_side": "short", "multiplier": 100,
            "currency": "USD", "contract_key": {"underlying_symbol": "NVDA", "option_type": "put", "strike": "25.0", "expiration_ymd": "2026-12-18"}}
    args = dict(fact=fact, facts=[fact], observation={"portfolio": portfolio}, wheel_read_model={"wheel_branches": []}, now_ms=now)
    assert trade_attribution_capacity_check(**args)["status"] == "available"
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
    assert "account_cash_capacity_exceeded" in trade_attribution_capacity_check(**args)["reason_codes"]
    portfolio["cash_by_currency"]["USD"] = 2500
    assert trade_attribution_capacity_check(**args)["status"] == "available"
    fact["multiplier"] = 50
    assert "broker_ledger_positions_mismatch" in trade_attribution_capacity_check(**args)["reason_codes"]


def _blocked_capacity_worker(connection, config, account):
    import time
    time.sleep(60)


@pytest.mark.parametrize("cancel", [False, True])
def test_provider_budget_terminates_its_worker(monkeypatch, cancel):
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
    result = capacity.observe_trade_attribution_capacity(config={}, account="lx", stop_event=stop)
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
