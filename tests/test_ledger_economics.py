from __future__ import annotations

from decimal import Decimal

from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event, project_trade_events


def _key(*, side: str = "short") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side=side,
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
    if basis:
        raw_payload["fee_provenance"] = {"basis": basis, "source": "test"}
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
        fees=fees,
        lot_id="lot-1" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        raw_payload=raw_payload,
    )


def test_short_partial_close_allocates_open_fee_and_last_close_absorbs_remainder() -> None:
    result = project_trade_events(
        [
            _event("open", "open", contracts=3, price=2, fees=1, basis="actual"),
            _event(
                "close-1", "close", contracts=1, price=1, time_ms=2, fees=0.2, basis="actual", target_lot_id="lot-1"
            ),
            _event(
                "close-2", "close", contracts=2, price=0.5, time_ms=3, fees=0.3, basis="actual", target_lot_id="lot-1"
            ),
        ]
    )

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
    result = project_trade_events(
        [
            _event("open", "open", side="long", price=1, fees=0, basis="actual"),
            _event("close", "close", side="long", price=2.5, time_ms=2, fees=0, basis="actual", target_lot_id="lot-1"),
        ]
    )

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


def test_missing_fee_preserves_gross_and_nulls_net() -> None:
    result = project_trade_events(
        [
            _event("open", "open", price=2),
            _event("close", "close", price=1, time_ms=2, target_lot_id="lot-1"),
        ]
    )

    allocation = result.allocations[0]
    assert allocation.realized_pnl_gross == Decimal("100.000000")
    assert allocation.realized_pnl_net is None
    assert allocation.fee_quality == "missing"


def test_estimated_fee_is_preserved_but_never_used_for_production_realized_net() -> None:
    result = project_trade_events(
        [
            _event("open", "open", price=2, basis="actual"),
            _event(
                "close",
                "close",
                price=1,
                time_ms=2,
                fees=0.4,
                basis="estimated",
                target_lot_id="lot-1",
            ),
        ]
    )

    allocation = result.allocations[0]
    assert allocation.close_fee.amount == Decimal("0.400000")
    assert allocation.fee_quality == "estimated"
    assert allocation.realized_pnl_gross == Decimal("100.000000")
    assert allocation.realized_pnl_net is None


def test_assignment_zero_close_price_and_strategy_metadata_are_stable() -> None:
    open_event = _event(
        "open",
        "open",
        price=2,
        fees=0,
        basis="actual",
        raw={"strategy_snapshot": {"strategy": "sell_put", "leg_role": "put", "strategy_group_id": "g1"}},
    )
    assignment = _event(
        "assign",
        "assignment",
        price=0,
        time_ms=2,
        fees=0,
        basis="actual",
        target_lot_id="lot-1",
        raw={"settlement_ref": "settle-1"},
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


def test_voided_close_produces_no_allocation() -> None:
    key = _key()
    close = _event("close", "close", price=1, time_ms=2, target_lot_id="lot-1")
    void = TradeEvent(
        event_id="void-close",
        event_type="void",
        event_time_ms=3,
        contract_key=key,
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_event_id="close",
    )
    result = project_trade_events([_event("open", "open", price=2), close, void])

    assert result.allocations == []
    assert result.lots[0].contracts_open == 1


def test_void_and_replacement_close_repair_produce_only_replacement_allocation() -> None:
    key = _key()
    original = _event("close-original", "close", price=1, time_ms=2, target_lot_id="lot-1")
    void = TradeEvent(
        event_id="void-original",
        event_type="void",
        event_time_ms=3,
        contract_key=key,
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_event_id=original.event_id,
    )
    replacement = _event(
        "close-replacement",
        "close",
        price=0.75,
        time_ms=4,
        fees=0,
        basis="actual",
        target_lot_id="lot-1",
    )
    result = project_trade_events([_event("open", "open", price=2, basis="actual"), original, void, replacement])

    assert result.diagnostics == []
    assert [item.close_event_id for item in result.allocations] == ["close-replacement"]
    assert result.allocations[0].realized_pnl_gross == Decimal("125.000000")
    assert result.lots[0].contracts_open == 0


def test_allocation_order_and_ids_are_deterministic_for_unsorted_input() -> None:
    events = [
        _event("open", "open", contracts=2, price=2, basis="actual"),
        _event(
            "close-later",
            "close",
            price=0.5,
            time_ms=3,
            basis="actual",
            target_lot_id="lot-1",
        ),
        _event(
            "close-earlier",
            "close",
            price=1,
            time_ms=2,
            basis="actual",
            target_lot_id="lot-1",
        ),
    ]

    ordered = project_trade_events(events)
    reversed_input = project_trade_events(list(reversed(events)))

    expected_close_ids = ["close-earlier", "close-later"]
    assert [item.close_event_id for item in ordered.allocations] == expected_close_ids
    assert [item.close_event_id for item in reversed_input.allocations] == expected_close_ids
    assert [item.allocation_id for item in ordered.allocations] == [
        item.allocation_id for item in reversed_input.allocations
    ]


def test_economic_allocation_failure_keeps_lot_close_and_advances_open_fee_state() -> None:
    result = project_trade_events(
        [
            _event("open", "open", contracts=2, price=2, fees=1, basis="actual"),
            _event(
                "close-invalid-price",
                "close",
                price=float("nan"),
                time_ms=2,
                basis="actual",
                target_lot_id="lot-1",
            ),
            _event(
                "close-valid",
                "close",
                price=1,
                time_ms=3,
                basis="actual",
                target_lot_id="lot-1",
            ),
        ]
    )

    assert result.lots[0].contracts_open == 0
    assert [item.close_event_id for item in result.allocations] == ["close-valid"]
    assert result.allocations[0].allocated_open_fee.amount == Decimal("0.500000")
    assert [item.code for item in result.diagnostics] == ["economic_allocation_failed"]


def test_currency_or_multiplier_mismatch_closes_lot_but_emits_no_economic_allocation() -> None:
    currency_mismatch = project_trade_events(
        [
            _event("open", "open", price=2, basis="actual"),
            _event(
                "close",
                "close",
                price=1,
                time_ms=2,
                basis="actual",
                currency="HKD",
                target_lot_id="lot-1",
            ),
        ]
    )
    multiplier_mismatch = project_trade_events(
        [
            _event("open", "open", price=2, basis="actual"),
            _event(
                "close",
                "close",
                price=1,
                time_ms=2,
                basis="actual",
                multiplier=10,
                target_lot_id="lot-1",
            ),
        ]
    )

    for result in (currency_mismatch, multiplier_mismatch):
        assert result.lots[0].contracts_open == 0
        assert result.allocations == []
        assert [item.code for item in result.diagnostics] == ["target_economic_units_mismatch"]
