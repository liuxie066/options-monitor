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
from typing import Any, Callable, Mapping
from urllib import request as urllib_request

from src.infrastructure.io_utils import atomic_write_json


OPEND_EXCHANGE_RATE_SOURCE = "opend_account_funds_conversion"
TENCENT_EXCHANGE_RATE_SOURCE = "tencent_quote"
SINA_EXCHANGE_RATE_SOURCE = "sina_quote"

_REQUIRED_RATES = ("USDCNY", "HKDCNY")

# 腾讯主源：单次请求取 USDCNY + HKDCNY，`~` 分隔，字段 [3] 为现价。
_TENCENT_URL = "https://qt.gtimg.cn/q=whUSDCNY,whHKDCNY"
# 新浪兑底：需 Referer，返回行 `..."价格,..."`，取第一个字段。
_SINA_URL = "https://hq.sinajs.cn/list=fx_susdcny,fx_shkdcny"
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}


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

    def convert(self, amount: float, *, from_ccy: str, to_ccy: str) -> float | None:
        source = str(from_ccy or '').strip().upper()
        target = str(to_ccy or '').strip().upper()
        if source == 'RMB':
            source = 'CNY'
        if target == 'RMB':
            target = 'CNY'
        if not source or not target:
            return None
        if source == target:
            return float(amount)
        amount_cny = self.native_to_cny(float(amount), native_ccy=source)
        if amount_cny is None:
            return None
        if target == 'CNY':
            return amount_cny
        return self.cny_to_native(amount_cny, native_ccy=target)


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
    return 0 <= age_seconds <= int(max_age_hours) * 3600


def exchange_rate_observation_status(
    payload: Mapping[str, Any] | None,
    *,
    max_age_hours: int = 24,
) -> str:
    """Classify one provider observation without changing its timestamp."""

    if not isinstance(payload, Mapping):
        return "unavailable"
    value = dict(payload)
    if str(value.get("source") or "").strip() not in {
        OPEND_EXCHANGE_RATE_SOURCE,
        TENCENT_EXCHANGE_RATE_SOURCE,
        SINA_EXCHANGE_RATE_SOURCE,
    }:
        return "unavailable"
    rates = value.get("rates")
    if not isinstance(rates, Mapping) or not rates:
        return "unavailable"
    if _payload_timestamp(value) is None:
        return "unavailable_stale"
    return (
        "ready"
        if _is_cache_fresh(value, max_age_hours=max_age_hours)
        else "unavailable_stale"
    )


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
    if str(obj.get("source") or "").strip() not in {
        OPEND_EXCHANGE_RATE_SOURCE,
        TENCENT_EXCHANGE_RATE_SOURCE,
        SINA_EXCHANGE_RATE_SOURCE,
    }:
        return None
    if _payload_timestamp(obj) is None:
        return None
    rates = obj.get("rates")
    if not isinstance(rates, Mapping) or not rates:
        return None
    if not _is_cache_fresh(obj, max_age_hours=max_age_hours):
        return None
    return obj


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout_sec: float = 8.0) -> str:
    req = urllib_request.Request(url, headers=headers or {})
    with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _validated_rates(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, float] = {}
    for key in _REQUIRED_RATES:
        raw = value.get(key)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if not (0 < number < 1000):
            return None
        out[key] = round(number, 4)
    return out


def _parse_tencent(text: str) -> dict[str, float] | None:
    """Parse `v_whUSDCNY="~名称~代码~价~..."` lines into USDCNY/HKDCNY."""

    rates: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if "whUSDCNY" in line:
            bits = line.split("~")
            if len(bits) >= 4:
                rates["USDCNY"] = float(bits[3])
        elif "whHKDCNY" in line:
            bits = line.split("~")
            if len(bits) >= 4:
                rates["HKDCNY"] = float(bits[3])
    return _validated_rates(rates)


def _parse_sina(text: str) -> dict[str, float] | None:
    """Parse `var hq_str_...="价,昨收,..."` lines into USDCNY/HKDCNY.

    Sina fx_susdcny / fx_shkdcny return `现价,昨收,今开,...`; take the first.
    """

    rates: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        code: str | None = None
        if "fx_susdcny" in line or ("USDCNY" in line and "hq_str_" in line):
            code = "USDCNY"
        elif "fx_shkdcny" in line or ("HKDCNY" in line and "hq_str_" in line):
            code = "HKDCNY"
        if code is None:
            continue
        try:
            parts = line.split('"')[1].split(",")
            if len(parts) >= 1:
                rates[code] = float(parts[0])
        except (IndexError, ValueError):
            continue
    return _validated_rates(rates)


def fetch_market_exchange_rates(timeout_sec: float = 8.0) -> dict[str, Any] | None:
    """Fetch USDCNY/HKDCNY from Tencent first, falling back to Sina.

    Returns a canonical observation dict ``{source, rates, timestamp}``, or
    ``None`` when both providers fail (fail-closed; never fabricate a rate).
    """

    errors: list[str] = []
    try:
        rates = _parse_tencent(_http_get(_TENCENT_URL, timeout_sec=timeout_sec))
        if rates is not None:
            return {
                "source": TENCENT_EXCHANGE_RATE_SOURCE,
                "rates": rates,
                "timestamp": _utc_now().isoformat(),
            }
        errors.append("tencent:invalid")
    except Exception as exc:
        errors.append(f"tencent:{type(exc).__name__}")

    try:
        rates = _parse_sina(_http_get(_SINA_URL, headers=_SINA_HEADERS, timeout_sec=timeout_sec))
        if rates is not None:
            return {
                "source": SINA_EXCHANGE_RATE_SOURCE,
                "rates": rates,
                "timestamp": _utc_now().isoformat(),
            }
        errors.append("sina:invalid")
    except Exception as exc:
        errors.append(f"sina:{type(exc).__name__}")

    _warn(None, f"[WARN] market FX fetch failed: {'; '.join(errors)}")
    return None


def _cache_from_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    return dict(observation)


def save_exchange_rate_observation(
    path: Path,
    payload: Mapping[str, Any],
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Persist the exact provider observation without refreshing its timestamp."""
    try:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(payload)
        rates = value.get("rates")
        if not isinstance(rates, dict) or not rates:
            raise ValueError("exchange-rate payload rates are required")
        if _payload_timestamp(value) is None:
            raise ValueError("exchange-rate payload timestamp is required")
        if str(value.get("source") or "").strip() not in {
            OPEND_EXCHANGE_RATE_SOURCE,
            TENCENT_EXCHANGE_RATE_SOURCE,
            SINA_EXCHANGE_RATE_SOURCE,
        }:
            raise ValueError("exchange-rate payload source is unknown")
        atomic_write_json(path, value, sort_keys=True)
    except Exception as exc:
        _warn(log, f"[WARN] exchange_rate cache write failed: path={path} error={exc}")


def get_exchange_rates_or_fetch_latest(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    write_through_path: Path | None = None,
    log: Callable[[str], None] | None = None,
    write_cache: bool = True,
) -> dict | None:
    """Return a fresh market FX observation, fetching Tencent/Sina on miss.

    Reads the cache first; when missing or stale, fetches from the market
    providers (Tencent primary, Sina fallback) and writes the fresh
    observation back to ``cache_path``. Returns ``None`` only when no source
    yields a valid rate (fail-closed).
    """

    cached = get_cached_exchange_rates(cache_path=cache_path, max_age_hours=max_age_hours)
    if cached is not None:
        return cached

    observation = fetch_market_exchange_rates()
    if observation is None:
        # Fall back to a stale cache rather than fabricating a number.
        stale = _read_cache(cache_path)
        if stale is not None and isinstance(stale.get("rates"), Mapping) and stale.get("rates"):
            _warn(log, f"[WARN] market FX unavailable; using stale cache {Path(cache_path).resolve()}")
            return stale
        _warn(log, f"[WARN] market FX unavailable and no cache: {Path(cache_path).resolve()}")
        return None

    if write_cache:
        try:
            save_exchange_rate_observation(cache_path, observation, log=log)
        except Exception as exc:
            _warn(log, f"[WARN] failed to persist FX observation: {exc}")
    if write_through_path is not None:
        try:
            save_exchange_rate_observation(write_through_path, observation, log=log)
        except Exception as exc:
            _warn(log, f"[WARN] failed to persist FX write-through: {exc}")
    return observation


def load_exchange_rate_info(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    fetch_latest_on_miss: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    del fetch_latest_on_miss, log
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

    NOTE: This function keeps the existing call signature and reads only a
    fresh OpenD observation from the repo-local cache.
    """
    try:
        base_dir = Path(base_dir).resolve()
        obj = get_exchange_rates_or_fetch_latest(
            cache_path=(base_dir / 'output_shared' / 'state' / 'rate_cache.json').resolve(),
            max_age_hours=24,
        )
        usdcny = _extract_usdcny_from_rates(obj)
        if usdcny is None or usdcny <= 0:
            return None
        return 1.0 / usdcny
    except Exception:
        return None
