"""24h content, 7d metadata, input identity and restart-safe cleanup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from om_group_query_bot.feishu import VerifiedGroupEvent
from om_group_query_bot.store import (
    CONVERSATION_DB_NAME,
    ConversationStore,
    DeliveryStore,
    InputIdentityConflict,
    derived_expiry,
)

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


def _event(message_id="om_1"):
    return VerifiedGroupEvent(
        app_id="cli_group", tenant_key="tenant", sender_open_id="ou_alice",
        sender_tenant_key="tenant", chat_id="oc_group", thread_id=None,
        message_id=message_id, event_id="ev_" + message_id)


def _turn(store, scope, turn_id, index, *, now=NOW, source_expiry=None):
    expiry = source_expiry or now + timedelta(hours=24)
    store.append_message(scope_sha256=scope, turn_id=turn_id, seq=0,
                         role="user", content=f"问题 {index:02d}", now=now,
                         source_id=f"om_{index}", source_expiry=expiry)
    store.append_message(scope_sha256=scope, turn_id=turn_id, seq=1,
                         role="assistant", content=f"回答 {index:02d}",
                         now=now, source_id=f"om_{index}",
                         source_expiry=expiry)


def _estimate(turn):
    return sum(len(item["content"]) for item in turn)


def test_derived_expiry_never_exceeds_the_first_24h_or_a_source(tmp_path):
    assert derived_expiry(NOW) == NOW + timedelta(hours=24)
    assert derived_expiry(NOW, None) == NOW + timedelta(hours=24)
    assert derived_expiry(NOW, NOW + timedelta(hours=30)) == NOW + timedelta(hours=24)
    assert derived_expiry(NOW, NOW + timedelta(hours=2)) == NOW + timedelta(hours=2)


def test_content_expires_at_24h_or_at_its_source(tmp_path):
    store = ConversationStore(tmp_path / "data")
    scope = "scope-a"
    store.append_message(scope_sha256=scope, turn_id="turn-1", seq=0,
                         role="user", content="问题", now=NOW, source_id="om_1",
                         source_expiry=NOW + timedelta(hours=24))
    store.append_message(scope_sha256=scope, turn_id="turn-1", seq=1,
                         role="assistant", content="回答", now=NOW,
                         source_id="om_1",
                         source_expiry=NOW + timedelta(minutes=30))

    assert len(store.context_turns(scope, NOW + timedelta(minutes=29),
                                   max_turns=10, max_tokens=1000,
                                   estimate=_estimate)) == 1
    # Exactly at the source expiry the row is already excluded, so the
    # incomplete turn is dropped whole.
    assert store.context_turns(scope, NOW + timedelta(minutes=30), max_turns=10,
                               max_tokens=1000, estimate=_estimate) == []
    assert store.context_turns(scope, NOW + timedelta(minutes=31), max_turns=10,
                               max_tokens=1000, estimate=_estimate) == []
    # The user body also expires exactly at its own 24h bound.
    assert store.context_turns(scope, NOW + timedelta(hours=24), max_turns=10,
                               max_tokens=1000, estimate=_estimate) == []
    assert store.context_turns(scope, NOW + timedelta(hours=25), max_turns=10,
                               max_tokens=1000, estimate=_estimate) == []
    assert store.counts(NOW + timedelta(hours=25))["messages"] == 0


def test_context_selection_is_bounded_by_turns_then_tokens(tmp_path):
    store = ConversationStore(tmp_path / "data")
    scope = "scope-a"
    for index in range(1, 13):
        _turn(store, scope, f"turn{index:02d}", index)

    by_turns = store.context_turns(scope, NOW, max_turns=10, max_tokens=1000,
                                   estimate=_estimate)
    assert [turn["messages"][0]["content"] for turn in by_turns] == [
        f"问题 {index:02d}" for index in range(3, 13)]

    # Each turn is 8 characters under this estimator; two fit in 20 tokens.
    by_tokens = store.context_turns(scope, NOW, max_turns=10, max_tokens=20,
                                    estimate=_estimate)
    assert [turn["messages"][0]["content"] for turn in by_tokens] == [
        "问题 11", "问题 12"]
    # A newest turn that alone exceeds the budget is not truncated into context.
    assert store.context_turns(scope, NOW, max_turns=10, max_tokens=5,
                               estimate=_estimate) == []


def test_scopes_are_isolated(tmp_path):
    store = ConversationStore(tmp_path / "data")
    _turn(store, "scope-a", "turn-a", 1)
    _turn(store, "scope-b", "turn-b", 2)

    turns = store.context_turns("scope-a", NOW, max_turns=10, max_tokens=1000,
                                estimate=_estimate)
    assert len(turns) == 1
    assert turns[0]["messages"][0]["content"] == "问题 01"


def test_pending_input_keeps_only_the_latest_per_operation(tmp_path):
    store = ConversationStore(tmp_path / "data")
    scope = "scope-a"
    store.save_pending_input(scope_sha256=scope, operation="evaluate_close",
                             parameters={"step": 1}, now=NOW,
                             source_id="om_1",
                             source_expiry=NOW + timedelta(hours=24))
    store.save_pending_input(scope_sha256=scope, operation="evaluate_close",
                             parameters={"step": 2}, now=NOW,
                             source_id="om_2",
                             source_expiry=NOW + timedelta(hours=24))
    store.save_pending_input(scope_sha256=scope, operation="evaluate_roll_down",
                             parameters={"step": 3}, now=NOW,
                             source_id="om_3",
                             source_expiry=NOW + timedelta(hours=24))

    pending = store.pending_inputs(scope, NOW)
    assert {item["operation"] for item in pending} == {
        "evaluate_close", "evaluate_roll_down"}
    close = next(item for item in pending
                 if item["operation"] == "evaluate_close")
    assert close["parameters"] == {"step": 2}
    # Pending rows obey the same 24h content lifetime.
    assert store.pending_inputs(scope, NOW + timedelta(hours=25)) == []


def test_confirmed_input_reuse_keeps_the_original_expiry(tmp_path):
    store = ConversationStore(tmp_path / "data")
    scope = "scope-a"
    first = store.save_confirmed_input(
        scope_sha256=scope, input_id="input-1", input_sha256="digest-a",
        operation="evaluate_close", parameters={"strike": 180.0}, now=NOW,
        source_id="om_1", source_expiry=NOW + timedelta(hours=24))

    assert store.resolve_confirmed_input(
        scope_sha256=scope, operation="evaluate_close",
        input_sha256="digest-a", now=NOW) == "input-1"
    assert store.resolve_confirmed_input(
        scope_sha256=scope, operation="evaluate_close",
        input_sha256="digest-b", now=NOW) is None

    # Re-saving the same identity after a later message must not renew it.
    later = NOW + timedelta(hours=2)
    second = store.save_confirmed_input(
        scope_sha256=scope, input_id="input-1", input_sha256="digest-a",
        operation="evaluate_close", parameters={"strike": 180.0}, now=later,
        source_id="om_2", source_expiry=later + timedelta(hours=24))
    assert second == first
    assert store.input_expiry(scope, "input-1", later) == datetime.fromisoformat(first)
    assert store.resolve_confirmed_input(
        scope_sha256=scope, operation="evaluate_close",
        input_sha256="digest-a", now=NOW + timedelta(hours=25)) is None

    with pytest.raises(InputIdentityConflict):
        store.save_confirmed_input(
            scope_sha256=scope, input_id="input-1", input_sha256="digest-b",
            operation="evaluate_close", parameters={"strike": 181.0}, now=later,
            source_id="om_3", source_expiry=later + timedelta(hours=24))


def test_outbox_body_expires_without_changing_delivery_state(tmp_path):
    data_dir = tmp_path / "data"
    conversations = ConversationStore(data_dir)
    deliveries = DeliveryStore(data_dir, db_name=CONVERSATION_DB_NAME)
    event = _event()
    row, claimed = deliveries.claim(event, "gq", NOW)
    assert claimed is True

    conversations.save_outbox(
        delivery_key=row["message_key"], scope_sha256="scope-a", body="回复正文",
        now=NOW, source_id=event.message_id,
        source_expiry=NOW + timedelta(hours=1))
    assert conversations.outbox_body(row["message_key"], NOW + timedelta(minutes=30)) \
        == "回复正文"
    assert conversations.outbox_body(row["message_key"], NOW + timedelta(hours=2)) \
        is None
    assert deliveries.counts(NOW) == {"claimed": 1}

    conversations.cleanup(NOW + timedelta(hours=2))
    assert conversations.outbox_body(row["message_key"], NOW) is None
    assert deliveries.counts(NOW) == {"claimed": 1}


def test_outbox_insert_ignores_replay_and_keeps_the_original_expiry(tmp_path):
    store = ConversationStore(tmp_path / "data")
    first = store.save_outbox(delivery_key="key", scope_sha256="scope-a",
                              body="第一次", now=NOW, source_id="om_1",
                              source_expiry=NOW + timedelta(hours=2))
    second = store.save_outbox(delivery_key="key", scope_sha256="scope-a",
                               body="第二次", now=NOW + timedelta(hours=1),
                               source_id="om_1",
                               source_expiry=NOW + timedelta(hours=30))
    assert second == first
    assert store.outbox_body("key", NOW + timedelta(minutes=10)) == "第一次"


def test_cleanup_removes_24h_bodies_before_7d_metadata_and_is_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    conversations = ConversationStore(data_dir)
    deliveries = DeliveryStore(data_dir, db_name=CONVERSATION_DB_NAME)
    conversations.record_audit(scope_sha256="scope-a", turn_id=None,
                               status="ok", reason_codes=("ok",), now=NOW)
    deliveries.claim(_event(), "gq", NOW)

    conversations.cleanup(NOW + timedelta(hours=25))
    assert conversations.counts(NOW + timedelta(hours=25)) == {
        "messages": 0, "scenario_inputs": 0, "outbox_payloads": 0, "audit": 0}
    assert deliveries.counts(NOW + timedelta(hours=25)) == {"claimed": 1}

    repeat = conversations.cleanup(NOW + timedelta(hours=25))
    assert repeat == {"messages": 0, "scenario_inputs": 0,
                      "outbox_payloads": 0, "audit": 0}

    deliveries.cleanup(NOW + timedelta(days=8))
    assert deliveries.counts(NOW + timedelta(days=8)) == {}


def test_audit_rows_hold_metadata_only(tmp_path):
    store = ConversationStore(tmp_path / "data")
    store.record_audit(scope_sha256="scope-a", turn_id="turn-1",
                       status="limited", reason_codes=("deadline_exceeded",),
                       now=NOW)

    db = sqlite3.connect(store.path)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(audit)")]
        row = db.execute("SELECT * FROM audit").fetchone()
    finally:
        db.close()

    assert columns == ["audit_id", "scope_sha256", "turn_id", "status",
                       "reason_codes", "created_at_utc", "expires_at_utc"]
    assert row[3] == "limited"
    assert row[4] == '["deadline_exceeded"]'
    assert not any("问题" in str(value) for value in row)
