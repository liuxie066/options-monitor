from __future__ import annotations

from scripts.auto_trade_intake import _build_audit_event
from scripts.trade_event_normalizer import NormalizedTradeDeal


def test_build_audit_event_promotes_multiplier_source_to_top_level() -> None:
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
        multiplier_source="builtin.hk.common",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-1"},
    )

    event = _build_audit_event("normalized", deal=deal)

    assert event["phase"] == "normalized"
    assert event["deal_id"] == "deal-1"
    assert event["account"] == "lx"
    assert event["multiplier"] == 100
    assert event["multiplier_source"] == "builtin.hk.common"
    assert event["deal"]["multiplier_source"] == "builtin.hk.common"
