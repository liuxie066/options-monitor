from __future__ import annotations

from pathlib import Path

from src.application.trades.inbox import (
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    mark_trade_payload_handled,
    mark_trade_payload_retryable,
    settle_trade_payload_result,
    trade_inbox_summary,
)


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
