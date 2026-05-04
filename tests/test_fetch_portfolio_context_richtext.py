from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.fetch_portfolio_context import build_context


def test_build_context_requires_broker_field_and_normalizes_hk_symbol() -> None:
    records = [
        {
            "fields": {
                "broker": [{"text": "富途", "type": "text"}],
                "account": [{"text": " LX ", "type": "text"}],
                "asset_type": "hk_stock",
                "asset_id": [{"text": "00700", "type": "text"}],
                "asset_name": [{"text": "腾讯控股", "type": "text"}],
                "currency": "港币",
                "quantity": 500,
                "avg_cost": 503.916,
                "asset_class": "港股资产",
            }
        },
        {
            "fields": {
                "broker": [{"text": "富途", "type": "text"}],
                "account": [{"text": "lx", "type": "text"}],
                "asset_type": "cash",
                "asset_id": [{"text": "CNY-CASH", "type": "text"}],
                "asset_name": [{"text": "账户余额", "type": "text"}],
                "currency": "rmb",
                "quantity": 406.24,
                "asset_class": "现金",
            }
        },
        {
            "fields": {
                "broker": [{"text": "富途", "type": "text"}],
                "account": [{"text": "lx", "type": "text"}],
                "asset_type": "us_stock",
                "asset_id": [{"text": "NVDA", "type": "text"}],
                "asset_name": [{"text": "英伟达", "type": "text"}],
                "currency": "USD",
                "quantity": 160,
                "avg_cost": 164.959,
                "asset_class": "美国资产",
            }
        },
    ]

    ctx = build_context(records, market="富途", account="lx")

    assert ctx["raw_selected_count"] == 3
    assert ctx["cash_by_currency"]["CNY"] == 406.24

    stocks = ctx["stocks_by_symbol"]
    assert "0700.HK" in stocks
    assert stocks["0700.HK"]["shares"] == 500
    assert stocks["0700.HK"]["currency"] == "HKD"
    assert stocks["0700.HK"]["account"] == "lx"

    assert "NVDA" in stocks
    assert stocks["NVDA"]["shares"] == 160


def test_build_context_accepts_legacy_market_only_holdings_rows() -> None:
    records = [
        {
            "fields": {
                "market": [{"text": "富途", "type": "text"}],
                "account": [{"text": "lx", "type": "text"}],
                "asset_type": "cash",
                "asset_id": [{"text": "USD-CASH", "type": "text"}],
                "currency": "USD",
                "quantity": 100,
            }
        }
    ]

    ctx = build_context(records, broker="富途", account="lx")

    assert ctx["raw_selected_count"] == 1
    assert ctx["cash_by_currency"] == {"USD": 100.0}
    assert ctx["stocks_by_symbol"] == {}


def test_build_context_accepts_broker_field_without_market() -> None:
    records = [
        {
            "fields": {
                "broker": "富途",
                "account": "lx",
                "asset_type": "cash",
                "asset_id": "USD-CASH",
                "currency": "USD",
                "quantity": "123.45",
            }
        },
        {
            "fields": {
                "broker": "富途",
                "account": "lx",
                "asset_type": "us_stock",
                "asset_id": "AAPL",
                "asset_name": "Apple",
                "currency": "USD",
                "quantity": "20",
                "avg_cost": "150",
            }
        },
        {
            "fields": {
                "broker": "其他券商",
                "account": "lx",
                "asset_type": "cash",
                "asset_id": "USD-CASH",
                "currency": "USD",
                "quantity": "999",
            }
        },
    ]

    ctx = build_context(records, broker="富途", account="lx")

    assert ctx["filters"]["broker"] == "富途"
    assert "market" not in ctx["filters"]
    assert ctx["raw_selected_count"] == 2
    assert ctx["cash_by_currency"]["USD"] == 123.45
    assert ctx["stocks_by_symbol"]["AAPL"]["broker"] == "富途"
