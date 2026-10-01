from __future__ import annotations

from datetime import datetime
import time

import pytest
from unittest.mock import patch

from domain.domain.ledger import ContractKey, TradeEvent
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


def test_meituan_five_covered_branches_enter_pending_via_trade_ingress(tmp_path, monkeypatch):
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
    payload = {"schema_version": "trade_execution.v1",
        "broker_account_ref": {**ref, "broker_account_id": "futu:REAL:1001", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "HK", "symbol": "3690.HK", "currency": "HKD",
            "option_type": "call", "strike": "80", "expiration_ymd": "2026-11-27", "multiplier": "500"},
        "external_id_namespace": "futu.deal", "external_execution_id": "meituan-call-fill",
        "external_order_namespace": "futu.order", "external_order_id": "meituan-order",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": "1.5", "currency": "HKD",
        "occurred_at_utc": "2026-09-27T02:45:31Z", "status": "OK"}
    result = _process_payload(payload, repo=repo, state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl", account_mapping={"1001": "lx"}, futu_account_ids=["1001"],
        apply_changes=True, host="127.0.0.1", port=11111, allow_external_lookup=False,
        config=config, runtime_root=tmp_path)
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
