from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.client import RemoteDisconnected
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ROBINHOOD_BASE_URL = "https://api.robinhood.com"
ROBINHOOD_TOKEN_ENV = "ROBINHOOD_AUTH_TOKEN"


class RobinhoodOptionsError(RuntimeError):
    pass


@dataclass(frozen=True)
class RobinhoodOptionChainRequest:
    symbol: str
    expiration: str | None = None
    side: str | None = None
    min_open_interest: int | None = None
    min_volume: int | None = None
    max_bid_ask_spread: float | None = None
    token: str | None = None
    timeout: float = 20.0
    base_url: str = ROBINHOOD_BASE_URL


def fetch_robinhood_option_chain(request: RobinhoodOptionChainRequest) -> list[dict[str, Any]]:
    symbol = normalize_robinhood_symbol(request.symbol)
    token = str(request.token or "").strip()
    if not token:
        raise RobinhoodOptionsError(
            f"set {ROBINHOOD_TOKEN_ENV} with a logged-in Robinhood session token before fetching options data"
        )

    equity = _find_equity_instrument(request, symbol=symbol, token=token)
    equity_id = _id_from_url(str(equity.get("url") or ""))
    if not equity_id:
        raise RobinhoodOptionsError(f"robinhood instrument lookup did not return an id for {symbol}")

    chain = _find_chain(request, symbol=symbol, equity_id=equity_id, token=token)
    chain_id = str(chain.get("id") or _id_from_url(str(chain.get("url") or ""))).strip()
    if not chain_id:
        raise RobinhoodOptionsError(f"robinhood options chain lookup did not return an id for {symbol}")

    option_instruments = _fetch_option_instruments(request, symbol=symbol, chain_id=chain_id, token=token)
    if not option_instruments:
        return []
    marketdata = _fetch_option_marketdata(request, option_instruments=option_instruments, token=token)
    rows = normalize_robinhood_option_chain_payload(
        option_instruments,
        marketdata_by_instrument=marketdata,
        requested_symbol=symbol,
    )
    filtered = [row for row in rows if _row_passes_filters(row, request)]
    return sorted(
        filtered,
        key=lambda row: (
            str(row.get("expiration") or "9999-99-99"),
            float(row.get("strike") if row.get("strike") is not None else 10**12),
            str(row.get("option_type") or ""),
        ),
    )


def fetch_robinhood_stock_quotes(
    symbols: list[str],
    *,
    token: str | None = None,
    timeout: float = 20.0,
    base_url: str = ROBINHOOD_BASE_URL,
) -> dict[str, dict[str, Any]]:
    normalized = [normalize_robinhood_symbol(symbol) for symbol in symbols if str(symbol or "").strip()]
    if not normalized:
        return {}
    stripped_token = str(token or "").strip()
    if not stripped_token:
        raise RobinhoodOptionsError(
            f"set {ROBINHOOD_TOKEN_ENV} with a logged-in Robinhood session token before fetching stock quotes"
        )
    rows = _request_results_json(
        _api_url(base_url, "/quotes/"),
        params={"symbols": ",".join(normalized)},
        token=stripped_token,
        timeout=timeout,
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_symbol = str(row.get("symbol") or "").strip()
        if not raw_symbol:
            continue
        symbol = normalize_robinhood_symbol(raw_symbol)
        if not symbol:
            continue
        out[symbol] = {
            "symbol": symbol,
            "last_trade_price": _float_or_none(row.get("last_trade_price")),
            "bid_price": _float_or_none(row.get("bid_price")),
            "ask_price": _float_or_none(row.get("ask_price")),
            "previous_close": _float_or_none(row.get("previous_close")),
            "updated_at": row.get("updated_at"),
            "raw_quote": row,
        }
    return out


def normalize_robinhood_option_chain_payload(
    option_instruments: list[dict[str, Any]],
    *,
    marketdata_by_instrument: dict[str, dict[str, Any]] | None = None,
    requested_symbol: str,
) -> list[dict[str, Any]]:
    marketdata_by_instrument = marketdata_by_instrument or {}
    symbol = normalize_robinhood_symbol(requested_symbol)
    rows: list[dict[str, Any]] = []
    for instrument in option_instruments:
        if not isinstance(instrument, dict):
            continue
        instrument_url = str(instrument.get("url") or "").strip()
        option_id = str(instrument.get("id") or _id_from_url(instrument_url) or "").strip()
        market = _marketdata_for_instrument(marketdata_by_instrument, instrument_url=instrument_url, option_id=option_id)
        option_type = str(instrument.get("type") or "").strip().lower() or None
        expiration = str(instrument.get("expiration_date") or "").strip() or None
        strike = _float_or_none(instrument.get("strike_price"))
        bid = _float_or_none(market.get("bid_price"))
        ask = _float_or_none(market.get("ask_price"))
        mid = _first_float(
            market.get("adjusted_mark_price"),
            market.get("mark_price"),
            _midpoint(bid, ask),
        )
        contract_symbol = _build_occ_contract_symbol(symbol, expiration, option_type, strike)
        rows.append(
            {
                "provider": "robinhood",
                "data_mode": "robinhood",
                "symbol": str(instrument.get("chain_symbol") or symbol).strip().upper(),
                "underlier_code": str(instrument.get("chain_symbol") or symbol).strip().upper(),
                "contract_symbol": contract_symbol,
                "option_code": contract_symbol,
                "option_id": option_id or None,
                "instrument_url": instrument_url or None,
                "option_type": option_type,
                "expiration": expiration,
                "expiration_ymd": expiration,
                "strike": strike,
                "strike_price": strike,
                "bid": bid,
                "bid_price": bid,
                "ask": ask,
                "ask_price": ask,
                "mid": mid,
                "last": _float_or_none(market.get("last_trade_price")),
                "last_price": _float_or_none(market.get("last_trade_price")),
                "volume": _int_or_none(market.get("volume")),
                "open_interest": _int_or_none(market.get("open_interest")),
                "iv": _float_or_none(market.get("implied_volatility")),
                "delta": _float_or_none(market.get("delta")),
                "gamma": _float_or_none(market.get("gamma")),
                "theta": _float_or_none(market.get("theta")),
                "vega": _float_or_none(market.get("vega")),
                "chance_of_profit_long": _float_or_none(market.get("chance_of_profit_long")),
                "chance_of_profit_short": _float_or_none(market.get("chance_of_profit_short")),
                "bid_ask_spread": _spread(bid, ask),
                "state": instrument.get("state"),
                "tradability": instrument.get("tradability") or instrument.get("rhs_tradability"),
                "updated_at": market.get("updated_at") or market.get("updated_at_timestamp"),
                "raw_marketdata": market or None,
            }
        )
    return rows


def normalize_robinhood_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    return symbol.replace("/", ".")


def _find_equity_instrument(
    request: RobinhoodOptionChainRequest,
    *,
    symbol: str,
    token: str,
) -> dict[str, Any]:
    rows = _request_paginated_json(
        _api_url(request.base_url, "/instruments/"),
        params={"symbol": symbol},
        token=token,
        timeout=request.timeout,
    )
    for row in rows:
        if str(row.get("symbol") or "").strip().upper() == symbol:
            return row
    raise RobinhoodOptionsError(f"robinhood could not find an equity instrument for {symbol}")


def _find_chain(
    request: RobinhoodOptionChainRequest,
    *,
    symbol: str,
    equity_id: str,
    token: str,
) -> dict[str, Any]:
    rows = _request_paginated_json(
        _api_url(request.base_url, "/options/chains/"),
        params={"equity_instrument_ids": equity_id},
        token=token,
        timeout=request.timeout,
    )
    for row in rows:
        if str(row.get("symbol") or "").strip().upper() == symbol:
            return row
    if rows:
        return rows[0]
    raise RobinhoodOptionsError(f"robinhood could not find an options chain for {symbol}")


def _fetch_option_instruments(
    request: RobinhoodOptionChainRequest,
    *,
    symbol: str,
    chain_id: str,
    token: str,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "chain_id": chain_id,
        "chain_symbol": symbol,
        "state": "active",
    }
    if request.expiration:
        params["expiration_dates"] = str(request.expiration)
    if request.side:
        params["type"] = str(request.side).strip().lower()

    rows = _request_paginated_json(
        _api_url(request.base_url, "/options/instruments/"),
        params=params,
        token=token,
        timeout=request.timeout,
    )
    return [row for row in rows if _instrument_matches(row, request=request, symbol=symbol)]


def _fetch_option_marketdata(
    request: RobinhoodOptionChainRequest,
    *,
    option_instruments: list[dict[str, Any]],
    token: str,
) -> dict[str, dict[str, Any]]:
    urls = [str(item.get("url") or "").strip() for item in option_instruments if item.get("url")]
    marketdata: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(urls, 50):
        rows = _request_results_json(
            _api_url(request.base_url, "/marketdata/options/"),
            params={"instruments": ",".join(chunk)},
            token=token,
            timeout=request.timeout,
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            instrument_url = str(row.get("instrument") or "").strip()
            option_id = str(row.get("id") or _id_from_url(instrument_url) or "").strip()
            if instrument_url:
                marketdata[instrument_url] = row
            if option_id:
                marketdata[option_id] = row
    return marketdata


def _request_paginated_json(
    url: str,
    *,
    params: dict[str, Any],
    token: str,
    timeout: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params: dict[str, Any] | None = params
    while next_url:
        payload = _request_json(next_url, params=next_params, token=token, timeout=timeout)
        if not isinstance(payload, dict):
            raise RobinhoodOptionsError("robinhood response is not a JSON object")
        results = payload.get("results")
        if isinstance(results, list):
            rows.extend(row for row in results if isinstance(row, dict))
        elif isinstance(results, dict):
            rows.append(results)
        else:
            rows.append(payload)
            break
        next_raw = payload.get("next")
        next_url = str(next_raw).strip() if next_raw else None
        next_params = None
    return rows


def _request_results_json(
    url: str,
    *,
    params: dict[str, Any],
    token: str,
    timeout: float,
) -> list[dict[str, Any]]:
    payload = _request_json(url, params=params, token=token, timeout=timeout)
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [row for row in results if isinstance(row, dict)]
        if isinstance(results, dict):
            return [results]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise RobinhoodOptionsError("robinhood marketdata response is not a results list")


def _request_json(url: str, *, params: dict[str, Any] | None, token: str, timeout: float) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    headers = {
        "Accept": "application/json",
        "Authorization": _authorization_header_value(token),
        "User-Agent": "options-monitor/robinhood-options-provider",
    }
    try:
        with urlopen(Request(f"{url}{query}", headers=headers, method="GET"), timeout=float(timeout)) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = _read_http_error_body(exc)
        if exc.code in {401, 403}:
            raise RobinhoodOptionsError(
                f"robinhood rejected the request; set {ROBINHOOD_TOKEN_ENV} from a logged-in session"
            ) from exc
        if exc.code == 429:
            raise RobinhoodOptionsError("robinhood rate limited the request; wait before retrying") from exc
        detail = f": {body}" if body else ""
        raise RobinhoodOptionsError(f"robinhood request failed with HTTP {exc.code}{detail}") from exc
    except URLError as exc:
        raise RobinhoodOptionsError(f"robinhood request failed: {exc.reason}") from exc
    except RemoteDisconnected as exc:
        raise RobinhoodOptionsError("robinhood closed the connection before returning data; retry the scan") from exc
    except TimeoutError as exc:
        raise RobinhoodOptionsError("robinhood request timed out") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RobinhoodOptionsError("robinhood response is not valid JSON") from exc


def _authorization_header_value(token: str) -> str:
    stripped = token.strip()
    if stripped.lower().startswith(("bearer ", "token ")):
        return stripped
    return f"Bearer {stripped}"


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.strip('/')}/"


def _instrument_matches(row: dict[str, Any], *, request: RobinhoodOptionChainRequest, symbol: str) -> bool:
    if str(row.get("chain_symbol") or "").strip().upper() not in {"", symbol}:
        return False
    if request.expiration and str(row.get("expiration_date") or "") != str(request.expiration):
        return False
    if request.side and str(row.get("type") or "").strip().lower() != str(request.side).strip().lower():
        return False
    return True


def _row_passes_filters(row: dict[str, Any], request: RobinhoodOptionChainRequest) -> bool:
    open_interest = _int_or_none(row.get("open_interest"))
    volume = _int_or_none(row.get("volume"))
    spread = _float_or_none(row.get("bid_ask_spread"))
    if request.min_open_interest is not None and (open_interest is None or open_interest < request.min_open_interest):
        return False
    if request.min_volume is not None and (volume is None or volume < request.min_volume):
        return False
    if request.max_bid_ask_spread is not None and (spread is None or spread > request.max_bid_ask_spread):
        return False
    return True


def _marketdata_for_instrument(
    marketdata_by_instrument: dict[str, dict[str, Any]],
    *,
    instrument_url: str,
    option_id: str,
) -> dict[str, Any]:
    if instrument_url and instrument_url in marketdata_by_instrument:
        return marketdata_by_instrument[instrument_url]
    if option_id and option_id in marketdata_by_instrument:
        return marketdata_by_instrument[option_id]
    return {}


def _build_occ_contract_symbol(
    symbol: str,
    expiration: str | None,
    option_type: str | None,
    strike: float | None,
) -> str | None:
    if not expiration or option_type not in {"call", "put"} or strike is None:
        return None
    try:
        ymd = expiration.replace("-", "")
        date_part = ymd[2:8]
        type_part = "C" if option_type == "call" else "P"
        strike_part = int((Decimal(str(strike)) * Decimal("1000")).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None
    root = symbol.replace(".", "").replace("/", "").upper()
    return f"{root}{date_part}{type_part}{strike_part:08d}"


def _id_from_url(value: str) -> str | None:
    path_parts = [part for part in urlparse(value).path.split("/") if part]
    if path_parts:
        return path_parts[-1]
    query = parse_qs(urlparse(value).query)
    ids = query.get("id") or query.get("ids")
    return ids[0] if ids else None


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2.0, 6)


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round(max(0.0, ask - bid), 6)


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


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except Exception:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("error") or payload)[:300]
    return body[:300]


__all__ = [
    "ROBINHOOD_BASE_URL",
    "ROBINHOOD_TOKEN_ENV",
    "RobinhoodOptionChainRequest",
    "RobinhoodOptionsError",
    "fetch_robinhood_option_chain",
    "fetch_robinhood_stock_quotes",
    "normalize_robinhood_option_chain_payload",
    "normalize_robinhood_symbol",
]
