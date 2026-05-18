from __future__ import annotations

from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.intent import trade_intent_from_manual_parse, trade_intent_from_normalized_deal


def test_manual_buy_close_intent_targets_short_position() -> None:
    parsed = {
        "ok": True,
        "raw": "【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20",
        "missing": [],
        "parsed": {
            "account": "lx",
            "symbol": "0700.HK",
            "option_type": "put",
            "side": "long",
            "strike": 480.0,
            "exp": "2026-04-29",
            "premium_per_share": 1.2,
            "contracts": 1,
            "currency": "HKD",
        },
    }

    intent = trade_intent_from_manual_parse(
        parsed,
        action="close",
        raw_text=str(parsed["raw"]),
        broker="富途",
    )

    assert intent.trade_side == "buy"
    assert intent.position_effect == "close"
    assert intent.target_position_side == "short"
    assert intent.symbol == "0700.HK"
    assert intent.contracts == 1


def test_futu_sell_open_intent_targets_short_position() -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=2,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-1"},
    )

    intent = trade_intent_from_normalized_deal(deal)

    assert intent.source_type == "futu_api"
    assert intent.source_event_id == "deal-1"
    assert intent.trade_side == "sell"
    assert intent.position_effect == "open"
    assert intent.target_position_side == "short"
