from __future__ import annotations

from domain.domain.ledger.events import TradeEvent, lot_id_for_open_event
from domain.domain.ledger.identity import ContractKey
from domain.domain.performance.attribution import resolve_event_attribution
from domain.domain.performance.engine import _open_event_facts


def _open_event(*, event_id: str = "put-open", raw_payload: dict | None = None) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1_780_000_000_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-07-31",
        ),
        contracts=1,
        price=5,
        currency="USD",
        multiplier=100,
        source="test",
        raw_payload=raw_payload or {},
    )


def test_combo_yield_funding_cycle_identity_comes_from_open_lot() -> None:
    event = _open_event(
        raw_payload={
            "strategy_snapshot": {
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "strategy_group_id": "combo_yield:lx:pair-1",
                "expiry_structure": "same_expiry",
            }
        }
    )

    resolved = resolve_event_attribution(event, lifecycle_source_id=lot_id_for_open_event(event))

    assert resolved.issues == ()
    assert resolved.attribution is not None
    assert resolved.attribution.lifecycle_id == f"funding_cycle:{lot_id_for_open_event(event)}"
    assert resolved.attribution.expiry_structure == "same_expiry"


def test_production_form_without_expiry_structure_keeps_none() -> None:
    event = _open_event(
        raw_payload={
            "strategy_snapshot": {
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "strategy_group_id": "combo_yield:lx:pair-1",
            }
        }
    )

    resolved = resolve_event_attribution(event, lifecycle_source_id=lot_id_for_open_event(event))

    assert resolved.issues == ()
    assert resolved.attribution is not None
    assert resolved.attribution.expiry_structure is None


def test_legacy_structure_mode_maps_to_expiry_structure() -> None:
    staggered = _open_event(
        raw_payload={
            "strategy_snapshot": {
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "strategy_group_id": "combo_yield:lx:pair-1",
                "structure_mode": "staggered_expiry_pair",
            }
        }
    )
    same_expiry = _open_event(
        raw_payload={
            "strategy_snapshot": {
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "strategy_group_id": "combo_yield:lx:pair-1",
                "structure_mode": "same_expiry_pair",
            }
        }
    )

    staggered_resolved = resolve_event_attribution(
        staggered, lifecycle_source_id=lot_id_for_open_event(staggered)
    )
    same_expiry_resolved = resolve_event_attribution(
        same_expiry, lifecycle_source_id=lot_id_for_open_event(same_expiry)
    )

    assert staggered_resolved.issues == ()
    assert staggered_resolved.attribution is not None
    assert staggered_resolved.attribution.expiry_structure == "diagonal"
    assert same_expiry_resolved.attribution is not None
    assert same_expiry_resolved.attribution.expiry_structure == "same_expiry"


def test_snapshot_and_top_level_conflict_fails_closed_for_attribution() -> None:
    event = _open_event(
        raw_payload={
            "strategy_group_id": "combo_yield:lx:other",
            "strategy_snapshot": {
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "strategy_group_id": "combo_yield:lx:pair-1",
            },
        }
    )

    resolved = resolve_event_attribution(event, lifecycle_source_id=lot_id_for_open_event(event))

    assert resolved.attribution is None
    assert resolved.issues == (f"strategy_metadata_conflict:{event.event_id}:strategy_group_id",)


def test_untagged_open_event_keeps_canonical_facts_without_attribution() -> None:
    event = _open_event()

    facts = _open_event_facts(event)

    assert facts
    assert all(fact.attribution is None for fact in facts)
    assert sum(fact.quantity or 0 for fact in facts if fact.fact_kind == "contracts_opened") == 1


def test_attributed_fact_serialization_is_additive() -> None:
    event = _open_event(
        raw_payload={
            "strategy": "combo_yield",
            "leg_role": "funding_put",
            "strategy_group_id": "combo_yield:lx:pair-1",
        }
    )

    row = _open_event_facts(event)[0].to_dict()

    assert row["attribution"] == {
        "strategy": "combo_yield",
        "leg_role": "funding_put",
        "strategy_group_id": "combo_yield:lx:pair-1",
        "lifecycle_id": f"funding_cycle:{lot_id_for_open_event(event)}",
        "expiry_structure": None,
    }


def test_close_cash_facts_reuse_target_lot_lifecycle_identity() -> None:
    from domain.domain.performance.engine import _close_event_cash_facts

    open_event = _open_event(
        raw_payload={
            "strategy": "combo_yield",
            "leg_role": "funding_put",
            "strategy_group_id": "combo_yield:lx:pair-1",
        }
    )
    lot_id = lot_id_for_open_event(open_event)
    close_event = TradeEvent(
        event_id="put-close",
        event_type="expire_close",
        event_time_ms=1_782_000_000_000,
        contract_key=open_event.contract_key,
        contracts=1,
        price=0,
        currency="USD",
        multiplier=100,
        source="test",
        target_lot_id=lot_id,
        raw_payload=dict(open_event.raw_payload),
    )

    facts = _close_event_cash_facts(close_event)

    assert facts
    assert all(fact.attribution is not None for fact in facts)
    assert {fact.attribution.lifecycle_id for fact in facts if fact.attribution} == {f"funding_cycle:{lot_id}"}


def test_tagged_close_without_target_lot_fails_closed_for_attribution() -> None:
    event = TradeEvent(
        event_id="orphan-close",
        event_type="close",
        event_time_ms=1_782_000_000_000,
        contract_key=_open_event().contract_key,
        contracts=1,
        price=1,
        currency="USD",
        multiplier=100,
        source="test",
        raw_payload={
            "strategy": "combo_yield",
            "leg_role": "funding_put",
            "strategy_group_id": "combo_yield:lx:pair-1",
        },
    )

    resolved = resolve_event_attribution(event)

    assert resolved.attribution is None
    assert resolved.issues == ("strategy_lifecycle_source_missing:orphan-close",)
