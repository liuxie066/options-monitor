from copy import deepcopy
from dataclasses import replace

import pytest

from domain.domain.wheel import (
    attach_lot_strategy_metadata,
    build_wheel_event,
    lot_strategy_metadata_from_trade_events,
    project_wheel_branches,
    project_wheel_coverage,
    project_wheel_lifecycles,
)
from src.application.ledger import assigned_stock_projection as assigned
from src.application.ledger.api import project_position_lots_and_assigned_stock_from_rows
from src.application.ledger.queries import project_trade_event_log
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from tests.test_performance_assignment import _assign_put, _trade
from tests.test_wheel_strategy import _started_event


def _old_inputs(rows, instant):
    """Independent original composition: deliberately replay assigned stock again."""
    projected = project_trade_event_log(rows.get("trade_events") or [])
    metadata = lot_strategy_metadata_from_trade_events(rows.get("trade_events") or [])
    lots = [{"record_id": item.lot_id, "fields": attach_lot_strategy_metadata(
        {"record_id": item.lot_id, "fields": dict(item.fields)}, metadata,
    )} for item in projected.lots]
    stock = assigned.project_assigned_stock_lifecycle_from_rows(
        {**rows, "account_position_lots": lots}, account="lx", as_of_ms=instant,
    )
    return lots, stock


@pytest.mark.parametrize("case", ["empty", "open", "assignment", "later_void", "fallback_time"])
def test_composed_inputs_equal_independent_projection_and_preserve_rows(monkeypatch, case):
    rows = {"trade_events": []}
    if case != "empty":
        rows["trade_events"] = [_trade().to_dict()]
    if case in {"assignment", "later_void"}:
        rows["trade_events"].append(_assign_put().to_dict())
    if case == "later_void":
        rows["trade_events"].append(_trade(
            event_id="void-assignment", event_type="void", event_time_ms=6000,
            contracts=0, price=0, lot_id=None, target_event_id="assign-put",
        ).to_dict())
    if case == "fallback_time":
        # Wheel admits event_time_ms=0; assigned-stock's fallback excludes it.
        rows["trade_events"][0].update(event_time_ms=0, trade_time_ms=6000)
    original = deepcopy(rows)
    expected = _old_inputs(rows, 4000)
    calls = []
    real = assigned.project_trade_event_log

    def counted(events):
        calls.append(deepcopy(events))
        return real(events)

    monkeypatch.setattr(assigned, "project_trade_event_log", counted)
    actual = project_position_lots_and_assigned_stock_from_rows(rows, account="lx", as_of_ms=4000)
    assert actual == expected
    assert rows == original
    assert len(calls) == (2 if case == "fallback_time" else 1)


def test_composition_preserves_first_error_before_assigned_stock_trust():
    rows = {"trade_events": [{"event_id": "bad", "event_type": "open", "event_time_ms": "bad-time"}]}
    with pytest.raises(ValueError) as original:
        _old_inputs(rows, 4000)
    with pytest.raises(ValueError) as optimized:
        project_position_lots_and_assigned_stock_from_rows(rows, account="lx", as_of_ms=4000)
    assert str(optimized.value) == str(original.value)
    assert "bad-time" in str(optimized.value)
    with pytest.raises(ValueError, match="untrusted"):
        assigned.project_assigned_stock_lifecycle_from_rows(rows, account="lx", as_of_ms=4000)


@pytest.mark.parametrize("market", [None, "us", "hk"])
@pytest.mark.parametrize("kind", ["legacy", "v2"])
def test_real_wheel_projection_equivalence_and_replay_counts(monkeypatch, kind, market):
    wheel_event = _started_event() if kind == "legacy" else build_wheel_event(
        event_id="branch-created", account="lx", wheel_branch_id="wheel-put:child",
        lot_id=None, event_type="wheel_branch_created", occurred_at_ms=2500,
        recorded_at_ms=2501, payload={"direction": "put", "parent_branch_id": "parent"},
    )
    call = _trade(event_id="call", event_time_ms=2500, lot_id="call-lot",
                  contract_key=replace(_trade().contract_key, option_type="call"),
                  raw_payload={"side": "sell", "strategy": "wheel", "leg_role": "wheel_call",
                               "source_stock_lot_id": "assigned-stock-assign-put"})
    rows = {"trade_events": [_trade().to_dict(), _assign_put().to_dict(), call.to_dict()],
            "account_wheel_events": [wheel_event]}
    original = deepcopy(rows)
    lots, stock = _old_inputs(rows, 4000)
    # Old read model first ran lifecycles, then branches (which runs it again).
    project_wheel_lifecycles([wheel_event], rows["trade_events"], lots, stock, 4000)
    expected = project_wheel_branches([wheel_event], rows["trade_events"], lots, stock, 4000)
    from domain.domain.symbol_identity import symbol_market
    if market:
        expected = [row for row in expected if symbol_market(row.get("symbol")) == market.upper()]
    for branch in expected:
        branch["monitoring_gate"] = "disabled"
        branch["coverage"] = project_wheel_coverage(branch)
    calls = []
    real = assigned.project_trade_event_log
    monkeypatch.setattr(assigned, "project_trade_event_log", lambda events: (calls.append(len(events)), real(events))[1])
    import domain.domain.wheel.projection as wheel_projection
    real_lifecycle = wheel_projection.project_wheel_lifecycles
    lifecycle_calls = []
    monkeypatch.setattr(wheel_projection, "project_wheel_lifecycles",
                        lambda *a, **kw: (lifecycle_calls.append(1), real_lifecycle(*a, **kw))[1])
    actual = build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=4000, market=market)
    # Presentation enrichment must leave the canonical projection unchanged.
    enriched = deepcopy(actual["wheel_branches"])
    for branch in enriched:
        contracts = branch.pop("active_option_contracts")
        active_ids = branch.get("active_option_lot_ids") or branch.get("active_call_lot_ids") or []
        assert [item["lot_id"] for item in contracts] == active_ids
        for item in contracts:
            assert item["underlying_symbol"] == "NVDA"
            assert item["option_type"] == "call"
            assert item["strike"] == "100"
            assert item["expiration_ymd"] == "2026-08-21"
    assert enriched == expected
    assert actual["assigned_stock_projection"] == stock
    assert rows == original
    assert calls == [3]
    assert len(lifecycle_calls) == 1
