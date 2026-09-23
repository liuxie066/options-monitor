from __future__ import annotations

from tests.ledger_sqlite_test_support import connect_ledger_fixture

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.source_consumption import build_source_consumption_claim
from src.application.trades.state import (
    load_trade_intake_state,
    update_trade_intake_state_entries,
    compare_and_update_trade_intake_state_entries,
    write_trade_intake_state,
)
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


def _write_state(
    state_path,
    *,
    processed: dict | None = None,
    failed: dict | None = None,
    unresolved: dict | None = None,
) -> None:
    """Write the three intake-state buckets, defaulting the ones a case omits."""
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": dict(processed or {}),
            "failed_deal_ids": dict(failed or {}),
            "unresolved_deal_ids": dict(unresolved or {}),
        },
    )


def test_readonly_sqlite_preview_reports_terminal_evidence_without_writing_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        failed={
            "futu:lx:1001:deal-close-1": {
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
            "futu_account_id": "1001",
            "record_id": "lot-1",
        },
    }
    with closing(connect_ledger_fixture(ledger_path)) as conn:
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


def test_readonly_sqlite_preview_delegates_canonical_lifecycle_pending(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    source_key = "futu:lx:1001:deal-option-waiting"
    _write_state(
        state_path,
        unresolved={
            source_key: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
            }
        },
    )
    original_state = state_path.read_bytes()
    ledger_path = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
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
    }
    source_claim = build_source_consumption_claim(
        source_key=source_key,
        case_id="lc_waiting",
        owner_evidence_id="ev-option-waiting",
        source_role="option_anchor",
        economic_payload=lifecycle_evidence,
    )
    with closing(connect_ledger_fixture(ledger_path)) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO position_lots (
                    lot_id, account, fields_json, source_event_id,
                    strike, multiplier, updated_at_ms
                ) VALUES (?, 'lx', ?, ?, 100, 100, ?)
                """,
                (
                    "lot-futu",
                    json.dumps(
                        {
                            "lot_id": "lot-futu",
                            "open_event_id": source_key,
                            "contract_key": {"account": "lx"},
                            "contracts_opened": 1,
                            "contracts_open": 1,
                            "asset_type": "option",
                        }
                    ),
                    source_key,
                    1_700_000_000_000,
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                    case_id, case_key, account, symbol, option_type,
                    position_side, strike, expiration_ymd, status,
                    decision_type, target_lot_ids_json, created_at_ms,
                    updated_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "lc_waiting",
                    "case-key-waiting",
                    "lx",
                    "FUTU",
                    "put",
                    "short",
                    100,
                    "2026-08-21",
                    "waiting_settlement_evidence",
                    "needs_review",
                    json.dumps(["lot-futu"]),
                    1_700_000_000_000,
                    1_700_000_000_000,
                    json.dumps(lifecycle_case),
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                    evidence_id, case_id, source_type, source_event_id,
                    evidence_type, account, symbol, raw_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ev-option-waiting",
                    "lc_waiting",
                    "futu",
                    source_key,
                    "option_zero_price_close",
                    "lx",
                    "FUTU",
                    json.dumps(lifecycle_evidence),
                    1_700_000_000_200,
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_lifecycle_source_consumptions (
                    source_key, case_id, owner_evidence_id, source_role,
                    source_payload_hash, created_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    "lc_waiting",
                    "ev-option-waiting",
                    "option_anchor",
                    source_claim["source_payload_hash"],
                    1_700_000_000_200,
                    json.dumps(source_claim),
                ),
            )

    out = preview_trade_intake_reconciliation_from_sqlite(
        state_path=state_path,
        sqlite_path=ledger_path,
    )

    assert out["available"] is True
    assert out["delegated_lifecycle_pending_count"] == 1
    assert out["delegated_lifecycle_pending_deal_ids"] == [source_key]
    assert out["pending_after_reconcile_count"] == 1
    assert out["actionable_pending_after_reconcile_count"] == 0
    assert state_path.read_bytes() == original_state

    with closing(connect_ledger_fixture(ledger_path)) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="lifecycle case JSON is invalid",
        ), conn:
            conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                    case_id, case_key, account, symbol, option_type,
                    position_side, strike, expiration_ymd, status,
                    decision_type, target_lot_ids_json, created_at_ms,
                    updated_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "lc_corrupt_competing",
                    "case-key-corrupt-competing",
                    "lx",
                    "FUTU",
                    "put",
                    "short",
                    100,
                    "2026-08-21",
                    "waiting_settlement_evidence",
                    "needs_review",
                    json.dumps(["lot-futu"]),
                    1_700_000_000_300,
                    1_700_000_000_300,
                    "{",
                ),
            )

    out = preview_trade_intake_reconciliation_from_sqlite(
        state_path=state_path,
        sqlite_path=ledger_path,
    )
    assert out["available"] is True
    assert out["delegated_lifecycle_pending_count"] == 1
    assert state_path.read_bytes() == original_state


def test_reconcile_trade_intake_state_dry_run_keeps_file_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        failed={
            "futu:lx:1001:deal-close-1": {"status": "failed", "action": "close", "account": "lx", "reason": "exception:LedgerPreflightError"}
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
                "raw_payload": {"source_deal_id": "deal-close-1", "futu_account_id": "1001", "record_id": "lot-1"},
            }
        ]
    )

    out = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=False)

    assert out["planned_count"] == 1
    assert out["applied_count"] == 0
    assert out["pending_after"]["failed_deal_ids"] == 0
    assert out["actions"][0]["reason"] == "ledger_event_already_recorded"
    state = load_trade_intake_state(state_path)
    assert "futu:lx:1001:deal-close-1" in state["failed_deal_ids"]


def test_reconcile_trade_intake_state_marks_ledger_recorded_failed_deal_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        failed={
            "futu:lx:1001:5646137975909129735": {
                "status": "failed",
                "action": "close",
                "account": "lx",
                "reason": "exception:LedgerPreflightError",
            }
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
                    "futu_account_id": "1001",
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
    assert "futu:lx:1001:5646137975909129735" not in state["failed_deal_ids"]
    processed = state["processed_deal_ids"]["futu:lx:1001:5646137975909129735"]
    assert processed["status"] == "reconciled"
    assert processed["reason"] == "ledger_event_already_recorded"
    assert processed["applied_record_ids"] == ["lot_manual-open-b36a7f9d4bdc7aa9"]
    assert processed["diagnostics"]["reconciled_ledger_event_type"] == "expire_close"


def test_reconcile_preserves_concurrent_unrelated_deal_state(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    stale_key = "futu:lx:1001:stale"
    concurrent_key = "futu:lx:1001:concurrent"
    _write_state(
        state_path,
        failed={
            stale_key: {
                "status": "failed",
                "action": "close",
                "account": "lx",
            }
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": "broker-expire-close-stale-lot-1",
                "event_type": "expire_close",
                "account": "lx",
                "position_effect": "close",
                "target_lot_id": "lot-1",
                "raw_payload": {
                    "source_deal_id": "stale",
                    "futu_account_id": "1001",
                    "record_id": "lot-1",
                },
            }
        ]
    )

    def interleaved_update(path, state, *, deal_ids, expected_state):
        concurrent = load_trade_intake_state(path)
        concurrent["processed_deal_ids"][concurrent_key] = {
            "status": "applied",
            "action": "open",
            "account": "lx",
        }
        update_trade_intake_state_entries(
            path,
            concurrent,
            deal_ids=[concurrent_key],
        )
        return compare_and_update_trade_intake_state_entries(
            path, state, deal_ids=deal_ids, expected_state=expected_state,
        )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=True,
        update_state_fn=interleaved_update,
    )

    state = load_trade_intake_state(state_path)
    assert stale_key in state["processed_deal_ids"]
    assert concurrent_key in state["processed_deal_ids"]
    assert out["pending_after"]["processed_deal_ids"] == 2


def test_reconcile_trade_intake_state_ignores_same_deal_id_for_different_account(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        failed={
            "same-deal-id": {
                "status": "failed",
                "action": "open",
                "account": "lx",
                "reason": "projection_verification_failed",
            }
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


@pytest.mark.parametrize("event_kind", ["option", "assigned_stock_sale"])
@pytest.mark.parametrize("ledger_physical_account", [None, "1001"])
def test_reconcile_bare_deal_id_without_physical_scope_stays_pending(
    tmp_path: Path, event_kind: str, ledger_physical_account: str | None,
) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(state_path, {
        "processed_deal_ids": {}, "unresolved_deal_ids": {},
        "failed_deal_ids": {"same-deal": {"status": "failed", "account": "lx"}},
    })
    evidence = {"account": "lx", "source_deal_id": "same-deal"}
    if ledger_physical_account:
        evidence["futu_account_id"] = ledger_physical_account
    repo = FakeRepo(
        [{"event_id": "open-1", "event_type": "open", "account": "lx", "raw_payload": evidence}]
        if event_kind == "option" else [],
        assigned_stock_events=[{**evidence, "stock_event_id": "sale-1"}]
        if event_kind == "assigned_stock_sale" else [],
    )

    result = reconcile_trade_intake_state(state_path=state_path, repo=repo, apply_changes=True)
    assert result["planned_count"] == 0
    assert result["actions"][0]["action"] == "keep_pending"
    assert "same-deal" in load_trade_intake_state(state_path)["failed_deal_ids"]


def test_reconcile_trade_intake_state_uses_lifecycle_stock_settlement_source_event(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            "futu:lx:1001:8433576313500456302": {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "stock_settlement_waiting_option_leg",
                "retryable": True,
            }
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
                    "futu_account_id": "1001",
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
    assert "futu:lx:1001:8433576313500456302" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["futu:lx:1001:8433576313500456302"]
    assert processed["reason"] == "ledger_event_already_recorded"
    assert processed["applied_record_ids"] == ["lot-futu-1"]


def test_reconcile_trade_intake_state_marks_assigned_stock_sale_event_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            "futu:lx:1001:6315806741161105994": {
                "status": "unresolved",
                "action": "assigned_stock_sale",
                "account": "lx",
                "reason": "ambiguous_assigned_stock_sale",
                "retryable": False,
            }
        },
    )
    repo = FakeRepo(
        [],
        assigned_stock_events=[
            {
                "stock_event_id": "assigned-stock-sale-6315806741161105994",
                "source_deal_id": "6315806741161105994",
                "futu_account_id": "1001",
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
    assert "futu:lx:1001:6315806741161105994" not in state["unresolved_deal_ids"]
    processed = state["processed_deal_ids"]["futu:lx:1001:6315806741161105994"]
    assert processed["status"] == "reconciled"
    assert processed["action"] == "assigned_stock_sale"
    assert processed["reason"] == "assigned_stock_sale_event_recorded"
    assert processed["applied_record_ids"] == ["assigned-stock-lot-a"]
    assert processed["diagnostics"]["reconciled_assigned_stock_event_id"] == "assigned-stock-sale-6315806741161105994"
    assert processed["diagnostics"]["previous_reason"] == "ambiguous_assigned_stock_sale"


def test_reconcile_trade_intake_state_marks_ignored_non_option_unresolved_deal_processed(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    audit_path = tmp_path / "auto_trade_intake_audit.jsonl"
    _write_state(
        state_path,
        unresolved={
            "4246552780115108684": {
                "status": "unresolved",
                "action": None,
                "account": "lx",
                "reason": "not_option_deal",
                "retryable": False,
            }
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


def test_reconcile_trade_intake_state_rejects_status_only_assignment(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            "3254612655429789712": {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
                "retryable": True,
            }
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

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"]


def test_reconcile_trade_intake_state_rejects_status_only_expiry(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            "775828694842258876": {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
                "retryable": True,
            }
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

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"]


def test_reconcile_trade_intake_state_rejects_cached_terminal_summary(tmp_path: Path) -> None:
    deal_id = "futu:lx:100000000000000001:2000000000000000001"
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            deal_id: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
            }
        },
    )
    repo = FakeRepo(
        [],
        lifecycle_cases=[
            {
                "schema_version": "lifecycle_case.v2",
                "case_id": "lc_0700_v2_expire_close",
                "status": "ledger_written",
                "decision_type": None,
                "account": "lx",
                "symbol": "0700.HK",
                "target_contracts_by_lot": {"lot-put-a": 1, "lot-put-b": 1},
                "derived_summary": {
                    "resolved_contracts_by_terminal_type": {"expire_close": 2},
                    "resolved_contracts_by_lot": {"lot-put-a": 1, "lot-put-b": 1},
                },
            }
        ],
        lifecycle_evidence=[
            {
                "case_id": "lc_0700_v2_expire_close",
                "evidence_id": "ev_0700_v2_option_zero",
                "evidence_type": "expire_close",
                "source_event_id": "observation_0700_v2_terminal",
                "account": "lx",
                "symbol": "0700.HK",
                "observation": {
                    "anchor_option_deal_key": deal_id,
                    "complete": True,
                },
            }
        ],
    )

    out = reconcile_trade_intake_state(
        state_path=state_path,
        repo=repo,
        apply_changes=True,
    )

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"]


def test_reconcile_trade_intake_state_dry_run_keeps_completed_lifecycle_file_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "auto_trade_intake_state.json"
    _write_state(
        state_path,
        unresolved={
            "deal-option-1": {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
                "retryable": True,
            }
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

    assert out["planned_count"] == 0
    assert out["applied_count"] == 0
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"]


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
    _write_state(
        state_path,
        unresolved={
            source_key: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
                "retryable": True,
            }
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
    _write_state(
        state_path,
        unresolved={
            source_key: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
            }
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
    _write_state(
        state_path,
        unresolved={
            source_key: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "lifecycle_case_futu_account_mismatch",
            }
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
    _write_state(
        state_path,
        unresolved={
            deal_id: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "waiting_settlement_evidence",
            }
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
    _write_state(
        state_path,
        unresolved={
            source_key: {
                "status": "unresolved",
                "action": "lifecycle",
                "account": "lx",
                "reason": "lifecycle_case_futu_account_mismatch",
            }
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
    _write_state(
        state_path,
        failed={"deal-failed-1": {"status": "failed", "reason": "exception:RuntimeError"}},
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
    _write_state(
        state_path,
        failed={
            opening_deal_id: {
                "status": "failed",
                "action": "open",
                "account": "lx",
                "reason": "exception:RuntimeError",
            }
        },
    )
    repo = FakeRepo(
        [
            {
                "event_id": (
                    "futu:lx:999000000000000001:495287541148725639:"
                    f"close:lot_futu:lx:999000000000000001:{opening_deal_id}"
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


def _completed_canonical_repo(*, bridge=False, terminal_type="expire_close"):
    from domain.domain.lifecycle_allocation import allocation_id_for, terminal_event_id_for

    case = {
        "schema_version": "lifecycle_case.v2", "case_id": "canonical",
        "status": "ledger_written", "account": "lx", "futu_account_id": "1001",
        "broker": "富途", "symbol": "FUTU", "option_type": "put",
        "position_side": "short", "strike": "120", "multiplier": 100,
        "expiration_ymd": "2026-08-21", "target_contracts_by_lot": {"lot-1": 1},
    }
    source_key = "futu:lx:1001:option-1"
    owner_case = "legacy" if bridge else "canonical"
    evidence = {
        "case_id": owner_case, "evidence_id": "anchor", "evidence_type": "option_zero_price_close",
        "source_event_id": source_key, "account": "lx", "symbol": "FUTU",
        "raw": {"price": "0"}, "contracts": 1,
        "target_contracts_by_lot": {"lot-1": 1}, "_ledger_created_at_ms": 1_700_000_000_200,
    }
    payload = {**case, "contracts": 1, "price": "0", "side": "buy", "event_time_ms": 1_700_000_000_100}
    claim = build_source_consumption_claim(source_key=source_key, case_id=owner_case,
        owner_evidence_id="anchor", source_role="option_anchor", economic_payload=payload)
    allocation = {
        "case_id": "canonical", "evidence_id": "anchor", "target_lot_id": "lot-1",
        "terminal_type": terminal_type, "contracts_allocated": 1,
        "allocation_id": allocation_id_for(case_id="canonical", evidence_id="anchor", target_lot_id="lot-1"),
        "canonical_terminal_event_id": terminal_event_id_for(case_id="canonical", evidence_id="anchor", target_lot_id="lot-1", terminal_type=terminal_type, contracts_allocated=1),
    }
    event = {**case, "event_id": allocation["canonical_terminal_event_id"],
        "event_type": terminal_type, "target_lot_id": "lot-1", "contracts": 1, "price": 0,
        "event_time_ms": 1_700_000_001_000, "source": "lifecycle", "currency": "USD",
        "contract_key": {"account": "lx", "broker": "富途", "underlying_symbol": "FUTU",
            "option_type": "put", "position_side": "short", "strike": 120, "expiration_ymd": "2026-08-21"},
        "raw_payload": {"case_id": "canonical", "evidence_id": "anchor", "target_lot_id": "lot-1", "allocation_id": allocation["allocation_id"]}}
    cases, evidences = [case], [evidence]
    if bridge:
        cases.append({"schema_version": "lifecycle_case.v1", "case_id": "legacy", "status": "superseded",
            "superseded_by_case_id": "canonical", "account": "lx", "symbol": "FUTU"})
        evidences.append({"schema_version": "migration_bridge_evidence.v1", "case_id": "canonical",
            "evidence_id": "bridge", "evidence_type": "migration_bridge", "account": "lx", "symbol": "FUTU",
            "referenced_legacy_case_id": "legacy", "referenced_legacy_evidence_id": "anchor", "allocating": False})
    return FakeRepo([event], lifecycle_cases=cases, lifecycle_evidence=evidences,
        lifecycle_allocations=[allocation], lifecycle_source_consumptions=[claim], position_lots=[_position_lot("lot-1")])


@pytest.mark.parametrize("bridge", [False, True])
def test_canonical_completion_uses_active_allocations_and_preserves_receipts(tmp_path, bridge):
    repo = _completed_canonical_repo(bridge=bridge)
    key = "futu:lx:1001:option-1"
    path = tmp_path / "state.json"
    entry = {"account": "lx", "status": "unresolved", "action": "lifecycle",
        "futu_account_id": "1001", "source_deal_id": "option-1", "receipt": {"status": "sent"},
        "economic_payload_hash": "historical-hash"}
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: entry}})
    before = path.read_bytes()
    preview = reconcile_trade_intake_state(state_path=path, repo=repo)
    assert preview["planned_count"] == 1 and path.read_bytes() == before
    result = reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)
    assert result["applied_count"] == 1
    processed = load_trade_intake_state(path)["processed_deal_ids"][key]
    assert processed["receipt"] == entry["receipt"]
    assert processed["economic_payload_hash"] == "historical-hash"
    assert processed["diagnostics"]["reconciled_terminal_event_ids"] == [repo.events[0]["event_id"]]
    assert reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)["applied_count"] == 0


@pytest.mark.parametrize("failure", ["void", "missing_event", "wrong_event_quantity", "fractional", "boolean", "wrong_account", "wrong_price", "wrong_multiplier", "partial", "duplicate_allocation", "identity_collision", "read_failure"])
def test_canonical_completion_rejects_unproven_terminal_effects(tmp_path, failure):
    repo = _completed_canonical_repo()
    key = "futu:lx:1001:option-1"
    entry = {"account": "lx", "status": "unresolved", "action": "lifecycle", "futu_account_id": "1001"}
    if failure == "void":
        repo.events.append({**repo.events[0], "event_id": "void", "event_type": "void", "target_event_id": repo.events[0]["event_id"], "raw_payload": {"target_event_id": repo.events[0]["event_id"]}})
    elif failure == "missing_event": repo.events.clear()
    elif failure == "wrong_event_quantity": repo.events[0]["contracts"] = 2
    elif failure == "fractional": repo.events[0]["contracts"] = 1.5
    elif failure == "boolean": repo.events[0]["contracts"] = True
    elif failure == "wrong_account": repo.events[0]["account"] = "sy"
    elif failure == "wrong_price": repo.events[0]["price"] = 1
    elif failure == "wrong_multiplier": repo.events[0]["multiplier"] = 10
    elif failure == "partial":
        repo.lifecycle_cases[0]["target_contracts_by_lot"]["lot-1"] = 2
        repo.position_lots = [_position_lot("lot-1", contracts=2)]
    elif failure == "duplicate_allocation": repo.lifecycle_allocations.append(dict(repo.lifecycle_allocations[0]))
    elif failure == "identity_collision":
        key = "option-1"
        entry["futu_account_id"] = "2002"
    elif failure == "read_failure":
        def unavailable(**kwargs): raise OSError("read failed")
        repo.read_lifecycle_account_rows = unavailable
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: entry}})
    before = path.read_bytes()
    result = reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)
    assert result["applied_count"] == result["planned_count"] == 0
    assert path.read_bytes() == before


def _stock_completion_repo():
    from src.application.ledger.api import execution_identity_from_input
    repo = _completed_canonical_repo(terminal_type="assignment")
    source_key = "futu:lx:1001:stock-1"
    raw = {"account": "lx", "futu_account_id": "1001", "symbol": "FUTU",
        "contracts": 100, "price": 120, "side": "buy", "trade_time_ms": 1_700_000_001_000,
        "execution_input": {"broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"},
            "external_id_namespace": "futu.deal", "external_execution_id": "stock-1"}}
    source = {"evidence_id": "stock-observed", "source_event_id": source_key, "raw": raw}
    owner = repo.lifecycle_evidence[0]
    owner["source_evidence_ids"] = ["stock-observed"]
    stock_claim = build_source_consumption_claim(source_key=source_key, case_id="canonical",
        owner_evidence_id="anchor", source_role="stock_settlement", economic_payload=raw)
    repo.lifecycle_source_consumptions.append(stock_claim)
    repo.events[0]["raw_payload"]["stock_settlement"] = {"source_event_id": source_key,
        "futu_account_id": "1001", "symbol": "FUTU", "shares": 100, "price": 120,
        "side": "buy", "event_time_ms": 1_700_000_001_000}
    key = execution_identity_from_input(raw["execution_input"])
    state = {"account": "lx", "futu_account_id": "1001", "source_deal_id": "stock-1",
        "status": "unresolved", "action": "lifecycle", "diagnostics": {"lifecycle_evidence": source},
        "economic_payload_hash": stock_claim["source_payload_hash"]}
    return repo, key, state


@pytest.mark.parametrize("failure", [None, "wrong_environment", "missing_environment", "wrong_quantity", "wrong_price", "wrong_source_ref", "changed_state_economics"])
def test_stock_source_requires_matching_terminal_and_explicit_execution_identity(tmp_path, failure):
    repo, key, state = _stock_completion_repo()
    if failure == "wrong_environment": state["diagnostics"]["lifecycle_evidence"]["raw"]["execution_input"]["broker_account_ref"]["environment"] = "SIMULATE"
    if failure == "missing_environment": state["diagnostics"]["lifecycle_evidence"]["raw"]["execution_input"]["broker_account_ref"].pop("environment")
    if failure == "wrong_quantity": repo.events[0]["raw_payload"]["stock_settlement"]["shares"] = 99
    if failure == "wrong_price": repo.events[0]["raw_payload"]["stock_settlement"]["price"] = 121
    if failure == "wrong_source_ref": repo.lifecycle_evidence[0]["source_evidence_ids"] = ["another-source"]
    if failure == "changed_state_economics": state["diagnostics"]["lifecycle_evidence"]["raw"]["contracts"] = 200
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: state}})
    result = reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)
    assert result["applied_count"] == (1 if failure is None else 0)
    if failure is None:
        assert result["actions"][0]["source_key"] == "futu:lx:1001:stock-1"
        assert result["actions"][0]["source_payload_hash"] == state["economic_payload_hash"]


def test_reconcile_callback_filters_preview_and_apply_and_retries_after_failure(tmp_path):
    repo = _completed_canonical_repo()
    key = "futu:lx:1001:option-1"
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: {"account": "lx", "status": "unresolved"}}})
    before = path.read_bytes()
    seen = []
    def reject(original, proposed, actions):
        seen.append(actions[0]["source_key"])
        return []
    for apply in (False, True):
        result = reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=apply, before_state_update=reject)
        assert result["planned_count"] == result["applied_count"] == 0
        assert result["pending_after"]["unresolved_deal_ids"] == 1
        assert path.read_bytes() == before
    assert seen == [key, key]
    def unavailable(*args): raise OSError("inbox write failed")
    with pytest.raises(OSError, match="inbox write failed"):
        reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True, before_state_update=unavailable)
    assert path.read_bytes() == before
    assert reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)["applied_count"] == 1


def _normalized_completion_deal(*, asset="option", side="buy"):
    from src.application.trades.normalizer import normalize_trade_deal
    return normalize_trade_deal({
        "code": "US.FUTU260821P120000" if asset == "option" else "US.FUTU",
        "asset_type": asset, "environment": "REAL", "external_id_namespace": "futu.deal",
        "futu_account_id": "1001", "deal_id": "option-1" if asset == "option" else "stock-1",
        "qty": 1 if asset == "option" else 100, "price": 0 if asset == "option" else 120,
        "trd_side": "BUY_BACK" if asset == "option" else side.upper(),
        "position_effect": "close" if asset == "option" else None,
        "multiplier": 100 if asset == "option" else None, "currency": "USD",
        "trade_time_ms": 1_700_000_000_100 if asset == "option" else 1_700_000_001_000,
    }, futu_account_mapping={"1001": "lx"}, allow_opend_refresh=False)


@pytest.mark.parametrize("asset", ["option", "stock"])
@pytest.mark.parametrize("change", [None, "price", "quantity", "time", "symbol", "account", "physical", "environment", "missing_quantity", "missing_time", "missing_price", "execution_price", "execution_quantity"])
def test_reconciled_lifecycle_source_matches_current_deal_economics(asset, change):
    from dataclasses import replace
    from src.application.trades.state_reconcile import reconciled_source_matches_deal
    repo = _completed_canonical_repo() if asset == "option" else _stock_completion_repo()[0]
    source = repo.lifecycle_source_consumptions[0 if asset == "option" else -1]
    action = {"reason": "lifecycle_case_already_recorded", "source_key": source["source_key"],
        "source_payload": source["source_payload"], "source_payload_hash": source["source_payload_hash"]}
    deal = _normalized_completion_deal(asset=asset)
    changes = {"price": {"price": 42}, "quantity": {"contracts": 2}, "time": {"trade_time_ms": 1},
        "symbol": {"symbol": "OTHER"}, "account": {"internal_account": "sy"}, "physical": {"futu_account_id": "2002"},
        "missing_quantity": {"contracts": None}, "missing_time": {"trade_time_ms": None}, "missing_price": {"price": None}}
    if change in changes: deal = replace(deal, **changes[change])
    if change == "environment": deal.execution_input["broker_account_ref"]["environment"] = "SIMULATE"
    if change == "execution_price": deal.execution_input["price"] = "42"
    if change == "execution_quantity": deal.execution_input["quantity"] = "42"
    assert reconciled_source_matches_deal(action, deal) is (change is None)


@pytest.mark.parametrize("change", [None, "quantity", "price", "time", "account", "physical", "missing_quantity", "missing_price", "missing_time", "wrong_side", "execution_economic_conflict"])
def test_reconciled_assigned_stock_sale_matches_recorded_economics(change):
    from copy import deepcopy
    from src.application.trades.state_reconcile import reconciled_source_matches_deal
    deal = _normalized_completion_deal(asset="stock", side="sell")
    event = {"event_type": "sale", "stock_event_id": "sale-1", "source_deal_id": "stock-1",
        "account": "lx", "futu_account_id": "1001", "symbol": "FUTU", "shares": 100,
        "price": 120, "trade_time_ms": 1_700_000_001_000, "side": "sell", "currency": "USD"}
    changes = {"quantity": ("shares", 99), "price": ("price", 121), "time": ("trade_time_ms", 1),
        "account": ("account", "sy"), "physical": ("futu_account_id", "2002"), "missing_quantity": ("shares", None),
        "missing_price": ("price", None), "missing_time": ("trade_time_ms", None), "wrong_side": ("side", "buy")}
    if change in changes:
        field, value = changes[change]
        event[field] = value
    if change == "execution_economic_conflict":
        event["execution_input"] = deepcopy(deal.execution_input)
        event["execution_input"]["price"] = "121"
    action = {"reason": "assigned_stock_sale_event_recorded", "assigned_stock_event": event}
    assert reconciled_source_matches_deal(action, deal) is (change is None)


@pytest.mark.parametrize("asset", ["stock", "option"])
@pytest.mark.parametrize("changed_economics", [None, "quantity", "price"])
def test_source_completion_callback_validates_lifecycle_inbox_without_optional_currency(
    tmp_path, asset, changed_economics,
):
    from copy import deepcopy
    from src.application.trades.auto_intake import _reconcile_source_completion
    from src.application.trades.deal_identity import broker_deal_key
    from src.application.trades.inbox import (
        begin_trade_receipt_attempt, claim_trade_payload_refresh_intent,
        enqueue_trade_payload, list_trade_receipt_recovery_rows,
        list_unclaimed_trade_payload_refresh_intents, read_trade_payload,
        record_trade_payload_refresh_intent,
    )
    from src.application.trades.normalizer import normalize_trade_deal

    deal = _normalized_completion_deal(asset=asset)
    payload = deepcopy(deal.execution_input)
    if asset == "stock":
        repo, key, state = _stock_completion_repo()
        payload["currency"] = payload["instrument_ref"]["currency"] = None
    else:
        repo = _completed_canonical_repo()
        key = "futu:lx:1001:option-1"
        state = {"account": "lx", "futu_account_id": "1001", "source_deal_id": "option-1",
            "status": "unresolved", "action": "lifecycle", "economic_payload_hash": "old-format-hash"}
    if changed_economics == "quantity": payload["quantity"] = "2" if asset == "option" else "200"
    if changed_economics == "price": payload["price"] = "42"
    observed_deal = normalize_trade_deal(payload, futu_account_mapping={"1001": "lx"}, allow_opend_refresh=False)
    if asset == "stock":
        assert "missing:currency" in observed_deal.execution_input["errors"]
        assert "missing:instrument_ref.currency" in observed_deal.execution_input["errors"]
    inbox = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=broker_deal_key(observed_deal), repo=repo)
    record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "prior-intent"})
    path = tmp_path / "state.json"
    state["receipt"] = {"status": "skipped", "reason": "lifecycle_outbox_not_created"}
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: state}})
    source = {"account": "lx", "account_mapping": {"1001": "lx"}, "state_path": path, "inbox_path": inbox}
    source_before = path.read_bytes()
    row_before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    ledger_before = deepcopy((repo.events, repo.lifecycle_cases, repo.lifecycle_evidence,
        repo.lifecycle_allocations, repo.lifecycle_source_consumptions, repo.position_lots, repo.assigned_stock_events))
    preview = _reconcile_source_completion(source=source, repo=repo, apply_changes=False)
    expected = int(changed_economics is None)
    assert preview["planned_count"] == expected
    assert preview["applied_count"] == preview["inbox_updated_count"] == 0
    assert path.read_bytes() == source_before
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == row_before
    applied = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert applied["applied_count"] == applied["inbox_updated_count"] == expected
    assert (repo.events, repo.lifecycle_cases, repo.lifecycle_evidence, repo.lifecycle_allocations,
        repo.lifecycle_source_consumptions, repo.position_lots, repo.assigned_stock_events) == ledger_before
    if changed_economics:
        assert applied["deferred"][0]["reason"] == "inbox_economic_evidence_unproven"
        assert path.read_bytes() == source_before
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == row_before
        return
    closed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert closed["status"] == "handled"
    assert closed["receipt_envelope"] == row_before["receipt_envelope"]
    assert closed["portfolio_refresh_intent_json"] == row_before["portfolio_refresh_intent_json"]
    assert closed["portfolio_refresh_attempted_at_ms"] is None
    assert load_trade_intake_state(path)["processed_deal_ids"][key]["receipt"] == state["receipt"]
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["1001"]) == []
    assert list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"1001": "lx"}) == []
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None
    assert not begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={"route": "test"}, message="ignored")["claimed"]
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == closed
    repeated = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert repeated["applied_count"] == repeated["inbox_updated_count"] == 0


@pytest.mark.parametrize("completion", ["option", "stock", "assigned_stock_sale"])
@pytest.mark.parametrize("explicit_namespace", [False, True])
def test_historical_pending_source_identity_conflict_cannot_be_reconciled(
    tmp_path, completion, explicit_namespace,
):
    from copy import deepcopy
    from src.application.trades.auto_intake import _reconcile_source_completion
    from src.application.trades.deal_identity import broker_deal_key
    from src.application.trades.inbox import (
        _connect, enqueue_trade_payload, read_trade_payload,
        record_trade_payload_refresh_intent,
    )
    from src.application.trades.normalizer import normalize_trade_deal

    asset = "option" if completion == "option" else "stock"
    side = "sell" if completion == "assigned_stock_sale" else "buy"
    original = _normalized_completion_deal(asset=asset, side=side)
    execution = deepcopy(original.execution_input)
    if asset == "stock":
        execution["currency"] = execution["instrument_ref"]["currency"] = None
    payload = {"execution_input": execution, "source_deal_id": "conflicting-source"}
    if explicit_namespace:
        payload["external_id_namespace"] = "futu.deal"
    deal = normalize_trade_deal(payload, futu_account_mapping={"1001": "lx"}, allow_opend_refresh=False)
    assert "invalid:source_execution_identity" in deal.execution_input["errors"]
    if completion == "stock":
        repo, key, state = _stock_completion_repo()
    else:
        key = f"futu:lx:1001:{original.deal_id}"
        state = {"account": "lx", "futu_account_id": "1001", "source_deal_id": original.deal_id,
            "status": "unresolved"}
        repo = _completed_canonical_repo() if asset == "option" else FakeRepo([], assigned_stock_events=[{
            "event_type": "sale", "stock_event_id": "sale-1", "source_deal_id": "stock-1",
            "account": "lx", "futu_account_id": "1001", "symbol": "FUTU", "shares": 100,
            "price": 120, "trade_time_ms": 1_700_000_001_000, "side": "sell",
        }])
    inbox = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=broker_deal_key(deal), repo=repo)
    receipt = {"status": "unknown", "reason": "sender_outcome_unknown", "attempt_id": "old-attempt"}
    previous_result = {"status": "unresolved", "reason": "historical_pending", "receipt_kind": "pending_retry"}
    # Reconstruct an upgrade-era durable pending row. Current enqueue rejects this
    # conflict, but startup recovery must also validate rows admitted by old code.
    with closing(_connect(inbox)) as conn, conn:
        conn.execute(
            """UPDATE trade_inbox SET status='pending', result_status='unresolved',
               result_reason='historical_pending', last_error=NULL, result_json=?, receipt_json=?
               WHERE inbox_id=?""",
            (json.dumps(previous_result), json.dumps(receipt), inbox_id),
        )
    record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "prior-intent"})
    state["receipt"] = dict(receipt)
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: state}})
    source = {"account": "lx", "account_mapping": {"1001": "lx"}, "state_path": path, "inbox_path": inbox}
    state_before = path.read_bytes()
    row_before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    ledger_before = deepcopy(vars(repo))
    assert row_before["status"] == "pending"
    assert row_before["receipt"] == receipt
    for apply in (False, True, True):
        result = _reconcile_source_completion(source=source, repo=repo, apply_changes=apply)
        assert result["planned_count"] == result["applied_count"] == result["inbox_updated_count"] == 0
        assert result["deferred"] == [{"deal_id": key, "reason": "inbox_source_identity_conflict"}]
        assert path.read_bytes() == state_before
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == row_before
        assert vars(repo) == ledger_before


def _multi_lot_stock_completion_repo(*, legacy=False, mixed=False):
    from copy import deepcopy
    from domain.domain.lifecycle_allocation import allocation_id_for, terminal_event_id_for

    repo, key, state = _stock_completion_repo()
    case, anchor = repo.lifecycle_cases[0], repo.lifecycle_evidence[0]
    case["target_contracts_by_lot"]["lot-2"] = 1
    anchor["target_contracts_by_lot"]["lot-2"] = 1
    anchor["contracts"] = 2
    repo.position_lots.append(_position_lot("lot-2"))
    for index, quantity in ((0, 2), (1, 100 if mixed else 200)):
        claim = repo.lifecycle_source_consumptions[index]
        repo.lifecycle_source_consumptions[index] = build_source_consumption_claim(
            source_key=claim["source_key"], case_id=claim["case_id"],
            owner_evidence_id=claim["owner_evidence_id"], source_role=claim["source_role"],
            economic_payload={**claim["source_payload"], "quantity": str(quantity)},
        )
    state["diagnostics"]["lifecycle_evidence"]["raw"]["contracts"] = 100 if mixed else 200
    state["economic_payload_hash"] = repo.lifecycle_source_consumptions[1]["source_payload_hash"]
    evidence_id, terminal_type = ("expiry", "expire_close") if mixed else ("anchor", "assignment")
    allocation = {**repo.lifecycle_allocations[0], "target_lot_id": "lot-2",
        "evidence_id": evidence_id, "terminal_type": terminal_type,
        "allocation_id": allocation_id_for(case_id="canonical", evidence_id=evidence_id, target_lot_id="lot-2"),
        "canonical_terminal_event_id": terminal_event_id_for(case_id="canonical", evidence_id=evidence_id,
            target_lot_id="lot-2", terminal_type=terminal_type, contracts_allocated=1)}
    repo.lifecycle_allocations.append(allocation)
    event = deepcopy(repo.events[0])
    event.update(event_id=allocation["canonical_terminal_event_id"], target_lot_id="lot-2", event_type=terminal_type)
    event["raw_payload"].update(target_lot_id="lot-2", evidence_id=evidence_id, allocation_id=allocation["allocation_id"])
    repo.events.append(event)
    if mixed:
        repo.lifecycle_evidence.append({"evidence_id": evidence_id, "case_id": "canonical", "evidence_type": "expiration_confirmation"})
        event["raw_payload"].pop("stock_settlement")
    else:
        source = {**event["raw_payload"]["stock_settlement"], "shares": 200}
        for row in repo.events:
            if legacy:
                row["raw_payload"]["stock_settlement"] = dict(source)
            else:
                row["raw_payload"]["stock_settlement_source"] = dict(source)
    return repo, key, state


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("asset", ["option", "stock"])
@pytest.mark.parametrize("failure", [None, "allocation", "source", "fractional", "boolean", "void", "missing"])
def test_source_completion_uses_canonical_stock_allocation_group(tmp_path, legacy, asset, failure):
    from copy import deepcopy
    from src.application.trades.auto_intake import _reconcile_source_completion
    from src.application.trades.deal_identity import broker_deal_key
    from src.application.trades.inbox import enqueue_trade_payload, read_trade_payload
    from src.application.trades.normalizer import normalize_trade_deal

    repo, key, state = _multi_lot_stock_completion_repo(legacy=legacy)
    if failure == "allocation":
        for event, shares in zip(repo.events, (50, 150)):
            event["raw_payload"]["stock_settlement"]["shares"] = shares
    elif failure == "source":
        repo.events[0]["raw_payload"]["stock_settlement"]["source_event_id"] = "futu:lx:1001:other"
    elif failure in {"fractional", "boolean"}:
        repo.events[0]["raw_payload"]["stock_settlement"]["shares"] = 100.5 if failure == "fractional" else True
    elif failure == "void":
        repo.events.append({**repo.events[-1], "event_id": "void", "event_type": "void", "account": "lx",
            "target_event_id": repo.events[-1]["event_id"], "raw_payload": {"target_event_id": repo.events[-1]["event_id"]}})
    elif failure == "missing":
        repo.events.pop()
    payload = deepcopy(_normalized_completion_deal(asset=asset).execution_input)
    payload["quantity"] = "200" if asset == "stock" else "2"
    if asset == "stock":
        payload["currency"] = payload["instrument_ref"]["currency"] = None
    else:
        key = "futu:lx:1001:option-1"
        state = {"account": "lx", "futu_account_id": "1001", "source_deal_id": "option-1", "status": "unresolved"}
    deal = normalize_trade_deal(payload, futu_account_mapping={"1001": "lx"}, allow_opend_refresh=False)
    inbox = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=broker_deal_key(deal), repo=repo)
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: state}})
    source = {"account": "lx", "account_mapping": {"1001": "lx"}, "state_path": path, "inbox_path": inbox}
    before = path.read_bytes()
    observed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    expected = int(failure is None)
    preview = _reconcile_source_completion(source=source, repo=repo, apply_changes=False)
    assert preview["planned_count"] == expected
    assert path.read_bytes() == before and read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == observed
    result = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert result["applied_count"] == result["inbox_updated_count"] == expected
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)["status"] == ("handled" if expected else "pending")
    if not expected:
        assert path.read_bytes() == before
    assert _reconcile_source_completion(source=source, repo=repo, apply_changes=True)["applied_count"] == 0


@pytest.mark.parametrize("asset", ["stock", "option"])
def test_source_completion_reports_all_mixed_terminal_types(tmp_path, asset):
    from copy import deepcopy
    from src.application.trades.auto_intake import _reconcile_source_completion
    from src.application.trades.deal_identity import broker_deal_key
    from src.application.trades.inbox import enqueue_trade_payload, read_trade_payload
    from src.application.trades.normalizer import normalize_trade_deal

    repo, key, state = _multi_lot_stock_completion_repo(mixed=True)
    payload = deepcopy(_normalized_completion_deal(asset=asset).execution_input)
    if asset == "option":
        payload["quantity"] = "2"
        key = "futu:lx:1001:option-1"
        state = {"account": "lx", "futu_account_id": "1001", "source_deal_id": "option-1", "status": "unresolved"}
    deal = normalize_trade_deal(payload, futu_account_mapping={"1001": "lx"}, allow_opend_refresh=False)
    inbox = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=broker_deal_key(deal), repo=repo)
    path = tmp_path / "state.json"
    write_trade_intake_state(path, {"unresolved_deal_ids": {key: state}})
    source = {"account": "lx", "account_mapping": {"1001": "lx"}, "state_path": path, "inbox_path": inbox}
    result = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert result["applied_count"] == result["inbox_updated_count"] == 1
    assert result["actions"][0]["lifecycle_decision_type"] == "mixed"
    assert result["actions"][0]["lifecycle_terminal_types"] == ["assignment", "expire_close"]
    processed = load_trade_intake_state(path)["processed_deal_ids"][key]
    assert processed["diagnostics"]["reconciled_lifecycle_decision_type"] == "mixed"
    assert processed["diagnostics"]["reconciled_lifecycle_terminal_types"] == ["assignment", "expire_close"]
    closed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert closed["result"]["action"] == "mixed"
    assert _reconcile_source_completion(source=source, repo=repo, apply_changes=True)["applied_count"] == 0
