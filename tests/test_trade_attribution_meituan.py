from __future__ import annotations

from datetime import datetime
import time

import pytest
from unittest.mock import patch

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.trade_execution import execution_identity_from_input
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.trades.auto_intake import _process_payload
from src.application.trades import attribution
from src.application.trades.attribution import build_trade_attribution_view, read_attribution_combo_evidence, apply_trade_attribution
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.wheel.config import resolve_wheel_activation_descriptor


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _call_payload(*, deal_id: str, contracts: int) -> dict:
    return {"schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL",
                               "broker_account_id": "futu:REAL:1001", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "HK", "symbol": "3690.HK", "currency": "HKD",
            "option_type": "call", "strike": "80", "expiration_ymd": "2026-11-27", "multiplier": "500"},
        "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": "order-" + deal_id,
        "side": "sell", "position_effect": "open", "quantity": str(contracts), "price": "1.5", "currency": "HKD",
        "occurred_at_utc": "2026-09-27T02:45:31Z", "status": "OK"}


def _meituan_repo(tmp_path, *, call_contracts: int = 1):
    ledger = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    ledger.parent.mkdir(parents=True)
    repo = SQLiteOptionPositionsRepository(ledger)
    config = {"market": "hk", "accounts": ["lx"],
              "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
              "wheel": {"accounts": ["lx"], "activation_by_account": {"lx": {
                  "generation": 1, "activated_at_ms": _ms("2026-09-01T00:00:00Z"), "deactivated_at_ms": None}}}}
    descriptor = resolve_wheel_activation_descriptor(config, market="hk", account="lx")
    with patch("src.application.ledger.repository_assigned_stock.now_ms", return_value=_ms("2026-09-01T00:00:00Z")):
        with repo._writer_connection(begin_immediate=True) as conn:
            repo.open_wheel_activation_window(market="hk", account="lx", expected_current_generation=0,
                policy_hash=descriptor["policy_hash"], request_id="meituan-window", request_hash="b" * 64, conn=conn)
    contract = ContractKey.from_values(broker="富途", account="lx", underlying_symbol="3690.HK",
        option_type="put", strike=75, expiration_ymd="2026-09-25")
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    for index in range(5):
        put_id = f"put-{index}"
        opened_at = _ms("2026-09-01T01:00:00Z") + index * 1000
        put_execution = {"external_id_namespace": "futu.deal", "external_execution_id": put_id,
            "broker_account_ref": ref}
        persist_trade_event_objects_atomically(repo, [TradeEvent(
            event_id=put_id, event_type="open", event_time_ms=opened_at, contract_key=contract,
            contracts=1, price=2, multiplier=500, currency="HKD", source="test", lot_id=f"lot-{put_id}",
            raw_payload={"side": "sell", "source_type": "broker_trade_event", "source_deal_id": put_id,
                         "external_event_key": f"futu:{put_id}", "multiplier_source": "payload",
                         "execution_input": put_execution,
                         "execution_id": execution_identity_from_input(put_execution)},
        )])
        persist_trade_event_objects_atomically(repo, [TradeEvent(
            event_id=f"assign-{index}", event_type="assignment", event_time_ms=opened_at + 86400000,
            contract_key=contract, contracts=1, price=0, multiplier=500, currency="HKD", source="test",
            target_lot_id=f"lot-{put_id}", raw_payload={"side": "buy", "target_lot_id": f"lot-{put_id}",
                "stock_settlement": {"side": "buy", "shares": 500, "price": 75, "fees": 0,
                    "currency": "HKD", "fee_provenance": {"basis": "actual", "source": "test"}}},
        )], wheel_start_enabled=True)
    payload = _call_payload(deal_id="meituan-call-fill", contracts=call_contracts)
    result = _process_payload(payload, repo=repo, state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl", account_mapping={"1001": "lx"}, futu_account_ids=["1001"],
        apply_changes=True, host="127.0.0.1", port=11111, allow_external_lookup=False,
        config=config, runtime_root=tmp_path)
    return repo, config, result


def test_meituan_five_covered_branches_enter_pending_via_trade_ingress(tmp_path, monkeypatch):
    repo, config, result = _meituan_repo(tmp_path)
    assert result["status"] == "applied", result
    call_event = next(e for e in repo.list_trade_events() if e["event_type"] == "open" and e["option_type"] == "call")
    assert call_event["lot_id"] is None
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    evidence = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path, now_ms=_ms("2026-10-01T00:00:00Z"))
    view = build_trade_attribution_view(rows, config=config, account="lx", market="hk", now_ms=_ms("2026-10-01T00:00:00Z"), combo_evidence=evidence)
    call = next(r for r in view["rows"] if r["contract_key"]["option_type"] == "call")
    assert "attribution_result" in result, (result.get("attribution_error"),
        {key: call.get(key) for key in ("execution_key", "contracts_open", "status", "reason_codes", "candidate_ids")})
    assert result["attribution_result"]["status"] == "pending", result
    assert len(result["attribution_result"]["candidate_ids"]) == 5, [(c["candidate_id"], c["reason_codes"], c["eligible"]) for c in call["candidates"]]
    assert all(not (row.get("raw_payload") or {}).get("source_stock_lot_id")
               for row in repo.list_trade_events() if row["event_type"] == "open" and row["option_type"] == "call")
    from src.application.daily_decision_brief_service import _pending_attribution_for_brief
    from src.application.daily_decision_brief_renderer import _attribution_review_lines
    pending, read_error = _pending_attribution_for_brief(base=tmp_path, config=config, account="lx",
        market="HK", now_ms=_ms("2026-10-01T00:00:00Z"))
    assert read_error is None and len(pending) == 1
    assert pending[0]["execution_key"] == result["attribution_result"]["execution_key"]
    assert "待确认归属｜1 笔" in _attribution_review_lines({"attribution_pending": pending})[0]
    missing_config = {**config, "account_settings": {}}
    unavailable = build_trade_attribution_view(rows, config=missing_config, account="lx", market="hk",
        now_ms=_ms("2026-10-01T00:00:00Z"), combo_evidence=evidence)
    blocked_call = next(row for row in unavailable["rows"] if row["contract_key"]["option_type"] == "call")
    assert blocked_call["selected_candidate_id"] is None
    assert "configured_physical_account_mismatch" in blocked_call["reason_codes"]
    # Complete scan and broker capacity are isolated here; their fail-closed contracts have separate tests.
    evidence = {"complete": True, "exposures": []}
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
        lambda **_: {"status": "available", "reason_codes": []})
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="hk",
        now_ms=_ms("2026-10-01T00:00:00Z"), combo_evidence=evidence)
    call = next(r for r in view["rows"] if r["contract_key"]["option_type"] == "call")
    assert len(call["candidate_ids"]) == 5 and call["selected_candidate_id"] is None
    chosen = call["candidate_ids"][0]
    with pytest.raises(ValueError, match="evidence changed"):
        apply_trade_attribution(repo, account="lx", market="hk", config=config,
            execution_key=call["execution_key"], candidate_id=chosen, expected_input_hash="stale",
            request_id="meituan-stale", actor="fixture:operator", combo_evidence=evidence,
            capacity_observation={}, combo_mode="confirm", manual=True)
    applied = apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=call["execution_key"], candidate_id=chosen, expected_input_hash=call["input_hash"],
        request_id="meituan-manual-1", actor="fixture:operator", combo_evidence=evidence,
        capacity_observation={}, combo_mode="confirm", manual=True)
    assert applied["status"] == "linked" and applied["wheel_branch_id"] == chosen.removeprefix("wheel:")
    repeated = apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=call["execution_key"], candidate_id=chosen, expected_input_hash=call["input_hash"],
        request_id="meituan-manual-1", actor="fixture:operator", combo_evidence=evidence,
        capacity_observation={}, combo_mode="confirm", manual=True)
    assert repeated["write_applied"] is False
    assert len([row for row in repo.list_trade_events() if row["event_type"] == "open" and row["option_type"] == "call"]) == 1
    from src.application.ledger.api import read_trade_attribution_facts
    linked = next(row for row in read_trade_attribution_facts(repo, account="lx")
                  if row["contract_key"]["option_type"] == "call")
    assert linked["status"] == "linked" and linked["wheel_branch_id"] == chosen.removeprefix("wheel:")
    from src.application.wheel.read_model import build_wheel_read_model
    branches = build_wheel_read_model(repo, "lx", int(time.time() * 1000) + 1000, market="hk")["wheel_branches"]
    assert len(branches) == 5
    assert sum(linked["lot_id"] in branch.get("active_option_lot_ids", []) for branch in branches) == 1
    assert {branch["wheel_branch_id"]: branch["active_option_committed_shares"] for branch in branches} == {
        branch["wheel_branch_id"]: (500 if branch["wheel_branch_id"] == linked["wheel_branch_id"] else 0)
        for branch in branches}
    pending_after, read_error = _pending_attribution_for_brief(base=tmp_path, config=config, account="lx",
        market="HK", now_ms=int(time.time() * 1000) + 1000)
    assert read_error is None and pending_after == []


def test_historical_futu_assignment_inherits_exact_source_account():
    from src.application.trades.attribution import _branch_account_ref

    source_id = "futu:lx:1001:put-deal"
    rows = {"trade_events": [
        {"event_id": source_id, "event_type": "open", "account": "lx", "broker": "富途",
         "lot_id": None, "raw_payload": {"futu_account_id": "1001", "trd_env": "REAL",
             "source_deal_id": "put-deal"}},
        {"event_id": "assigned", "event_type": "assignment", "target_lot_id": "lot_" + source_id,
         "raw_payload": {}},
    ], "account_wheel_events": []}
    branch = {"source_assignment_event_id": "assigned"}
    assert _branch_account_ref(branch, rows) == {
        "broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    rows["trade_events"][0]["raw_payload"]["futu_account_id"] = "other"
    assert _branch_account_ref(branch, rows) is None


def test_manual_preview_reads_combo_evidence_for_target_date_only(tmp_path, monkeypatch):
    repo, config, _intake = _meituan_repo(tmp_path)
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    call = next(row for row in rows["trade_events"] if row["event_type"] == "open"
                and row["option_type"] == "call")
    checked = []
    def read_one(**kwargs):
        checked.append(kwargs["market_trading_date"])
        return {"available": True, "complete": True, "delivery_available": True,
                "reason": "ok", "exposures": []}
    monkeypatch.setattr(attribution, "read_combo_candidate_exposures", read_one)
    evidence = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path,
        now_ms=_ms("2026-10-01T00:00:00Z"), focus_open_event_id=call["event_id"])
    assert evidence["complete"] is True and len(checked) == 1
    assert checked == ["2026-09-27"]
    all_evidence = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path,
        now_ms=_ms("2026-10-01T00:00:00Z"))
    assert len(checked) > 1
    args = dict(rows=rows, config=config, account="lx", market="hk", now_ms=_ms("2026-10-01T00:00:00Z"))
    focused = build_trade_attribution_view(**args, combo_evidence=evidence)
    all_dates = build_trade_attribution_view(**args, combo_evidence=all_evidence)
    assert next(row for row in focused["rows"] if row["open_event_id"] == call["event_id"])["input_hash"] == next(
        row for row in all_dates["rows"] if row["open_event_id"] == call["event_id"])["input_hash"]


def test_three_contract_fill_can_be_confirmed_across_three_stock_branches(tmp_path, monkeypatch):
    repo, config, intake = _meituan_repo(tmp_path, call_contracts=3)
    assert intake["status"] == "applied"
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    evidence = {"complete": True, "exposures": []}
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="hk",
        now_ms=_ms("2026-10-01T00:00:00Z"), combo_evidence=evidence)
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    assert call["contracts"] == 3 and call["status"] == "pending"
    assert all("wheel_branch_capacity_exceeded" in item["reason_codes"]
               for item in call["candidates"] if item["strategy"] == "wheel")
    chosen = tuple(sorted(item["wheel_branch_id"] for item in call["candidates"] if item["strategy"] == "wheel")[:3])
    candidate_id = "wheel-multi:" + canonical_sha256(sorted(chosen))[:24]
    args = dict(account="lx", market="hk", config=config, execution_key=call["execution_key"],
                candidate_id=candidate_id, expected_input_hash=call["input_hash"], request_id="manual:3",
                actor="fixture:operator", combo_evidence=evidence, capacity_observation={},
                combo_mode="confirm", manual=True, wheel_branch_ids=chosen)
    planned = apply_trade_attribution(repo, **args, apply_changes=False)
    assert [row["contracts"] for row in planned["wheel_call_allocations"]] == [1, 1, 1]
    assert len(repo.list_trade_events()) == len(rows["trade_events"])
    with pytest.raises(ValueError, match="evidence changed"):
        apply_trade_attribution(repo, **{**args, "expected_input_hash": "stale"})
    with pytest.raises(ValueError, match="exceeds unclaimed branch capacity"):
        duplicate = (chosen[0], chosen[0], chosen[1])
        apply_trade_attribution(repo, **{**args, "wheel_branch_ids": duplicate,
            "candidate_id": "wheel-multi:" + canonical_sha256(sorted(duplicate))[:24]})
    applied = apply_trade_attribution(repo, **args)
    assert applied["status"] == "linked" and applied["origin"] == "manual"
    assert applied["wheel_branch_id"] is None
    assert len(applied["wheel_call_allocations"]) == 3
    assert apply_trade_attribution(repo, **args)["write_applied"] is False
    call_opens = [row for row in repo.list_trade_events()
                  if row["event_type"] == "open" and row["option_type"] == "call"]
    assert len(call_opens) == 1 and call_opens[0]["contracts"] == 3
    assert len([row for row in repo.list_trade_events()
                if row["event_type"] == "adjust" and row["source"] == "wheel_linkage"]) == 1
    after_rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    after_view = build_trade_attribution_view(after_rows, config=config, account="lx", market="hk",
        now_ms=int(time.time() * 1000) + 1000, combo_evidence=evidence)
    assert next(row for row in after_view["rows"]
                if row["execution_key"] == call["execution_key"])["status"] == "linked"
    from src.application.wheel.read_model import build_wheel_read_model
    branches = build_wheel_read_model(repo, "lx", int(time.time() * 1000) + 1000, market="hk")["wheel_branches"]
    assert {row["wheel_branch_id"]: row["active_option_committed_shares"] for row in branches} == {
        row["wheel_branch_id"]: (500 if row["wheel_branch_id"] in chosen else 0) for row in branches}
    close_at = int(time.time() * 1000) + 10_000
    for index, contracts in enumerate((1, 2)):
        persist_trade_event_objects_atomically(repo, [TradeEvent(
            event_id=f"call-close-{index}", event_type="close", event_time_ms=close_at + index * 1000,
            contract_key=TradeEvent.from_dict(call_opens[0]).contract_key,
            contracts=contracts, price=0.5, multiplier=500, currency="HKD", source="test",
            target_lot_id=applied["lot_id"], raw_payload={"side": "buy", "target_lot_id": applied["lot_id"]},
        )])
        branches = build_wheel_read_model(repo, "lx", close_at + index * 1000 + 1, market="hk")["wheel_branches"]
        expected = 500 if index == 0 else 0
        assert all(row["active_option_committed_shares"] == expected
                   for row in branches if row["wheel_branch_id"] in chosen)


def test_linked_call_with_late_conflict_does_not_reserve_other_branches(tmp_path, monkeypatch):
    repo, config, _ = _meituan_repo(tmp_path)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    evidence = {"complete": True, "exposures": []}
    def view_now():
        return build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="hk"),
            config=config, account="lx", market="hk", now_ms=int(time.time() * 1000),
            combo_evidence=evidence, combo_mode="confirm")
    first = next(row for row in view_now()["rows"] if row["contract_key"]["option_type"] == "call")
    linked_branch = first["candidate_ids"][0]
    apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=first["execution_key"], candidate_id=linked_branch,
        expected_input_hash=first["input_hash"], request_id="manual:first", actor="fixture:operator",
        combo_evidence=evidence, capacity_observation={}, combo_mode="confirm", manual=True)
    intake = _process_payload(_call_payload(deal_id="meituan-three-fill", contracts=3), repo=repo,
        state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"1001": "lx"}, futu_account_ids=["1001"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, config=config, runtime_root=tmp_path)
    assert intake["status"] == "applied"
    view = view_now()
    linked = next(row for row in view["rows"] if row["execution_key"] == first["execution_key"])
    assert linked["status"] == "conflict" and linked["reason_codes"]
    assert linked["wheel_branch_id"] == linked_branch.removeprefix("wheel:")
    target = next(row for row in view["rows"] if row["contracts"] == 3)
    chosen = tuple(item["wheel_branch_id"] for item in target["candidates"]
                   if item["strategy"] == "wheel" and item["candidate_id"] != linked_branch)[:3]
    args = dict(account="lx", market="hk", config=config, execution_key=target["execution_key"],
                candidate_id="wheel-multi:" + canonical_sha256(sorted(chosen))[:24],
                expected_input_hash=target["input_hash"], request_id="manual:three", actor="fixture:operator",
                combo_evidence=evidence, capacity_observation={}, combo_mode="confirm", manual=True,
                wheel_branch_ids=chosen)
    assert len(apply_trade_attribution(repo, **args, apply_changes=False)["wheel_call_allocations"]) == 3
    assert apply_trade_attribution(repo, **args)["status"] == "linked"
    from src.application.wheel.read_model import build_wheel_read_model
    branches = build_wheel_read_model(repo, "lx", int(time.time() * 1000), market="hk")["wheel_branches"]
    assert {row["wheel_branch_id"]: row["active_option_committed_shares"] for row in branches
            if row["wheel_branch_id"] in chosen or row["wheel_branch_id"] == linked["wheel_branch_id"]} == {
                branch_id: 500 for branch_id in (*chosen, linked["wheel_branch_id"])}


@pytest.mark.parametrize("terminal_event", ["assignment", "expire_close"])
def test_multi_branch_terminal_keeps_stock_coverage_blocked_until_settlement_is_attributed(
        tmp_path, monkeypatch, terminal_event):
    repo, config, _intake = _meituan_repo(tmp_path, call_contracts=3)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    evidence = {"complete": True, "exposures": []}
    rows = read_trade_attribution_snapshot(repo, account="lx", market="hk")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="hk",
        now_ms=_ms("2026-10-01T00:00:00Z"), combo_evidence=evidence)
    call = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    chosen = tuple(sorted(item["wheel_branch_id"] for item in call["candidates"] if item["strategy"] == "wheel")[:3])
    applied = apply_trade_attribution(repo, account="lx", market="hk", config=config,
        execution_key=call["execution_key"], candidate_id="wheel-multi:" + canonical_sha256(sorted(chosen))[:24],
        expected_input_hash=call["input_hash"], request_id="manual:assign", actor="fixture:operator",
        combo_evidence=evidence, capacity_observation={}, combo_mode="confirm", manual=True,
        wheel_branch_ids=chosen)
    call_open = next(row for row in repo.list_trade_events()
                     if row["event_type"] == "open" and row["option_type"] == "call")
    assignment_at = int(time.time() * 1000) + 10_000
    raw_payload = {"side": "buy", "target_lot_id": applied["lot_id"]}
    if terminal_event == "assignment":
        raw_payload["stock_settlement"] = {
            "side": "sell", "shares": 1500, "price": 80, "fees": 0,
            "currency": "HKD", "fee_provenance": {"basis": "actual", "source": "test"}}
    persist_trade_event_objects_atomically(repo, [TradeEvent(
        event_id="call-terminal-all", event_type=terminal_event, event_time_ms=assignment_at,
        contract_key=TradeEvent.from_dict(call_open).contract_key,
        contracts=3, price=0, multiplier=500, currency="HKD", source="test",
        target_lot_id=applied["lot_id"], raw_payload=raw_payload,
    )])
    from src.application.wheel.read_model import build_wheel_read_model
    branches = build_wheel_read_model(repo, "lx", assignment_at + 1, market="hk")["wheel_branches"]
    assert all(row["active_option_committed_shares"] == 500
               and "wheel_call_settlement_allocation_pending" in row["reason_codes"]
               for row in branches if row["wheel_branch_id"] in chosen)
