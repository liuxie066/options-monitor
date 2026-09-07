from __future__ import annotations

import multiprocessing
import json
import os
import sqlite3
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.auto_intake import _process_payload
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.inbox import (
    TradePayloadClaimLost,
    claim_trade_payload,
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    mark_trade_payload_retryable,
    mark_trade_payload_review,
    read_trade_payload,
    resume_trade_payload,
    save_trade_payload_result,
    trade_payload_commit_scope,
)
from src.application.trades.inbox_authority import resolve_execution_inbox_path


def _execution(*, physical: str = "123", deal_id: str = "fill-1", price: str = "2.50") -> dict:
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {
            "broker_id": "futu", "external_account_id": physical, "environment": "REAL",
            "broker_account_id": f"futu:REAL:{physical}", "account_label": "lx",
        },
        "instrument_ref": {
            "asset_type": "option", "market": "US", "symbol": "NVDA", "currency": "USD",
            "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "100",
        },
        "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{deal_id}",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": price,
        "currency": "USD", "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _process(repo, root: Path, entry: str, payload: dict, **kwargs):
    return _process_payload(
        payload, repo=repo, state_path=root / entry / "state.json",
        audit_path=root / entry / "audit.jsonl", inbox_path=root / entry / "inbox.sqlite3",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, **kwargs,
    )


def _candidate_event(repo, payload, *, stock: bool, event_id: str, conn=None):
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.api import execution_identity_from_input

    raw = {"execution_input": payload, "execution_id": execution_identity_from_input(payload)}
    if stock:
        event = {"stock_event_id": event_id, "account": "lx", "event_type": "sale",
                 "trade_time_ms": 1_000, **raw}
        repo.upsert_assigned_stock_event(event, conn=conn)
        return event
    event = TradeEvent(
        event_id=event_id, event_type="open", event_time_ms=1_000,
        contract_key=ContractKey.from_values(
            broker="futu", account="lx", underlying_symbol="NVDA", option_type="put",
            position_side="short", strike=100, expiration_ymd="2026-09-18",
        ),
        contracts=1, price=2.5, currency="USD", source="test", multiplier=100,
        lot_id=f"lot-{event_id}", raw_payload=raw,
    )
    repo.upsert_trade_event(event, conn=conn)
    return event.to_dict()


@pytest.mark.parametrize("stock", [False, True])
def test_inbox_candidates_bound_decoding_and_keep_durable_conflicts(tmp_path, monkeypatch, stock):
    from src.application.ledger.api import execution_identity_from_input

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    if stock:
        payload = {**payload, "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "currency": "USD", "market": "US"},
                   "side": "sell", "position_effect": "close"}
    with repo._writer_connection(begin_immediate=True) as conn:
        for i in range(30):
            _candidate_event(repo, {**payload, "external_execution_id": f"unrelated-{i}"},
                             stock=stock, event_id=f"unrelated-{i}", conn=conn)
        _candidate_event(repo, payload, stock=stock, event_id="target", conn=conn)
    before = repo.list_trade_events(), repo.list_assigned_stock_events()
    proxy = SimpleNamespace(list_trade_events=lambda: before[0], list_assigned_stock_events=lambda: before[1])
    seen = []
    targeted_name = "list_assigned_stock_events_for_execution" if stock else "list_trade_events_for_execution"
    read = getattr(repo, targeted_name)

    def candidates(identity):
        rows = read(identity)
        seen.append(len(rows))
        return rows

    def unexpected(**_kwargs):
        raise AssertionError("indexed reception must not read all events")

    monkeypatch.setattr(repo, targeted_name, candidates)
    monkeypatch.setattr(repo, "list_trade_events", unexpected)
    monkeypatch.setattr(repo, "list_assigned_stock_events", unexpected)
    for i, incoming in enumerate((payload, {**payload, "external_order_id": "conflicting-order"})):
        identity = execution_identity_from_input(incoming)
        results = []
        for name, owner in (("oracle", proxy), ("indexed", repo)):
            path = tmp_path / f"{name}-{i}.sqlite3"
            inbox_id = enqueue_trade_payload(path, payload=incoming, source="push", repo=owner, broker_deal_key=identity)
            assert enqueue_trade_payload(path, payload=incoming, source="backfill", repo=owner, broker_deal_key=identity) == inbox_id
            row = read_trade_payload(path, inbox_id=inbox_id, read_only=True)
            results.append((row["status"], row["last_error"], row["receipt_recovery_allowed"]))
        assert results[0] == results[1]
        assert results[1][2] == 0
        assert results[1][0] == ("conflict" if i else "pending")
    assert seen and set(seen) == {1}


@pytest.mark.parametrize("stock", [False, True])
def test_indexed_legacy_candidates_preserve_existence_and_receipt_qualification(tmp_path, stock):
    from src.application.ledger.api import execution_identity_from_input
    from src.application.trades.inbox import _has_persisted_execution

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    stored = _candidate_event(repo, payload, stock=stock, event_id="legacy")
    table, key = ("assigned_stock_events", "stock_event_id") if stock else ("trade_events", "event_id")
    aliases = ["source_deal_id", "deal_id", "futu_deal_id", "external_execution_id", "dealID", "dealId", "id"]
    variants = [({}, False), ({"execution_id": None}, False), ({"execution_id": ""}, False),
                ({"execution_input": payload}, True)]
    variants += [({alias: payload["external_execution_id"]}, None) for alias in aliases]
    variants += [({"stock_settlement": {"source_event_id": payload["external_execution_id"]}}, None)] if not stock else []
    for field, value in (("external_account_id", "other"), ("environment", "SIMULATE"), ("broker_id", "other")):
        other = {**payload, "broker_account_ref": {**payload["broker_account_ref"], field: value}}
        variants.append(({"execution_input": other, "execution_id": execution_identity_from_input(other)}, False))
    other = {**payload, "external_id_namespace": "other.deals"}
    variants.append(({"execution_input": other, "execution_id": execution_identity_from_input(other)}, False))
    for i, (raw, expected) in enumerate(variants):
        event = {**stored, **raw} if stock else {**stored, "raw_payload": raw}
        if stock:
            event.pop("execution_input", None)
            event.pop("execution_id", None)
            event.update(raw)
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute(f"UPDATE {table} SET event_json=? WHERE {key}=?", (json.dumps(event), "legacy"))
        proxy = SimpleNamespace(list_trade_events=repo.list_trade_events, list_assigned_stock_events=repo.list_assigned_stock_events)
        identity = execution_identity_from_input(payload)
        assert _has_persisted_execution(proxy, identity, payload) is expected
        assert _has_persisted_execution(repo, identity, payload) is expected
        path = tmp_path / f"inbox-{i}.sqlite3"
        inbox_id = enqueue_trade_payload(path, payload=payload, source="push", repo=repo, broker_deal_key=identity)
        assert read_trade_payload(path, inbox_id=inbox_id, read_only=True)["receipt_recovery_allowed"] == int(expected is False)


def _worker(root_text: str, mode: str, ready, release, results) -> None:
    import src.application.trades.auto_intake as intake

    root = Path(root_text)
    repo = SQLiteOptionPositionsRepository(root / "ledger.sqlite3")
    normalize = intake.normalize_trade_deal
    claim = intake.claim_trade_payload

    def short_claim(*args, **kwargs):
        return claim(*args, **{**kwargs, "lease_ms": 1})

    def normalize_with_boundary(*args, **kwargs):
        value = normalize(*args, **kwargs)
        if mode == "crash_before_commit":
            os._exit(81)
        if mode == "pause_before_commit":
            ready.set()
            if not release.wait(15):
                raise RuntimeError("test commit barrier timed out")
        return value

    def before_receipt(result):
        if mode == "crash_after_commit":
            os._exit(82)
        return result

    intake.normalize_trade_deal = normalize_with_boundary
    if mode.startswith("crash_"):
        intake.claim_trade_payload = short_claim
    try:
        result = _process(repo, root, "push", _execution(), source="push", before_receipt_fn=before_receipt)
        results.put({"result": result})
    except BaseException as exc:
        results.put({"error": type(exc).__name__, "message": str(exc)})


def _stop(process, release) -> None:
    release.set()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)


def test_execution_inbox_path_is_ledger_owned_and_protocol_repos_keep_requested_path(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    expected = tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    assert resolve_execution_inbox_path(repo, tmp_path / "a" / "inbox.sqlite3") == expected
    assert resolve_execution_inbox_path(SimpleNamespace(primary_repo=repo), tmp_path / "b" / "inbox.sqlite3") == expected
    other_repo = SQLiteOptionPositionsRepository(tmp_path / "other.sqlite3")
    assert resolve_execution_inbox_path(other_repo, tmp_path / "a" / "inbox.sqlite3") == tmp_path / "other.sqlite3.trade_intake_inbox.sqlite3"
    requested = tmp_path / "protocol.sqlite3"
    assert resolve_execution_inbox_path(SimpleNamespace(), requested) == requested


def test_same_stem_ledgers_have_independent_intake_completion(tmp_path: Path) -> None:
    first = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    second = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    requested = tmp_path / "source" / "inbox.sqlite3"
    first_inbox = resolve_execution_inbox_path(first, requested)
    second_inbox = resolve_execution_inbox_path(second, requested)
    assert first_inbox != second_inbox
    for index, repo in enumerate((first, second)):
        result = _process(repo, tmp_path, str(index), _execution(), source="file")
        assert result["status"] == "applied"
        assert len(repo.list_trade_events()) == 1
        replay = _process(repo, tmp_path, f"retry-{index}", _execution(), source="file")
        assert replay["reason"] == "duplicate"
        assert len(repo.list_trade_events()) == 1
    assert first_inbox.exists() and second_inbox.exists()


@pytest.mark.parametrize("stage", ["normalize", "resolve"])
def test_transient_processing_exception_recovers_unchanged_once(
    tmp_path: Path,
    monkeypatch,
    stage: str,
) -> None:
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    owner = "normalize_trade_deal" if stage == "normalize" else "resolve_trade_deal"
    original = getattr(auto_intake, owner)
    monkeypatch.setattr(
        auto_intake,
        owner,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline transient failure")),
    )

    first = _process(repo, tmp_path, "initial", payload, source="push")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    saved = read_trade_payload(inbox, inbox_id=first["inbox_id"], read_only=True)
    assert first["status"] == "failed"
    assert saved["status"] == "pending"
    assert saved["result"]["diagnostics"]["retryable"] is True
    assert [row["inbox_id"] for row in list_retryable_trade_payloads(
        inbox, retry_delay_sec=0
    )] == [first["inbox_id"]]
    assert repo.list_trade_events() == []

    monkeypatch.setattr(auto_intake, owner, original)
    assert resume_trade_payload(
        inbox,
        inbox_id=first["inbox_id"],
        operator="offline-recovery",
        repo=repo,
    )
    recovered = _process(
        repo,
        tmp_path,
        "recovery",
        payload,
        source="manual",
    )
    assert recovered["status"] == "applied"
    assert len(repo.list_trade_events()) == 1

    replay = _process(
        repo,
        tmp_path,
        "replay",
        payload,
        source="manual",
        retry_failed_deal=True,
    )
    assert replay["reason"] == "duplicate"
    assert len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("requested_name", ["source/inbox.sqlite3", "ledger.sqlite3.trade_intake_inbox.sqlite3"])
def test_former_derived_inbox_cannot_be_abandoned_by_new_authority(tmp_path: Path, requested_name: str) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    former = repo.db_path.with_suffix(".trade_intake_inbox.sqlite3")
    payload = _execution()
    inbox_id = enqueue_trade_payload(former, payload=payload, source="push", repo=repo,
                                    broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    before = read_trade_payload(former, inbox_id=inbox_id, read_only=True)
    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        resolve_execution_inbox_path(repo, tmp_path / requested_name)
    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        _process(repo, tmp_path, "restart", payload, source="push")
    assert read_trade_payload(former, inbox_id=inbox_id, read_only=True) == before
    assert repo.list_trade_events() == []
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()


@pytest.mark.parametrize("namespace", ["futu.deal", "verified.partition.deal"])
def test_inferred_open_recovers_after_commit_with_one_receipt(tmp_path: Path, monkeypatch, namespace: str) -> None:
    from src.application.trades import auto_intake, receipt

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "position_effect": None, "side": "buy", "external_id_namespace": namespace}
    payload["instrument_ref"] = {**payload["instrument_ref"], "option_type": "call"}
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "offline-message"}

    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda **kwargs: kwargs,
    ))
    callback = auto_intake._build_receipt_callback(
        base=tmp_path, cfg={"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test"}},
        receipt_config={"enabled": True}, repo=repo,
    )

    class Crash(BaseException):
        pass

    def after_commit(_):
        raise Crash()

    with pytest.raises(Crash):
        _process(repo, tmp_path, "initial", payload, source="push", on_result_fn=callback, before_receipt_fn=after_commit)
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(events) == 1 and events[0]["event_type"] == "open"
    assert events[0]["raw_payload"]["execution_input"]["position_effect"] is None
    assert calls == [] and not (tmp_path / "initial/state.json").exists()
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    pending = list_retryable_trade_payloads(path, retry_delay_sec=0)[0]
    stored = read_trade_payload(path, inbox_id=pending["inbox_id"], read_only=True)
    assert stored["result"] is None and stored["receipt"] is None
    assert resume_trade_payload(path, inbox_id=pending["inbox_id"], operator="offline-recovery", repo=repo)
    renamed = {**payload, "broker_account_ref": {**payload["broker_account_ref"], "account_label": "renamed"}}
    recovered = _process_payload(
        renamed, repo=repo, state_path=tmp_path / "recovery/state.json", audit_path=tmp_path / "recovery/audit.jsonl",
        account_mapping={"123": "renamed"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, source="push", on_result_fn=callback,
    )
    assert (recovered["status"], recovered["action"], recovered["reason"]) == ("applied", "open", "applied_open")
    assert recovered["receipt"]["delivery_confirmed"] is True
    assert len(calls) == 1
    assert read_trade_payload(path, inbox_id=pending["inbox_id"])["receipt"]["status"] == "sent"
    assert _process(repo, tmp_path, "again", payload, source="push", on_result_fn=callback)["reason"] == "duplicate"
    assert len(calls) == 1
    assert repo.list_trade_events() == events and repo.list_position_lots() == lots


def test_file_review_cannot_revoke_claim_during_economic_commit(tmp_path: Path, monkeypatch) -> None:
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.trades import auto_intake
    from src.application.trades.file_intake import run_execution_file
    from src.application.trades.normalizer import normalize_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    received, committing, finished = threading.Event(), threading.Event(), threading.Event()

    def pause_review(*args, **kwargs):
        received.set()
        assert committing.wait(5)
        mark_trade_payload_review(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(auto_intake, "mark_trade_payload_review", pause_review)
    path = tmp_path / "input.jsonl"
    path.write_text(json.dumps({**payload, "data_type": "order_summary"}) + "\n")
    results = []
    worker = threading.Thread(target=lambda: results.append(run_execution_file(
        path, process_payload_fn=partial(
            _process_payload, repo=repo, state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx"},
            futu_account_ids=["123"], host="localhost", port=11111,
        ), configured_accounts=[payload["broker_account_ref"]], dry_run=False,
    )))
    worker.start()
    try:
        assert received.wait(5)
        inbox_id = enqueue_trade_payload(
            inbox, payload=payload, source="push", repo=repo,
            broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
        )
        claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
        assert claim is not None
        with trade_payload_commit_scope(inbox, claim=claim, repo=repo):
            committing.set()
            assert not finished.wait(0.2)
            current = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
            assert current["status"] == "pending"
            assert current["claim_id"] == claim["claim_id"]
            record_normalized_trade_event(repo, normalize_trade_deal(payload))
        assert finished.wait(5)
    finally:
        committing.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1
    assert len(repo.list_trade_events()) == 1
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)["status"] == "identity_needs_review"
    with pytest.raises(TradePayloadClaimLost):
        with trade_payload_commit_scope(inbox, claim=claim, repo=repo):
            pytest.fail("review completed before this second commit")


def test_saved_result_keeps_pm_intent_atomic_and_claimable_once(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    inbox_id = enqueue_trade_payload(
        inbox, payload=payload, source="push", repo=repo,
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
    )
    claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    intent = {"account": "lx", "request_id": "stock-refresh:atomic"}
    result = {"status": "skipped", "reason": "not_option", "portfolio_refresh_intent": intent}
    save_trade_payload_result(inbox, claim=claim, result=result)
    current = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert current["result"] == result
    assert json.loads(current["portfolio_refresh_intent_json"]) == intent
    assert current["portfolio_refresh_attempted_at_ms"] is None

    with pytest.raises(ValueError, match="portfolio refresh intent conflict"):
        save_trade_payload_result(inbox, claim=claim, result={
            "status": "changed", "portfolio_refresh_intent": {**intent, "request_id": "different"},
        })
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == current
    # A recovery result may omit the already-saved intent; it must remain available.
    save_trade_payload_result(inbox, claim=claim, result={"status": "skipped"})
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) == intent
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None


def test_new_known_associations_fence_old_claim_and_survive_original_payload_retry(tmp_path: Path, monkeypatch) -> None:
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    initial = _execution()
    initial.update(side="buy", position_effect=None, external_order_id=None, external_order_namespace=None)
    initial["instrument_ref"]["option_type"] = "call"
    normalized, release = threading.Event(), threading.Event()
    normalize = auto_intake.normalize_trade_deal
    results = []
    failures = []

    def pause_normalize(*args, **kwargs):
        deal = normalize(*args, **kwargs)
        if threading.current_thread().name == "unknown-effect-writer":
            normalized.set()
            assert release.wait(5)
        return deal

    monkeypatch.setattr(auto_intake, "normalize_trade_deal", pause_normalize)
    def stale_worker():
        try:
            results.append(_process(repo, tmp_path, "push", initial, source="push"))
        except TradePayloadClaimLost as exc:
            failures.append(exc)

    worker = threading.Thread(name="unknown-effect-writer", target=stale_worker)
    worker.start()
    try:
        assert normalized.wait(5)
        key = broker_deal_key_from_payload(initial, account_mapping={"123": "lx"})
        inbox_id = enqueue_trade_payload(inbox, payload=initial, source="push", broker_deal_key=key, repo=repo)
        before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert before["claim_id"]
        for evidence in (
            {**initial, "position_effect": "close"},
            {**initial, "external_order_id": "late-order", "external_order_namespace": "futu.order"},
        ):
            assert enqueue_trade_payload(inbox, payload=evidence, source="backfill", broker_deal_key=key, repo=repo) == inbox_id
        enriched = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert enriched["claim_id"] is None
        assert enriched["payload_version"] == before["payload_version"] + 2
        assert enriched["payload"]["position_effect"] is None
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results == []
    assert len(failures) == 1
    assert repo.list_trade_events() == []
    retry = _process(repo, tmp_path, "retry", initial, source="backfill")
    assert retry["status"] == "unresolved"
    assert repo.list_trade_events() == []
    audit = [json.loads(line) for line in (tmp_path / "retry" / "audit.jsonl").read_text().splitlines()]
    deal = next(row["deal"] for row in audit if row["phase"] == "normalized")
    assert deal["position_effect"] == "close"
    assert deal["order_id"] == "late-order"
    assert deal["execution_input"]["external_order_namespace"] == "futu.order"
    assert deal["raw_payload"]["position_effect"] is None


def test_nonempty_legacy_inbox_requires_migration_without_mutation(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    old = tmp_path / "legacy" / "inbox.sqlite3"
    key = broker_deal_key_from_payload(_execution(), account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(old, payload=_execution(), source="push", broker_deal_key=key, repo=repo)
    before = read_trade_payload(old, inbox_id=inbox_id, read_only=True)

    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        resolve_execution_inbox_path(repo, old)

    assert read_trade_payload(old, inbox_id=inbox_id, read_only=True) == before
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()


def test_empty_execution_inbox_keeps_legacy_lifecycle_control_in_place(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    old = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE trade_inbox (inbox_id TEXT)")
        conn.execute("CREATE TABLE lifecycle_control (source_id TEXT)")
        conn.execute("INSERT INTO lifecycle_control VALUES ('lx')")
    before = old.read_bytes()
    assert resolve_execution_inbox_path(repo, old) == tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    assert old.read_bytes() == before


def test_retry_scope_reads_past_foreign_backlog_without_starving_own_account(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.sqlite3"
    for index in range(105):
        payload = _execution(physical="456", deal_id=f"foreign-{index}")
        enqueue_trade_payload(inbox, payload=payload, source="backfill", broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"456": "sy"}))
    own = _execution()
    target = enqueue_trade_payload(inbox, payload=own, source="file", broker_deal_key=broker_deal_key_from_payload(own, account_mapping={"123": "lx"}))
    retry = list_retryable_trade_payloads(inbox, account_ids=["123"], limit=1, retry_delay_sec=0)
    assert [row["inbox_id"] for row in retry] == [target]
    assert list_retryable_trade_payloads(inbox, account_ids=[], limit=1) == []


def test_new_inbox_rejects_old_writer_mutations_and_preserves_evidence(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.sqlite3"
    payload = _execution()
    inbox_id = enqueue_trade_payload(
        inbox, payload=payload, source="push",
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
    )
    original = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    with sqlite3.connect(inbox) as old_writer:
        for table in ("trade_inbox", "trade_inbox_evidence"):
            before = old_writer.execute(f"SELECT * FROM {table}").fetchall()
            assert len(before) == 1
            for statement in (
                f"UPDATE {table} SET payload_json = '{{}}'",
                f"DELETE FROM {table}",
                f"INSERT INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.OperationalError, match="trade_inbox_writer_version"):
                    old_writer.execute(statement)
                old_writer.rollback()
                assert old_writer.execute(f"SELECT * FROM {table}").fetchall() == before
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == original


def test_claim_takeover_and_exhaustion_require_safe_explicit_recovery(tmp_path: Path, monkeypatch) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=key, repo=repo)
    now = [time.time()]
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: now[0])
    old_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo, lease_ms=1)
    assert old_claim is not None
    now[0] += 1
    current_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    assert current_claim is not None
    assert current_claim["claim_id"] != old_claim["claim_id"]
    with pytest.raises(TradePayloadClaimLost):
        with trade_payload_commit_scope(inbox, claim=old_claim, repo=repo):
            pytest.fail("a replaced claim must not reach economic commit")
    with pytest.raises(TradePayloadClaimLost):
        save_trade_payload_result(inbox, claim=old_claim, result={"status": "applied"})
    pending = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert pending["claim_id"] == current_claim["claim_id"]
    assert pending["result"] is None

    for attempt in range(20):
        if attempt:
            current_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
        assert current_claim is not None
        mark_trade_payload_retryable(inbox, inbox_id=inbox_id, error="offline failure", claim=current_claim)
    exhausted = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert exhausted["status"] == "pending"
    assert exhausted["attempt_count"] == 20
    assert claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo) is None
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []

    assert resume_trade_payload(inbox, inbox_id=inbox_id, operator="test-operator", repo=repo)
    resumed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert resumed["attempt_count"] == 0
    assert resumed["last_error"] == "resumed_by:test-operator"
    assert [row["inbox_id"] for row in list_retryable_trade_payloads(inbox, retry_delay_sec=0)] == [inbox_id]
    resumed_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    assert resumed_claim is not None
    with trade_payload_commit_scope(inbox, claim=resumed_claim, repo=repo):
        pass

    assert enqueue_trade_payload(inbox, payload=_execution(price="3"), source="file", broker_deal_key=key, repo=repo) == inbox_id
    conflict = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert conflict["status"] == "conflict"
    assert not resume_trade_payload(inbox, inbox_id=inbox_id, operator="test-operator", repo=repo)
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == conflict
    assert claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo) is None
    with pytest.raises(TradePayloadClaimLost):
        save_trade_payload_result(inbox, claim=resumed_claim, result={"status": "applied"})
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("SELECT inbox_id, operator FROM trade_inbox_recovery").fetchall() == [(inbox_id, "test-operator")]
        assert conn.execute("SELECT COUNT(*) FROM trade_inbox_evidence").fetchone()[0] == 2
    assert repo.list_trade_events() == []


def test_conflict_from_another_entry_invalidates_claim_before_economic_commit(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), "pause_before_commit", ready, release, results))
    process.start()
    try:
        assert ready.wait(10), "writer failed to reach the precommit barrier"
        second = _process(repo, tmp_path, "file", _execution(price="3"), source="file")
        release.set()
        process.join(10)
        assert process.exitcode == 0
        assert second["reason"] == "inbox_conflict"
        assert repo.list_trade_events() == []
        authoritative = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
        assert read_trade_payload(authoritative, inbox_id=second["inbox_id"])["status"] == "conflict"
    finally:
        _stop(process, release)


def test_conflict_after_economic_commit_preserves_original_event_across_entries(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), "commit", ready, release, results))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 0
        first = results.get(timeout=2)
        assert first["result"]["status"] == "applied"
        original = repo.list_trade_events()
        assert len(original) == 1
        second = _process(repo, tmp_path, "file", _execution(price="3"), source="file")
        assert second["reason"] == "inbox_conflict"
        assert repo.list_trade_events() == original
    finally:
        _stop(process, release)


@pytest.mark.parametrize("phase,exit_code", [("before_commit", 81), ("after_commit", 82)])
def test_process_crash_recovers_expired_claim_from_another_entry(tmp_path: Path, phase: str, exit_code: int) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), f"crash_{phase}", ready, release, results))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == exit_code
        original = repo.list_trade_events()
        assert len(original) == int(phase == "after_commit")
        authoritative = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
        pending = list_retryable_trade_payloads(authoritative, account_ids=["123"], retry_delay_sec=0)
        assert len(pending) == 1
        interrupted = read_trade_payload(authoritative, inbox_id=pending[0]["inbox_id"])
        assert interrupted["result"] is None
        assert interrupted["claim_id"] is not None
        time.sleep(0.01)  # Worker uses a real 1 ms lease; no manual resume or state repair.
        replay = _process(repo, tmp_path, "manual", _execution(), source="manual")
        final = repo.list_trade_events()
        assert len(final) == 1
        if original:
            assert final == original
        assert replay["inbox_id"] == interrupted["inbox_id"]
        saved = read_trade_payload(authoritative, inbox_id=replay["inbox_id"])
        assert saved["status"] == "handled"
        assert saved["result"] is not None
        assert saved["claim_id"] is None
    finally:
        _stop(process, release)
