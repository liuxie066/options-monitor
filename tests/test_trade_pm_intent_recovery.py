import json
import multiprocessing
import threading
import time
from types import SimpleNamespace

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.inbox import (
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    list_unclaimed_trade_payload_refresh_intents,
    mark_trade_payload_handled,
    read_trade_payload,
    record_trade_payload_refresh_intent,
)


class ProcessInterrupted(BaseException):
    pass


def _inbox(tmp_path):
    return resolve_execution_inbox_path(
        SimpleNamespace(db_path=tmp_path / "ledger.sqlite3"), tmp_path / "legacy-uncreated.sqlite3",
    )


def _stock(execution_id="stock-1", *, physical="123", account="lx"):
    return {
        "schema_version": "trade_execution.v1", "acc_id": physical,
        "broker_account_ref": {"broker_id": "futu", "external_account_id": physical,
            "environment": "REAL", "broker_account_id": f"futu:REAL:{physical}", "account_label": account},
        "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"},
        "external_id_namespace": "futu.deal", "external_execution_id": execution_id,
        "side": "buy", "quantity": "1", "price": "100", "currency": "USD",
        "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _core(tmp_path, payload, *, source="push"):
    return auto_intake._process_payload(
        payload, repo=SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3"),
        state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx", "456": "sy"}, futu_account_ids=["123", "456"],
        apply_changes=True, host="127.0.0.1", port=11111, source=source, allow_external_lookup=False,
    )


def _loop(tmp_path, monkeypatch, *, payload=None, client=None, apply_changes=True):
    stop = threading.Event()
    class Listener:
        def __init__(self, *, on_deal, **kwargs):
            self.on_deal = on_deal
        def start(self, **kwargs):
            if payload is not None:
                self.on_deal(payload)
        def check_health(self):
            stop.set()
        def close(self):
            pass
    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(auto_intake, "enrich_trade_push_payload_with_account_id", lambda raw, **kwargs: raw)
    monkeypatch.setattr(auto_intake, "append_lifecycle_attempt_checkpoint_seal", lambda *args, **kwargs: None)
    monkeypatch.setattr(auto_intake, "reconcile_due_lifecycle_cases_for_source", lambda *args, **kwargs: {})
    monkeypatch.setattr(auto_intake, "resolve_portfolio_management_client", lambda *args, **kwargs: client)
    source = {"id": "lx", "account": "lx", "host": "127.0.0.1", "port": 11111,
        "state_path": tmp_path / "state.json", "audit_path": tmp_path / "audit.jsonl",
        "status_path": tmp_path / "status.json", "inbox_path": _inbox(tmp_path),
        "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"],
        "backfill": {"enabled": False}, "settlement_observation": {"enabled": False}}
    return auto_intake._run_listener_source_loop(
        source=source, repo=SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3"), cfg={},
        cfg_path=tmp_path / "config.json", runtime_root=tmp_path, runtime_root_source="test",
        intake_cfg={"mode": "apply" if apply_changes else "dry-run", "enabled": True},
        apply_changes=apply_changes, receipt_callback=lambda context: {},
        process_lock=threading.RLock(), stop_event=stop,
    )


@pytest.mark.parametrize("outcome", ["accepted", "timeout", "interrupted"])
def test_listener_restart_recovers_handled_unclaimed_pm_intent_once(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: True)
    original = auto_intake._durable_fee_target
    def crash_after_handled(**kwargs):
        raise ProcessInterrupted()
    monkeypatch.setattr(auto_intake, "_durable_fee_target", crash_after_handled)
    with pytest.raises(ProcessInterrupted):
        _loop(tmp_path, monkeypatch, payload=_stock())
    inbox = _inbox(tmp_path)
    candidates = list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"123": "lx"})
    assert len(candidates) == 1
    inbox_id = candidates[0]["inbox_id"]
    saved = read_trade_payload(inbox, inbox_id=inbox_id)
    intent = json.loads(saved["portfolio_refresh_intent_json"])
    assert saved["status"] == "handled"
    assert saved["portfolio_refresh_attempted_at_ms"] is None
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []
    monkeypatch.setattr(auto_intake, "_durable_fee_target", original)
    calls = []
    class PMClient:
        def request_holdings_refresh(self, **kwargs):
            calls.append(kwargs)
            assert read_trade_payload(inbox, inbox_id=inbox_id)["portfolio_refresh_attempted_at_ms"] is not None
            if outcome == "timeout":
                raise TimeoutError("ambiguous PM response")
            if outcome == "interrupted":
                raise ProcessInterrupted()
    if outcome == "interrupted":
        with pytest.raises(ProcessInterrupted):
            _loop(tmp_path, monkeypatch, client=PMClient())
    else:
        assert _loop(tmp_path, monkeypatch, client=PMClient()) == 0
    assert _loop(tmp_path, monkeypatch, client=PMClient()) == 0
    assert calls == [{**intent, "timeout": 2.0}]
    assert list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"123": "lx"}) == []
    assert SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3").list_trade_events() == []


def test_recovery_batch_coalesces_accounts_and_keeps_other_source_unclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: True)
    results = [_core(tmp_path, _stock(value)) for value in ("stock-1", "stock-2")]
    other = _core(tmp_path, _stock("stock-sy", physical="456", account="sy"))
    calls = []
    client = SimpleNamespace(request_holdings_refresh=lambda **kwargs: calls.append(kwargs))
    assert _loop(tmp_path, monkeypatch, client=client) == 0
    assert _loop(tmp_path, monkeypatch, client=client) == 0
    assert len(calls) == 1 and calls[0]["account"] == "lx"
    assert calls[0]["request_id"] in {item["portfolio_refresh_intent"]["request_id"] for item in results}
    inbox = _inbox(tmp_path)
    assert all(read_trade_payload(inbox, inbox_id=item["inbox_id"])["portfolio_refresh_attempted_at_ms"] is not None for item in results)
    assert read_trade_payload(inbox, inbox_id=other["inbox_id"])["portfolio_refresh_attempted_at_ms"] is None


@pytest.mark.parametrize("excluded", ["pending", "historical", "manual", "conflict", "claimed", "physical", "label"])
def test_recovery_query_preserves_scope_and_delivery_gates(tmp_path, excluded):
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    inbox = tmp_path / "inbox.sqlite3"
    payload = _stock()
    source = {"historical": "file", "manual": "manual"}.get(excluded, "push")
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source=source, broker_deal_key=key)
    record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "stock-refresh:test"})
    if excluded != "pending":
        mark_trade_payload_handled(inbox, inbox_id=inbox_id, result={"status": "skipped", "reason": "not_option_deal"})
    if excluded == "conflict":
        enqueue_trade_payload(inbox, payload={**payload, "price": "101"}, source=source, broker_deal_key=key)
        assert read_trade_payload(inbox, inbox_id=inbox_id)["status"] == "conflict"
    if excluded == "claimed":
        assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id)
    mapping = {"456": "lx"} if excluded == "physical" else {"123": "sy"} if excluded == "label" else {"123": "lx"}
    before = inbox.read_bytes()
    assert list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping=mapping) == []
    assert inbox.read_bytes() == before


@pytest.mark.parametrize("gate", ["dry-run", "pm-disabled"])
def test_disabled_delivery_does_not_claim_saved_pm_intent(tmp_path, monkeypatch, gate):
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: True)
    result = _core(tmp_path, _stock())
    calls = []
    if gate == "pm-disabled":
        monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: False)
    assert _loop(tmp_path, monkeypatch, client=SimpleNamespace(request_holdings_refresh=lambda **kwargs: calls.append(kwargs)), apply_changes=gate != "dry-run") == 0
    inbox = _inbox(tmp_path)
    assert read_trade_payload(inbox, inbox_id=result["inbox_id"])["portfolio_refresh_attempted_at_ms"] is None
    assert calls == []


def test_pending_retry_while_pm_disabled_preserves_intent_for_enabled_restart(tmp_path, monkeypatch):
    from src.application.trades.inbox import resume_trade_payload

    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    original = auto_intake.update_trade_intake_state_entries
    def interrupt(path, state, **kwargs):
        original(path, state, **kwargs)
        raise ProcessInterrupted()
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", interrupt)
    with pytest.raises(ProcessInterrupted):
        _core(tmp_path, _stock())
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", original)
    path = _inbox(tmp_path)
    after_lease = time.time() + 121
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: after_lease)
    pending = list_retryable_trade_payloads(path, retry_delay_sec=0)[0]
    saved = read_trade_payload(path, inbox_id=pending["inbox_id"])
    original_intent = json.loads(saved["portfolio_refresh_intent_json"])
    resume_trade_payload(path, inbox_id=pending["inbox_id"], operator="offline-test",
                         repo=SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: False)
    assert _loop(tmp_path, monkeypatch, client=auto_intake.PORTFOLIO_MANAGEMENT_DISABLED) == 0
    saved = read_trade_payload(path, inbox_id=pending["inbox_id"])
    assert saved["status"] == "handled" and saved["portfolio_refresh_attempted_at_ms"] is None
    calls = []
    client = SimpleNamespace(request_holdings_refresh=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    assert _loop(tmp_path, monkeypatch, client=client) == 0
    assert _loop(tmp_path, monkeypatch, client=client) == 0
    assert calls == [{**original_intent, "timeout": 2.0}]


def _claim_saved_pm(path, ready, release, results):
    candidates = list_unclaimed_trade_payload_refresh_intents(path, account_mapping={"123": "lx"})
    ready.put(len(candidates))
    if not release.wait(10):
        raise RuntimeError("PM claim barrier timed out")
    results.put(claim_trade_payload_refresh_intent(path, inbox_id=candidates[0]["inbox_id"]))


def test_two_recovery_processes_claim_original_intent_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: True)
    result = _core(tmp_path, _stock())
    path = _inbox(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, release, results = context.Queue(), context.Event(), context.Queue()
    workers = [context.Process(target=_claim_saved_pm, args=(str(path), ready, release, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert [ready.get(timeout=10) for _ in workers] == [1, 1]
        release.set()
        claims = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert claims.count(None) == 1
        assert result["portfolio_refresh_intent"] in claims
    finally:
        release.set()
        for worker in workers:
            worker.join(1)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


@pytest.mark.parametrize("legacy_inbox", [False, True])
def test_saved_inbox_public_preview_uses_authority_and_legacy_guard(tmp_path, monkeypatch, capsys, legacy_inbox):
    import shutil
    from src.interfaces.cli.main import main as public_main

    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda cfg: True)
    stored = _core(tmp_path, _stock())
    ledger = tmp_path / "ledger.sqlite3"
    source = {"id": "lx", "account": "lx", "host": "127.0.0.1", "port": 11111,
        "state_path": tmp_path / "state.json", "audit_path": tmp_path / "audit.jsonl",
        "status_path": tmp_path / "status.json", "inbox_path": tmp_path / "configured-inbox.sqlite3",
        "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"]}
    intake_cfg = {"enabled": True, "mode": "apply", "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl", "status_path": tmp_path / "status.json",
        "receipt": {"enabled": False}, "backfill": {"enabled": False},
        "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"], "sources": [source]}
    monkeypatch.setattr("src.application.trades.process_supervisor.run_trade_intake_process", auto_intake.main)
    monkeypatch.setattr(auto_intake, "load_config", lambda **kwargs: {})
    monkeypatch.setattr(auto_intake, "resolve_trade_intake_config", lambda *args, **kwargs: intake_cfg)
    monkeypatch.setattr(auto_intake, "resolve_position_ledger_sqlite_path", lambda **kwargs: ledger)
    def unexpected_open(**kwargs):
        pytest.fail("saved Inbox preview must not open a write-capable Ledger")
    monkeypatch.setattr(auto_intake, "open_position_ledger_from_runtime_config", unexpected_open)
    if legacy_inbox:
        shutil.copy2(_inbox(tmp_path), source["inbox_path"])
    before = ledger.read_bytes()
    result = public_main(["run", "trade-intake", "--config", str(tmp_path / "config.json"),
        "--runtime-root", str(tmp_path), "--inbox-id", stored["inbox_id"]])
    output = capsys.readouterr().out
    if legacy_inbox:
        assert result == 2
        assert "legacy_inbox_migration_required" in output
    else:
        assert result == 0
        assert json.loads(output)["inbox_id"] == stored["inbox_id"]
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("kind", ["pending_retry", "verification_pending"])
def test_resumed_unclaimed_work_keeps_processing_and_verification_owners(tmp_path, monkeypatch, kind):
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from src.application.trades.inbox import (
        begin_trade_receipt_attempt, claim_trade_payload, finish_trade_receipt_attempt,
        list_trade_receipt_recovery_rows, mark_trade_payload_retryable, resume_trade_payload,
    )

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    path = _inbox(tmp_path)
    payload = _stock()
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
    claim = claim_trade_payload(path, inbox_id=inbox_id, repo=repo)
    mark_trade_payload_retryable(path, inbox_id=inbox_id, claim=claim, error="offline interrupted processing",
        result={"status": "failed", "receipt_kind": kind,
                "diagnostics": {"retryable": kind == "pending_retry", "verification_pending": kind == "verification_pending"}})
    attempt = begin_trade_receipt_attempt(path, inbox_id=inbox_id, result_key=kind,
        route={"provider": "wechat_clawbot", "channel": "wechat_clawbot", "target": "wechat:offline-test"}, message="offline notice")
    assert attempt["claimed"]
    finish_trade_receipt_attempt(path, inbox_id=inbox_id, attempt_id=attempt["attempt_id"], result={"delivery_confirmed": True})
    assert resume_trade_payload(path, inbox_id=inbox_id, operator="offline-test", repo=repo)
    row = read_trade_payload(path, inbox_id=inbox_id)
    assert row["attempt_count"] == 0 and row["result"]["receipt_kind"] == kind
    assert row["receipt"]["status"] == "sent"
    after_due = time.time() + 61
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: after_due)
    recovery = list_trade_receipt_recovery_rows(path, account_ids=["123"])
    assert [item["inbox_id"] for item in recovery] == ([inbox_id] if kind == "verification_pending" else [])
    assert bool(claim_trade_payload(path, inbox_id=inbox_id, repo=repo)) is (kind == "pending_retry")
