from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.application.agent_tools import receipts
from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.tools import compact_observation, conservative_json_tokens, build_tool_payload
from src.application.receipt_query import receipt_event
from src.application.trades.inbox import query_trade_receipts


def _setup(monkeypatch, tmp_path, rows, missing=None):
    monkeypatch.setattr(receipts, "load_runtime_config", lambda **kw: (tmp_path / "config.hk.json", {"accounts": ["sy"]}))
    monkeypatch.setattr(receipts, "resolve_runtime_root", lambda **kw: SimpleNamespace(runtime_root=tmp_path))
    monkeypatch.setattr(receipts, "_cursor_key", lambda: "test-only-not-a-secret")
    monkeypatch.setattr(receipts, "_sources", lambda **kw: (rows, missing or []))


def _event(i=0, body="原始业务回执"):
    return receipt_event(source="trade_inbox", event_id=str(i), account="sy", market="HK", kind="trade",
                         occurred=1788934554000 + i, revision=1, body=body,
                         business_result={"status": "failed", "reason": "exception:OperationalError"},
                         deal_id="7258806397173991645", diagnostic_code="exception:OperationalError")


def test_closed_schema_and_config_scope(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [])
    for payload in ({"account": "lx"}, {"market": "US"}, {"start_time": "2026-09-09T12:00:00"}, {"path": "/tmp/no"}, {"limit": 51}):
        with pytest.raises(AgentToolError):
            receipts.RECEIPT_READ_TOOL.call(payload)
    for field in ("config_path", "authenticated_sender_id", "authority_scope"):
        payload, error = build_tool_payload("receipt_read", {field: "forged"})
        assert payload is None and error
    monkeypatch.setattr(receipts, "load_runtime_config", lambda **kw: (tmp_path / "config.hk.json", {}))
    with pytest.raises(AgentToolError, match="configured accounts"):
        receipts.RECEIPT_READ_TOOL.call({})


def test_body_cursor_round_trip_is_bounded_scope_and_revision_bound(monkeypatch, tmp_path):
    body = "真实回执；不要把异常类名当作数据库锁。" * 600
    rows = [_event(body=body)]
    _setup(monkeypatch, tmp_path, rows)
    payload = {"deal_id": "7258806397173991645", "authenticated_sender_id": "sender-a"}
    chunks = []
    first_cursor = None
    while True:
        value, warnings, _ = receipts.RECEIPT_READ_TOOL.call(payload)
        observation = compact_observation("receipt_read", {"ok": True, "data": value, "warnings": warnings}, payload)
        assert observation["value"]["rows"][0]["receipt_body"] == value["rows"][0]["receipt_body"]
        assert conservative_json_tokens(observation) <= 4000
        assert observation["coverage"]["complete_for"] == "point"
        chunks.append(value["rows"][0]["receipt_body"])
        cursor = value["next_cursor"]
        if not cursor:
            assert value["body_complete"] and value["body_range"]["start"] > 0
            break
        first_cursor = first_cursor or cursor
        payload = {"cursor": cursor, "authenticated_sender_id": "sender-a"}
    assert "".join(chunks) == body
    for bad in ({"cursor": first_cursor, "authenticated_sender_id": "sender-b"},
                {"cursor": first_cursor, "authenticated_sender_id": "sender-a", "account": "lx"},
                {"cursor": first_cursor[:-3] + "xxx", "authenticated_sender_id": "sender-a"}):
        with pytest.raises(AgentToolError) as error:
            receipts.RECEIPT_READ_TOOL.call(bad)
        assert error.value.code == "cursor_invalidated"
    rows[0]["receipt_body"] += "更正"
    with pytest.raises(AgentToolError) as error:
        receipts.RECEIPT_READ_TOOL.call({"cursor": first_cursor, "authenticated_sender_id": "sender-a"})
    assert error.value.code == "cursor_invalidated"


def test_event_pages_and_empty_partial_distinction(monkeypatch, tmp_path):
    rows = [_event(i) for i in range(31)]
    _setup(monkeypatch, tmp_path, rows)
    read = []
    payload = {"limit": 3}
    while True:
        result, _, _ = receipts.RECEIPT_READ_TOOL.call(payload)
        read.extend(row["event_ref"]["source_event_id"] for row in result["rows"])
        if not result["next_cursor"]:
            break
        assert result["coverage"]["complete_for"] == "requested_page"
        payload = {"cursor": result["next_cursor"], "limit": 3}
    assert len(set(read)) == len(read) == 31
    _setup(monkeypatch, tmp_path, [])
    empty = receipts.RECEIPT_READ_TOOL.call({})[0]
    assert empty["status"] == "empty" and empty["coverage"]["status"] == "complete"
    _setup(monkeypatch, tmp_path, [], [{"source": "trade_inbox", "reason": "OperationalError"}])
    failed = receipts.RECEIPT_READ_TOOL.call({})[0]
    assert failed["status"] == "unavailable" and failed["coverage"]["total_count"] is None


def test_real_inbox_semantic_receipts_are_read_only_and_not_delivery_inference(tmp_path):
    db = tmp_path / "inbox.sqlite3"
    payload = {"broker_account_ref": {"account_label": "sy"}, "instrument_ref": {"market": "HK", "symbol": "0700.HK"}}
    entries = {"manual_required": {"receipt_id": "first", "business_result": {"reason": "exception:OperationalError"}, "message": "未记录：exception:OperationalError", "status": "failed", "superseded_by": "recorded"},
               "recorded": {"receipt_id": "second", "business_result": {"status": "applied"}, "status": "unknown"}}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE trade_inbox(inbox_id TEXT, deal_id TEXT, payload_json TEXT, result_json TEXT, receipt_json TEXT, received_at_ms INTEGER, updated_at_ms INTEGER)")
        conn.execute("INSERT INTO trade_inbox VALUES (?,?,?,?,?,?,?)", ("inbox-1", "7258806397173991645", json.dumps(payload), "{}", json.dumps({"schema_version": 2, "current_result_key": "recorded", "receipts": entries}), 1788934554000, 1788934555000))
    before = db.read_bytes()
    rows = query_trade_receipts(db, accounts=["sy"], query={"deal_id": "7258806397173991645", "market": "HK"})
    assert len(rows) == 2 and db.read_bytes() == before
    assert rows[0]["body_provenance"] == "frozen"
    assert rows[0]["diagnostic_code"] == "exception:OperationalError"
    assert "锁" not in rows[0]["receipt_body"]
    assert rows[1]["body_provenance"] == "reconstructed" and rows[1]["delivery_state"] == "unknown"
    assert query_trade_receipts(db, accounts=["lx"], query={}) == []


def test_monitor_and_scheduled_retained_facts_do_not_create_state(tmp_path):
    from src.application.positions.maintenance_receipt import query_maintenance_receipts
    from src.application.agent_tools.runtime_status_impl import query_run_receipts
    with pytest.raises(FileNotFoundError):
        query_maintenance_receipts(base=tmp_path, account="sy", query={})
    assert not list(tmp_path.iterdir())
    state = tmp_path / "output_accounts/sy/state"
    state.mkdir(parents=True)
    (state / "auto_close_receipts.json").write_text(json.dumps({"receipts": {"m-1": {"result_summary": {"account": "sy", "as_of_utc": "2026-09-09T06:15:54Z", "applied_closed": 1}, "receipt": {"status": "unknown"}}}}))
    row = query_maintenance_receipts(base=tmp_path, account="sy", query={})[0]
    assert row["body_provenance"] == "reconstructed" and '"applied_closed": 1' in row["receipt_body"]
    run = tmp_path / "output_runs/r1/accounts/sy/state"
    run.mkdir(parents=True)
    (run / "last_run.json").write_text(json.dumps({"account": "sy", "last_run_utc": "2026-09-09T06:15:54Z", "reason": "scan_failed", "sent": False}))
    row = query_run_receipts(base=tmp_path, accounts=["sy"], query={"run_id": "r1"})[0]
    assert row["related_run"] == "r1" and row["body_provenance"] == "reconstructed"
    assert row["delivery_state"] == "unknown" and row["occurred_at"] == "2026-09-09T06:15:54+00:00"


def test_daily_brief_query_survives_missing_run_plan(tmp_path):
    from tests.test_daily_decision_brief_repository_v2 import _persist, _prepare_fixed
    from src.application.daily_decision_brief_repository import query_daily_brief_receipts
    persisted = _persist(tmp_path)
    prepared = _prepare_fixed(tmp_path, persisted, message="当时的原始消息")
    prepared["paths"]["run_plan"].unlink()
    before = prepared["paths"]["delivery"].read_bytes()
    row = query_daily_brief_receipts(base=tmp_path, account="lx", market="US", query={})[0]
    assert row["receipt_body"] == "当时的原始消息" and row["body_provenance"] == "frozen"
    assert row["type"] == "scheduled" and row["delivery_state"] == "prepared"
    assert prepared["paths"]["delivery"].read_bytes() == before


def test_lifecycle_send_start_freezes_original_without_changing_payload_identity(tmp_path):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.api import build_notification_intent, query_lifecycle_receipts
    from src.application.trades.lifecycle_outbox import enqueue_notification_intent, claim_next_notification, mark_notification_send_started, complete_notification_attempt, reconcile_unknown_notification
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    intent = build_notification_intent(case_id="case-1", transition_type="resolution_confirmed", resolution_revision=1,
        transition_key="case-1:resolved", state_fingerprint="state-1", payload={"account": "sy", "market": "HK", "symbol": "0700.HK", "deal_id": "7258806397173991645"})
    enqueue_notification_intent(repo, intent)
    before = query_lifecycle_receipts(repo.db_path, accounts=["sy"], query={})[0]
    assert before["body_provenance"] == "reconstructed"
    due = repo.get_trade_lifecycle_notification(intent["outbox_id"])["next_attempt_at_ms"]
    claim_next_notification(repo, now_ms=due, claim_id="fixture-claim")
    started = mark_notification_send_started(repo, outbox_id=intent["outbox_id"], claim_id="fixture-claim", now_ms=due)
    assert started["payload_hash"] == intent["payload_hash"]
    original = started["payload"]["_frozen_message"]
    complete_notification_attempt(repo, outbox_id=intent["outbox_id"], claim_id="fixture-claim", outcome="accepted", now_ms=due + 1, provider_receipt={"status": "accepted"})
    reconcile_unknown_notification(repo, outbox_id=intent["outbox_id"], action="unknown", broker_ref="fixture", note="fixture provider uncertain", apply_changes=True, now_ms=due + 2)
    row = query_lifecycle_receipts(repo.db_path, accounts=["sy"], query={})[0]
    assert row["receipt_body"] == original and row["body_provenance"] == "frozen"
    assert row["delivery_state"] == "unknown"
    assert query_lifecycle_receipts(repo.db_path, accounts=["lx"], query={}) == []


def test_fixed_failure_original_is_distinct_from_latest_success(tmp_path):
    import hashlib
    from tests.test_daily_decision_brief_repository_v2 import _persist, MARKET_DATE, TARGET_1000
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief_delivery, query_daily_brief_receipts
    _persist(tmp_path)
    artifact = tmp_path / "output_runs/run-fail/accounts/lx/state/pipeline_failure.US.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"reason":"pipeline_failed"}')
    prepare_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE,
        run_id="run-fail", delivery_kind="fixed_failure", source_kind="scan_failure",
        source_digest=hashlib.sha256(artifact.read_bytes()).hexdigest(), source_reference=artifact.relative_to(tmp_path).as_posix(),
        scheduled_target_market=TARGET_1000, rendered_message="当时扫描失败", render_context={"projection": "fixed_failure"})
    artifact.unlink()
    row = query_daily_brief_receipts(base=tmp_path, account="lx", market="US", query={"run_id": "run-fail"})[0]
    assert row["receipt_body"] == "当时扫描失败" and row["diagnostic_code"] == "scan_failure"
    assert row["related_run"] == "run-fail"


def test_large_metadata_is_partial_and_large_candidate_pages_keep_cursor(monkeypatch, tmp_path):
    rows = [_event(i) for i in range(50)]
    for row in rows:
        row["diagnostic_code"] = "源诊断" * 60
    _setup(monkeypatch, tmp_path, rows)
    value, warnings, _ = receipts.RECEIPT_READ_TOOL.call({"limit": 50})
    observation = compact_observation("receipt_read", {"ok": True, "data": value, "warnings": warnings})
    assert observation["value"]["next_cursor"] and "rows" in observation["value"]
    assert conservative_json_tokens(observation) <= 4000
    rows[0]["diagnostic_code"] = "invalid metadata" * 10000
    value = receipts.RECEIPT_READ_TOOL.call({})[0]
    assert value["status"] == "partial"
    assert any(item["reason"] == "receipt_metadata_size_limit" for item in value["missing_sources"])


def test_cursor_binds_original_source_when_redacted_body_is_unchanged(monkeypatch, tmp_path):
    from src.application.research.redaction import redact_text

    body = "x" * 1793 + "\n" + "token=FIRST_FIXTURE_SECRET"
    rows = [_event(body=body)]
    _setup(monkeypatch, tmp_path, rows)
    first = receipts.RECEIPT_READ_TOOL.call({})[0]
    assert first["next_cursor"]
    rows[0]["receipt_body"] = body.replace("FIRST_FIXTURE_SECRET", "SECOND_FIXTURE_SECRET")
    assert redact_text(rows[0]["receipt_body"]) == redact_text(body)
    with pytest.raises(AgentToolError) as error:
        receipts.RECEIPT_READ_TOOL.call({"cursor": first["next_cursor"]})
    assert error.value.code == "cursor_invalidated"
