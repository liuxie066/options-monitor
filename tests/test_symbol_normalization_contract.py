from __future__ import annotations

from pathlib import Path

import pytest

from src.application.multiplier_cache import normalize_symbol as normalize_multiplier_symbol
from src.application.opend_utils import resolve_underlier_alias
from domain.domain.option_position_lots import norm_symbol as normalize_position_symbol
import src.application.ledger.repository as ledger_repository
import src.application.ledger.writer as ledger_writer
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.normalizer import normalize_trade_deal
from src.application.symbol_mutations import normalize_symbol as normalize_config_symbol


def test_symbol_alias_contract_canonicalizes_pop_consistently() -> None:
    expected = "9992.HK"

    assert resolve_underlier_alias("POP") == expected
    assert normalize_config_symbol("POP") == expected
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


def test_ledger_trade_event_canonicalizes_noncanonical_deal_symbol(tmp_path: Path) -> None:
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
        multiplier_source="cache",
        expiration_ymd="2026-05-28",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"symbol": "POP"},
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_writer.persist_trade_event(repo, deal)
    event = repo.list_trade_events()[0]

    assert event["symbol"] == "9992.HK"


def test_ledger_trade_event_rejects_missing_broker_trade_time(tmp_path: Path) -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-missing-time",
        order_id="order-missing-time",
        symbol="0700.HK",
        option_type="call",
        side="sell",
        position_effect="open",
        contracts=1,
        price=6.3,
        strike=510.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-05-28",
        currency="HKD",
        trade_time_ms=None,
        raw_payload={"symbol": "0700.HK"},
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    with pytest.raises(ValueError, match="requires positive trade_time_ms"):
        ledger_writer.persist_trade_event(repo, deal)

    assert repo.list_trade_events() == []
