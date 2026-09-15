"""Current opening policy must agree with sealed evidence before any new intent."""
from copy import deepcopy

import pytest

import src.application.agent_tools.positions as agent
import src.application.wheel.workflows as workflows
import src.interfaces.cli.wheel as cli
from src.application.account_run import build_account_runtime_config
from src.application.tick_run_workspace import publish_account_run_config
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.opening_candidate_snapshot import strategy_policy_hash
from src.application.wheel import build_wheel_read_model
from tests.test_wheel_workflows import _assign_short_put, _open_test_activation, _trusted_multiplier_payload
from tests.test_wheel_candidate_snapshot import _dependencies


POLICY_A = {"wheel": {"call": {"min_dte": 30}, "put": {"min_dte": 30}}}
POLICY_B = {"wheel": {"call": {"min_dte": 7}, "put": {"min_dte": 7}}}


def _environment(tmp_path, direction, monkeypatch):
    repo, _, stock_id = _assign_short_put(tmp_path, wheel_start_enabled=direction == "call")
    if direction == "put":
        key = ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA",
                                      option_type="call", position_side="short", strike=110,
                                      expiration_ymd="2026-09-18")
        _open_test_activation(repo)
        for event in [
            TradeEvent(event_id="cc-open", event_type="open", event_time_ms=3_000,
                       contract_key=key, contracts=1, price=2, currency="USD", source="test",
                       multiplier=100, lot_id="cc-lot",
                       raw_payload=_trusted_multiplier_payload("cc-open", strategy="cc",
                           leg_role="covered_call", source_stock_lot_id=stock_id)),
            TradeEvent(event_id="cc-assignment", event_type="assignment", event_time_ms=4_000,
                       contract_key=key, contracts=1, price=0, currency="USD", source="test",
                       multiplier=100, target_lot_id="cc-lot", raw_payload={
                           "target_lot_id": "cc-lot", "stock_settlement": {
                               "side": "sell", "shares": 100, "price": 110, "fees": 1,
                               "currency": "USD", "fee_provenance": {"basis": "actual", "source": "test"}}}),
        ]:
            persist_trade_event_objects_atomically(repo, [event])
        monkeypatch.setattr(workflows, "revalidate_selected_wheel_put_candidate_from_rows",
            lambda **_: {"account": "lx", "allocation_status": "allocated", "granted_contracts": 1,
                         "capacity_identity_hash": "capacity", "cash_reservation_amount": 10_000,
                         "cash_reservation_currency": "USD"})
    branch = build_wheel_read_model(repo, "lx", 5_000, market="us")["wheel_branches"][0]
    assert branch["direction"] == direction
    candidate = {"final_candidate_id": "candidate", "symbol": "NVDA", "strike": 100,
                 "expiration_ymd": "2026-09-18", "granted_contracts": 1, "multiplier": 100,
                 "currency": "USD", "direction": direction,
                 "wheel_branch_id": branch["wheel_branch_id"], "stock_lot_id": branch.get("stock_lot_id"),
                 "branch_generation_hash": branch["branch_generation_hash"],
                 "capacity_identity_hash": "capacity", "cash_reservation_currency": "USD"}
    snapshot = {"account": "lx", "snapshot_hash": "snapshot",
                "strategy_policy_sha256": strategy_policy_hash(POLICY_A),
                "batches": [{**branch, "final_candidate": candidate}]}
    capacity = {"account": "lx", "symbol": "NVDA", "capacity_identity_hash": "capacity",
                "status": "available", "shares_eligible": 100, "shares_locked": 0,
                "shares_reserved": 0, "shares_available_for_cover": 100}
    descriptor = repo.get_current_wheel_activation_window(market="us", account="lx")
    resolved = {"market": "us", "enabled_for_new_lifecycle": True,
                "activation_descriptor": descriptor, "policy_sha256": "a" * 64}
    return repo, branch, snapshot, capacity, resolved


def _request(branch, snapshot, capacity, resolved):
    return dict(candidate_snapshot=snapshot, account="lx", wheel_branch_id=branch["wheel_branch_id"],
                direction=branch["direction"], final_candidate_id="candidate", expected_snapshot_hash="snapshot",
                expected_branch_generation_hash=branch["branch_generation_hash"], expires_at_ms=10_000,
                request_id="intent-request", actor="tester", capacity_fact=capacity,
                new_intent_enabled=True, market="us", activation_descriptor=resolved["activation_descriptor"],
                policy_sha256=resolved["policy_sha256"], as_of_ms=5_000)


@pytest.mark.parametrize("entry", ["legacy_call", "call", "put"])
def test_new_intent_rejects_old_policy_and_allows_restored_policy(tmp_path, monkeypatch, entry):
    direction = "put" if entry == "put" else "call"
    repo, branch, snapshot, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
    request = _request(branch, snapshot, capacity, resolved)
    create = workflows.create_wheel_intent
    if entry == "legacy_call":
        create = workflows.create_wheel_call_intent
        request["stock_lot_id"] = branch["stock_lot_id"]
        request["expected_batch_generation_hash"] = request.pop("expected_branch_generation_hash")
        request["coverage_fact"] = request.pop("capacity_fact")
        request.pop("direction")
        request.pop("wheel_branch_id")
    before = repo.list_wheel_events(account="lx")
    for apply in (False, True):
        with pytest.raises(ValueError, match="candidate strategy policy changed"):
            create(repo, **request, current_strategy_policy_sha256=strategy_policy_hash(POLICY_B), apply_changes=apply)
        assert repo.list_wheel_events(account="lx") == before
    missing_snapshot_policy = {key: value for key, value in snapshot.items() if key != "strategy_policy_sha256"}
    for current_hash in (None, "", "invalid", strategy_policy_hash(POLICY_A)):
        with pytest.raises(ValueError, match="policy.*(invalid|changed)"):
            create(repo, **{**request, "candidate_snapshot": missing_snapshot_policy},
                   current_strategy_policy_sha256=current_hash, apply_changes=True)
        assert repo.list_wheel_events(account="lx") == before
    # A -> B -> A uses content identity; no revision is added to sealed artifacts.
    accepted = create(repo, **request, current_strategy_policy_sha256=strategy_policy_hash(POLICY_A), apply_changes=True)
    assert accepted["write_applied"] is True
    after = repo.list_wheel_events(account="lx")
    assert len(after) == len(before) + 1
    if entry != "call":  # neutral Call's existing branch-generation replay contract is unchanged
        replay = create(repo, **{**request, "candidate_snapshot": {}},
                        current_strategy_policy_sha256=strategy_policy_hash(POLICY_B), apply_changes=True)
        assert replay["status"] == "idempotent"
        assert repo.list_wheel_events(account="lx") == after


@pytest.mark.parametrize("entry", ["cli_call", "cli_put", "agent_call", "agent_put", "agent_legacy_call"])
def test_intent_facades_supply_current_global_policy(tmp_path, monkeypatch, entry):
    direction = "put" if entry.endswith("put") else "call"
    repo, branch, snapshot, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
    config = deepcopy(POLICY_B)
    runtime = tmp_path / "config.us.json"
    before = repo.list_wheel_events(account="lx")
    if entry.startswith("cli"):
        monkeypatch.setattr(cli, "_open_runtime", lambda *_a, **_k: (runtime, config, repo))
        monkeypatch.setattr(cli, "_now_ms", lambda: 5_000)
        monkeypatch.setattr(cli, "load_wheel_candidate_snapshot", lambda **_: snapshot)
        monkeypatch.setattr(cli, "resolve_wheel_config", lambda *_a, **_k: resolved)
        monkeypatch.setattr(cli, "_coverage", lambda *_a, **_k: capacity)
        monkeypatch.setattr(cli, "_cash_capacity", lambda *_a, **_k: capacity)
        args = cli.parse_args(["intent", "create", "--config-key", "us", "--account", "lx",
            "--wheel-branch-id", branch["wheel_branch_id"], "--direction", direction,
            "--expected-branch-generation-hash", branch["branch_generation_hash"],
            "--run-id", "old-run", "--final-candidate-id", "candidate", "--expected-snapshot-hash", "snapshot",
            "--expires-at-ms", "10000", "--request-id", "intent-request", "--actor", "tester"])
        with pytest.raises(ValueError, match="candidate strategy policy changed"):
            cli.execute(args)
        config.clear()
        config.update(deepcopy(POLICY_A))
        assert cli.execute(args)["status"] == "planned"
    else:
        monkeypatch.setattr(agent, "_wheel_runtime", lambda _: (runtime, config, repo, {}))
        monkeypatch.setattr(agent, "_wheel_now_ms", lambda _: 5_000)
        monkeypatch.setattr(agent, "load_wheel_candidate_snapshot", lambda **_: snapshot)
        monkeypatch.setattr(agent, "resolve_wheel_config", lambda *_a, **_k: resolved)
        monkeypatch.setattr(agent, "_wheel_coverage", lambda *_a, **_k: capacity)
        monkeypatch.setattr(agent, "_wheel_cash_capacity", lambda *_a, **_k: capacity)
        payload = dict(config_key="us", account="lx", action="create", direction=direction,
            wheel_branch_id=branch["wheel_branch_id"], expected_branch_generation_hash=branch["branch_generation_hash"],
            run_id="old-run", final_candidate_id="candidate", expected_snapshot_hash="snapshot",
            expires_at_ms=10_000, request_id="intent-request", actor="tester", apply=False)
        tool = agent.WHEEL_INTENT_TOOL
        if entry == "agent_legacy_call":
            tool = agent.WHEEL_CALL_INTENT_TOOL
            payload.pop("direction")
            payload.pop("wheel_branch_id")
            payload["stock_lot_id"] = branch["stock_lot_id"]
            payload["expected_batch_generation_hash"] = payload.pop("expected_branch_generation_hash")
        with pytest.raises(AgentToolError, match="candidate strategy policy changed"):
            tool.call(payload)
        config.clear()
        config.update(deepcopy(POLICY_A))
        assert tool.call(payload)[0]["status"] == "planned"
    assert repo.list_wheel_events(account="lx") == before


@pytest.mark.parametrize("entry", ["cli_call", "cli_put", "agent_call", "agent_put", "agent_legacy_call"])
def test_scoped_published_candidate_preserves_current_policy_checks(tmp_path, monkeypatch, entry):
    direction = "put" if entry.endswith("put") else "call"
    repo, branch, fixture, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
    runtime = tmp_path / "config.us.json"
    config = {**deepcopy(POLICY_A), "symbols": [
        {"symbol": "NVDA", "broker": "futu", "sell_put": {"min_dte": 30}},
        {"symbol": "AAPL", "broker": "futu"},
    ]}
    retained = build_account_runtime_config(base_cfg=config, cfg_path=runtime, account="lx",
                                           markets_to_run=["futu"], symbols_arg="NVDA")
    authority = publish_account_run_config(base=tmp_path, run_id="scoped", account="lx", config=retained)
    candidate = {**fixture["batches"][0]["final_candidate"], "candidate_id": "candidate"}
    if direction == "put":
        candidate.update(capacity_identity_hash="c" * 64, allocation_input_hash="d" * 64,
                         cash_reservation_amount=10_000)
        monkeypatch.setattr(workflows, "revalidate_selected_wheel_put_candidate_from_rows",
            lambda **_: {"account": "lx", "allocation_status": "allocated", "granted_contracts": 1,
                         "capacity_identity_hash": "c" * 64, "cash_reservation_amount": 10_000,
                         "cash_reservation_currency": "USD"})
    batch = {**branch, "projection_hash": branch["branch_generation_hash"],
             "raw_candidates": [candidate], "final_candidate": candidate, "granted_contracts": 1}
    snapshot = seal_wheel_candidate_snapshot(
        base=tmp_path, run_id="scoped", account="lx", market="us",
        account_config_sha256=authority.account_config_sha256,
        strategy_policy_sha256=strategy_policy_hash(retained), dependencies=_dependencies(),
        scope_results=[{"symbol": "NVDA", "direction": direction,
                        "status": "completed", "candidate_count": 1}], batches=[batch],
    )
    assert snapshot["strategy_policy_sha256"] != strategy_policy_hash(config)
    before = repo.list_wheel_events(account="lx")
    if entry.startswith("cli"):
        monkeypatch.setattr(cli, "_open_runtime", lambda *_a, **_k: (runtime, config, repo))
        monkeypatch.setattr(cli, "_now_ms", lambda: 5_000)
        monkeypatch.setattr(cli, "resolve_wheel_config", lambda *_a, **_k: resolved)
        monkeypatch.setattr(cli, "_coverage", lambda *_a, **_k: capacity)
        monkeypatch.setattr(cli, "_cash_capacity", lambda *_a, **_k: capacity)
        args = cli.parse_args(["intent", "create", "--config-key", "us", "--account", "lx",
            "--wheel-branch-id", branch["wheel_branch_id"], "--direction", direction,
            "--expected-branch-generation-hash", branch["branch_generation_hash"],
            "--run-id", "scoped", "--final-candidate-id", "candidate", "--expected-snapshot-hash", snapshot["snapshot_hash"],
            "--expires-at-ms", "10000", "--request-id", "intent-request", "--actor", "tester"])
        invoke = lambda: cli.execute(args)
        error = ValueError
    else:
        monkeypatch.setattr(agent, "_wheel_runtime", lambda _: (runtime, config, repo, {}))
        monkeypatch.setattr(agent, "_wheel_now_ms", lambda _: 5_000)
        monkeypatch.setattr(agent, "resolve_wheel_config", lambda *_a, **_k: resolved)
        monkeypatch.setattr(agent, "_wheel_coverage", lambda *_a, **_k: capacity)
        monkeypatch.setattr(agent, "_wheel_cash_capacity", lambda *_a, **_k: capacity)
        payload = dict(config_key="us", account="lx", action="create", direction=direction,
            wheel_branch_id=branch["wheel_branch_id"], expected_branch_generation_hash=branch["branch_generation_hash"],
            run_id="scoped", final_candidate_id="candidate", expected_snapshot_hash=snapshot["snapshot_hash"],
            expires_at_ms=10_000, request_id="intent-request", actor="tester", apply=False)
        tool = agent.WHEEL_INTENT_TOOL
        if entry == "agent_legacy_call":
            tool = agent.WHEEL_CALL_INTENT_TOOL
            payload.pop("direction")
            payload.pop("wheel_branch_id")
            payload["stock_lot_id"] = branch["stock_lot_id"]
            payload["expected_batch_generation_hash"] = payload.pop("expected_branch_generation_hash")
        invoke = lambda: tool.call(payload)[0]
        error = AgentToolError
    # Real published config and sealed snapshot loaders feed the real intent gate.
    assert invoke()["status"] == "planned"
    original = deepcopy(config)
    for change in ("symbol_removed", "symbol_policy", "wheel_policy", "templates"):
        config.clear()
        config.update(deepcopy(original))
        if change == "symbol_removed":
            config["symbols"].pop(0)
        elif change == "symbol_policy":
            config["symbols"][0]["sell_put"]["min_dte"] = 7
        elif change == "wheel_policy":
            config["wheel"]["call"]["min_dte"] = 7
        else:
            config["templates"] = {"changed": {"min_dte": 7}}
        with pytest.raises(error, match="candidate strategy policy changed"):
            invoke()
        assert repo.list_wheel_events(account="lx") == before
    config.clear()
    config.update(original)
    retained_bytes = authority.state_path.read_bytes()
    authority.state_path.unlink()
    with pytest.raises(error, match="candidate strategy policy changed"):
        invoke()
    if entry in {"cli_put", "agent_put", "agent_legacy_call"}:
        authority.state_path.write_bytes(retained_bytes)
        if entry.startswith("cli"):
            args.apply = args.confirm = True
        else:
            payload.update(apply=True, confirm=True)
        assert invoke()["write_applied"] is True
        accepted_events = repo.list_wheel_events(account="lx")
        authority.state_path.unlink()
        config["wheel"]["call"]["min_dte"] = 7
        replay = invoke()
        assert replay["status"] == "idempotent"
        assert replay["write_applied"] is False
        assert repo.list_wheel_events(account="lx") == accepted_events
