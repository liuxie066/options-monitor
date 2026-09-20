from __future__ import annotations

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import parse_exp_to_ms
from domain.domain.trade_contract_identity import derive_trade_side
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


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


def _event(**overrides: object) -> TradeEvent:
    """Build one stored ledger trade event.

    The defaults are the manual NVDA put open this module repeats most often,
    so a call site spells out only the fields that differ from it. ``lot_id``,
    ``target_lot_id`` and ``raw_payload`` are deliberately absent from the
    defaults: open events set ``lot_id``, close/adjust/void events set
    ``target_lot_id``/``target_event_id``, and leaving them unset keeps each
    call site's field set exactly what it was.
    """
    base: dict[str, object] = {
        "event_id": "open-nvda",
        "event_type": "open",
        "event_time_ms": 1000,
        "contract_key": _key(strike=100.0, expiration_ymd="2026-06-19"),
        "contracts": 1,
        "price": 2.5,
        "currency": "USD",
        "source": "cli_manual_open",
        "multiplier": 100,
    }
    base.update(overrides)
    return TradeEvent(**base)


def test_publisher_applies_adjust_patch_to_legacy_position_lot_fields() -> None:
    adjusted_exp_ms = parse_exp_to_ms("2026-07-17")
    assert adjusted_exp_ms is not None

    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                lot_id="lot_open-nvda",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "sell"},
            ),
            _event(
                event_id="adjust-nvda",
                event_type="adjust",
                event_time_ms=3000,
                contracts=0,
                price=0.0,
                source="cli_manual_adjust",
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
    assert fields["source_event_id"] == "open-nvda"
    assert fields["contracts"] == 2
    assert fields["contracts_open"] == 2
    # §7.4: the published row carries money as decimal text, not float.
    assert fields["strike"] == "105"
    assert fields["premium"] == "3.1"
    assert fields["opened_at"] == 2000
    assert fields["last_action_at"] == 3000
    assert fields["position_key"] == "富途|lx|NVDA|2026-07-17|105P|short"
    assert fields["cash_secured_amount"] == "21000"


def test_publisher_preserves_open_strategy_snapshot() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
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
    assert projection.lots[0].fields["strategy_snapshot"] == {
        "strategy_family": "sell_put",
        "strategy_profile": "short_vol",
        "strategy_source": "current_config",
        "risk_model": "short_vol",
    }


def test_publisher_preserves_open_strategy_metadata_fields() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                event_id="open-pdd-call",
                contract_key=_key(
                    strike=100.0,
                    expiration_ymd="2026-07-17",
                    option_type="call",
                ),
                price=0.73,
                source="opend_push",
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
    fields = projection.lots[0].fields
    assert fields["strategy"] == "combo_yield"
    assert fields["leg_role"] == "enhancement_call"
    assert fields["strategy_group_id"] == "combo_yield:lot_pdd_short_put"
    assert fields["yield_enhancement_mode"] == "income_upside_enhancement"


def test_publisher_applies_adjust_strategy_metadata_patch() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
    )

    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                event_id="open-nvda-call",
                contract_key=open_key,
                price=1.0,
                lot_id="lot_open-nvda-call",
                raw_payload={"source": "test", "source_type": "manual_trade_event", "side": "buy"},
            ),
            _event(
                event_id="adjust-nvda-call-strategy",
                event_type="adjust",
                event_time_ms=3000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                source="cli_manual_adjust",
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
    fields = projection.lots[0].fields
    assert fields["strategy"] == "yield_enhancement"
    assert fields["leg_role"] == "enhancement_call"
    assert fields["strategy_group_id"] == "ye_nvda_1"
    assert fields["yield_enhancement_mode"] == "income_upside_enhancement"


@pytest.mark.parametrize(
    ("snapshot", "expected_retired_mode"),
    [
        ({"strategy_family": "sell_put", "strategy_profile": "return_first"}, None),
        ({"strategy_family": "sell_put", "strategy_profile": "return"}, None),
        ({"structure_mode": "same_expiry_pair"}, "vol_convexity_enhancement"),
    ],
)
def test_fallback_strategy_snapshot_patch_preserves_risk_semantics(
    snapshot: dict[str, str],
    expected_retired_mode: str | None,
) -> None:
    key = _key(strike=100.0, expiration_ymd="2026-06-19")
    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                event_id="legacy-open",
                contract_key=key,
                source="legacy",
                lot_id="legacy-lot",
                raw_payload={
                    "side": "sell",
                    "strategy": "combo_yield",
                    "yield_enhancement_mode": "vol_convexity_enhancement",
                },
            ),
            _event(
                event_id="new-adjust",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=key,
                contracts=0,
                price=0,
                source="cli_manual_adjust",
                target_lot_id="legacy-lot",
                raw_payload={"patch": {"strategy_snapshot": snapshot}},
            ),
            _event(
                event_id="unrelated-invalid-close",
                event_type="close",
                event_time_ms=3000,
                contract_key=key,
                price=1,
                source="legacy",
                target_lot_id="missing-lot",
            ),
        ]
    )

    assert [item.code for item in projection.diagnostics] == ["target_lot_not_found"]
    assert projection.lots[0].fields.get("yield_enhancement_mode") == expected_retired_mode
    assert projection.lots[0].fields["strategy_snapshot"] == snapshot


def test_publisher_does_not_reapply_voided_adjust_strategy_patch() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
    )
    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                event_id="open-nvda-call",
                contract_key=open_key,
                price=1.0,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "buy"},
            ),
            _event(
                event_id="adjust-nvda-call",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                source="cli_manual_adjust",
                target_lot_id="lot_open-nvda-call",
                raw_payload={
                    "patch": {
                        "strategy": "yield_enhancement",
                        "leg_role": "enhancement_call",
                    }
                },
            ),
            _event(
                event_id="void-adjust-nvda-call",
                event_type="void",
                event_time_ms=3000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                source="test",
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
            _event(
                event_id="open-nvda-call",
                contract_key=key,
                contracts=2,
                price=4.1,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "sell"},
            ),
            _event(
                event_id="invalid-close",
                event_type="close",
                event_time_ms=0,
                contract_key=key,
                contracts=2,
                price=0.28,
                source="opend_push",
                target_lot_id="lot_open-nvda-call",
            ),
            _event(
                event_id="void-invalid-close",
                event_type="void",
                event_time_ms=2000,
                contract_key=key,
                contracts=0,
                price=0.0,
                source="cli_trade_event_repair",
                target_event_id="invalid-close",
            ),
            _event(
                event_id="replacement-close",
                event_type="close",
                event_time_ms=3000,
                contract_key=key,
                contracts=2,
                price=0.28,
                source="cli_trade_event_repair",
                target_lot_id="lot_open-nvda-call",
            ),
        ]
    )

    assert projection.diagnostics == []
    assert projection.lots[0].fields["status"] == "close"


def test_publisher_keeps_import_diagnostics_for_active_invalid_event() -> None:
    projection = project_stored_trade_events_to_position_lots(
        [
            _event(
                event_id="invalid-open",
                event_time_ms=0,
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
            _event(
                event_id="open-nvda-call",
                contract_key=open_key,
                price=1.0,
                lot_id="lot_open-nvda-call",
                raw_payload={"side": "buy"},
            ),
            _event(
                event_id="adjust-missing-lot",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=open_key,
                contracts=0,
                price=0.0,
                source="cli_manual_adjust",
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
    assert fields["quantity_unit"] == "share"
    assert fields["shares_opened"] == "5"
    assert fields["shares_open"] == "3"
    assert fields["shares_closed"] == "2"
    assert fields["cost_basis_total"] == "227.5"
    assert fields["position_key"] == "富途|lx|AAPL|stock|long"
    assert fields["contracts"] == 0
    # The option shape does not describe a stock lot, so it must not be published.
    for option_only in (
        "strike",
        "expiration",
        "expiration_ymd",
        "premium",
        "cash_secured_amount",
        "underlying_share_locked",
    ):
        assert option_only not in fields, option_only


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
    # Precondition: the product really does land off the exact value in floats, so
    # the assertion below is about arithmetic and not only about rendering.
    assert float(5.001) * 100 * 3 != 1500.3
    assert fields["cash_secured_amount"] == "1500.3"
    assert fields["strike"] == "5.001"
    assert fields["premium"] == "0.125"
    for money_key in ("strike", "premium", "cash_secured_amount"):
        assert isinstance(fields[money_key], str), money_key


def test_publisher_applies_money_quantum_to_patched_premium() -> None:
    """§7.4: the published row follows ``quantize_money``, not "whatever fits".

    An adjust patch reaches ``premium_open`` through ``to_decimal`` with no
    decimal-place check, so a patched premium can carry more places than money
    admits -- the open path validates against ``PRICE_DECIMAL_PLACES``, the patch
    path does not. Publishing it verbatim would put a 10-place price in the read
    model; the project's money rule is ``MONEY_QUANTUM`` (6 places, ROUND_HALF_UP).
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
    assert fields["premium"] == "3.123457"


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
    assert fields["strike"] == "100"
    assert fields["premium"] == "0.73"
    assert fields["cash_secured_amount"] == "73000"
    assert fields["close_price"] == "1.5"
    for money_key in (
        "strike",
        "premium",
        "cash_secured_amount",
        "close_price",
    ):
        assert isinstance(fields[money_key], str), money_key
