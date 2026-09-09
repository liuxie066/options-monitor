from functools import partial
import json
import threading
from types import SimpleNamespace

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake, receipt
from src.infrastructure.futu_trade_push import OpenDTradePushListener
from tests.test_trade_receipt_recovery import _payload, _processor, _stored


def _source(tmp_path):
    return {"id": "lx", "account": "lx", "host": "127.0.0.1", "port": 11111,
            "state_path": tmp_path / "state.json", "audit_path": tmp_path / "audit.jsonl",
            "status_path": tmp_path / "status.json",
            "inbox_path": tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3",
            "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"],
            "receipt": {"enabled": True}, "backfill": {"enabled": False},
            "settlement_observation": {"enabled": False}}


def _callback(tmp_path, repo, monkeypatch, sender, *, receipt_config=None):
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda send_result: send_result))
    return auto_intake._build_receipt_callback(
        base=tmp_path, repo=repo, receipt_config=receipt_config or {"enabled": True},
        cfg={"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test"}})


def _run(tmp_path, monkeypatch, repo, source, callback, stop):
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", lambda **_: SimpleNamespace(close=lambda: None))
    return auto_intake._run_listener_source_loop(
        source=source, repo=repo, cfg={}, cfg_path=tmp_path / "config.json", runtime_root=tmp_path,
        runtime_root_source="test", intake_cfg={"mode": "apply", "enabled": True},
        apply_changes=True, receipt_callback=callback, process_lock=threading.RLock(), stop_event=stop)


def test_source_recovers_again_while_sdk_constructor_remains_unfinished(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _payload("778")
    _processor(tmp_path, repo, monkeypatch, lambda **_: pytest.fail("no route cannot send"), routed=False)(payload)
    before = repo.list_trade_events(), repo.list_position_lots()
    source = _source(tmp_path)
    clock = [0.0]
    wall = auto_intake.time.time()
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auto_intake.time, "time", lambda: wall + clock[0])
    monkeypatch.setattr(auto_intake, "resolve_trade_deal", lambda *_, **__: pytest.fail("receipt retry must not write a trade"))
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    stop = threading.Event()
    sends = []

    def build(_self):
        entered.set()
        clock[0] = 61
        try:
            if not release.wait(5):
                stop.set()
            raise RuntimeError("test SDK constructor released")
        finally:
            finished.set()

    def sender(**kwargs):
        sends.append(kwargs)
        if len(sends) == 1:
            return {"ok": False, "explicit_pre_acceptance_failure": True, "error_code": "PROVIDER_REJECTED"}
        assert entered.is_set() and not finished.is_set()
        stop.set()
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "recovered-during-init"}

    monkeypatch.setattr(OpenDTradePushListener, "_build_default_context", build)
    try:
        assert _run(tmp_path, monkeypatch, repo, source, _callback(tmp_path, repo, monkeypatch, sender), stop) == 0
    finally:
        release.set()
        assert finished.wait(2)
    assert len(sends) == 2
    assert _stored(tmp_path, payload)["receipt"]["result"]["delivery_confirmed"] is True
    assert _stored(tmp_path, payload)["receipt"]["attempt_count"] == 2
    assert (repo.list_trade_events(), repo.list_position_lots()) == before
    assert json.loads(source["status_path"].read_text())["stage"] == "start_cancelled"


@pytest.mark.parametrize("change", ["disabled_source", "disabled_receipt", "other_account", "cancelled"])
def test_receipt_recovery_rechecks_current_source_and_cancellation(tmp_path, monkeypatch, change):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _processor(tmp_path, repo, monkeypatch, lambda **_: pytest.fail("no route cannot send"), routed=False)(_payload())
    source = _source(tmp_path)
    stop = threading.Event()
    if change == "disabled_source":
        source["enabled"] = False
    elif change == "disabled_receipt":
        source["receipt"] = {"enabled": False}
    elif change == "other_account":
        source["account"] = "sy"
    else:
        stop.set()
    sends = []
    result = auto_intake.recover_trade_intake_receipts(
        repo=repo, source=source,
        receipt_callback=_callback(tmp_path, repo, monkeypatch, lambda **kwargs: sends.append(kwargs) or {
            "ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "must-not-send"}),
        stop_event=stop)
    assert sends == []
    assert result["sent"] == 0


def test_source_isolates_recovery_error_and_retries_during_start(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    source = _source(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])
    stop = threading.Event()
    calls = []

    def recover(**_):
        calls.append(clock[0])
        if len(calls) == 1:
            raise OSError("temporary Inbox read failure")
        stop.set()
        return {"checked": 0, "sent": 0, "errors": []}

    class Listener:
        def __init__(self, **_):
            pass
        def start(self, *, cancel_event, on_wait):
            saved = json.loads(source["status_path"].read_text())
            assert "temporary Inbox read failure" in saved["receipt_recovery"]["error"]
            clock[0] = 61
            on_wait()
            clock[0] = 122
            on_wait()
        def close(self):
            pass

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "recover_trade_intake_receipts", recover)
    assert _run(tmp_path, monkeypatch, repo, source, lambda _: {}, stop) == 0
    assert calls == [0, 61]


@pytest.mark.parametrize("phase", ["normal", "reconnect"])
def test_source_runs_due_recovery_in_existing_normal_and_reconnect_loops(tmp_path, monkeypatch, phase):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    source = {**_source(tmp_path), "reconnect_sec": 40}
    clock = [0.0]
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])
    recovered = []

    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, seconds):
            clock[0] += seconds
            return self.stopped

    stop = Stop()

    def recover(**kwargs):
        assert kwargs["source"]["account"] == "lx"
        assert not stop.is_set()
        recovered.append(clock[0])
        if len(recovered) == 2:
            stop.set()
        return {"checked": 0, "sent": 0, "errors": []}

    class Listener:
        starts = 0
        def __init__(self, **_):
            pass
        def start(self, *, cancel_event, on_wait):
            type(self).starts += 1
            if phase == "reconnect":
                raise ConnectionRefusedError("OpenD unavailable")
            clock[0] = 61
        def check_health(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "recover_trade_intake_receipts", recover)
    monkeypatch.setattr(auto_intake, "build_futu_gateway", lambda **_: pytest.fail("recovery must not require broker availability"))
    assert _run(tmp_path, monkeypatch, repo, source, lambda _: {}, stop) == 0
    assert recovered == [0, 61 if phase == "normal" else 60]
    assert Listener.starts == (1 if phase == "normal" else 2)


@pytest.mark.parametrize("kind,status,category,flag", [
    ("recorded", "applied", None, "notify_applied"),
    ("pending_retry", "failed", None, "notify_failed"),
    ("manual_required", "failed", None, "notify_failed"),
    ("manual_required", "unresolved", None, "notify_unresolved"),
    ("verification_pending", "unresolved", "applied", "notify_applied"),
    ("verification_pending", "unresolved", "failed", "notify_failed"),
    ("verification_pending", "unresolved", "unresolved", "notify_unresolved"),
])
def test_receipt_subflags_preserve_pending_semantic_result_until_reenabled(
    tmp_path, monkeypatch, kind, status, category, flag,
):
    from src.application.trades import inbox
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from src.application.trades.normalizer import normalize_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _payload("779")
    path = _source(tmp_path)["inbox_path"]
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", repo=repo,
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)
    diagnostics = {"retryable": kind == "pending_retry", "verification_pending": kind == "verification_pending"}
    if category:
        diagnostics["notification_category"] = category
    prepared = inbox.prepare_trade_receipt_result(
        path, inbox_id=inbox_id, expected_payload_version=1,
        result={"status": status, "reason": "applied_open" if kind == "recorded" else "sqlite_busy",
                "receipt_kind": kind, "diagnostics": diagnostics,
                "_receipt_payload": auto_intake._receipt_deal_snapshot(deal)})
    sends = []
    config = {"enabled": True, flag: False}
    callback = _callback(tmp_path, repo, monkeypatch, lambda **kwargs: sends.append(kwargs) or {
        "ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "subflag-enabled"},
        receipt_config=config)
    context = {"result": prepared, "deal": deal, "state": {}, "apply_changes": True,
               "effective_payload": payload, "inbox_path": path, "inbox_id": inbox_id}

    suppressed = callback(context)
    assert suppressed["status"] == "skipped"
    assert sends == []
    frozen = inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt"]
    assert frozen["receipt_kind"] == kind
    assert frozen["status"] == "pending" and frozen["attempt_count"] == 0
    if category:
        assert frozen["business_result"]["diagnostics"]["notification_category"] == category

    config[flag] = True
    assert callback(context)["delivery_confirmed"] is True
    callback(context)
    assert len(sends) == 1
    frozen = inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt"]
    assert frozen["status"] == "sent" and frozen["attempt_count"] == 1
    assert frozen["result"]["delivery_confirmed"] is True
    assert repo.list_trade_events() == []


@pytest.mark.parametrize("prior_attempts", [0, 19])
@pytest.mark.parametrize("rollback", [False, True])
def test_recovery_waits_for_expired_claim_transaction_before_concluding_absence(
    tmp_path, monkeypatch, prior_attempts, rollback,
):
    import sqlite3
    from src.application.trades import inbox
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from tests.test_trade_receipt_claim_fence import _execution, _process, _receipt_callback, _start_worker

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "external_execution_id": "888"}
    source = {**_source(tmp_path), "state_path": tmp_path / "worker" / "state.json"}
    path = source["inbox_path"]
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", repo=repo,
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET attempt_count=? WHERE inbox_id=?", (prior_attempts, inbox_id))
    inserted, release, selected, readback = (threading.Event() for _ in range(4))
    errors, sends = [], []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, sends)
    real_insert = repo.upsert_trade_event
    real_rows = auto_intake.list_trade_receipt_recovery_rows
    real_readback = auto_intake._readback_trade_receipt_result

    def paused_insert(event, **kwargs):
        result = real_insert(event, **kwargs)
        assert kwargs.get("conn") is not None
        inserted.set()
        assert release.wait(5)
        if rollback:
            raise sqlite3.OperationalError("database is locked")
        return result

    def recovery_rows(*args, **kwargs):
        rows = real_rows(*args, **kwargs)
        selected.set()
        return rows

    def observe_readback(**kwargs):
        readback.set()
        return real_readback(**kwargs)

    monkeypatch.setattr(repo, "upsert_trade_event", paused_insert)
    monkeypatch.setattr(auto_intake, "list_trade_receipt_recovery_rows", recovery_rows)
    monkeypatch.setattr(auto_intake, "_readback_trade_receipt_result", observe_readback)
    started = auto_intake.time.time()
    writer = _start_worker(lambda: _process(repo, tmp_path, "worker", payload, callback), errors)
    recovery = None
    try:
        assert inserted.wait(5)
        monkeypatch.setattr(inbox.time, "time", lambda: started + 121)
        recover = lambda: auto_intake.recover_trade_intake_receipts(
            repo=repo, source=source, receipt_callback=callback, stop_event=threading.Event())
        recovery = _start_worker(recover, errors)
        assert selected.wait(5)
        assert not readback.wait(0.1), "absence must not be read while an expired writer can still commit"
        assert sends == []
    finally:
        release.set()
        writer.join(5)
        if recovery is not None:
            recovery.join(5)
    assert not writer.is_alive() and recovery is not None and not recovery.is_alive()
    assert all(isinstance(error, inbox.TradePayloadClaimLost) for error in errors)
    recover()
    row = inbox.read_trade_payload(path, inbox_id=inbox_id)
    expected = ("manual_required" if prior_attempts == 19 else "pending_retry") if rollback else "recorded"
    assert row["result"]["receipt_kind"] == expected
    assert row["receipt"]["status"] == "sent"
    assert len(sends) == 1
    assert len(repo.list_trade_events()) == (0 if rollback else 1)
    assert len(repo.list_position_lots()) == (0 if rollback else 1)
    if not rollback:
        assert "✅ 已记录" in sends[0]["message"] and "未记录" not in sends[0]["message"]
