from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from src.application.trades.auto_intake import (
    _cached_trade_inbox_summary,
)

from src.application.trades.inbox import (
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    mark_trade_payload_handled,
    mark_trade_payload_retryable,
    record_trade_payload_refresh_intent,
    settle_trade_payload_result,
    trade_inbox_revision,
    trade_inbox_summary,
)


def test_trade_inbox_claims_portfolio_refresh_intent_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "stock-1"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:stock-1",
    )
    intent = {"account": "lx", "request_id": "stock-refresh:abc"}

    record_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
        intent=intent,
    )
    assert claim_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
    ) == intent
    record_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
        intent=intent,
    )
    assert claim_trade_payload_refresh_intent(path, inbox_id=inbox_id) is None


def test_trade_inbox_is_idempotent_and_retries_callback_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    payload = {"deal_id": "deal-1", "code": "US.NVDA260821P100000"}

    first_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    second_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    assert first_id == second_id
    assert trade_inbox_summary(path)["pending_count"] == 1

    mark_trade_payload_retryable(
        path,
        inbox_id=first_id,
        error="RuntimeError: callback failed",
    )
    retry = list_retryable_trade_payloads(path, retry_delay_sec=0)
    assert len(retry) == 1
    assert retry[0]["attempt_count"] == 1
    assert retry[0]["payload"] == payload

    mark_trade_payload_handled(
        path,
        inbox_id=first_id,
        result={"status": "applied", "reason": "applied_open"},
    )
    third_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="backfill",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    assert third_id == first_id
    mark_trade_payload_handled(
        path,
        inbox_id=third_id,
        result={"status": "skipped", "reason": "duplicate"},
    )
    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []
    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["handled_count"] == 1
    assert summary["max_attempt_count"] == 2


def test_trade_inbox_handles_lifecycle_pending_after_evidence_acceptance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "deal-waiting"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-waiting",
    )

    settle_trade_payload_result(
        path,
        inbox_id=inbox_id,
        result={
            "status": "unresolved",
            "reason": "waiting_settlement_evidence",
            "diagnostics": {
                "retryable": True,
                "broker_evidence_accepted": True,
            },
        },
    )

    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []
    summary = trade_inbox_summary(path)
    assert summary["handled_count"] == 1
    assert summary["pending_count"] == 0


def test_trade_inbox_quarantines_missing_canonical_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    enqueue_trade_payload(
        path,
        payload={"deal_id": "raw-only"},
        source="push",
    )

    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["identity_needs_review_count"] == 1
    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []


def test_trade_inbox_scopes_same_deal_id_by_broker_account(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    lx_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "same-id"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:same-id",
    )
    sy_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "same-id"},
        source="push",
        broker_deal_key="futu:sy:REAL_2:same-id",
    )

    assert lx_id != sy_id
    assert trade_inbox_summary(path)["pending_count"] == 2


def test_trade_inbox_quarantines_same_key_economic_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    source_key = "futu:lx:REAL_1:stock-1"
    first = {
        "deal_id": "stock-1",
        "code": "US.NVDA",
        "trd_side": "BUY",
        "qty": 100,
        "price": "100",
        "trade_time_ms": 1_800_000_000_000,
    }
    enqueue_trade_payload(
        path,
        payload=first,
        source="push",
        broker_deal_key=source_key,
    )
    enqueue_trade_payload(
        path,
        payload={**first, "price": "100.01"},
        source="poll",
        broker_deal_key=source_key,
    )

    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["conflict_count"] == 1
    assert list_retryable_trade_payloads(
        path,
        retry_delay_sec=0,
    ) == []


def test_trade_inbox_summary_cache_is_revision_gated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.auto_intake as auto_intake

    path = tmp_path / "inbox.sqlite3"
    first_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "seed"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:seed",
    )
    mark_trade_payload_handled(
        path,
        inbox_id=first_id,
        result={"status": "applied", "reason": "seed"},
    )
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO trade_inbox (
              inbox_id, source, deal_id, broker_deal_key,
              identity_status, payload_json, economic_payload_hash,
              status, attempt_count, received_at_ms, updated_at_ms,
              last_error, result_status, result_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"historical-{index}",
                    "backfill",
                    f"historical-{index}",
                    f"futu:lx:REAL_1:historical-{index}",
                    "bound",
                    json.dumps({"deal_id": f"historical-{index}"}),
                    f"hash-{index}",
                    "handled",
                    0,
                    index + 1,
                    index + 1,
                    None,
                    "applied",
                    "historical",
                )
                for index in range(1_200)
            ],
        )

    summary_reads = 0
    original_summary = auto_intake.trade_inbox_summary

    def counted_summary(summary_path: Path) -> dict:
        nonlocal summary_reads
        summary_reads += 1
        return original_summary(summary_path)

    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_summary",
        counted_summary,
    )
    cache: dict = {}
    for _ in range(10):
        summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 1
    assert summary["handled_count"] == 1_201

    revision_before = trade_inbox_revision(path)
    pending_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "new"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:new",
    )
    assert trade_inbox_revision(path) == revision_before + 1
    summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 2
    assert summary["pending_count"] == 1

    duplicate_revision = trade_inbox_revision(path)
    assert enqueue_trade_payload(
        path,
        payload={"deal_id": "new"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:new",
    ) == pending_id
    assert trade_inbox_revision(path) == duplicate_revision
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 2

    mark_trade_payload_handled(
        path,
        inbox_id=pending_id,
        result={"status": "applied", "reason": "new"},
    )
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 3
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM trade_inbox WHERE inbox_id = ?",
            (first_id,),
        )
    final_summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 4
    assert final_summary["handled_count"] == 1_201

    cache.clear()
    stable_revision = trade_inbox_revision(path)
    racing_revisions = iter(
        (stable_revision, stable_revision + 1)
    )
    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_revision",
        lambda _path: next(racing_revisions),
    )
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 5
    assert cache == {}

    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_revision",
        lambda _path: stable_revision + 1,
    )
    _cached_trade_inbox_summary(path, cache=cache)
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 6
