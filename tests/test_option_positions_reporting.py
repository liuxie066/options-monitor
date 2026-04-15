from __future__ import annotations

from scripts.option_positions_core.domain import BUY_TO_CLOSE, EXPIRE_AUTO_CLOSE, parse_exp_to_ms
from scripts.option_positions_core.reporting import build_income_row, build_monthly_income_report


def _ms(date: str) -> int:
    out = parse_exp_to_ms(date)
    assert out is not None
    return out


def test_build_income_row_for_buy_to_close() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途证券（香港）",
                "account": "LX",
                "symbol": "0700.HK",
                "status": "close",
                "contracts": 2,
                "contracts_closed": 2,
                "currency": "港币",
                "premium": 3.93,
                "close_price": 1.2,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-04-20"),
                "note": "multiplier=100",
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.month == "2026-04"
    assert row.account == "lx"
    assert row.broker == "富途"
    assert row.currency == "HKD"
    assert row.realized_gross == 546.0


def test_build_income_row_for_expire_auto_close_uses_zero_close_price() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_2",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "close_type": EXPIRE_AUTO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.month == "2026-05"
    assert row.close_price == 0.0
    assert row.multiplier == 100
    assert row.realized_gross == 250.0


def test_build_income_row_warns_when_buy_close_missing_close_price() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_3",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        }
    )

    assert row is None
    assert warning == "rec_3: missing close_price"


def test_build_monthly_income_report_groups_by_month_account_currency() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "0700.HK",
                "status": "close",
                "contracts": 2,
                "contracts_closed": 2,
                "currency": "HKD",
                "premium": 3.93,
                "close_price": 1.2,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-04-20"),
                "note": "multiplier=100",
            },
        },
        {
            "record_id": "rec_2",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "close_type": EXPIRE_AUTO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        },
    ]

    report = build_monthly_income_report(records, account="lx", broker="富途", month="2026-04")

    assert report["warnings"] == []
    assert len(report["rows"]) == 1
    assert report["summary"] == [
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "HKD",
            "realized_gross": 546.0,
            "closed_contracts": 2,
            "positions": 1,
        }
    ]
