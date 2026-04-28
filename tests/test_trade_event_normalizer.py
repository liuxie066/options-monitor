from __future__ import annotations

from scripts.trade_event_normalizer import normalize_trade_deal


def test_normalize_trade_deal_maps_core_fields() -> None:
    payload = {
        "deal_id": "deal-1",
        "order_id": "order-1",
        "trd_acc_id": "REAL_1",
        "code": "0700.HK",
        "option_type": "PUT",
        "side": "SELL",
        "position_effect": "OPEN",
        "qty": 2,
        "price": "3.93",
        "strike": "480",
        "multiplier": 100,
        "expiration": "20260429",
        "currency": "HKD",
        "create_time": "2026-04-09 13:10:25",
    }

    deal = normalize_trade_deal(payload, futu_account_mapping={"REAL_1": "lx"})

    assert deal.deal_id == "deal-1"
    assert deal.internal_account == "lx"
    assert deal.symbol == "0700.HK"
    assert deal.option_type == "put"
    assert deal.side == "sell"
    assert deal.position_effect == "open"
    assert deal.contracts == 2
    assert deal.price == 3.93
    assert deal.strike == 480.0
    assert deal.multiplier == 100
    assert deal.multiplier_source == "payload"
    assert deal.expiration_ymd == "2026-04-29"
    assert deal.currency == "HKD"
    assert isinstance(deal.trade_time_ms, int)


def test_normalize_trade_deal_keeps_unknown_position_effect_when_missing() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-2",
            "account_id": "REAL_2",
            "symbol": "NVDA",
            "option_type": "CALL",
            "trade_side": "BUY",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "multiplier": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"REAL_2": "sy"},
    )

    assert deal.position_effect is None
    assert deal.internal_account == "sy"
    assert deal.multiplier == 100
    assert deal.multiplier_source == "payload"
    assert deal.expiration_ymd == "2026-06-18"


def test_normalize_trade_deal_recognizes_additional_account_id_fields() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-3",
            "trade_acc_id": "987654321",
            "symbol": "NVDA",
            "option_type": "CALL",
            "trade_side": "SELL",
            "position_effect": "OPEN",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "multiplier": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"987654321": "lx"},
    )

    assert deal.futu_account_id == "987654321"
    assert deal.internal_account == "lx"
    assert deal.visible_account_fields == {"trade_acc_id": "987654321"}
    assert deal.account_mapping_keys == ["987654321"]


def test_normalize_trade_deal_parses_futu_option_code_with_lookup_underlying_fields() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-4",
            "futu_account_id": "281756479859383816",
            "code": "HK.POP260528P150000",
            "stock_name": "泡泡玛特",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
            "create_time": "2026-04-28 10:15:56",
        },
        futu_account_mapping={"281756479859383816": "lx"},
    )

    assert deal.internal_account == "lx"
    assert deal.symbol == "9992.HK"
    assert deal.option_type == "put"
    assert deal.side == "sell"
    assert deal.position_effect == "open"
    assert deal.strike == 150.0
    assert deal.expiration_ymd == "2026-05-28"
    assert deal.currency == "HKD"
