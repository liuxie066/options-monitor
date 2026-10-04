from __future__ import annotations

from dataclasses import replace

import pytest

from domain.domain.assigned_stock import assigned_stock_lot_id_for_event, project_assigned_stock_lifecycle
from domain.domain.ledger import project_trade_events
from domain.domain.ledger.events import lot_id_for_open_event
from domain.domain.wheel.projection import (
    effective_wheel_events,
    lot_strategy_metadata_from_trade_events,
)
from domain.domain.wheel.events import build_wheel_event
from domain.domain.trade_execution import execution_identity_from_input
from domain.domain.strategy_membership import POSITION_LOT_STRATEGY_PATCH_FIELDS
from src.application.ledger.api import preview_trade_event_repair
from src.application.ledger.combo_membership import resolve_combo_group_membership
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.position_projection_runtime import (
    run_position_projection_forced_full,
    run_position_projection_fast_if_safe,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer_decision import _event_position_lot_id
from src.application.positions.inspection import _event_record_refs
from tests.test_position_projection_runtime import _event


@pytest.mark.parametrize("explicit", [None, "lot_open", "historical-lot"])
def test_identity_survives_storage_receipts_metadata_partial_close_and_replay(tmp_path, explicit):
    expected = explicit or "lot_open"
    opening = _event("open", "open", 1000, lot_id=explicit, contracts=2,
                     raw_payload={"strategy": "wheel", "strategy_group_id": "group"})
    before = encode_trade_event_for_storage(opening).event_json
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    run_position_projection_forced_full(repo, [opening], seed_checkpoint=True)
    repo.set_position_projection_checkpoint_mode("enabled")
    stored = repo.list_trade_events()[0]
    assert stored["lot_id"] == explicit
    assert lot_id_for_open_event(stored) == _event_position_lot_id(opening) == expected
    assert _event_record_refs(stored) >= {"open", expected}
    assert set(lot_strategy_metadata_from_trade_events([stored])) == {expected}
    membership = resolve_combo_group_membership(
        group_id="group", account="lx", trade_events=[stored],
        projected_position_lots=repo.list_position_lots(),
    )
    assert membership.global_historical_lot_ids == (expected,)
    close = _event("close", "close", 2000, target_lot_id=expected)
    advanced = run_position_projection_fast_if_safe(repo, [close])
    assert advanced.mode_used == "fast_tail"
    lots = repo.list_position_lots()
    assert lots[0]["record_id"] == expected
    assert lots[0]["fields"]["contracts_open"] == 1
    retry = run_position_projection_fast_if_safe(repo, [opening, close])
    assert retry.mode_used == "unchanged"
    assert len(repo.list_trade_events()) == 2
    with repo._connect() as conn:
        actual = conn.execute("SELECT event_json FROM trade_events WHERE event_id='open'").fetchone()[0]
    assert actual == before
    full = project_trade_events([opening, close])
    assert not full.diagnostics
    assert full.lots[0].lot_id == expected
    assert full.lots[0].contracts_open == 1
    # Repair must see the exact downstream target, including historical custom IDs.
    with pytest.raises(ValueError, match="downstream"):
        preview_trade_event_repair(repo, event_id="open", overrides={"strike": 110}, reason="test")


@pytest.mark.parametrize("alias", ["record_id", "lot_record_id", "lot_id"])
def test_conflicting_alias_is_rejected_by_storage_projection_and_metadata(tmp_path, alias):
    opening = _event("open", "open", 1000, lot_id="historical-lot",
                     raw_payload={alias: "other-lot", "strategy": "wheel"})
    projected = project_trade_events([opening])
    assert not projected.lots
    assert "lot_identity_conflict" in {item.code for item in projected.diagnostics}
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError, match="lot_identity_conflict"):
        repo.upsert_trade_event(opening)
    assert repo.list_trade_events() == []
    with pytest.raises(ValueError, match="lot_identity_conflict"):
        lot_strategy_metadata_from_trade_events([opening.to_dict()])
    with pytest.raises(ValueError, match="lot_identity_conflict"):
        resolve_combo_group_membership(group_id="group", account="lx",
            trade_events=[opening.to_dict()], projected_position_lots=[])


@pytest.mark.parametrize("explicit", [None, "lot_open", "historical-lot"])
@pytest.mark.parametrize("fault", [None, "other_account", "duplicate", "alias"])
def test_wheel_proof_uses_unique_canonical_opening_in_same_account(explicit, fault):
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": "fill",
                 "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}
    opening = _event("open", "open", 1000, lot_id=explicit,
                     account="sy" if fault == "other_account" else "lx",
                     raw_payload={"execution_input": execution})
    expected = explicit or "lot_open"
    before = dict.fromkeys(POSITION_LOT_STRATEGY_PATCH_FIELDS)
    after = {**before, "strategy": "sell_put"}
    decision = {"schema_version": "attribution_decision.v1", "account": "lx", "manual": True,
        "request_id": "request", "actor": "operator", "input_hash": "hash", "policy_version": "test",
        "branch_generations": {"stock": "generation"}, "conflict_event_ids": ["conflict"],
        "members": [{"execution_key": execution_identity_from_input(execution), "open_event_id": "open",
            "lot_id": expected, "proof_event_id": "proof", "before": before, "after": after}]}
    proof = _event("proof", "adjust", 3000, target_lot_id=expected, contracts=0, price=0,
        raw_payload={"patch": after, "actor": "operator",
                     "attribution_decision": decision, "adjust_target_source_event_id": "open",
                     "attribution_policy_version": "test",
                     "attribution_request_id": "request", "attribution_origin": "manual",
                     "attribution_action": "ordinary"})
    proof = replace(proof, source="trade_attribution")
    common = dict(account="lx", lot_id="stock", occurred_at_ms=2000, recorded_at_ms=2000)
    conflict = build_wheel_event(event_id="conflict", event_type="wheel_attribution_conflict", **common,
        payload={"actor": "intake", "request_id": "request", "branch_generation_hash": "generation",
                 "input_hash": "hash", "execution_keys": [execution_identity_from_input(execution)]})
    resolution = build_wheel_event(event_id="resolved", event_type="wheel_attribution_conflict_resolved",
        **{**common, "occurred_at_ms": 4000, "recorded_at_ms": 4000},
        payload={"actor": "operator", "request_id": "request", "branch_generation_hash": "generation",
                 "input_hash": "hash", "conflict_event_id": "conflict", "resolution_evidence_event_id": "proof"})
    rows = [opening.to_dict(), proof.to_dict()]
    if fault == "duplicate":
        rows.append(replace(opening, event_id="other-open", lot_id=expected).to_dict())
    elif fault == "alias":
        rows[0]["raw_payload"]["record_id"] = "wrong"
    else:
        rows.append(opening.to_dict())  # Replay of identical input is harmless.
    _, issues = effective_wheel_events([conflict, resolution], trade_events=rows,
        known_trade_event_ids={"open", "proof"}, as_of_ms=5000)
    assert ("strategy_attribution_conflict" in issues.get(("lx", "stock"), set())) == bool(fault)


def test_assigned_stock_identity_requires_source_and_retains_existing_format():
    assert assigned_stock_lot_id_for_event("assignment") == "assigned-stock-assignment"
    with pytest.raises(ValueError, match="source event_id"):
        assigned_stock_lot_id_for_event("")
    result = project_assigned_stock_lifecycle(
        [{"event_type": "assignment", "position_effect": "close", "account": "lx",
          "raw_payload": {"close_type": "assignment"}}],
        assignment_option_rows=[], option_open_lots=[], assigned_stock_events=[],
        account_norm="lx", broker_norm=None, month=None,
    )
    assert result["assigned_stock_lots"] == []
    assert result["assigned_stock_review_rows"][0]["details"]["reason"] == "event_id_required"


def test_metadata_readers_reject_duplicate_lots_across_accounts():
    opening = _event("open", "open", 1000, lot_id="shared",
                     raw_payload={"strategy": "wheel", "strategy_group_id": "group"})
    other = _event("other", "open", 2000, lot_id="shared", account="sy")
    rows = [opening.to_dict(), other.to_dict()]
    with pytest.raises(ValueError, match="duplicate_lot_id"):
        lot_strategy_metadata_from_trade_events(rows)
    with pytest.raises(ValueError, match="duplicate_lot_id"):
        resolve_combo_group_membership(group_id="group", account="lx",
            trade_events=rows, projected_position_lots=[])


def test_history_does_not_invent_lot_ids_from_arbitrary_references():
    assert _event_record_refs({"event_id": "adjust", "event_type": "adjust",
                              "raw_payload": {"record_id": "historical"}}) == {"adjust", "historical"}
