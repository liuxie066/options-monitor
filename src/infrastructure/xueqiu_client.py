from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


XUEQIU_BASE_URL = "https://xueqiu.com"
XUEQIU_STOCK_BASE_URL = "https://stock.xueqiu.com"
DEFAULT_COOKIE_ENV = "XUEQIU_COOKIE"


class XueqiuClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class XueqiuHolding:
    source_cube: str
    raw_symbol: str
    symbol: str
    name: str | None
    weight: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_cube": self.source_cube,
            "raw_symbol": self.raw_symbol,
            "symbol": self.symbol,
            "name": self.name,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class XueqiuUserStock:
    source_user_id: str
    raw_symbol: str
    symbol: str
    name: str | None
    exchange: str | None = None
    marketplace: str | None = None
    current: float | None = None
    change_percent: float | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_user_id": self.source_user_id,
            "raw_symbol": self.raw_symbol,
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "marketplace": self.marketplace,
            "current": self.current,
            "change_percent": self.change_percent,
            "created_at": self.created_at,
        }


def normalize_cube_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("xueqiu cube symbol is required")
    if not re.fullmatch(r"[A-Z0-9_.-]+", symbol):
        raise ValueError("xueqiu cube symbol contains unsupported characters")
    return symbol


def normalize_user_id(value: str) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("xueqiu user id is required")
    if not re.fullmatch(r"\d+", user_id):
        raise ValueError("xueqiu user id must contain digits only")
    return user_id


def extract_user_id_from_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("xueqiu user URL is required")
    parsed = urlparse(raw)
    path = parsed.path or raw
    match = re.search(r"/u/(\d+)", path)
    if match:
        return normalize_user_id(match.group(1))
    if raw.isdigit():
        return normalize_user_id(raw)
    raise ValueError("could not find a xueqiu user id in the URL")


def canonicalize_xueqiu_stock_symbol(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""

    if ":" in raw:
        prefix, tail = raw.rsplit(":", 1)
        prefix = prefix.strip().upper()
        tail = tail.strip().upper()
        if prefix in {"SH", "SSE"} and tail.isdigit():
            return f"{tail}.SS"
        if prefix in {"SZ", "SZSE"} and tail.isdigit():
            return f"{tail}.SZ"
        if prefix in {"HK", "HKEX"}:
            digits = re.sub(r"\D", "", tail)
            if digits:
                return f"{digits[-4:].zfill(4)}.HK"
        raw = tail
    if raw.startswith("US.") and len(raw) > 3:
        return raw[3:]
    if raw.startswith("US") and len(raw) > 2 and raw[2:].replace(".", "").isalnum():
        return raw[2:].lstrip(".")
    if raw.startswith("HK"):
        digits = re.sub(r"\D", "", raw[2:])
        if digits:
            return f"{digits[-4:].zfill(4)}.HK"
    if raw.isdigit() and 1 <= len(raw) <= 5:
        return f"{raw[-4:].zfill(4)}.HK"
    if raw.startswith("SH") and raw[2:].isdigit():
        return f"{raw[2:]}.SS"
    if raw.startswith("SZ") and raw[2:].isdigit():
        return f"{raw[2:]}.SZ"
    return raw


def extract_user_stocks_from_portfolio_payload(payload: dict[str, Any], *, user_id: str) -> list[XueqiuUserStock]:
    normalized_user_id = normalize_user_id(user_id)
    raw_stocks = _find_stocks_list(payload)
    out: list[XueqiuUserStock] = []
    for item in raw_stocks:
        if not isinstance(item, dict):
            continue
        raw_symbol = str(item.get("symbol") or item.get("stock_symbol") or item.get("code") or "").strip()
        exchange = str(item.get("exchange") or "").strip().upper()
        symbol_input = f"{exchange}:{raw_symbol}" if exchange and raw_symbol and ":" not in raw_symbol else raw_symbol
        symbol = canonicalize_xueqiu_stock_symbol(symbol_input)
        if not symbol:
            continue
        out.append(
            XueqiuUserStock(
                source_user_id=normalized_user_id,
                raw_symbol=raw_symbol,
                symbol=symbol,
                name=str(item.get("name") or item.get("stock_name") or "").strip() or None,
                exchange=exchange or None,
                marketplace=str(item.get("marketplace") or item.get("market") or "").strip() or None,
                current=_float_or_none(item.get("current")),
                change_percent=_float_or_none(item.get("percent")),
                created_at=str(item.get("created") or item.get("created_at") or "").strip() or None,
            )
        )
    return out


def extract_holdings_from_cube_payload(payload: dict[str, Any], *, cube_symbol: str) -> list[XueqiuHolding]:
    cube = normalize_cube_symbol(cube_symbol)
    raw_holdings = _find_holdings_list(payload)
    out: list[XueqiuHolding] = []
    for item in raw_holdings:
        if not isinstance(item, dict):
            continue
        raw_symbol = str(
            item.get("stock_symbol")
            or item.get("symbol")
            or item.get("code")
            or item.get("stock_code")
            or ""
        ).strip()
        symbol = canonicalize_xueqiu_stock_symbol(raw_symbol)
        if not symbol:
            continue
        name = str(item.get("stock_name") or item.get("name") or "").strip() or None
        out.append(
            XueqiuHolding(
                source_cube=cube,
                raw_symbol=raw_symbol,
                symbol=symbol,
                name=name,
                weight=_float_or_none(item.get("weight")),
            )
        )
    return out


def fetch_user_stocks(
    user_url_or_id: str,
    *,
    cookie: str | None = None,
    cookie_env: str = DEFAULT_COOKIE_ENV,
    timeout: float = 10.0,
    base_url: str = XUEQIU_STOCK_BASE_URL,
    category: int = 1,
    size: int = 1000,
) -> list[XueqiuUserStock]:
    user_id = extract_user_id_from_url(user_url_or_id)
    payload = _request_json(
        base_url=base_url,
        path="/v5/stock/portfolio/stock/list.json",
        params={"uid": user_id, "category": int(category), "size": int(size)},
        cookie=cookie if cookie is not None else os.environ.get(cookie_env),
        timeout=timeout,
        referer=f"https://xueqiu.com/u/{user_id}",
    )
    if not isinstance(payload, dict):
        raise XueqiuClientError("xueqiu user stock response is not a JSON object")
    return extract_user_stocks_from_portfolio_payload(payload, user_id=user_id)


def fetch_cube_holdings(
    cube_symbol: str,
    *,
    cookie: str | None = None,
    cookie_env: str = DEFAULT_COOKIE_ENV,
    timeout: float = 10.0,
    base_url: str = XUEQIU_BASE_URL,
) -> list[XueqiuHolding]:
    cube = normalize_cube_symbol(cube_symbol)
    payload = _request_json(
        base_url=base_url,
        path="/cubes/show.json",
        params={"cube_symbol": cube},
        cookie=cookie if cookie is not None else os.environ.get(cookie_env),
        timeout=timeout,
        referer="https://xueqiu.com/",
    )
    if not isinstance(payload, dict):
        raise XueqiuClientError("xueqiu cube response is not a JSON object")
    return extract_holdings_from_cube_payload(payload, cube_symbol=cube)


def _request_json(
    *,
    base_url: str,
    path: str,
    params: dict[str, Any],
    cookie: str | None,
    timeout: float,
    referer: str = "https://xueqiu.com/",
) -> Any:
    url = f"{base_url.rstrip('/')}{path}?{urlencode(params)}"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Referer": referer,
    }
    if cookie:
        headers["Cookie"] = str(cookie)
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=float(timeout)) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = _read_http_error_body(exc)
        error_code = _xueqiu_error_code(error_body)
        error_description = _xueqiu_error_description(error_body)
        if exc.code in {401, 403}:
            raise XueqiuClientError(
                "xueqiu rejected the request; set XUEQIU_COOKIE if this page requires a logged-in session"
            ) from exc
        if exc.code == 400 and error_code == "400016":
            detail = f": {error_description}" if error_description else ""
            raise XueqiuClientError(
                "xueqiu requires a logged-in browser cookie for this stock page; set XUEQIU_COOKIE"
                f"{detail}"
            ) from exc
        if exc.code == 429:
            raise XueqiuClientError("xueqiu rate limited the request; wait before retrying") from exc
        raise XueqiuClientError(f"xueqiu request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise XueqiuClientError(f"xueqiu request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise XueqiuClientError("xueqiu request timed out") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise XueqiuClientError("xueqiu response is not valid JSON") from exc


def _find_holdings_list(payload: dict[str, Any]) -> list[Any]:
    for path in (
        ("view_rebalancing", "holdings"),
        ("last_rb", "holdings"),
        ("holdings",),
        ("data", "view_rebalancing", "holdings"),
        ("data", "last_rb", "holdings"),
        ("data", "holdings"),
    ):
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, list):
            return value
    return []


def _read_http_error_body(exc: HTTPError) -> dict[str, Any]:
    try:
        body = exc.read().decode("utf-8")
    except Exception:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _xueqiu_error_code(payload: dict[str, Any]) -> str:
    return str(payload.get("error_code") or "").strip()


def _xueqiu_error_description(payload: dict[str, Any]) -> str:
    return str(payload.get("error_description") or "").strip()


def _find_stocks_list(payload: dict[str, Any]) -> list[Any]:
    for path in (
        ("data", "stocks"),
        ("stocks",),
        ("data", "items"),
        ("items",),
    ):
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, list):
            return value
    return []


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_COOKIE_ENV",
    "XUEQIU_STOCK_BASE_URL",
    "XueqiuClientError",
    "XueqiuHolding",
    "XueqiuUserStock",
    "canonicalize_xueqiu_stock_symbol",
    "extract_holdings_from_cube_payload",
    "extract_user_id_from_url",
    "extract_user_stocks_from_portfolio_payload",
    "fetch_cube_holdings",
    "fetch_user_stocks",
    "normalize_cube_symbol",
    "normalize_user_id",
]
