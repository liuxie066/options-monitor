from __future__ import annotations

import pytest

from src.infrastructure.xueqiu_client import (
    canonicalize_xueqiu_stock_symbol,
    extract_holdings_from_cube_payload,
    extract_user_id_from_url,
    extract_user_stocks_from_portfolio_payload,
    normalize_cube_symbol,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NASDAQ:NVDA", "NVDA"),
        ("US.NVDA", "NVDA"),
        ("USNVDA", "NVDA"),
        ("SH:600519", "600519.SS"),
        ("SZ:000001", "000001.SZ"),
        ("HK:00700", "0700.HK"),
        ("HK00700", "0700.HK"),
        ("HK09992", "9992.HK"),
        ("SH600519", "600519.SS"),
        ("SZ000001", "000001.SZ"),
    ],
)
def test_canonicalize_xueqiu_stock_symbol(raw: str, expected: str) -> None:
    assert canonicalize_xueqiu_stock_symbol(raw) == expected


def test_normalize_cube_symbol_rejects_url_like_input() -> None:
    with pytest.raises(ValueError):
        normalize_cube_symbol("https://xueqiu.com/P/ZH123456")


def test_extract_user_id_from_xueqiu_stock_url() -> None:
    assert extract_user_id_from_url("https://xueqiu.com/u/1247347556#/stock") == "1247347556"
    assert extract_user_id_from_url("1247347556") == "1247347556"


def test_extract_user_stocks_from_portfolio_payload() -> None:
    payload = {
        "data": {
            "stocks": [
                {
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "exchange": "SH",
                    "marketplace": "CN",
                    "current": "1720.5",
                    "percent": -1.2,
                    "created": "2026-08-16T09:00:00Z",
                },
                {
                    "symbol": "NVDA",
                    "name": "NVIDIA",
                    "exchange": "NASDAQ",
                    "marketplace": "US",
                },
            ]
        }
    }

    rows = extract_user_stocks_from_portfolio_payload(payload, user_id="1247347556")

    assert [row.to_dict() for row in rows] == [
        {
            "source_user_id": "1247347556",
            "raw_symbol": "600519",
            "symbol": "600519.SS",
            "name": "贵州茅台",
            "exchange": "SH",
            "marketplace": "CN",
            "current": 1720.5,
            "change_percent": -1.2,
            "created_at": "2026-08-16T09:00:00Z",
        },
        {
            "source_user_id": "1247347556",
            "raw_symbol": "NVDA",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "exchange": "NASDAQ",
            "marketplace": "US",
            "current": None,
            "change_percent": None,
            "created_at": None,
        },
    ]


def test_extract_holdings_from_cube_payload_supports_current_rebalancing_shape() -> None:
    payload = {
        "view_rebalancing": {
            "holdings": [
                {"stock_symbol": "NASDAQ:NVDA", "stock_name": "NVIDIA", "weight": 25.5},
                {"stock_symbol": "HK00700", "stock_name": "Tencent", "weight": "12.3"},
                {"stock_symbol": "", "stock_name": "Missing"},
            ]
        }
    }

    rows = extract_holdings_from_cube_payload(payload, cube_symbol="zh123456")

    assert [row.to_dict() for row in rows] == [
        {
            "source_cube": "ZH123456",
            "raw_symbol": "NASDAQ:NVDA",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "weight": 25.5,
        },
        {
            "source_cube": "ZH123456",
            "raw_symbol": "HK00700",
            "symbol": "0700.HK",
            "name": "Tencent",
            "weight": 12.3,
        },
    ]


def test_extract_holdings_from_cube_payload_supports_last_rebalance_shape() -> None:
    payload = {
        "last_rb": {
            "holdings": [
                {"symbol": "SZ000001", "name": "Ping An Bank", "weight": None},
            ]
        }
    }

    rows = extract_holdings_from_cube_payload(payload, cube_symbol="ZHABC")

    assert rows[0].symbol == "000001.SZ"
    assert rows[0].weight is None
