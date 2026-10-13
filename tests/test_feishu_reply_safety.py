from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.bot import host_store
from src.application.inbound import feishu_ws
from src.infrastructure import feishu_bot
from tests.test_feishu_reply_recovery import recovery, _wait, _request


@pytest.fixture
def clock(monkeypatch):
    value = {"at": datetime.now(timezone.utc)}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value["at"] if tz else value["at"].replace(tzinfo=None)
    monkeypatch.setattr(host_store, "datetime", Clock)
    monkeypatch.setattr(feishu_ws, "datetime", Clock)
    monkeypatch.setattr(host_store, "utc_now_iso", lambda: value["at"].isoformat())
    return value


@pytest.mark.parametrize("age", [3599, 3600, 3601, 25 * 3600])
def test_public_serve_respects_finite_provider_window(recovery, clock, tmp_path, age):
    settings, store, envelope = recovery
    settings = replace(settings, config_key="us", allowed_senders="synthetic-user")
    provider = tmp_path / "finite-provider.sqlite3"
    with sqlite3.connect(provider) as conn:
        conn.execute("CREATE TABLE effects (uuid TEXT, at REAL, body TEXT)")
    calls = []
    def reply(**kwargs):
        kwargs["before_send"]()
        calls.append(_request(kwargs))
        now = clock["at"].timestamp()
        with sqlite3.connect(provider) as conn:
            previous = conn.execute("SELECT MAX(at) FROM effects WHERE uuid=?", (kwargs["uuid"],)).fetchone()[0]
            if previous is None or now - previous >= 3600:
                conn.execute("INSERT INTO effects VALUES (?, ?, ?)",
                             (kwargs["uuid"], now, json.dumps(kwargs["content"], sort_keys=True)))
        return {} if len(calls) == 1 else {"code": 0, "data": {"message_id": "synthetic-receipt"}}
    feishu_ws.serve_feishu_ws(settings, reply_fn=reply,
        start_client_fn=lambda **kw: _wait(lambda: store.list_replies()[0]["status"] == "retryable_failed"))
    first = store.list_replies()[0]
    clock["at"] += timedelta(seconds=age)
    expected = "delivered" if age < 3600 else "delivery_unknown"
    feishu_ws.serve_feishu_ws(settings, reply_fn=reply,
        start_client_fn=lambda **kw: _wait(lambda: store.list_replies()[0]["status"] == expected))
    row = store.list_replies()[0]
    assert row["replay_until"] == first["replay_until"]
    assert len(calls) == (2 if age < 3600 else 1)
    with sqlite3.connect(provider) as conn:
        assert conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
    if age >= 3600:
        assert json.loads(row["payload_json"]) == envelope
        assert store.claim_reply(channel="feishu") is None
        assert store.list_replies()[0] == row
    else:
        assert calls[0] == calls[1]


def test_explicit_rejection_can_retry_after_one_hour(recovery, clock):
    settings, store, _ = recovery
    assert not feishu_ws._retry_pending_feishu_reply(settings=settings,
        reply_fn=lambda **kw: {"code": 230001})["ok"]
    first = store.list_replies()[0]
    assert first["last_error"] == "retryable_delivery_error"
    clock["at"] += timedelta(seconds=3601)
    out = feishu_ws._retry_pending_feishu_reply(settings=settings,
        reply_fn=lambda **kw: {"code": 0, "data": {"message_id": "synthetic-receipt"}})
    assert out["ok"] is True
    assert store.list_replies()[0]["replay_until"] != first["replay_until"]


def test_later_explicit_rejection_cannot_erase_earlier_unknown(recovery, clock):
    settings, store, envelope = recovery
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=lambda **kw: {})
    deadline = store.list_replies()[0]["replay_until"]
    clock["at"] += timedelta(seconds=31)
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=lambda **kw: {"code": 230001})
    row = store.list_replies()[0]
    assert row["last_error"] == "delivery_outcome_unknown" and row["replay_until"] == deadline
    clock["at"] += timedelta(seconds=3570)
    assert store.claim_reply(channel="feishu") is None
    row = store.list_replies()[0]
    assert row["status"] == "delivery_unknown" and json.loads(row["payload_json"]) == envelope


@pytest.mark.parametrize("outcome", ["failed", "delivered"])
def test_late_settlement_cannot_cancel_or_complete_new_claim(recovery, clock, outcome):
    _, store, envelope = recovery
    first = store.claim_reply(channel="feishu")
    clock["at"] += timedelta(seconds=301)
    second = host_store.BotHostStore(store.path).claim_reply(channel="feishu")
    assert second["attempt_count"] == 2
    before = store.list_replies()[0]
    if outcome == "failed":
        assert not store.mark_reply_failed(first["delivery_key"], retryable=True, error="old failure",
                                           attempt_count=first["attempt_count"], delivery_unknown=True)
    else:
        assert not store.mark_reply_delivered(first["delivery_key"], attempt_count=first["attempt_count"])
    assert store.list_replies()[0] == before
    assert store.claim_reply(channel="feishu") is None
    assert json.loads(store.list_replies()[0]["payload_json"]) == envelope
    assert store.mark_reply_delivered(second["delivery_key"], attempt_count=second["attempt_count"])
    assert store.list_replies()[0]["status"] == "delivered"


def test_unfenced_legacy_feishu_settlement_is_rejected(recovery):
    _, store, _ = recovery
    claim = store.claim_reply(channel="feishu")
    before = store.list_replies()
    assert not store.mark_reply_failed(claim["delivery_key"], retryable=True, error="legacy")
    assert not store.mark_reply_delivered(claim["delivery_key"])
    assert store.list_replies() == before


@pytest.mark.parametrize("status", ["retryable_failed", "delivering"])
def test_legacy_attempt_without_proven_window_is_retained(recovery, clock, status):
    _, store, envelope = recovery
    with sqlite3.connect(store.path) as conn:
        conn.execute("ALTER TABLE bot_reply_outbox DROP COLUMN replay_until")
        conn.execute("UPDATE bot_reply_outbox SET status=?, attempt_count=1, updated_at=?",
                     (status, (clock["at"] - timedelta(seconds=301)).isoformat()))
    assert store.claim_reply(channel="feishu") is None
    row = store.list_replies()[0]
    assert row["status"] == "delivery_unknown" and row["replay_until"] is None
    assert json.loads(row["payload_json"]) == envelope


def test_guard_rechecks_after_token_wait_and_prevents_post(recovery, clock, monkeypatch):
    settings, store, envelope = recovery
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=lambda **kw: {})
    clock["at"] += timedelta(seconds=3599)
    posts = []
    def token_wait(app_id, app_secret, fn):
        clock["at"] += timedelta(seconds=2)
        return fn("synthetic-token")
    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", token_wait)
    def transport(**kwargs):
        return feishu_bot.reply_message(**kwargs, http_json_fn=lambda *a, **kw: posts.append((a, kw)))
    out = feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=transport)
    assert not out["ok"] and posts == []
    row = store.list_replies()[0]
    assert row["status"] == "delivery_unknown" and json.loads(row["payload_json"]) == envelope


def test_wall_clock_rollback_does_not_extend_send_guard(recovery, clock, monkeypatch):
    _, store, _ = recovery
    record = store.claim_reply(channel="feishu")
    now = time.monotonic()
    monkeypatch.setattr(feishu_ws.time, "monotonic", lambda: now)
    guard = feishu_ws._reply_send_guard(record)
    clock["at"] -= timedelta(hours=2)
    monkeypatch.setattr(feishu_ws.time, "monotonic", lambda: now + 3601)
    with pytest.raises(feishu_ws.FeishuPermanentError):
        guard()


def test_unknown_fallback_retries_only_selected_identity(recovery, clock):
    settings, store, _ = recovery
    calls = []
    def reply(**kwargs):
        calls.append(_request(kwargs))
        if len(calls) == 1:
            raise feishu_ws.FeishuPermanentError("synthetic card rejection", code=230001)
        return {} if len(calls) == 2 else {"code": 0, "data": {"message_id": "synthetic-text"}}
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)
    assert json.loads(store.list_replies()[0]["payload_json"])["fallback_selected"] is True
    clock["at"] += timedelta(seconds=31)
    assert feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)["ok"]
    assert calls[1] == calls[2] and calls[1]["uuid"] == "feishu:synthetic:fallback"


def test_prior_unknown_card_does_not_start_new_fallback_identity(recovery, clock):
    settings, store, _ = recovery
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=lambda **kw: {})
    clock["at"] += timedelta(seconds=31)
    calls = []
    def reply(**kwargs):
        calls.append(_request(kwargs))
        raise feishu_ws.FeishuPermanentError("synthetic card rejection", code=230001)
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)
    assert len(calls) == 1 and calls[0]["uuid"] == "feishu:synthetic"
    assert store.list_replies()[0]["status"] == "delivery_unknown"


def test_reply_http_attempts_are_owned_by_outbox(monkeypatch):
    calls = []
    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", lambda a, b, fn: fn("synthetic-token"))
    feishu_bot.reply_message(app_id="synthetic", app_secret="synthetic", message_id="synthetic",
                            text="合成", http_json_fn=lambda *a, **kw: calls.append(kw) or {"code": 0})
    assert calls[0]["retry_max_attempts"] == 1


_ACTOR = '''
import json, sys, time
from pathlib import Path
from src.application.bot.host_store import BotHostStore
from src.application.inbound import feishu_ws as f
folder, name, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def wait(file):
    end = time.monotonic() + 10
    while not (folder / file).exists():
        if time.monotonic() > end: raise TimeoutError("synthetic coordinator")
        time.sleep(.01)
store = BotHostStore(folder / "host.sqlite3")
(folder / (name + ".ready")).write_text("ready")
wait("go")
if mode == "claim":
    out = store.claim_reply(channel="feishu")
else:
    def reply(**kw):
        (folder / (name + ".entered")).write_text("entered")
        wait(name + ".release")
        kw["before_send"]()
        if mode == "failed": raise f.FeishuTransientError("synthetic late failure")
        return {"code": 0, "data": {"message_id": "synthetic-" + name}}
    out = f._retry_pending_feishu_reply(settings=f.FeishuWsSettings(
        audit_db=str(store.path), app_id="synthetic", app_secret="synthetic"), reply_fn=reply)
(folder / (name + ".json")).write_text(json.dumps(out))
'''


@pytest.mark.parametrize("late_outcome", ["failed", "confirmed"])
def test_two_processes_reject_old_settlement_and_third_claim(recovery, tmp_path, late_outcome):
    _, store, _ = recovery
    procs = []
    def spawn(name, mode):
        proc = subprocess.Popen([sys.executable, "-c", _ACTOR, str(tmp_path), name, mode],
                                cwd=Path(feishu_ws.__file__).resolve().parents[3],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(proc)
        return proc
    def finish(proc):
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
    try:
        first = spawn("A", late_outcome)
        _wait(lambda: (tmp_path / "A.ready").exists())
        (tmp_path / "go").write_text("go")
        _wait(lambda: (tmp_path / "A.entered").exists())
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE bot_reply_outbox SET updated_at=?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(),))
        second = spawn("B", "confirmed")
        _wait(lambda: (tmp_path / "B.entered").exists())
        before = store.list_replies()[0]
        assert before["attempt_count"] == 2 and before["status"] == "delivering"
        (tmp_path / "A.release").write_text("release")
        finish(first)
        assert store.list_replies()[0] == before
        assert store.claim_reply(channel="feishu") is None
        (tmp_path / "B.release").write_text("release")
        finish(second)
        assert store.list_replies()[0]["status"] == "delivered"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=5)


def test_four_processes_have_one_fresh_claim_winner(recovery, tmp_path):
    _, store, _ = recovery
    procs = [subprocess.Popen([sys.executable, "-c", _ACTOR, str(tmp_path), str(n), "claim"],
                             cwd=Path(feishu_ws.__file__).resolve().parents[3],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(4)]
    try:
        _wait(lambda: all((tmp_path / (str(n) + ".ready")).exists() for n in range(4)))
        (tmp_path / "go").write_text("go")
        for proc in procs:
            out, err = proc.communicate(timeout=12)
            assert proc.returncode == 0, (out, err)
        rows = [json.loads((tmp_path / (str(n) + ".json")).read_text()) for n in range(4)]
        assert sum(row is not None for row in rows) == 1
        assert store.list_replies()[0]["attempt_count"] == 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=5)
