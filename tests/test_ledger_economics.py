from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from domain.domain.ledger import (
    ContractKey,
    PositionLot,
    ResumableProjectionState,
    TradeEvent,
    fee_fact_for_event,
    project_resumable_trade_events,
    project_trade_events,
)
from domain.domain.trade_contract_identity import derive_trade_side


def _key(*, side: str = "short") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        strike=100,
        expiration_ymd="2026-08-21",
        )


def _event(
    event_id: str,
    event_type: str,
    *,
    side: str = "short",
    contracts: int = 1,
    price: float = 1.0,
    time_ms: int = 1,
    fees: float = 0.0,
    basis: str | None = None,
    currency: str = "USD",
    multiplier: float = 100,
    target_lot_id: str | None = None,
    raw: dict | None = None,
) -> TradeEvent:
    raw_payload = dict(raw or {})
    # §9.2 step 3: the contract key no longer carries the position side, so the
    # fixture's side travels as the trade side the projection derives it from.
    raw_payload.setdefault("side", derive_trade_side(event_type, side) or "")
    if basis:
        raw_payload["fee_provenance"] = {"basis": basis, "source": "test", "amount": fees}
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=time_ms,
        contract_key=_key(side=side),
        contracts=contracts,
        price=price,
        currency=currency,
        source="test",
        multiplier=multiplier,
        fees=0.0 if basis == "estimated" else fees,
        lot_id="lot-1" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        raw_payload=raw_payload,
    )


def _void(event_id: str, *, key: ContractKey, target_event_id: str) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=3,
        contract_key=key,
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        multiplier=100,
        target_event_id=target_event_id,
    )


def _snapshot(
    *,
    strategy: str = "sell_put",
    leg_role: str = "put",
    strategy_group_id: str = "g1",
) -> dict:
    return {
        "strategy": strategy,
        "leg_role": leg_role,
        "strategy_group_id": strategy_group_id,
    }


def test_short_partial_close_allocates_open_fee_and_last_close_absorbs_remainder() -> None:
    events = [
        _event("open", "open", contracts=3, price=2, fees=1, basis="actual"),
        _event("close-1", "close", contracts=1, price=1, time_ms=2, fees=0.2, basis="actual", target_lot_id="lot-1"),
        _event("close-2", "close", contracts=2, price=0.5, time_ms=3, fees=0.3, basis="actual", target_lot_id="lot-1"),
    ]
    result = project_trade_events(events)

    assert result.diagnostics == []
    assert len(result.allocations) == 2
    first, second = result.allocations
    assert first.realized_pnl_gross == Decimal("100.000000")
    assert first.allocated_open_fee.amount == Decimal("0.333333")
    assert first.realized_pnl_net == Decimal("99.466667")
    assert second.realized_pnl_gross == Decimal("300.000000")
    assert second.allocated_open_fee.amount == Decimal("0.666667")
    assert sum(item.allocated_open_fee.amount or Decimal(0) for item in result.allocations) == Decimal("1.000000")
    # Keep the legacy lot field unchanged for compatibility: it subtracts
    # close-event fees only. Exact gross/net economics live on allocations.
    assert result.lots[0].realized_pnl == 399.5


def test_long_close_uses_opposite_cash_signs() -> None:
    events = [
        _event("open", "open", side="long", price=1, fees=0, basis="actual"),
        _event("close", "close", side="long", price=2.5, time_ms=2, fees=0, basis="actual", target_lot_id="lot-1"),
    ]
    result = project_trade_events(events)

    allocation = result.allocations[0]
    assert allocation.open_amount_gross == Decimal("-100.000000")
    assert allocation.close_amount_gross == Decimal("250.000000")
    assert allocation.realized_pnl_gross == allocation.realized_pnl_net == Decimal("150.000000")


def test_bare_zero_fee_is_missing_but_explicit_actual_zero_is_complete() -> None:
    missing = fee_fact_for_event(_event("open-missing", "open", fees=0))
    actual = fee_fact_for_event(_event("open-zero", "open", fees=0, basis="actual"))

    assert missing.amount is None
    assert missing.basis.value == "missing"
    assert actual.amount == Decimal("0.000000")
    assert actual.basis.value == "actual"


def test_legacy_nonzero_fee_is_actual_and_estimated_provenance_is_preserved() -> None:
    legacy = fee_fact_for_event(_event("legacy-fee", "close", fees=1.25))
    estimated = fee_fact_for_event(_event("estimated-fee", "close", fees=0.4, basis="estimated"))

    assert legacy.amount == Decimal("1.250000")
    assert legacy.basis.value == "actual"
    assert legacy.source == "legacy_nonzero_fees"
    assert estimated.amount == Decimal("0.400000")
    assert estimated.basis.value == "estimated"


def test_invalid_explicit_fee_provenance_fails_closed_instead_of_legacy_inference() -> None:
    event = _event("invalid-basis", "close", fees=1.25, raw={"fee_provenance": {"basis": "actaul"}})

    fee = fee_fact_for_event(event)

    assert fee.amount is None
    assert fee.basis.value == "missing"
    assert "invalid fee provenance basis" in str(fee.reason)


def test_fee_provenance_conflict_with_compatibility_amount_fails_closed() -> None:
    actual = _event(
        "actual-conflict", "close", fees=9,
        raw={"fee_provenance": {"basis": "actual", "amount": 1, "source": "test"}},
    )
    estimated = _event(
        "estimated-conflict", "close", fees=9,
        raw={"fee_provenance": {"basis": "estimated", "amount": 1, "source": "test"}},
    )

    assert fee_fact_for_event(actual).basis.value == "missing"
    assert "conflicts" in str(fee_fact_for_event(actual).reason)
    assert fee_fact_for_event(estimated).basis.value == "missing"
    assert "conflicts" in str(fee_fact_for_event(estimated).reason)


def test_missing_fee_preserves_gross_and_nulls_net() -> None:
    events = [
        _event("open", "open", price=2),
        _event("close", "close", price=1, time_ms=2, target_lot_id="lot-1"),
    ]
    result = project_trade_events(events)

    allocation = result.allocations[0]
    assert allocation.realized_pnl_gross == Decimal("100.000000")
    assert allocation.realized_pnl_net is None
    assert allocation.fee_quality == "missing"


def test_estimated_fee_is_preserved_but_never_used_for_production_realized_net() -> None:
    events = [
        _event("open", "open", price=2, basis="actual"),
        _event("close", "close", price=1, time_ms=2, fees=0.4, basis="estimated", target_lot_id="lot-1"),
    ]
    result = project_trade_events(events)

    allocation = result.allocations[0]
    assert allocation.close_fee.amount == Decimal("0.400000")
    assert allocation.fee_quality == "estimated"
    assert allocation.realized_pnl_gross == Decimal("100.000000")
    assert allocation.realized_pnl_net is None


def test_assignment_zero_close_price_and_strategy_metadata_are_stable() -> None:
    open_event = _event(
        "open", "open", price=2, fees=0, basis="actual",
        raw={"strategy_snapshot": _snapshot()},
    )
    assignment = _event(
        "assign", "assignment", price=0, time_ms=2, fees=0, basis="actual",
        target_lot_id="lot-1", raw={"settlement_ref": "settle-1"},
    )

    first = project_trade_events([open_event, assignment])
    second = project_trade_events([assignment, open_event])
    allocation = first.allocations[0]

    assert allocation.realized_pnl_gross == Decimal("200.000000")
    assert allocation.close_type == "assignment"
    assert allocation.strategy == "sell_put"
    assert allocation.leg_role == "put"
    assert allocation.strategy_group_id == "g1"
    assert allocation.settlement_ref == "settle-1"
    assert allocation.allocation_id == second.allocations[0].allocation_id


def test_conflicting_close_strategy_metadata_cannot_override_open_metadata() -> None:
    events = [
        _event(
            "open", "open", price=2, basis="actual",
            raw={"strategy_snapshot": _snapshot()},
        ),
        _event(
            "close", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1",
            raw={
                "strategy": "wheel",
                "strategy_snapshot": _snapshot(
                    strategy="combo_yield", leg_role="funding_put", strategy_group_id="other"
                ),
            },
        ),
    ]
    result = project_trade_events(events)

    allocation = result.allocations[0]
    assert (allocation.strategy, allocation.leg_role, allocation.strategy_group_id) == (
        "sell_put",
        "put",
        "g1",
    )


def test_voided_close_produces_no_allocation() -> None:
    key = _key()
    close = _event("close", "close", price=1, time_ms=2, target_lot_id="lot-1")
    void = _void("void-close", key=key, target_event_id="close")
    result = project_trade_events([_event("open", "open", price=2), close, void])

    assert result.allocations == []
    assert result.lots[0].contracts_open == 1


def test_void_and_replacement_close_repair_produce_only_replacement_allocation() -> None:
    key = _key()
    original = _event("close-original", "close", price=1, time_ms=2, target_lot_id="lot-1")
    void = _void("void-original", key=key, target_event_id=original.event_id)
    replacement = _event(
        "close-replacement", "close", price=0.75, time_ms=4, fees=0, basis="actual", target_lot_id="lot-1"
    )
    result = project_trade_events([_event("open", "open", price=2, basis="actual"), original, void, replacement])

    assert result.diagnostics == []
    assert [item.close_event_id for item in result.allocations] == ["close-replacement"]
    assert result.allocations[0].realized_pnl_gross == Decimal("125.000000")
    assert result.lots[0].contracts_open == 0


def test_allocation_order_and_ids_are_deterministic_for_unsorted_input() -> None:
    events = [
        _event("open", "open", contracts=2, price=2, basis="actual"),
        _event("close-later", "close", price=0.5, time_ms=3, basis="actual", target_lot_id="lot-1"),
        _event("close-earlier", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1"),
    ]

    ordered = project_trade_events(events)
    reversed_input = project_trade_events(list(reversed(events)))

    expected_close_ids = ["close-earlier", "close-later"]
    assert [item.close_event_id for item in ordered.allocations] == expected_close_ids
    assert [item.close_event_id for item in reversed_input.allocations] == expected_close_ids
    assert [item.allocation_id for item in ordered.allocations] == [
        item.allocation_id for item in reversed_input.allocations
    ]


def test_invalid_close_is_rejected_without_advancing_lot_or_open_fee_state() -> None:
    events = [
        _event("open", "open", contracts=2, price=2, fees=1, basis="actual"),
        _event(
            "close-invalid-price", "close", price=float("nan"), time_ms=2, basis="actual", target_lot_id="lot-1"
        ),
        _event("close-valid", "close", price=1, time_ms=3, basis="actual", target_lot_id="lot-1"),
    ]
    result = project_trade_events(events)

    assert result.lots[0].contracts_open == 1
    assert [item.close_event_id for item in result.allocations] == ["close-valid"]
    assert result.allocations[0].allocated_open_fee.amount == Decimal("0.500000")
    assert [item.code for item in result.diagnostics] == ["event_price_invalid"]


def test_currency_or_multiplier_mismatch_closes_lot_but_emits_no_economic_allocation() -> None:
    currency_mismatch = project_trade_events(
        [
            _event("open", "open", price=2, basis="actual"),
            _event(
                "close", "close", price=1, time_ms=2, basis="actual", currency="HKD", target_lot_id="lot-1"
            ),
        ]
    )
    multiplier_mismatch = project_trade_events(
        [
            _event("open", "open", price=2, basis="actual"),
            _event(
                "close", "close", price=1, time_ms=2, basis="actual", multiplier=10, target_lot_id="lot-1"
            ),
        ]
    )

    for result in (currency_mismatch, multiplier_mismatch):
        assert result.lots[0].contracts_open == 0
        assert result.allocations == []
        assert [item.code for item in result.diagnostics] == ["target_economic_units_mismatch"]


def _premium_adjustment(
    *, premium: object = 3, time_ms: int = 3, event_id: str = "adjust", **patch: object,
) -> TradeEvent:
    return _event(
        event_id, "adjust", contracts=0, price=0, time_ms=time_ms,
        target_lot_id="lot-1", raw={"patch": {"premium": premium, **patch}},
    )


def _assert_partial_restatement(result, *, expected: Decimal, case: str) -> None:
    assert result.diagnostics == ()
    assert len(result.allocations) == 1
    allocation = result.allocations[0]
    assert allocation.realized_pnl_net == expected
    lot = result.retained_lots[0]
    # This structured assertion identifies the original mismatch without
    # accepting an unrelated import, diagnostic or fixture failure as evidence.
    assert lot.realized_pnl == expected, {
        "case": case, "lot": format(lot.realized_pnl.normalize(), "f"),
        "expected": format(expected.normalize(), "f"),
        "allocation_net": format(allocation.realized_pnl_net.normalize(), "f"),
    }
    assert result.active_lots == (lot,)
    assert (lot.contracts_opened, lot.contracts_open, lot.contracts_closed) == (2, 1, 1)
    assert lot.close_event_ids == ("close",)
    assert result.transitions[-1].lot_after == lot
    assert result.state is not None and result.eligible
    assert result.state.active_lots[0].realized_pnl == expected


def test_late_premium_adjustment_restates_lot_realized_pnl() -> None:
    result = project_resumable_trade_events([
        _event("open", "open", contracts=2, price=2, basis="actual"),
        _event("close", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1"),
        _premium_adjustment(),
    ])
    _assert_partial_restatement(result, expected=Decimal("200"), case="late-premium")
    assert result.retained_lots[0].last_event_id == "adjust"


def test_premium_adjustment_before_close_matches_effective_history() -> None:
    result = project_resumable_trade_events([
        _event("open", "open", contracts=2, price=2, basis="actual"),
        _premium_adjustment(time_ms=2),
        _event("close", "close", price=1, time_ms=3, basis="actual", target_lot_id="lot-1"),
    ])
    _assert_partial_restatement(result, expected=Decimal("200"), case="before-close")
    assert result.retained_lots[0].last_event_id == "close"


def test_same_premium_adjustment_preserves_lot_realized_pnl() -> None:
    result = project_resumable_trade_events([
        _event("open", "open", contracts=2, price=2, basis="actual"),
        _event("close", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1"),
        _premium_adjustment(premium=2),
    ])
    _assert_partial_restatement(result, expected=Decimal("100"), case="same-premium")
    assert result.retained_lots[0].last_event_id == "adjust"


def test_premium_restatement_preserves_lot_fee_contract() -> None:
    for side, basis, expected_lot, expected_net in (
        ("short", "actual", Decimal("198"), Decimal("196.5")),
        ("long", "actual", Decimal("-202"), Decimal("-203.5")),
        ("short", None, Decimal("200"), None),
        ("short", "estimated", Decimal("200"), None),
    ):
        result = project_resumable_trade_events([
            _event("open", "open", side=side, contracts=2, price=2, fees=3, basis="actual"),
            _event(
                "close", "close", side=side, price=1, time_ms=2,
                fees=0 if basis is None else 2, basis=basis, target_lot_id="lot-1",
            ),
            _premium_adjustment(),
        ])
        assert result.eligible
        assert result.retained_lots[0].realized_pnl == expected_lot, (side, basis)
        assert result.allocations[0].realized_pnl_net == expected_net, (side, basis)
        assert result.allocations[0].allocated_open_fee.amount == Decimal("1.5")
        assert result.state.active_lots[0].allocated_open_fee == Decimal("1.5")
    no_close = project_resumable_trade_events([
        _event("open", "open", contracts=2, price=2, fees=3, basis="actual"),
        _premium_adjustment(),
    ])
    assert no_close.eligible and no_close.allocations == ()
    assert no_close.retained_lots[0].realized_pnl == Decimal("0")


def test_premium_restatement_updates_every_transition_prefix() -> None:
    events = [
        _event("open", "open", contracts=3, price=2, basis="actual"),
        _event("close-1", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1"),
        _premium_adjustment(event_id="adjust-1"),
        _event("close-2", "close", price=0.5, time_ms=4, basis="actual", target_lot_id="lot-1"),
        _premium_adjustment(event_id="adjust-2", premium=4, time_ms=5),
    ]
    other = replace(
        _event("other-open", "open", price=8, basis="actual"),
        lot_id="lot-other", contract_key=replace(_key(), account="sy"),
    )
    result = project_resumable_trade_events([*events, other])
    assert result.eligible
    transitions = [item for item in result.transitions if item.lot_after.lot_id == "lot-1"]
    expected_prefixes = ((None, 0), (0, 300), (300, 300), (300, 650), (650, 650))
    chronological = None
    for event, transition, (before_pnl, after_pnl) in zip(events, transitions, expected_prefixes, strict=True):
        if chronological is None:
            assert transition.lot_before is None
            chronological = PositionLot.from_open_event(event, lot_id="lot-1")
        else:
            assert transition.lot_before == replace(chronological, realized_pnl=Decimal(before_pnl))
            chronological = (
                chronological.apply_close(event, actual_fee_amount=Decimal("0"))
                if event.event_type == "close" else chronological.apply_adjust(event)
            )
        assert transition.lot_after == replace(chronological, realized_pnl=Decimal(after_pnl))
        assert transition.event == event
    expected_other = PositionLot.from_open_event(other, lot_id="lot-other")
    assert next(lot for lot in result.retained_lots if lot.lot_id == "lot-other") == expected_other
    final = next(lot for lot in result.active_lots if lot.lot_id == "lot-1")
    assert final == transitions[-1].lot_after
    assert final.close_event_ids == ("close-1", "close-2")
    assert final.last_event_id == "adjust-2"


def test_premium_restatement_full_then_serialized_tail_matches_full() -> None:
    events = [
        _event("open", "open", contracts=4, price=2, fees=3, basis="actual"),
        _event("close-1", "close", price=1, time_ms=2, fees=2, basis="actual", target_lot_id="lot-1"),
        _premium_adjustment(premium=Decimal("3.123456789")),
        _event("close-2", "close", price=0.5, time_ms=4, fees=0.25, basis="actual", target_lot_id="lot-1"),
        _event("close-3", "close", contracts=2, price=0.25, time_ms=5, fees=0.5, basis="actual", target_lot_id="lot-1"),
    ]
    prefix = project_resumable_trade_events(events[:3])
    assert prefix.eligible
    assert prefix.retained_lots[0].realized_pnl == Decimal("210.3456789")
    state = ResumableProjectionState.from_json_bytes(prefix.state.to_json_bytes())
    assert state == prefix.state
    assert state.active_lots[0].allocated_open_fee == Decimal("0.75")
    empty = project_resumable_trade_events([], initial_state=state, entry_mode="tail")
    assert empty.state == state and empty.active_lots == prefix.active_lots
    control = project_resumable_trade_events([events[2]], initial_state=state, entry_mode="tail")
    assert control.requires_full_replay and control.full_replay_reason == "tail_control_event"
    for end, expected in ((4, Decimal("472.4413578")), (5, Decimal("1046.6327156"))):
        resumed = project_resumable_trade_events([events[end - 1]], initial_state=state, entry_mode="tail")
        full = project_resumable_trade_events(events[:end])
        assert resumed.eligible and full.eligible
        assert resumed.retained_lots == full.retained_lots
        assert resumed.active_lots == full.active_lots
        assert resumed.state == full.state
        assert resumed.retained_lots[0].realized_pnl == expected
        assert resumed.retained_lots[0].last_event_id == events[end - 1].event_id
        assert resumed.retained_lots[0].close_event_ids == tuple(f"close-{i}" for i in range(1, end - 1))
        state = ResumableProjectionState.from_json_bytes(resumed.state.to_json_bytes())
    assert resumed.active_lots == () and state.active_lots == ()
    assert resumed.retained_lots[0].status == "close"
    assert resumed.allocations[0].allocated_open_fee.amount == Decimal("1.5")


def test_premium_restatement_preserves_invalid_history_diagnostics() -> None:
    opened = _event("open", "open", contracts=2, price=2, basis="actual")
    closed = _event("close", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1")
    adjusted = _premium_adjustment()
    for target, expected, close_ids in (("adjust", Decimal("100"), ("close",)), ("close", Decimal("0"), ())):
        void = replace(_void("void", key=_key(), target_event_id=target), event_time_ms=4)
        result = project_resumable_trade_events([opened, closed, adjusted, void])
        assert result.eligible
        assert result.retained_lots[0].realized_pnl == expected
        assert result.retained_lots[0].close_event_ids == close_ids
        assert target not in [item.event.event_id for item in result.transitions]
    rejected = replace(closed, event_id="rejected", event_time_ms=4, contracts=3)
    result = project_resumable_trade_events([opened, closed, adjusted, rejected])
    assert result.retained_lots[0].realized_pnl == Decimal("200")
    assert result.retained_lots[0].close_event_ids == ("close",)
    assert [item.code for item in result.diagnostics] == ["close_contracts_exceed_open"]
    assert result.state is None and not result.eligible
    invalid = project_resumable_trade_events([opened, closed, _premium_adjustment(opened_at=4)])
    assert invalid.retained_lots[0].realized_pnl == Decimal("100")
    assert invalid.allocations == () and invalid.effective_open_events == ()
    assert [item.code for item in invalid.diagnostics] == ["economic_adjust_invalid"]
    assert invalid.state is None and not invalid.eligible
    for mismatch in (replace(closed, currency="HKD"), replace(closed, multiplier=10)):
        result = project_resumable_trade_events([opened, mismatch, adjusted])
        assert result.allocations == ()
        assert result.retained_lots[0].realized_pnl == Decimal("200")
        assert result.retained_lots[0].close_event_ids == ("close",)
        assert result.transitions[1].lot_after.realized_pnl == Decimal("200")
        assert [item.code for item in result.diagnostics] == ["target_economic_units_mismatch"]
        assert result.state is None and not result.eligible
    stock_key = ContractKey.from_values(
        broker="futu", account="sy", underlying_symbol="NVDA", asset_type="stock",
        option_type="", strike=0, expiration_ymd="",
    )
    stock_open = TradeEvent(
        event_id="stock-open", event_type="open", event_time_ms=1, contract_key=stock_key,
        contracts=Decimal("1.25"), price=10, currency="USD", source="test", lot_id="stock-lot",
        raw_payload={"side": "buy"}, asset_type="stock",
    )
    stock_close = replace(
        stock_open, event_id="stock-close", event_type="close", event_time_ms=2,
        contracts=Decimal("0.5"), price=12, lot_id=None, target_lot_id="stock-lot", raw_payload={"side": "sell"},
    )
    mixed = project_resumable_trade_events([opened, closed, adjusted, stock_open, stock_close])
    assert mixed.eligible
    stock = next(lot for lot in mixed.retained_lots if lot.asset_type == "stock")
    assert (stock.shares_opened, stock.shares_open, stock.shares_closed) == (
        Decimal("1.25"), Decimal("0.75"), Decimal("0.5"),
    )
    assert stock.realized_pnl is None and stock.cost_basis_total is None
    assert stock.close_event_ids == ("stock-close",)


def test_premium_restatement_missing_close_prefix_preserves_final_structure() -> None:
    opened = _event("open", "open", contracts=2, price=2, basis="actual")
    closed = _event("close", "close", price=1, time_ms=2, basis="actual", target_lot_id="lot-1")
    adjusted = _premium_adjustment(strike=105)
    result = project_resumable_trade_events([opened, closed, adjusted])
    original_open = PositionLot.from_open_event(opened, lot_id="lot-1")
    original_closed = original_open.apply_close(closed, actual_fee_amount=Decimal("0"))
    original_adjusted = original_closed.apply_adjust(adjusted)
    assert result.retained_lots == result.active_lots == (original_adjusted,)
    assert (original_adjusted.contracts_opened, original_adjusted.contracts_open, original_adjusted.contracts_closed) == (2, 1, 1)
    assert original_adjusted.contract_key.strike == Decimal("105")
    assert original_adjusted.premium_open == Decimal("3")
    assert original_adjusted.status == "open" and original_adjusted.realized_pnl == Decimal("100")
    assert original_adjusted.last_event_id == "adjust" and original_adjusted.close_event_ids == ("close",)
    assert result.transitions[1].lot_before == original_open
    assert result.transitions[1].lot_after == result.transitions[2].lot_before == original_closed
    assert result.transitions[2].lot_after == original_adjusted
    assert result.allocations == ()
    assert [item.code for item in result.diagnostics] == ["target_contract_mismatch"]
    assert result.diagnostics[0].severity == "error"
    assert result.state is None and not result.eligible
