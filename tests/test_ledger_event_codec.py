from __future__ import annotations

import json
from pathlib import Path

from domain.domain.ledger import ContractKey, TradeEvent
from tests.ledger_legacy_helpers import LegacyTradeEvent
from src.application.ledger import repository as ledger_repository
from src.application.ledger.event_codec import encode_trade_event_for_storage, import_stored_trade_events
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


def _contract_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="AAPL",
        option_type="put",
        position_side="short",
        strike=150.0,
        expiration_ymd="2026-06-19",
    )


def test_event_codec_encodes_legacy_trade_event_as_canonical_payload() -> None:
    legacy = LegacyTradeEvent(
        event_id="deal-open-1",
        source_type="broker_trade_event",
        source_name="opend_push",
        broker="富途",
        account="lx",
        symbol="AAPL",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.0,
        strike=150.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        currency="USD",
        trade_time_ms=1000,
        order_id="order-1",
        multiplier_source="payload",
        raw_payload={"deal_id": "deal-open-1"},
    )

    encoded = encode_trade_event_for_storage(legacy)

    assert encoded.event_id == "deal-open-1"
    assert encoded.event_time_ms == 1000
    assert encoded.payload["event_type"] == "open"
    assert encoded.payload["event_time_ms"] == 1000
    assert encoded.payload["contract_key"]["underlying_symbol"] == "AAPL"
    assert "position_effect" not in encoded.payload
    assert "trade_time_ms" not in encoded.payload


def test_sqlite_repo_stores_canonical_event_json_and_returns_compat_payload(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    event = TradeEvent(
        event_id="open-aapl",
        event_type="open",
        event_time_ms=1000,
        contract_key=_contract_key(),
        contracts=1,
        price=1.0,
        currency="USD",
        source="manual",
        multiplier=100,
        lot_id="lot_open-aapl",
    )

    assert repo.upsert_trade_event(event) is True
    assert repo.upsert_trade_event(event) is False

    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT event_json, trade_time_ms FROM trade_events WHERE event_id = ?", ("open-aapl",)).fetchone()
    stored = json.loads(str(row["event_json"]))
    assert row["trade_time_ms"] == 1000
    assert stored["event_type"] == "open"
    assert stored["event_time_ms"] == 1000
    assert "position_effect" not in stored

    listed = repo.list_trade_events()
    assert listed[0]["event_type"] == "open"
    assert listed[0]["trade_time_ms"] == 1000
    assert listed[0]["position_effect"] == "open"
    assert listed[0]["side"] == "sell"


def test_publisher_projects_mixed_canonical_and_legacy_stored_events() -> None:
    canonical_open = TradeEvent(
        event_id="open-aapl",
        event_type="open",
        event_time_ms=1000,
        contract_key=_contract_key(),
        contracts=2,
        price=1.0,
        currency="USD",
        source="manual",
        multiplier=100,
        lot_id="lot_open-aapl",
    ).to_dict()
    legacy_close = LegacyTradeEvent(
        event_id="close-aapl",
        source_type="broker_trade_event",
        source_name="opend_push",
        broker="富途",
        account="lx",
        symbol="AAPL",
        option_type="put",
        side="buy",
        position_effect="close",
        contracts=1,
        price=0.5,
        strike=150.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        currency="USD",
        trade_time_ms=2000,
        order_id="order-2",
        multiplier_source="payload",
        raw_payload={"record_id": "lot_open-aapl"},
    )

    imported, diagnostics = import_stored_trade_events([canonical_open, legacy_close])
    assert diagnostics == []
    assert [event.event_id for event in imported] == ["open-aapl", "close-aapl"]
    projection = project_stored_trade_events_to_position_lots([canonical_open, legacy_close])

    assert projection.diagnostics == []
    assert projection.lots[0].record_id == "lot_open-aapl"
    assert projection.lots[0].fields["contracts_open"] == 1
