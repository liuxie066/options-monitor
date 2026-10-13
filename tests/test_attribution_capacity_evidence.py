"""Unavailable provider data must not overwrite durable manual ownership."""
from copy import deepcopy

import pytest

from domain.domain.strategy_membership import resolve_trade_attribution
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.ledger import trade_attribution as ledger_attribution
from src.application.trades import attribution
from test_trade_attribution_full_branches import (
    EVIDENCE, NOW, _ingest, _observation, _occupied_scope, _view,
)


def _fully_linked(tmp_path, monkeypatch, *, all_rules_enabled=False):
    repo, config, branches, execution, _ = _occupied_scope(tmp_path, monkeypatch,
        policy_effective_from_ms=1 if all_rules_enabled else None)
    observation = _observation(repo)
    target = next(row for row in _view(repo, config, observation=observation)["rows"]
                  if row["execution_key"] == execution)
    attribution.apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=execution, candidate_id="wheel:" + branches[-1],
        expected_input_hash=target["input_hash"], request_id="fixture:last-branch",
        actor="fixture:operator", combo_evidence=EVIDENCE, capacity_observation=observation,
        combo_mode="confirm", manual=True)
    return repo, config, branches, execution


def _recover(repo, config, tmp_path, monkeypatch, observation):
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity",
                        lambda **k: observation)
    return attribution.reconcile_trade_attribution_account(repo, config=config, account="lx", market="hk",
        runtime_root=tmp_path, inbox_path=tmp_path / "unused-inbox.sqlite3", combo_mode="confirm")


@pytest.mark.parametrize("unknown", ["missing", "stale", "partial", "account", "market", "quantity", "parity"])
def test_unknown_with_pending_competitor_cannot_persist_conflict_or_erase_manual(tmp_path, monkeypatch, unknown):
    repo, config, _, execution = _fully_linked(tmp_path, monkeypatch)
    pending, _ = _ingest(repo, config, tmp_path, deal_id="new-pending", occurred_at="2026-10-06T07:34:04Z")
    observation = _observation(repo, now_ms=NOW - 61000 if unknown == "stale" else NOW)
    snapshot = observation["portfolio"]["position_snapshot_input"]
    if unknown == "missing":
        observation = {}
    elif unknown == "partial":
        snapshot["completeness"] = "partial"
    elif unknown == "account":
        snapshot["broker_account_ref"]["account_label"] = "sy"
    elif unknown == "market":
        snapshot["scope"]["markets"] = ["US"]
    elif unknown == "quantity":
        snapshot["rows"][0]["quantity"] = "-1"
    elif unknown == "parity":
        snapshot["rows"] = [r for r in snapshot["rows"] if r["instrument_ref"]["asset_type"] == "stock"]
    before = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    view = _view(repo, config, observation=observation)
    manual = [r for r in view["rows"] if r["origin"] == "manual" and r["contracts_open"] > 0]
    assert len(manual) == 3 and all(r["status"] == "linked" for r in manual)
    assert next(r for r in manual if r["execution_key"] == execution)["status"] == "linked"
    competitor = next(r for r in view["rows"] if r["execution_key"] == pending)
    assert competitor["selected_candidate_id"] is None
    for row in manual + [competitor]:
        for candidate in row["candidates"]:
            assert candidate["capacity_evidence_complete"] is False
            assert not {"account_stock_capacity_exceeded", "competing_fills_exceed_capacity"}.intersection(candidate["reason_codes"])
    result = _recover(repo, config, tmp_path, monkeypatch, observation)
    assert result["conflicts"] == result["linked"] == 0 and not result["errors"]
    assert read_trade_attribution_snapshot(repo, account="lx", market="hk") == before


@pytest.mark.parametrize("all_rules_enabled", [False, True])
def test_proven_zero_stock_records_only_real_branch_claims_and_survives_retry(tmp_path, monkeypatch, all_rules_enabled):
    repo, config, branches, execution = _fully_linked(tmp_path, monkeypatch, all_rules_enabled=all_rules_enabled)
    pending, _ = _ingest(repo, config, tmp_path, deal_id="new-competing", occurred_at="2026-10-06T07:34:04Z")
    observation = _observation(repo)
    observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    before_view = _view(repo, config, observation=observation)
    affected = {r["execution_key"] for r in before_view["rows"] if r["contracts_open"] > 0}
    before = repo.list_trade_events()
    result = _recover(repo, config, tmp_path, monkeypatch, observation)
    assert result["conflicts"] == 1 and not result["errors"], result
    conflicts = [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"]
    assert conflicts
    event = conflicts[0]
    if not all_rules_enabled:
        assert len(conflicts) == 1 and event["wheel_branch_id"] == branches[-1]
    # Different branch owners share the depleted stock pool and are truly affected.
    assert all(set(e["payload"]["execution_keys"]) == affected for e in conflicts)
    assert {execution, pending} <= affected
    diagnostic = event["payload"]["diagnostic"]
    assert diagnostic["reason_codes"] == ["late_capacity_conflict"]
    assert "account_stock_capacity_exceeded" in diagnostic["candidate_reason_codes"]["wheel:" + event["wheel_branch_id"]]
    assert len(diagnostic["capacity_evidence"]["semantic_sha256"]) == 64
    assert diagnostic["capacity_evidence"]["snapshot"]["completeness"] == "complete"
    assert diagnostic["evaluated_at_ms"] == NOW
    assert repo.list_trade_events() == before
    # Evidence recovery must not silently erase, extend or duplicate a durable conflict.
    healthy = _observation(repo)
    healthy["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "3000"
    recovered = _recover(repo, config, tmp_path, monkeypatch, healthy)
    assert recovered["conflicts"] == 0 and not recovered["errors"]
    assert [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"] == conflicts
    view = _view(repo, config, observation=healthy)
    assert next(r for r in view["rows"] if r["execution_key"] == execution)["reason_codes"] == ["unresolved_attribution_conflict"]
    assert all(r["status"] == "conflict" for r in view["rows"] if r["execution_key"] in affected)


def test_multi_branch_shortage_is_detected_once_without_double_counting(tmp_path, monkeypatch):
    repo, config, _, _ = _fully_linked(tmp_path, monkeypatch)
    healthy = _observation(repo)
    view = _view(repo, config, observation=healthy)
    multi = next(r for r in view["rows"] if r.get("wheel_call_allocations"))
    assert multi["status"] == "linked"
    zero = deepcopy(healthy)
    zero["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    multi = next(r for r in _view(repo, config, observation=zero)["rows"] if r.get("wheel_call_allocations"))
    assert multi["status"] == "conflict" and multi["reason_codes"] == ["late_capacity_conflict"]
    assert all(c["capacity_evidence_complete"] for c in multi["candidates"])


def test_independent_intent_overflow_is_not_hidden_by_unknown_provider_capacity():
    existing = {"status": "linked", "origin": "manual", "strategy": "wheel", "candidate_id": "wheel:a"}
    candidate = {"candidate_id": "wheel:a", "strategy": "wheel", "capacity_evidence_complete": False,
                 "reason_codes": ["capacity_authority_unavailable", "competing_fills_exceed_intent_remainder"]}
    result = resolve_trade_attribution(candidates=(candidate,), evidence_complete=False, existing=existing)
    assert result.status == "conflict" and result.reason_codes == ("late_capacity_conflict",)


def test_branch_only_conflict_does_not_include_other_manual_candidate_owner():
    def row(key, owner, *, pending=False):
        return {"execution_key": key, "contracts_open": 1, "wheel_branch_id": owner,
            "account": "fixture", "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"},
            "strategy_group_id": None, "status": "pending" if pending else "linked",
            "contract_key": {"underlying_symbol": "NVDA", "option_type": "call"},
            "candidates": [{"candidate_id": "wheel:a", "capacity_evidence_complete": True,
                "reason_codes": ["competing_fills_exceed_capacity"]}]}
    trigger = row("trigger", "a")
    competitor = row("pending", None, pending=True)
    unrelated = row("other-owner", "b")
    assert attribution._conflict_execution_keys([trigger, competitor, unrelated], "a", trigger) == ["pending", "trigger"]

    shortage = deepcopy(trigger)
    shortage["candidates"][0]["reason_codes"] = ["account_stock_capacity_exceeded"]
    resource_owner = deepcopy(unrelated)
    resource_owner["candidates"][0]["reason_codes"] = ["account_stock_capacity_exceeded"]
    assert attribution._conflict_execution_keys([shortage, resource_owner], "a", shortage) == ["other-owner", "trigger"]
    for mutation in ("symbol", "account", "environment"):
        isolated = deepcopy(resource_owner)
        if mutation == "symbol":
            isolated["contract_key"]["underlying_symbol"] = "AAPL"
        elif mutation == "account":
            isolated["account"] = "other"
        else:
            isolated["broker_account_ref"]["environment"] = "SIMULATE"
        assert attribution._conflict_execution_keys([shortage, isolated], "a", shortage) == ["trigger"]


def test_cancel_after_transaction_recheck_does_not_publish_conflict(tmp_path, monkeypatch):
    from threading import Event

    repo, config, _, _ = _fully_linked(tmp_path, monkeypatch)
    observation = _observation(repo)
    observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    before = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    stop = Event()
    original = attribution.build_trade_attribution_view
    calls = 0

    def cancel_after_recheck(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            stop.set()
        return result

    monkeypatch.setattr(attribution, "build_trade_attribution_view", cancel_after_recheck)
    monkeypatch.setattr(ledger_attribution, "build_trade_attribution_view", cancel_after_recheck)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: observation)
    result = attribution.reconcile_trade_attribution_account(repo, config=config, account="lx", market="hk",
        runtime_root=tmp_path, inbox_path=tmp_path / "unused-inbox.sqlite3", combo_mode="confirm", stop_event=stop)
    assert calls == 2 and stop.is_set()
    assert result["conflicts"] == 0 and result["errors"]
    assert read_trade_attribution_snapshot(repo, account="lx", market="hk") == before


def test_capacity_expiring_at_conflict_transaction_cannot_publish(tmp_path, monkeypatch):
    repo, config, _, _ = _fully_linked(tmp_path, monkeypatch)
    observation = _observation(repo)
    observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    before = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    original = ledger_attribution.with_sqlite_repo_transaction

    def expire_before_recheck(*args, **kwargs):
        monkeypatch.setattr(attribution.time, "time", lambda: (NOW + 61001) / 1000)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_attribution, "with_sqlite_repo_transaction", expire_before_recheck)
    result = _recover(repo, config, tmp_path, monkeypatch, observation)
    assert result["conflicts"] == 0 and result["errors"]
    assert read_trade_attribution_snapshot(repo, account="lx", market="hk") == before


@pytest.mark.parametrize("late_failure", ["cancel", "expire"])
def test_conflict_write_rolls_back_if_cancelled_or_expired_before_commit(tmp_path, monkeypatch, late_failure):
    from threading import Event

    repo, config, _, _ = _fully_linked(tmp_path, monkeypatch)
    observation = _observation(repo)
    observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    before = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    stop = Event()
    original = ledger_attribution.record_trade_attribution_conflict
    writes = []

    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        writes.append(result)
        if late_failure == "cancel":
            stop.set()
        else:
            monkeypatch.setattr(attribution.time, "time", lambda: (NOW + 61001) / 1000)
        return result

    monkeypatch.setattr(ledger_attribution, "record_trade_attribution_conflict", fail_after_write)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: observation)
    result = attribution.reconcile_trade_attribution_account(repo, config=config, account="lx", market="hk",
        runtime_root=tmp_path, inbox_path=tmp_path / "unused-inbox.sqlite3", combo_mode="confirm", stop_event=stop)
    assert writes  # The provisional append really occurred inside the transaction.
    assert result["conflicts"] == 0 and result["errors"]
    assert read_trade_attribution_snapshot(repo, account="lx", market="hk") == before


_CASH_NOW = 1790748000000  # Verified FX quote session, before the National Day holiday.

def _cross_market_put_scope(tmp_path, monkeypatch, *, ordinary_metadata=False):
    """Real assigned-stock -> called-away -> Put branches in one temporary ledger."""
    from datetime import datetime, timezone
    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.wheel import build_wheel_read_model
    from src.application.wheel.config import resolve_wheel_activation_descriptor
    from test_wheel_intent_policy_binding import _environment

    seed = tmp_path / "cash-seed"
    seed.mkdir()
    source, _, _, _, _ = _environment(seed, "put", monkeypatch)
    repo = SQLiteOptionPositionsRepository(tmp_path / "cash-cohort.sqlite3")
    configs, fills, branches = {}, {}, {}
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: _CASH_NOW - 6500)
    monkeypatch.setattr(attribution.time, "time", lambda: _CASH_NOW / 1000)
    monkeypatch.setattr("src.infrastructure.exchange_rates._utc_now", lambda: datetime.fromtimestamp(_CASH_NOW / 1000, timezone.utc))
    scopes = [("us", "lx", "REAL"), ("hk", "lx", "REAL"), ("hk", "sy", "REAL")]
    for market, account, environment in scopes:
        prefix = f"{market}-{account}-{environment}"
        symbol, currency, multiplier = ("NVDA", "USD", 100) if market == "us" else ("0700.HK", "HKD", 100)
        config = {"market": market, "_resolved": {"market": market}, "accounts": [account],
            "portfolio": {"source": "futu", "base_currency": "CNY"},
            "account_settings": {account: {"futu": {"account_id": "1001", "trd_env": environment}}},
            "wheel": {"accounts": [account], "activation_by_account": {account: {
                "generation": 1, "activated_at_ms": _CASH_NOW - 6500, "deactivated_at_ms": None}}}}
        configs[(market, account, environment)] = config
        descriptor = resolve_wheel_activation_descriptor(config, market=market, account=account)
        # One logical market activation; broker environment remains bound by the trade evidence.
        if environment == "REAL":
            with repo._writer_connection(begin_immediate=True) as conn:
                repo.open_wheel_activation_window(market=market, account=account, expected_current_generation=0,
                    policy_hash=descriptor["policy_hash"], request_id=prefix + ":window", request_hash="b" * 64, conn=conn)
                conn.execute("""INSERT INTO trade_attribution_policy_enablings
                    (broker, physical_account_id, environment, account, market, policy_version,
                     effective_from_ms, created_at_ms, actor, request_id, request_hash)
                    VALUES ('futu', '1001', ?, ?, ?, 'trade_attribution.v2', ?, ?, 'fixture', ?, ?)""",
                    (environment, account, market, _CASH_NOW - 2500, _CASH_NOW - 2500, prefix + ":cutover", "a" * 64))
        ref = {"broker_id": "futu", "external_account_id": "1001", "environment": environment}
        def scoped(value):
            if isinstance(value, dict):
                return {key: scoped(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scoped(item) for item in value]
            if isinstance(value, str) and (value.startswith("wheel-source-") or value.startswith("assigned-stock-") or value.startswith("cc-")):
                return prefix + ":" + value
            return value
        for original in source.list_trade_events():
            event = scoped(deepcopy(original))
            event["event_time_ms"] += _CASH_NOW - 7000
            event["contract_key"].update(account=account, underlying_symbol=symbol, expiration_ymd="2026-12-18")
            event.update(currency=currency, multiplier=multiplier)
            if "stock_settlement" in event["raw_payload"]:
                event["raw_payload"]["stock_settlement"]["currency"] = currency
            if event["event_type"] == "open":
                execution = {"external_id_namespace": "futu.deal", "external_execution_id": event["event_id"], "broker_account_ref": ref}
                event["raw_payload"].update(execution_input=execution, execution_id=execution_identity_from_input(execution))
            persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(event)])
        projected = build_wheel_read_model(repo, account, _CASH_NOW - 2000, market=market)["wheel_branches"]
        branch = next(row for row in projected if row["direction"] == "put" and prefix in row["source_assignment_event_id"])
        assert branch["integrity_status"] == "trusted" and branch["lifecycle_status"] == "active"
        branches[prefix] = branch["wheel_branch_id"]
        execution = {"external_id_namespace": "futu.deal", "external_execution_id": prefix + ":put-fill", "broker_account_ref": ref}
        fills[prefix] = execution_identity_from_input(execution)
        persist_trade_event_objects_atomically(repo, [TradeEvent(event_id=prefix + ":put-fill", event_type="open", event_time_ms=_CASH_NOW - 1000,
            contract_key=ContractKey.from_values(broker="futu", account=account, underlying_symbol=symbol,
                option_type="put", strike=100, expiration_ymd="2026-12-18"), contracts=1, price=1,
            currency=currency, multiplier=multiplier, source="test", lot_id=prefix + ":put-fill",
            raw_payload={"side": "sell", "execution_input": execution, "execution_id": fills[prefix], "multiplier_source": "payload"})])
    # An ordinary CSP consumes cash too, but has no Wheel ownership to suspend.
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": "fixture:ordinary-csp",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}
    fills["ordinary-lx-REAL"] = execution_identity_from_input(execution)
    persist_trade_event_objects_atomically(repo, [TradeEvent(event_id="fixture:ordinary-csp", event_type="open",
        event_time_ms=_CASH_NOW - 1000, contract_key=ContractKey.from_values(broker="futu", account="lx",
            underlying_symbol="AAPL", option_type="put", strike=100, expiration_ymd="2026-12-18"),
        contracts=1, price=1, currency="USD", multiplier=100, source="test", lot_id="fixture:ordinary-csp",
        raw_payload={"side": "sell", "execution_input": execution,
            "execution_id": fills["ordinary-lx-REAL"], "multiplier_source": "payload",
            **({"strategy": "csp", "source_stock_lot_id": "fixture:unrelated-stock"} if ordinary_metadata else {})})])
    # Establish the manual links with fully observed healthy cash, never bypassing the capacity owner.
    for market in ("us", "hk"):
        config = configs[(market, "lx", "REAL")]
        observation = _cross_market_cash_observation(repo, market=market, cash_hkd=200000)
        view = _cash_view(repo, config, market, observation)
        fill = next(row for row in view["rows"] if row["execution_key"] == fills[f"{market}-lx-REAL"])
        assert fill["candidates"][0]["capacity_evidence_complete"] is True, [c["reason_codes"] for c in fill["candidates"]]
        attribution.apply_trade_attribution(repo, account="lx", market=market, config=config,
            execution_key=fill["execution_key"], candidate_id="wheel:" + branches[f"{market}-lx-REAL"],
            expected_input_hash=fill["input_hash"], request_id="fixture:cash:" + market, actor="fixture:operator",
            combo_evidence=EVIDENCE, capacity_observation=observation, combo_mode="confirm", manual=True)
    return repo, configs, fills, branches


def _cross_market_cash_observation(repo, *, market, cash_hkd, unavailable=None):
    from datetime import datetime, timezone
    from cash_evidence_helpers import cash_portfolio
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    from src.application.ledger.api import read_trade_attribution_facts

    observed = datetime.fromtimestamp(_CASH_NOW / 1000, timezone.utc).isoformat()
    positions = []
    for fact in read_trade_attribution_facts(repo, account="lx"):
        if fact["contracts_open"] <= 0 or fact["broker_account_ref"]["environment"] != "REAL":
            continue
        symbol = fact["contract_key"]["underlying_symbol"]
        owner = "HK." + symbol.removesuffix(".HK").zfill(5) if symbol.endswith(".HK") else "US." + symbol
        positions.append({"code": owner + "261218P00100000", "stock_owner": owner, "sec_type": "OPTION",
            "qty": -fact["contracts_open"], "option_type": "PUT", "option_strike_price": 100,
            "strike_time": "2026-12-18", "multiplier": fact["multiplier"]})
    snapshot = build_futu_position_snapshot(rows=positions,
        broker_account_ref={"broker_id": "futu", "external_account_id": "1001", "environment": "REAL",
            "account_label": "lx", "broker_account_id": "futu:REAL:1001"}, markets=["US", "HK"],
        asset_types=["stock", "option"], completeness="complete", observed_at_utc=observed)
    portfolio = cash_portfolio({"cash_source_observed_at": observed, "capacity_authority": {"market": market},
        "position_snapshot_input": snapshot, "cash_by_currency": {"USD": 0, "HKD": cash_hkd},
        "exchange_rates": {"pairs": {pair: {"rate": rate, "source": "tencent_quote",
            "quote_at_utc": observed, "observed_at_utc": observed} for pair, rate in (("USDCNY", 7), ("HKDCNY", 0.9))}},
        "exchange_rate_status": "ready"}, account_id="1001")
    if unavailable == "fx":
        portfolio["exchange_rates"] = {"rates": {"USDCNY": 7, "HKDCNY": 0.9}}
    elif unavailable == "cash":
        portfolio["cash_by_currency"] = None
    elif unavailable == "scope":
        portfolio["position_snapshot_input"]["scope"]["markets"] = [market.upper()]
    return {"portfolio": portfolio}


def _cash_view(repo, config, market, observation):
    return attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market=market),
        config=config, account="lx", market=market, now_ms=_CASH_NOW, combo_evidence=EVIDENCE,
        capacity_observation=observation, combo_mode="confirm")


@pytest.mark.parametrize("cash_hkd", [0, 80000])
@pytest.mark.parametrize("first_market", ["us", "hk"])
@pytest.mark.parametrize("ordinary_metadata", [False, True])
def test_cross_market_fx_cash_shortage_persists_actual_claimants_once(tmp_path, monkeypatch, cash_hkd, first_market, ordinary_metadata):
    repo, configs, fills, _ = _cross_market_put_scope(tmp_path, monkeypatch, ordinary_metadata=ordinary_metadata)
    observation = _cross_market_cash_observation(repo, market=first_market, cash_hkd=cash_hkd)
    for market in ("us", "hk"):
        view = _cash_view(repo, configs[(market, "lx", "REAL")], market,
            _cross_market_cash_observation(repo, market=market, cash_hkd=cash_hkd))
        fill = next(row for row in view["rows"] if row["execution_key"] == fills[f"{market}-lx-REAL"])
        assert fill["status"] == "conflict" and fill["reason_codes"] == ["late_capacity_conflict"], fill
        assert fill["candidates"][0]["capacity_evidence_complete"] is True
        assert "account_cash_capacity_exceeded" in fill["candidates"][0]["reason_codes"]
    before = repo.list_trade_events()
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: observation)
    args = dict(config=configs[(first_market, "lx", "REAL")], account="lx", market=first_market, runtime_root=tmp_path,
        inbox_path=tmp_path / "cash-inbox.sqlite3", combo_mode="confirm")
    result = attribution.reconcile_trade_attribution_account(repo, **args)
    assert result["conflicts"] == 1 and not result["errors"], result
    conflicts = [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"]
    assert len(conflicts) == 1
    affected = {fills["us-lx-REAL"], fills["hk-lx-REAL"]}
    assert set(conflicts[0]["payload"]["execution_keys"]) == affected
    assert fills["hk-sy-REAL"] not in conflicts[0]["payload"]["execution_keys"]
    assert fills["ordinary-lx-REAL"] not in conflicts[0]["payload"]["execution_keys"]
    diagnostic = conflicts[0]["payload"]["diagnostic"]
    assert diagnostic["reason_codes"] == ["late_capacity_conflict"]
    assert diagnostic["capacity_evidence"]["authority"]["market"] == first_market
    assert diagnostic["capacity_evidence"]["snapshot"]["completeness"] == "complete"
    assert len(diagnostic["capacity_evidence"]["semantic_sha256"]) == 64
    retry = attribution.reconcile_trade_attribution_account(repo, **args)
    assert retry["conflicts"] == 0 and not retry["errors"]
    assert [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"] == conflicts
    assert repo.list_trade_events() == before
    for market in ("us", "hk"):
        # The other market's next scheduled batch must consume the same durable
        # conflict, never create a second effect for the same cash-pool shortage.
        next_observation = _cross_market_cash_observation(repo, market=market, cash_hkd=cash_hkd)
        monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: next_observation)
        replay = attribution.reconcile_trade_attribution_account(repo,
            **{**args, "config": configs[(market, "lx", "REAL")], "market": market})
        assert replay["conflicts"] == replay["linked"] == 0 and not replay["errors"], replay
    assert [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"] == conflicts
    assert repo.list_trade_events() == before
    # A new repository reader and healthy evidence cannot silently clear either market's sticky conflict.
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    reopened = SQLiteOptionPositionsRepository(repo.db_path)
    for market in ("us", "hk"):
        view = _cash_view(reopened, configs[(market, "lx", "REAL")], market,
            _cross_market_cash_observation(reopened, market=market, cash_hkd=200000))
        fill = next(row for row in view["rows"] if row["execution_key"] == fills[f"{market}-lx-REAL"])
        assert fill["status"] == "conflict" and fill["reason_codes"] == ["unresolved_attribution_conflict"]


@pytest.mark.parametrize("unavailable", ["cash", "fx", "scope"])
def test_cross_market_unknown_cash_cannot_create_durable_conflict(tmp_path, monkeypatch, unavailable):
    repo, configs, fills, _ = _cross_market_put_scope(tmp_path, monkeypatch)
    before = read_trade_attribution_snapshot(repo, account="lx", market="us")
    for market in ("us", "hk"):
        observation = _cross_market_cash_observation(repo, market=market, cash_hkd=80000, unavailable=unavailable)
        view = _cash_view(repo, configs[(market, "lx", "REAL")], market, observation)
        fill = next(row for row in view["rows"] if row["execution_key"] == fills[f"{market}-lx-REAL"])
        assert fill["status"] == "linked" and fill["reason_codes"] == ["existing_attribution_preserved"], fill
        assert all(c["capacity_evidence_complete"] is False for c in fill["candidates"])
        assert all("account_cash_capacity_exceeded" not in c["reason_codes"] for c in fill["candidates"])
        monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
        monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: observation)
        result = attribution.reconcile_trade_attribution_account(repo, config=configs[(market, "lx", "REAL")],
            account="lx", market=market, runtime_root=tmp_path,
            inbox_path=tmp_path / "unknown-inbox.sqlite3", combo_mode="confirm")
        assert result["conflicts"] == result["linked"] == 0 and not result["errors"], result
    assert read_trade_attribution_snapshot(repo, account="lx", market="us") == before
    assert not [e for e in repo.list_wheel_events(account="lx") if e["event_type"] == "wheel_attribution_conflict"]


def test_cross_market_mixed_environment_cash_is_unproven_and_cannot_conflict(tmp_path, monkeypatch):
    from domain.domain.ledger import TradeEvent
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.writer import persist_trade_event_objects_atomically

    repo, configs, fills, _ = _cross_market_put_scope(tmp_path, monkeypatch)
    # Retain a real SIMULATE fill in the same logical account. A REAL snapshot
    # cannot prove that mixed-environment ledger; never charge or pause it by guess.
    foreign = deepcopy(next(e for e in repo.list_trade_events() if e["event_id"] == "us-lx-REAL:put-fill"))
    foreign.update(event_id="fixture:simulate-put", lot_id="fixture:simulate-put")
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": foreign["event_id"],
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "SIMULATE"}}
    foreign["raw_payload"].update(execution_input=execution, execution_id=execution_identity_from_input(execution))
    persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(foreign)])
    before = read_trade_attribution_snapshot(repo, account="lx", market="us")
    observation = _cross_market_cash_observation(repo, market="us", cash_hkd=0)
    view = _cash_view(repo, configs[("us", "lx", "REAL")], "us", observation)
    live = next(row for row in view["rows"] if row["execution_key"] == fills["us-lx-REAL"])
    assert live["status"] == "linked"
    assert all(c["capacity_evidence_complete"] is False for c in live["candidates"])
    assert "ledger_capacity_account_unproven" in live["candidates"][0]["reason_codes"]
    other = next(row for row in view["rows"] if row["execution_key"] == execution_identity_from_input(execution))
    assert other["selected_candidate_id"] is None and "configured_physical_account_mismatch" in other["reason_codes"]
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **k: EVIDENCE)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **k: observation)
    result = attribution.reconcile_trade_attribution_account(repo, config=configs[("us", "lx", "REAL")], account="lx",
        market="us", runtime_root=tmp_path, inbox_path=tmp_path / "env-inbox.sqlite3", combo_mode="confirm")
    assert result["conflicts"] == result["linked"] == 0 and not result["errors"], result
    assert read_trade_attribution_snapshot(repo, account="lx", market="us") == before
