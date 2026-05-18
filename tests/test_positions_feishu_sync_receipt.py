from __future__ import annotations

from pathlib import Path

import pytest

from src.application.positions.feishu_sync_receipt import (
    build_option_positions_feishu_sync_receipt_identity,
    build_option_positions_feishu_sync_receipt_message,
    decide_option_positions_feishu_sync_receipt,
    persist_option_positions_feishu_sync_receipt_state,
    resolve_option_positions_feishu_sync_receipt_config,
    send_option_positions_feishu_sync_receipt,
)


def _result(summary: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "mode": "apply",
        "status": "applied",
        "data_config_path": "/tmp/data.json",
        "table_ref_hash": "table-hash-1",
        "started_at": "2026-05-15T16:10:00+00:00",
        "finished_at": "2026-05-15T16:10:01+00:00",
        "filters": {"only_open": False, "prune_remote_missing_local": False},
        "summary": summary or {"scanned": 1, "create": 1, "update": 0, "delete": 0, "skip": 0, "conflict": 0, "failed": 0},
        "rows": [{"record_id": "lot_1", "action": "create", "reason": "missing_feishu_record_id"}],
    }


def test_feishu_sync_receipt_config_defaults_retry_unconfirmed() -> None:
    cfg = resolve_option_positions_feishu_sync_receipt_config({})

    assert cfg["enabled"] is True
    assert cfg["notify_applied"] is True
    assert cfg["notify_noop"] is False
    assert cfg["retry_unconfirmed"] is True


def test_feishu_sync_receipt_config_rejects_non_boolean_flag() -> None:
    with pytest.raises(ValueError, match="option_positions.sync_to_feishu.receipt.enabled must be a boolean"):
        resolve_option_positions_feishu_sync_receipt_config({"enabled": "yes"})


def test_feishu_sync_receipt_decision_defaults_send_applied_failed_and_conflict() -> None:
    applied = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result=_result({"scanned": 1, "create": 1, "update": 0, "delete": 0, "skip": 0, "conflict": 0, "failed": 0}),
    )
    failed = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result={
            **_result({"scanned": 1, "create": 0, "update": 0, "delete": 0, "skip": 0, "conflict": 0, "failed": 1}),
            "status": "failed",
        },
    )
    conflict = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result={
            **_result({"scanned": 1, "create": 0, "update": 0, "delete": 0, "skip": 0, "conflict": 1, "failed": 0}),
            "status": "conflict",
        },
    )
    noop = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result={
            **_result({"scanned": 1, "create": 0, "update": 0, "delete": 0, "skip": 1, "conflict": 0, "failed": 0}),
            "status": "noop",
        },
    )

    assert applied == {"should_send": True, "reason": "applied"}
    assert failed == {"should_send": True, "reason": "failed"}
    assert conflict == {"should_send": True, "reason": "conflict"}
    assert noop == {"should_send": False, "reason": "noop"}


def test_feishu_sync_receipt_decision_skips_confirmed_duplicate() -> None:
    result = _result()
    identity = build_option_positions_feishu_sync_receipt_identity(result=result)  # type: ignore[arg-type]

    out = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result=result,  # type: ignore[arg-type]
        prior_receipt={"status": "sent", "delivery_confirmed": True, "attempt_count": 1},
        receipt_key=identity["receipt_key"],
    )

    assert out == {"should_send": False, "reason": "skipped_duplicate_confirmed"}


def test_feishu_sync_receipt_decision_retries_unconfirmed_duplicate() -> None:
    out = decide_option_positions_feishu_sync_receipt(
        receipt_config={},
        dry_run=False,
        result=_result(),  # type: ignore[arg-type]
        prior_receipt={"status": "failed", "delivery_confirmed": False, "attempt_count": 2},
        receipt_key="receipt-key-1",
    )

    assert out == {"should_send": True, "reason": "applied_retry_unconfirmed_receipt"}


def test_send_feishu_sync_receipt_skips_without_route(tmp_path: Path) -> None:
    result = _result()
    identity = build_option_positions_feishu_sync_receipt_identity(result=result)  # type: ignore[arg-type]

    out = send_option_positions_feishu_sync_receipt(
        base=tmp_path,
        config={},
        receipt_config={},
        dry_run=False,
        result=result,  # type: ignore[arg-type]
        receipt_key=identity["receipt_key"],
        receipt_key_fields=identity["receipt_key_fields"],
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_no_route"
    assert out["decision_reason"] == "applied"
    assert out["receipt_key"] == identity["receipt_key"]


def test_send_feishu_sync_receipt_uses_existing_route_and_sender(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "msg-sync-1", "returncode": 0}

    out = send_option_positions_feishu_sync_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "openclaw", "channel": "openclaw-weixin", "target": "user:test"}},
        receipt_config={},
        dry_run=False,
        result=_result(),  # type: ignore[arg-type]
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert out["attempt_count"] == 1
    assert calls
    assert "option_positions 已同步到 Feishu" in str(calls[0]["message"])


def test_persist_feishu_sync_receipt_state_keeps_business_date_key(tmp_path: Path) -> None:
    result = _result()
    identity = build_option_positions_feishu_sync_receipt_identity(result=result)  # type: ignore[arg-type]
    receipt = {
        "status": "sent",
        "reason": "applied",
        "delivery_confirmed": True,
        "message_id": "msg-sync-1",
        "attempt_count": 1,
        **identity,
    }

    state = persist_option_positions_feishu_sync_receipt_state(base=tmp_path, result=result, receipt=receipt)  # type: ignore[arg-type]

    assert state is not None
    item = state["receipts"][identity["receipt_key"]]
    assert item["receipt"]["message_id"] == "msg-sync-1"
    assert item["receipt_key_fields"]["business_date"] == "2026-05-16"


def test_build_feishu_sync_receipt_message_includes_failure_rows() -> None:
    msg = build_option_positions_feishu_sync_receipt_message(
        dry_run=False,
        result={
            **_result({"scanned": 1, "create": 0, "update": 0, "delete": 0, "skip": 0, "conflict": 0, "failed": 1}),
            "status": "failed",
            "rows": [{"record_id": "lot_1", "action": "failed", "reason": "update_failed: denied"}],
        },  # type: ignore[arg-type]
    )

    assert "[异常]" in msg
    assert "failed=1" in msg
    assert "lot_1 | update_failed: denied" in msg
