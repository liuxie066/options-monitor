"""Current opening policy must agree with sealed evidence before any new intent."""
import json
from hashlib import sha256
from cash_evidence_helpers import cash_config, cash_portfolio
from src.application.portfolio_context_service import cash_snapshot_evidence
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import src.application.agent_tools.positions as agent
import src.application.wheel.workflows as workflows
import src.interfaces.cli.wheel as cli
from src.application.account_run import build_account_runtime_config
from src.application.tick_run_workspace import publish_account_run_config
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.ledger.api import decision_state_snapshot
from src.application.opening_candidate_snapshot import strategy_policy_hash
from src.application.wheel import build_wheel_read_model
from tests.test_wheel_workflows import _assign_short_put, _open_test_activation, _trusted_multiplier_payload
from tests.test_wheel_candidate_snapshot import _dependencies


POLICY_A = {"wheel": {"call": {"min_dte": 30}, "put": {"min_dte": 30}}}
POLICY_B = {"wheel": {"call": {"min_dte": 7}, "put": {"min_dte": 7}}}


@pytest.mark.parametrize("multiplier", [None, "100.5", "duplicate"])
def test_historical_v2_invalid_intent_units_remain_unknown(tmp_path, monkeypatch, multiplier):
    from domain.domain.wheel import build_wheel_event
    repo, branch, _, _, _ = _environment(tmp_path, "put", monkeypatch)
    event = build_wheel_event(event_id="invalid-intent", account="lx", lot_id=None,
        wheel_branch_id=branch["wheel_branch_id"], event_type="wheel_put_intent_created",
        occurred_at_ms=5100, recorded_at_ms=5100, intent_id="invalid-intent", payload={
            "contracts": 1, "multiplier": 100 if multiplier == "duplicate" else multiplier, "expires_at_ms": 9000, "strike": 100,
            "cash_reservation_currency": "USD", "capacity_identity_hash": "capacity"})
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(event, conn=conn)
        if multiplier == "duplicate":
            repo.append_wheel_event_once(build_wheel_event(event_id="duplicate", account="lx", lot_id=None,
                wheel_branch_id=branch["wheel_branch_id"], event_type="wheel_put_intent_created",
                occurred_at_ms=5200, recorded_at_ms=5200, intent_id="invalid-intent", payload=event["payload"]), conn=conn)
    current = build_wheel_read_model(repo, "lx", 6000, market="us")["wheel_branches"][0]
    assert current["integrity_status"] == "conflict"
    assert current["active_intent_reserved_shares"] is None
    assert current["coverage"]["reserved_shares"] is None
    assert current["coverage"]["available_shares"] is None


def _patch_cli(monkeypatch, *, runtime, config, repo, resolved, capacity) -> None:
    monkeypatch.setattr(cli, "_open_runtime", lambda *_a, **_k: (runtime, config, repo))
    monkeypatch.setattr(cli, "_now_ms", lambda: 5_000)
    monkeypatch.setattr(cli, "resolve_wheel_config", lambda *_a, **_k: resolved)
    monkeypatch.setattr(cli, "_coverage", lambda *_a, **_k: capacity)
    monkeypatch.setattr(cli, "_cash_capacity", lambda *_a, **_k: pytest.fail("create must use original cash"))


def _patch_agent(monkeypatch, *, runtime, config, repo, resolved, capacity) -> None:
    monkeypatch.setattr(agent, "_wheel_runtime", lambda _: (runtime, config, repo, {}))
    monkeypatch.setattr(agent, "_wheel_now_ms", lambda _: 5_000)
    monkeypatch.setattr(agent, "resolve_wheel_config", lambda *_a, **_k: resolved)
    monkeypatch.setattr(agent, "_wheel_coverage", lambda *_a, **_k: capacity)
    monkeypatch.setattr(agent, "_wheel_cash_capacity", lambda *_a, **_k: pytest.fail("create must use original cash"))


def _cli_intent_args(branch, direction, *, run_id, expected_snapshot_hash):
    return cli.parse_args(["intent", "create", "--config-key", "us", "--account", "lx",
        "--wheel-branch-id", branch["wheel_branch_id"], "--direction", direction,
        "--expected-batch-generation-hash", branch["batch_generation_hash"], "--run-id", run_id,
        "--final-candidate-id", "candidate", "--expected-snapshot-hash", expected_snapshot_hash,
        "--expires-at-ms", "10000", "--request-id", "intent-request", "--actor", "tester"])


def _agent_payload(branch, direction, *, run_id, expected_snapshot_hash):
    return dict(config_key="us", account="lx", action="create", direction=direction,
        wheel_branch_id=branch["wheel_branch_id"], expected_batch_generation_hash=branch["batch_generation_hash"],
        run_id=run_id, final_candidate_id="candidate", expected_snapshot_hash=expected_snapshot_hash,
        expires_at_ms=10_000, request_id="intent-request", actor="tester", apply=False)


def _environment(tmp_path, direction, monkeypatch):
    if direction == "put":
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(5, timezone.utc)
        monkeypatch.setattr(workflows, "datetime", Clock)
    repo, _, stock_id = _assign_short_put(tmp_path, wheel_start_enabled=direction == "call")
    if direction == "put":
        key = ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA",
                                      option_type="call", strike=110, expiration_ymd="2026-09-18")
        _open_test_activation(repo)
        for event in [
            TradeEvent(event_id="cc-open", event_type="open", event_time_ms=3_000, contract_key=key,
                       contracts=1, price=2, currency="USD", source="test", multiplier=100, lot_id="cc-lot",
                       # §9.2 step 3: the contract key no longer carries the
                       # position side, so the short call side travels as the
                       # trade side.
                       raw_payload=_trusted_multiplier_payload("cc-open", side="sell", strategy="cc",
                           leg_role="covered_call", source_stock_lot_id=stock_id)),
            TradeEvent(event_id="cc-assignment", event_type="assignment", event_time_ms=4_000,
                       contract_key=key, contracts=1, price=0, currency="USD", source="test",
                       multiplier=100, target_lot_id="cc-lot", raw_payload={
                           # §9.2 step 3: closing a short call is a buy.
                           "side": "buy",
                           "target_lot_id": "cc-lot", "stock_settlement": {
                               "side": "sell", "shares": 100, "price": 110, "fees": 1, "currency": "USD",
                               "fee_provenance": {"basis": "actual", "source": "test"}}}),
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
                 "currency": "USD", "direction": direction, "wheel_branch_id": branch["wheel_branch_id"],
                 "stock_lot_id": branch.get("stock_lot_id"), "batch_generation_hash": branch["batch_generation_hash"],
                 "capacity_identity_hash": "capacity", "cash_reservation_currency": "USD"}
    snapshot = {"account": "lx", "snapshot_hash": "snapshot",
                "strategy_policy_sha256": strategy_policy_hash(POLICY_A),
                "batches": [{**branch, "final_candidate": candidate}]}
    observed_at = datetime.now(timezone.utc).isoformat()
    capacity = {"account": "lx", "symbol": "NVDA", "capacity_identity_hash": "capacity", "status": "available",
                "source_observed_at": observed_at,
                "shares_eligible": 100, "shares_locked": 0, "shares_reserved": 0, "shares_available_for_cover": 100}
    if direction == "put":
        capacity["cash_evidence"] = cash_snapshot_evidence(_cash())
    if direction == "call":
        decision = decision_state_snapshot(repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"),
            source_observed_at=observed_at, current_decision_now_ms=5_000)
        capacity["decision_state_fingerprint"] = decision["decision_state_fingerprint"]
    descriptor = repo.get_current_wheel_activation_window(market="us", account="lx")
    resolved = {"market": "us", "enabled_for_new_lifecycle": True, "account_configured": True,
                "activation_descriptor": descriptor, "policy_sha256": "a" * 64}
    return repo, branch, snapshot, capacity, resolved


def _request(branch, snapshot, capacity, resolved):
    return dict(candidate_snapshot=snapshot, account="lx", wheel_branch_id=branch["wheel_branch_id"],
                direction=branch["direction"], final_candidate_id="candidate", expected_snapshot_hash="snapshot",
                expected_batch_generation_hash=branch["batch_generation_hash"], expires_at_ms=10_000,
                request_id="intent-request", actor="tester", capacity_fact=capacity,
                **({"runtime_config": cash_config()} if branch["direction"] == "put" else {}),
                new_intent_enabled=True, account_configured=True, market="us", activation_descriptor=resolved["activation_descriptor"],
                policy_sha256=resolved["policy_sha256"], as_of_ms=5_000)


def test_unowned_put_linkage_gates_branch_and_new_intent_until_rejected(tmp_path, monkeypatch):
    repo, branch, snapshot, capacity, resolved = _environment(tmp_path, "put", monkeypatch)
    assert branch["phase"] == "ready"
    assert branch["stock_lot_id"] is None
    persist_trade_event_objects_atomically(repo, [TradeEvent(
        event_id="unowned-put-open", event_type="open", event_time_ms=4_500,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-09-18"),
        contracts=1, price=1, currency="USD", source="test", multiplier=100,
        lot_id="unowned-put-lot",
        raw_payload=_trusted_multiplier_payload("unowned-put-open", side="sell"),
    )])

    model = build_wheel_read_model(repo, "lx", 5_000, market="us")
    candidate = next(item for item in model["linkage_candidates"] if item["direction"] == "put")
    current = next(item for item in model["wheel_branches"]
                   if item["wheel_branch_id"] == branch["wheel_branch_id"])
    assert candidate["wheel_branch_id"] == current["wheel_branch_id"]
    assert candidate.get("stock_lot_id") is None
    assert current["stock_lot_id"] is None
    assert current["phase"] == "linkage_unresolved"
    assert "linkage_unresolved" in current["coverage"]["reason_codes"]

    before = repo.list_wheel_events(account="lx")
    for apply in (False, True):
        with pytest.raises(ValueError, match="not ready for a Put intent"):
            workflows.create_wheel_intent(
                repo, **_request(branch, snapshot, capacity, resolved),
                current_strategy_policy_sha256=strategy_policy_hash(POLICY_A),
                apply_changes=apply,
            )
        assert repo.list_wheel_events(account="lx") == before

    result = workflows.reject_wheel_linkage(
        repo, account="lx", option_lot_id=candidate["option_record_id"],
        wheel_branch_id=branch["wheel_branch_id"], direction="put",
        linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="reject-unowned-put", actor="tester", reason="not this cycle",
        market="us", apply_changes=True, as_of_ms=5_000,
    )
    assert result["status"] == "rejected"
    after = build_wheel_read_model(repo, "lx", 5_000, market="us")
    assert all(item["linkage_candidate_id"] != candidate["linkage_candidate_id"]
               for item in after["linkage_candidates"])
    restored = next(item for item in after["wheel_branches"]
                    if item["wheel_branch_id"] == branch["wheel_branch_id"])
    assert restored["phase"] == "ready"

    # Attribution removes a later candidate and lets the normal option-open phase win.
    persist_trade_event_objects_atomically(repo, [TradeEvent(
        event_id="attributed-put-open", event_type="open", event_time_ms=4_600,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-09-18"),
        contracts=1, price=1, currency="USD", source="test", multiplier=100,
        lot_id="attributed-put-lot",
        raw_payload=_trusted_multiplier_payload("attributed-put-open", side="sell"),
    )])
    pending = build_wheel_read_model(repo, "lx", 5_000, market="us")
    assert any(item["option_open_event_id"] == "attributed-put-open"
               for item in pending["linkage_candidates"])
    assert pending["wheel_branches"][0]["phase"] == "linkage_unresolved"
    persist_trade_event_objects_atomically(repo, [TradeEvent(
        event_id="attributed-put-proof", event_type="adjust", event_time_ms=4_700,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-09-18"),
        contracts=0, price=0, currency="USD", source="test", multiplier=100,
        target_lot_id="attributed-put-lot",
        raw_payload={"adjust_target_source_event_id": "attributed-put-open", "patch": {
            "strategy": "wheel", "leg_role": "wheel_put",
            "source_wheel_branch_id": branch["wheel_branch_id"],
        }},
    )])
    attributed = build_wheel_read_model(repo, "lx", 5_000, market="us")
    assert all(item["option_open_event_id"] != "attributed-put-open"
               for item in attributed["linkage_candidates"])
    assert attributed["wheel_branches"][0]["phase"] == "option_open"


@pytest.mark.parametrize("entry", ["legacy_call", "call", "put"])
def test_new_intent_rejects_old_policy_and_allows_restored_policy(tmp_path, monkeypatch, entry):
    direction = "put" if entry == "put" else "call"
    repo, branch, snapshot, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
    request = _request(branch, snapshot, capacity, resolved)
    create = workflows.create_wheel_intent
    if entry == "legacy_call":
        create = workflows.create_wheel_call_intent
        request["lot_id"] = branch["stock_lot_id"]
        request["expected_batch_generation_hash"] = request.pop("expected_batch_generation_hash")
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
    config = {**cash_config(), **deepcopy(POLICY_B)}
    runtime = tmp_path / "config.us.json"
    if direction == "put":
        retained = {**cash_config(), **deepcopy(POLICY_A)}
        retained["portfolio"]["account"] = "lx"
        authority = publish_account_run_config(base=tmp_path, run_id="old-run", account="lx", config=retained)
        snapshot.update(run_id="old-run", account_config_sha256=authority.account_config_sha256,
                        dependencies=_cash_dependencies(tmp_path, "old-run"))
    before = repo.list_wheel_events(account="lx")
    if entry.startswith("cli"):
        _patch_cli(monkeypatch, runtime=runtime, config=config, repo=repo, resolved=resolved, capacity=capacity)
        monkeypatch.setattr(cli, "load_wheel_candidate_snapshot", lambda **_: snapshot)
        args = _cli_intent_args(branch, direction, run_id="old-run", expected_snapshot_hash="snapshot")
        with pytest.raises(ValueError, match="candidate strategy policy changed"):
            cli.execute(args)
        config.clear()
        config.update({**cash_config(), **deepcopy(POLICY_A)})
        assert cli.execute(args)["status"] == "planned"
    else:
        _patch_agent(monkeypatch, runtime=runtime, config=config, repo=repo, resolved=resolved, capacity=capacity)
        monkeypatch.setattr(agent, "load_wheel_candidate_snapshot", lambda **_: snapshot)
        payload = _agent_payload(branch, direction, run_id="old-run", expected_snapshot_hash="snapshot")
        tool = agent.WHEEL_INTENT_TOOL
        if entry == "agent_legacy_call":
            tool = agent.WHEEL_CALL_INTENT_TOOL
            payload.pop("direction")
            payload.pop("wheel_branch_id")
            payload["stock_lot_id"] = branch["stock_lot_id"]
            payload["expected_batch_generation_hash"] = payload.pop("expected_batch_generation_hash")
        with pytest.raises(AgentToolError, match="candidate strategy policy changed"):
            tool.call(payload)
        config.clear()
        config.update({**cash_config(), **deepcopy(POLICY_A)})
        assert tool.call(payload)[0]["status"] == "planned"
    assert repo.list_wheel_events(account="lx") == before


@pytest.mark.parametrize("entry", ["cli_call", "cli_put", "agent_call", "agent_put", "agent_legacy_call"])
def test_scoped_published_candidate_preserves_current_policy_checks(tmp_path, monkeypatch, entry):
    direction = "put" if entry.endswith("put") else "call"
    repo, branch, fixture, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
    runtime = tmp_path / "config.us.json"
    config = {**cash_config(), **deepcopy(POLICY_A), "symbols": [
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
    batch = {**branch, "projection_hash": branch["batch_generation_hash"],
             "raw_candidates": [candidate], "final_candidate": candidate, "granted_contracts": 1}
    snapshot = seal_wheel_candidate_snapshot(
        base=tmp_path, run_id="scoped", account="lx", market="us",
        account_config_sha256=authority.account_config_sha256,
        strategy_policy_sha256=strategy_policy_hash(retained),
        dependencies=_cash_dependencies(tmp_path, "scoped") if direction == "put" else _dependencies(),
        run_mode={"scan_mode": "standard", "executable": True},
        scope_results=[{"symbol": "NVDA", "direction": direction,
                        "status": "completed", "candidate_count": 1}], batches=[batch],
    )
    assert snapshot["strategy_policy_sha256"] != strategy_policy_hash(config)
    before = repo.list_wheel_events(account="lx")
    if entry.startswith("cli"):
        _patch_cli(monkeypatch, runtime=runtime, config=config, repo=repo, resolved=resolved, capacity=capacity)
        args = _cli_intent_args(branch, direction, run_id="scoped", expected_snapshot_hash=snapshot["snapshot_hash"])
        invoke = lambda: cli.execute(args)
        error = ValueError
    else:
        _patch_agent(monkeypatch, runtime=runtime, config=config, repo=repo, resolved=resolved, capacity=capacity)
        payload = _agent_payload(branch, direction, run_id="scoped", expected_snapshot_hash=snapshot["snapshot_hash"])
        tool = agent.WHEEL_INTENT_TOOL
        if entry == "agent_legacy_call":
            tool = agent.WHEEL_CALL_INTENT_TOOL
            payload.pop("direction")
            payload.pop("wheel_branch_id")
            payload["stock_lot_id"] = branch["stock_lot_id"]
            payload["expected_batch_generation_hash"] = payload.pop("expected_batch_generation_hash")
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
        if direction == "put":
            original_clock = workflows.datetime
            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls.fromtimestamp(906, timezone.utc)
            # Both public facades still submit historical as_of_ms=5000.
            monkeypatch.setattr(workflows, "datetime", ExpiredClock)
            with pytest.raises(error, match="repreview required"):
                invoke()
            assert repo.list_wheel_events(account="lx") == before
            monkeypatch.setattr(workflows, "datetime", original_clock)
        assert invoke()["write_applied"] is True
        if direction == "put":
            monkeypatch.setattr(workflows, "datetime", ExpiredClock)
        accepted_events = repo.list_wheel_events(account="lx")
        authority.state_path.unlink()
        config["wheel"]["call"]["min_dte"] = 7
        replay = invoke()
        assert replay["status"] == "idempotent"
        assert replay["write_applied"] is False
        assert repo.list_wheel_events(account="lx") == accepted_events


def _cash():
    return cash_portfolio({"cash_by_currency": {"USD": 20000},
                           "source_observed_at": "1970-01-01T00:00:05+00:00"})


def _cash_dependencies(base, run_id):
    deps = _dependencies()
    state = base / "output_runs" / run_id / "accounts/lx/state"
    for kind, name, payload in (("portfolio", "portfolio_context.json", _cash()),
                               ("ledger", "option_positions_context.json", {"exchange_rates": {"rates": {}}})):
        path = state / name
        path.write_text(json.dumps(payload))
        deps = [{"kind": kind, "relpath": str(path.relative_to(base)),
                 "sha256": sha256(path.read_bytes()).hexdigest()} if row["kind"] == kind else row for row in deps]
    return deps
