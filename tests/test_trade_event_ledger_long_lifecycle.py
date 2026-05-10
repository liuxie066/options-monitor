from __future__ import annotations

from domain.domain.option_position_ledger import TradeEvent, project_position_lot_records_with_diagnostics


def _event(*, event_id: str, side: str, position_effect: str, contracts: int, price: float, trade_time_ms: int) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        source_type="broker_trade_event",
        source_name="opend_push",
        broker="富途",
        account="lx",
        symbol="0700.HK",
        option_type="call",
        side=side,
        position_effect=position_effect,
        contracts=contracts,
        price=price,
        strike=500.0,
        multiplier=100,
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=trade_time_ms,
        order_id=None,
        multiplier_source="payload",
        raw_payload={},
    )


def test_projection_creates_long_lot_for_buy_open() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event(
                event_id="evt-open-long-1",
                side="buy",
                position_effect="open",
                contracts=2,
                price=3.5,
                trade_time_ms=1000,
            )
        ]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0]["fields"]
    assert fields["side"] == "long"
    assert fields["contracts_open"] == 2
    assert fields["contracts_closed"] == 0


def test_projection_sell_close_closes_long_lot() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event(
                event_id="evt-open-long-1",
                side="buy",
                position_effect="open",
                contracts=2,
                price=3.5,
                trade_time_ms=1000,
            ),
            _event(
                event_id="evt-close-long-1",
                side="sell",
                position_effect="close",
                contracts=2,
                price=4.2,
                trade_time_ms=2000,
            ),
        ]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0]["fields"]
    assert fields["side"] == "long"
    assert fields["contracts_open"] == 0
    assert fields["contracts_closed"] == 2
    assert fields["status"] == "close"
    assert fields["close_type"] == "sell_to_close"
    assert fields["close_reason"] == "broker_trade_sell_to_close"


def test_projection_buy_close_still_closes_short_lot() -> None:
    result = project_position_lot_records_with_diagnostics(
        [
            _event(
                event_id="evt-open-short-1",
                side="sell",
                position_effect="open",
                contracts=1,
                price=5.1,
                trade_time_ms=1000,
            ),
            _event(
                event_id="evt-close-short-1",
                side="buy",
                position_effect="close",
                contracts=1,
                price=2.2,
                trade_time_ms=2000,
            ),
        ]
    )

    assert result.diagnostics == []
    assert len(result.lots) == 1
    fields = result.lots[0]["fields"]
    assert fields["side"] == "short"
    assert fields["contracts_open"] == 0
    assert fields["close_type"] == "buy_to_close"
    assert fields["close_reason"] == "broker_trade_buy_to_close"
