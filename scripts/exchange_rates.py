"""Exchange-rate conversion utilities (Stage 2).

Goal: centralize exchange-rate math so call-sites don't replicate USD/HKD/CNY conversions.

Conventions:
- usd_per_cny_exchange_rate: USD per 1 CNY (e.g., 0.14)
- cny_per_hkd_exchange_rate: CNY per 1 HKD (e.g., 0.92)

This module is intentionally minimal; expand only as needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Callable
from urllib import request


@dataclass(frozen=True)
class ExchangeRates:
    usd_per_cny: float | None = None
    cny_per_hkd: float | None = None


@dataclass(frozen=True)
class CurrencyConverter:
    """Convert between base CNY and option native currencies (USD/HKD)."""

    rates: ExchangeRates

    def cny_to_usd(self, cny: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(cny) * float(r)

    def usd_to_cny(self, usd: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(usd) / float(r)

    def cny_to_hkd(self, cny: float) -> float | None:
        cny_per_hkd_exchange_rate = self.rates.cny_per_hkd
        if cny_per_hkd_exchange_rate is None or cny_per_hkd_exchange_rate <= 0:
            return None
        return float(cny) / float(cny_per_hkd_exchange_rate)

    def hkd_to_cny(self, hkd: float) -> float | None:
        cny_per_hkd_exchange_rate = self.rates.cny_per_hkd
        if cny_per_hkd_exchange_rate is None or cny_per_hkd_exchange_rate <= 0:
            return None
        return float(hkd) * float(cny_per_hkd_exchange_rate)

    def cny_to_native(self, cny: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.cny_to_usd(cny)
        if c == 'HKD':
            return self.cny_to_hkd(cny)
        return None

    def native_to_cny(self, amount: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.usd_to_cny(amount)
        if c == 'HKD':
            return self.hkd_to_cny(amount)
        if c == 'CNY':
            return float(amount)
        return None


SINA_EXCHANGE_RATE_SYMBOLS = {
    'USDCNY': 'usdcny',
    'HKDCNY': 'hkdcny',
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _payload_timestamp(obj: dict | None) -> datetime | None:
    if not isinstance(obj, dict):
        return None
    return _parse_iso_datetime(obj.get('timestamp'))


def _is_cache_fresh(obj: dict | None, *, max_age_hours: int | None) -> bool:
    if not isinstance(obj, dict):
        return False
    if max_age_hours is None or int(max_age_hours) <= 0:
        return True
    ts = _payload_timestamp(obj)
    if ts is None:
        return False
    age_seconds = (_utc_now() - ts).total_seconds()
    return age_seconds <= int(max_age_hours) * 3600


def _read_cache(path: Path) -> dict | None:
    try:
        p = Path(path).resolve()
        if not p.exists() or p.stat().st_size <= 0:
            return None
        obj = json.loads(p.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def get_cached_exchange_rates(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
) -> dict | None:
    """Read current-project exchange-rate cache when present and fresh enough."""
    obj = _read_cache(cache_path)
    if obj is None:
        return None
    if not _is_cache_fresh(obj, max_age_hours=max_age_hours):
        return None
    return obj


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def _save_rates(path: Path, rates: dict[str, float], *, log: Callable[[str], None] | None = None) -> None:
    try:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rates": rates,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _warn(log, f"[WARN] exchange_rate cache write failed: path={path} error={exc}")


def _extract_last_float(payload: str) -> float | None:
    try:
        left = payload.index('"')
        right = payload.rindex('"')
    except ValueError:
        return None
    fields = [item.strip() for item in payload[left + 1:right].split(',')]
    if len(fields) < 2:
        return None
    for idx in (8, 1):
        try:
            value = float(fields[idx])
        except Exception:
            continue
        if value > 0:
            return value
    return None


def _fetch_sina_exchange_rate(
    pair: str,
    *,
    log: Callable[[str], None] | None = None,
) -> float | None:
    symbol = SINA_EXCHANGE_RATE_SYMBOLS.get(pair)
    if not symbol:
        return None
    url = f'https://hq.sinajs.cn/list=fx_s{symbol}'
    req = request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn/',
        },
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            payload = resp.read().decode('gbk', errors='ignore')
    except Exception as exc:
        _warn(log, f"[WARN] sina exchange_rate fetch failed: pair={pair} error={exc}")
        return None
    value = _extract_last_float(payload)
    if value is None:
        _warn(log, f"[WARN] sina exchange_rate payload invalid: pair={pair}")
    return value


def fetch_latest_exchange_rates(
    *,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    rates: dict[str, float] = {}
    for pair in ('USDCNY', 'HKDCNY'):
        value = _fetch_sina_exchange_rate(pair, log=log)
        if value is None:
            return None
        rates[pair] = value
    return {
        'rates': rates,
        'timestamp': _utc_now().isoformat(),
        'source': 'sina_fx',
    }


def get_exchange_rates_or_fetch_latest(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    write_through_path: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    cached = get_cached_exchange_rates(cache_path=cache_path, max_age_hours=max_age_hours)
    if cached is not None:
        return cached

    stale = _read_cache(cache_path)
    _warn(
        log,
        f"[WARN] exchange_rate cache miss/stale: {Path(cache_path).resolve()}; trying sina live fetch",
    )
    latest = fetch_latest_exchange_rates(log=log)
    if latest is not None:
        rates = latest.get('rates')
        if isinstance(rates, dict):
            _save_rates(Path(write_through_path or cache_path), rates, log=log)
        return latest
    if stale is not None:
        _warn(log, "[WARN] exchange_rate live fetch unavailable; fallback to stale cache")
        return stale
    _warn(log, "[WARN] exchange_rate live fetch unavailable and no local cache")
    return None


def load_exchange_rate_info(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    fetch_latest_on_miss: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    if fetch_latest_on_miss:
        return get_exchange_rates_or_fetch_latest(
            cache_path=cache_path,
            max_age_hours=max_age_hours,
            log=log,
        )
    return get_cached_exchange_rates(cache_path=cache_path, max_age_hours=max_age_hours)


def _extract_usdcny_from_rates(obj: dict | None) -> float | None:
    """Extract USDCNY from either legacy or nested schema.

    - Legacy: {USDCNY: <value>, HKDCNY: <value>}
    - New: {rates: {USDCNY: <value>, HKDCNY: <value>}}
    Returns float or None.
    """
    if not obj:
        return None
    # Try new nested schema
    rates_map = obj.get('rates')
    if isinstance(rates_map, dict):
        usdcny = rates_map.get('USDCNY')
        if usdcny is not None:
            try:
                return float(usdcny)
            except Exception:
                return None
    # Legacy top-level
    usdcny = obj.get('USDCNY')
    if usdcny is not None:
        try:
            return float(usdcny)
        except Exception:
            return None
    return None


def get_usd_per_cny_exchange_rate(base_dir: Path) -> float | None:
    """Return USD per 1 CNY from rate_cache.json.

    rate_cache stores USDCNY (CNY per 1 USD). We invert it.

    NOTE: For backward compatibility this function keeps the original signature.
    It reads and refreshes the repo-local cache:
      1) <base_dir>/output/state/rate_cache.json
      2) on miss/stale, fetch latest online and write back to the same file
    """
    try:
        base_dir = Path(base_dir).resolve()
        obj = get_exchange_rates_or_fetch_latest(
            cache_path=(base_dir / 'output' / 'state' / 'rate_cache.json').resolve(),
            max_age_hours=24,
        )
        usdcny = _extract_usdcny_from_rates(obj)
        if usdcny is None or usdcny <= 0:
            return None
        return 1.0 / usdcny
    except Exception:
        return None
