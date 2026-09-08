from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import OpenPositionCommand
from domain.domain.wheel import (
    WHEEL_EVENT_TYPES,
    WHEEL_EVENT_SCHEMA_V1,
    build_wheel_call_rank_key,
    build_wheel_put_rank_key,
    build_wheel_event,
    evaluate_wheel_call_candidate,
    evaluate_wheel_put_candidate,
    project_wheel_call_intents,
    project_wheel_call_linkage_candidates,
    project_wheel_lifecycles,
)
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.wheel import build_wheel_read_model, build_wheel_read_model_from_rows


def _started_event(*, source_trade_event_id: str = "assign-put") -> dict:
    return build_wheel_event(
        event_id="wheel-start-1",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account="lx",
        stock_lot_id="assigned-stock-assign-put",
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        source_trade_event_id=source_trade_event_id,
        payload={"request_id": "assignment:assign-put"},
    )


def _assignment_trade() -> dict:
    return {
        "event_id": "assign-put",
        "event_type": "assignment",
        "event_time_ms": 2_000,
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "multiplier": 100,
    }


def _assigned_stock(*, remaining: int = 100) -> dict:
    return {
        "_all_assigned_stock_lots": [
            {
                "stock_lot_id": "assigned-stock-assign-put",
                "source_assignment_event_id": "assign-put",
                "account": "lx",
                "symbol": "NVDA",
                "shares_opened": 100,
                "shares_remaining": remaining,
                "shares_sold": 100 - remaining,
                "assignment_price": 100,
                "assignment_fees": 0,
                "stock_cost_basis_total": 10_000,
                "sale_event_ids": [],
            }
        ],
        "assigned_stock_review_rows": [],
    }


def test_wheel_projection_is_order_independent_and_tracks_linked_call() -> None:
    call_lot = {
        "record_id": "call-lot-1",
        "fields": {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "open",
            "contracts_open": 1,
            "multiplier": 100,
            "strategy": "wheel",
            "leg_role": "wheel_call",
            "source_stock_lot_id": "assigned-stock-assign-put",
            "source_event_id": "open-call-1",
        },
    }
    call_trade = {
        "event_id": "open-call-1",
        "event_type": "open",
        "event_time_ms": 2_500,
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "position_side": "short",
        "multiplier": 100,
    }
    first = project_wheel_lifecycles(
        [_started_event()],
        [_assignment_trade(), call_trade],
        [call_lot],
        _assigned_stock(),
        3_000,
    )[0]
    replay = project_wheel_lifecycles(
        [_started_event(), _started_event()],
        [call_trade, _assignment_trade()],
        [call_lot],
        _assigned_stock(),
        3_000,
    )[0]

    assert first == replay
    assert first["lifecycle_status"] == "active"
    assert first["phase"] == "call_open"
    assert first["integrity_status"] == "trusted"
    assert first["active_call_lot_ids"] == ["call-lot-1"]


def test_wheel_projection_fails_closed_when_called_away_event_is_missing() -> None:
    closed_call = {
        "record_id": "call-lot-1",
        "fields": {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "close",
            "contracts_open": 0,
            "multiplier": 100,
            "strategy": "wheel",
            "leg_role": "wheel_call",
            "source_stock_lot_id": "assigned-stock-assign-put",
            "source_event_id": "open-call-1",
        },
    }
    call_assignment = {
        "event_id": "assign-call",
        "event_type": "assignment",
        "event_time_ms": 3_000,
        "target_lot_id": "call-lot-1",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "position_side": "short",
        "multiplier": 100,
    }

    batch = project_wheel_lifecycles(
        [_started_event()],
        [_assignment_trade(), call_assignment],
        [closed_call],
        _assigned_stock(remaining=0),
        4_000,
    )[0]

    assert batch["integrity_status"] == "conflict"
    assert batch["phase"] is None
    assert "called_away_event_missing" in batch["reason_codes"]


def test_repository_appends_wheel_event_once_and_reads_it_in_same_snapshot(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
    )
    assignment = next(
        item for item in repo.list_trade_events() if item["event_type"] == "assignment"
    )
    stock_lot_id = f"assigned-stock-{assignment['event_id']}"
    event = build_wheel_event(
        event_id=f"wheel-start-{assignment['event_id']}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account="lx",
        stock_lot_id=stock_lot_id,
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        source_trade_event_id=assignment["event_id"],
        payload={"request_id": f"assignment:{assignment['event_id']}"},
    )
    with repo._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert repo.append_wheel_event_once(event, conn=conn) is True
        assert repo.append_wheel_event_once(event, conn=conn) is False
        conn.commit()

    rows = repo.read_decision_state_rows_many(accounts=["lx"])["lx"]
    model = build_wheel_read_model(repo, "lx", 3_000)

    assert rows["account_wheel_events"] == [event]
    assert event["event_schema_version"] == "wheel_event.v1"
    assert event["wheel_branch_id"] == stock_lot_id
    assert model["batches"][0]["stock_lot_id"] == stock_lot_id
    assert model["batches"][0]["phase"] == "ready"
    with repo._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE wheel_events SET occurred_at_ms = 9 WHERE event_id = ?",
            (event["event_id"],),
        )
    with repo._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM wheel_events WHERE event_id = ?", (event["event_id"],))


def test_read_model_reprojects_position_lots_from_same_as_of_trade_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )
    events = [
        TradeEvent(
            event_id="put-open",
            event_type="open",
            event_time_ms=1_000,
            contract_key=key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            lot_id="put-lot",
        ),
        TradeEvent(
            event_id="put-close",
            event_type="close",
            event_time_ms=2_000,
            contract_key=key,
            contracts=1,
            price=1,
            currency="USD",
            source="test",
            target_lot_id="put-lot",
        ),
        TradeEvent(
            event_id="void-put-close",
            event_type="void",
            event_time_ms=3_000,
            contract_key=key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            target_event_id="put-close",
        ),
    ]
    captured = []

    def _capture(_wheel_events, trade_events, position_lots, _stock, _instant):
        captured.append(
            (
                [item["event_id"] for item in trade_events],
                [
                    (
                        item["record_id"],
                        item["fields"]["status"],
                        item["fields"]["contracts_open"],
                    )
                    for item in position_lots
                ],
            )
        )
        return {"batches": [], "wheel_branches": []}

    monkeypatch.setattr(
        "src.application.wheel.read_model.project_wheel_lifecycles",
        _capture,
    )
    monkeypatch.setattr(
        "src.application.wheel.read_model.build_assigned_stock_projection_from_rows",
        lambda *_args, **_kwargs: {},
    )
    rows = {"trade_events": [event.to_dict() for event in events]}

    for instant in (1_500, 2_500, 3_500):
        build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=instant)

    assert captured == [
        (["put-open"], [("put-lot", "open", 1)]),
        (["put-open", "put-close"], [("put-lot", "close", 0)]),
        (
            ["put-open", "put-close", "void-put-close"],
            [("put-lot", "open", 1)],
        ),
    ]


def test_repository_migrates_wheel_event_v1_without_changing_hash_or_facts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    event = build_wheel_event(
        event_id="legacy-wheel-start",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account="lx",
        stock_lot_id="assigned-stock-legacy",
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        payload={"request_id": "legacy-assignment"},
    )
    payload_json = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True)
    with repo._connect() as conn:
        conn.execute("DROP TABLE wheel_events")
        conn.execute(
            """
            CREATE TABLE wheel_events (
              event_id TEXT PRIMARY KEY,
              account TEXT NOT NULL CHECK(
                typeof(account) = 'text' AND account != '' AND account = lower(account)
              ),
              stock_lot_id TEXT NOT NULL CHECK(stock_lot_id != ''),
              event_type TEXT NOT NULL CHECK(event_type IN (
                'wheel_started', 'wheel_manual_ended', 'wheel_called_away',
                'wheel_call_intent_created', 'wheel_call_intent_cancelled',
                'wheel_call_intent_consumed', 'wheel_call_linkage_rejected',
                'wheel_event_voided'
              )),
              occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms > 0),
              recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms > 0),
              intent_id TEXT,
              source_trade_event_id TEXT,
              payload_json TEXT NOT NULL CHECK(
                json_valid(payload_json) AND json_type(payload_json) = 'object'
              ),
              payload_hash TEXT NOT NULL CHECK(
                length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'
              ),
              FOREIGN KEY(source_trade_event_id) REFERENCES trade_events(event_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wheel_events (
              event_id, account, stock_lot_id, event_type, occurred_at_ms,
              recorded_at_ms, intent_id, source_trade_event_id, payload_json,
              payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["account"],
                event["stock_lot_id"],
                event["event_type"],
                event["occurred_at_ms"],
                event["recorded_at_ms"],
                event["intent_id"],
                event["source_trade_event_id"],
                payload_json,
                event["payload_hash"],
            ),
        )
        conn.commit()

    migrated = SQLiteOptionPositionsRepository(db_path)

    assert migrated.list_wheel_events(account="lx") == [event]
    with migrated._connect() as conn:
        columns = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(wheel_events)")
        }
        stored = conn.execute(
            "SELECT * FROM wheel_events WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wheel_events'"
        ).fetchone()["sql"]
        violations = conn.execute("PRAGMA foreign_key_check(wheel_events)").fetchall()

    assert columns["stock_lot_id"]["notnull"] == 0
    assert columns["wheel_branch_id"]["notnull"] == 1
    assert stored["event_schema_version"] == WHEEL_EVENT_SCHEMA_V1
    assert stored["wheel_branch_id"] == event["stock_lot_id"]
    assert stored["payload_json"] == payload_json
    assert stored["payload_hash"] == event["payload_hash"]
    assert all(event_type in table_sql for event_type in WHEEL_EVENT_TYPES)
    assert violations == []


def test_repository_appends_nullable_stock_wheel_event_v2_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = build_wheel_event(
        event_id="wheel-put-branch-created",
        account="lx",
        wheel_branch_id="wheel-put:branch-1",
        stock_lot_id=None,
        event_type="wheel_branch_created",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        payload={"direction": "put", "parent_branch_id": "wheel-call:parent-1"},
    )
    with repo._writer_connection(begin_immediate=True) as conn:
        assert repo.append_wheel_event_once(event, conn=conn) is True
    assert repo.list_wheel_events(account="lx") == [event]

    tampered = {**event, "payload": {**event["payload"], "direction": "call"}}
    with repo._writer_connection(begin_immediate=True) as conn, pytest.raises(
        ValueError, match="payload hash mismatch"
    ):
        repo.append_wheel_event_once(tampered, conn=conn)


def test_repository_rejects_wheel_v1_migration_when_hash_does_not_recompute(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    event = build_wheel_event(
        event_id="legacy-invalid-hash",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account="lx",
        stock_lot_id="assigned-stock-legacy",
        event_type="wheel_started",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        payload={"request_id": "legacy-assignment"},
    )
    with repo._connect() as conn:
        conn.execute("DROP TABLE wheel_events")
        conn.execute(
            """
            CREATE TABLE wheel_events (
              event_id TEXT PRIMARY KEY,
              account TEXT NOT NULL,
              stock_lot_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              occurred_at_ms INTEGER NOT NULL,
              recorded_at_ms INTEGER NOT NULL,
              intent_id TEXT,
              source_trade_event_id TEXT,
              payload_json TEXT NOT NULL,
              payload_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wheel_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["account"],
                event["stock_lot_id"],
                event["event_type"],
                event["occurred_at_ms"],
                event["recorded_at_ms"],
                None,
                None,
                json.dumps(event["payload"]),
                "0" * 64,
            ),
        )
        conn.commit()

    with pytest.raises(ValueError, match="payload hash mismatch"):
        SQLiteOptionPositionsRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(wheel_events)")]
    assert "event_schema_version" not in columns


def test_position_lot_patch_accepts_first_class_stock_lot_link() -> None:
    from domain.domain.ledger.position_fields import build_open_adjustment_patch

    patch = build_open_adjustment_patch(
        {
            "symbol": "NVDA",
            "option_type": "call",
            "side": "short",
            "status": "open",
            "contracts": 1,
            "contracts_closed": 0,
            "strike": 110,
            "multiplier": 100,
            "expiration_ymd": "2026-08-21",
        },
        strategy="wheel",
        leg_role="wheel_call",
        source_stock_lot_id="assigned-stock-assign-put",
        as_of_ms=3_000,
    )

    assert patch["source_stock_lot_id"] == "assigned-stock-assign-put"


def test_wheel_candidate_uses_batch_cost_floor_and_lifecycle_pnl() -> None:
    batch = {
        "shares_remaining": 100,
        "remaining_stock_cost_basis": 10_010,
        "realized_sell_put_net_pnl": 240,
        "realized_prior_call_net_pnl": 100,
        "realized_prior_stock_sale_net_pnl": 0,
    }
    candidate = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA-CALL-102",
        "strike": 102,
        "spot": 100,
        "delta": 0.31,
        "multiplier": 100,
        "net_premium": 190,
        "net_premium_cny": 1_350,
        "period_net_premium_return": 0.019,
        "annualized_net_premium_return": 0.16,
        "spread_ratio": 0.1,
        "open_interest": 500,
    }

    accepted = evaluate_wheel_call_candidate(
        batch,
        candidate,
        {"min_abs_delta": 0.25, "max_abs_delta": 0.35},
        {"basis": "estimated", "amount": 15},
        1,
    )
    below_cost = evaluate_wheel_call_candidate(
        batch,
        {**candidate, "strike": 100},
        {"min_abs_delta": 0.25, "max_abs_delta": 0.35},
        {"basis": "estimated", "amount": 15},
        1,
    )
    negative_delta = evaluate_wheel_call_candidate(
        batch,
        {**candidate, "delta": -0.31},
        {"min_abs_delta": 0.25, "max_abs_delta": 0.35},
        {"basis": "estimated", "amount": 15},
        1,
    )

    assert accepted["accepted"] is True
    assert accepted["projected_lifecycle_net_pnl_if_called"] == 705
    assert accepted["projected_lifecycle_pnl_scope"] == "final_total_if_called"
    assert below_cost["wheel_candidate_status"] == "rejected"
    assert "wheel_call_strike_below_cost_floor" in below_cost["reason_codes"]
    assert negative_delta["wheel_candidate_status"] == "accepted"


def test_wheel_candidate_rank_uses_lifecycle_pnl_before_covered_call_ties() -> None:
    higher_lifecycle = build_wheel_call_rank_key(
        {
            "projected_lifecycle_net_pnl_if_called": 500,
            "period_net_premium_return": 0.01,
            "strike": 105,
            "contract_symbol": "LOW-PREMIUM",
        }
    )
    lower_lifecycle = build_wheel_call_rank_key(
        {
            "projected_lifecycle_net_pnl_if_called": 400,
            "period_net_premium_return": 0.03,
            "strike": 120,
            "contract_symbol": "HIGH-PREMIUM",
        }
    )

    assert higher_lifecycle["sort_tuple"] < lower_lifecycle["sort_tuple"]


def test_void_removes_intent_and_linkage_rejection_from_standalone_projections() -> None:
    intent = build_wheel_event(
        event_id="intent-created-1",
        account="lx",
        stock_lot_id="stock-1",
        event_type="wheel_call_intent_created",
        occurred_at_ms=2_000,
        recorded_at_ms=2_001,
        intent_id="intent-1",
        payload={"contracts": 1, "multiplier": 100, "expires_at_ms": 9_000},
    )
    rejection = build_wheel_event(
        event_id="linkage-rejected-1",
        account="lx",
        stock_lot_id="stock-1",
        event_type="wheel_call_linkage_rejected",
        occurred_at_ms=2_100,
        recorded_at_ms=2_101,
        payload={"call_open_event_id": "call-open-1"},
    )
    void_intent = build_wheel_event(
        event_id="void-intent-1",
        account="lx",
        stock_lot_id="stock-1",
        event_type="wheel_event_voided",
        occurred_at_ms=2_200,
        recorded_at_ms=2_201,
        payload={"target_wheel_event_id": intent["event_id"]},
    )
    void_rejection = build_wheel_event(
        event_id="void-rejection-1",
        account="lx",
        stock_lot_id="stock-1",
        event_type="wheel_event_voided",
        occurred_at_ms=2_300,
        recorded_at_ms=2_301,
        payload={"target_wheel_event_id": rejection["event_id"]},
    )

    assert project_wheel_call_intents(
        [intent, void_intent],
        account="lx",
        stock_lot_id="stock-1",
        as_of_ms=3_000,
    ) == []
    candidates = project_wheel_call_linkage_candidates(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": "stock-1",
                "lifecycle_status": "active",
                "integrity_status": "trusted",
                "active_call_lot_ids": [],
                "shares_remaining": 100,
                "batch_generation_hash": "generation-1",
            }
        ],
        [
            {
                "record_id": "call-lot-1",
                "fields": {
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "call",
                    "side": "short",
                    "status": "open",
                    "contracts_open": 1,
                    "multiplier": 100,
                    "source_event_id": "call-open-1",
                },
            }
        ],
        [rejection, void_rejection],
    )
    assert len(candidates) == 1


def test_wheel_put_candidate_enforces_principal_spot_and_abs_delta() -> None:
    branch = {
        "remaining_contracts": 1,
        "multiplier": 100,
        "principal_anchor": 10_010,
        "realized_put_net_pnl_in_current_stage": 0,
        "currency": "USD",
    }
    candidate = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA-PUT-99",
        "strike": 99,
        "spot": 100,
        "delta": -0.30,
        "multiplier": 100,
        "currency": "USD",
        "net_premium": 180,
        "period_net_premium_return": 0.018,
        "annualized_net_premium_return": 0.15,
        "spread_ratio": 0.1,
        "open_interest": 500,
    }
    policy = {"min_abs_delta": 0.25, "max_abs_delta": 0.35}

    accepted = evaluate_wheel_put_candidate(
        branch,
        candidate,
        policy,
        {"basis": "estimated", "amount": 10},
        1,
    )
    over_anchor = evaluate_wheel_put_candidate(
        {**branch, "principal_anchor": 9_900},
        candidate,
        policy,
        {"basis": "estimated", "amount": 10},
        1,
    )
    above_spot = evaluate_wheel_put_candidate(
        branch,
        {**candidate, "strike": 101},
        policy,
        {"basis": "estimated", "amount": 10},
        1,
    )

    assert accepted["accepted"] is True
    assert accepted["projected_assignment_total"] == 9_910
    assert accepted["replenishment_cash_remainder"] == 280
    assert accepted["cash_reservation_amount"] == 9_900
    assert "wheel_put_principal_anchor_exceeded" in over_anchor["reason_codes"]
    assert "wheel_put_strike_above_spot" in above_spot["reason_codes"]


def test_wheel_put_candidate_fails_closed_and_ranks_remainder_first() -> None:
    unavailable = evaluate_wheel_put_candidate(
        {
            "remaining_contracts": 1,
            "multiplier": 100,
            "principal_anchor": 10_010,
            "realized_put_net_pnl_in_current_stage": 0,
            "currency": "USD",
        },
        {
            "strike": 99,
            "spot": None,
            "delta": -0.30,
            "multiplier": 100,
            "currency": "USD",
            "net_premium": 180,
        },
        {"min_abs_delta": 0.25, "max_abs_delta": 0.35},
        {"basis": "estimated", "amount": 10},
        1,
    )
    higher = build_wheel_put_rank_key(
        {
            "replenishment_cash_remainder": 300,
            "period_net_premium_return": 0.01,
            "strike": 99,
            "contract_symbol": "HIGH-REMAINDER",
        }
    )
    lower = build_wheel_put_rank_key(
        {
            "replenishment_cash_remainder": 200,
            "period_net_premium_return": 0.02,
            "strike": 98,
            "contract_symbol": "LOW-REMAINDER",
        }
    )

    assert unavailable["wheel_candidate_status"] == "data_unavailable"
    assert "spot_unavailable" in unavailable["reason_codes"]
    assert tuple(higher["sort_tuple"]) < tuple(lower["sort_tuple"])
