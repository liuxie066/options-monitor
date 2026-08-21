from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.ledger.api import (
    lifecycle_option_close_anchor_facts,
)
from src.application.ledger.decision_snapshot import (
    decision_state_snapshot,
)
from src.application.ledger.lifecycle_migration import (
    apply_lifecycle_migration_manifest,
    build_lifecycle_migration_inventory,
    select_lifecycle_migration_targets,
)
from src.application.ledger.lifecycle_overlay import (
    lifecycle_case_resolution as resolved_lifecycle_case,
    resolve_lifecycle_account_rows,
)
from src.application.ledger.notification_outbox import (
    build_notification_intent,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.auto_intake import (
    _lifecycle_delivery_status,
    _refresh_lifecycle_delivery_status,
)
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)
from src.application.positions.context_builder import (
    build_lifecycle_read_models_from_decision_snapshot,
)
from src.application.trades.lifecycle_outbox import (
    CLAIM_LEASE_MS,
    QUIET_WINDOW_MS,
    build_notification_batch_route,
    claim_next_notification,
    complete_notification_attempt,
    dispatch_notification_batch_once,
    enqueue_notification_intent,
    mark_notification_send_started,
    reconcile_unknown_notification,
    recover_stale_notifications,
)
from src.application.trades.close_reason_evidence import (
    build_lifecycle_timing_policy,
)
from src.application.trades.manual_lifecycle_resolution import (
    resolve_lifecycle_manually,
)


EXPIRATION_YMD = "2026-08-21"


def _open_event() -> TradeEvent:
    contract = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd=EXPIRATION_YMD,
    )
    return TradeEvent(
        event_id="open-1",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=contract,
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="lot-1",
        raw_payload={
            "fields": {
                "broker": "futu",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "contracts": 1,
                "contracts_open": 1,
                "contracts_closed": 0,
                "currency": "USD",
                "strike": 100,
                "expiration_ymd": EXPIRATION_YMD,
                "multiplier": 100,
            }
        },
    )


def _case_with_option_anchor(
    tmp_path: Path,
    *,
    bind_timing: bool = True,
) -> tuple[SQLiteOptionPositionsRepository, str, int]:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    persist_trade_event_object(repo, _open_event())
    observed_at_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observed_at_ms is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observed_at_ms,
    )["created_case_ids"][0]
    option_source = "futu:lx:1001:option-close-1"
    option_evidence = {
        "evidence_id": "option-anchor-1",
        "case_id": case_id,
        "source_type": "futu_broker_deal",
        "source_event_id": option_source,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "target_lot_id": "lot-1",
        "price": 0,
        "event_time_ms": observed_at_ms,
        "received_at_ms": observed_at_ms + 100,
    }
    assert repo.insert_trade_lifecycle_evidence_once(
        option_evidence
    )
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=option_source,
            case_id=case_id,
            owner_evidence_id="option-anchor-1",
            source_role="option_anchor",
            economic_payload=option_evidence,
        )
    )
    if bind_timing:
        assert repo.insert_trade_lifecycle_timing_policy_once(
            build_lifecycle_timing_policy(
            case_id=case_id,
            market="US",
            expiration_ymd=EXPIRATION_YMD,
            contract_metadata={
                "settlement_style": "physical",
                "underlying_security_type": "equity",
                "last_trade_cutoff_ms": observed_at_ms - 1,
                "last_trade_cutoff_source": (
                    "instrument_policy_registry"
                ),
            },
            trading_days=[
                {"date": "2026-08-21", "type": "TRADING"},
                {"date": "2026-08-24", "type": "TRADING"},
                {"date": "2026-08-25", "type": "TRADING"},
            ],
            calendar_source="test_calendar",
            calendar_observed_at_ms=observed_at_ms,
            )
        )
    return repo, case_id, observed_at_ms


def test_coherent_lifecycle_reader_resolves_direct_anchor(
    tmp_path: Path,
) -> None:
    repo, case_id, _observed_at_ms = _case_with_option_anchor(
        tmp_path,
    )

    rows = repo.read_lifecycle_case_rows(case_id=case_id)
    resolution = resolve_lifecycle_account_rows(rows)
    case_resolution = resolved_lifecycle_case(
        resolution,
        case_id=case_id,
    )

    assert rows["requested_lifecycle_case_id"] == case_id
    assert len(rows["account_lifecycle_source_consumptions"]) == 1
    assert len(rows["account_lifecycle_timing_policies"]) == 1
    assert case_resolution is not None
    assert case_resolution["status"] == "direct"
    assert case_resolution["effective_reservations_by_lot"] == {
        "lot-1": 1
    }


def _legacy_terminal_mapping_fixture(
    tmp_path: Path,
    *,
    stock_price: int = 100,
    adopted_event: bool = False,
    case_multiplier: int = 100,
) -> tuple[
    SQLiteOptionPositionsRepository,
    str,
    dict,
    str,
]:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    open_event = _open_event()
    persist_trade_event_object(repo, open_event)
    observed_at_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observed_at_ms is not None
    case_id = "legacy-terminal-case-1"
    terminal_event_id = "legacy-assignment-event-1"
    option_raw = {
        "internal_account": "lx",
        "futu_account_id": "1001",
        "deal_id": "legacy-option-deal-1",
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "currency": "USD",
        "multiplier": 100,
        "price": 0,
        "trade_time_ms": observed_at_ms,
        "visible_account_fields": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
        },
        "raw_payload": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
            "deal_id": "legacy-option-deal-1",
        },
    }
    assert repo.upsert_trade_lifecycle_case(
        {
            "case_id": case_id,
            "case_key": "legacy-terminal-case-key-1",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": EXPIRATION_YMD,
            "currency": "USD",
            "multiplier": case_multiplier,
            "contracts": 1,
            "decision_type": "assignment",
            "status": "ledger_written",
            "target_lot_ids": ["lot-1"],
            "adopted_event_ids": (
                [terminal_event_id] if adopted_event else []
            ),
            "raw": {"option_deal": option_raw},
        }
    )
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "legacy-option-evidence-1",
            "case_id": case_id,
            "source_type": "futu_trade_push",
            "source_event_id": "legacy-option-deal-1",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "NVDA",
            "trade_time_ms": observed_at_ms,
            "raw": option_raw,
        }
    )
    stock_time_ms = observed_at_ms + 2_000
    stock_raw = {
        "internal_account": "lx",
        "futu_account_id": "1001",
        "deal_id": "legacy-stock-deal-1",
        "symbol": "NVDA",
        "contracts": 100,
        "price": stock_price,
        "side": "buy",
        "trade_time_ms": stock_time_ms,
        "visible_account_fields": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
        },
        "raw_payload": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
            "deal_id": "legacy-stock-deal-1",
        },
    }
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "legacy-stock-evidence-1",
            "case_id": None,
            "source_type": "futu_trade_push",
            "source_event_id": "legacy-stock-deal-1",
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "symbol": "NVDA",
            "side": "buy",
            "stock_qty": 100,
            "stock_price": stock_price,
            "trade_time_ms": stock_time_ms,
            "raw": stock_raw,
        }
    )
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id=terminal_event_id,
            event_type="assignment",
            event_time_ms=stock_time_ms,
            contract_key=open_event.contract_key,
            contracts=1,
            price=0,
            currency="USD",
            source="legacy_lifecycle",
            multiplier=100,
            target_lot_id="lot-1",
            raw_payload={
                **(
                    {}
                    if adopted_event
                    else {"case_id": case_id}
                ),
                "target_lot_id": "lot-1",
            },
        ),
    )
    mapping = {
        "schema_version": "lifecycle_explicit_mapping.v1",
        "rows": [
            {
                "legacy_case_id": case_id,
                "disposition": "terminal_frozen",
                "canonical_contract": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "position_side": "short",
                    "strike": 100,
                    "expiration_ymd": EXPIRATION_YMD,
                    "currency": "USD",
                    "multiplier": 100,
                },
                "target_contracts_by_lot": {"lot-1": 1},
                "terminal_type": "assignment",
                "terminal_event_ids": [terminal_event_id],
                "evidence_sources": [
                    {
                        "evidence_id": "legacy-option-evidence-1",
                        "source_key": (
                            "futu:lx:1001:legacy-option-deal-1"
                        ),
                        "source_role": "option_anchor",
                    },
                    {
                        "evidence_id": "legacy-stock-evidence-1",
                        "source_key": (
                            "futu:lx:1001:legacy-stock-deal-1"
                        ),
                        "source_role": "stock_settlement",
                    },
                ],
                "settlement_window": {
                    "start_ms": observed_at_ms,
                    "end_ms": observed_at_ms + 10_000,
                    "source": "frozen_broker_settlement_window",
                },
            }
        ],
    }
    return repo, case_id, mapping, terminal_event_id


def test_source_claim_hash_ignores_push_poll_transport() -> None:
    base = {
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "price": 0,
        "event_time_ms": 1_800_000_000_000,
    }
    push = build_source_consumption_claim(
        source_key="futu:lx:1001:deal-1",
        case_id="case-1",
        owner_evidence_id="evidence-1",
        source_role="option_anchor",
        economic_payload={
            **base,
            "_trade_intake_source": {"transport": "push"},
        },
    )
    poll = build_source_consumption_claim(
        source_key="futu:lx:1001:deal-1",
        case_id="case-1",
        owner_evidence_id="evidence-1",
        source_role="option_anchor",
        economic_payload={
            **base,
            "_trade_intake_source": {"transport": "poll"},
        },
    )
    changed = build_source_consumption_claim(
        source_key="futu:lx:1001:deal-1",
        case_id="case-1",
        owner_evidence_id="evidence-1",
        source_role="option_anchor",
        economic_payload={**base, "price": 1},
    )

    assert push == poll
    assert (
        push["source_payload_hash"]
        != changed["source_payload_hash"]
    )


def test_manual_correction_is_atomic_void_aware_and_idempotent(
    tmp_path: Path,
) -> None:
    repo, case_id, observed_at_ms = _case_with_option_anchor(
        tmp_path
    )
    expiry = reconcile_lifecycle_evidence(
        repo,
        evidence={
            "evidence_id": "expiry-1",
            "source_type": "broker_settlement_observation",
            "source_event_id": "observation-1",
            "evidence_type": "expire_close",
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": EXPIRATION_YMD,
            "contracts": 1,
            "event_time_ms": observed_at_ms + 1,
            "target_lot_id": "lot-1",
        },
        case_id=case_id,
        apply_changes=True,
        now_ms=observed_at_ms + 1,
    )
    assert expiry.status == "applied"
    expire_event_id = str(
        expiry.allocation_plan[0]["canonical_terminal_event_id"]
    )
    expire_event = next(
        item
        for item in repo.list_trade_events()
        if item.get("event_id") == expire_event_id
    )
    expire_cash = expire_event["raw_payload"]["cash_conversions"][
        "option_trade_cash_gross"
    ]
    assert expire_cash["status"] == "observed"
    assert expire_cash["method"] == "zero_identity"
    assert expire_cash["amount_cny"] == "0"
    stock_source = "futu:lx:1001:stock-deal-1"
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "stock-evidence-1",
            "case_id": None,
            "source_type": "futu_broker_deal",
            "source_event_id": stock_source,
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "side": "buy",
            "stock_qty": 100,
            "stock_price": 100,
            "trade_time_ms": observed_at_ms + 2,
            "order_id": "order-stock-1",
            "raw": {
                "futu_account_id": "1001",
                "symbol": "NVDA",
                "side": "buy",
                "contracts": 100,
                "price": 100,
                "trade_time_ms": observed_at_ms + 2,
            },
        }
    )
    current_revision = int(
        (
            repo.get_trade_lifecycle_case(case_id)[
                "derived_summary"
            ]
        )["resolution_revision"]
    )

    preview = resolve_lifecycle_manually(
        repo,
        case_id=case_id,
        expected_revision=current_revision,
        reason="assignment",
        broker_ref=stock_source,
        note="late broker assignment evidence",
        void_terminal_event_id=expire_event_id,
        apply_changes=False,
        now_ms=observed_at_ms + 3,
    )
    assert preview["status"] == "dry_run"
    assert repo.get_position_lot_fields("lot-1")[
        "contracts_open"
    ] == 0

    applied = resolve_lifecycle_manually(
        repo,
        case_id=case_id,
        expected_revision=current_revision,
        reason="assignment",
        broker_ref=stock_source,
        note="late broker assignment evidence",
        void_terminal_event_id=expire_event_id,
        apply_changes=True,
        now_ms=observed_at_ms + 3,
    )
    assert applied["status"] == "applied"
    assert applied["next_revision"] == current_revision + 1
    assert repo.get_position_lot_fields("lot-1")[
        "contracts_open"
    ] == 0
    allocations = repo.list_trade_lifecycle_allocations(
        case_id=case_id
    )
    assert {item["terminal_type"] for item in allocations} == {
        "expire_close",
        "assignment",
    }
    void_targets = {
        str(item.get("target_event_id") or "")
        for item in repo.list_trade_events()
        if item.get("event_type") == "void"
    }
    assert expire_event_id in void_targets
    correction_rows = [
        item
        for item in repo.list_trade_lifecycle_notifications(
            case_id=case_id
        )
        if item["transition_type"] == "resolution_corrected"
    ]
    assert len(correction_rows) == 1
    notification_count = len(
        repo.list_trade_lifecycle_notifications(case_id=case_id)
    )

    repeated = resolve_lifecycle_manually(
        repo,
        case_id=case_id,
        expected_revision=current_revision,
        reason="assignment",
        broker_ref=stock_source,
        note="late broker assignment evidence",
        void_terminal_event_id=expire_event_id,
        apply_changes=True,
        now_ms=observed_at_ms + 3,
    )
    assert repeated["status"] == "idempotent"
    assert len(
        repo.list_trade_lifecycle_notifications(case_id=case_id)
    ) == notification_count


def test_migration_manifest_requires_explicit_selection_and_replays_noop(
    tmp_path: Path,
) -> None:
    repo, case_id, _observed_at_ms = _case_with_option_anchor(
        tmp_path
    )
    inventory = build_lifecycle_migration_inventory(repo)
    target_key = f"lifecycle:{case_id}"
    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[target_key],
    )

    dry_run = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=False,
    )
    assert dry_run["would_apply_target_keys"] == [target_key]
    assert repo.list_trade_lifecycle_migration_receipts() == []

    applied = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )
    assert applied["applied_count"] == 1
    suppressed = [
        item
        for item in repo.list_trade_lifecycle_notifications(
            case_id=case_id
        )
        if item["status"] == "suppressed"
    ]
    assert len(suppressed) == 1

    replay = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )
    assert replay["applied_count"] == 0
    assert replay["existing_count"] == 1
    assert len(
        repo.list_trade_lifecycle_migration_receipts()
    ) == 1


def test_migration_inventory_blocks_v2_without_timing_policy(
    tmp_path: Path,
) -> None:
    repo, case_id, _observed_at_ms = _case_with_option_anchor(
        tmp_path,
        bind_timing=False,
    )

    inventory = build_lifecycle_migration_inventory(repo)
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )

    assert row["mapping_status"] == "needs_review"
    assert (
        "lifecycle_timing_policy_missing"
        in row["review_reason_codes"]
    )


def test_migration_upgrades_unique_legacy_case_with_bridge(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    persist_trade_event_object(repo, _open_event())
    observed_at_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observed_at_ms is not None
    contract = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd=EXPIRATION_YMD,
    )
    legacy_id = "legacy-case-1"
    assert repo.upsert_trade_lifecycle_case(
        {
            "schema_version": "lifecycle_case.v1",
            "case_id": legacy_id,
            "case_key": legacy_id,
            "account": "lx",
            "broker": "futu",
            "contract_key": contract.position_key,
            "position_side": "short",
            "expiration_ymd": EXPIRATION_YMD,
            "market": "US",
            "symbol": "NVDA",
            "option_type": "put",
            "strike": 100,
            "currency": "USD",
            "multiplier": 100,
            "target_contracts_by_lot": {"lot-1": 1},
            "status": "waiting_settlement_evidence",
        }
    )
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "legacy-anchor-1",
            "case_id": legacy_id,
            "source_type": "futu_broker_deal",
            "source_event_id": (
                "futu:lx:1001:legacy-option-close-1"
            ),
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": EXPIRATION_YMD,
            "contracts": 1,
            "price": 0,
            "event_time_ms": observed_at_ms,
        }
    )
    legacy_policy = build_lifecycle_timing_policy(
        case_id=legacy_id,
        market="US",
        expiration_ymd=EXPIRATION_YMD,
        contract_metadata={
            "settlement_style": "physical",
            "underlying_security_type": "equity",
            "last_trade_cutoff_ms": observed_at_ms - 1,
            "last_trade_cutoff_source": (
                "instrument_policy_registry"
            ),
        },
        trading_days=[
            {"date": "2026-08-21", "type": "TRADING"},
            {"date": "2026-08-24", "type": "TRADING"},
            {"date": "2026-08-25", "type": "TRADING"},
        ],
        calendar_source="test_calendar",
        calendar_observed_at_ms=observed_at_ms,
    )
    assert repo.insert_trade_lifecycle_timing_policy_once(
        legacy_policy
    )

    inventory = build_lifecycle_migration_inventory(repo)
    target_key = f"lifecycle:{legacy_id}"
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == target_key
    )
    assert row["mapping_status"] == "exact"
    canonical_case_id = row["legacy_upgrade"][
        "canonical_case"
    ]["case_id"]
    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[target_key],
    )
    applied = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )

    assert applied["applied_count"] == 1
    legacy = repo.get_trade_lifecycle_case(legacy_id)
    assert legacy is not None
    assert legacy["status"] == "superseded"
    assert legacy["superseded_by_case_id"] == canonical_case_id
    canonical = repo.get_trade_lifecycle_case(
        canonical_case_id
    )
    assert canonical is not None
    assert canonical["schema_version"] == "lifecycle_case.v2"
    assert canonical["futu_account_id"] == "1001"
    assert repo.get_trade_lifecycle_timing_policy(
        canonical_case_id
    ) is not None
    bridges = repo.list_trade_lifecycle_evidence(
        case_id=canonical_case_id
    )
    assert [item["evidence_type"] for item in bridges] == [
        "migration_bridge"
    ]
    anchor_facts = lifecycle_option_close_anchor_facts(
        repo,
        case_id=canonical_case_id,
    )
    assert anchor_facts["status"] == "bridged"
    assert anchor_facts["reason_codes"] == []
    assert len(anchor_facts["anchors"]) == 1
    assert anchor_facts["anchors"][0]["source_owner_case_id"] == legacy_id
    assert anchor_facts["anchors"][0]["case_id"] == canonical_case_id
    assert anchor_facts["anchors"][0]["target_contracts_by_lot"] == {"lot-1": 1}
    model = lifecycle_case_read_model(
        repo,
        case_id=canonical_case_id,
        now_ms=observed_at_ms,
    )
    assert model["reason_state"] == "cause_pending"
    assert "evidence_without_allocation" not in model["lifecycle_reason_codes"]


def test_explicit_terminal_frozen_mapping_only_links_existing_facts(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, terminal_event_id = (
        _legacy_terminal_mapping_fixture(tmp_path)
    )
    before_events = repo.list_trade_events()
    before_lots = repo.list_position_lots()
    before_cases = repo.list_trade_lifecycle_cases()

    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    target_key = f"lifecycle:{case_id}"
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == target_key
    )
    assert row["mapping_status"] == "exact", row[
        "review_reason_codes"
    ]
    assert row["legacy_terminal_frozen"] is True
    assert row["terminal_event_ids"] == [terminal_event_id]
    assert len(row["planned_source_claims"]) == 2
    assert row["planned_evidence_bindings"] == [
        {
            "evidence_id": "legacy-stock-evidence-1",
            "case_id": case_id,
        }
    ]

    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[target_key],
    )
    applied = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )

    assert applied["applied_count"] == 1
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert repo.list_trade_lifecycle_cases() == before_cases
    assert len(
        repo.list_trade_lifecycle_source_consumptions(
            case_id=case_id
        )
    ) == 2
    assert (
        repo.get_trade_lifecycle_evidence(
            "legacy-stock-evidence-1"
        )["case_id"]
        == case_id
    )
    assert repo.get_trade_lifecycle_timing_policy(case_id) is None
    outbox = repo.list_trade_lifecycle_notifications(
        case_id=case_id
    )
    assert len(outbox) == 1
    assert outbox[0]["status"] == "suppressed"

    replay = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )
    assert replay["applied_count"] == 0
    assert replay["existing_count"] == 1


def test_explicit_terminal_mapping_fails_closed_without_terminal_event(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, _terminal_event_id = (
        _legacy_terminal_mapping_fixture(tmp_path)
    )
    mapping["rows"][0]["terminal_event_ids"] = []

    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )

    assert row["mapping_status"] == "needs_review"
    assert "explicit_terminal_event_missing" in (
        row["review_reason_codes"]
    )
    assert "explicit_terminal_quantity_mismatch" in (
        row["review_reason_codes"]
    )


def test_explicit_terminal_mapping_accepts_case_adopted_event(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, terminal_event_id = (
        _legacy_terminal_mapping_fixture(
            tmp_path,
            adopted_event=True,
        )
    )

    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )

    assert row["mapping_status"] == "exact", row[
        "review_reason_codes"
    ]
    assert row["terminal_event_ids"] == [terminal_event_id]


def test_explicit_terminal_mapping_requires_exact_multiplier_exception(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, _terminal_event_id = (
        _legacy_terminal_mapping_fixture(
            tmp_path,
            case_multiplier=200,
        )
    )
    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    blocked = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )
    assert blocked["mapping_status"] == "needs_review"
    assert "explicit_case_contract_mismatch" in (
        blocked["review_reason_codes"]
    )

    mapping["rows"][0]["legacy_case_exceptions"] = {
        "multiplier": {
            "legacy_value": "200",
            "canonical_value": "100",
            "reason": (
                "canonical terminal event and target lot agree"
            ),
        }
    }
    accepted = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    row = next(
        item
        for item in accepted["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )
    assert row["mapping_status"] == "exact", row[
        "review_reason_codes"
    ]


def test_explicit_assignment_mapping_rejects_cross_account_and_window(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, _terminal_event_id = (
        _legacy_terminal_mapping_fixture(tmp_path)
    )
    mapping["rows"][0]["evidence_sources"][1][
        "source_key"
    ] = "futu:sy:2002:legacy-stock-deal-1"
    mapping["rows"][0]["settlement_window"]["end_ms"] = (
        mapping["rows"][0]["settlement_window"]["start_ms"] + 1
    )

    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )

    assert row["mapping_status"] == "needs_review"
    assert "explicit_broker_source_identity_mismatch" in (
        row["review_reason_codes"]
    )
    assert "explicit_stock_settlement_window_mismatch" in (
        row["review_reason_codes"]
    )


def test_explicit_assignment_mapping_rejects_wrong_settlement_price(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, _terminal_event_id = (
        _legacy_terminal_mapping_fixture(
            tmp_path,
            stock_price=99,
        )
    )

    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == f"lifecycle:{case_id}"
    )

    assert row["mapping_status"] == "needs_review"
    assert "explicit_stock_settlement_economics_mismatch" in (
        row["review_reason_codes"]
    )


def test_explicit_mapping_apply_rejects_source_drift(
    tmp_path: Path,
) -> None:
    repo, case_id, mapping, _terminal_event_id = (
        _legacy_terminal_mapping_fixture(tmp_path)
    )
    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[f"lifecycle:{case_id}"],
    )
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=(
                "futu:lx:1001:legacy-option-deal-1"
            ),
            case_id=case_id,
            owner_evidence_id="legacy-option-evidence-1",
            source_role="option_anchor",
            economic_payload={
                "account": "lx",
                "futu_account_id": "1001",
                "symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "strike": 100,
                "expiration_ymd": EXPIRATION_YMD,
                "multiplier": 100,
                "contracts": 1,
                "price": 1,
                "event_time_ms": 1,
            },
        )
    )

    with pytest.raises(
        ValueError,
        match="lifecycle migration source drift",
    ):
        apply_lifecycle_migration_manifest(
            repo,
            manifest=manifest,
            apply_changes=True,
        )
    assert repo.list_trade_lifecycle_migration_receipts() == []
    assert repo.list_trade_lifecycle_notifications() == []


def test_explicit_bridge_reuses_existing_v2_case_without_terminal_write(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    persist_trade_event_object(repo, _open_event())
    observed_at_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observed_at_ms is not None
    canonical_case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observed_at_ms,
    )["created_case_ids"][0]
    option_raw = {
        "internal_account": "lx",
        "futu_account_id": "1001",
        "deal_id": "bridge-option-deal-1",
        "symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "currency": "USD",
        "multiplier": 100,
        "price": 0,
        "trade_time_ms": observed_at_ms,
        "visible_account_fields": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
        },
        "raw_payload": {
            "futu_account_id": "1001",
            "trd_acc_id": "1001",
            "deal_id": "bridge-option-deal-1",
        },
    }
    legacy_case_id = "legacy-waiting-case-1"
    canonical_case = repo.get_trade_lifecycle_case(
        canonical_case_id
    )
    assert canonical_case is not None
    assert repo.upsert_trade_lifecycle_case(
        {
            "case_id": legacy_case_id,
            "case_key": "legacy-waiting-key-1",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": EXPIRATION_YMD,
            "currency": "USD",
            "multiplier": 100,
            "contracts": 1,
            "decision_type": None,
            "status": "waiting_settlement_evidence",
            "target_lot_ids": ["lot-1"],
            "raw": {"option_deal": option_raw},
        }
    )
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "bridge-option-evidence-1",
            "case_id": legacy_case_id,
            "source_type": "futu_trade_push",
            "source_event_id": "bridge-option-deal-1",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "NVDA",
            "trade_time_ms": observed_at_ms,
            "raw": option_raw,
        }
    )
    policy = build_lifecycle_timing_policy(
        case_id=canonical_case_id,
        market="US",
        expiration_ymd=EXPIRATION_YMD,
        contract_metadata={
            "settlement_style": "physical",
            "underlying_security_type": "equity",
            "last_trade_cutoff_ms": observed_at_ms - 1,
            "last_trade_cutoff_source": (
                "instrument_policy_registry"
            ),
        },
        trading_days=[
            {"date": "2026-08-21", "type": "TRADING"},
            {"date": "2026-08-24", "type": "TRADING"},
            {"date": "2026-08-25", "type": "TRADING"},
        ],
        calendar_source="test_calendar",
        calendar_observed_at_ms=observed_at_ms,
    )
    mapping = {
        "schema_version": "lifecycle_explicit_mapping.v1",
        "rows": [
            {
                "legacy_case_id": legacy_case_id,
                "disposition": "bridge_to_v2",
                "canonical_case_id": canonical_case_id,
                "canonical_contract": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "position_side": "short",
                    "strike": 100,
                    "expiration_ymd": EXPIRATION_YMD,
                    "currency": "USD",
                    "multiplier": 100,
                },
                "target_contracts_by_lot": {"lot-1": 1},
                "evidence_sources": [
                    {
                        "evidence_id": "bridge-option-evidence-1",
                        "source_key": (
                            "futu:lx:1001:bridge-option-deal-1"
                        ),
                        "source_role": "option_anchor",
                    }
                ],
                "timing_policy": policy,
            }
        ],
    }
    before_event_count = len(repo.list_trade_events())
    before_case_count = len(repo.list_trade_lifecycle_cases())
    inventory = build_lifecycle_migration_inventory(
        repo,
        explicit_mapping=mapping,
    )
    target_key = f"lifecycle:{legacy_case_id}"
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == target_key
    )
    assert row["mapping_status"] == "exact", row[
        "review_reason_codes"
    ]

    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[target_key],
    )
    applied = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )

    assert applied["applied_count"] == 1
    assert len(repo.list_trade_events()) == before_event_count
    assert len(repo.list_trade_lifecycle_cases()) == before_case_count
    legacy = repo.get_trade_lifecycle_case(legacy_case_id)
    assert legacy is not None
    assert legacy["status"] == "superseded"
    assert legacy["superseded_by_case_id"] == canonical_case_id
    canonical = repo.get_trade_lifecycle_case(
        canonical_case_id
    )
    assert canonical is not None
    assert canonical["futu_account_id"] == "1001"
    assert repo.get_trade_lifecycle_timing_policy(
        canonical_case_id
    ) == policy
    assert repo.list_trade_lifecycle_allocations(
        case_id=canonical_case_id
    ) == []

    now_ms = observed_at_ms + 1
    live_model = lifecycle_case_read_model(
        repo,
        case_id=canonical_case_id,
        now_ms=now_ms,
    )
    snapshot = decision_state_snapshot(
        repo,
        account="lx",
        portfolio_scope_id="futu:lx",
    )
    frozen_models = (
        build_lifecycle_read_models_from_decision_snapshot(
            snapshot,
            now_ms=now_ms,
        )
    )
    frozen_model = frozen_models["lot-1"]
    assert snapshot["snapshot_status"] == "trusted"
    assert next(
        item
        for item in snapshot["account_lifecycle_resolution"][
            "case_resolutions"
        ]
        if item["case_id"] == canonical_case_id
    )["status"] == "bridged"
    for field in (
        "closure_fact",
        "reason_state",
        "reserved_contracts_by_lot",
        "remaining_contracts_by_lot",
        "lifecycle_generation_token",
    ):
        assert frozen_model[field] == live_model[field]


def test_outbox_stale_boundaries_and_resend_revision_split(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )

    def _intent(suffix: str) -> dict:
        return build_notification_intent(
            case_id=f"case-{suffix}",
            transition_type="resolution_confirmed",
            resolution_revision=7,
            transition_key=f"lifecycle:case-{suffix}:final",
            state_fingerprint=f"state-{suffix}",
            payload={
                "account": "lx",
                "case_id": f"case-{suffix}",
            },
        )

    first = _intent("claimed")
    enqueue_notification_intent(repo, first)
    first_due_at = int(
        repo.get_trade_lifecycle_notification(
            first["outbox_id"]
        )["next_attempt_at_ms"]
    )
    claimed = claim_next_notification(
        repo,
        now_ms=first_due_at,
        claim_id="claim-1",
    )
    assert claimed is not None
    recovered = recover_stale_notifications(
        repo,
        now_ms=first_due_at + CLAIM_LEASE_MS + 1,
    )
    assert recovered["reclaimed_claimed_count"] == 1
    assert repo.get_trade_lifecycle_notification(
        first["outbox_id"]
    )["status"] == "pending"

    second_claim_at = first_due_at + CLAIM_LEASE_MS + 2
    claimed_second = claim_next_notification(
        repo,
        now_ms=second_claim_at,
        claim_id="claim-2",
        account="lx",
    )
    assert claimed_second is not None
    mark_notification_send_started(
        repo,
        outbox_id=claimed_second["outbox_id"],
        claim_id="claim-2",
        now_ms=second_claim_at,
    )
    frozen = recover_stale_notifications(
        repo,
        now_ms=second_claim_at + CLAIM_LEASE_MS + 1,
    )
    assert frozen["frozen_unknown_count"] == 1
    unknown = repo.get_trade_lifecycle_notification(
        claimed_second["outbox_id"]
    )
    assert unknown["status"] == "unknown"

    resend = reconcile_unknown_notification(
        repo,
        outbox_id=unknown["outbox_id"],
        action="resend",
        broker_ref="operator-check-1",
        note="provider acceptance could not be proven",
        apply_changes=True,
        now_ms=3_000,
    )
    compensating = resend["compensating_outbox"]
    assert compensating["resolution_revision"] == 7
    assert compensating["delivery_revision"] == 1
    assert (
        compensating["state_fingerprint"]
        == unknown["state_fingerprint"]
    )
    assert repo.get_trade_lifecycle_notification(
        unknown["outbox_id"]
    )["status"] == "unknown"


def test_accepted_outbox_must_become_unknown_before_resend(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    intent = build_notification_intent(
        case_id="case-accepted",
        transition_type="resolution_confirmed",
        resolution_revision=4,
        transition_key="lifecycle:case-accepted:final",
        state_fingerprint="state-accepted",
        payload={"account": "lx", "case_id": "case-accepted"},
    )
    enqueue_notification_intent(repo, intent)
    due_at = int(
        repo.get_trade_lifecycle_notification(
            intent["outbox_id"]
        )["next_attempt_at_ms"]
    )
    claimed = claim_next_notification(
        repo,
        now_ms=due_at,
        claim_id="accepted-claim",
    )
    assert claimed is not None
    mark_notification_send_started(
        repo,
        outbox_id=intent["outbox_id"],
        claim_id="accepted-claim",
        now_ms=due_at,
    )
    complete_notification_attempt(
        repo,
        outbox_id=intent["outbox_id"],
        claim_id="accepted-claim",
        outcome="accepted",
        now_ms=due_at + 1,
        provider_message_id="provider-message-1",
        provider_receipt={
            "status": "accepted",
            "request_id": "request-1",
        },
    )

    with pytest.raises(
        ValueError,
        match="notification reconciliation action is invalid",
    ):
        reconcile_unknown_notification(
            repo,
            outbox_id=intent["outbox_id"],
            action="resend",
            broker_ref="provider-check-1",
            note="accepted is not eligible for direct resend",
            apply_changes=True,
            now_ms=due_at + 2,
        )

    reconciled = reconcile_unknown_notification(
        repo,
        outbox_id=intent["outbox_id"],
        action="unknown",
        broker_ref="provider-check-2",
        note="provider could not confirm final delivery",
        apply_changes=True,
        now_ms=due_at + 3,
    )
    row = reconciled["outbox"]
    assert row["status"] == "unknown"
    assert row["provider_message_id"] == "provider-message-1"
    assert row["provider_receipt"]["action"] == "unknown"
    assert row["provider_receipt"][
        "original_provider_receipt"
    ] == {
        "status": "accepted",
        "request_id": "request-1",
    }


def test_historical_split_normal_close_seeds_one_final_slot(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    contract = _open_event().contract_key
    for suffix, lot_id in (("a", "lot-a"), ("b", "lot-b")):
        assert repo.upsert_trade_event(
            TradeEvent(
                event_id=f"historical-close-{suffix}",
                event_type="close",
                event_time_ms=1_800_000_000_000,
                contract_key=contract,
                contracts=1,
                price=1,
                currency="USD",
                source="history",
                multiplier=100,
                target_lot_id=lot_id,
                raw_payload={
                    "futu_account_id": "1001",
                    "source_deal_id": "split-close-1",
                    "target_lot_id": lot_id,
                },
            )
        )

    inventory = build_lifecycle_migration_inventory(repo)
    target_key = "close:futu:lx:1001:split-close-1"
    row = next(
        item
        for item in inventory["rows"]
        if item["target_key"] == target_key
    )
    assert row["mapping_status"] == "exact"
    assert row["split_event_count"] == 2
    assert (
        row["transition_key"]
        == f"{target_key}:resolution_confirmed"
    )

    manifest = select_lifecycle_migration_targets(
        inventory,
        target_keys=[target_key],
    )
    applied = apply_lifecycle_migration_manifest(
        repo,
        manifest=manifest,
        apply_changes=True,
    )
    assert applied["applied_count"] == 1
    rows = repo.list_trade_lifecycle_notifications(
        case_id=target_key
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "suppressed"
    assert rows[0]["transition_type"] == "resolution_confirmed"
    assert rows[0]["transition_key"] == (
        f"{target_key}:resolution_confirmed"
    )


def test_historical_broker_close_recovers_one_consistent_account(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    contract = _open_event().contract_key
    assert repo.upsert_trade_event(
        TradeEvent(
            event_id="historical-close-with-null-account",
            event_type="close",
            event_time_ms=1_800_000_000_000,
            contract_key=contract,
            contracts=1,
            price=1,
            currency="USD",
            source="history",
            multiplier=100,
            target_lot_id="lot-a",
            raw_payload={
                "futu_account_id": "1001",
                "source_deal_id": "legacy-close-1",
                "close_target_account": "lx",
            },
        )
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?",
            ("historical-close-with-null-account",),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["account"] = None
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "historical-close-with-null-account",
            ),
        )

    inventory = build_lifecycle_migration_inventory(repo)

    assert inventory["review_count"] == 0
    assert inventory["exact_count"] == 1
    assert inventory["rows"][0]["target_key"] == (
        "close:futu:lx:1001:legacy-close-1"
    )


def test_historical_broker_close_keeps_account_conflict_in_review(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    contract = _open_event().contract_key
    assert repo.upsert_trade_event(
        TradeEvent(
            event_id="historical-close-with-account-conflict",
            event_type="close",
            event_time_ms=1_800_000_000_000,
            contract_key=contract,
            contracts=1,
            price=1,
            currency="USD",
            source="history",
            multiplier=100,
            target_lot_id="lot-a",
            raw_payload={
                "futu_account_id": "1001",
                "source_deal_id": "legacy-close-2",
                "close_target_account": "lx",
            },
        )
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?",
            ("historical-close-with-account-conflict",),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["account"] = "sy"
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "historical-close-with-account-conflict",
            ),
        )

    inventory = build_lifecycle_migration_inventory(repo)

    assert inventory["exact_count"] == 0
    assert inventory["review_count"] == 1
    assert inventory["rows"][0]["review_reason_codes"] == [
        "canonical_broker_deal_key_missing"
    ]


@pytest.mark.parametrize(
    ("event_id", "source_type"),
    [
        ("manual-close-history", "manual_trade_event"),
        ("manual-repair-history", "system_trade_event"),
    ],
)
def test_internal_non_broker_close_is_not_a_migration_target(
    tmp_path: Path,
    event_id: str,
    source_type: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    assert repo.upsert_trade_event(
        TradeEvent(
            event_id=event_id,
            event_type="close",
            event_time_ms=1_800_000_000_000,
            contract_key=_open_event().contract_key,
            contracts=1,
            price=1,
            currency="USD",
            source="history",
            multiplier=100,
            target_lot_id="lot-a",
            raw_payload={"source_type": source_type},
        )
    )

    inventory = build_lifecycle_migration_inventory(repo)

    assert inventory["row_count"] == 0
    assert inventory["review_count"] == 0


def test_partial_or_unknown_broker_close_stays_in_review(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    contract = _open_event().contract_key
    for event_id, raw_payload in (
        (
            "manual-close-with-partial-broker-identity",
            {
                "source_type": "manual_trade_event",
                "futu_account_id": "1001",
            },
        ),
        ("unknown-close", {"source_type": "unknown_history"}),
    ):
        assert repo.upsert_trade_event(
            TradeEvent(
                event_id=event_id,
                event_type="close",
                event_time_ms=1_800_000_000_000,
                contract_key=contract,
                contracts=1,
                price=1,
                currency="USD",
                source="history",
                multiplier=100,
                target_lot_id=f"lot-{event_id}",
                raw_payload=raw_payload,
            )
        )

    inventory = build_lifecycle_migration_inventory(repo)

    assert inventory["exact_count"] == 0
    assert inventory["review_count"] == 2
    assert {
        item["target_key"] for item in inventory["rows"]
    } == {
        "normal-close-review:"
        "manual-close-with-partial-broker-identity",
        "normal-close-review:unknown-close",
    }


def test_voided_historical_broker_close_is_not_a_migration_target(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    contract = _open_event().contract_key
    assert repo.upsert_trade_event(
        TradeEvent(
            event_id="voided-broker-close",
            event_type="close",
            event_time_ms=1_800_000_000_000,
            contract_key=contract,
            contracts=1,
            price=1,
            currency="USD",
            source="history",
            multiplier=100,
            target_lot_id="lot-a",
            raw_payload={
                "futu_account_id": "1001",
                "source_deal_id": "voided-close-1",
            },
        )
    )
    assert repo.upsert_trade_event(
        TradeEvent(
            event_id="void-broker-close",
            event_type="void",
            event_time_ms=1_800_000_000_001,
            contract_key=contract,
            contracts=0,
            price=0,
            currency="USD",
            source="history",
            multiplier=100,
            target_event_id="voided-broker-close",
        )
    )

    inventory = build_lifecycle_migration_inventory(repo)

    assert inventory["row_count"] == 0
    assert inventory["review_count"] == 0


def test_migration_inventory_blocks_cross_case_source_owner_ambiguity(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    source_key = "futu:lx:1001:ambiguous-deal-1"
    for suffix, strike in (("a", 100), ("b", 101)):
        case_id = f"case-{suffix}"
        assert repo.insert_trade_lifecycle_case_once(
            {
                "schema_version": "lifecycle_case.v2",
                "case_id": case_id,
                "case_key": f"case-key-{suffix}",
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "strike": strike,
                "expiration_ymd": EXPIRATION_YMD,
                "contract_key": f"contract-{suffix}",
                "status": "closure_observed",
                "decision_type": "option_expiry",
                "target_lot_ids": [f"lot-{suffix}"],
                "target_contracts_by_lot": {
                    f"lot-{suffix}": 1
                },
                "observation_start_ms": 1,
                "pending_until_ms": None,
                "created_at_ms": 1,
                "updated_at_ms": 1,
            }
        )
        evidence_type = (
            "option_zero_price_close"
            if suffix == "a"
            else "stock_settlement_leg"
        )
        assert repo.insert_trade_lifecycle_evidence_once(
            {
                "evidence_id": f"evidence-{suffix}",
                "case_id": case_id,
                "source_type": "futu_broker_deal",
                "source_event_id": source_key,
                "evidence_type": evidence_type,
                "account": "lx",
                "futu_account_id": "1001",
                "symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "strike": strike,
                "expiration_ymd": EXPIRATION_YMD,
                "contracts": 1,
                "shares": 100,
                "price": 0,
                "event_time_ms": 1,
                "target_contracts_by_lot": {
                    f"lot-{suffix}": 1
                },
            }
        )

    inventory = build_lifecycle_migration_inventory(repo)
    lifecycle_rows = [
        item
        for item in inventory["rows"]
        if item["target_key"] in {
            "lifecycle:case-a",
            "lifecycle:case-b",
        }
    ]
    assert len(lifecycle_rows) == 2
    assert all(
        item["mapping_status"] == "needs_review"
        for item in lifecycle_rows
    )
    assert all(
        "source_claim_owner_ambiguous"
        in item["review_reason_codes"]
        for item in lifecycle_rows
    )


def test_outbox_v1_schema_upgrade_preserves_delivery_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE trade_lifecycle_notification_outbox (
              outbox_id TEXT PRIMARY KEY,
              case_id TEXT NOT NULL,
              transition_type TEXT NOT NULL,
              resolution_revision INTEGER NOT NULL,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              provider_message_id TEXT,
              claim_id TEXT,
              claimed_at_ms INTEGER,
              send_started_at_ms INTEGER,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at_ms INTEGER,
              last_error TEXT,
              provider_receipt_json TEXT,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL,
              confirmed_at_ms INTEGER,
              UNIQUE(case_id, transition_type, resolution_revision)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_lifecycle_notification_outbox (
              outbox_id, case_id, transition_type,
              resolution_revision, status, payload_json,
              payload_hash, provider_message_id, claim_id,
              claimed_at_ms, send_started_at_ms, attempt_count,
              next_attempt_at_ms, last_error,
              provider_receipt_json, created_at_ms,
              updated_at_ms, confirmed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-outbox-1",
                "case-legacy",
                "resolution_confirmed",
                3,
                "accepted",
                json.dumps({"account": "lx"}),
                "legacy-payload-hash",
                "provider-1",
                "claim-1",
                10,
                11,
                2,
                None,
                None,
                json.dumps({"accepted": True}),
                12,
                13,
                None,
            ),
        )

    repo = SQLiteOptionPositionsRepository(db_path)
    row = repo.get_trade_lifecycle_notification(
        "legacy-outbox-1"
    )
    assert row is not None
    assert row["status"] == "accepted"
    assert row["attempt_count"] == 2
    assert row["provider_message_id"] == "provider-1"
    assert row["provider_receipt"] == {"accepted": True}
    assert row["delivery_revision"] == 0
    assert row["transition_key"] == "legacy:legacy-outbox-1"
    assert row["state_fingerprint"] == "legacy-payload-hash"
    assert row["delivery_batch_id"] is None
    assert repo.list_trade_lifecycle_notification_batches() == []


def test_lifecycle_delivery_status_separates_case_and_outbox_states(
    tmp_path: Path,
) -> None:
    repo, case_id, observed_at_ms = _case_with_option_anchor(
        tmp_path
    )
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    lifecycle_case["status"] = "waiting_settlement_evidence"
    lifecycle_case["derived_summary"] = {
        "reason_state": "cause_pending",
        "settlement_deadline_ms": observed_at_ms + 500,
    }
    assert repo.upsert_trade_lifecycle_case(lifecycle_case)
    intent = build_notification_intent(
        case_id=case_id,
        transition_type="option_leg_closed",
        resolution_revision=1,
        transition_key=f"lifecycle:{case_id}:option_leg_closed",
        state_fingerprint="status-test-state",
        payload={"account": "lx", "case_id": case_id},
    )
    assert repo.insert_trade_lifecycle_notification_once(intent)

    status = _lifecycle_delivery_status(
        repo,
        account="lx",
        now_ms=observed_at_ms + 1_000,
    )
    assert status["schema_version"] == (
        "trade_lifecycle_delivery_status.v2"
    )
    assert status["reason_state_counts"]["cause_pending"] == 1
    assert status["overdue_pending_count"] == 1
    assert status["oldest_pending_case"]["case_id"] == case_id
    assert status["outbox_status_counts"]["pending"] == 1
    assert status["unbound_eligible_count"] == 1
    assert status["oldest_unbound_eligible"]["case_id"] == case_id
    assert status["delivery_batch_count"] == 0
    assert status["batched_member_count"] == 0
    assert status["messages_avoided"]["total"] == 0
    assert status["oldest_unknown_outbox"] is None


def test_lifecycle_delivery_status_reports_batch_scope_without_target(
    tmp_path: Path,
) -> None:
    repo, case_id, _observed_at_ms = _case_with_option_anchor(
        tmp_path
    )
    for revision, transition in (
        (1, "option_leg_closed"),
        (2, "needs_review"),
    ):
        intent = build_notification_intent(
            case_id=case_id,
            transition_type=transition,
            resolution_revision=revision,
            transition_key=(
                f"lifecycle:{case_id}:{transition}:{revision}"
            ),
            state_fingerprint=f"batch-status-{revision}",
            payload={
                "account": "lx",
                "case_id": case_id,
                "transition_type": transition,
            },
        )
        assert repo.insert_trade_lifecycle_notification_once(intent)
    rows = repo.list_trade_lifecycle_notifications(case_id=case_id)
    dispatch_at = max(
        int(row["created_at_ms"]) for row in rows
    ) + QUIET_WINDOW_MS
    target = "sensitive-target-must-not-appear"
    result = dispatch_notification_batch_once(
        repo,
        route=build_notification_batch_route(
            provider="feishu_app",
            channel="feishu_app",
            target=target,
        ),
        send_fn=lambda _payload: {
            "status": "confirmed",
            "delivery_confirmed": True,
            "message_id": "provider-message",
        },
        now_ms=dispatch_at,
    )
    assert result["status"] == "confirmed"

    status = _lifecycle_delivery_status(
        repo,
        account="lx",
        now_ms=dispatch_at + 1,
    )

    assert status["batch_status_counts"]["confirmed"] == 1
    assert status["delivery_batch_count"] == 1
    assert status["batched_member_count"] == 2
    assert status["active_batched_member_count"] == 0
    assert status["messages_avoided"] == {
        "scope": "full_delivery_batches_touching_account",
        "confirmed": 1,
        "accepted": 0,
        "total": 1,
    }
    assert status["oldest_unknown_batch"] is None
    assert target not in str(status)


def test_lifecycle_delivery_status_cache_skips_unchanged_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.auto_intake as auto_intake

    repo, case_id, observed_at_ms = _case_with_option_anchor(
        tmp_path
    )
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    lifecycle_case["status"] = "waiting_settlement_evidence"
    lifecycle_case["derived_summary"] = {
        "reason_state": "cause_pending",
        "settlement_deadline_ms": observed_at_ms + 500,
    }
    assert repo.upsert_trade_lifecycle_case(lifecycle_case)

    evidence_reads = 0
    dispatcher_reads = 0
    original_evidence_reader = repo.list_trade_lifecycle_evidence

    def counted_evidence_reader(*args, **kwargs):
        nonlocal evidence_reads
        evidence_reads += 1
        return original_evidence_reader(*args, **kwargs)

    def dispatcher_status() -> dict:
        nonlocal dispatcher_reads
        dispatcher_reads += 1
        return {"status": "running", "sample": dispatcher_reads}

    monkeypatch.setattr(
        repo,
        "list_trade_lifecycle_evidence",
        counted_evidence_reader,
    )
    wall_time_ms = observed_at_ms + 100
    monkeypatch.setattr(
        auto_intake.time,
        "time",
        lambda: wall_time_ms / 1000,
    )
    status_state: dict = {}
    cache: dict = {}

    for _ in range(10):
        _refresh_lifecycle_delivery_status(
            status_state,
            repo=repo,
            account="lx",
            dispatcher_status_fn=dispatcher_status,
            snapshot_cache=cache,
        )

    assert evidence_reads == 1
    assert dispatcher_reads == 10
    assert status_state["lifecycle_delivery"][
        "overdue_pending_count"
    ] == 0
    wall_time_ms = observed_at_ms + 1_000
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        dispatcher_status_fn=dispatcher_status,
        snapshot_cache=cache,
    )
    assert evidence_reads == 1
    assert status_state["lifecycle_delivery"][
        "overdue_pending_count"
    ] == 1
    assert status_state["lifecycle_delivery"]["dispatcher"][
        "sample"
    ] == 11

    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "delivery-status-cache-invalidation",
            "case_id": case_id,
            "source_type": "status_cache_test",
            "source_event_id": "delivery-status-cache-invalidation",
            "evidence_type": "status_cache_test",
            "account": "lx",
            "symbol": lifecycle_case["symbol"],
        }
    )
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        dispatcher_status_fn=dispatcher_status,
        snapshot_cache=cache,
    )
    assert evidence_reads == 2

    with repo._connect() as conn:  # noqa: SLF001 - trigger coverage
        trigger_names = {
            str(row["name"])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name LIKE 'trg_lifecycle_delivery_status_%'
                """
            ).fetchall()
        }
    assert len(trigger_names) == 18

    cache.clear()
    stable_revision = (
        repo.get_trade_lifecycle_delivery_status_revision()
    )
    racing_revisions = iter(
        (stable_revision, stable_revision + 1)
    )
    monkeypatch.setattr(
        repo,
        "get_trade_lifecycle_delivery_status_revision",
        lambda: next(racing_revisions),
    )
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        snapshot_cache=cache,
    )
    assert evidence_reads == 3
    assert cache == {}

    monkeypatch.setattr(
        repo,
        "get_trade_lifecycle_delivery_status_revision",
        lambda: stable_revision + 1,
    )
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        snapshot_cache=cache,
    )
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        snapshot_cache=cache,
    )
    assert evidence_reads == 4

    def broken_revision_reader() -> int:
        raise RuntimeError("revision unavailable")

    monkeypatch.setattr(
        repo,
        "get_trade_lifecycle_delivery_status_revision",
        broken_revision_reader,
    )
    _refresh_lifecycle_delivery_status(
        status_state,
        repo=repo,
        account="lx",
        snapshot_cache=cache,
    )
    assert cache == {}
    assert status_state["lifecycle_delivery"]["status"] == "unavailable"
    assert "revision unavailable" in status_state[
        "lifecycle_delivery"
    ]["error"]
