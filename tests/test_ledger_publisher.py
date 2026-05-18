from __future__ import annotations

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import parse_exp_to_ms
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


def _key(*, strike: float, expiration_ymd: str) -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
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
                        "position_id": "NVDA_20260717_105P_short",
                        "cash_secured_amount": 21000.0,
                    },
                },
            ),
        ]
    )

    assert projection.diagnostics == []
    assert len(projection.lots) == 1
    record = projection.lots[0]
    assert record.record_id == "lot_open-nvda"
    fields = record.fields
    assert fields["source_event_id"] == "open-nvda"
    assert fields["contracts"] == 2
    assert fields["contracts_open"] == 2
    assert fields["strike"] == 105.0
    assert fields["premium"] == 3.1
    assert fields["opened_at"] == 2000
    assert fields["last_action_at"] == 3000
    assert fields["position_id"] == "NVDA_20260717_105P_short"
    assert fields["cash_secured_amount"] == 21000.0
