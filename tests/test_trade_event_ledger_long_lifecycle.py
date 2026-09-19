from __future__ import annotations

from typing import Any

from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records_with_diagnostics


def _event(
    event_id: str,
    side: str,
    position_effect: str,
    contracts: int,
    price: float,
    trade_time_ms: int,
    *,
    option_type: str = "call",
    strike: float = 500.0,
    expiration_ymd: str = "2026-04-29",
    raw_payload: dict[str, Any] | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        source_type="broker_trade_event",
        source_name="opend_push",
        broker="富途",
        account="lx",
        symbol="0700.HK",
        option_type=option_type,
        side=side,
        position_effect=position_effect,
        contracts=contracts,
        price=price,
        strike=strike,
        multiplier=100,
        expiration_ymd=expiration_ymd,
        currency="HKD",
        trade_time_ms=trade_time_ms,
        order_id=None,
        multiplier_source="payload",
        raw_payload=dict(raw_payload or {}),
    )


def test_projection_creates_long_lot_for_buy_open() -> None:
    result = project_position_lot_records_with_diagnostics(
        [_event("evt-open-long-1", "buy", "open", 2, 3.5, 1000)]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0].fields
    assert fields["side"] == "long"
    assert fields["contracts_open"] == 2
    assert fields["contracts_closed"] == 0


def test_projection_sell_close_closes_long_lot() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event("evt-open-long-1", "buy", "open", 2, 3.5, 1000),
            _event("evt-close-long-1", "sell", "close", 2, 4.2, 2000,
                   raw_payload={"record_id": "lot_evt-open-long-1"}),
        ]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0].fields
    assert fields["side"] == "long"
    assert fields["contracts_open"] == 0
    assert fields["contracts_closed"] == 2
    assert fields["status"] == "close"
    assert fields["close_type"] == "sell_to_close"
    assert fields["close_reason"] == "broker_trade_sell_to_close"


def test_projection_buy_close_still_closes_short_lot() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event("evt-open-short-1", "sell", "open", 1, 5.1, 1000),
            _event("evt-close-short-1", "buy", "close", 1, 2.2, 2000,
                   raw_payload={"record_id": "lot_evt-open-short-1"}),
        ]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0].fields
    assert fields["side"] == "short"
    assert fields["contracts_open"] == 0
    assert fields["close_type"] == "buy_to_close"
    assert fields["close_reason"] == "broker_trade_buy_to_close"


def test_projection_explicit_close_does_not_cross_same_strike_different_expiry() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event("evt-open-may", "sell", "open", 6, 5.1, 1000, option_type="put",
                   strike=450.0, expiration_ymd="2026-05-28"),
            _event("evt-open-jun", "sell", "open", 3, 8.2, 2000, option_type="put",
                   strike=450.0, expiration_ymd="2026-06-29"),
            _event("evt-close-jun", "buy", "close", 3, 1.0, 3000, option_type="put",
                   strike=450.0, expiration_ymd="2026-06-29",
                   raw_payload={"record_id": "lot_evt-open-jun"}),
        ]
    )

    assert result.diagnostics == []
    lots_by_id = {item.lot_id: item.fields for item in result.lots}
    assert lots_by_id["lot_evt-open-may"]["contracts_open"] == 6
    assert lots_by_id["lot_evt-open-may"]["status"] == "open"
    assert lots_by_id["lot_evt-open-jun"]["contracts_open"] == 0
    assert lots_by_id["lot_evt-open-jun"]["status"] == "close"


def test_projection_explicit_target_mismatch_checks_strike_and_expiry() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event("evt-open-may", "sell", "open", 6, 5.1, 1000, option_type="put",
                   strike=450.0, expiration_ymd="2026-05-28"),
            _event("evt-close-wrong-expiry", "buy", "close", 3, 1.0, 2000, option_type="put",
                   strike=450.0, expiration_ymd="2026-06-29",
                   raw_payload={"record_id": "lot_evt-open-may"}),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["target_contract_mismatch"]
    lots_by_id = {item.lot_id: item.fields for item in result.lots}
    assert lots_by_id["lot_evt-open-may"]["contracts_open"] == 6
    assert lots_by_id["lot_evt-open-may"]["status"] == "open"
