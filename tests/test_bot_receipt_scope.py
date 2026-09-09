"""Real-source regressions for trusted receipt owners, markets, and batch bodies."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import receipts
from src.application.ledger.api import build_notification_intent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.lifecycle_outbox import (
    QUIET_WINDOW_MS, build_notification_batch_route, dispatch_notification_batch_once,
    enqueue_notification_intent,
)


@pytest.fixture
def source_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(receipts, "_cursor_key", lambda: "fixture-receipt-key")
    config = tmp_path / "config.hk.json"
    config.write_text(json.dumps({"accounts": ["lx", "sy"], "_generated": {"market": "hk", "source_format": "yaml"}}))
    ledger = tmp_path / "output_shared/state/option_positions.sqlite3"
    ledger.parent.mkdir(parents=True)
    repo = SQLiteOptionPositionsRepository(ledger)
    inbox = ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3")
    with sqlite3.connect(inbox) as conn:
        conn.execute("CREATE TABLE trade_inbox(inbox_id TEXT, deal_id TEXT, payload_json TEXT, result_json TEXT, receipt_json TEXT, received_at_ms INTEGER, updated_at_ms INTEGER)")
    return config, repo, inbox


def _read(config, **query):
    return receipts.RECEIPT_READ_TOOL.call({"config_path": str(config), "config_key": "hk", "type": "trade", **query})[0]


def _inbox_row(path, identity, market):
    payload = {"broker_account_ref": {"account_label": "sy"}, "instrument_ref": {"market": market, "symbol": "fixture"}}
    envelope = {"receipt_id": identity, "message": identity + "_PRIVATE_BODY", "business_result": {"status": "failed"}}
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO trade_inbox VALUES (?,?,?,?,?,?,?)", (identity, identity, json.dumps(payload), "{}", json.dumps(envelope), 1788934554000, 1788934555000))


def test_market_omission_and_cursor_cannot_widen_trusted_scope(source_runtime):
    config, repo, inbox = source_runtime
    for identity, market in (("hk-one", "HK"), ("hk-two", "HK"), ("us-one", "US")):
        _inbox_row(inbox, identity, market)
    before = inbox.read_bytes()
    first = _read(config, limit=1)
    assert first["coverage"]["scope"]["query"]["market"] == "HK"
    assert first["next_cursor"]
    second = _read(config, cursor=first["next_cursor"])
    assert {row["deal_id"] for page in (first, second) for row in page["rows"]} == {"hk-one", "hk-two"}
    assert _read(config, deal_id="us-one")["status"] == "empty"
    assert _read(config, deal_id="hk-one", market="HK")["rows"][0]["receipt_body"] == "hk-one_PRIVATE_BODY"
    with pytest.raises(AgentToolError, match="market outside"):
        _read(config, market="US")
    assert inbox.read_bytes() == before
    _inbox_row(inbox, "missing-market", None)
    value = _read(config, deal_id="missing-market")
    assert not value["rows"] and value["status"] == "unavailable"
    assert {item["reason"] for item in value["missing_sources"]} == {"receipt_market_unlinkable"}


def _batch(repo, *, markets=("HK", "HK")):
    rows = []
    for index in range(12):
        intent = build_notification_intent(case_id=f"case-{index}", transition_type="needs_review", resolution_revision=1,
            transition_key=f"fixture-{index}", state_fingerprint=f"state-{index}", payload={
                "account": "lx" if index % 2 else "sy", "market": markets[index % 2],
                "symbol": "SYM" + str(index) + "X" * 150, "case_id": f"case-{index}", "transition_type": "needs_review"})
        rows.append(enqueue_notification_intent(repo, intent)["outbox"])
    sent = []
    def fixture_send(payload):
        sent.append(payload)
        return {"status": "confirmed", "delivery_confirmed": True, "message_id": "fixture-only"}
    outcome = dispatch_notification_batch_once(repo, route=build_notification_batch_route(provider="fixture", channel="fixture", target="fixture"),
        send_fn=fixture_send, now_ms=max(row["created_at_ms"] for row in rows) + QUIET_WINDOW_MS, allowed_accounts={"lx", "sy"})
    assert outcome["status"] == "confirmed" and len(sent) == 1
    batch = outcome["batch"]
    body = repo.get_trade_lifecycle_notification_batch(batch["batch_id"])["provider_receipt"]["rendered_message"]
    assert len(body) > 1800
    return batch["batch_id"], body


@pytest.mark.parametrize("requested_account", [None, "lx"])
def test_mixed_account_batch_has_one_retrievable_paginated_body(source_runtime, requested_account):
    config, repo, inbox = source_runtime
    batch_id, original = _batch(repo)
    before = repo.db_path.read_bytes()
    query = {"event_id": "trade_lifecycle_batch:" + batch_id}
    if requested_account:
        query["account"] = requested_account
    chunks = []
    while True:
        value = _read(config, **query)
        assert value["status"] == "ok" and len(value["rows"]) == 1
        row = value["rows"][0]
        assert row["accounts"] == ["lx", "sy"] and row["market"] == "HK"
        assert value["coverage"]["scope"]["owner_accounts"] == ["lx", "sy"]
        assert row["body_provenance"] == "frozen"
        chunks.append(row["receipt_body"])
        if not value["next_cursor"]:
            break
        query = {"cursor": value["next_cursor"]}
    assert len(chunks) > 1 and "".join(chunks) == original
    assert repo.db_path.read_bytes() == before
    cfg = json.loads(config.read_text()); cfg["accounts"] = ["lx"]; config.write_text(json.dumps(cfg))
    denied = _read(config, event_id="trade_lifecycle_batch:" + batch_id, account="lx")
    assert denied["status"] == "unavailable" and not denied["rows"]
    assert "lifecycle_batch_account_scope_incomplete" in {item["reason"] for item in denied["missing_sources"]}


@pytest.mark.parametrize(("markets", "reason"), [(("HK", None), "receipt_market_unlinkable"), (("HK", "US"), "lifecycle_batch_market_scope_incomplete"), (("US", "US"), None)])
def test_batch_market_comes_only_from_all_frozen_members(source_runtime, markets, reason):
    config, repo, inbox = source_runtime
    batch_id, _ = _batch(repo, markets=markets)
    value = _read(config, event_id="trade_lifecycle_batch:" + batch_id)
    assert not value["rows"]
    if reason:
        assert value["status"] == "unavailable"
        assert reason in {item["reason"] for item in value["missing_sources"]}
    else:
        assert value["status"] == "empty"
