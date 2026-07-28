from __future__ import annotations

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import parse_exp_to_ms
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


def _key(
    *,
    strike: float,
    expiration_ymd: str,
    option_type: str = "put",
    position_side: str = "short",
) -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        position_side=position_side,
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


def test_publisher_preserves_open_strategy_snapshot() -> None:
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
            TradeEvent(
                event_id="open-pdd-call",
                event_type="open",
                event_time_ms=1000,
                contract_key=_key(
                    strike=100.0,
                    expiration_ymd="2026-07-17",
                    option_type="call",
                    position_side="long",
                ),
                contracts=1,
                price=0.73,
                currency="USD",
                source="opend_push",
                multiplier=100,
                lot_id="lot_open-pdd-call",
                raw_payload={
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
        position_side="long",
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
    fields = projection.lots[0].fields
    assert fields["strategy"] == "yield_enhancement"
    assert fields["leg_role"] == "enhancement_call"
    assert fields["strategy_group_id"] == "ye_nvda_1"
    assert fields["yield_enhancement_mode"] == "income_upside_enhancement"


def test_publisher_does_not_reapply_voided_adjust_strategy_patch() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
        position_side="long",
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


def test_publisher_does_not_apply_strategy_patch_from_rejected_adjust() -> None:
    open_key = _key(
        strike=140.0,
        expiration_ymd="2026-06-19",
        option_type="call",
        position_side="long",
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
