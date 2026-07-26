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

    first_id = enqueue_trade_payload(path, payload=payload, source="push")
    second_id = enqueue_trade_payload(path, payload=payload, source="push")
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
    third_id = enqueue_trade_payload(path, payload=payload, source="backfill")
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


def test_trade_inbox_keeps_retryable_unresolved_result_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "deal-waiting"},
        source="push",
    )

    settle_trade_payload_result(
        path,
        inbox_id=inbox_id,
        result={
            "status": "unresolved",
            "reason": "waiting_settlement_evidence",
            "diagnostics": {"retryable": True},
        },
    )

    rows = list_retryable_trade_payloads(path, retry_delay_sec=0)
    assert len(rows) == 1
    assert rows[0]["deal_id"] == "deal-waiting"
    assert rows[0]["attempt_count"] == 1
    assert rows[0]["last_error"] == ""
