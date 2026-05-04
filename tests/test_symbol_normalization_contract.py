from __future__ import annotations

from scripts.multiplier_cache import normalize_symbol as normalize_multiplier_symbol
from scripts.opend_utils import resolve_underlier_alias
from scripts.option_positions_core.ledger import trade_event_from_normalized_deal
from scripts.option_positions_core.domain import norm_symbol as normalize_position_symbol
from scripts.trade_event_normalizer import NormalizedTradeDeal
from scripts.trade_event_normalizer import normalize_trade_deal
from src.application.watchlist_mutations import normalize_symbol as normalize_watchlist_symbol


def test_symbol_alias_contract_canonicalizes_pop_consistently() -> None:
    expected = "9992.HK"

    assert resolve_underlier_alias("POP") == expected
    assert normalize_watchlist_symbol("POP") == expected
    assert normalize_multiplier_symbol("POP") == expected
    assert normalize_position_symbol("POP") == expected


def test_trade_event_contract_canonicalizes_option_code_root_alias() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-contract-1",
            "futu_account_id": "281756479859383816",
            "code": "HK.POP260528P150000",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
            "create_time": "2026-04-28 10:15:56",
        },
        futu_account_mapping={"281756479859383816": "lx"},
    )

    assert deal.symbol == "9992.HK"


def test_ledger_trade_event_canonicalizes_noncanonical_deal_symbol() -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-contract-2",
        order_id="order-contract-2",
        symbol="POP",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=6.3,
        strike=150.0,
        multiplier=1000,
        multiplier_source="config:intake.multiplier_by_symbol",
        expiration_ymd="2026-05-28",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"symbol": "POP"},
    )

    event = trade_event_from_normalized_deal(deal)

    assert event.symbol == "9992.HK"
