from __future__ import annotations

from src.infrastructure.xueqiu_client import XueqiuHolding, XueqiuUserStock
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.xueqiu_ops import handle_xueqiu_command


def test_xueqiu_holdings_cli_sorts_and_limits_rows() -> None:
    args = parse_args(["xueqiu", "holdings", "--cube", "zh123456", "--top", "1"])

    def _fetch(cube: str, **_kwargs) -> list[XueqiuHolding]:
        assert cube == "ZH123456"
        return [
            XueqiuHolding(source_cube=cube, raw_symbol="HK00700", symbol="0700.HK", name="Tencent", weight=12.0),
            XueqiuHolding(source_cube=cube, raw_symbol="NASDAQ:NVDA", symbol="NVDA", name="NVIDIA", weight=25.0),
        ]

    payload = handle_xueqiu_command(args, fetch_cube_holdings_fn=_fetch)

    assert payload["ok"] is True
    assert payload["schema_version"] == "xueqiu_holdings.v1"
    assert payload["cubes"][0]["holding_count"] == 2
    assert payload["cubes"][0]["holdings"] == [
        {
            "source_cube": "ZH123456",
            "raw_symbol": "NASDAQ:NVDA",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "weight": 25.0,
        }
    ]


def test_xueqiu_holdings_cli_reports_per_cube_errors() -> None:
    args = parse_args(["xueqiu", "holdings", "--cube", "ZH123456"])

    def _fetch(_cube: str, **_kwargs) -> list[XueqiuHolding]:
        raise RuntimeError("rate limited")

    payload = handle_xueqiu_command(args, fetch_cube_holdings_fn=_fetch)

    assert payload["ok"] is False
    assert payload["errors"] == [{"cube": "ZH123456", "error": "rate limited"}]


def test_xueqiu_user_stocks_cli_accepts_blogger_stock_url() -> None:
    args = parse_args(
        [
            "xueqiu",
            "user-stocks",
            "--user-url",
            "https://xueqiu.com/u/1247347556#/stock",
            "--top",
            "1",
        ]
    )

    def _fetch(target: str, **_kwargs) -> list[XueqiuUserStock]:
        assert target == "https://xueqiu.com/u/1247347556#/stock"
        return [
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="NVDA",
                symbol="NVDA",
                name="NVIDIA",
                exchange="NASDAQ",
                marketplace="US",
            ),
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="HK00700",
                symbol="0700.HK",
                name="Tencent",
                exchange="HK",
                marketplace="HK",
            ),
        ]

    payload = handle_xueqiu_command(args, fetch_user_stocks_fn=_fetch)

    assert payload["ok"] is True
    assert payload["schema_version"] == "xueqiu_user_stocks.v1"
    assert payload["users"][0]["user_id"] == "1247347556"
    assert payload["users"][0]["stock_count"] == 2
    assert payload["users"][0]["stocks"] == [
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
        }
    ]
