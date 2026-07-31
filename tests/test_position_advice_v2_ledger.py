from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from domain.domain.combo_identity import build_combo_identity, build_combo_identity_intent
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    plan_evidence_allocation,
    terminal_event_id_for,
)
from domain.domain.option_lifecycle import build_lifecycle_case
from domain.domain.position_advice_authority import scope_for
from src.application.ledger.decision_snapshot import decision_state_snapshot
from src.application.ledger.api import (
    record_combo_trade_open,
    record_lifecycle_allocation,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
    initialize_ledger_connection,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)
from src.application.ledger.writer import persist_trade_event_object


def _contract(*, option_type: str = "put") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        position_side="short" if option_type == "put" else "long",
        strike=100 if option_type == "put" else 110,
        expiration_ymd="2026-08-21",
    )


def _open_event(*, event_id: str, option_type: str = "put") -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract(option_type=option_type),
        contracts=2,
        price=2.0,
        currency="USD",
        source="test",
        lot_id=f"lot-{event_id}",
        raw_payload={
            "fields": {
                "broker": "futu",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": option_type,
                "side": "short" if option_type == "put" else "long",
                "contracts": 2,
                "contracts_open": 2,
                "contracts_closed": 0,
                "currency": "USD",
                "strike": 100 if option_type == "put" else 110,
                "expiration_ymd": "2026-08-21",
                "multiplier": 100,
                "premium": 2.0,
                "strategy": "combo_yield",
                "strategy_group_id": "combo:lx:1",
                "leg_role": "funding_put" if option_type == "put" else "participation_call",
            }
        },
    )


def test_repository_enables_foreign_keys_and_creates_v2_additive_tables(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")

    with repo._connect() as conn:  # noqa: SLF001 - connection contract verification
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "trade_lifecycle_allocations" in tables
        assert "strategy_group_identities" in tables
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    injected = sqlite3.connect(":memory:")
    assert initialize_ledger_connection(injected).execute("PRAGMA foreign_keys").fetchone()[0] == 1
    injected.close()


def test_decision_snapshot_reprojects_inside_coherent_ledger_read(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(repo, _open_event(event_id="put-open"))

    trusted = decision_state_snapshot(repo, account="lx", portfolio_scope_id=scope_for("lx"))
    assert trusted["snapshot_status"] == "trusted"
    assert trusted["actionable"] is True
    assert trusted["decision_state_fingerprint"]
    assert trusted["projection_comparison"]["summary"] == {"matched": 1}

    with repo._connect() as conn:  # noqa: SLF001 - deterministic drift fixture
        row = conn.execute(
            "SELECT fields_json FROM position_lots WHERE record_id = ?",
            ("lot-put-open",),
        ).fetchone()
        fields = json.loads(row["fields_json"])
        fields["contracts_open"] = 1
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, sort_keys=True), "lot-put-open"),
        )
        conn.commit()

    untrusted = decision_state_snapshot(repo, account="lx", portfolio_scope_id=scope_for("lx"))
    assert untrusted["snapshot_status"] == "projection_untrusted"
    assert untrusted["actionable"] is False
    assert untrusted["reason_codes"] == ["same_snapshot_projection_mismatch"]


def test_identity_is_insert_only_and_requires_canonical_open_events(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    put = _open_event(event_id="put-open")
    call = _open_event(event_id="call-open", option_type="call")
    persist_trade_event_object(repo, put)
    persist_trade_event_object(repo, call)
    identity = build_combo_identity(
        {
            "group_id": "combo:lx:1",
            "schema_version": "combo_identity.v2",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": "lot-put-open",
            "funding_put_open_event_id": "put-open",
            "funding_put_contract_key": put.contract_key.to_dict(),
            "participation_call_record_id": "lot-call-open",
            "participation_call_open_event_id": "call-open",
            "participation_call_contract_key": call.contract_key.to_dict(),
            "original_contracts": 2,
        }
    )

    assert repo.insert_strategy_group_identity(identity) is True
    assert repo.insert_strategy_group_identity(identity) is False
    assert repo.get_strategy_group_identity("combo:lx:1") == identity
    with pytest.raises(ValueError, match="identity conflict"):
        repo.insert_strategy_group_identity({**identity, "identity_hash": "different"})


def test_lifecycle_case_evidence_allocation_foreign_keys_and_immutability(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    open_event = _open_event(event_id="put-open")
    persist_trade_event_object(repo, open_event)
    lifecycle_case = {
        **build_lifecycle_case(
            account="lx",
            broker="futu",
            contract_key=open_event.contract_key.position_key,
            position_side="short",
            expiration_ymd="2026-08-21",
            market="US",
            target_contracts_by_lot={"lot-put-open": 2},
        ),
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
    }
    assert repo.insert_trade_lifecycle_case_once(lifecycle_case) is True
    assert repo.insert_trade_lifecycle_case_once(lifecycle_case) is False
    with pytest.raises(ValueError, match="immutable conflict"):
        repo.insert_trade_lifecycle_case_once(
            {
                **lifecycle_case,
                "target_contracts_by_lot": {"lot-put-open": 1},
            }
        )

    evidence = {
        "evidence_id": "evidence-1",
        "case_id": None,
        "source_type": "broker_settlement",
        "source_event_id": "settlement-1",
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "contracts": 1,
    }
    assert repo.insert_trade_lifecycle_evidence_once(evidence) is True
    assert repo.bind_trade_lifecycle_evidence_case_once(
        evidence_id="evidence-1",
        case_id=lifecycle_case["case_id"],
    ) is True
    assert repo.bind_trade_lifecycle_evidence_case_once(
        evidence_id="evidence-1",
        case_id=lifecycle_case["case_id"],
    ) is False

    event_id = terminal_event_id_for(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-1",
        target_lot_id="lot-put-open",
        terminal_type="assignment",
        contracts_allocated=1,
    )
    terminal_event = TradeEvent(
        event_id=event_id,
        event_type="assignment",
        event_time_ms=1_800_000_000_000,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={"case_id": lifecycle_case["case_id"], "evidence_id": "evidence-1"},
    )
    repo.upsert_trade_event(terminal_event)
    allocation = {
        "allocation_id": allocation_id_for(
            case_id=lifecycle_case["case_id"],
            evidence_id="evidence-1",
            target_lot_id="lot-put-open",
        ),
        "case_id": lifecycle_case["case_id"],
        "evidence_id": "evidence-1",
        "target_lot_id": "lot-put-open",
        "terminal_type": "assignment",
        "contracts_allocated": 1,
        "canonical_terminal_event_id": event_id,
    }
    assert repo.insert_trade_lifecycle_allocation(allocation) is True
    assert repo.insert_trade_lifecycle_allocation(allocation) is False
    repo.assert_foreign_keys_clean()

    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_trade_lifecycle_allocation(
            {
                **allocation,
                "allocation_id": "missing-event-allocation",
                "target_lot_id": "other-lot",
                "canonical_terminal_event_id": "missing-event",
            }
        )


def test_combo_second_leg_projection_and_identity_are_one_transaction(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    put_event = _open_event(event_id="put-open")
    call_event = _open_event(event_id="call-open", option_type="call")
    persist_trade_event_object(repo, put_event)
    put_leg = {
        "strategy_group_id": "combo:lx:1",
        "strategy": "combo_yield",
        "account": "lx",
        "symbol": "NVDA",
        "leg_role": "funding_put",
        "contracts": 2,
        "open_event_id": "put-open",
        "record_id": "lot-put-open",
        "contract_key": put_event.contract_key.to_dict(),
    }
    call_leg = {
        "strategy_group_id": "combo:lx:1",
        "strategy": "combo_yield",
        "account": "lx",
        "symbol": "NVDA",
        "leg_role": "participation_call",
        "contracts": 2,
        "open_event_id": "call-open",
        "record_id": "lot-call-open",
        "contract_key": call_event.contract_key.to_dict(),
    }
    intent = build_combo_identity_intent(first_leg=put_leg, second_leg=call_leg)

    result = record_combo_trade_open(
        repo,
        event=call_event,
        combo_identity_intent=intent,
    )

    assert result["event_created"] is True
    assert result["identity_created"] is True
    assert repo.get_strategy_group_identity("combo:lx:1") == result["identity"]
    assert {item["event_id"] for item in repo.list_trade_events()} == {"put-open", "call-open"}
    assert {item["record_id"] for item in repo.list_position_lots()} == {
        "lot-put-open",
        "lot-call-open",
    }

    replay = record_combo_trade_open(
        repo,
        event=call_event,
        combo_identity_intent=intent,
    )
    assert replay["event_created"] is False
    assert replay["identity_created"] is False


def test_duplicate_second_leg_without_identity_requires_review_not_backfill(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    put_event = _open_event(event_id="put-open")
    call_event = _open_event(event_id="call-open", option_type="call")
    persist_trade_event_object(repo, put_event)
    persist_trade_event_object(repo, call_event)
    intent = build_combo_identity_intent(
        first_leg={
            "strategy_group_id": "combo:lx:1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "leg_role": "funding_put",
            "contracts": 2,
            "open_event_id": "put-open",
            "record_id": "lot-put-open",
            "contract_key": put_event.contract_key.to_dict(),
        },
        second_leg={
            "strategy_group_id": "combo:lx:1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "leg_role": "participation_call",
            "contracts": 2,
            "open_event_id": "call-open",
            "record_id": "lot-call-open",
            "contract_key": call_event.contract_key.to_dict(),
        },
    )

    with pytest.raises(ValueError, match="identity_missing_for_existing_second_leg"):
        record_combo_trade_open(
            repo,
            event=call_event,
            combo_identity_intent=intent,
        )
    assert repo.get_strategy_group_identity("combo:lx:1") is None


def test_lifecycle_evidence_event_projection_and_allocation_are_atomic(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    open_event = _open_event(event_id="put-open")
    persist_trade_event_object(repo, open_event)
    lifecycle_case = {
        **build_lifecycle_case(
            account="lx",
            broker="futu",
            contract_key=open_event.contract_key.position_key,
            position_side="short",
            expiration_ymd="2026-08-21",
            market="US",
            target_contracts_by_lot={"lot-put-open": 2},
        ),
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
    }
    repo.insert_trade_lifecycle_case_once(lifecycle_case)
    evidence = {
        "evidence_id": "evidence-1",
        "case_id": None,
        "source_type": "broker_settlement",
        "source_event_id": "settlement-1",
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "contracts": 1,
    }
    plan = plan_evidence_allocation(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-1",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-put-open": 2},
        target_lot_id="lot-put-open",
    )
    assert plan.status == "planned"
    allocation = dict(plan.allocations[0])
    event = TradeEvent(
        event_id=allocation["canonical_terminal_event_id"],
        event_type="assignment",
        event_time_ms=1_800_000_000_000,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={
            "case_id": lifecycle_case["case_id"],
            "evidence_id": "evidence-1",
            "allocation_id": allocation["allocation_id"],
            "contracts": 1,
        },
    )

    result = record_lifecycle_allocation(
        repo,
        case_id=lifecycle_case["case_id"],
        evidence=evidence,
        terminal_events=[event],
        allocations=[allocation],
        derived_status="partially_resolved",
        derived_summary={"remaining_contracts_by_lot": {"lot-put-open": 1}},
    )

    assert result["evidence_created"] is True
    assert result["terminal_events_created"] == [True]
    assert result["allocations_created"] == [True]
    assert repo.get_position_lot_fields("lot-put-open")["contracts_open"] == 1
    assert repo.list_trade_lifecycle_allocations(case_id=lifecycle_case["case_id"]) == [allocation]

    replay = record_lifecycle_allocation(
        repo,
        case_id=lifecycle_case["case_id"],
        evidence=evidence,
        terminal_events=[event],
        allocations=[allocation],
        derived_status="partially_resolved",
        derived_summary={"remaining_contracts_by_lot": {"lot-put-open": 1}},
    )
    assert replay["evidence_created"] is False
    assert replay["terminal_events_created"] == [False]
    assert replay["allocations_created"] == [False]
    assert repo.get_position_lot_fields("lot-put-open")["contracts_open"] == 1

    evidence_2 = {
        **evidence,
        "evidence_id": "evidence-2",
        "source_event_id": "settlement-2",
    }
    plan_2 = plan_evidence_allocation(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-2",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-put-open": 1},
        target_lot_id="lot-put-open",
    )
    allocation_2 = dict(plan_2.allocations[0])
    event_2 = TradeEvent(
        event_id=allocation_2["canonical_terminal_event_id"],
        event_type="assignment",
        event_time_ms=1_800_000_000_001,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={
            "case_id": lifecycle_case["case_id"],
            "evidence_id": "evidence-2",
            "allocation_id": allocation_2["allocation_id"],
            "contracts": 1,
        },
    )
    completed = record_lifecycle_allocation(
        repo,
        case_id=lifecycle_case["case_id"],
        evidence=evidence_2,
        terminal_events=[event_2],
        allocations=[allocation_2],
        derived_status="ledger_written",
        derived_summary={"remaining_contracts_by_lot": {"lot-put-open": 0}},
    )
    assert completed["terminal_events_created"] == [True]
    assert repo.get_position_lot_fields("lot-put-open")["contracts_open"] == 0
    assert repo.get_trade_lifecycle_case(lifecycle_case["case_id"])["status"] == "ledger_written"
    assert len(
        repo.list_trade_lifecycle_allocations(case_id=lifecycle_case["case_id"])
    ) == 2


@pytest.mark.parametrize(
    ("variant", "expected_error"),
    (
        ("price", "stock_settlement_price_mismatch"),
        ("futu_account", "stock_settlement_futu_account_mismatch"),
        ("ambiguous", "ambiguous_lifecycle_case_match"),
    ),
)
def test_lifecycle_writer_revalidates_broker_settlement_pair(
    tmp_path: Path,
    variant: str,
    expected_error: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    open_event = _open_event(event_id="put-open")
    persist_trade_event_object(repo, open_event)
    lifecycle_case = {
        **build_lifecycle_case(
            account="lx",
            futu_account_id="1001",
            broker="futu",
            contract_key=open_event.contract_key.position_key,
            position_side="short",
            expiration_ymd="2026-08-21",
            market="US",
            target_contracts_by_lot={"lot-put-open": 2},
        ),
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
    }
    assert repo.insert_trade_lifecycle_case_once(lifecycle_case)
    if variant == "ambiguous":
        assert repo.insert_trade_lifecycle_case_once(
            {
                **lifecycle_case,
                "case_id": "duplicate-case",
                "case_key": "duplicate-case",
            }
        )

    anchor_source = "futu:lx:1001:option-anchor"
    anchor = {
        "evidence_id": "option-anchor",
        "case_id": lifecycle_case["case_id"],
        "source_type": "futu_broker_deal",
        "source_event_id": anchor_source,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": "2026-08-21",
        "contracts": 1,
        "price": 0,
        "event_time_ms": 1_800_000_000_000,
    }
    assert repo.insert_trade_lifecycle_evidence_once(anchor)
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=anchor_source,
            case_id=lifecycle_case["case_id"],
            owner_evidence_id=anchor["evidence_id"],
            source_role="option_anchor",
            economic_payload=anchor,
        )
    )

    stock_futu_account_id = (
        "2002" if variant == "futu_account" else "1001"
    )
    evidence = {
        "evidence_id": "settlement-pair",
        "case_id": lifecycle_case["case_id"],
        "source_type": "broker_settlement_pair",
        "source_event_id": (
            "futu:lx:1001:option-anchor|"
            f"futu:lx:{stock_futu_account_id}:stock-settlement"
        ),
        "evidence_type": "assignment",
        "terminal_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": "2026-08-21",
        "contracts": 1,
        "event_time_ms": 1_800_000_000_001,
        "option_event_time_ms": 1_800_000_000_000,
        "stock_settlement": {
            "source_event_id": (
                f"futu:lx:{stock_futu_account_id}:stock-settlement"
            ),
            "futu_account_id": stock_futu_account_id,
            "symbol": "NVDA",
            "side": "buy",
            "shares": 100,
            "price": 100.01 if variant == "price" else 100,
            "event_time_ms": 1_800_000_000_001,
        },
    }
    plan = plan_evidence_allocation(
        case_id=lifecycle_case["case_id"],
        evidence_id=evidence["evidence_id"],
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-put-open": 2},
        target_lot_id="lot-put-open",
    )
    allocation = dict(plan.allocations[0])
    event = TradeEvent(
        event_id=allocation["canonical_terminal_event_id"],
        event_type="assignment",
        event_time_ms=1_800_000_000_001,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={
            "case_id": lifecycle_case["case_id"],
            "evidence_id": evidence["evidence_id"],
            "allocation_id": allocation["allocation_id"],
            "contracts": 1,
        },
    )

    with pytest.raises(ValueError, match=expected_error):
        record_lifecycle_allocation(
            repo,
            case_id=lifecycle_case["case_id"],
            evidence=evidence,
            terminal_events=[event],
            allocations=[allocation],
            derived_status="partially_resolved",
            derived_summary={},
        )

    assert repo.get_trade_lifecycle_evidence(
        evidence["evidence_id"]
    ) is None
    assert repo.list_trade_lifecycle_allocations(
        case_id=lifecycle_case["case_id"]
    ) == []
    assert repo.get_position_lot_fields(
        "lot-put-open"
    )["contracts_open"] == 2


def test_lifecycle_atomic_validation_failure_rolls_back_every_fact(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    open_event = _open_event(event_id="put-open")
    persist_trade_event_object(repo, open_event)
    lifecycle_case = {
        **build_lifecycle_case(
            account="lx",
            broker="futu",
            contract_key=open_event.contract_key.position_key,
            position_side="short",
            expiration_ymd="2026-08-21",
            market="US",
            target_contracts_by_lot={"lot-put-open": 2},
        ),
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
    }
    repo.insert_trade_lifecycle_case_once(lifecycle_case)
    evidence = {
        "evidence_id": "evidence-bad",
        "case_id": None,
        "source_type": "broker_settlement",
        "source_event_id": "settlement-bad",
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "contracts": 1,
    }
    allocation_id = allocation_id_for(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-bad",
        target_lot_id="lot-put-open",
    )
    terminal_event_id = terminal_event_id_for(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-bad",
        target_lot_id="lot-put-open",
        terminal_type="assignment",
        contracts_allocated=1,
    )
    allocation = {
        "allocation_id": allocation_id,
        "case_id": lifecycle_case["case_id"],
        "evidence_id": "evidence-bad",
        "target_lot_id": "lot-put-open",
        "terminal_type": "assignment",
        "contracts_allocated": 1,
        "canonical_terminal_event_id": terminal_event_id,
    }
    mismatched_event = TradeEvent(
        event_id=terminal_event_id,
        event_type="assignment",
        event_time_ms=1_800_000_000_000,
        contract_key=open_event.contract_key,
        contracts=2,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={
            "case_id": lifecycle_case["case_id"],
            "evidence_id": "evidence-bad",
            "allocation_id": allocation_id,
            "contracts": 2,
        },
    )

    with pytest.raises(ValueError, match="allocation and terminal event mismatch"):
        record_lifecycle_allocation(
            repo,
            case_id=lifecycle_case["case_id"],
            evidence=evidence,
            terminal_events=[mismatched_event],
            allocations=[allocation],
            derived_status="partially_resolved",
            derived_summary={},
        )
    assert repo.get_trade_lifecycle_evidence("evidence-bad") is None
    assert all(
        item["event_id"] != terminal_event_id
        for item in repo.list_trade_events()
    )
    assert repo.list_trade_lifecycle_allocations(case_id=lifecycle_case["case_id"]) == []
    assert repo.get_position_lot_fields("lot-put-open")["contracts_open"] == 2


def test_lifecycle_writer_rejects_forged_ids_and_target_quantity_drift(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    open_event = _open_event(event_id="put-open")
    persist_trade_event_object(repo, open_event)
    lifecycle_case = {
        **build_lifecycle_case(
            account="lx",
            broker="futu",
            contract_key=open_event.contract_key.position_key,
            position_side="short",
            expiration_ymd="2026-08-21",
            market="US",
            target_contracts_by_lot={"lot-put-open": 2},
        ),
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
    }
    repo.insert_trade_lifecycle_case_once(lifecycle_case)
    evidence = {
        "evidence_id": "evidence-1",
        "case_id": None,
        "source_type": "broker_settlement",
        "source_event_id": "settlement-1",
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "contracts": 1,
    }
    plan = plan_evidence_allocation(
        case_id=lifecycle_case["case_id"],
        evidence_id="evidence-1",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-put-open": 2},
        target_lot_id="lot-put-open",
    )
    allocation = dict(plan.allocations[0])
    event = TradeEvent(
        event_id=allocation["canonical_terminal_event_id"],
        event_type="assignment",
        event_time_ms=1_800_000_000_000,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        source="lifecycle_reconciliation",
        target_lot_id="lot-put-open",
        raw_payload={
            "case_id": lifecycle_case["case_id"],
            "evidence_id": "evidence-1",
            "allocation_id": allocation["allocation_id"],
            "contracts": 1,
        },
    )

    with pytest.raises(ValueError, match="allocation id is not deterministic"):
        record_lifecycle_allocation(
            repo,
            case_id=lifecycle_case["case_id"],
            evidence=evidence,
            terminal_events=[event],
            allocations=[{**allocation, "allocation_id": "f" * 64}],
            derived_status="partially_resolved",
            derived_summary={},
        )
    assert repo.get_trade_lifecycle_evidence("evidence-1") is None

    with repo._connect() as conn:  # noqa: SLF001 - unexplained quantity drift fixture
        row = conn.execute(
            "SELECT fields_json FROM position_lots WHERE record_id = ?",
            ("lot-put-open",),
        ).fetchone()
        fields = json.loads(row["fields_json"])
        fields["contracts_open"] = 1
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, sort_keys=True), "lot-put-open"),
        )
        conn.commit()

    with pytest.raises(ValueError, match="target_lot_quantity_drift"):
        record_lifecycle_allocation(
            repo,
            case_id=lifecycle_case["case_id"],
            evidence=evidence,
            terminal_events=[event],
            allocations=[allocation],
            derived_status="partially_resolved",
            derived_summary={},
        )
    assert repo.get_trade_lifecycle_evidence("evidence-1") is None
    assert repo.list_trade_lifecycle_allocations(case_id=lifecycle_case["case_id"]) == []
