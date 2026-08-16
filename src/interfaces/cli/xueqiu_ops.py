from __future__ import annotations

import argparse
from typing import Any, Callable

from src.infrastructure.xueqiu_client import (
    DEFAULT_COOKIE_ENV,
    XueqiuHolding,
    XueqiuUserStock,
    extract_user_id_from_url,
    fetch_cube_holdings,
    fetch_user_stocks,
    normalize_cube_symbol,
)


FetchCubeHoldings = Callable[..., list[XueqiuHolding]]
FetchUserStocks = Callable[..., list[XueqiuUserStock]]


def add_xueqiu_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("xueqiu", help="inspect Xueqiu public portfolio signals")
    commands = parser.add_subparsers(dest="xueqiu_command", required=True)

    holdings = commands.add_parser("holdings", help="fetch current holdings from one or more Xueqiu cubes")
    holdings.add_argument("--cube", action="append", required=True, help="Xueqiu cube symbol, for example ZH123456")
    holdings.add_argument("--top", type=int, default=0, help="limit rows per cube after sorting by weight")
    holdings.add_argument(
        "--cookie-env",
        default=DEFAULT_COOKIE_ENV,
        help="environment variable containing an optional Xueqiu browser cookie",
    )
    holdings.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")

    user_stocks = commands.add_parser("user-stocks", help="fetch stocks from a Xueqiu blogger stock page")
    user_stocks.add_argument("--user-url", action="append", default=[], help="Xueqiu blogger URL, for example https://xueqiu.com/u/1247347556#/stock")
    user_stocks.add_argument("--uid", action="append", default=[], help="Xueqiu numeric user id")
    user_stocks.add_argument("--top", type=int, default=0, help="limit rows per blogger")
    user_stocks.add_argument(
        "--cookie-env",
        default=DEFAULT_COOKIE_ENV,
        help="environment variable containing an optional Xueqiu browser cookie",
    )
    user_stocks.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")


def handle_xueqiu_command(
    args: argparse.Namespace,
    *,
    fetch_cube_holdings_fn: FetchCubeHoldings = fetch_cube_holdings,
    fetch_user_stocks_fn: FetchUserStocks = fetch_user_stocks,
) -> dict[str, Any]:
    if args.xueqiu_command == "user-stocks":
        return _handle_user_stocks(args, fetch_user_stocks_fn=fetch_user_stocks_fn)
    if args.xueqiu_command != "holdings":
        return {"ok": False, "error": f"unsupported xueqiu command: {args.xueqiu_command}"}

    cubes = [normalize_cube_symbol(item) for item in (args.cube or [])]
    top = max(0, int(args.top or 0))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for cube in cubes:
        try:
            holdings = fetch_cube_holdings_fn(
                cube,
                cookie_env=str(args.cookie_env or DEFAULT_COOKIE_ENV),
                timeout=float(args.timeout or 10.0),
            )
        except Exception as exc:
            errors.append({"cube": cube, "error": str(exc)})
            continue
        rows = sorted(
            (holding.to_dict() for holding in holdings),
            key=lambda item: item.get("weight") if item.get("weight") is not None else -1.0,
            reverse=True,
        )
        if top:
            rows = rows[:top]
        results.append({"cube": cube, "holding_count": len(holdings), "holdings": rows})

    return {
        "ok": not errors,
        "schema_version": "xueqiu_holdings.v1",
        "cubes": results,
        "errors": errors,
    }


def _handle_user_stocks(
    args: argparse.Namespace,
    *,
    fetch_user_stocks_fn: FetchUserStocks,
) -> dict[str, Any]:
    raw_targets = list(args.user_url or []) + list(args.uid or [])
    if not raw_targets:
        return {
            "ok": False,
            "schema_version": "xueqiu_user_stocks.v1",
            "users": [],
            "errors": [{"error": "provide at least one --user-url or --uid"}],
        }

    top = max(0, int(args.top or 0))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for target in raw_targets:
        try:
            user_id = extract_user_id_from_url(target)
            stocks = fetch_user_stocks_fn(
                target,
                cookie_env=str(args.cookie_env or DEFAULT_COOKIE_ENV),
                timeout=float(args.timeout or 10.0),
            )
        except Exception as exc:
            errors.append({"target": str(target), "error": str(exc)})
            continue
        rows = [stock.to_dict() for stock in stocks]
        if top:
            rows = rows[:top]
        results.append({"user_id": user_id, "stock_count": len(stocks), "stocks": rows})

    return {
        "ok": not errors,
        "schema_version": "xueqiu_user_stocks.v1",
        "users": results,
        "errors": errors,
    }


__all__ = ["add_xueqiu_commands", "handle_xueqiu_command"]
