from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sqlite3

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.commands import record_manual_assignment
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.workflows import execute_manual_assigned_stock_sale
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import resolve_trade_deal
from domain.domain.trade_execution import normalize_execution_input


def _stock_sale_deal(**overrides: object) -> NormalizedTradeDeal:
    base = {
        "broker": "富途",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
        "deal_id": "stock-sale-1",
        "order_id": "order-stock-sale-1",
        "symbol": "NVDA",
        "option_type": None,
        "side": "sell",
        "position_effect": None,
        "contracts": 100,
        "price": 105.0,
        "strike": None,
        "multiplier": None,
        "multiplier_source": None,
        "expiration_ymd": None,
        "currency": "USD",
        "trade_time_ms": 3000,
        "raw_payload": {"deal_id": "stock-sale-1", "code": "US.NVDA"},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


def _repo_with_assigned_stock(tmp_path: Path, *, opened_at_ms: int = 1000, assigned_at_ms: int = 2000):
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=opened_at_ms,
        ),
    )
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=assigned_at_ms,
    )
    assignment_event = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"][0]
    return repo, f"assigned-stock-{assignment_event['event_id']}"


def _assigned_stock_lifecycle(repo, stock_lot_id: str) -> dict:
    report = build_assigned_stock_view(repo)
    return [row for row in report["assigned_stock_lots"] if row["stock_lot_id"] == stock_lot_id][0]


def test_resolve_trade_previews_broker_assigned_stock_sale(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "preview_assigned_stock_sale"
    assert repo.list_assigned_stock_events() == []
    operation = result.operations[0].to_payload()
    assert operation["record_id"] == stock_lot_id
    assert operation["event_id"] == "assigned-stock-sale-stock-sale-1"
    assert operation["fields"]["source"] == "broker"
    assert operation["fields"]["target_stock_lot_id"] == stock_lot_id
    assert operation["fields"]["fees"] == 0.0
    assert operation["fields"]["fee_provenance"]["basis"] == "estimated"
    assert operation["fields"]["fee_provenance"]["amount"] == "2.5261"
    assert result.diagnostics["assigned_stock_sale"]["stock_lot_after"]["assigned_stock_realized_pnl"] == 497.4739


def test_resolve_trade_applies_broker_assigned_stock_sale(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)

    assert result.status == "applied"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "applied_assigned_stock_sale"
    events = repo.list_assigned_stock_events()
    assert len(events) == 1
    assert events[0]["stock_event_id"] == "assigned-stock-sale-stock-sale-1"
    assert events[0]["source"] == "broker"
    assert events[0]["source_deal_id"] == "stock-sale-1"
    assert events[0]["fees"] == 0.0
    assert events[0]["fee_provenance"]["basis"] == "estimated"
    assert events[0]["fee_provenance"]["amount"] == "2.5261"
    lifecycle = _assigned_stock_lifecycle(repo, stock_lot_id)
    assert lifecycle["status"] == "closed"
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739
    assert lifecycle["option_premium_attribution"] == 250.0
    assert lifecycle["assignment_lifecycle_pnl"] == 747.4739


def test_broker_assigned_stock_sale_does_not_admit_raw_fee_components_as_actual(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(
        _stock_sale_deal(raw_payload={"commission": -0.99, "platform_fee": -1.0}),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    event = repo.list_assigned_stock_events()[0]
    assert event["fees"] == 0.0
    assert event["fee_provenance"]["basis"] == "estimated"
    assert event["fee_provenance"]["amount"] == "2.5261"
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["assigned_stock_realized_pnl"] == 497.4739


def test_manual_assigned_stock_sale_freezes_formula_estimate(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    execute_manual_assigned_stock_sale(
        repo,
        target_stock_lot_id=stock_lot_id,
        shares=100,
        price=105.0,
        trade_time_ms=3000,
        dry_run=False,
    )

    event = repo.list_assigned_stock_events()[0]
    assert event["fees"] == 0.0
    assert event["fee_provenance"]["basis"] == "estimated"
    assert event["fee_provenance"]["amount"] == "2.5261"
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["assigned_stock_realized_pnl"] == 497.4739


def test_resolve_trade_does_not_reopen_closed_assigned_stock_lot_after_stock_buy(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    sale = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    buy = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-buy-1", side="buy", price=95.0, trade_time_ms=4000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    later_sale = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-sale-2", price=110.0, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert sale.status == "applied"
    assert buy.status == "skipped"
    assert buy.reason == "not_option_deal"
    assert later_sale.status == "skipped"
    assert later_sale.reason == "not_option_deal"
    assert len(repo.list_assigned_stock_events()) == 1
    lifecycle = _assigned_stock_lifecycle(repo, stock_lot_id)
    assert lifecycle["status"] == "closed"
    assert lifecycle["shares_remaining"] == 0
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739


def test_resolve_trade_broker_assigned_stock_sale_duplicate_is_idempotent(tmp_path: Path) -> None:
    repo, _stock_lot_id = _repo_with_assigned_stock(tmp_path)

    first = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    duplicate = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)

    assert first.status == "applied"
    assert duplicate.status == "applied"
    assert duplicate.operations[0].to_payload()["result"]["created"] is False
    assert duplicate.diagnostics["assigned_stock_sale"]["idempotent_duplicate"] is True
    assert len(repo.list_assigned_stock_events()) == 1


def test_resolve_trade_keeps_unmatched_stock_sale_as_non_option(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    result = resolve_trade_deal(_stock_sale_deal(symbol="TIGR"), repo=repo, state={}, apply_changes=True)

    assert result.status == "skipped"
    assert result.action is None
    assert result.reason == "not_option_deal"
    assert repo.list_assigned_stock_events() == []


def test_resolve_trade_broker_assigned_stock_sale_ambiguous_lot_is_unresolved(tmp_path: Path) -> None:
    repo, _first_stock_lot_id = _repo_with_assigned_stock(tmp_path, opened_at_ms=1000, assigned_at_ms=2000)
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
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-07-17",
            premium_per_share=2.5,
            opened_at_ms=1100,
        ),
    )
    second_lot = [item for item in repo.list_position_lots() if item["fields"]["status"] == "open"][0]
    record_manual_assignment(
        repo,
        record_id=second_lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=2100,
    )

    result = resolve_trade_deal(_stock_sale_deal(contracts=100, trade_time_ms=3000), repo=repo, state={}, apply_changes=True)

    assert result.status == "unresolved"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "ambiguous_assigned_stock_sale"
    assert result.diagnostics["viable_count"] == 2
    assert {item["stock_cost_per_share"] for item in result.diagnostics["candidates"]} == {100.0}
    assert all(item.get("source_assignment_event_id") for item in result.diagnostics["candidates"])
    assert repo.list_assigned_stock_events() == []


def _standard_stock_sale_deal(**overrides) -> NormalizedTradeDeal:
    deal = _stock_sale_deal(**overrides)
    execution = normalize_execution_input({
        "broker_account_ref": {
            "broker_account_id": f"futu:REAL:{deal.futu_account_id}",
            "broker_id": "futu", "external_account_id": deal.futu_account_id,
            "environment": "REAL", "account_label": deal.internal_account,
        },
        "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": deal.symbol, "currency": deal.currency},
        "external_id_namespace": "futu.deal", "external_execution_id": deal.deal_id,
        "external_order_namespace": "futu.order" if deal.order_id else None, "external_order_id": deal.order_id,
        "side": deal.side, "quantity": str(deal.contracts), "price": str(deal.price),
        "currency": deal.currency, "occurred_at_utc": "1970-01-01T00:00:03Z",
    })
    assert execution["errors"] == []
    return replace(deal, asset_type="stock", execution_input=execution)


def test_standard_stock_sale_replay_keeps_original_event_and_economic_references(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal()
    first = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    before = repo.list_assigned_stock_events()
    replay = replace(deal, execution_input={**deal.execution_input, "evidence_refs": ["second-source"]})
    duplicate = resolve_trade_deal(replay, repo=repo, state={}, apply_changes=True)
    assert first.status == duplicate.status == "applied"
    assert duplicate.operations[0].to_payload()["result"]["created"] is False
    assert repo.list_assigned_stock_events() == before
    assert before[0]["target_stock_lot_id"] == stock_lot_id
    assert "cash_conversions" in before[0]


def test_standard_stock_sale_legacy_without_environment_requires_evidence(tmp_path: Path) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    assert resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True).status == "applied"
    before = repo.list_assigned_stock_events()
    replay = resolve_trade_deal(_standard_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    assert replay.status == "unresolved"
    assert repo.list_assigned_stock_events() == before


def test_standard_stock_sale_conflict_cannot_apply_again(tmp_path: Path) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    assert resolve_trade_deal(_standard_stock_sale_deal(), repo=repo, state={}, apply_changes=True).status == "applied"
    conflict = resolve_trade_deal(_standard_stock_sale_deal(price=106.0), repo=repo, state={}, apply_changes=True)
    assert conflict.status == "unresolved"
    assert len(repo.list_assigned_stock_events()) == 1


def test_stock_sale_same_deal_id_different_account_is_not_a_duplicate(tmp_path: Path) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    assert resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True).status == "applied"
    other = resolve_trade_deal(_stock_sale_deal(internal_account="sy", futu_account_id="REAL_2"), repo=repo, state={}, apply_changes=True)
    assert other.status == "skipped"
    assert other.reason == "not_option_deal"
    assert len(repo.list_assigned_stock_events()) == 1


@pytest.mark.parametrize("apply_changes", [False, True])
def test_intake_rejects_second_physical_account_after_durable_stock_sale(tmp_path: Path, apply_changes: bool) -> None:
    from src.application.trades.auto_intake import _process_payload

    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    def process(deal, apply):
        return _process_payload(
            deal.execution_input, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
            account_mapping={"REAL_1": "lx", "REAL_2": "lx"}, futu_account_ids=["REAL_1", "REAL_2"],
            host="localhost", port=11111, source="file", apply_changes=apply, allow_external_lookup=False,
        )

    first = _standard_stock_sale_deal(contracts=50)
    assert process(first, True)["status"] == "applied"
    events, lots = repo.list_assigned_stock_events(), repo.list_position_lots()
    second = _standard_stock_sale_deal(contracts=50, futu_account_id="REAL_2", deal_id="stock-sale-2")
    rejected = process(second, apply_changes)
    assert (rejected["status"], rejected["reason"]) == ("unresolved", "execution_admission_failed")
    assert "unsupported:multiple_physical_accounts_in_projection" in rejected["diagnostics"]["errors"]
    assert repo.list_assigned_stock_events() == events and repo.list_position_lots() == lots
    assert events[0]["futu_account_id"] == "REAL_1"
    assert events[0]["execution_input"]["broker_account_ref"]["external_account_id"] == "REAL_1"
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["shares_remaining"] == 50


def test_execution_metadata_rejects_older_sqlite_writers(tmp_path: Path) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    assert resolve_trade_deal(_standard_stock_sale_deal(), repo=repo, state={}, apply_changes=True).status == "applied"
    with sqlite3.connect(repo.db_path) as old_connection:
        with pytest.raises(sqlite3.OperationalError, match="om_execution_writer_v1"):
            old_connection.execute("DELETE FROM assigned_stock_events")
    assert len(repo.list_assigned_stock_events()) == 1


def test_stock_sale_pending_intake_recovers_original_scope_after_label_change(tmp_path: Path) -> None:
    from src.application.trades.auto_intake import _process_payload
    from src.application.trades.deal_identity import broker_deal_key
    from src.application.trades.inbox import enqueue_trade_payload, read_trade_payload
    from src.application.trades.inbox_authority import resolve_execution_inbox_path

    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal()
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    inbox_id = enqueue_trade_payload(inbox, payload=deal.execution_input, source="file",
                                    broker_deal_key=broker_deal_key(deal), repo=repo)
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True).status == "applied"
    before_events, before_lots = repo.list_assigned_stock_events(), repo.list_position_lots()
    assert read_trade_payload(inbox, inbox_id=inbox_id)["status"] == "pending"
    renamed = {**deal.execution_input, "broker_account_ref": {
        **deal.execution_input["broker_account_ref"], "account_label": "renamed",
    }}
    result = _process_payload(renamed, repo=repo, state_path=tmp_path / "state.json",
                              audit_path=tmp_path / "audit.jsonl", account_mapping={"REAL_1": "renamed"},
                              futu_account_ids=["REAL_1"], host="127.0.0.1", port=11111,
                              source="file", apply_changes=True, allow_external_lookup=False)
    assert result["status"] == "applied"
    stored = read_trade_payload(inbox, inbox_id=inbox_id)
    assert stored["status"] == "handled"
    assert result["operations"][0]["result"]["created"] is False
    assert before_events[0]["target_stock_lot_id"] == stock_lot_id
    assert before_events[0]["account"] == "lx"
    assert repo.list_assigned_stock_events() == before_events
    assert repo.list_position_lots() == before_lots


def test_stock_sale_same_fill_id_in_other_namespace_is_an_independent_execution(tmp_path: Path) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal(contracts=50)
    first = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    other = replace(deal, execution_input={**deal.execution_input, "external_id_namespace": "verified.partition.deal"})
    second = resolve_trade_deal(other, repo=repo, state={}, apply_changes=True)
    assert first.status == second.status == "applied"
    events = repo.list_assigned_stock_events()
    assert len(events) == 2
    assert len({row["stock_event_id"] for row in events}) == 2
    assert sum(row["shares"] for row in events) == 100
    assert resolve_trade_deal(other, repo=repo, state={}, apply_changes=True).operations[0].to_payload()["result"]["created"] is False
    assert repo.list_assigned_stock_events() == events


@pytest.mark.parametrize("effect", ["open", "close"])
def test_stock_sale_source_effect_enrichment_checks_original_sale(tmp_path: Path, effect: str) -> None:
    repo, _ = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal()
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True).status == "applied"
    before = repo.list_assigned_stock_events()
    enriched = replace(deal, position_effect=effect,
                       execution_input={**deal.execution_input, "position_effect": effect})
    replay = resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True)
    assert replay.status == ("unresolved" if effect == "open" else "applied")
    assert repo.list_assigned_stock_events() == before


def test_explicit_stock_open_does_not_consume_assigned_stock_lot(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal(position_effect="open")
    deal = replace(deal, execution_input={**deal.execution_input, "position_effect": "open"})
    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert (result.status, result.reason) == ("skipped", "not_option_deal")
    assert repo.list_assigned_stock_events() == []
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["shares_remaining"] == 100


def test_late_stock_order_enrichment_preserves_sale_and_recovers_fee_target(tmp_path: Path) -> None:
    from copy import deepcopy
    from src.application.trades.order_fee_sync import recover_order_fee_targets

    repo, _ = _repo_with_assigned_stock(tmp_path)
    deal = _standard_stock_sale_deal(order_id=None, trade_time_ms=1788748200000)
    deal = replace(deal, execution_input={**deal.execution_input, "occurred_at_utc": "2026-09-07T03:50:00Z"})
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True).status == "applied"
    before, lots = repo.list_assigned_stock_events(), repo.list_position_lots()
    enriched = replace(deal, order_id="late-stock-order", execution_input={
        **deal.execution_input, "external_order_id": "late-stock-order", "external_order_namespace": "futu.order",
        "evidence_refs": ["source:history:stock-order"],
    })
    assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=False).status == "dry_run"
    assert repo.list_assigned_stock_events() == before
    replay = resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True)
    assert replay.status == "applied"
    assert replay.operations[0].to_payload()["result"]["created"] is False
    after = repo.list_assigned_stock_events()
    assert after[0]["order_id"] == after[0]["execution_input"]["external_order_id"] == "late-stock-order"
    assert after[0]["external_order_namespace"] == after[0]["execution_input"]["external_order_namespace"] == "futu.order"
    assert after[0]["execution_order_identity_enrichments"][0]["evidence_refs"] == ["source:history:stock-order"]
    reverted = deepcopy(after)
    for key in ("order_id", "external_order_namespace", "execution_input", "execution_order_identity_enrichments"):
        if key in before[0]:
            reverted[0][key] = before[0][key]
        else:
            reverted[0].pop(key, None)
    assert reverted == before
    assert repo.list_position_lots() == lots
    targets = recover_order_fee_targets(repo, account="lx", allowed_futu_account_ids=["REAL_1"])["targets"]
    assert any(item[-1] == "late-stock-order" for item in targets)
    assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True).status == "applied"
    assert repo.list_assigned_stock_events() == after
