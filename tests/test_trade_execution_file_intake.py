from copy import deepcopy
from functools import partial
import json
import sqlite3

import pytest

from src.application.ledger.api import record_normalized_trade_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.auto_intake import _process_payload
from src.application.trades.file_intake import run_execution_file
from src.application.trades.inbox import (
    read_trade_payload,
    read_trade_source_evidence,
    trade_inbox_summary,
)
from src.application.trades.normalizer import normalize_trade_deal


BINDING = {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
           "broker_account_id": "futu:REAL:123", "account_label": "lx"}


def _execution(deal_id="fill-1", *, effect="open"):
    return {
        "schema_version": "trade_execution.v1", "broker_account_ref": dict(BINDING),
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA",
                           "currency": "USD", "option_type": "put", "strike": "100",
                           "expiration_ymd": "2026-09-18", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{deal_id}",
        "side": "sell" if effect == "open" else "buy", "position_effect": effect,
        "quantity": "1", "price": "2.50", "currency": "USD",
        "occurred_at_utc": "2026-09-07T02:30:00Z" if effect == "open" else "2026-09-07T03:30:00Z",
    }


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _core(tmp_path, repo, **kwargs):
    return partial(_process_payload, repo=repo, state_path=tmp_path / "state.json",
                   audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx"},
                   futu_account_ids=["123"], host="127.0.0.1", port=11111, **kwargs)


def test_file_defaults_to_read_only_and_never_fetches_provider(tmp_path, monkeypatch):
    def provider(**_kwargs):
        pytest.fail("file intake must stay offline")
    monkeypatch.setattr("src.application.trades.normalizer.resolve_multiplier_with_source_and_diagnostics", provider)
    monkeypatch.setattr("src.application.trades.auto_intake.enrich_trade_push_payload_with_account_id", provider)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    result = run_execution_file(_write(tmp_path / "trades.jsonl", [_execution()]),
                                process_payload_fn=_core(tmp_path, repo), configured_accounts=[BINDING])
    assert result["dry_run"] is True
    assert result["row_count"] == 1
    assert repo.list_trade_events() == []
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("content,kwargs,message", [
    (b"{}\n{}\n", {"max_rows": 1}, "exceeds 1 rows"),
    (b"{}\n", {"max_bytes": 2}, "exceeds 2 bytes"),
    (b"{}\nnot-json\n", {}, "line 2 is not valid JSON"),
    (b"\xff\n", {}, "must be UTF-8"),
    (b'{"price":"1","price":"2"}\n', {}, "not valid JSON"),
])
def test_file_rejects_structural_errors_before_any_partial_processing(tmp_path, content, kwargs, message):
    path = tmp_path / "trades.jsonl"
    path.write_bytes(content)
    calls = []
    with pytest.raises(ValueError, match=message):
        run_execution_file(path, configured_accounts=[BINDING],
                           process_payload_fn=lambda *args, **kw: calls.append((args, kw)), **kwargs)
    assert calls == []


def test_api_history_and_file_share_identity_and_preserve_every_source_evidence(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    core = _core(tmp_path, repo)
    api = {"acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
           "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
           "deal_id": "fill-1", "order_id": "order-fill-1", "code": "US.NVDA260918P00100000",
           "qty": "1", "price": "2.50", "multiplier": "100", "trd_side": "SELL_SHORT",
           "create_time": "2026-09-07 10:30:00"}
    first = core(api, source="push", apply_changes=True, allow_external_lookup=False)
    assert first["status"] == "applied"
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    history = core({**api, "source_evidence": "history"}, source="backfill", apply_changes=True, allow_external_lookup=False)
    assert history["reason"] == "duplicate"
    path = _write(tmp_path / "trades.jsonl", [_execution()])
    result = run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
    replay = run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
    assert result["results"][0]["inbox_id"] == replay["results"][0]["inbox_id"] == first["inbox_id"]
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    with sqlite3.connect(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3") as conn:
        sources = {row[0] for row in conn.execute("SELECT source FROM trade_inbox_evidence")}
    assert sources == {"push", "backfill", "file"}
    execution = before_events[0]["raw_payload"]["execution_input"]
    assert len(execution["evidence_refs"]) == 1
    evidence = read_trade_source_evidence(
        tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3",
        evidence_ref=execution["evidence_refs"][0],
        read_only=True,
    )
    assert {item["source"] for item in evidence} == {"push", "backfill", "file"}
    assert {item["adapter_version"] for item in evidence} == {
        "om.trade-intake.push.v1",
        "om.trade-intake.history.v1",
        "om.trade-intake.execution-jsonl.v1",
    }
    assert len({item["evidence_id"] for item in evidence}) == 3
    assert all(
        item["schema_version"] == "source_evidence.v1"
        and item["data_type"] == "execution"
        and item["source_record_identity"].startswith("execution:v1:")
        and item["payload_version"]
        and item["content_digest"]
        and item["received_at_ms"] > 0
        and item["raw_payload"]
        for item in evidence
    )


@pytest.mark.parametrize("change", ["order_summary", "missing_id", "bad_price", "foreign_account", "wrong_label", "nested"])
def test_invalid_file_execution_is_durable_review_evidence_without_economic_write(tmp_path, change):
    row = _execution()
    if change == "order_summary":
        row["data_type"] = "order_summary"
    elif change == "missing_id":
        row.pop("external_execution_id")
    elif change == "bad_price":
        row["price"] = "1e999999999"
    elif change == "foreign_account":
        row["broker_account_ref"]["external_account_id"] = "999"
    elif change == "wrong_label":
        row["broker_account_ref"]["account_label"] = "sy"
    else:
        row["execution_input"] = _execution("hidden-fill")
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    path = _write(tmp_path / "trades.jsonl", [row])
    result = run_execution_file(path, process_payload_fn=_core(tmp_path, repo), configured_accounts=[BINDING], dry_run=False)
    item = result["results"][0]
    assert item["status"] == "unresolved"
    assert repo.list_trade_events() == []
    stored = read_trade_payload(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3", inbox_id=item["inbox_id"])
    assert stored["status"] == "identity_needs_review"
    assert json.loads(stored["payload"]["_trade_intake_file_evidence"]["raw_line"]) == row


def test_file_conflict_keeps_original_economics_and_stays_conflicted_after_good_replay(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    core = _core(tmp_path, repo)
    path = _write(tmp_path / "trades.jsonl", [_execution()])
    run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
    before = repo.list_trade_events()
    changed = {**_execution(), "price": "3"}
    _write(path, [changed])
    conflict = run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
    assert conflict["results"][0]["reason"] == "inbox_conflict"
    _write(path, [_execution()])
    replay = run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
    assert replay["results"][0]["reason"] == "inbox_conflict"
    assert repo.list_trade_events() == before
    assert trade_inbox_summary(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3")["conflict_count"] == 1


@pytest.mark.parametrize("existing_live_close", [False, True])
def test_historical_file_close_never_creates_or_changes_live_notification_intent(tmp_path, existing_live_close):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    record_normalized_trade_event(repo, normalize_trade_deal(_execution("opening")))
    callbacks = []
    core = _core(tmp_path, repo, on_result_fn=lambda payload: callbacks.append(payload))
    close = _execution("closing", effect="close")
    if existing_live_close:
        live = core(deepcopy(close), source="push", apply_changes=True, allow_external_lookup=False)
        assert live["status"] == "applied"
    before = repo.list_trade_lifecycle_notifications()
    assert len(before) == int(existing_live_close)
    callbacks.clear()
    path = _write(tmp_path / "close.jsonl", [close])
    for _ in range(2):
        result = run_execution_file(path, process_payload_fn=core, configured_accounts=[BINDING], dry_run=False)
        assert result["results"][0]["status"] in {"applied", "skipped"}
    assert repo.list_trade_lifecycle_notifications() == before
    assert callbacks == []
    assert len(repo.list_trade_events()) == 2
