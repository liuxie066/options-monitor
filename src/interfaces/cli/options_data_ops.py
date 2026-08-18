from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Any, Callable

from src.application.sell_put_underwriting import (
    SellPutCandidate,
    SellPutPolicy,
    evaluate_sell_put_candidate,
    parse_target_basis,
)
from src.application.settings import build_effective_env
from src.infrastructure.robinhood_options import (
    ROBINHOOD_TOKEN_ENV,
    RobinhoodOptionChainRequest,
    RobinhoodOptionsError,
    fetch_robinhood_option_chain,
    fetch_robinhood_stock_quotes,
)
from src.infrastructure.stockvoice_client import (
    STOCKVOICE_URL,
    StockVoiceSignal,
    fetch_stockvoice_signals,
)
from src.infrastructure.xueqiu_client import (
    DEFAULT_COOKIE_ENV,
    XueqiuUserStock,
    extract_user_id_from_url,
    fetch_user_stocks,
)


FetchOptionChain = Callable[[RobinhoodOptionChainRequest], list[dict[str, Any]]]
FetchStockQuotes = Callable[..., dict[str, dict[str, Any]]]
FetchUserStocks = Callable[..., list[XueqiuUserStock]]
FetchStockVoiceSignals = Callable[..., list[StockVoiceSignal]]


def add_options_data_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("options-data", help="inspect non-broker options market data providers")
    commands = parser.add_subparsers(dest="options_data_command", required=True)

    chain = commands.add_parser("chain", help="fetch an option chain from a non-broker data provider")
    chain.add_argument("--provider", choices=("robinhood",), default="robinhood")
    chain.add_argument("--symbol", required=True, help="underlying symbol, for example NVDA")
    chain.add_argument("--expiration", default=None, help="expiration date YYYY-MM-DD")
    chain.add_argument("--side", choices=("call", "put"), default=None)
    chain.add_argument("--mode", default=None, help=argparse.SUPPRESS)
    chain.add_argument("--min-open-interest", type=int, default=None)
    chain.add_argument("--min-volume", type=int, default=None)
    chain.add_argument("--max-bid-ask-spread", type=float, default=None)
    chain.add_argument("--limit", type=int, default=20, help="limit returned rows; 0 returns all rows")
    chain.add_argument("--env-file", default=None)
    chain.add_argument("--no-local-env-file", action="store_true")
    chain.add_argument("--timeout", type=float, default=20.0)

    stockvoice = commands.add_parser(
        "stockvoice-signals",
        help="fetch public StockVoice bullish consensus signals",
    )
    stockvoice.add_argument("--url", default=STOCKVOICE_URL)
    stockvoice.add_argument("--min-bullish-count", type=int, default=8)
    stockvoice.add_argument("--min-bull-bear-ratio", type=float, default=2.0)
    stockvoice.add_argument("--min-net-bullish", type=int, default=3)
    stockvoice.add_argument("--limit", type=int, default=20)
    stockvoice.add_argument("--timeout", type=float, default=20.0)

    blogger = commands.add_parser(
        "blogger-opportunities",
        help="rank read-only option candidates from a Xueqiu blogger's US stock list",
    )
    blogger.add_argument("--user-url", required=True, help="Xueqiu blogger stock page URL")
    blogger.add_argument("--expiration", default=None, help="expiration date YYYY-MM-DD")
    blogger.add_argument("--symbols-limit", type=int, default=5, help="limit US stocks fetched from the blogger list")
    blogger.add_argument("--per-symbol-limit", type=int, default=0, help="limit candidates per symbol; 0 returns all evaluated rows")
    blogger.add_argument("--max-results", type=int, default=250, help="maximum yield-ranked rows returned; 0 returns all")
    blogger.add_argument("--portfolio-nav", type=float, default=None, help=argparse.SUPPRESS)
    blogger.add_argument("--target-basis", action="append", default=[], help="authorized Tier-A basis, for example AMZN=232 or SPCX=120")
    blogger.add_argument("--min-days-to-expiration", type=int, default=21)
    blogger.add_argument("--max-days-to-expiration", type=int, default=60)
    blogger.add_argument("--min-out-of-money-pct", type=float, default=0.15)
    blogger.add_argument("--max-abs-delta", type=float, default=0.30)
    blogger.add_argument("--min-iv", type=float, default=0.40, help="minimum implied volatility for high-volatility sell-put candidates")
    blogger.add_argument("--min-open-interest", type=int, default=50)
    blogger.add_argument("--min-volume", type=int, default=0)
    blogger.add_argument("--max-bid-ask-spread", type=float, default=None)
    blogger.add_argument("--max-bid-ask-spread-pct", type=float, default=0.20)
    blogger.add_argument("--max-single-name-pct-nav", type=float, default=0.15, help=argparse.SUPPRESS)
    blogger.add_argument("--max-stress-loss-pct-nav", type=float, default=0.05, help=argparse.SUPPRESS)
    blogger.add_argument("--env-file", default=None)
    blogger.add_argument("--no-local-env-file", action="store_true")
    blogger.add_argument("--timeout", type=float, default=20.0)
    blogger.add_argument("--include-stockvoice", action="store_true")
    blogger.add_argument("--stockvoice-url", default=STOCKVOICE_URL)
    blogger.add_argument("--stockvoice-limit", type=int, default=20)
    blogger.add_argument("--stockvoice-min-bullish-count", type=int, default=8)
    blogger.add_argument("--stockvoice-min-bull-bear-ratio", type=float, default=2.0)
    blogger.add_argument("--stockvoice-min-net-bullish", type=int, default=3)


def handle_options_data_command(
    args: argparse.Namespace,
    *,
    fetch_robinhood_option_chain_fn: FetchOptionChain = fetch_robinhood_option_chain,
    fetch_robinhood_stock_quotes_fn: FetchStockQuotes = fetch_robinhood_stock_quotes,
    fetch_user_stocks_fn: FetchUserStocks = fetch_user_stocks,
    fetch_stockvoice_signals_fn: FetchStockVoiceSignals = fetch_stockvoice_signals,
) -> dict[str, Any]:
    if args.options_data_command == "stockvoice-signals":
        return _handle_stockvoice_signals(args, fetch_stockvoice_signals_fn=fetch_stockvoice_signals_fn)
    if args.options_data_command == "blogger-opportunities":
        return _handle_blogger_opportunities(
            args,
            fetch_robinhood_option_chain_fn=fetch_robinhood_option_chain_fn,
            fetch_robinhood_stock_quotes_fn=fetch_robinhood_stock_quotes_fn,
            fetch_user_stocks_fn=fetch_user_stocks_fn,
            fetch_stockvoice_signals_fn=fetch_stockvoice_signals_fn,
        )
    if args.options_data_command != "chain":
        return {"ok": False, "error": f"unsupported options-data command: {args.options_data_command}"}
    if args.provider != "robinhood":
        return {"ok": False, "error": f"unsupported options data provider: {args.provider}"}

    effective = build_effective_env(
        env_file=getattr(args, "env_file", None),
        include_local_env_file=not bool(getattr(args, "no_local_env_file", False)),
    )
    token = effective.get(ROBINHOOD_TOKEN_ENV)
    try:
        rows = fetch_robinhood_option_chain_fn(
            RobinhoodOptionChainRequest(
                symbol=args.symbol,
                expiration=args.expiration,
                side=args.side,
                min_open_interest=args.min_open_interest,
                min_volume=args.min_volume,
                max_bid_ask_spread=args.max_bid_ask_spread,
                token=token or None,
                timeout=float(args.timeout or 20.0),
            )
        )
    except RobinhoodOptionsError as exc:
        return {
            "ok": False,
            "schema_version": "options_data_chain.v1",
            "provider": "robinhood",
            "symbol": str(args.symbol).upper(),
            "requested_mode": getattr(args, "mode", None),
            "token_configured": bool(token),
            "error": str(exc),
        }
    limit = max(0, int(args.limit or 0))
    limited_rows = rows[:limit] if limit else rows
    return {
        "ok": True,
        "schema_version": "options_data_chain.v1",
        "provider": "robinhood",
        "symbol": str(args.symbol).upper(),
        "requested_mode": getattr(args, "mode", None),
        "token_configured": bool(token),
        "row_count": len(rows),
        "rows": limited_rows,
        "notes": [
            "Robinhood is used as a read-only options quote source here; this command does not place trades.",
            "Robinhood's official developer API is crypto-focused, so stock and options web API endpoints may change without notice.",
        ],
    }


def _handle_blogger_opportunities(
    args: argparse.Namespace,
    *,
    fetch_robinhood_option_chain_fn: FetchOptionChain,
    fetch_robinhood_stock_quotes_fn: FetchStockQuotes,
    fetch_user_stocks_fn: FetchUserStocks,
    fetch_stockvoice_signals_fn: FetchStockVoiceSignals,
) -> dict[str, Any]:
    effective = build_effective_env(
        env_file=getattr(args, "env_file", None),
        include_local_env_file=not bool(getattr(args, "no_local_env_file", False)),
    )
    robinhood_token = effective.get(ROBINHOOD_TOKEN_ENV)
    xueqiu_cookie = effective.get(DEFAULT_COOKIE_ENV)
    portfolio_nav = _float_or_none(getattr(args, "portfolio_nav", None))
    user_url = str(args.user_url or "").strip()
    symbols_limit = max(1, int(args.symbols_limit or 5))
    per_symbol_limit = max(0, int(args.per_symbol_limit or 0))
    max_results = max(0, int(args.max_results or 0))
    try:
        policy = SellPutPolicy(
            target_basis_by_symbol=parse_target_basis(getattr(args, "target_basis", None)),
            min_days_to_expiration=max(0, int(args.min_days_to_expiration or 0)),
            max_days_to_expiration=max(0, int(args.max_days_to_expiration or 0)),
            min_out_of_money_pct=max(0.0, float(args.min_out_of_money_pct or 0.0)),
            max_abs_delta=max(0.0, float(args.max_abs_delta or 0.0)),
            min_iv=max(0.0, float(args.min_iv or 0.0)),
            min_open_interest=max(0, int(args.min_open_interest or 0)),
            min_volume=max(0, int(args.min_volume or 0)),
            max_bid_ask_spread_pct=max(0.0, float(args.max_bid_ask_spread_pct or 0.0)),
        )
    except ValueError as exc:
        return {
            "ok": False,
            "schema_version": "options_data_blogger_opportunities.v1",
            "provider": "robinhood",
            "user_url": user_url,
            "token_configured": bool(robinhood_token),
            "xueqiu_cookie_configured": bool(xueqiu_cookie),
            "error": str(exc),
        }
    try:
        user_id = extract_user_id_from_url(user_url)
        stocks = fetch_user_stocks_fn(
            user_url,
            cookie=xueqiu_cookie or None,
            timeout=float(args.timeout or 20.0),
        )
    except Exception as exc:
        return {
            "ok": False,
            "schema_version": "options_data_blogger_opportunities.v1",
            "provider": "robinhood",
            "user_url": user_url,
            "token_configured": bool(robinhood_token),
            "xueqiu_cookie_configured": bool(xueqiu_cookie),
            "error": str(exc),
        }

    errors: list[dict[str, str]] = []
    stockvoice_signals: list[StockVoiceSignal] = []
    if bool(getattr(args, "include_stockvoice", False)):
        try:
            stockvoice_signals = fetch_stockvoice_signals_fn(
                url=str(getattr(args, "stockvoice_url", STOCKVOICE_URL) or STOCKVOICE_URL),
                timeout=float(args.timeout or 20.0),
                min_bullish_count=max(0, int(getattr(args, "stockvoice_min_bullish_count", 8) or 0)),
                min_bull_bear_ratio=max(0.0, float(getattr(args, "stockvoice_min_bull_bear_ratio", 2.0) or 0.0)),
                min_net_bullish=max(0, int(getattr(args, "stockvoice_min_net_bullish", 3) or 0)),
                limit=max(0, int(getattr(args, "stockvoice_limit", 20) or 0)),
            )
        except Exception as exc:
            stockvoice_signals = []
            errors.append({"stage": "stockvoice_signals", "error": str(exc)})

    us_stocks = [stock for stock in stocks if _is_us_stock(stock)]
    stockvoice_stocks = [_stock_from_stockvoice_signal(signal) for signal in stockvoice_signals]
    selected_xueqiu_stocks = us_stocks[:symbols_limit]
    source_stocks = _dedupe_stocks([*selected_xueqiu_stocks, *stockvoice_stocks])
    selected_stocks = source_stocks
    stockvoice_by_symbol = {signal.symbol: signal for signal in stockvoice_signals}
    quotes: dict[str, dict[str, Any]] = {}
    try:
        quotes = fetch_robinhood_stock_quotes_fn(
            [stock.symbol for stock in selected_stocks],
            token=robinhood_token or None,
            timeout=float(args.timeout or 20.0),
        )
    except RobinhoodOptionsError as exc:
        errors.append({"stage": "stock_quotes", "error": str(exc)})
    opportunities: list[dict[str, Any]] = []
    for stock in selected_stocks:
        quote = quotes.get(stock.symbol, {})
        try:
            rows = fetch_robinhood_option_chain_fn(
                RobinhoodOptionChainRequest(
                    symbol=stock.symbol,
                    expiration=args.expiration,
                    side="put",
                    min_open_interest=None,
                    min_volume=None,
                    max_bid_ask_spread=None,
                    token=robinhood_token or None,
                    timeout=float(args.timeout or 20.0),
                )
            )
        except RobinhoodOptionsError as exc:
            errors.append({"symbol": stock.symbol, "strategy": "sell_put", "error": str(exc)})
            continue
        ranked = _rank_sell_put_rows(
            stock=stock,
            quote=quote,
            rows=rows,
            policy=policy,
            portfolio_nav=portfolio_nav,
            enforce_dte_scope=not bool(args.expiration),
            stockvoice_signal=stockvoice_by_symbol.get(stock.symbol),
        )
        opportunities.extend(ranked[:per_symbol_limit] if per_symbol_limit else ranked)
    sorted_opportunities = sorted(opportunities, key=_yield_sort_key)
    returned_opportunities = sorted_opportunities[:max_results] if max_results else sorted_opportunities

    return {
        "ok": not errors or bool(returned_opportunities),
        "partial_success": bool(errors and returned_opportunities),
        "schema_version": "options_data_blogger_opportunities.v1",
        "provider": "robinhood",
        "user_id": user_id,
        "user_url": user_url,
        "token_configured": bool(robinhood_token),
        "xueqiu_cookie_configured": bool(xueqiu_cookie),
        "source_stock_count": len(stocks),
        "us_stock_count": len(us_stocks),
        "stockvoice_signal_count": len(stockvoice_signals),
        "stockvoice_symbols": [signal.symbol for signal in stockvoice_signals],
        "stock_source_count": len(source_stocks),
        "selected_symbols": [stock.symbol for stock in selected_stocks],
        "quote_count": len(quotes),
        "cash_assumption": "unlimited_cash",
        "portfolio_nav_configured": portfolio_nav is not None,
        "policy": {
            "tier_a_target_basis": policy.target_basis_by_symbol,
            "index_symbols": sorted(policy.index_symbols),
            "min_days_to_expiration": policy.min_days_to_expiration,
            "max_days_to_expiration": policy.max_days_to_expiration,
            "min_out_of_money_pct": policy.min_out_of_money_pct,
            "max_abs_delta": policy.max_abs_delta,
            "min_iv": policy.min_iv,
            "max_bid_ask_spread_pct": policy.max_bid_ask_spread_pct,
            "stockvoice_min_bullish_count": max(0, int(getattr(args, "stockvoice_min_bullish_count", 8) or 0)),
            "stockvoice_min_bull_bear_ratio": max(0.0, float(getattr(args, "stockvoice_min_bull_bear_ratio", 2.0) or 0.0)),
            "stockvoice_min_net_bullish": max(0, int(getattr(args, "stockvoice_min_net_bullish", 3) or 0)),
        },
        "opportunity_count": len(returned_opportunities),
        "evaluated_count": len(opportunities),
        "returned_count": len(returned_opportunities),
        "truncated": len(returned_opportunities) < len(opportunities),
        "opportunities": returned_opportunities,
        "errors": errors,
        "notes": [
            "This is a read-only monitor preview, not a trade order.",
            "Sell-put candidates are evaluated as cash-secured acquisition underwriting, not as yield chasing.",
            "Unknown single-stock names are not eligible for cash-secured puts unless a target basis is explicitly authorized.",
            "Evaluated put contracts include GO and NO_GO rows inside the sell-put strategy universe and are ranked by annualized yield from high to low.",
            "The sell-put strategy universe excludes in-the-money and near-the-money puts; strikes must be at least the configured OTM distance below current price before guardrail scoring.",
            "When no expiration is specified, the scan is scoped to the strategy DTE window before ranking so unrelated expirations do not overwhelm the dashboard.",
            "Default hard guardrails enforce DTE, OTM distance, delta, liquidity, and target basis.",
            "Cash is assumed unlimited for this local strategy; assignment cash required and stress losses are shown in dollars instead of NAV percentages.",
            "Macro, VIX, Fear & Greed, earnings, and analyst data are not yet wired into this local backend, so the original framework remains unresolved when only those gates are missing.",
            "Missing upstream values remain null so they are easy to spot.",
            "When enabled, StockVoice is used only as a public bullish-consensus symbol source before option guardrails run.",
        ],
    }


def _handle_stockvoice_signals(
    args: argparse.Namespace,
    *,
    fetch_stockvoice_signals_fn: FetchStockVoiceSignals,
) -> dict[str, Any]:
    try:
        signals = fetch_stockvoice_signals_fn(
            url=str(args.url or STOCKVOICE_URL),
            timeout=float(args.timeout or 20.0),
            min_bullish_count=max(0, int(args.min_bullish_count or 0)),
            min_bull_bear_ratio=max(0.0, float(args.min_bull_bear_ratio or 0.0)),
            min_net_bullish=max(0, int(args.min_net_bullish or 0)),
            limit=max(0, int(args.limit or 0)),
        )
    except Exception as exc:
        return {
            "ok": False,
            "schema_version": "stockvoice_signals.v1",
            "provider": "stockvoice",
            "source_url": str(args.url or STOCKVOICE_URL),
            "error": str(exc),
        }
    return {
        "ok": True,
        "schema_version": "stockvoice_signals.v1",
        "provider": "stockvoice",
        "source_url": str(args.url or STOCKVOICE_URL),
        "signal_count": len(signals),
        "signals": [signal.to_dict() for signal in signals],
        "notes": [
            "StockVoice signals are parsed from the public website and used as advisory symbol-discovery input only.",
            "A bullish consensus signal does not bypass sell-put option guardrails.",
        ],
    }


def _is_us_stock(stock: XueqiuUserStock) -> bool:
    exchange = str(stock.exchange or "").upper()
    marketplace = str(stock.marketplace or "").upper()
    symbol = str(stock.symbol or "").upper()
    if exchange in {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "US"}:
        return True
    if marketplace == "US":
        return True
    return bool(symbol and "." not in symbol and symbol.isascii())


def _stock_from_stockvoice_signal(signal: StockVoiceSignal) -> XueqiuUserStock:
    return XueqiuUserStock(
        source_user_id="stockvoice",
        raw_symbol=signal.symbol,
        symbol=signal.symbol,
        name=signal.name,
        exchange="US",
        marketplace="US",
        current=signal.price,
    )


def _dedupe_stocks(stocks: list[XueqiuUserStock]) -> list[XueqiuUserStock]:
    out: list[XueqiuUserStock] = []
    seen: set[str] = set()
    for stock in stocks:
        symbol = str(stock.symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(stock)
    return out


def _rank_strategy_rows(
    *,
    stock: XueqiuUserStock,
    quote: dict[str, Any],
    strategy: str,
    rows: list[dict[str, Any]],
    min_days_to_expiration: int,
    min_out_of_money_pct: float,
    max_abs_delta: float | None,
) -> list[dict[str, Any]]:
    quote_midpoint = _midpoint(_float_or_none(quote.get("bid_price")), _float_or_none(quote.get("ask_price")))
    current, price_source = _current_price_with_source(
        robinhood_last=quote.get("last_trade_price"),
        robinhood_midpoint=quote_midpoint,
        source_current=stock.current,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        bid = _float_or_none(row.get("bid"))
        ask = _float_or_none(row.get("ask"))
        mid = _first_float(row.get("mid"), _midpoint(bid, ask), bid)
        strike = _float_or_none(row.get("strike"))
        if strike is None or mid is None or mid <= 0:
            continue
        dte = _days_to_expiration(row.get("expiration"))
        if dte is None or dte < min_days_to_expiration:
            continue
        otm_pct = _out_of_money_pct(strategy=strategy, strike=strike, current=current)
        if otm_pct is None or otm_pct < min_out_of_money_pct:
            continue
        delta = _float_or_none(row.get("delta"))
        if max_abs_delta is not None and delta is not None and abs(delta) > max_abs_delta:
            continue
        premium_pct = (mid / current) if current else None
        annualized_yield = (premium_pct * 365 / dte) if premium_pct is not None and dte and dte > 0 else None
        out.append(
            {
                "strategy": strategy,
                "symbol": stock.symbol,
                "name": stock.name,
                "source_current": _float_or_none(stock.current),
                "underlying_price": current,
                "underlying_price_source": price_source,
                "underlying_quote_updated_at": quote.get("updated_at"),
                "contract_symbol": row.get("contract_symbol"),
                "option_type": row.get("option_type"),
                "expiration": row.get("expiration"),
                "dte": dte,
                "strike": strike,
                "out_of_money_pct": _round_or_none(otm_pct, 6),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "bid_ask_spread": row.get("bid_ask_spread"),
                "premium_pct": _round_or_none(premium_pct, 6),
                "annualized_yield": _round_or_none(annualized_yield, 6),
                "volume": row.get("volume"),
                "open_interest": row.get("open_interest"),
                "iv": row.get("iv"),
                "delta": delta,
            }
        )
    return sorted(out, key=_opportunity_sort_key)


def _rank_sell_put_rows(
    *,
    stock: XueqiuUserStock,
    quote: dict[str, Any],
    rows: list[dict[str, Any]],
    policy: SellPutPolicy,
    portfolio_nav: float | None,
    enforce_dte_scope: bool = False,
    stockvoice_signal: StockVoiceSignal | None = None,
) -> list[dict[str, Any]]:
    quote_midpoint = _midpoint(_float_or_none(quote.get("bid_price")), _float_or_none(quote.get("ask_price")))
    current, price_source = _current_price_with_source(
        robinhood_last=quote.get("last_trade_price"),
        robinhood_midpoint=quote_midpoint,
        source_current=stock.current,
    )
    out: list[dict[str, Any]] = []
    excluded_count = 0
    for row in rows:
        dte = _days_to_expiration(row.get("expiration"))
        if enforce_dte_scope and (
            dte is None or dte < policy.min_days_to_expiration or dte > policy.max_days_to_expiration
        ):
            excluded_count += 1
            continue
        bid = _float_or_none(row.get("bid"))
        ask = _float_or_none(row.get("ask"))
        mid = _first_float(row.get("mid"), _midpoint(bid, ask), bid)
        strike = _float_or_none(row.get("strike"))
        if current is None or strike is None or strike >= current:
            excluded_count += 1
            continue
        otm_pct = (current - strike) / current if current > 0 else None
        if otm_pct is None or otm_pct < policy.min_out_of_money_pct:
            excluded_count += 1
            continue
        evaluated = evaluate_sell_put_candidate(
            SellPutCandidate(
                symbol=stock.symbol,
                name=stock.name,
                current_price=current,
                strike=strike,
                expiration=str(row.get("expiration") or "") or None,
                bid=bid,
                ask=ask,
                mid=mid,
                delta=_float_or_none(row.get("delta")),
                iv=_float_or_none(row.get("iv")),
                volume=_int_or_none(row.get("volume")),
                open_interest=_int_or_none(row.get("open_interest")),
                contract_symbol=str(row.get("contract_symbol") or "") or None,
                option_type=str(row.get("option_type") or "put") or "put",
            ),
            policy=policy,
            portfolio_nav=portfolio_nav,
        )
        evaluated["source_current"] = _float_or_none(stock.current)
        evaluated["underlying_price_source"] = price_source
        evaluated["underlying_quote_updated_at"] = quote.get("updated_at")
        if stockvoice_signal is not None:
            evaluated["stockvoice_signal"] = stockvoice_signal.to_dict()
        out.append(evaluated)
    out = sorted(out, key=_yield_sort_key)
    for item in out:
        item["excluded_outside_strategy_universe_count"] = excluded_count
    return out


def _yield_sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    score = _float_or_none(row.get("mature_score"))
    annualized = _float_or_none(row.get("annualized_return_on_cash"))
    premium = _float_or_none(row.get("premium_per_contract"))
    return (
        -(annualized if annualized is not None else -1.0),
        -(premium if premium is not None else -1.0),
        -(score if score is not None else -1.0),
        str(row.get("contract_symbol") or ""),
    )


def _opportunity_sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    annualized = _float_or_none(row.get("annualized_yield"))
    premium = _float_or_none(row.get("premium_pct"))
    open_interest = _float_or_none(row.get("open_interest"))
    return (
        -(annualized if annualized is not None else -1.0),
        -(premium if premium is not None else -1.0),
        -(open_interest if open_interest is not None else -1.0),
        str(row.get("contract_symbol") or ""),
    )


def _out_of_money_pct(*, strategy: str, strike: float, current: float | None) -> float | None:
    if current is None or current <= 0:
        return None
    if strategy == "sell_put":
        return (current - strike) / current
    if strategy == "covered_call":
        return (strike - current) / current
    return None


def _days_to_expiration(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        exp = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (exp - date.today()).days)


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2.0, 6)


def _current_price_with_source(
    *,
    robinhood_last: Any,
    robinhood_midpoint: float | None,
    source_current: Any,
) -> tuple[float | None, str | None]:
    robinhood_last_float = _float_or_none(robinhood_last)
    if robinhood_last_float is not None:
        return robinhood_last_float, "robinhood_quote"
    if robinhood_midpoint is not None:
        return robinhood_midpoint, "robinhood_bid_ask_midpoint"
    source_current_float = _float_or_none(source_current)
    if source_current_float is not None:
        return source_current_float, "source_current"
    return None, None


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


__all__ = ["add_options_data_commands", "handle_options_data_command"]
