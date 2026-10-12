from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

import pytest

from src.application.ledger import api, notification_outbox as owner
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import lifecycle_outbox as facade, receipt


def test_outbox_state_operations_use_the_ledger_owner() -> None:
    wrappers = {"mark_notification_send_started", "mark_notification_batch_send_started"}
    for name in facade.__all__:
        if name not in wrappers | {"dispatch_notification_batch_once"}:
            assert getattr(facade, name) is getattr(api, name) is getattr(owner, name)
    for name in wrappers:
        assert getattr(api, name) is getattr(owner, name)
        assert getattr(facade, name) is not getattr(owner, name)
    app_tree = ast.parse(Path(facade.__file__).read_text())
    assert {node.name for node in app_tree.body if isinstance(node, ast.FunctionDef)} == (
        wrappers | {"dispatch_notification_batch_once"}
    )
    assert not any(
        isinstance(node, ast.Attribute)
        and (node.attr.startswith("compare_and_set_") or node.attr in {"execute", "_writer_connection"})
        for node in ast.walk(app_tree)
    )
    for node in ast.walk(ast.parse(Path(owner.__file__).read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("src.application.trades")
            assert node.module not in {"src.application.ledger.api", "src.application.cash_conversion"}


def _claimed(tmp_path: Path, *, batch: bool) -> tuple[SQLiteOptionPositionsRepository, dict, int]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    intent = api.build_notification_intent(
        case_id="case-freeze", transition_type="needs_review", resolution_revision=1,
        transition_key="case-freeze:review", state_fingerprint="state-freeze",
        payload={"account": "lx", "case_id": "case-freeze", "symbol": "NVDA", "transition_type": "needs_review"},
    )
    facade.enqueue_notification_intent(repo, intent)
    row = repo.get_trade_lifecycle_notification(intent["outbox_id"])
    current = int(row["created_at_ms"]) + facade.QUIET_WINDOW_MS
    if batch:
        facade.plan_notification_batch(
            repo, route=facade.build_notification_batch_route(provider="feishu_app", channel="bot", target="fixture"),
            allowed_accounts=["lx"], now_ms=current,
        )
        row = facade.claim_next_notification_batch(repo, now_ms=current, claim_id="claim-first")
    else:
        row = facade.claim_next_notification(repo, now_ms=current, claim_id="claim-first", account="lx")
    assert row is not None
    return repo, row, current


def _operations(batch: bool) -> tuple:
    if batch:
        return (facade.mark_notification_batch_send_started, facade.complete_notification_batch_attempt,
                facade.claim_next_notification_batch, "batch_id", "build_trade_lifecycle_notification_batch_message")
    return (facade.mark_notification_send_started, facade.complete_notification_attempt,
            facade.claim_next_notification, "outbox_id", "build_trade_lifecycle_notification_message")


@pytest.mark.parametrize("batch", [False, True])
def test_retry_reuses_frozen_body_and_rejects_corrupt_digest(tmp_path, monkeypatch, batch):
    repo, row, current = _claimed(tmp_path, batch=batch)
    start, complete, claim, id_key, renderer_name = _operations(batch)
    rendered: list[dict] = []

    def render(payload):
        rendered.append(copy.deepcopy(payload))
        return "原始冻结正文"

    monkeypatch.setattr(receipt, renderer_name, render)
    identity = {id_key: row[id_key]}
    original_payload = copy.deepcopy(row["payload"])
    started = start(repo, **identity, claim_id="claim-first", now_ms=current)
    assert rendered == [original_payload]
    assert started["payload"]["_frozen_message"] == "原始冻结正文"
    assert started["payload_hash"] == row["payload_hash"]
    get = repo.get_trade_lifecycle_notification_batch if batch else repo.get_trade_lifecycle_notification
    stored = get(row[id_key])
    assert stored["payload"] == original_payload
    assert stored["provider_receipt"]["message_sha256"] == hashlib.sha256("原始冻结正文".encode()).hexdigest()
    failed = complete(
        repo, **identity, claim_id="claim-first", outcome="explicit_failed", now_ms=current,
        provider_receipt={"rendered_message": "provider replacement", "status": "explicit_failed"},
    )
    assert failed["provider_receipt"]["rendered_message"] == "原始冻结正文"
    due = failed["next_attempt_at_ms"]
    second = claim(repo, now_ms=due, claim_id="claim-second")
    assert second is not None

    def unexpected_render(_payload):
        pytest.fail("frozen body must not be rendered again")

    monkeypatch.setattr(receipt, renderer_name, unexpected_render)
    retried = start(repo, **identity, claim_id="claim-second", now_ms=due)
    assert retried["payload"]["_frozen_message"] == "原始冻结正文"
    assert retried["payload_hash"] == row["payload_hash"]
    failed = complete(repo, **identity, claim_id="claim-second", outcome="explicit_failed", now_ms=due)
    third = claim(repo, now_ms=failed["next_attempt_at_ms"], claim_id="claim-third")
    assert third is not None
    cas = repo.compare_and_set_trade_lifecycle_notification_batch if batch else repo.compare_and_set_trade_lifecycle_notification
    assert cas(
        **identity, expected_status="claimed", new_status="claimed", expected_claim_id="claim-third",
        fields={"provider_receipt_json": {**third["provider_receipt"], "message_sha256": "0" * 64}},
    )
    before = get(row[id_key])
    with pytest.raises(ValueError, match="frozen receipt digest mismatch"):
        start(repo, **identity, claim_id="claim-third", now_ms=failed["next_attempt_at_ms"])
    assert get(row[id_key]) == before
    assert before["status"] == "claimed"


@pytest.mark.parametrize("batch", [False, True])
def test_renderer_failure_preserves_claim_and_attempt_count(tmp_path, monkeypatch, batch):
    repo, row, current = _claimed(tmp_path, batch=batch)
    start, _complete, _claim, id_key, renderer_name = _operations(batch)

    def render(_payload):
        raise RuntimeError("fixture renderer failure")

    monkeypatch.setattr(receipt, renderer_name, render)
    with pytest.raises(RuntimeError, match="fixture renderer failure"):
        start(repo, **{id_key: row[id_key]}, claim_id=row["claim_id"], now_ms=current)
    get = repo.get_trade_lifecycle_notification_batch if batch else repo.get_trade_lifecycle_notification
    assert get(row[id_key]) == row
    assert row["attempt_count"] == (0 if batch else 1)


@pytest.mark.parametrize("batch", [False, True])
def test_wrong_send_start_claim_does_not_write(tmp_path, batch):
    repo, row, current = _claimed(tmp_path, batch=batch)
    start, _complete, _claim, id_key, _renderer_name = _operations(batch)
    with pytest.raises(ValueError, match="claim lost before send_started"):
        start(repo, **{id_key: row[id_key]}, claim_id="wrong-claim", now_ms=current)
    get = repo.get_trade_lifecycle_notification_batch if batch else repo.get_trade_lifecycle_notification
    assert get(row[id_key]) == row


@pytest.mark.parametrize("batch", [False, True])
def test_reconcile_preview_preserves_all_outbox_state(tmp_path, batch):
    repo, row, current = _claimed(tmp_path, batch=batch)
    start, complete, _claim, id_key, _renderer_name = _operations(batch)
    identity = {id_key: row[id_key]}
    start(repo, **identity, claim_id=row["claim_id"], now_ms=current)
    unknown = complete(repo, **identity, claim_id=row["claim_id"], outcome="unknown", now_ms=current)
    before = (repo.list_trade_lifecycle_notifications(), repo.list_trade_lifecycle_notification_batches())
    reconcile = facade.reconcile_notification_batch if batch else facade.reconcile_unknown_notification
    preview = reconcile(repo, **identity, action="resend", broker_ref="fixture", note="fixture review", apply_changes=False)
    assert preview["current_status"] == unknown["status"] == "unknown"
    assert (repo.list_trade_lifecycle_notifications(), repo.list_trade_lifecycle_notification_batches()) == before


def test_original_explicit_retry_constant_import_is_preserved() -> None:
    from src.application.trades.lifecycle_outbox import RETRY_BACKOFF_MS

    assert RETRY_BACKOFF_MS is api.RETRY_BACKOFF_MS is owner.RETRY_BACKOFF_MS
    assert RETRY_BACKOFF_MS == (30 * 1000, 5 * 60 * 1000)
    assert "RETRY_BACKOFF_MS" not in facade.__all__
