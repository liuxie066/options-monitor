from __future__ import annotations

from decimal import Decimal

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.lots import PositionLot
from domain.domain.option_position_lots import parse_exp_to_ms
from domain.domain.trade_contract_identity import derive_trade_side
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


#: ``write-side-definition.md`` §2 RECONSTRUCTIBLE: the fact lives on the event or
#: strategy side, so the published payload must not carry the key at all.
RETIRED_LOT_KEYS = frozenset(
    {
        "auto_close_exp_src",
        "auto_close_grace_days",
        "cash_secured_amount",
        "close_price",
        "close_reason",
        "close_type",
        "closed_at",
        "event_source_name",
        "event_source_type",
        "last_action_at",
        "leg_role",
        "source_stock_lot_id",
        "source_wheel_branch_id",
        "strategy",
        "strategy_group_id",
        "strategy_snapshot",
        "underlying_share_locked",
    }
)

#: §2 "CARRIED by asset type": ``asset_type`` plus ``shares_*`` say it, so the key
#: itself is dropped.
DROPPED_LOT_KEYS = frozenset({"quantity_unit"})

#: §1/§3: the published key set is exactly ``PositionLot.to_dict()``'s -- a stock
#: lot adds the four ``shares_*``/``cost_basis_total`` keys.
OPTION_LOT_KEYS = frozenset(
    {
        "lot_id",
        "open_event_id",
        "contract_key",
        "position_side",
        "position_key",
        "opened_at_ms",
        "contracts_opened",
        "contracts_open",
        "contracts_closed",
        "status",
        "premium_open",
        "multiplier",
        "currency",
        "realized_pnl",
        "last_event_id",
        "close_event_ids",
        "asset_type",
    }
)
STOCK_LOT_KEYS = OPTION_LOT_KEYS | {
    "shares_opened",
    "shares_open",
    "shares_closed",
    "cost_basis_total",
}


def _assert_converged_payload(fields: dict, *, stock: bool = False) -> None:
    """§1: the published payload is the converged shape, no more and no less."""

    assert set(fields) == (STOCK_LOT_KEYS if stock else OPTION_LOT_KEYS)
    assert not (RETIRED_LOT_KEYS | DROPPED_LOT_KEYS) & set(fields)


def _key(
    *,
    strike: float,
    expiration_ymd: str,
    option_type: str = "put",
) -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        strike=strike,
        expiration_ymd=expiration_ymd,
        )


def test_publisher_applies_adjust_patch_to_legacy_position_lot_fields() -> None:
    adjusted_exp_ms = parse_exp_to_ms("2026-07-17")
    assert adjusted_exp_ms is not None

    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda",
                event_type="open",
                event_time_ms=1000,
                contract_key=_key(strike=100.0, expiration_ymd="2026-06-19"),
                contracts=1,
                price=2.5,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "sell"},
            ),
            TradeEvent(
                event_id="adjust-nvda",
                event_type="adjust",
                event_time_ms=3000,
                contract_key=_key(strike=100.0, expiration_ymd="2026-06-19"),
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="lot_open-nvda",
                raw_payload={
                    "record_id": "lot_open-nvda",
                    "target_lot_id": "lot_open-nvda",
                    "adjust_target_source_event_id": "open-nvda",
                    "patch": {
                        "contracts": 2,
                        "contracts_open": 2,
                        "contracts_closed": 0,
                        "strike": 105.0,
                        "expiration": adjusted_exp_ms,
                        "premium": 3.1,
                        "opened_at": 2000,
                        "last_action_at": 3000,
                        "cash_secured_amount": 21000.0,
                    },
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    assert len(projection.lots) == 1
    record = projection.lots[0]
    assert record.lot_id == "lot_open-nvda"
    fields = record.fields
    # §2 renames: ``source_event_id``→``open_event_id``, ``contracts``→
    # ``contracts_opened``, ``premium``→``premium_open``, ``opened_at``→
    # ``opened_at_ms``, and the contract moves under ``contract_key``.
    assert fields["open_event_id"] == "open-nvda"
    assert fields["contracts_opened"] == 2
    assert fields["contracts_open"] == 2
    # §7.4: the published row carries money as decimal text, not float.
    assert fields["contract_key"]["strike"] == "105"
    assert fields["premium_open"] == "3.1"
    assert fields["opened_at_ms"] == 2000
    assert fields["position_key"] == "富途|lx|NVDA|2026-07-17|105P|short"
    # §2 RECONSTRUCTIBLE: ``last_action_at`` and ``cash_secured_amount`` left the
    # payload (the adjust patch still accepted them on the way in).
    _assert_converged_payload(fields)


def test_publisher_drops_open_strategy_snapshot() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda",
                event_type="open",
                event_time_ms=1000,
                contract_key=_key(strike=100.0, expiration_ymd="2026-06-19"),
                contracts=1,
                price=2.5,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda",
                raw_payload={
                    "source": "test",
                    "source_type": "manual_trade_event",
                    "side": "sell",
                    "strategy_snapshot": {
                        "strategy_family": "sell_put",
                        "strategy_profile": "short_vol",
                        "strategy_source": "current_config",
                        "risk_model": "short_vol",
                    },
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    # §2/§7: the strategy family is no longer a payload carrier -- the open event's
    # raw_payload keeps the snapshot and the strategy side reads it from there.
    _assert_converged_payload(projection.lots[0].fields)


def test_publisher_drops_open_strategy_metadata_fields() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-pdd-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=_key(
                    strike=100.0,
                    expiration_ymd="2026-07-17",
                    option_type="call",
                ),
                contracts=1,
                price=0.73,
                currency="USD",
                source="opend_push",
                multiplier=100,
                lot_id="lot_open-pdd-call",
                raw_payload={
                    "side": "buy",
                    "strategy": "combo_yield",
                    "leg_role": "enhancement_call",
                    "strategy_group_id": "combo_yield:lot_pdd_short_put",
                    "yield_enhancement_mode": "income_upside_enhancement",
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    # The open event still carries the family (it is the home §7 moves it to); the
    # published payload must not.
    _assert_converged_payload(projection.lots[0].fields)


def test_publisher_drops_adjust_strategy_metadata_patch() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
    )

    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=open_key,
                contracts=1,
                price=1.0,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda-call",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "buy"},
            ),
            TradeEvent(
                event_id="adjust-nvda-call-strategy",
                event_type="adjust",
                event_time_ms=3000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="lot_open-nvda-call",
                raw_payload={
                    "record_id": "lot_open-nvda-call",
                    "target_lot_id": "lot_open-nvda-call",
                    "patch": {
                        "last_action_at": 3000,
                        "strategy": "yield_enhancement",
                        "leg_role": "enhancement_call",
                        "strategy_group_id": "ye_nvda_1",
                        "yield_enhancement_mode": "income_upside_enhancement",
                    },
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    # An adjust patch that names the family is accepted on the way in and dropped
    # on the way out (§2 RECONSTRUCTIBLE / §7).
    _assert_converged_payload(projection.lots[0].fields)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"strategy_family": "sell_put", "strategy_profile": "return_first"},
        {"strategy_family": "sell_put", "strategy_profile": "return"},
        {"structure_mode": "same_expiry_pair"},
    ],
    ids=["return_first", "return", "same_expiry_pair"],
)
def test_fallback_strategy_snapshot_patch_preserves_risk_semantics(
    snapshot: dict[str, str],
) -> None:
    key = _key(strike=100.0, expiration_ymd="2026-06-19")
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="legacy-open",
                event_type="open",
                event_time_ms=1000,
                contract_key=key,
                contracts=1,
                price=2.5,
                currency="USD",
                source="legacy",
                multiplier=100,
                lot_id="legacy-lot",
                raw_payload={
                    "side": "sell",
                    "strategy": "combo_yield",
                    "yield_enhancement_mode": "vol_convexity_enhancement",
                },
            ),
            TradeEvent(
                event_id="new-adjust",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=key,
                contracts=0,
                price=0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="legacy-lot",
                raw_payload={
                    "patch": {"strategy_snapshot": snapshot}
                },
            ),
            TradeEvent(
                event_id="unrelated-invalid-close",
                event_type="close",
                event_time_ms=3000,
                contract_key=key,
                contracts=1,
                price=1,
                currency="USD",
                source="legacy",
                multiplier=100,
                target_lot_id="missing-lot",
            ),
        ]
    )

    assert [item.code for item in projection.diagnostics] == ["target_lot_not_found"]
    fields = projection.lots[0].fields
    # §2 RECONSTRUCTIBLE: the family the fallback path carries is no longer
    # published, whatever shape the patch's snapshot has.
    _assert_converged_payload(fields)
    # The risk semantics the fallback exists for are the contract itself: the
    # strike, multiplier and open quantity survive the patch untouched.
    assert fields["contract_key"]["strike"] == "100"
    assert fields["multiplier"] == 100
    assert fields["contracts_open"] == 1


def test_publisher_does_not_reapply_voided_adjust_strategy_patch() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
    )
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=open_key,
                contracts=1,
                price=1.0,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "buy"},
            ),
            TradeEvent(
                event_id="adjust-nvda-call",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="lot_open-nvda-call",
                raw_payload={
                    "patch": {
                        "strategy": "yield_enhancement",
                        "leg_role": "enhancement_call",
                    }
                },
            ),
            TradeEvent(
                event_id="void-adjust-nvda-call",
                event_type="void",
                event_time_ms=3000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="test",
                multiplier=100,
                target_event_id="adjust-nvda-call",
            ),
        ]
    )

    assert projection.diagnostics == []
    fields = projection.lots[0].fields
    assert "strategy" not in fields
    assert "leg_role" not in fields


def test_publisher_ignores_import_diagnostics_for_voided_invalid_event() -> None:
    key = _key(
        strike=510.0,
        expiration_ymd="2026-05-28",
        option_type="call",
    )
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=key,
                contracts=2,
                price=4.1,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "sell"},
            ),
            TradeEvent(
                event_id="invalid-close",
                event_type="close",
                event_time_ms=0,
                contract_key=key,
                contracts=2,
                price=0.28,
                currency="USD",
                source="opend_push",
                multiplier=100,
                target_lot_id="lot_open-nvda-call",
            ),
            TradeEvent(
                event_id="void-invalid-close",
                event_type="void",
                event_time_ms=2000,
                contract_key=key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_trade_event_repair",
                multiplier=100,
                target_event_id="invalid-close",
            ),
            TradeEvent(
                event_id="replacement-close",
                event_type="close",
                event_time_ms=3000,
                contract_key=key,
                contracts=2,
                price=0.28,
                currency="USD",
                source="cli_trade_event_repair",
                multiplier=100,
                target_lot_id="lot_open-nvda-call",
            ),
        ]
    )

    assert projection.diagnostics == []
    assert projection.lots[0].fields["status"] == "close"


def test_publisher_keeps_import_diagnostics_for_active_invalid_event() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="invalid-open",
                event_type="open",
                event_time_ms=0,
                contract_key=_key(
                    strike=100.0,
                    expiration_ymd="2026-06-19",
                ),
                contracts=1,
                price=2.5,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_invalid-open",
            ),
        ]
    )

    assert projection.has_errors is True
    assert {
        item.code
        for item in projection.diagnostics
    } == {"event_time_must_be_positive"}


def test_publisher_does_not_apply_strategy_patch_from_rejected_adjust() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
    )
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=open_key,
                contracts=1,
                price=1.0,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "buy"},
            ),
            TradeEvent(
                event_id="adjust-missing-lot",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="lot_missing",
                raw_payload={"patch": {"strategy": "yield_enhancement"}},
            ),
        ]
    )

    assert [item.code for item in projection.diagnostics] == ["target_lot_not_found"]
    assert "strategy" not in projection.lots[0].fields


def _stock_key(*, symbol: str = "AAPL") -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol=symbol,
        option_type="",
        strike=0.0,
        expiration_ymd="",
        asset_type="stock",
        )


def _stock_event(
    event_id: str,
    event_type: str,
    event_time_ms: int,
    *,
    key: ContractKey,
    contracts: int,
    price: float,
    lot_id: str | None = None,
    target_lot_id: str | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=key,
        contracts=contracts,
        price=price,
        currency="USD",
        source="test",
        lot_id=lot_id,
        target_lot_id=target_lot_id,
        asset_type="stock",
        raw_payload={"side": derive_trade_side(event_type, "long") or ""},
    )


def test_publisher_publishes_stock_lot_in_shares_vocabulary() -> None:
    """§7.3: a stock lot publishes ``shares_*``/``cost_basis_total``.

    Before the stock branch existed every lot went through the option builder,
    so a stock event failed *inside the write transaction* with
    ``option_type must be one of: call, put`` -- long after the domain layer had
    already accepted it. The published shape is the §7.3 quantity vocabulary.
    """
    key = _stock_key()
    projection = project_stored_trade_events_to_position_lots(
        [
            _stock_event(
                "open-stock",
                "open",
                1_000,
                key=key,
                contracts=5,
                price=45.5,
                lot_id="lot-stock",
            ),
            _stock_event(
                "close-stock",
                "close",
                2_000,
                key=key,
                contracts=2,
                price=50.0,
                target_lot_id="lot-stock",
            ),
        ]
    )

    assert projection.diagnostics == []
    assert len(projection.lots) == 1
    fields = projection.lots[0].fields
    assert fields["asset_type"] == "stock"
    assert fields["contract_key"]["asset_type"] == "stock"
    assert fields["shares_opened"] == "5"
    assert fields["shares_open"] == "3"
    assert fields["shares_closed"] == "2"
    assert fields["cost_basis_total"] == "227.5"
    assert fields["position_key"] == "富途|lx|AAPL|stock|long"
    # §2 dropped ``quantity_unit`` (``asset_type`` plus ``shares_*`` say it) and §3
    # puts the contract scalars under ``contract_key``, so a stock row publishes
    # none of the flat option spellings.
    _assert_converged_payload(fields, stock=True)


def test_publisher_publishes_option_money_as_decimal_text() -> None:
    """§7.4: money/price reach the published row as decimal text.

    Conformance is the main point -- the stored JSON carried a binary float where
    §7.4 asks for a decimal string. The arithmetic half is narrower and only
    shows up once the strike needs more than two decimals: a 3-decimal strike is
    exactly what ``PRICE_DECIMAL_PLACES = 3`` exists to admit, and
    ``5.001 * 100 * 3`` then evaluates to ``1500.3000000000002``. The float path
    stored that number, so the published cash-secured amount was wrong and not
    merely rendered differently; ``Decimal`` plus ``quantize_money`` yields
    ``1500.3``.
    """
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda-put",
                event_type="open",
                event_time_ms=1_000,
                contract_key=_key(strike=5.001, expiration_ymd="2026-06-19"),
                contracts=3,
                price=0.125,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda-put",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "sell"},
            ),
        ]
    )

    assert projection.diagnostics == []
    fields = projection.lots[0].fields
    assert fields["contract_key"]["strike"] == "5.001"
    assert fields["premium_open"] == "0.125"
    assert isinstance(fields["contract_key"]["strike"], str)
    assert isinstance(fields["premium_open"], str)
    # §2 RECONSTRUCTIBLE: the derived ``cash_secured_amount`` is no longer a
    # published key, so the 3-decimal strike itself is what has to survive the
    # round trip as decimal text.
    _assert_converged_payload(fields)


def test_publisher_applies_money_quantum_to_patched_premium() -> None:
    """§7.4: the published row follows ``quantize_money``, not "whatever fits".

    An adjust patch reaches ``premium_open`` through ``to_decimal`` with no
    decimal-place check, so a patched premium can carry more places than money
    admits -- the open path validates against ``PRICE_DECIMAL_PLACES``, the patch
    path does not. The project's money rule is ``MONEY_QUANTUM`` (6 places,
    ROUND_HALF_UP), and it is applied at the write side's single money render
    point (``PositionLot.to_dict()`` -> ``_money_text``), so a 10-place price
    cannot reach the read model.
    """

    key = _key(strike=100.0, expiration_ymd="2026-06-19")
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-nvda",
                event_type="open",
                event_time_ms=1_000,
                contract_key=key,
                contracts=1,
                price=2.5,
                currency="USD",
                source="cli_manual_open",
                multiplier=100,
                lot_id="lot_open-nvda",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "sell"},
            ),
            TradeEvent(
                event_id="adjust-nvda",
                event_type="adjust",
                event_time_ms=2_000,
                contract_key=key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="cli_manual_adjust",
                multiplier=100,
                target_lot_id="lot_open-nvda",
                raw_payload={
                    "patch": {"premium": "3.1234567891", "last_action_at": 2_000}
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    fields = projection.lots[0].fields
    assert fields["premium_open"] == "3.123457"


def test_position_lot_money_render_applies_the_money_quantum() -> None:
    """§7.4: the money rule binds at the render point, not only on a patch.

    ``PositionLot`` keeps ``Decimal`` authority values (an event price can carry
    17 digits, and a computed ``realized_pnl`` is a product of price, multiplier
    and contracts), so ``to_dict()`` -- the write side's only money render point,
    and therefore every published ``fields_json`` -- is where ``MONEY_QUANTUM``
    has to bind. The rendered text is what production rows are compared on, so a
    text that keeps the raw scale is the failure this pins.
    """
    lot = PositionLot(
        lot_id="lot-money",
        open_event_id="open-money",
        contract_key=_key(strike=100.0, expiration_ymd="2026-06-19"),
        position_side="short",
        opened_at_ms=1_000,
        contracts_opened=1,
        contracts_open=1,
        contracts_closed=0,
        status="open",
        # A binary float that survives ``Decimal(str(...))`` with 17 digits.
        premium_open=Decimal("1.8399999999999999"),
        multiplier=100,
        currency="USD",
        # ``1.0000005`` sits exactly on the 6th-place boundary, so ROUND_HALF_UP carries.
        realized_pnl=Decimal("1.0000005"),
        last_event_id="open-money",
    )

    payload = lot.to_dict()

    assert payload["premium_open"] == "1.84"
    assert payload["realized_pnl"] == "1.000001"


def test_publisher_normalizes_money_in_a_legacy_snapshot() -> None:
    """§7.4: one shape per published row, including keys carried from a snapshot.

    ``_base_fields_for_lot`` seeds from the open event's stored ``fields``, and
    only the keys this publisher re-derives are overwritten. A legacy row that
    stored money as a float would otherwise publish a float next to a decimal
    string -- and for a long call nothing re-derives ``cash_secured_amount`` or
    ``close_price`` at all, so those two survive untouched.
    """
    projection = project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="open-legacy-call",
                event_type="open",
                event_time_ms=1_000,
                contract_key=_key(
                    strike=100.0, expiration_ymd="2026-06-19", option_type="call"
                ),
                contracts=1,
                price=0.73,
                currency="USD",
                source="legacy",
                multiplier=100,
                lot_id="lot_open-legacy-call",
                raw_payload={
                    "side": "buy",
                    "fields": {
                        "strike": 100.0,
                        "premium": 0.73,
                        "cash_secured_amount": 73000.0,
                        "close_price": 1.5,
                    },
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    fields = projection.lots[0].fields
    assert fields["contract_key"]["strike"] == "100"
    assert fields["premium_open"] == "0.73"
    assert isinstance(fields["contract_key"]["strike"], str)
    assert isinstance(fields["premium_open"], str)
    # §6/§2: a legacy snapshot's retired keys -- ``cash_secured_amount`` and
    # ``close_price`` among them -- do not leak into the published row.
    _assert_converged_payload(fields)
