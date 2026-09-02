from __future__ import annotations

import json
import random

import pytest

from domain.domain.ledger import (
    ContractKey,
    ResumableProjectionState,
    TradeEvent,
    project_resumable_trade_events,
    project_trade_events,
)
from src.application.ledger.publisher import (
    ResumablePublicationState,
    project_stored_trade_events_to_position_lots,
    project_stored_trade_events_to_resumable_position_lots,
)


def _key(
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    strike: float = 100.0,
    option_type: str = "put",
    side: str = "short",
) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd="2026-06-19",
    )


def _event(
    event_id: str,
    event_type: str,
    event_time_ms: int,
    *,
    key: ContractKey,
    contracts: int,
    price: float,
    lot_id: str | None = None,
    target_lot_id: str | None = None,
    fees: float = 0.0,
    patch: dict[str, object] | None = None,
    raw_payload: dict[str, object] | None = None,
) -> TradeEvent:
    payload = dict(raw_payload or {})
    if patch is not None:
        payload["patch"] = dict(patch)
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=key,
        contracts=contracts,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=fees,
        lot_id=lot_id,
        target_lot_id=target_lot_id,
        raw_payload=payload,
    )


def _sequence() -> list[TradeEvent]:
    lx_put = _key()
    sy_call = _key(
        account="sy",
        symbol="AAPL",
        strike=200,
        option_type="call",
        side="long",
    )
    return [
        _event(
            "open-lx",
            "open",
            1_000,
            key=lx_put,
            contracts=5,
            price=2.5,
            fees=1.03,
            lot_id="lot-lx",
            raw_payload={
                "source_type": "manual_trade_event",
                "strategy": "sell_put",
            },
        ),
        _event(
            "open-sy",
            "open",
            2_000,
            key=sy_call,
            contracts=2,
            price=1.2,
            fees=0.44,
            lot_id="lot-sy",
            raw_payload={
                "strategy": "combo_yield",
                "leg_role": "enhancement_call",
            },
        ),
        _event(
            "close-lx-1",
            "close",
            3_000,
            key=lx_put,
            contracts=2,
            price=1.5,
            fees=0.2,
            target_lot_id="lot-lx",
            raw_payload={"close_reason": "partial_profit"},
        ),
        _event(
            "adjust-lx",
            "adjust",
            4_000,
            key=lx_put,
            contracts=0,
            price=0,
            target_lot_id="lot-lx",
            patch={
                "strategy": "yield_enhancement",
                "leg_role": "funding_put",
                "note": "resumed",
            },
        ),
        _event(
            "close-sy",
            "close",
            5_000,
            key=sy_call,
            contracts=2,
            price=2.1,
            fees=0.3,
            target_lot_id="lot-sy",
        ),
        _event(
            "close-lx-2",
            "close",
            6_000,
            key=lx_put,
            contracts=1,
            price=1.0,
            fees=0.1,
            target_lot_id="lot-lx",
        ),
    ]


def _lots_payload(lots: object) -> list[dict[str, object]]:
    return [item.to_dict() for item in lots]  # type: ignore[union-attr]


def _records_by_id(records: object) -> dict[str, dict[str, object]]:
    return {
        item.record_id: item.to_dict()  # type: ignore[union-attr]
        for item in records  # type: ignore[union-attr]
    }


def test_resumable_state_round_trip_is_canonical_active_only_and_bounded() -> None:
    key = _key()
    events = [
        _event(
            "open",
            "open",
            1_000,
            key=key,
            contracts=50,
            price=2.5,
            fees=1.03,
            lot_id="lot-a",
        ),
        *[
            _event(
                f"close-{index:02d}",
                "close",
                2_000 + index,
                key=key,
                contracts=1,
                price=1.5,
                target_lot_id="lot-a",
            )
            for index in range(25)
        ],
    ]
    result = project_resumable_trade_events(events, entry_mode="full")
    assert result.eligible is True
    assert result.state is not None
    payload = result.state.to_json_bytes()
    assert b"close_event_ids" not in payload
    assert b'"last_close_event_id":"close-24"' in payload
    assert result.retained_lots[0].close_event_ids == tuple(
        f"close-{index:02d}" for index in range(25)
    )
    assert ResumableProjectionState.from_json_bytes(payload) == result.state

    noncanonical = json.dumps(result.state.to_dict()).encode()
    assert noncanonical != payload
    with pytest.raises(ValueError, match="not canonical"):
        ResumableProjectionState.from_json_bytes(noncanonical)

    one_closed = project_resumable_trade_events(
        [
            _event(
                "open-one",
                "open",
                1,
                key=key,
                contracts=1,
                price=1,
                lot_id="lot-one",
            ),
            _event(
                "close-one",
                "close",
                2,
                key=key,
                contracts=1,
                price=0,
                target_lot_id="lot-one",
            ),
        ],
        entry_mode="full",
    )
    many_closed_events: list[TradeEvent] = []
    for index in range(100):
        lot_id = f"lot-{index:03d}"
        many_closed_events.extend(
            [
                _event(
                    f"open-{index:03d}",
                    "open",
                    10 + index * 2,
                    key=key,
                    contracts=1,
                    price=1,
                    lot_id=lot_id,
                ),
                _event(
                    f"close-{index:03d}",
                    "close",
                    11 + index * 2,
                    key=key,
                    contracts=1,
                    price=0,
                    target_lot_id=lot_id,
                ),
            ]
        )
    many_closed = project_resumable_trade_events(
        many_closed_events,
        entry_mode="full",
    )
    assert one_closed.state is not None
    assert many_closed.state is not None
    assert one_closed.state.to_json_bytes() == many_closed.state.to_json_bytes()


def test_resumable_state_rejects_impossible_balances_and_trims_open_payload() -> None:
    key = _key()
    result = project_resumable_trade_events(
        [
            _event(
                "open",
                "open",
                1,
                key=key,
                contracts=3,
                price=1,
                fees=0.25,
                lot_id="lot-a",
                raw_payload={
                    "fields": {"opaque": "x" * 50_000},
                    "strategy": "sell_put",
                    "fee_provenance": {
                        "basis": "actual",
                        "amount": 0.25,
                        "source": "test",
                    },
                },
            )
        ],
        entry_mode="full",
    )
    assert result.state is not None
    canonical = result.state.to_json_bytes()
    assert b'"fields"' not in canonical
    assert b'"strategy":"sell_put"' in canonical
    assert len(canonical) < 2_500

    invalid = result.state.to_dict()
    invalid["active_lots"][0]["contracts_open"] = 4
    invalid_bytes = json.dumps(
        invalid,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    with pytest.raises(ValueError, match="contract balance"):
        ResumableProjectionState.from_json_bytes(invalid_bytes)

    noncanonical_decimal = canonical.replace(
        b'"allocated_open_fee":"0"',
        b'"allocated_open_fee":"0.00"',
    )
    assert noncanonical_decimal != canonical
    with pytest.raises(ValueError, match="not canonical"):
        ResumableProjectionState.from_json_bytes(noncanonical_decimal)


def test_domain_full_then_tail_matches_full_at_every_prefix() -> None:
    events = _sequence()
    full = project_resumable_trade_events(events, entry_mode="full")
    historical = project_trade_events(events)
    assert full.eligible is True
    assert historical.diagnostics == []
    assert _lots_payload(full.retained_lots) == _lots_payload(historical.lots)

    for prefix_length in range(len(events) + 1):
        prefix = project_resumable_trade_events(
            events[:prefix_length],
            entry_mode="full",
        )
        assert prefix.state is not None
        resumed = project_resumable_trade_events(
            events[prefix_length:],
            initial_state=ResumableProjectionState.from_json_bytes(
                prefix.state.to_json_bytes()
            ),
            entry_mode="tail",
        )
        if any(
            event.event_type == "adjust"
            for event in events[prefix_length:]
        ):
            assert resumed.requires_full_replay is True
            assert resumed.full_replay_reason == "tail_control_event"
            continue
        assert resumed.eligible is True
        assert resumed.state == full.state
        assert _lots_payload(resumed.active_lots) == _lots_payload(full.active_lots)
        assert [item.to_dict() for item in resumed.views] == [
            item.to_dict() for item in full.views
        ]
        assert [item.to_dict() for item in (*prefix.allocations, *resumed.allocations)] == [
            item.to_dict() for item in full.allocations
        ]


def test_resume_preserves_fee_snapshot_and_skips_verification_history() -> None:
    key = _key()
    events = [
        _event(
            "open",
            "open",
            1,
            key=key,
            contracts=2,
            price=2,
            lot_id="lot-a",
            raw_payload={
                "fee_provenance": {
                    "basis": "actual",
                    "amount": 0,
                    "source": "broker",
                },
                "strategy_snapshot": {
                    "strategy": "sell_put",
                    "leg_role": "funding_put",
                    "strategy_group_id": "group-a",
                    "unused": "not-economic-state",
                },
            },
        ),
        TradeEvent(
            event_id="verify",
            event_type="verification",
            event_time_ms=2,
            contract_key=key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            raw_payload={"opaque": "x" * 50_000},
        ),
        _event(
            "partial",
            "close",
            3,
            key=key,
            contracts=1,
            price=1,
            fees=0,
            target_lot_id="lot-a",
            raw_payload={
                "fee_provenance": {
                    "basis": "actual",
                    "amount": 0,
                    "source": "broker",
                }
            },
        ),
        _event(
            "final",
            "close",
            4,
            key=key,
            contracts=1,
            price=0.5,
            fees=0,
            target_lot_id="lot-a",
            raw_payload={
                "fee_provenance": {
                    "basis": "actual",
                    "amount": 0,
                    "source": "broker",
                }
            },
        ),
    ]
    full = project_resumable_trade_events(events, entry_mode="full")
    prefix = project_resumable_trade_events(events[:3], entry_mode="full")
    assert prefix.state is not None
    assert b'"basis":"actual"' in prefix.state.to_json_bytes()
    assert b'"amount":"0"' in prefix.state.to_json_bytes()
    assert b'"unused"' not in prefix.state.to_json_bytes()
    assert [item.event.event_id for item in prefix.transitions] == [
        "open",
        "partial",
    ]

    resumed = project_resumable_trade_events(
        events[3:],
        initial_state=ResumableProjectionState.from_json_bytes(
            prefix.state.to_json_bytes()
        ),
        entry_mode="tail",
    )
    assert resumed.eligible is True
    assert [item.to_dict() for item in (*prefix.allocations, *resumed.allocations)] == [
        item.to_dict() for item in full.allocations
    ]
    assert full.allocations[-1].realized_pnl_net is not None
    assert full.allocations[-1].strategy == "sell_put"
    assert full.allocations[-1].leg_role == "funding_put"
    assert full.allocations[-1].strategy_group_id == "group-a"


def test_seeded_valid_sequences_resume_at_every_prefix() -> None:
    for seed in range(12):
        randomizer = random.Random(seed)
        events: list[TradeEvent] = []
        keys = [_key(strike=100 + index * 5) for index in range(3)]
        remaining: dict[str, int] = {}
        clock = 1_000
        for index, key in enumerate(keys):
            contracts = randomizer.randint(2, 6)
            lot_id = f"lot-{seed}-{index}"
            events.append(
                _event(
                    f"open-{seed}-{index}",
                    "open",
                    clock,
                    key=key,
                    contracts=contracts,
                    price=2 + index,
                    fees=0.91 + index,
                    lot_id=lot_id,
                )
            )
            remaining[lot_id] = contracts
            clock += 1
        for step in range(15):
            active = [lot_id for lot_id, count in remaining.items() if count > 0]
            if not active:
                break
            lot_id = randomizer.choice(active)
            index = int(lot_id.rsplit("-", 1)[-1])
            quantity = randomizer.randint(1, remaining[lot_id])
            events.append(
                _event(
                    f"close-{seed}-{step}",
                    "close",
                    clock,
                    key=keys[index],
                    contracts=quantity,
                    price=1 + step / 10,
                    fees=0.07,
                    target_lot_id=lot_id,
                )
            )
            remaining[lot_id] -= quantity
            clock += 1

        full = project_resumable_trade_events(events, entry_mode="full")
        assert full.eligible is True
        for prefix_length in range(len(events) + 1):
            prefix = project_resumable_trade_events(
                events[:prefix_length],
                entry_mode="full",
            )
            assert prefix.state is not None
            resumed = project_resumable_trade_events(
                events[prefix_length:],
                initial_state=prefix.state,
                entry_mode="tail",
            )
            assert resumed.eligible is True
            assert resumed.state == full.state


def test_stateful_publisher_matches_full_fields_at_every_prefix() -> None:
    events = _sequence()
    oracle = project_stored_trade_events_to_position_lots(events)
    full = project_stored_trade_events_to_resumable_position_lots(
        events,
        entry_mode="full",
    )
    assert full.eligible is True
    assert _records_by_id(full.touched_lots) == _records_by_id(oracle.lots)

    for prefix_length in range(len(events) + 1):
        prefix = project_stored_trade_events_to_resumable_position_lots(
            events[:prefix_length],
            entry_mode="full",
        )
        assert prefix.domain_state is not None
        assert prefix.publication_state is not None
        domain_state = ResumableProjectionState.from_json_bytes(
            prefix.domain_state.to_json_bytes()
        )
        publication_state = ResumablePublicationState.from_json_bytes(
            prefix.publication_state.to_json_bytes()
        )
        resumed = project_stored_trade_events_to_resumable_position_lots(
            events[prefix_length:],
            domain_state=domain_state,
            publication_state=publication_state,
            entry_mode="tail",
        )
        if any(
            event.event_type == "adjust"
            for event in events[prefix_length:]
        ):
            assert resumed.requires_full_replay is True
            assert resumed.full_replay_reason == "tail_control_event"
            continue
        assert resumed.eligible is True
        assert _records_by_id(resumed.active_lots) == _records_by_id(
            full.active_lots
        )
        oracle_by_id = _records_by_id(oracle.lots)
        for lot_id, record in _records_by_id(resumed.touched_lots).items():
            assert record == oracle_by_id[lot_id]


def test_full_publisher_preserves_open_order_and_legacy_close_adjust_precedence() -> None:
    key = _key()
    second_key = _key(strike=105)
    events = [
        _event(
            "open-z",
            "open",
            1,
            key=key,
            contracts=3,
            price=2,
            lot_id="lot-z",
        ),
        _event(
            "open-a",
            "open",
            2,
            key=second_key,
            contracts=1,
            price=1,
            lot_id="lot-a",
        ),
        _event(
            "expire-partial",
            "expire_close",
            3,
            key=key,
            contracts=1,
            price=0,
            target_lot_id="lot-z",
            raw_payload={
                "close_type": "expire_auto_close",
                "auto_close_exp_src": "contract",
                "auto_close_grace_days": 1,
            },
        ),
        _event(
            "normal-partial",
            "close",
            4,
            key=key,
            contracts=1,
            price=0.5,
            target_lot_id="lot-z",
        ),
        _event(
            "adjust-after-close",
            "adjust",
            5,
            key=key,
            contracts=0,
            price=0,
            target_lot_id="lot-z",
            patch={"strategy": "yield_enhancement"},
        ),
    ]
    projection = project_stored_trade_events_to_position_lots(events)
    assert projection.diagnostics == []
    assert [item.record_id for item in projection.lots] == ["lot-z", "lot-a"]
    fields = projection.lots[0].fields
    assert fields["last_close_event_id"] == "normal-partial"
    assert fields["last_action_at"] == 4
    assert fields["strategy"] == "yield_enhancement"
    assert "auto_close_exp_src" not in fields
    assert "auto_close_grace_days" not in fields


def test_snapshot_baseline_fields_and_adjust_without_real_close_match_oracle() -> None:
    key = _key()
    open_event = _event(
        "open",
        "open",
        1,
        key=key,
        contracts=2,
        price=2,
        lot_id="lot-a",
        raw_payload={
            "fields": {
                "auto_close_exp_src": "bootstrap",
                "auto_close_grace_days": 9,
                "last_close_event_id": "legacy-marker",
            }
        },
    )
    adjust = _event(
        "adjust",
        "adjust",
        2,
        key=key,
        contracts=0,
        price=0,
        target_lot_id="lot-a",
        patch={"strategy": "sell_put"},
    )
    close = _event(
        "close",
        "close",
        3,
        key=key,
        contracts=1,
        price=1,
        target_lot_id="lot-a",
    )
    expected_after_adjust = project_stored_trade_events_to_position_lots(
        [open_event, adjust]
    )
    assert expected_after_adjust.lots[0].fields["last_action_at"] == 2

    prefix = project_stored_trade_events_to_resumable_position_lots(
        [open_event, adjust],
        entry_mode="full",
    )
    assert prefix.domain_state is not None
    assert prefix.publication_state is not None
    round_trip_publication = ResumablePublicationState.from_json_bytes(
        prefix.publication_state.to_json_bytes()
    )
    resumed = project_stored_trade_events_to_resumable_position_lots(
        [close],
        domain_state=prefix.domain_state,
        publication_state=round_trip_publication,
        entry_mode="tail",
    )
    oracle = project_stored_trade_events_to_position_lots(
        [open_event, adjust, close]
    )
    assert resumed.eligible is True
    assert resumed.touched_lots[0].to_dict() == oracle.lots[0].to_dict()
    assert resumed.touched_lots[0].fields["auto_close_exp_src"] == "bootstrap"
    assert resumed.touched_lots[0].fields["auto_close_grace_days"] == 9


def test_full_publisher_retains_finalized_row_for_later_full_only_adjust() -> None:
    key = _key()
    events = [
        _event(
            "open",
            "open",
            1,
            key=key,
            contracts=1,
            price=1,
            lot_id="lot-a",
        ),
        _event(
            "close",
            "close",
            2,
            key=key,
            contracts=1,
            price=0,
            target_lot_id="lot-a",
        ),
        _event(
            "adjust-closed",
            "adjust",
            3,
            key=key,
            contracts=0,
            price=0,
            target_lot_id="lot-a",
            patch={"strategy": "closed_history"},
        ),
    ]
    projection = project_stored_trade_events_to_position_lots(events)
    assert projection.diagnostics == []
    assert len(projection.lots) == 1
    fields = projection.lots[0].fields
    assert fields["status"] == "close"
    assert fields["strategy"] == "closed_history"
    assert fields["last_close_event_id"] == "close"
    assert fields["last_action_at"] == 2


def test_final_close_emits_exact_row_then_evicts_and_later_target_forces_full() -> None:
    key = _key()
    prefix_events = [
        _event(
            "open",
            "open",
            1,
            key=key,
            contracts=3,
            price=2,
            fees=1,
            lot_id="lot-a",
        ),
        _event(
            "partial",
            "close",
            2,
            key=key,
            contracts=1,
            price=1,
            target_lot_id="lot-a",
        ),
    ]
    final_close = _event(
        "final",
        "close",
        3,
        key=key,
        contracts=2,
        price=0.5,
        target_lot_id="lot-a",
    )
    prefix = project_stored_trade_events_to_resumable_position_lots(
        prefix_events,
        entry_mode="full",
    )
    assert prefix.domain_state is not None
    assert prefix.publication_state is not None
    resumed = project_stored_trade_events_to_resumable_position_lots(
        [final_close],
        domain_state=prefix.domain_state,
        publication_state=prefix.publication_state,
        entry_mode="tail",
    )
    oracle = project_stored_trade_events_to_position_lots(
        [*prefix_events, final_close]
    )
    assert resumed.eligible is True
    assert resumed.domain_state is not None
    assert resumed.publication_state is not None
    assert resumed.domain_state.active_lots == ()
    assert resumed.publication_state.fields_by_lot_id == {}
    assert resumed.active_lots == ()
    assert len(resumed.touched_lots) == 1
    assert resumed.touched_lots[0].to_dict() == oracle.lots[0].to_dict()
    assert resumed.touched_lots[0].fields["last_close_event_id"] == "final"

    later = project_resumable_trade_events(
        [
            _event(
                "late-close",
                "close",
                4,
                key=key,
                contracts=1,
                price=0,
                target_lot_id="lot-a",
            )
        ],
        initial_state=resumed.domain_state,
        entry_mode="tail",
    )
    assert later.requires_full_replay is True
    assert later.full_replay_reason == "target_lot_not_active"


def test_tail_diagnostic_is_never_checkpoint_eligible() -> None:
    key = _key()
    prefix = project_resumable_trade_events(
        [
            _event(
                "open",
                "open",
                1,
                key=key,
                contracts=1,
                price=1,
                lot_id="lot-a",
            )
        ],
        entry_mode="full",
    )
    assert prefix.state is not None
    invalid = project_resumable_trade_events(
        [
            _event(
                "oversized",
                "close",
                2,
                key=key,
                contracts=2,
                price=0,
                target_lot_id="lot-a",
            )
        ],
        initial_state=prefix.state,
        entry_mode="tail",
    )
    assert invalid.state is None
    assert invalid.requires_full_replay is True
    assert invalid.full_replay_reason == "tail_diagnostic"
    assert [item.code for item in invalid.diagnostics] == [
        "close_contracts_exceed_open"
    ]


def test_resumable_publication_state_mismatch_forces_full() -> None:
    key = _key()
    prefix = project_stored_trade_events_to_resumable_position_lots(
        [
            _event(
                "open",
                "open",
                1,
                key=key,
                contracts=1,
                price=1,
                lot_id="lot-a",
            )
        ],
        entry_mode="full",
    )
    assert prefix.domain_state is not None
    mismatch = project_stored_trade_events_to_resumable_position_lots(
        [],
        domain_state=prefix.domain_state,
        publication_state=ResumablePublicationState.empty(),
        entry_mode="tail",
    )
    assert mismatch.requires_full_replay is True
    assert mismatch.full_replay_reason == "resumable_state_lot_ids_mismatch"


def test_publication_state_is_canonical_and_does_not_alias_results() -> None:
    state = ResumablePublicationState(
        fields_by_lot_id={"lot-a": {"nested": {"value": 1}}},
        auto_close_baseline_by_lot_id={"lot-a": {}},
    )
    payload = state.to_json_bytes()
    decoded = ResumablePublicationState.from_json_bytes(payload)
    decoded.fields_by_lot_id["lot-a"]["nested"]["value"] = 2
    assert state.fields_by_lot_id["lot-a"]["nested"]["value"] == 1

    with pytest.raises(ValueError, match="not canonical"):
        ResumablePublicationState.from_json_bytes(json.dumps(state.to_dict()).encode())
    duplicate = payload.replace(
        b'"schema_version":',
        b'"schema_version":"resumable_publication_state.v1","schema_version":',
    )
    with pytest.raises(ValueError, match="duplicate publication state key"):
        ResumablePublicationState.from_json_bytes(duplicate)

    projection = project_stored_trade_events_to_resumable_position_lots(
        [
            _event(
                "open",
                "open",
                1,
                key=_key(),
                contracts=1,
                price=1,
                lot_id="lot-a",
                raw_payload={"strategy_snapshot": {"nested": {"value": 1}}},
            )
        ],
        entry_mode="full",
    )
    assert projection.publication_state is not None
    projection.active_lots[0].fields["strategy_snapshot"]["nested"]["value"] = 2
    assert (
        projection.publication_state.fields_by_lot_id["lot-a"]
        ["strategy_snapshot"]["nested"]["value"]
        == 1
    )


@pytest.mark.parametrize("event_type", ["void", "repair"])
def test_tail_control_events_force_full(event_type: str) -> None:
    key = _key()
    prefix = project_resumable_trade_events(
        [
            _event(
                "open",
                "open",
                1,
                key=key,
                contracts=1,
                price=1,
                lot_id="lot-a",
            )
        ],
        entry_mode="full",
    )
    assert prefix.state is not None
    control = TradeEvent(
        event_id=event_type,
        event_type=event_type,
        event_time_ms=2,
        contract_key=key,
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_event_id="open",
    )
    result = project_resumable_trade_events(
        [control],
        initial_state=prefix.state,
        entry_mode="tail",
    )
    assert result.requires_full_replay is True
    assert result.full_replay_reason == "tail_control_event"


def test_full_resumable_publisher_matches_void_expire_and_field_clear() -> None:
    key = _key(option_type="call", side="long")
    events = [
        _event(
            "open",
            "open",
            1,
            key=key,
            contracts=2,
            price=1,
            lot_id="lot-a",
            raw_payload={"strategy": "combo_yield", "leg_role": "call"},
        ),
        _event(
            "adjust-voided",
            "adjust",
            2,
            key=key,
            contracts=0,
            price=0,
            target_lot_id="lot-a",
            patch={"strategy": "wrong", "leg_role": "wrong"},
        ),
        TradeEvent(
            event_id="void-adjust",
            event_type="void",
            event_time_ms=3,
            contract_key=key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            target_event_id="adjust-voided",
        ),
        _event(
            "clear-strategy",
            "adjust",
            4,
            key=key,
            contracts=0,
            price=0,
            target_lot_id="lot-a",
            patch={"strategy": None, "leg_role": None},
        ),
        _event(
            "expire",
            "expire_close",
            5,
            key=key,
            contracts=2,
            price=0,
            target_lot_id="lot-a",
            raw_payload={
                "close_type": "expire_auto_close",
                "auto_close_exp_src": "contract",
                "auto_close_grace_days": 1,
            },
        ),
    ]
    oracle = project_stored_trade_events_to_position_lots(events)
    resumed = project_stored_trade_events_to_resumable_position_lots(
        events,
        entry_mode="full",
    )
    assert oracle.diagnostics == []
    assert resumed.eligible is True
    assert _records_by_id(resumed.touched_lots) == _records_by_id(oracle.lots)
    fields = resumed.touched_lots[0].fields
    assert "strategy" not in fields
    assert "leg_role" not in fields
    assert fields["close_type"] == "expire_auto_close"
    assert fields["auto_close_grace_days"] == 1


def test_domain_layer_has_no_application_or_sqlite_dependency() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (root / "domain" / "domain" / "ledger").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import sqlite3" not in source
        assert "from src." not in source
        assert "import src." not in source
