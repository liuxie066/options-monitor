from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from domain.domain.ledger import ContractKey, TradeEvent
from tests.ledger_legacy_helpers import LegacyTradeEvent
from src.application.ledger import repository as ledger_repository
from src.application.ledger.event_codec import (
    encode_trade_event_for_storage,
    import_stored_trade_events,
    stored_trade_event_to_ledger_event,
)
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


def _contract_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="AAPL",
        option_type="put",
        strike=150.0,
        expiration_ymd="2026-06-19",
        )


def _stock_contract_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="AAPL",
        option_type="",
        strike=0.0,
        expiration_ymd="",
        asset_type="stock",
        )


def _legacy_event(**overrides: object) -> LegacyTradeEvent:
    base = {
        "event_id": "deal-open-1",
        "source_type": "broker_trade_event",
        "source_name": "opend_push",
        "broker": "富途",
        "account": "lx",
        "symbol": "AAPL",
        "option_type": "put",
        "side": "sell",
        "position_effect": "open",
        "contracts": 1,
        "price": 1.0,
        "strike": 150.0,
        "multiplier": 100,
        "expiration_ymd": "2026-06-19",
        "currency": "USD",
        "trade_time_ms": 1000,
        "order_id": "order-1",
        "multiplier_source": "payload",
        "raw_payload": {"deal_id": "deal-open-1"},
    }
    base.update(overrides)
    return LegacyTradeEvent(**base)


def _canonical_event_kwargs(**overrides: object) -> dict:
    base = {
        "event_id": "open-aapl",
        "event_type": "open",
        "event_time_ms": 1000,
        "contract_key": _contract_key(),
        "contracts": 1,
        "price": 1.0,
        "currency": "USD",
        "source": "manual",
        "multiplier": 100,
        "lot_id": "lot_open-aapl",
        # §9.2 step 3: the short put side travels as the trade side.
        "raw_payload": {"side": "sell"},
    }
    base.update(overrides)
    return base


def _canonical_event(**overrides: object) -> TradeEvent:
    return TradeEvent(**_canonical_event_kwargs(**overrides))


def test_event_codec_rejects_legacy_trade_event_payloads() -> None:
    legacy = _legacy_event()

    with pytest.raises(ValueError) as _caught:
        encode_trade_event_for_storage(legacy.to_legacy_dict())
    exc = _caught.value
    assert "non_canonical_trade_event_schema" in str(exc)


def test_sqlite_repo_stores_canonical_event_json_and_returns_compat_payload(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    event = _canonical_event()

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


def test_sqlite_event_money_round_trip_preserves_decimal_precision(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = _canonical_event(
        price=Decimal("123.1234567890123456789"),
        fees=Decimal("0.1234567890123456789"),
    )
    assert repo.upsert_trade_event(event) is True
    assert repo.upsert_trade_event(event) is False
    with repo._connect() as conn:
        stored = json.loads(conn.execute("SELECT event_json FROM trade_events").fetchone()[0])
    assert stored["price"] == str(event.price)
    assert stored["fees"] == str(event.fees)
    decoded, diagnostics = stored_trade_event_to_ledger_event(stored)
    assert diagnostics == []
    assert decoded.price == event.price
    assert decoded.fees == event.fees


def test_stock_quantity_survives_sqlite_and_legacy_integer_events(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "quantity.sqlite3")
    for index, quantity in enumerate((Decimal("2.500000000000000001"), 3)):
        event = _canonical_event(event_id=f"stock-{index}", lot_id=f"stock-lot-{index}", contract_key=_stock_contract_key(),
                                 asset_type="stock", contracts=quantity, fees=Decimal(index), raw_payload={"side": "buy"})
        assert repo.upsert_trade_event(event)
        assert not repo.upsert_trade_event(event)
        stored = next(row for row in repo.list_trade_events() if row["event_id"] == event.event_id)
        decoded, diagnostics = stored_trade_event_to_ledger_event(stored)
        assert not diagnostics
        assert decoded.contracts == Decimal(str(quantity))
        assert isinstance(decoded.contracts, Decimal)
    legacy = event.to_dict()
    legacy["contracts"] = 3
    assert not repo.upsert_trade_event(legacy)
    from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
    run_position_projection_forced_full(repo, [])
    lots = {row["lot_id"]: row["fields"] for row in repo.list_position_lots()}
    assert lots["stock-lot-0"]["shares_open"] == "2.500000000000000001"
    assert lots["stock-lot-1"]["shares_open"] == "3"
    assert lots["stock-lot-0"]["cost_basis_total"] is None
    assert lots["stock-lot-0"]["realized_pnl"] is None
    assert lots["stock-lot-1"]["cost_basis_total"] == "4"


def test_fractional_option_quantity_is_rejected_without_truncation() -> None:
    with pytest.raises(ValueError, match="whole number"):
        _canonical_event(contracts=Decimal("1.5"))
    event, diagnostics = stored_trade_event_to_ledger_event({**_canonical_event().to_dict(), "contracts": "1.5"})
    assert event is None
    assert diagnostics


def test_publisher_rejects_mixed_canonical_and_legacy_stored_events() -> None:
    canonical_open = _canonical_event(contracts=2).to_dict()
    legacy_close = _legacy_event(
        event_id="close-aapl",
        side="buy",
        position_effect="close",
        price=0.5,
        trade_time_ms=2000,
        order_id="order-2",
        raw_payload={"record_id": "lot_open-aapl"},
    )

    legacy_payload = legacy_close.to_legacy_dict()
    imported, diagnostics = import_stored_trade_events([canonical_open, legacy_payload])
    assert [item.code for item in diagnostics] == ["non_canonical_trade_event_schema"]
    assert [event.event_id for event in imported] == ["open-aapl"]
    projection = project_stored_trade_events_to_position_lots([canonical_open, legacy_payload])

    assert [item.code for item in projection.diagnostics] == ["non_canonical_trade_event_schema"]
    assert projection.lots[0].lot_id == "lot_open-aapl"
    assert projection.lots[0].fields["contracts_open"] == 2


def test_encode_rejects_event_values_that_cannot_form_a_publishable_lot() -> None:
    base = _canonical_event_kwargs(
        event_id="invalid-open",
        source="test",
        lot_id="lot-invalid-open",
        raw_payload={},
    )
    invalid_variants = [
        {"event_time_ms": 0},
        {"price": float("nan")},
        {"currency": "EUR"},
        {"source": ""},
        {"multiplier": 0},
        {"multiplier": float("inf")},
    ]

    for overrides in invalid_variants:
        with pytest.raises(
            ValueError,
            match="trade event could not be encoded|trade event failed validation",
        ):
            encode_trade_event_for_storage(TradeEvent(**(base | overrides)))


def test_codec_persists_asset_type_and_quantity_unit() -> None:
    option_event = _canonical_event(
        event_id="open-aapl-opt",
        lot_id="lot_open-aapl-opt",
        asset_type="option",
    )
    stock_event = _canonical_event(
        event_id="open-aapl-stk",
        contract_key=_stock_contract_key(),
        contracts=50,
        price=45.5,
        lot_id="lot_open-aapl-stk",
        asset_type="stock",
    )
    encoded_opt = encode_trade_event_for_storage(option_event)
    encoded_stk = encode_trade_event_for_storage(stock_event)
    assert encoded_opt.payload["asset_type"] == "option"
    assert encoded_opt.payload["quantity_unit"] == "contract"
    assert encoded_stk.payload["asset_type"] == "stock"
    assert encoded_stk.payload["quantity_unit"] == "share"
    stored_stk = json.loads(encoded_stk.event_json)
    assert stored_stk["asset_type"] == "stock"
    assert stored_stk["quantity_unit"] == "share"


def test_legacy_canonical_payload_without_asset_type_defaults_to_option() -> None:
    contract_key = _contract_key().to_dict()
    contract_key.pop("asset_type", None)
    legacy_payload = {
        "event_id": "legacy-open-1",
        "event_type": "open",
        "event_time_ms": 1000,
        "contract_key": contract_key,
        "contracts": 2,
        "price": 1.0,
        "currency": "USD",
        "source": "manual",
        "multiplier": 100,
        "lot_id": "lot_legacy_open-1",
    }
    event, diagnostics = stored_trade_event_to_ledger_event(legacy_payload)
    assert diagnostics == []
    assert event is not None
    assert event.asset_type == "option"
    assert event.quantity_unit == "contract"
    # 旧事件重写后 asset_type/quantity_unit 被显式持久化
    reencoded = encode_trade_event_for_storage(legacy_payload)
    assert reencoded.payload["asset_type"] == "option"
    assert reencoded.payload["quantity_unit"] == "contract"


def test_sqlite_repo_stores_stock_event_asset_type_and_quantity_unit(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    stock_event = _canonical_event(
        event_id="open-aapl-stk",
        contract_key=_stock_contract_key(),
        contracts=50,
        price=45.5,
        lot_id="lot_open-aapl-stk",
        asset_type="stock",
    )
    assert repo.upsert_trade_event(stock_event) is True
    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?", ("open-aapl-stk",)
        ).fetchone()
    stored = json.loads(str(row["event_json"]))
    assert stored["asset_type"] == "stock"
    assert stored["quantity_unit"] == "share"
    listed = repo.list_trade_events()
    assert listed[0]["asset_type"] == "stock"
    assert listed[0]["quantity_unit"] == "share"
