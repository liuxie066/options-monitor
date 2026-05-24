from __future__ import annotations

import json
from pathlib import Path

from src.application.trades.state import load_trade_intake_state, write_trade_intake_state
from src.application.trades.state_reconcile import reconcile_trade_intake_state


class FakeRepo:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def list_trade_events(self) -> list[dict]:
        return list(self.events)


def test_reconcile_trade_intake_state_dry_run_keeps_file_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {
                "deal-close-1": {"status": "failed", "action": "close", "account": "lx", "reason": "exception:LedgerPreflightError"}
            },
            "unresolved_deal_ids": {},
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": "broker-expire-close-deal-close-1-lot-1",
                "event_type": "expire_close",
                "account": "lx",
                "position_effect": "close",
                "target_lot_id": "lot-1",
                "raw_payload": {"source_deal_id": "deal-close-1", "record_id": "lot-1"},
            }
        ]
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=False)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 0
    assert out["pending_after"]["failed_deal_ids"] == 0
    assert out["actions"][0]["reason"] == "ledger_event_already_recorded"
    state = load_trade_intake_state(state_path)
    assert "deal-close-1" in state["failed_deal_ids"]


def test_reconcile_trade_intake_state_marks_ledger_recorded_failed_deal_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {
                "5646137975909129735": {
                    "status": "failed",
                    "action": "close",
                    "account": "lx",
                    "reason": "exception:LedgerPreflightError",
                }
            },
            "unresolved_deal_ids": {},
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": "broker-expire-close-5646137975909129735-lot_manual-open-b36a7f9d4bdc7aa9",
                "event_type": "expire_close",
                "account": "lx",
                "position_effect": "close",
                "target_lot_id": "lot_manual-open-b36a7f9d4bdc7aa9",
                "raw_payload": {
                    "source_deal_id": "5646137975909129735",
                    "record_id": "lot_manual-open-b36a7f9d4bdc7aa9",
                    "broker_close_type": "expiration_zero_close",
                },
            }
        ]
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 1
    assert out["backup_path"]
    state = load_trade_intake_state(state_path)
    assert "5646137975909129735" not in state["failed_deal_ids"]
    processed = state["processed_deal_ids"]["5646137975909129735"]
    assert processed["status"] == "reconciled"
    assert processed["reason"] == "ledger_event_already_recorded"
    assert processed["applied_record_ids"] == ["lot_manual-open-b36a7f9d4bdc7aa9"]
    assert processed["diagnostics"]["reconciled_ledger_event_type"] == "expire_close"


def test_reconcile_trade_intake_state_marks_ignored_non_option_unresolved_deal_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    audit_path = tmp_path / "auto_trade_intake_audit.jsonl"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "4246552780115108684": {
                    "status": "unresolved",
                    "action": None,
                    "account": "lx",
                    "reason": "not_option_deal",
                    "retryable": False,
                }
            },
        },
    )
    audit_path.write_text(
        json.dumps(
            {
                "phase": "resolved",
                "deal_id": "4246552780115108684",
                "result": {"status": "unresolved", "reason": "not_option_deal"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out = reconcile_trade_intake_state(state_path=state_path, audit_path=audit_path, repo=FakeRepo([]), apply_changes=True)

    assert out["planned_count"] == 1
    state = load_trade_intake_state(state_path)
    assert "4246552780115108684" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["4246552780115108684"]
    assert processed["status"] == "skipped"
    assert processed["reason"] == "not_option_deal"


def test_reconcile_trade_intake_state_keeps_pending_without_evidence(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {"deal-failed-1": {"status": "failed", "reason": "exception:RuntimeError"}},
            "unresolved_deal_ids": {},
        },
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=FakeRepo([]), apply_changes=True)

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert out["actions"][0]["action"] == "keep_pending"
    assert "deal-failed-1" in load_trade_intake_state(state_path)["failed_deal_ids"]
