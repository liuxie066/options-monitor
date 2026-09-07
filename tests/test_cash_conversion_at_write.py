from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.cash_conversion import (
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
    MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
    validate_observed_cash_conversion,
)
from domain.domain.option_position_lots import OpenPositionCommand
from src.application.cash_conversion import build_cash_conversion
from src.application.ledger import writer_trade_events as ledger_writer
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.positions.workflows import execute_broker_assigned_stock_sale, execute_manual_assigned_stock_sale
from src.application.trades.normalizer import normalize_trade_deal
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 23, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _open_event(event_id: str, *, price: float) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=_ms("2026-07-03T10:00:00"),
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-08-21",
        ),
        contracts=1,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0.0,
        lot_id=f"lot-{event_id}",
        raw_payload={},
    )


def test_trade_write_freezes_cny_and_duplicate_keeps_original_booking_rate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
    event = _open_event("open-1", price=2.0)
    fx_payloads = iter(
        [
            {"rates": {"USDCNY": 7.2}, "timestamp": "2026-07-03T02:00:00+00:00"},
            {"rates": {"USDCNY": 8.0}, "timestamp": "2026-07-04T02:00:00+00:00"},
        ]
    )
    observed_times = iter([_ms("2026-07-03T10:00:01"), _ms("2026-07-04T10:00:00")])
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo, **_kwargs: next(fx_payloads))
    monkeypatch.setattr(ledger_writer, "utc_now_ms", lambda: next(observed_times))

    first = persist_trade_event_object(repo, event)
    duplicate = persist_trade_event_object(repo, event)

    assert first.created is True
    assert duplicate.created is False
    stored = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert stored["option_trade_cash_gross"]["fx_rate"] == "7.2"
    assert stored["option_trade_cash_gross"]["amount_cny"] == "1440"
    assert stored["option_fee_cash"]["amount_cny"] == "-18.16776"


def test_missing_fx_is_pending_but_zero_cash_needs_no_rate(tmp_path: Path, monkeypatch) -> None:
    pending_repo = SQLiteOptionPositionsRepository(tmp_path / "pending.sqlite3")
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo, **_kwargs: {})
    monkeypatch.setattr(ledger_writer, "utc_now_ms", lambda: NOW_MS)
    persist_trade_event_object(pending_repo, _open_event("pending", price=2.0))
    zero = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:zero",
        amount=0,
        currency="USD",
        fx_payload={},
        effective_at_ms=NOW_MS,
        observed_at_ms=NOW_MS,
    )
    stale = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:stale",
        amount=200,
        currency="USD",
        fx_payload={"rates": {"USDCNY": 7.2}, "timestamp": "2026-07-20T02:00:00+00:00"},
        effective_at_ms=NOW_MS,
        observed_at_ms=NOW_MS,
    )

    pending = pending_repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]
    assert pending["status"] == "pending"
    assert pending["amount_cny"] is None
    assert zero["status"] == "observed"
    assert zero["method"] == "zero_identity"
    assert zero["amount_cny"] == "0"
    assert stale["status"] == "pending"
    assert stale["amount_cny"] is None
    assert stale["missing_reason"] == "USDCNY booking FX outside 24h event window"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("conversion_id", "cashfx_forged", "conversion_id_mismatch"),
        ("amount_cny", "999999", "fx_arithmetic_mismatch"),
        ("fx_rate", "-7.2", "fx_rate_invalid"),
        ("fx_rate", "1e999999", "invalid_numeric_contract"),
        ("native_amount", "1e999999", "invalid_numeric_contract"),
        ("amount_cny", "1e999999", "invalid_numeric_contract"),
        ("method", "unknown", "fx_provenance_invalid"),
        ("native_currency", "?", "identity_contract_mismatch"),
        ("rate_timestamp", "2026-07-10T02:00:00+00:00", "rate_timestamp_outside_booking_window"),
    ],
)
def test_observed_cash_conversion_rejects_tampered_contract(
    field: str,
    value: str,
    reason: str,
) -> None:
    effective_at_ms = _ms("2026-07-03T10:00:00")
    conversion = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:tamper",
        amount=200,
        currency="USD",
        fx_payload={
            "rates": {"USDCNY": 7.2},
            "timestamp": "2026-07-03T02:00:00+00:00",
        },
        effective_at_ms=effective_at_ms,
        observed_at_ms=effective_at_ms + 1_000,
    )
    conversion[field] = value

    amount_cny, issue = validate_observed_cash_conversion(
        conversion,
        cash_fact_id="option_trade_cash_gross:tamper",
        native_amount=200,
        native_currency="USD",
        effective_at_ms=effective_at_ms,
    )

    assert amount_cny is None
    assert issue == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("rate_timestamp", "2026-07-06T02:00:00+00:00", "fx_provenance_invalid"),
        (
            "rate_timestamp",
            "2026-06-27T01:59:59+00:00",
            "rate_timestamp_outside_booking_window",
        ),
        ("rate_evidence_fact_id", None, "fx_provenance_invalid"),
        ("rate_source", "broker_snapshot", "fx_provenance_invalid"),
    ],
)
def test_historical_business_day_cash_conversion_rejects_invalid_provenance(
    field: str,
    value: str | None,
    reason: str,
) -> None:
    effective_at_ms = _ms("2026-07-05T10:00:00")
    conversion = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:carry",
        amount=200,
        currency="USD",
        fx_payload={
            "rates": {"USDCNY": 7.2},
            "timestamp": "2026-07-03T01:15:00+00:00",
        },
        effective_at_ms=effective_at_ms,
        observed_at_ms=effective_at_ms + 1_000,
        rate_source="manual_correction",
        rate_source_id="manual:carry:2026-07-05",
        rate_evidence_fact_id="fx-official",
        method=HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
        max_rate_distance_ms=MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
    )
    conversion[field] = value

    amount_cny, issue = validate_observed_cash_conversion(
        conversion,
        cash_fact_id="option_trade_cash_gross:carry",
        native_amount=200,
        native_currency="USD",
        effective_at_ms=effective_at_ms,
    )

    assert amount_cny is None
    assert issue == reason


def test_forged_cny_is_rejected_at_conversion_boundary() -> None:
    event = _open_event("forged", price=2.0)
    conversion = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:forged",
        amount=200,
        currency="USD",
        fx_payload={
            "rates": {"USDCNY": 7.2},
            "timestamp": "2026-07-03T02:00:00+00:00",
        },
        effective_at_ms=event.event_time_ms,
        observed_at_ms=event.event_time_ms + 1_000,
    )
    conversion["amount_cny"] = "999999"
    amount_cny, issue = validate_observed_cash_conversion(
        conversion,
        cash_fact_id="option_trade_cash_gross:forged",
        native_amount=200,
        native_currency="USD",
        effective_at_ms=event.event_time_ms,
    )

    assert amount_cny is None
    assert issue == "fx_arithmetic_mismatch"


def test_assignment_and_assigned_stock_sale_store_their_own_cny_cash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "output_shared" / "state"
    repo = SQLiteOptionPositionsRepository(state_dir / "option_positions.sqlite3")
    fx_payload = {
        "rates": {"USDCNY": 7.2},
        "timestamp": "2026-07-23T01:00:00+00:00",
    }
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo, **_kwargs: fx_payload)
    monkeypatch.setattr("src.application.positions.workflows.load_cash_fx_payload", lambda _repo, **_kwargs: fx_payload)
    monkeypatch.setattr("src.application.ledger.writer_lifecycle_evidence.load_cash_fx_payload", lambda _repo, **_kwargs: fx_payload)
    persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-07-23T08:00:00"),
        ),
    )
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=_ms("2026-07-23T09:00:00"),
    )
    assignment = next(item for item in repo.list_trade_events() if item.get("event_type") == "assignment")
    stock_lot_id = f"assigned-stock-{assignment['event_id']}"
    execute_manual_assigned_stock_sale(
        repo,
        target_stock_lot_id=stock_lot_id,
        shares=100,
        price=105.0,
        trade_time_ms=_ms("2026-07-23T10:00:00"),
        dry_run=False,
    )

    assignment_conversions = assignment["raw_payload"]["cash_conversions"]
    sale_conversions = repo.list_assigned_stock_events()[0]["cash_conversions"]
    assert assignment_conversions["stock_settlement_cash_gross"]["amount_cny"] == "-72000"
    assert sale_conversions["assigned_stock_sale_cash_gross"]["amount_cny"] == "75600"


def _sale_fx_fixture(tmp_path: Path, monkeypatch, *, initialized: bool = True):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with monkeypatch.context() as patch:
        if not initialized:
            # Represent an existing ledger from before FX evidence persistence.
            patch.setattr(ledger_writer, "load_cash_fx_payload", lambda *_args, **_kwargs: None)
        persist_manual_open_event(repo, OpenPositionCommand(
            broker="富途", account="lx", symbol="NVDA", option_type="put", side="short",
            contracts=1, currency="USD", strike=100, multiplier=100,
            expiration_ymd="2026-08-21", premium_per_share=2.5,
            opened_at_ms=_ms("2026-07-23T08:00:00"),
        ))
        record_manual_assignment(repo, record_id=repo.list_position_lots()[0]["record_id"],
            contracts_to_close=1, stock_side="buy", stock_qty=100, stock_price=100,
            as_of_ms=_ms("2026-07-23T09:00:00"))
    assignment = next(row for row in repo.list_trade_events() if row["event_type"] == "assignment")
    monkeypatch.setattr("src.application.cash_conversion.utc_now_ms", lambda: NOW_MS)
    cache = tmp_path / "rate_cache.json"
    _write_sale_fx_cache(cache, "7.2")
    return repo, f"assigned-stock-{assignment['event_id']}", cache


def _write_sale_fx_cache(path: Path, rate: str) -> None:
    quote = "2026-07-23T02:00:00+00:00"
    path.write_text(json.dumps({
        "source": "tencent_quote", "rates": {"USDCNY": rate, "HKDCNY": "0.92"},
        "timestamp": quote, "quote_timestamps": {"USDCNY": quote, "HKDCNY": quote},
        "observed_at": "2026-07-23T02:00:01+00:00",
    }))


def _sale_request(repo, lot_id: str, *, broker: bool, dry_run: bool, identity: str = "sale-1", shares: int = 40):
    if not broker:
        return execute_manual_assigned_stock_sale(repo, target_stock_lot_id=lot_id,
            shares=shares, price=105, trade_time_ms=_ms("2026-07-23T11:00:00"),
            source_deal_id=identity, dry_run=dry_run)
    deal = normalize_trade_deal({
        "broker_account_ref": {"broker_account_id": "futu:REAL:123", "broker_id": "futu",
            "external_account_id": "123", "environment": "REAL", "account_label": "lx"},
        "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "market": "US", "currency": "USD"},
        "external_id_namespace": "futu.deal", "external_execution_id": identity,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{identity}",
        "side": "sell", "quantity": str(shares), "price": "105", "currency": "USD",
        "occurred_at_utc": "2026-07-23T03:00:00Z",
    })
    return execute_broker_assigned_stock_sale(repo, deal, dry_run=dry_run)


def _ledger_dump(repo) -> tuple[str, ...]:
    with sqlite3.connect(f"file:{repo.db_path}?mode=ro", uri=True) as conn:
        return tuple(conn.iterdump())


@pytest.mark.parametrize("broker", [False, True], ids=["manual", "broker"])
@pytest.mark.parametrize("initialized", [False, True], ids=["legacy-schema", "current-schema"])
def test_stock_sale_preview_and_invalid_request_do_not_write_fx(tmp_path, monkeypatch, broker, initialized) -> None:
    repo, lot_id, _cache = _sale_fx_fixture(tmp_path, monkeypatch, initialized=initialized)
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    assert evidence.schema_state() == ("initialized_v1" if initialized else "not_initialized")
    before = _ledger_dump(repo)

    preview = _sale_request(repo, lot_id, broker=broker, dry_run=True)
    assert preview["mode"] == "dry_run"
    assert preview["sale_event"]["cash_conversions"]["assigned_stock_sale_cash_gross"]["status"] == "pending"
    assert _ledger_dump(repo) == before
    for dry_run in (True, False):
        with pytest.raises(ValueError):
            _sale_request(repo, lot_id, broker=broker, dry_run=dry_run, shares=101)
        assert _ledger_dump(repo) == before


@pytest.mark.parametrize("broker", [False, True], ids=["manual", "broker"])
def test_stock_sale_apply_fixes_daily_fx_in_transaction_and_returns_stored_winner(tmp_path, monkeypatch, broker) -> None:
    repo, lot_id, cache = _sale_fx_fixture(tmp_path, monkeypatch)
    preview = _sale_request(repo, lot_id, broker=broker, dry_run=True)
    assert preview["sale_event"]["cash_conversions"]["assigned_stock_sale_cash_gross"]["status"] == "pending"
    _write_sale_fx_cache(cache, "7.3")
    from src.application.ledger import writer_lifecycle_evidence as stock_writer

    actual_load = stock_writer.load_cash_fx_payload
    connections = []

    def transactional_load(candidate, *, conn):
        assert conn.in_transaction
        connections.append(conn)
        return actual_load(candidate, conn=conn)

    monkeypatch.setattr(stock_writer, "load_cash_fx_payload", transactional_load)
    applied = _sale_request(repo, lot_id, broker=broker, dry_run=False)
    stored = repo.list_assigned_stock_events()[0]
    assert applied["sale_event"] == stored
    assert stored["cash_conversions"]["assigned_stock_sale_cash_gross"]["fx_rate"] == "7.3"
    assert len(connections) == 1
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    first_rates = evidence.read_all().fx_rates
    assert len(first_rates) == 2

    _write_sale_fx_cache(cache, "7.8")
    before = _ledger_dump(repo)
    replay = _sale_request(repo, lot_id, broker=broker, dry_run=False)
    assert replay["idempotent_duplicate"] is True
    assert replay["sale_event"] == stored
    assert len(connections) == 1
    assert _ledger_dump(repo) == before
    preview = _sale_request(repo, lot_id, broker=broker, dry_run=True, identity="sale-2")
    assert preview["sale_event"]["cash_conversions"]["assigned_stock_sale_cash_gross"]["fx_rate"] == "7.3"
    assert _ledger_dump(repo) == before
    second = _sale_request(repo, lot_id, broker=broker, dry_run=False, identity="sale-2")
    assert second["sale_event"]["cash_conversions"]["assigned_stock_sale_cash_gross"]["fx_rate"] == "7.3"
    assert evidence.read_all().fx_rates == first_rates


@pytest.mark.parametrize("broker", [False, True], ids=["manual", "broker"])
def test_stock_sale_failed_commit_rolls_back_daily_fx(tmp_path, monkeypatch, broker) -> None:
    repo, lot_id, _cache = _sale_fx_fixture(tmp_path, monkeypatch, initialized=False)
    from src.application.ledger import writer_lifecycle_evidence as stock_writer

    before = _ledger_dump(repo)
    with monkeypatch.context() as patch:
        def fail_publication(*_args, **_kwargs):
            raise RuntimeError("injected stock publication failure")

        patch.setattr(stock_writer, "finalize_current_decision_projection", fail_publication)
        with pytest.raises(RuntimeError, match="injected stock publication failure"):
            _sale_request(repo, lot_id, broker=broker, dry_run=False)
    assert _ledger_dump(repo) == before
    assert PerformanceEvidenceSQLiteRepository(repo.db_path).schema_state() == "not_initialized"
    applied = _sale_request(repo, lot_id, broker=broker, dry_run=False)
    assert applied["sale_event"] == repo.list_assigned_stock_events()[0]
    assert len(PerformanceEvidenceSQLiteRepository(repo.db_path).read_all().fx_rates) == 2
