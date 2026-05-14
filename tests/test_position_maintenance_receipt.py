from __future__ import annotations

from pathlib import Path

import pytest

from src.application.position_maintenance_receipt import (
    build_auto_close_receipt_message,
    decide_auto_close_receipt,
    resolve_auto_close_receipt_config,
    send_auto_close_receipt,
)


def test_auto_close_receipt_decision_defaults_send_applied_and_failed() -> None:
    applied = decide_auto_close_receipt(
        receipt_config={},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
    )
    failed = decide_auto_close_receipt(
        receipt_config={},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 0, "candidates_should_close": 1, "errors": ["boom"]},
    )
    partial = decide_auto_close_receipt(
        receipt_config={},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 2, "errors": ["boom"]},
    )

    assert applied == {"should_send": True, "reason": "applied"}
    assert failed == {"should_send": True, "reason": "failed"}
    assert partial == {"should_send": True, "reason": "partial_failed"}


def test_auto_close_receipt_decision_skips_dry_run_and_noop_by_default() -> None:
    dry_run = decide_auto_close_receipt(
        receipt_config={},
        dry_run=True,
        result={"mode": "dry_run", "applied_closed": 0, "candidates_should_close": 1, "errors": []},
    )
    noop = decide_auto_close_receipt(
        receipt_config={},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 0, "candidates_should_close": 0, "errors": []},
    )

    assert dry_run == {"should_send": False, "reason": "dry_run"}
    assert noop == {"should_send": False, "reason": "noop"}


def test_auto_close_receipt_config_rejects_non_boolean_flag() -> None:
    with pytest.raises(ValueError, match="option_positions.auto_close.receipt.enabled must be a boolean"):
        resolve_auto_close_receipt_config({"enabled": "yes"})


def test_send_auto_close_receipt_skips_without_route(tmp_path: Path) -> None:
    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "openclaw", "channel": "openclaw-weixin"}},
        receipt_config={},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_no_route"


def test_send_auto_close_receipt_uses_existing_route_and_sender(tmp_path: Path) -> None:
    calls: list[dict] = []

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "msg-auto-1", "returncode": 0}

    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "openclaw", "channel": "openclaw-weixin", "target": "user:test"}},
        receipt_config={},
        dry_run=False,
        result={
            "mode": "applied",
            "account": "lx",
            "broker": "富途",
            "grace_days": 1,
            "as_of_utc": "2026-05-03T00:00:00+00:00",
            "applied_closed": 1,
            "candidates_should_close": 1,
            "errors": [],
            "applied": [{"record_id": "rec_1", "position_id": "pos_1", "expiration_ymd": "2026-05-01"}],
        },
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert out["delivery_confirmed"] is True
    assert out["message_id"] == "msg-auto-1"
    assert calls[0]["target"] == "user:test"
    assert "过期自动平仓已写入 option_positions" in calls[0]["message"]
    assert "账户：lx" in calls[0]["message"]
    assert "rec_1 | pos_1 | exp=2026-05-01" in calls[0]["message"]


def test_build_auto_close_receipt_message_marks_partial_failure() -> None:
    msg = build_auto_close_receipt_message(
        dry_run=False,
        result={
            "mode": "applied",
            "account": "lx",
            "broker": "富途",
            "grace_days": 2,
            "applied_closed": 1,
            "candidates_should_close": 2,
            "errors": ["rec_2 pos_2: sqlite locked"],
            "applied": [{"record_id": "rec_1", "position_id": "pos_1", "expiration_ymd": "2026-05-01"}],
        },
    )

    assert "[未完全记录]" in msg
    assert "平仓：1/2" in msg
    assert "sqlite locked" in msg
