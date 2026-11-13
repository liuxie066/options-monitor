"""Public-API conversion budgets; wall-clock acceptance lives in the benchmark."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.benchmark_ledger_interventions import (
    NOW_MS, TARGET_ID, invoke, prepare_template,
)
from src.application.ledger import event_codec, interventions
from src.application.ledger.repository import SQLiteOptionPositionsRepository


@pytest.mark.parametrize("operation", ["void_preview", "void_apply", "repair_preview", "repair_apply"])
def test_intervention_does_not_decode_non_void_history_for_void_lookup(tmp_path: Path, monkeypatch, operation: str):
    monkeypatch.setattr(interventions, "now_ms", lambda: NOW_MS)
    path = tmp_path / "synthetic.sqlite3"
    confirmation, _ = prepare_template(path, 101, 1)
    repo = SQLiteOptionPositionsRepository(path, initialize=False)
    decode = Mock(wraps=event_codec.stored_trade_event_to_ledger_event)
    canonical = Mock(wraps=event_codec._canonical_payload_to_ledger_event)
    monkeypatch.setattr(event_codec, "stored_trade_event_to_ledger_event", decode)
    monkeypatch.setattr(event_codec, "_canonical_payload_to_ledger_event", canonical)
    before = repo.list_trade_events()
    canonical.reset_mock()

    result = invoke(repo, operation, confirmation)

    # All history is still imported and projected by the public API. Only void
    # lookup's redundant conversions of ordinary open/close rows disappear.
    assert decode.call_count <= 10
    factor, constant = {"void_preview": (2, 2), "repair_preview": (2, 4),
        "void_apply": (5, 7), "repair_apply": (7, 18)}[operation]
    assert canonical.call_count <= factor * len(before) + constant
    if operation.endswith("preview"):
        assert repo.list_trade_events() == before
    else:
        assert result["mode"] == "applied"
        assert len(repo.list_trade_events()) == len(before) + (2 if operation.startswith("repair") else 1)


@pytest.mark.parametrize("event_type", ["open", "close", "adjust", "repair", "OPEN", "unknown", ""])
def test_void_lookup_skips_non_void_payload_conversion(monkeypatch, event_type: str):
    decode = Mock(wraps=event_codec.stored_trade_event_to_ledger_event)
    monkeypatch.setattr(event_codec, "stored_trade_event_to_ledger_event", decode)
    # Even malformed non-void history cannot supply a valid void target.
    payload = {"event_type": event_type, "event_time_ms": "invalid", "contract_key": {},
        "target_event_id": TARGET_ID}
    assert event_codec.valid_void_target_event_id(payload) is None
    assert decode.call_count == 0


def test_void_lookup_still_decodes_and_rejects_invalid_void(monkeypatch):
    decode = Mock(wraps=event_codec.stored_trade_event_to_ledger_event)
    monkeypatch.setattr(event_codec, "stored_trade_event_to_ledger_event", decode)
    payload = {"event_type": "void", "event_time_ms": "invalid", "contract_key": {},
        "target_event_id": TARGET_ID}
    assert event_codec.valid_void_target_event_id(payload) is None
    assert decode.call_count == 1


@pytest.mark.parametrize("event_type", ["void", " VOID ", "VoId"])
def test_void_lookup_retains_normalization_and_typed_event_support(event_type: str):
    from dataclasses import replace
    from scripts.benchmark_ledger_interventions import synthetic_events

    opening = synthetic_events(1, 1)[0]
    void = replace(opening, event_type="void", target_event_id=TARGET_ID)
    payload = {**void.to_dict(), "event_type": event_type}
    assert event_codec.valid_void_target_event_id(payload) == TARGET_ID
    assert event_codec.valid_void_target_event_id(void) == TARGET_ID


@pytest.mark.parametrize("close_payload,expected", [
    ({}, ("sell_put", "open-leg", "open-group")),
    ({"strategy": "cc", "leg_role": "close-leg"}, ("sell_call", "close-leg", "open-group")),
    ({"strategy": "cc", "strategy_snapshot": {"strategy": "csp"}},
        ("sell_put", "open-leg", "open-group")),
    ({"strategy": "cc", "wheel_call_allocations": "invalid"},
        ("sell_put", "open-leg", "open-group")),
    ({"strategy_snapshot": {"strategy": "cc", "leg_role": "close-leg", "strategy_group_id": "close-group"}},
        ("sell_call", "close-leg", "close-group")),
])
def test_allocation_resolves_each_events_strategy_metadata_once(monkeypatch, close_payload, expected):
    from dataclasses import replace
    from decimal import Decimal
    from domain.domain.ledger import economics
    from domain.domain.ledger.lots import PositionLot
    from scripts.benchmark_ledger_interventions import synthetic_events

    _, opening, closing = synthetic_events(3, 1)
    opening = replace(opening, raw_payload={**opening.raw_payload,
        "strategy": "csp", "leg_role": "open-leg", "strategy_group_id": "open-group"})
    closing = replace(closing, raw_payload={**closing.raw_payload, **close_payload})
    resolve = Mock(wraps=economics.resolve_strategy_metadata)
    monkeypatch.setattr(economics, "resolve_strategy_metadata", resolve)

    allocation, _ = economics.build_option_economic_allocation(
        lot=PositionLot.from_open_event(opening, lot_id=opening.lot_id), open_event=opening, close_event=closing,
        sequence=1, open_fee_allocated_before=Decimal("0"))

    assert (allocation.strategy, allocation.leg_role, allocation.strategy_group_id) == expected
    assert resolve.call_count <= 2


def test_unchanged_realized_pnl_does_not_rebuild_transition_snapshots(monkeypatch):
    from decimal import Decimal
    from domain.domain.ledger import projection
    from scripts.benchmark_ledger_interventions import synthetic_events

    rebuild = Mock(wraps=projection.replace)
    monkeypatch.setattr(projection, "replace", rebuild)
    result = projection.project_resumable_trade_events(synthetic_events(21, 1), entry_mode="full")
    assert len(result.transitions) == 21
    assert not result.diagnostics
    for transition in result.transitions:
        assert transition.lot_after.realized_pnl == (Decimal("150") if transition.event.is_close else Decimal("0"))
    assert rebuild.call_count == 0


def test_identical_scalar_contract_keys_share_normalization(monkeypatch):
    from domain.domain.ledger import ContractKey
    from scripts.benchmark_ledger_interventions import synthetic_events

    payloads = [event.to_dict() for event in synthetic_events(21, 1)]
    for payload in payloads:
        payload["contract_key"]["account"] = "perf-" + payload["contract_key"]["account"]
    normalize = Mock(wraps=ContractKey.from_values)
    monkeypatch.setattr(ContractKey, "from_values", normalize)
    decoded = [event_codec.stored_trade_event_to_ledger_event(payload) for payload in payloads]
    assert all(event is not None and not diagnostics for event, diagnostics in decoded)
    assert {event.contract_key.account for event, _ in decoded} == {"perf-lx", "perf-sy"}
    assert normalize.call_count <= 2


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_decimal_direct_path_retains_validation_and_deferred_diagnostics(text: str):
    from decimal import Decimal
    from domain.domain import money

    value = Decimal(text)
    with pytest.raises(ValueError, match="price must be a finite decimal"):
        money.to_decimal(value, field_name="price")
    assert money.coerce_decimal(value).as_tuple() == value.as_tuple()


@pytest.mark.parametrize("normalizer,value", [
    ("normalize_trade_side", "buy"), ("normalize_trade_side", "sell"),
    ("normalize_position_effect", "open"), ("normalize_position_effect", "close"),
    ("normalize_asset_type", "stock"), ("normalize_asset_type", "option"),
    ("normalize_quantity_unit", "share"), ("normalize_quantity_unit", "contract"),
])
def test_canonical_choices_need_no_alias_compaction(monkeypatch, normalizer: str, value: str):
    from domain.domain import trade_contract_identity as identity

    compact = Mock(wraps=identity._compact_choice)
    monkeypatch.setattr(identity, "_compact_choice", compact)
    assert getattr(identity, normalizer)(value) == value
    assert compact.call_count == 0


@pytest.mark.parametrize("function", ["to_decimal", "coerce_decimal"])
def test_decimal_inputs_need_no_text_roundtrip(function: str):
    from decimal import Decimal
    from domain.domain import money

    class NumericDecimal(Decimal):
        def __str__(self):
            raise AssertionError("typed decimal was needlessly serialized")

    assert getattr(money, function)(NumericDecimal("1.2500")) == Decimal("1.2500")


def test_allocation_with_no_strategy_fields_needs_no_metadata_resolution(monkeypatch):
    from decimal import Decimal
    from domain.domain.ledger import economics
    from domain.domain.ledger.lots import PositionLot
    from scripts.benchmark_ledger_interventions import synthetic_events

    _, opening, closing = synthetic_events(3, 1)
    resolve = Mock(wraps=economics.resolve_strategy_metadata)
    monkeypatch.setattr(economics, "resolve_strategy_metadata", resolve)
    allocation, _ = economics.build_option_economic_allocation(
        lot=PositionLot.from_open_event(opening, lot_id=opening.lot_id), open_event=opening, close_event=closing,
        sequence=1, open_fee_allocated_before=Decimal("0"))
    assert (allocation.strategy, allocation.leg_role, allocation.strategy_group_id) == (None, None, None)
    assert resolve.call_count == 0
