from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.application.trades import auto_intake


class _OutboxRepo:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = dict(rows or {})
        self.reads: list[str] = []

    def get_trade_lifecycle_notification(
        self,
        outbox_id: str,
    ) -> dict[str, Any] | None:
        self.reads.append(outbox_id)
        row = self.rows.get(outbox_id)
        return dict(row) if isinstance(row, dict) else None


def _context(
    *,
    result: dict[str, Any],
    position_effect: str,
) -> dict[str, Any]:
    return {
        "apply_changes": True,
        "state": {},
        "deal": SimpleNamespace(
            deal_id="deal-1",
            position_effect=position_effect,
        ),
        "result": result,
        "effective_payload": {"deal_id": "deal-1"},
    }


def test_open_receipt_uses_direct_intake_sender_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def _send_trade_intake_receipt(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "status": "sent",
            "delivery_confirmed": True,
            "message_id": "msg-1",
        }

    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        _send_trade_intake_receipt,
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path,
        cfg={"notifications": {"target": "wechat:ops"}},
        receipt_config={"enabled": True},
        repo=_OutboxRepo(),
    )

    out = callback(
        _context(
            result={
                "status": "applied",
                "action": "open",
                "reason": "applied_open",
                "deal_id": "deal-1",
                "operations": [
                    {
                        "action": "open",
                        "result": {"event_id": "event-1", "created": True},
                    }
                ],
            },
            position_effect="open",
        )
    )

    assert out["status"] == "sent"
    assert len(calls) == 1
    assert calls[0]["result"]["action"] == "open"
    assert calls[0]["payload"] == {"deal_id": "deal-1"}


def test_close_receipt_is_outbox_managed_only_after_id_readback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _OutboxRepo(
        {
            "outbox-1": {
                "outbox_id": "outbox-1",
                "status": "pending",
                "provider_message_id": None,
            }
        }
    )
    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("lifecycle close must not send a direct receipt")
        ),
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path,
        cfg={},
        receipt_config={"enabled": True},
        repo=repo,
    )

    out = callback(
        _context(
            result={
                "status": "applied",
                "action": "close",
                "reason": "applied_close",
                "operations": [
                    {
                        "action": "close",
                        "result": {
                            "notification_outbox_id": "outbox-1",
                            "notification_outbox_created": True,
                        },
                    }
                ],
            },
            position_effect="close",
        )
    )

    assert out["status"] == "outbox_managed"
    assert out["outbox_id"] == "outbox-1"
    assert out["outbox_readback_confirmed"] is True
    assert out["delivery_confirmed"] is False
    assert repo.reads == ["outbox-1"]
    assert auto_intake._receipt_summary(out) == {
        "status": "outbox_managed",
        "reason": "transactional_outbox",
        "delivery_confirmed": False,
        "message_id": None,
        "error_code": None,
        "outbox_id": "outbox-1",
        "outbox_readback_confirmed": True,
    }


def test_claimed_outbox_id_without_readback_is_not_outbox_managed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing outbox readback must fail closed")
        ),
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path,
        cfg={},
        receipt_config={"enabled": True},
        repo=_OutboxRepo(),
    )

    out = callback(
        _context(
            result={
                "status": "applied",
                "action": "close",
                "operations": [
                    {
                        "action": "close",
                        "result": {
                            "notification_outbox_id": "outbox-missing",
                            "notification_outbox_created": True,
                        },
                    }
                ],
            },
            position_effect="close",
        )
    )

    assert out["status"] == "failed"
    assert out["reason"] == "lifecycle_outbox_readback_missing"
    assert out["missing_outbox_ids"] == ["outbox-missing"]


def test_lifecycle_waiting_state_without_outbox_does_not_direct_send(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("lifecycle state is owned by the outbox")
        ),
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path,
        cfg={},
        receipt_config={"enabled": True},
        repo=_OutboxRepo(),
    )

    out = callback(
        _context(
            result={
                "status": "unresolved",
                "action": "lifecycle",
                "reason": "waiting_settlement_evidence",
                "operations": [],
                "diagnostics": {
                    "notification_authority": "lifecycle_outbox"
                },
            },
            position_effect="close",
        )
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "lifecycle_outbox_not_created"


def test_duplicate_close_without_current_outbox_does_not_direct_send(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate close remains lifecycle-outbox owned")
        ),
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path,
        cfg={},
        receipt_config={"enabled": True},
        repo=_OutboxRepo(),
    )

    out = callback(
        _context(
            result={
                "status": "skipped",
                "action": None,
                "reason": "duplicate_deal_id",
                "operations": [],
            },
            position_effect="close",
        )
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_duplicate_lifecycle_outbox_owned"
