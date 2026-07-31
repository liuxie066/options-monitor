from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.application.ledger.source_consumption import build_source_consumption_claim
from src.application.trades.state import load_trade_intake_state, write_trade_intake_state
from src.application.trades.state_reconcile import (
    preview_trade_intake_reconciliation_from_sqlite,
    reconcile_trade_intake_state,
)


class FakeRepo:
    def __init__(
        self,
        events: list[dict],
        *,
        lifecycle_cases: list[dict] | None = None,
        lifecycle_evidence: list[dict] | None = None,
        lifecycle_allocations: list[dict] | None = None,
        lifecycle_source_consumptions: list[dict] | None = None,
        lifecycle_timing_policies: list[dict] | None = None,
        position_lots: list[dict] | None = None,
        assigned_stock_events: list[dict] | None = None,
    ) -> None:
        self.events = events
        self.lifecycle_cases = list(lifecycle_cases or [])
        self.lifecycle_evidence = list(lifecycle_evidence or [])
        self.lifecycle_allocations = list(lifecycle_allocations or [])
        self.lifecycle_source_consumptions = list(
            lifecycle_source_consumptions or []
        )
        self.lifecycle_timing_policies = list(lifecycle_timing_policies or [])
        self.position_lots = list(position_lots or [])
        self.assigned_stock_events = list(assigned_stock_events or [])

    def list_trade_events(self) -> list[dict]:
        return list(self.events)

    def list_assigned_stock_events(self) -> list[dict]:
        return list(self.assigned_stock_events)

    def list_trade_lifecycle_cases(self) -> list[dict]:
        return list(self.lifecycle_cases)

    def list_trade_lifecycle_evidence(
        self,
        *,
        case_id: str | None = None,
        account: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        rows = list(self.lifecycle_evidence)
        if case_id:
            rows = [item for item in rows if str(item.get("case_id") or "") == str(case_id)]
        if account:
            rows = [item for item in rows if str(item.get("account") or "") == str(account)]
        if symbol:
            rows = [item for item in rows if str(item.get("symbol") or "") == str(symbol)]
        return rows

    def list_trade_lifecycle_source_consumptions(
        self,
        *,
        case_id: str | None = None,
    ) -> list[dict]:
        rows = list(self.lifecycle_source_consumptions)
        if case_id:
            rows = [
                item
                for item in rows
                if str(item.get("case_id") or "") == str(case_id)
            ]
        return rows

    def read_lifecycle_account_rows(self, *, account: str) -> dict:
        account_value = str(account or "").strip().lower()
        cases = [
            item
            for item in self.lifecycle_cases
            if str(item.get("account") or "").strip().lower() == account_value
        ]
        case_ids = {
            str(item.get("case_id") or "").strip()
            for item in cases
            if str(item.get("case_id") or "").strip()
        }
        evidence = [
            item
            for item in self.lifecycle_evidence
            if str(item.get("case_id") or "").strip() in case_ids
        ]
        received_at_ms_by_id = {
            str(item.get("evidence_id") or "").strip(): int(
                item.get("received_at_ms")
                or item.get("_ledger_created_at_ms")
                or 0
            )
            for item in evidence
            if str(item.get("evidence_id") or "").strip()
            and int(
                item.get("received_at_ms")
                or item.get("_ledger_created_at_ms")
                or 0
            )
            > 0
        }
        return {
            "account": account_value,
            "trade_events": list(self.events),
            "account_position_lots": [
                item
                for item in self.position_lots
                if str((item.get("fields") or {}).get("account") or "")
                .strip()
                .lower()
                == account_value
            ],
            "account_lifecycle_cases": cases,
            "account_lifecycle_evidence": evidence,
            "account_lifecycle_evidence_received_at_ms_by_id": received_at_ms_by_id,
            "account_lifecycle_allocations": [
                item
                for item in self.lifecycle_allocations
                if str(item.get("case_id") or "").strip() in case_ids
            ],
            "account_lifecycle_source_consumptions": [
                item
                for item in self.lifecycle_source_consumptions
                if str(item.get("case_id") or "").strip() in case_ids
            ],
            "account_lifecycle_timing_policies": [
                item
                for item in self.lifecycle_timing_policies
                if str(item.get("case_id") or "").strip() in case_ids
            ],
        }


def _position_lot(lot_id: str, *, account: str = "lx", contracts: int = 1) -> dict:
    return {
        "record_id": lot_id,
        "fields": {
            "account": account,
            "contracts": contracts,
            "original_contracts": contracts,
        },
    }



def test_readonly_sqlite_preview_reports_terminal_evidence_without_writing_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {
                "deal-close-1": {
                    "status": "failed",
                    "action": "close",
                    "account": "lx",
                },
                "deal-still-pending": {
                    "status": "unresolved",
                    "action": "close",
                    "account": "lx",
                },
            },
            "unresolved_deal_ids": {},
        },
    )
    original_state = state_path.read_bytes()
    ledger_path = tmp_path / "ledger.sqlite3"
    event = {
        "event_id": "broker-close-deal-close-1-lot-1",
        "event_type": "close",
        "account": "lx",
        "position_effect": "close",
        "target_lot_id": "lot-1",
        "raw_payload": {
            "source_deal_id": "deal-close-1",
            "record_id": "lot-1",
        },
    }
    with closing(sqlite3.connect(ledger_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE trade_events (
                    event_id TEXT PRIMARY KEY,
                    event_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO trade_events (event_id, event_json) VALUES (?, ?)",
                (event["event_id"], json.dumps(event)),
            )

    out = preview_trade_intake_reconciliation_from_sqlite(
        state_path=state_path,
        sqlite_path=ledger_path,
    )

    assert out == {
        "available": True,
        "reason": None,
        "terminal_evidence_found": True,
        "terminal_evidence_count": 1,
        "ignored_non_option_count": 0,
        "delegated_lifecycle_pending_count": 0,
        "delegated_lifecycle_pending_deal_ids": [],
        "stale_state_count": 1,
        "pending_before_count": 2,
        "pending_after_reconcile_count": 1,
        "actionable_pending_after_reconcile_count": 1,
    }
    assert state_path.read_bytes() == original_state


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


def test_reconcile_trade_intake_state_ignores_same_deal_id_for_different_account(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {
                "same-deal-id": {
                    "status": "failed",
                    "action": "open",
                    "account": "lx",
                    "reason": "projection_verification_failed",
                }
            },
            "unresolved_deal_ids": {},
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": "futu:sy:281756479859383817:same-deal-id",
                "event_type": "open",
                "account": "sy",
                "raw_payload": {
                    "source": "api",
                    "source_deal_id": "same-deal-id",
                    "futu_account_id": "281756479859383817",
                },
            }
        ]
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)
    state = load_trade_intake_state(state_path)

    assert out["actions"][0]["action"] == "keep_pending"
    assert "same-deal-id" in state["failed_deal_ids"]
    assert "same-deal-id" not in state["processed_deal_ids"]


def test_reconcile_trade_intake_state_uses_lifecycle_stock_settlement_source_event(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "8433576313500456302": {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "stock_settlement_waiting_option_leg",
                    "retryable": True,
                }
            },
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": "assignment-lot-futu-1",
                "event_type": "assignment",
                "account": "lx",
                "position_effect": "close",
                "target_lot_id": "lot-futu-1",
                "raw_payload": {
                    "record_id": "lot-futu-1",
                    "stock_settlement": {
                        "source_event_id": "8433576313500456302",
                        "side": "buy",
                        "shares": 100,
                        "price": 117.45,
                    },
                },
            }
        ]
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 1
    state = load_trade_intake_state(state_path)
    assert "8433576313500456302" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["8433576313500456302"]
    assert processed["reason"] == "ledger_event_already_recorded"
    assert processed["applied_record_ids"] == ["lot-futu-1"]


def test_reconcile_trade_intake_state_marks_assigned_stock_sale_event_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "6315806741161105994": {
                    "status": "unresolved",
                    "action": "assigned_stock_sale",
                    "account": "lx",
                    "reason": "ambiguous_assigned_stock_sale",
                    "retryable": False,
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        assigned_stock_events=[
            {
                "stock_event_id": "assigned-stock-sale-6315806741161105994",
                "source_deal_id": "6315806741161105994",
                "target_stock_lot_id": "assigned-stock-lot-a",
                "account": "lx",
                "symbol": "FUTU",
            }
        ],
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 1
    assert out["actions"][0]["reason"] == "assigned_stock_sale_event_recorded"
    state = load_trade_intake_state(state_path)
    assert "6315806741161105994" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["6315806741161105994"]
    assert processed["status"] == "reconciled"
    assert processed["action"] == "assigned_stock_sale"
    assert processed["reason"] == "assigned_stock_sale_event_recorded"
    assert processed["applied_record_ids"] == ["assigned-stock-lot-a"]
    assert processed["diagnostics"]["reconciled_assigned_stock_event_id"] == "assigned-stock-sale-6315806741161105994"
    assert processed["diagnostics"]["previous_reason"] == "ambiguous_assigned_stock_sale"


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


def test_reconcile_trade_intake_state_marks_completed_lifecycle_deal_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "3254612655429789712": {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                    "retryable": True,
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[
            {
                "case_id": "lc_futu_assignment",
                "status": "ledger_written",
                "decision_type": "assignment",
                "account": "lx",
                "symbol": "FUTU",
                "target_lot_ids": ["lot_manual-open-df078270b91449a1"],
            }
        ],
        lifecycle_evidence=[
            {
                "case_id": "lc_futu_assignment",
                "evidence_id": "ev_option_close",
                "evidence_type": "option_zero_price_close",
                "source_event_id": "3254612655429789712",
                "account": "lx",
                "symbol": "FUTU",
            }
        ],
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 1
    assert out["actions"][0]["reason"] == "lifecycle_case_already_recorded"
    state = load_trade_intake_state(state_path)
    assert "3254612655429789712" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["3254612655429789712"]
    assert processed["status"] == "reconciled"
    assert processed["action"] == "lifecycle"
    assert processed["reason"] == "lifecycle_case_already_recorded"
    assert processed["applied_record_ids"] == ["lot_manual-open-df078270b91449a1"]
    assert processed["diagnostics"]["reconciled_lifecycle_case_id"] == "lc_futu_assignment"
    assert processed["diagnostics"]["reconciled_lifecycle_decision_type"] == "assignment"
    assert processed["diagnostics"]["reconciled_lifecycle_evidence_id"] == "ev_option_close"


def test_reconcile_trade_intake_state_marks_expire_close_lifecycle_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "775828694842258876": {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                    "retryable": True,
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[
            {
                "case_id": "lc_0700_expire_close",
                "status": "ledger_written",
                "decision_type": "expire_close",
                "account": "lx",
                "symbol": "0700.HK",
                "target_lot_ids": ["lot_0700_440p"],
            }
        ],
        lifecycle_evidence=[
            {
                "case_id": "lc_0700_expire_close",
                "evidence_id": "ev_0700_option_zero",
                "evidence_type": "option_zero_price_close",
                "source_event_id": "775828694842258876",
                "account": "lx",
                "symbol": "0700.HK",
            }
        ],
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 1
    state = load_trade_intake_state(state_path)
    assert "775828694842258876" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["775828694842258876"]
    assert processed["status"] == "reconciled"
    assert processed["reason"] == "lifecycle_case_already_recorded"
    assert processed["applied_record_ids"] == ["lot_0700_440p"]
    assert processed["diagnostics"]["reconciled_lifecycle_decision_type"] == "expire_close"


def test_reconcile_trade_intake_state_dry_run_keeps_completed_lifecycle_file_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "deal-option-1": {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                    "retryable": True,
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[
            {
                "case_id": "lc_assignment_1",
                "status": "ledger_written",
                "decision_type": "assignment",
                "account": "lx",
                "symbol": "TIGR",
                "target_lot_ids": ["lot-1"],
            }
        ],
        lifecycle_evidence=[
            {
                "case_id": "lc_assignment_1",
                "evidence_id": "ev-option-1",
                "evidence_type": "option_zero_price_close",
                "source_event_id": "deal-option-1",
                "account": "lx",
                "symbol": "TIGR",
            }
        ],
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=False)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 0
    assert out["pending_after"]["unresolved_deal_ids"] == 0
    state = load_trade_intake_state(state_path)
    assert "deal-option-1" in state["unresolved_deal_ids"]
    assert "deal-option-1" not in state["processed_deal_ids"]


def test_reconcile_trade_intake_state_keeps_waiting_lifecycle_pending(tmp_path: Path) -> None:
    source_key = "futu:lx:1001:deal-option-waiting"
    lifecycle_case = {
        "schema_version": "lifecycle_case.v2",
        "case_id": "lc_waiting",
        "status": "waiting_settlement_evidence",
        "decision_type": "needs_review",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "target_contracts_by_lot": {"lot-futu": 1},
    }
    lifecycle_evidence = {
        "case_id": "lc_waiting",
        "evidence_id": "ev-option-waiting",
        "evidence_type": "option_zero_price_close",
        "source_event_id": source_key,
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "contracts": 1,
        "target_contracts_by_lot": {"lot-futu": 1},
        "price": "0",
        "event_time_ms": 1_700_000_000_100,
        "received_at_ms": 1_700_000_000_200,
    }
    source_claim = build_source_consumption_claim(
        source_key=source_key,
        case_id="lc_waiting",
        owner_evidence_id="ev-option-waiting",
        source_role="option_anchor",
        economic_payload=lifecycle_evidence,
    )
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                source_key: {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                    "retryable": True,
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[lifecycle_case],
        lifecycle_evidence=[lifecycle_evidence],
        lifecycle_source_consumptions=[source_claim],
        position_lots=[_position_lot("lot-futu")],
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert out["actions"][0]["reason"] == "lifecycle_pending_delegated"
    assert out["actions"][0]["lifecycle_case_id"] == "lc_waiting"
    assert out["actions"][0]["lifecycle_anchor_kind"] == "direct"
    state = load_trade_intake_state(state_path)
    assert source_key in state["unresolved_deal_ids"]


def test_reconcile_trade_intake_state_does_not_delegate_missing_target_manifest(
    tmp_path: Path,
) -> None:
    source_key = "futu:lx:1001:deal-option-missing-manifest"
    lifecycle_case = {
        "schema_version": "lifecycle_case.v2",
        "case_id": "lc_missing_manifest",
        "status": "waiting_settlement_evidence",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
    }
    lifecycle_evidence = {
        "case_id": "lc_missing_manifest",
        "evidence_id": "ev-missing-manifest",
        "evidence_type": "option_zero_price_close",
        "source_event_id": source_key,
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "contracts": 1,
        "target_contracts_by_lot": {"lot-futu": 1},
        "price": "0",
        "event_time_ms": 1_700_000_000_100,
        "received_at_ms": 1_700_000_000_200,
    }
    source_claim = build_source_consumption_claim(
        source_key=source_key,
        case_id="lc_missing_manifest",
        owner_evidence_id="ev-missing-manifest",
        source_role="option_anchor",
        economic_payload=lifecycle_evidence,
    )
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                source_key: {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[lifecycle_case],
        lifecycle_evidence=[lifecycle_evidence],
        lifecycle_source_consumptions=[source_claim],
        position_lots=[_position_lot("lot-futu")],
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=False,
    )

    assert out["planned_count"] == 0
    assert out["actions"][0]["reason"] == "no_reconciliation_evidence"


def test_reconcile_trade_intake_state_delegates_valid_migration_bridge(
    tmp_path: Path,
) -> None:
    source_key = "futu:lx:1001:deal-option-legacy"
    legacy_case = {
        "schema_version": "lifecycle_case.v1",
        "case_id": "lc_legacy",
        "status": "superseded",
        "superseded_by_case_id": "lc_canonical",
        "account": "lx",
        "symbol": "FUTU",
    }
    canonical_case = {
        "schema_version": "lifecycle_case.v2",
        "case_id": "lc_canonical",
        "status": "waiting_settlement_evidence",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "call",
        "position_side": "short",
        "strike": "550",
        "expiration_ymd": "2026-08-21",
        "target_contracts_by_lot": {"lot-futu": 1},
    }
    legacy_evidence = {
        "case_id": "lc_legacy",
        "evidence_id": "ev-legacy",
        "evidence_type": "option_zero_price_close",
        "source_event_id": "deal-option-legacy",
        "account": "lx",
        "symbol": "FUTU",
        "raw": {"price": "0"},
        "_ledger_created_at_ms": 1_700_000_000_200,
    }
    bridge = {
        "schema_version": "migration_bridge_evidence.v1",
        "case_id": "lc_canonical",
        "evidence_id": "ev-bridge",
        "evidence_type": "migration_bridge",
        "account": "lx",
        "symbol": "FUTU",
        "referenced_legacy_case_id": "lc_legacy",
        "referenced_legacy_evidence_id": "ev-legacy",
        "allocating": False,
    }
    source_claim = build_source_consumption_claim(
        source_key=source_key,
        case_id="lc_legacy",
        owner_evidence_id="ev-legacy",
        source_role="option_anchor",
        economic_payload={
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "FUTU",
            "option_type": "call",
            "position_side": "short",
            "strike": "550",
            "expiration_ymd": "2026-08-21",
            "contracts": 1,
            "price": "0",
            "event_time_ms": 1_700_000_000_100,
        },
    )
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                source_key: {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "lifecycle_case_futu_account_mismatch",
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[legacy_case, canonical_case],
        lifecycle_evidence=[legacy_evidence, bridge],
        lifecycle_source_consumptions=[source_claim],
        position_lots=[_position_lot("lot-futu")],
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=False,
    )

    assert out["planned_count"] == 0
    assert out["actions"][0]["reason"] == "lifecycle_pending_delegated"
    assert out["actions"][0]["lifecycle_case_id"] == "lc_canonical"
    assert out["actions"][0]["lifecycle_anchor_kind"] == "migration_bridge"


def test_reconcile_trade_intake_state_does_not_delegate_ambiguous_numeric_deal_id(
    tmp_path: Path,
) -> None:
    deal_id = "deal-option-ambiguous"
    cases: list[dict] = []
    evidence: list[dict] = []
    claims: list[dict] = []
    lots: list[dict] = []
    for index, futu_account_id in enumerate(("1001", "1002"), start=1):
        case_id = f"lc_ambiguous_{index}"
        lot_id = f"lot-futu-{index}"
        source_key = f"futu:lx:{futu_account_id}:{deal_id}"
        lifecycle_case = {
            "schema_version": "lifecycle_case.v2",
            "case_id": case_id,
            "status": "waiting_settlement_evidence",
            "account": "lx",
            "futu_account_id": futu_account_id,
            "symbol": "FUTU",
            "option_type": "put",
            "position_side": "short",
            "strike": "100",
            "expiration_ymd": "2026-08-21",
            "target_contracts_by_lot": {lot_id: 1},
        }
        lifecycle_evidence = {
            "case_id": case_id,
            "evidence_id": f"ev-ambiguous-{index}",
            "evidence_type": "option_zero_price_close",
            "source_event_id": source_key,
            "account": "lx",
            "futu_account_id": futu_account_id,
            "symbol": "FUTU",
            "option_type": "put",
            "position_side": "short",
            "strike": "100",
            "expiration_ymd": "2026-08-21",
            "contracts": 1,
            "target_contracts_by_lot": {lot_id: 1},
            "price": "0",
            "event_time_ms": 1_700_000_000_100 + index,
            "received_at_ms": 1_700_000_000_200 + index,
        }
        cases.append(lifecycle_case)
        evidence.append(lifecycle_evidence)
        claims.append(
            build_source_consumption_claim(
                source_key=source_key,
                case_id=case_id,
                owner_evidence_id=str(lifecycle_evidence["evidence_id"]),
                source_role="option_anchor",
                economic_payload=lifecycle_evidence,
            )
        )
        lots.append(_position_lot(lot_id))

    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                deal_id: {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "waiting_settlement_evidence",
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=cases,
        lifecycle_evidence=evidence,
        lifecycle_source_consumptions=claims,
        position_lots=lots,
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=False,
    )

    assert out["planned_count"] == 0
    assert out["actions"][0]["reason"] == "no_reconciliation_evidence"


def test_reconcile_trade_intake_state_rejects_invalid_migration_bridge(
    tmp_path: Path,
) -> None:
    source_key = "futu:lx:1001:deal-option-legacy"
    legacy_case = {
        "case_id": "lc_legacy",
        "status": "superseded",
        "superseded_by_case_id": "lc_canonical",
        "account": "lx",
        "symbol": "FUTU",
    }
    canonical_case = {
        "case_id": "lc_canonical",
        "status": "waiting_settlement_evidence",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "FUTU",
        "option_type": "put",
        "position_side": "short",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "target_contracts_by_lot": {"lot-futu": 1},
    }
    legacy_evidence = {
        "case_id": "lc_legacy",
        "evidence_id": "ev-legacy",
        "evidence_type": "option_zero_price_close",
        "source_event_id": "deal-option-legacy",
        "account": "lx",
        "symbol": "FUTU",
        "raw": {"price": "0"},
        "_ledger_created_at_ms": 1_700_000_000_200,
    }
    invalid_bridge = {
        "schema_version": "migration_bridge_evidence.v1",
        "case_id": "lc_canonical",
        "evidence_id": "ev-bridge",
        "evidence_type": "migration_bridge",
        "account": "lx",
        "symbol": "FUTU",
        "referenced_legacy_case_id": "lc_legacy",
        "referenced_legacy_evidence_id": "ev-legacy",
        "allocating": True,
    }
    source_claim = build_source_consumption_claim(
        source_key=source_key,
        case_id="lc_legacy",
        owner_evidence_id="ev-legacy",
        source_role="option_anchor",
        economic_payload={
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "FUTU",
            "option_type": "put",
            "position_side": "short",
            "strike": "100",
            "expiration_ymd": "2026-08-21",
            "contracts": 1,
            "price": "0",
            "event_time_ms": 1_700_000_000_100,
        },
    )
    state_path = tmp_path / "auto_trade_intake_state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                source_key: {
                    "status": "unresolved",
                    "action": "lifecycle",
                    "account": "lx",
                    "reason": "lifecycle_case_futu_account_mismatch",
                }
            },
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[legacy_case, canonical_case],
        lifecycle_evidence=[legacy_evidence, invalid_bridge],
        lifecycle_source_consumptions=[source_claim],
        position_lots=[_position_lot("lot-futu")],
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=False,
    )

    assert out["actions"][0]["reason"] == "no_reconciliation_evidence"


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


def test_reconcile_does_not_complete_deal_from_numeric_target_lot_lineage(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    opening_deal_id = "9162790356868244299"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {
                opening_deal_id: {
                    "status": "failed",
                    "action": "open",
                    "account": "lx",
                    "reason": "exception:RuntimeError",
                }
            },
            "unresolved_deal_ids": {},
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": (
                    "futu:lx:281756479859383816:495287541148725639:"
                    f"close:lot_futu:lx:281756479859383816:{opening_deal_id}"
                ),
                "event_type": "close",
                "account": "lx",
                "raw_payload": {"source_deal_id": "495287541148725639"},
            }
        ]
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=True,
    )

    assert out["planned_count"] == 0
    assert out["actions"][0]["reason"] == "no_reconciliation_evidence"
    assert opening_deal_id in load_trade_intake_state(state_path)["failed_deal_ids"]
