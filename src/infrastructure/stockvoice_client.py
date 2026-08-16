from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STOCKVOICE_URL = "https://stockvoice.cmoney.tw/"


class StockVoiceClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class StockVoiceSignal:
    symbol: str
    name: str | None
    bullish_count: int
    bearish_count: int
    neutral_count: int
    bullish_ratio: float | None = None
    bull_bear_ratio: float | None = None
    price: float | None = None
    source_url: str = STOCKVOICE_URL

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "bullish_count": self.bullish_count,
            "bearish_count": self.bearish_count,
            "neutral_count": self.neutral_count,
            "bullish_ratio": self.bullish_ratio,
            "bull_bear_ratio": self.bull_bear_ratio,
            "price": self.price,
            "source_url": self.source_url,
        }


def fetch_stockvoice_signals(
    *,
    url: str = STOCKVOICE_URL,
    timeout: float = 10.0,
    min_bullish_count: int = 8,
    min_bull_bear_ratio: float = 2.0,
    min_net_bullish: int = 3,
    limit: int = 20,
) -> list[StockVoiceSignal]:
    html = _request_text(url=url, timeout=timeout)
    return extract_stockvoice_signals(
        html,
        source_url=url,
        min_bullish_count=min_bullish_count,
        min_bull_bear_ratio=min_bull_bear_ratio,
        min_net_bullish=min_net_bullish,
        limit=limit,
    )


def extract_stockvoice_signals(
    html: str,
    *,
    source_url: str = STOCKVOICE_URL,
    min_bullish_count: int = 8,
    min_bull_bear_ratio: float = 2.0,
    min_net_bullish: int = 3,
    limit: int = 20,
) -> list[StockVoiceSignal]:
    text = _normalize_text(html)
    signals_by_symbol: dict[str, StockVoiceSignal] = {}
    count_pattern = (
        r"看多\s*(?P<bullish>\d+)\s*(?:位)?\s*"
        r"(?:"
        r"中性\s*(?P<neutral_before>\d+)\s*(?:位)?\s*看空\s*(?P<bearish_after>\d+)\s*(?:位)?"
        r"|"
        r"看空\s*(?P<bearish_before>\d+)\s*(?:位)?\s*(?:中性\s*(?P<neutral_after>\d+)\s*(?:位)?)?"
        r")"
    )
    patterns = [
        re.compile(
            r"(?<![A-Z0-9])(?P<symbol>[A-Z][A-Z0-9]{0,5})(?:\s+(?P=symbol))?"
            r"(?:\s+[A-Za-z][A-Za-z0-9 .&\-]{0,40})?"
            r"\s*\$\d+(?:,\d{3})*(?:\.\d+)?"
            r".{0,180}?"
            + count_pattern
        ),
        re.compile(
            r"(?<![A-Z0-9])(?P<symbol>[A-Z][A-Z0-9]{0,5})"
            r"\s*讨论串.{0,80}?"
            + count_pattern
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            _add_signal_match(
                match,
                text=text,
                signals_by_symbol=signals_by_symbol,
                source_url=source_url,
                min_bullish_count=min_bullish_count,
                min_bull_bear_ratio=min_bull_bear_ratio,
                min_net_bullish=min_net_bullish,
            )
    out = sorted(signals_by_symbol.values(), key=_signal_sort_key)
    limit = max(0, int(limit or 0))
    return out[:limit] if limit else out


def _add_signal_match(
    match: re.Match[str],
    *,
    text: str,
    signals_by_symbol: dict[str, StockVoiceSignal],
    source_url: str,
    min_bullish_count: int,
    min_bull_bear_ratio: float,
    min_net_bullish: int,
) -> None:
        symbol = normalize_stockvoice_symbol(match.group("symbol"))
        if not symbol or not _looks_like_us_symbol(symbol):
            return
        bullish = _int_or_zero(match.group("bullish"))
        bearish = _int_or_zero(match.group("bearish_after") or match.group("bearish_before"))
        neutral = _int_or_zero(match.group("neutral_before") or match.group("neutral_after"))
        total = bullish + bearish + neutral
        bullish_ratio = round(bullish / total, 6) if total > 0 else None
        bull_bear_ratio = round(bullish / bearish, 6) if bearish > 0 else float("inf") if bullish > 0 else None
        if bullish < int(min_bullish_count):
            return
        if bullish - bearish < int(min_net_bullish):
            return
        if bearish > 0 and bullish / bearish < float(min_bull_bear_ratio):
            return
        price = _extract_price_near(text, match.start(), match.end())
        candidate = StockVoiceSignal(
            symbol=symbol,
            name=None,
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            bullish_ratio=bullish_ratio,
            bull_bear_ratio=bull_bear_ratio,
            price=price,
            source_url=source_url,
        )
        existing = signals_by_symbol.get(symbol)
        if existing is None or _signal_sort_key(candidate) < _signal_sort_key(existing):
            signals_by_symbol[symbol] = candidate


def normalize_stockvoice_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    symbol = symbol.replace("／", "/").split("/", 1)[0].strip()
    symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol)
    return symbol


def _request_text(*, url: str, timeout: float) -> str:
    request = Request(
        str(url),
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Referer": "https://stockvoice.cmoney.tw/",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=float(timeout)) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise StockVoiceClientError(f"stockvoice request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise StockVoiceClientError(f"stockvoice request failed: {exc.reason}") from exc
    except OSError as exc:
        raise StockVoiceClientError(f"stockvoice request failed: {exc}") from exc


def _normalize_text(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_us_symbol(symbol: str) -> bool:
    if not symbol or not symbol.isascii():
        return False
    if "." in symbol:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", symbol))


def _extract_price_near(text: str, start: int, end: int) -> float | None:
    window = text[start : min(len(text), end + 80)]
    match = re.search(r"\$(\d+(?:,\d{3})*(?:\.\d+)?)", window)
    if not match:
        window = text[max(0, start - 80) : end]
        match = re.search(r"\$(\d+(?:,\d{3})*(?:\.\d+)?)", window)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _int_or_zero(value: str | None) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _signal_sort_key(signal: StockVoiceSignal) -> tuple[float, int, int, str]:
    ratio = signal.bull_bear_ratio
    ratio_for_sort = ratio if ratio is not None and ratio != float("inf") else 999.0
    return (-ratio_for_sort, -signal.bullish_count, signal.bearish_count, signal.symbol)


__all__ = [
    "STOCKVOICE_URL",
    "StockVoiceClientError",
    "StockVoiceSignal",
    "extract_stockvoice_signals",
    "fetch_stockvoice_signals",
    "normalize_stockvoice_symbol",
]
