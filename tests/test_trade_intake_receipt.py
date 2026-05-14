from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.application.trade_intake_receipt import (
    build_trade_intake_receipt_message,
    decide_trade_intake_receipt,
    send_trade_intake_receipt,
)


def test_receipt_decision_defaults_send_applied() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={},
        deal_id="deal-1",
        result={"status": "applied", "reason": "applied_open"},
    )

    assert out == {"should_send": True, "reason": "applied"}


def test_receipt_decision_defaults_send_unresolved_and_failed() -> None:
    for status in ("unresolved", "failed"):
        out = decide_trade_intake_receipt(
            receipt_config={},
            apply_changes=True,
            state={},
            deal_id="deal-1",
            result={"status": status, "reason": status},
        )

        assert out == {"should_send": True, "reason": status}


def test_receipt_decision_skips_dry_run() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=False,
        state={},
        deal_id="deal-1",
        result={"status": "dry_run", "reason": "preview_open"},
    )

    assert out == {"should_send": False, "reason": "skipped_dry_run"}


def test_receipt_decision_skips_confirmed_duplicate_by_default() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "processed_deal_ids": {
                "deal-1": {
                    "status": "applied",
                    "receipt": {"status": "sent", "delivery_confirmed": True},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "skipped", "reason": "duplicate_deal_id"},
    )

    assert out == {"should_send": False, "reason": "skipped_duplicate"}


def test_receipt_decision_retries_unconfirmed_duplicate() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "processed_deal_ids": {
                "deal-1": {
                    "status": "applied",
                    "receipt": {"status": "failed", "delivery_confirmed": False},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "skipped", "reason": "duplicate_deal_id"},
    )

    assert out == {"should_send": True, "reason": "duplicate_retry_unconfirmed_receipt"}


def test_send_trade_intake_receipt_skips_without_route(tmp_path: Path) -> None:
    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "openclaw", "channel": "openclaw-weixin"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=None,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1"},
        payload={"deal_id": "deal-1"},
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_no_route"


def test_send_trade_intake_receipt_uses_existing_route_and_sender(tmp_path: Path) -> None:
    calls: list[dict] = []
    deal = SimpleNamespace(
        deal_id="deal-1",
        internal_account="lx",
        position_effect="open",
        side="sell",
        symbol="NVDA",
        option_type="put",
        expiration_ymd="2026-06-19",
        strike=120,
        contracts=1,
        price=1.23,
    )

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "msg-1", "returncode": 0}

    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "openclaw", "channel": "openclaw-weixin", "target": "user:test"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=deal,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1", "account": "lx", "action": "open"},
        payload={},
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert out["delivery_confirmed"] is True
    assert out["message_id"] == "msg-1"
    assert calls[0]["target"] == "user:test"
    assert "成交已写入 option_positions" in calls[0]["message"]
    assert "deal_id：deal-1" in calls[0]["message"]


def test_build_trade_intake_receipt_message_marks_unresolved() -> None:
    msg = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "unresolved",
            "reason": "missing_required_fields:multiplier",
            "deal_id": "deal-1",
            "account": "lx",
            "action": "open",
        },
        payload={"symbol": "9992.HK", "qty": 1, "price": 6.3},
    )

    assert "[未记录]" in msg
    assert "missing_required_fields:multiplier" in msg
    assert "9992.HK" in msg
