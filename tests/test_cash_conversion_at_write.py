from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
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
from src.application.ledger import writer as ledger_writer
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.performance.service import build_option_period_performance
from src.application.positions.workflows import execute_manual_assigned_stock_sale


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


def _report(repo: SQLiteOptionPositionsRepository) -> dict:
    return build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-07"},
        account="lx",
        now_ms=NOW_MS,
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
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo: next(fx_payloads))
    monkeypatch.setattr(ledger_writer, "utc_now_ms", lambda: next(observed_times))

    first = persist_trade_event_object(repo, event)
    duplicate = persist_trade_event_object(repo, event)

    assert first.created is True
    assert duplicate.created is False
    stored = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert stored["option_trade_cash_gross"]["fx_rate"] == "7.2"
    assert stored["option_trade_cash_gross"]["amount_cny"] == "1440"
    assert stored["option_fee_cash"]["amount_cny"] == "-18.16776"
    report = _report(repo)
    assert report["cash"]["option_trade_cash_gross"]["cny"] == 1440.0
    assert report["cash"]["option_fee_cash"]["cny"] == -18.16776
    assert report["cash"]["total_cash_change_net"]["cny"] == 1421.83224


def test_missing_fx_is_pending_but_zero_cash_needs_no_rate(tmp_path: Path, monkeypatch) -> None:
    pending_repo = SQLiteOptionPositionsRepository(tmp_path / "pending.sqlite3")
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo: {})
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
    pending_metric = _report(pending_repo)["cash"]["option_trade_cash_gross"]
    assert pending_metric["cny"] is None
    assert pending_metric["fx_fact_ids"] == [pending["conversion_id"]]
    assert pending_metric["missing"] == [
        "cash_conversion_pending:option_trade_cash_gross:pending:USDCNY booking FX unavailable"
    ]
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
        ("method", "unknown", "fx_provenance_invalid"),
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


def test_performance_keeps_native_cash_but_rejects_forged_cny(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "forged.sqlite3")
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
    repo.upsert_trade_event(
        replace(
            event,
            raw_payload={
                **event.raw_payload,
                "cash_conversions": {
                    "option_trade_cash_gross": conversion,
                },
            },
        )
    )

    metric = _report(repo)["cash"]["option_trade_cash_gross"]

    assert metric["by_currency"] == {"USD": 200.0}
    assert metric["cny"] is None
    assert metric["status"] == "partial"
    assert metric["missing"] == [
        "cash_conversion_corrupt:option_trade_cash_gross:forged:fx_arithmetic_mismatch"
    ]


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
    monkeypatch.setattr(ledger_writer, "load_cash_fx_payload", lambda _repo: fx_payload)
    monkeypatch.setattr("src.application.positions.workflows.load_cash_fx_payload", lambda _repo: fx_payload)
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
    report = _report(repo)
    assert report["cash"]["stock_settlement_cash_gross"]["cny"] == -72000.0
    assert report["cash"]["assigned_stock_sale_cash_gross"]["cny"] == 75600.0
