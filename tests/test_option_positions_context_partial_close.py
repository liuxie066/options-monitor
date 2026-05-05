from __future__ import annotations

from pathlib import Path
import sys
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from domain.domain.expiration_dates import expiration_business_today
from scripts.fetch_option_positions_context import build_context, build_shared_context
from src.application.option_positions_facade import load_option_position_records, list_position_rows


def test_build_context_preserves_record_id_without_position_id() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途证券（香港）",
                "account": "LX",
                "symbol": "NVDA",
                "status": "OPEN",
                "side": "Sell To Open",
                "option_type": "认沽",
                "contracts": 1,
                "contracts_open": 1,
                "cash_secured_amount": 1000,
                "currency": "美元",
                "premium": 1.23,
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    assert ctx["open_positions_min"][0]["record_id"] == "rec_1"
    assert ctx["open_positions_min"][0]["position_id"] is None
    assert ctx["open_positions_min"][0]["broker"] == "富途"
    assert ctx["open_positions_min"][0]["account"] == "lx"
    assert ctx["open_positions_min"][0]["option_type"] == "put"
    assert ctx["open_positions_min"][0]["side"] == "short"
    assert ctx["open_positions_min"][0]["currency"] == "USD"
    assert ctx["open_positions_min"][0]["premium"] == 1.23
    assert ctx["open_positions_min"][0]["expiration_ymd"] is None
    assert ctx["open_positions_min"][0]["days_to_expiration"] is None


def test_build_context_reads_premium_from_note_fallback() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "contracts_open": 1,
                "cash_secured_amount": 1000,
                "currency": "USD",
                "note": "premium_per_share=0.88",
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    assert ctx["open_positions_min"][0]["premium"] == "0.88"


def test_build_context_exposes_expiration_ymd_and_days_to_expiration() -> None:
    expiration_ms = int(datetime(2026, 5, 3, tzinfo=timezone.utc).timestamp() * 1000)
    as_of_days = (datetime(2026, 5, 3, tzinfo=timezone.utc).date() - expiration_business_today()).days
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "contracts_open": 1,
                "cash_secured_amount": 1000,
                "currency": "USD",
                "strike": 120.0,
                "multiplier": 100,
                "expiration": expiration_ms,
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    row = ctx["open_positions_min"][0]
    assert row["expiration"] == expiration_ms
    assert row["expiration_ymd"] == "2026-05-03"
    assert row["days_to_expiration"] == as_of_days
    assert row["strike"] == 120.0
    assert row["multiplier"] == 100


def test_build_context_requires_broker_on_persisted_rows() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "market": "富途证券（香港）",
                "account": "lx",
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "contracts_open": 1,
                "cash_secured_amount": 1000,
                "currency": "USD",
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    assert ctx["raw_selected_count"] == 0
    assert ctx["open_positions_min"] == []


def test_build_shared_context_requires_broker_on_persisted_rows() -> None:
    shared = build_shared_context(
        [
            {
                "record_id": "rec_1",
                "fields": {
                    "market": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "status": "open",
                    "side": "short",
                    "option_type": "call",
                    "contracts": 1,
                    "contracts_open": 1,
                    "underlying_share_locked": 100,
                },
            }
        ],
        broker="富途",
    )

    assert shared["all_accounts"]["raw_selected_count"] == 0
    assert shared["by_account"] == {}


def test_build_context_scales_cash_secured_for_partial_close() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 4,
                "contracts_open": 1,
                "contracts_closed": 3,
                "cash_secured_amount": 4000,
                "currency": "USD",
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    assert ctx["cash_secured_by_symbol_by_ccy"]["NVDA"]["USD"] == 1000.0
    assert ctx["cash_secured_total_by_ccy"]["USD"] == 1000.0
    assert ctx["cash_secured_total_cny"] == 7200.0
    assert ctx["open_positions_min"][0]["contracts_open"] == 1
    assert ctx["open_positions_min"][0]["contracts_closed"] == 3


def test_build_context_scales_locked_shares_for_partial_close() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "AAPL",
                "status": "open",
                "side": "short",
                "option_type": "call",
                "contracts": 3,
                "contracts_open": 2,
                "contracts_closed": 1,
                "underlying_share_locked": 300,
            },
        }
    ]

    ctx = build_context(records, broker="富途", account="sy")

    assert ctx["locked_shares_by_symbol"]["AAPL"] == 200


def test_build_context_excludes_closed_or_zero_open_records() -> None:
    records = [
        {
            "record_id": "closed",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "close",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "cash_secured_amount": 1000,
                "currency": "USD",
            },
        },
        {
            "record_id": "zero",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "AAPL",
                "status": "open",
                "side": "short",
                "option_type": "call",
                "contracts": 1,
                "contracts_open": 0,
                "contracts_closed": 1,
                "underlying_share_locked": 100,
            },
        },
    ]

    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7.2})

    assert ctx["open_positions_min"] == []
    assert ctx["cash_secured_by_symbol_by_ccy"] == {}
    assert ctx["locked_shares_by_symbol"] == {}


def test_load_option_position_records_prefers_position_lots_when_available() -> None:
    class _PrimaryRepo:
        def list_position_lots(self) -> list[dict]:
            return [{"record_id": "lot_1", "fields": {"symbol": "NVDA"}}]

    class _Repo:
        primary_repo = _PrimaryRepo()

        def list_records(self, *, page_size: int = 500) -> list[dict]:
            return [{"record_id": "legacy_1", "fields": {"symbol": "AAPL"}}]

    rows = load_option_position_records(_Repo())

    assert rows == [{"record_id": "lot_1", "fields": {"symbol": "NVDA"}}]


def test_load_option_position_records_falls_back_to_legacy_records_when_projection_empty() -> None:
    class _PrimaryRepo:
        def list_position_lots(self) -> list[dict]:
            return []

    class _Repo:
        primary_repo = _PrimaryRepo()

        def list_records(self, *, page_size: int = 500) -> list[dict]:
            return [{"record_id": "legacy_1", "fields": {"symbol": "AAPL"}}]

    rows = load_option_position_records(_Repo())

    assert rows == [{"record_id": "legacy_1", "fields": {"symbol": "AAPL"}}]


def test_list_position_rows_requires_broker_on_persisted_rows() -> None:
    class _Repo:
        def list_records(self, *, page_size: int = 500) -> list[dict]:
            return [
                {"record_id": "legacy_1", "fields": {"market": "富途", "account": "lx", "symbol": "AAPL", "status": "open"}},
                {"record_id": "lot_1", "fields": {"broker": "富途", "account": "lx", "symbol": "NVDA", "status": "open"}},
            ]

    rows = list_position_rows(_Repo(), broker="富途", account="lx", status="open", limit=10)

    assert [row["record_id"] for row in rows] == ["lot_1"]
    assert rows[0]["broker"] == "富途"
