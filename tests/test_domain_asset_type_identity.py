from __future__ import annotations

from domain.domain.ledger import ContractKey, PositionLot, TradeEvent
from domain.domain.ledger.identity import position_key_for
from domain.domain.ledger.events import validate_trade_event
from domain.domain.trade_contract_identity import (
    derive_position_side,
    normalize_asset_type,
    normalize_quantity_unit,
)


def _contract_key(asset_type: str = "option") -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="AAPL",
        option_type="put",
        strike=150.0,
        expiration_ymd="2026-06-19",
        asset_type=asset_type,
        )


def _trade_event(asset_type: str = "option", quantity_unit: str | None = None) -> TradeEvent:
    return TradeEvent(
        event_id="evt-1",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract_key(asset_type),
        contracts=1,
        price=2.5,
        currency="USD",
        source="api",
        asset_type=asset_type,
        quantity_unit=quantity_unit,
    )


def test_derive_position_side_open_close_mapping() -> None:
    assert derive_position_side("open", "buy") == "long"
    assert derive_position_side("open", "sell") == "short"
    assert derive_position_side("close", "buy") == "short"
    assert derive_position_side("close", "sell") == "long"


def test_derive_position_side_void_and_adjust_are_none() -> None:
    assert derive_position_side("void", "buy") is None
    assert derive_position_side("adjust", "sell") is None


def test_derive_position_side_normalizes_aliases() -> None:
    assert derive_position_side("buy_to_open", "买") == "long"
    assert derive_position_side("sell_to_close", "卖") == "long"


def test_normalize_asset_type_maps_stock_like_and_option() -> None:
    assert normalize_asset_type("stock") == "stock"
    assert normalize_asset_type("etf") == "stock"
    assert normalize_asset_type("equity") == "stock"
    assert normalize_asset_type("option") == "option"
    assert normalize_asset_type("crypto") is None
    assert normalize_asset_type("") is None


def test_normalize_quantity_unit_maps_share_and_contract() -> None:
    assert normalize_quantity_unit("share") == "share"
    assert normalize_quantity_unit("shares") == "share"
    assert normalize_quantity_unit("contract") == "contract"
    assert normalize_quantity_unit("") is None


def test_contract_key_defaults_asset_type_to_option() -> None:
    assert _contract_key().asset_type == "option"
    assert _contract_key("stock").asset_type == "stock"


def test_stock_contract_key_drops_option_specific_fields() -> None:
    key = _contract_key("stock")
    assert key.asset_type == "stock"
    assert key.option_type == ""
    assert key.strike == 0.0
    assert key.expiration_ymd == ""
    # 股票契约键的聚合键不含期权后缀/行权价/到期日，避免与同 underlying 期权 lot 撞键
    assert position_key_for(key, "short") == (
        f"{key.broker}|{key.account}|{key.underlying_symbol}|stock|short"
    )


def test_stock_contract_key_ignores_option_fields() -> None:
    key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="AAPL",
        option_type="put",
        strike=150.0,
        expiration_ymd="2026-06-19",
        asset_type="stock",
        )
    assert key.option_type == ""
    assert key.strike == 0.0
    assert key.expiration_ymd == ""
    assert position_key_for(key, "short") == (
        f"{key.broker}|{key.account}|{key.underlying_symbol}|stock|short"
    )


def test_trade_event_derives_quantity_unit_from_asset_type() -> None:
    option_event = _trade_event("option")
    assert option_event.asset_type == "option"
    assert option_event.quantity_unit == "contract"

    stock_event = _trade_event("stock")
    assert stock_event.asset_type == "stock"
    assert stock_event.quantity_unit == "share"


def test_trade_event_explicit_quantity_unit_is_preserved() -> None:
    event = _trade_event("stock", quantity_unit="share")
    assert event.quantity_unit == "share"


def test_stock_event_forces_multiplier_to_zero() -> None:
    # 股票无乘数：无论传入多少，__post_init__ 一律归零，避免期权语义渗入股票事件。
    assert _trade_event("stock").multiplier == 0.0
    forced = TradeEvent(
        event_id="evt-stock",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract_key("stock"),
        contracts=1,
        price=45.5,
        currency="USD",
        source="api",
        multiplier=100.0,
        asset_type="stock",
    )
    assert forced.multiplier == 0.0


def test_stock_event_validates_without_multiplier() -> None:
    # 股票事件 multiplier=0 不应触发 event_multiplier_invalid（期权仍要求 multiplier>0）。
    event = _trade_event("stock")
    assert event.multiplier == 0.0
    assert not any(
        d.code == "event_multiplier_invalid" for d in validate_trade_event(event)
    )


def test_option_event_still_requires_positive_multiplier() -> None:
    event = _trade_event("option")
    assert event.multiplier == 100.0
    assert not any(
        d.code == "event_multiplier_invalid" for d in validate_trade_event(event)
    )
    invalid = TradeEvent(
        event_id="evt-opt",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract_key("option"),
        contracts=1,
        price=2.5,
        currency="USD",
        source="api",
        multiplier=0.0,
        asset_type="option",
    )
    assert any(
        d.code == "event_multiplier_invalid" for d in validate_trade_event(invalid)
    )


def test_trade_event_roundtrips_asset_type_through_dict() -> None:
    event = _trade_event("stock")
    payload = event.to_dict()
    assert payload["asset_type"] == "stock"
    assert payload["quantity_unit"] == "share"
    decoded = TradeEvent.from_dict(payload)
    assert decoded.asset_type == "stock"
    assert decoded.quantity_unit == "share"


def test_position_lot_propagates_asset_type_from_open_event() -> None:
    lot = PositionLot.from_open_event(_trade_event("stock"), lot_id="lot-1")
    assert lot.asset_type == "stock"


def test_stock_lot_tracks_shares_and_cost_basis() -> None:
    event = TradeEvent(
        event_id="evt-stock",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract_key("stock"),
        contracts=120,
        price=45.5,
        currency="USD",
        source="api",
        asset_type="stock",
    )
    lot = PositionLot.from_open_event(event, lot_id="lot-stock")
    assert lot.asset_type == "stock"
    assert lot.shares_opened == 120.0
    assert lot.shares_open == 120.0
    assert lot.shares_closed == 0.0
    assert lot.cost_basis_total == 45.5 * 120.0
    # 股票 lot 不占用期权专属数量/成本字段
    assert lot.contracts_opened == 0
    assert lot.premium_open == 0.0


def test_stock_lot_close_derives_realized_pnl_from_cost_basis() -> None:
    event = TradeEvent(
        event_id="evt-stock",
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=_contract_key("stock"),
        contracts=100,
        price=10.0,
        currency="USD",
        source="api",
        asset_type="stock",
    )
    lot = PositionLot.from_open_event(event, lot_id="lot-stock")
    close = TradeEvent(
        event_id="evt-stock-close",
        event_type="close",
        event_time_ms=1_700_000_100_000,
        contract_key=_contract_key("stock"),
        contracts=40,
        price=12.0,
        currency="USD",
        source="api",
        asset_type="stock",
    )
    closed = lot.apply_close(close, actual_fee_amount=1.0)
    assert closed.shares_open == 60.0
    assert closed.shares_closed == 40.0
    # (12 - 10) * 40 - 1 = 79
    assert closed.realized_pnl == 79.0


def test_from_stock_settlement_builds_stock_lot_without_option_identity() -> None:
    lot = PositionLot.from_stock_settlement(
        lot_id="stock-lot-1",
        open_event_id="evt-assign-1",
        broker="futu",
        account="lx",
        symbol="AAPL",
        position_side="long",
        currency="USD",
        opened_at_ms=1_700_000_000_000,
        shares_opened=100.0,
        cost_basis_total=4500.0,
    )
    assert lot.asset_type == "stock"
    assert lot.shares_opened == 100.0
    assert lot.shares_open == 100.0
    assert lot.shares_closed == 0.0
    assert lot.cost_basis_total == 4500.0
    assert lot.contracts_opened == 0
    assert lot.premium_open == 0.0
    assert lot.multiplier == 0.0
    # 股票 lot 身份 = broker/account/symbol + stock 方向，不含期权专属字段
    key = lot.contract_key
    assert key.asset_type == "stock"
    assert key.option_type == ""
    assert key.strike == 0.0
    assert key.expiration_ymd == ""
    assert lot.position_key == f"{key.broker}|{key.account}|{key.underlying_symbol}|stock|long"


def test_from_stock_settlement_propagates_close_state() -> None:
    lot = PositionLot.from_stock_settlement(
        lot_id="stock-lot-1",
        open_event_id="evt-assign-1",
        broker="futu",
        account="lx",
        symbol="AAPL",
        position_side="long",
        currency="USD",
        opened_at_ms=1_700_000_000_000,
        shares_opened=100.0,
        shares_open=30.0,
        shares_closed=70.0,
        cost_basis_total=4500.0,
        status="open",
        realized_pnl=123.0,
        last_event_id="evt-sale-3",
    )
    assert lot.shares_open == 30.0
    assert lot.shares_closed == 70.0
    assert lot.realized_pnl == 123.0
    assert lot.last_event_id == "evt-sale-3"


def test_stock_lot_to_dict_emits_stock_fields_only_for_stock() -> None:
    stock = PositionLot.from_open_event(
        _trade_event("stock"),
        lot_id="lot-stock",
    )
    stock_payload = stock.to_dict()
    assert stock_payload["asset_type"] == "stock"
    assert "shares_opened" in stock_payload
    assert "cost_basis_total" in stock_payload

    option = PositionLot.from_open_event(
        _trade_event("option"),
        lot_id="lot-option",
    )
    option_payload = option.to_dict()
    assert "shares_opened" not in option_payload
    assert "cost_basis_total" not in option_payload
