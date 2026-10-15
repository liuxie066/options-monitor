from copy import deepcopy
from threading import Event

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_execution import execution_identity_from_input
from src.application.ledger import api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object


@pytest.fixture
def ordinary_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 10)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    config = {"market": "us", "accounts": ["sy"],
              "account_settings": {"sy": {"futu": {"account_id": "1002", "trd_env": "REAL"}}}}
    return repo, config


def _open(repo, name, *, contracts=3, option="put"):
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": name,
                 "broker_account_ref": {"broker_id": "futu", "external_account_id": "1002", "environment": "REAL"}}
    event = TradeEvent(event_id=name, event_type="open", event_time_ms=1000,
        contract_key=ContractKey.from_values(broker="futu", account="sy", underlying_symbol="NVDA",
            option_type=option, strike=100, expiration_ymd="2026-12-18"),
        contracts=contracts, price=2, multiplier=100, currency="USD", source="test",
        raw_payload={"side": "sell", "execution_input": execution,
                     "execution_id": execution_identity_from_input(execution)})
    persist_trade_event_object(repo, event)
    return event


def _view(repo, config, evidence):
    return api.build_trade_attribution_view(api.read_trade_attribution_snapshot(repo, account="sy", market="us"),
        config=config, account="sy", market="us", now_ms=10000, combo_evidence=evidence)


def _args(repo, config, name, *, complete=False):
    evidence = {"complete": complete, "exposures": []}
    fact = next(row for row in _view(repo, config, evidence)["rows"] if row["open_event_id"] == name)
    return fact, dict(account="sy", market="us", config=config, execution_key=fact["execution_key"],
        candidate_id="ordinary", expected_input_hash=fact["input_hash"], request_id="manual:" + name,
        actor="test:operator", manual=True, combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")


@pytest.mark.parametrize("option,strategy", [("put", "csp"), ("call", "cc")])
def test_manual_ordinary_missing_history_preview_apply_retry_preserves_other_fill(ordinary_scope, option, strategy):
    repo, config = ordinary_scope
    _open(repo, "older-fill", option=option)
    _open(repo, "newer-fill", contracts=4, option=option)
    _, newer_args = _args(repo, config, "newer-fill", complete=True)
    newer = api.apply_trade_attribution(repo, **newer_args)
    protected_ids = {"newer-fill", *newer["proof_event_ids"]}
    protected = [row for row in repo.list_trade_events() if row["event_id"] in protected_ids]
    before = api.read_trade_attribution_snapshot(repo, account="sy", market="us")
    fact, args = _args(repo, config, "older-fill")
    assert fact["status"] == "pending" and not fact["evidence_complete"]
    assert fact["ordinary_previewable"] and fact["reason_codes"] == ["attribution_evidence_incomplete"]
    preview = api.apply_trade_attribution(repo, **args, apply_changes=False)
    assert not preview["write_applied"]
    assert api.read_trade_attribution_snapshot(repo, account="sy", market="us") == before
    result = api.apply_trade_attribution(repo, **args)
    assert result["write_applied"] and result["status"] == "ordinary" and len(result["proof_event_ids"]) == 1
    member = result["decision"]["members"][0]
    assert member["before"]["strategy"] is None and member["after"]["strategy"] == strategy
    assert all(member["after"][key] is None for key in ("strategy_group_id", "source_wheel_branch_id", "wheel_call_allocations"))
    events = repo.list_trade_events()
    proof = next(row for row in events if row["event_id"] in result["proof_event_ids"])
    assert proof["event_type"] == "adjust" and proof["contracts"] == 0
    assert float(proof["price"]) == float(proof["fees"]) == 0
    assert len(events) == len(before["trade_events"]) + 1
    assert [row for row in events if row["event_id"] in protected_ids] == protected
    assert not api.apply_trade_attribution(repo, **args)["write_applied"]
    assert repo.list_trade_events() == events
    with pytest.raises(ValueError, match="request identity conflicts"):
        api.apply_trade_attribution(repo, **{**args, "actor": "test:other-operator"})
    assert repo.list_trade_events() == events


@pytest.mark.parametrize("invalid", ["account", "environment", "quantity", "identity", "successor"])
def test_missing_history_never_bypasses_fill_account_or_projection_checks(ordinary_scope, invalid):
    repo, config = ordinary_scope
    opening = _open(repo, "older-fill")
    if invalid in {"account", "environment"}:
        config = deepcopy(config)
        config["account_settings"]["sy"]["futu"].update(
            {"account_id": "9999"} if invalid == "account" else {"trd_env": "SIMULATE"})
    elif invalid == "quantity":
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute("UPDATE position_lots SET fields_json=json_set(fields_json, '$.contracts_open', 2)")
    elif invalid == "identity":
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute("UPDATE trade_events SET event_json=json_set(event_json, '$.raw_payload.execution_input.errors', json('[\"unproven\"]'))")
    else:
        lot = next(row["lot_id"] for row in api.trade_attribution_facts_from_events(repo.list_trade_events(), account="sy"))
        persist_trade_event_object(repo, TradeEvent(event_id="partial-close", event_type="close", event_time_ms=2000,
            contract_key=opening.contract_key, contracts=1, price=1, multiplier=100, currency="USD", source="test",
            target_lot_id=lot, raw_payload={"side": "buy"}))
    before = api.read_trade_attribution_snapshot(repo, account="sy", market="us")
    fact, args = _args(repo, config, "older-fill")
    assert not fact["ordinary_previewable"]
    with pytest.raises(ValueError, match="identity or dependencies changed"):
        api.apply_trade_attribution(repo, **args)
    assert api.read_trade_attribution_snapshot(repo, account="sy", market="us") == before


@pytest.mark.parametrize("failure", ["stale", "automatic", "cancel", "after-projection"])
def test_manual_ordinary_missing_history_keeps_request_and_atomicity_guards(ordinary_scope, failure):
    repo, config = ordinary_scope
    _open(repo, "older-fill")
    _, args = _args(repo, config, "older-fill")
    before = api.read_trade_attribution_snapshot(repo, account="sy", market="us")
    if failure == "stale":
        args["expected_input_hash"] = "stale"
    elif failure == "automatic":
        args["manual"] = False
    elif failure == "cancel":
        stop = Event()
        stop.set()
        args["stop_event"] = stop
    else:
        calls = []
        def before_commit():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("authority revoked after projection")
        args["before_commit"] = before_commit
    with pytest.raises(ValueError):
        api.apply_trade_attribution(repo, **args)
    if failure == "after-projection":
        assert len(calls) == 2
    assert api.read_trade_attribution_snapshot(repo, account="sy", market="us") == before


@pytest.mark.parametrize("manual", [False, True])
def test_missing_history_still_blocks_wheel_candidate(tmp_path, monkeypatch, manual):
    from test_trade_attribution_view import _writable_call_scope
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    evidence = {"complete": False, "exposures": []}
    rows = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = api.build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=4000,
        combo_evidence=evidence)
    fact = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    with pytest.raises(ValueError, match="identity or dependencies changed"):
        api.apply_trade_attribution(repo, account="lx", market="us", config=config, execution_key=fact["execution_key"],
            candidate_id=fact["candidate_ids"][0], expected_input_hash=fact["input_hash"], request_id="blocked-wheel",
            actor="test:operator", manual=manual, combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == rows


def test_missing_history_does_not_grant_release_of_existing_wheel_relationship(tmp_path, monkeypatch):
    from test_trade_attribution_ledger_command import _command_scope
    repo, args, _ = _command_scope(tmp_path, monkeypatch)
    api.apply_trade_attribution(repo, **args)
    evidence = {"complete": False, "exposures": []}
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = api.build_trade_attribution_view(before, config=args["config"], account="lx", market="us",
        now_ms=4000, combo_evidence=evidence)
    fact = next(row for row in view["rows"] if row["execution_key"] == args["execution_key"])
    assert not fact["ordinary_previewable"]
    with pytest.raises(ValueError, match="identity or dependencies changed"):
        api.apply_trade_attribution(repo, **{**args, "manual": True, "candidate_id": "ordinary",
            "expected_input_hash": fact["input_hash"], "request_id": "release-without-history",
            "combo_evidence": evidence, "member_decisions": ({"execution_key": fact["execution_key"],
                "candidate_id": "ordinary"},)})
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == before


@pytest.mark.parametrize("manual", [False, True])
def test_missing_history_still_blocks_combo_candidate(tmp_path, monkeypatch, manual):
    from test_combo_reconciliation_application import _put_open, _call_open, BASE_TIME_MS
    from test_trades_combo_reconciliation import _exposure
    monkeypatch.setattr("time.time", lambda: (BASE_TIME_MS + 5000) / 1000)
    monkeypatch.setattr("src.application.ledger.trade_attribution.trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    repo = SQLiteOptionPositionsRepository(tmp_path / "combo.sqlite3")
    for event in (_put_open(), _call_open()):
        persist_trade_event_object(repo, event)
    config = {"market": "us", "accounts": ["lx"],
        "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
        "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}}}
    evidence = {"complete": False, "exposures": [_exposure()]}
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = api.build_trade_attribution_view(before, config=config, account="lx", market="us",
        now_ms=BASE_TIME_MS + 5000, combo_evidence=evidence)
    fact = next(row for row in view["rows"] if row["open_event_id"] == "put-open")
    assert fact["candidate_ids"]
    with pytest.raises(ValueError, match="identity or dependencies changed"):
        api.apply_trade_attribution(repo, account="lx", market="us", config=config, execution_key=fact["execution_key"],
            candidate_id=fact["candidate_ids"][0], expected_input_hash=fact["input_hash"], request_id="blocked-combo",
            actor="test:operator", manual=manual, combo_evidence=evidence, capacity_observation={}, combo_mode="confirm")
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == before
