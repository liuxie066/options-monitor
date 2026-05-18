from __future__ import annotations

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.option_position_lots import parse_exp_to_ms


def _key(
    *,
    account: str = "sy",
    symbol: str = "0700.HK",
    option_type: str = "put",
    side: str = "short",
    strike: float = 450.0,
    expiration_ymd: str = "2026-05-28",
) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd=expiration_ymd,
    )


def _event(
    *,
    event_id: str,
    event_type: str,
    contract_key: ContractKey,
    contracts: int,
    event_time_ms: int,
    price: float = 1.0,
    lot_id: str | None = None,
    target_lot_id: str | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=contracts,
        price=price,
        currency="HKD",
        source="test",
        multiplier=100,
        lot_id=lot_id,
        target_lot_id=target_lot_id,
    )


def test_projection_closes_same_expiry_0700_lots_without_crossing_later_expiry() -> None:
    may_call_510 = _key(option_type="call", strike=510.0, expiration_ymd="2026-05-28")
    may_put_450 = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")
    jun_put_450 = _key(option_type="put", strike=450.0, expiration_ymd="2026-06-29")

    result = project_trade_events(
        [
            _event(event_id="open-call-may", event_type="open", contract_key=may_call_510, contracts=2, event_time_ms=1000, lot_id="lot_call_may"),
            _event(event_id="open-put-may", event_type="open", contract_key=may_put_450, contracts=6, event_time_ms=2000, lot_id="lot_put_may"),
            _event(event_id="open-put-jun", event_type="open", contract_key=jun_put_450, contracts=3, event_time_ms=3000, lot_id="lot_put_jun"),
            _event(
                event_id="expire-call-may",
                event_type="expire_close",
                contract_key=may_call_510,
                contracts=2,
                event_time_ms=4000,
                target_lot_id="lot_call_may",
            ),
            _event(
                event_id="expire-put-may",
                event_type="expire_close",
                contract_key=may_put_450,
                contracts=6,
                event_time_ms=5000,
                target_lot_id="lot_put_may",
            ),
        ]
    )

    assert result.diagnostics == []
    lots = {item.lot_id: item for item in result.lots}
    assert lots["lot_call_may"].status == "close"
    assert lots["lot_call_may"].contracts_open == 0
    assert lots["lot_put_may"].status == "close"
    assert lots["lot_put_may"].contracts_open == 0
    assert lots["lot_put_jun"].status == "open"
    assert lots["lot_put_jun"].contracts_open == 3
    assert len(result.views) == 1
    assert result.views[0].contract_key == jun_put_450
    assert result.views[0].lot_ids == ("lot_put_jun",)


def test_projection_view_keeps_multiple_same_contract_lots_as_read_only_aggregation() -> None:
    key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")

    result = project_trade_events(
        [
            _event(event_id="open-a", event_type="open", contract_key=key, contracts=2, event_time_ms=1000, lot_id="lot_a"),
            _event(event_id="open-b", event_type="open", contract_key=key, contracts=6, event_time_ms=2000, lot_id="lot_b"),
            _event(event_id="close-a", event_type="close", contract_key=key, contracts=2, event_time_ms=3000, target_lot_id="lot_a"),
        ]
    )

    assert result.diagnostics == []
    lots = {item.lot_id: item for item in result.lots}
    assert lots["lot_a"].status == "close"
    assert lots["lot_b"].status == "open"
    assert result.views[0].position_key == key.position_key
    assert result.views[0].total_contracts_open == 6
    assert result.views[0].lot_ids == ("lot_b",)


def test_projection_rejects_close_target_with_different_expiry() -> None:
    may_key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")
    jun_key = _key(option_type="put", strike=450.0, expiration_ymd="2026-06-29")

    result = project_trade_events(
        [
            _event(event_id="open-may", event_type="open", contract_key=may_key, contracts=6, event_time_ms=1000, lot_id="lot_may"),
            _event(event_id="close-wrong-exp", event_type="close", contract_key=jun_key, contracts=3, event_time_ms=2000, target_lot_id="lot_may"),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["target_contract_mismatch"]
    lot = result.lots[0]
    assert lot.lot_id == "lot_may"
    assert lot.status == "open"
    assert lot.contracts_open == 6


def test_projection_rejects_close_without_exact_target_lot_id() -> None:
    key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")

    result = project_trade_events(
        [
            _event(event_id="open-a", event_type="open", contract_key=key, contracts=6, event_time_ms=1000, lot_id="lot_a"),
            _event(event_id="close-no-target", event_type="close", contract_key=key, contracts=3, event_time_ms=2000),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["target_lot_id_required"]
    assert result.lots[0].status == "open"
    assert result.lots[0].contracts_open == 6


def test_projection_rejects_oversized_close_without_partial_mutation() -> None:
    key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")

    result = project_trade_events(
        [
            _event(event_id="open-a", event_type="open", contract_key=key, contracts=3, event_time_ms=1000, lot_id="lot_a"),
            _event(event_id="close-too-large", event_type="close", contract_key=key, contracts=4, event_time_ms=2000, target_lot_id="lot_a"),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["close_contracts_exceed_open"]
    assert result.lots[0].status == "open"
    assert result.lots[0].contracts_open == 3


def test_projection_rejects_duplicate_lot_id() -> None:
    key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")

    result = project_trade_events(
        [
            _event(event_id="open-a", event_type="open", contract_key=key, contracts=3, event_time_ms=1000, lot_id="lot_dup"),
            _event(event_id="open-b", event_type="open", contract_key=key, contracts=2, event_time_ms=2000, lot_id="lot_dup"),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["duplicate_lot_id"]
    assert [item.lot_id for item in result.lots] == ["lot_dup"]
    assert result.lots[0].contracts_open == 3


def test_projection_applies_adjust_patch_to_target_lot_state() -> None:
    current_key = _key(
        account="lx",
        symbol="NVDA",
        option_type="put",
        strike=100.0,
        expiration_ymd="2026-06-19",
    )
    adjusted_key = _key(
        account="lx",
        symbol="NVDA",
        option_type="put",
        strike=105.0,
        expiration_ymd="2026-07-17",
    )
    adjusted_exp_ms = parse_exp_to_ms("2026-07-17")
    assert adjusted_exp_ms is not None

    result = project_trade_events(
        [
            _event(
                event_id="open-nvda",
                event_type="open",
                contract_key=current_key,
                contracts=1,
                price=2.5,
                event_time_ms=1000,
                lot_id="lot_nvda",
            ),
            TradeEvent(
                event_id="adjust-nvda",
                event_type="adjust",
                event_time_ms=3000,
                contract_key=current_key,
                contracts=0,
                price=0.0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id="lot_nvda",
                raw_payload={
                    "patch": {
                        "contracts": 2,
                        "contracts_open": 2,
                        "contracts_closed": 0,
                        "strike": 105.0,
                        "expiration": adjusted_exp_ms,
                        "premium": 3.1,
                        "currency": "USD",
                        "opened_at": 2000,
                        "last_action_at": 3000,
                    }
                },
            ),
        ]
    )

    assert result.diagnostics == []
    lot = result.lots[0]
    assert lot.lot_id == "lot_nvda"
    assert lot.contract_key == adjusted_key
    assert lot.contracts_opened == 2
    assert lot.contracts_open == 2
    assert lot.contracts_closed == 0
    assert lot.premium_open == 3.1
    assert lot.currency == "USD"
    assert lot.opened_at_ms == 2000
    assert lot.last_event_id == "adjust-nvda"
    assert result.views[0].contract_key == adjusted_key
    assert result.views[0].cash_secured_amount == 21000.0


def test_projection_rejects_adjust_patch_with_unsupported_field() -> None:
    key = _key(option_type="put", strike=450.0, expiration_ymd="2026-05-28")

    result = project_trade_events(
        [
            _event(
                event_id="open-a",
                event_type="open",
                contract_key=key,
                contracts=1,
                event_time_ms=1000,
                lot_id="lot_a",
            ),
            TradeEvent(
                event_id="adjust-a",
                event_type="adjust",
                event_time_ms=2000,
                contract_key=key,
                contracts=0,
                price=0.0,
                currency="HKD",
                source="test",
                multiplier=100,
                target_lot_id="lot_a",
                raw_payload={"patch": {"contracts": 2, "free_form_field": "bad"}},
            ),
        ]
    )

    assert [item.code for item in result.diagnostics] == ["adjust_patch_invalid"]
    assert "unsupported fields: free_form_field" in result.diagnostics[0].details["error"]
    assert result.lots[0].contracts_opened == 1
