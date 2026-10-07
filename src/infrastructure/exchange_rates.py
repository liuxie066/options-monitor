"""Exchange-rate conversion utilities (Stage 2).

Goal: centralize exchange-rate math so call-sites don't replicate USD/HKD/CNY conversions.

Conventions:
- usd_per_cny_exchange_rate: USD per 1 CNY (e.g., 0.14)
- cny_per_hkd_exchange_rate: CNY per 1 HKD (e.g., 0.92)

This module is intentionally minimal; expand only as needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Mapping
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

from domain.domain.fx_quote_policy import CALENDAR_EVIDENCE, quote_quality, quote_session_start

from src.infrastructure.io_utils import atomic_write_json


OPEND_EXCHANGE_RATE_SOURCE = "opend_account_funds_conversion"
TENCENT_EXCHANGE_RATE_SOURCE = "tencent_quote"
SINA_EXCHANGE_RATE_SOURCE = "sina_quote"

_REQUIRED_RATES = ("USDCNY", "HKDCNY")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _valid_rate(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number < 1000 else None


# 腾讯主源：单次请求取 USDCNY + HKDCNY，`~` 分隔，字段 [3] 为现价。
_TENCENT_URL = "https://qt.gtimg.cn/q=whUSDCNY,whHKDCNY"
# 新浪后备：需 Referer；fx_s* 为 `时间,价格,...,日期`。
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


def exchange_rate_observation_status(
    payload: Mapping[str, Any] | None,
    *,
    max_age_hours: int = 24,
) -> str:
    """Classify one provider observation without changing its timestamp."""

    if not isinstance(payload, Mapping):
        return "unavailable"
    now = _utc_now()
    pairs = _verified_pairs(payload, now=now)
    if any(pair not in pairs for pair in _REQUIRED_RATES):
        return "unavailable"
    for pair in _REQUIRED_RATES:
        quality, _ = _pair_quality(pairs[pair], now=now)
        if quality == "holiday_carried":
            continue
        if quality != "fresh" or now - _strict_timestamp(pairs[pair]["quote_at_utc"]) > timedelta(hours=max_age_hours):
            return "unavailable_stale"
    return "ready"


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
    """Read verified pair facts through the current owner, without fetching."""
    obj = _read_cache(cache_path)
    pairs = _verified_pairs(obj, now=_utc_now())
    if not pairs:
        return None
    if max_age_hours is not None:
        now = _utc_now()
        pairs = {
            pair: row for pair, row in pairs.items()
            if now - _strict_timestamp(row["quote_at_utc"]) <= timedelta(hours=max_age_hours)
        }
    return _legacy_projection({"pairs": pairs}, purpose=None) if pairs else None


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout_sec: float = 8.0) -> str:
    merged = {
        "User-Agent": "Mozilla/5.0 (compatible; options-monitor/1.0)",
        **(headers or {}),
    }
    req = urllib_request.Request(url, headers=merged)
    with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _validated_rates(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, float] = {}
    for key in _REQUIRED_RATES:
        number = _valid_rate(value.get(key))
        if number is None:
            return None
        out[key] = number
    return out


def _parse_provider_quotes(
    payload: str, *, source: str, observed_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Parse only the two known wire layouts; each pair stands on its own."""
    observed = observed_at.astimezone(timezone.utc)
    quotes: dict[str, dict[str, Any]] = {}
    for line in payload.splitlines():
        name, separator, value = line.strip().partition("=")
        if not separator or not value.startswith('"') or '"' not in value[1:]:
            continue
        if source == TENCENT_EXCHANGE_RATE_SOURCE:
            pair = next((p for p in _REQUIRED_RATES if name == f"v_wh{p}"), None)
            fields = value[1:].split('"', 1)[0].split("~")
            if pair is None or len(fields) <= 5 or fields[2] != pair:
                continue
            raw_rate, raw_time = fields[3], fields[5]
            if len(raw_time) != 14 or not raw_time.isdigit():
                continue
            try:
                quoted = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=_SHANGHAI)
            except ValueError:
                continue
        elif source == SINA_EXCHANGE_RATE_SOURCE:
            pair = next((p for p in _REQUIRED_RATES if name == f"var hq_str_fx_s{p.lower()}"), None)
            fields = value[1:].split('"', 1)[0].split(",")
            if pair is None or len(fields) <= 17:
                continue
            raw_rate = fields[1]
            if len(fields[17].strip()) != 10 or len(fields[0].strip()) != 8:
                continue
            try:
                quoted = datetime.strptime(
                    f"{fields[17].strip()} {fields[0].strip()}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=_SHANGHAI)
            except ValueError:
                continue
        else:
            return {}
        rate = _valid_rate(raw_rate)
        quote_at = quoted.astimezone(timezone.utc)
        if rate is None or quote_at > observed:
            continue
        candidate = {
            "rate": rate,
            "source": source,
            "quote_at_utc": quote_at.isoformat(),
            "observed_at_utc": observed.isoformat(),
        }
        quotes[pair] = _newer_pair(quotes.get(pair), candidate)
    return quotes


def _parse_tencent(text: str) -> dict[str, float] | None:
    quotes = _parse_provider_quotes(text, source=TENCENT_EXCHANGE_RATE_SOURCE, observed_at=_utc_now())
    return _validated_rates({pair: row["rate"] for pair, row in quotes.items()})


def _parse_sina(text: str) -> dict[str, float] | None:
    quotes = _parse_provider_quotes(text, source=SINA_EXCHANGE_RATE_SOURCE, observed_at=_utc_now())
    return _validated_rates({pair: row["rate"] for pair, row in quotes.items()})


def fetch_market_exchange_rates(timeout_sec: float = 8.0) -> dict[str, Any] | None:
    """Fetch both sources and choose the newest verified quote per pair."""
    observed = _utc_now()
    errors: list[str] = []
    chosen: dict[str, dict[str, Any]] = {}
    for source, url, headers in (
        (TENCENT_EXCHANGE_RATE_SOURCE, _TENCENT_URL, None),
        (SINA_EXCHANGE_RATE_SOURCE, _SINA_URL, _SINA_HEADERS),
    ):
        try:
            raw = _http_get(url, headers=headers, timeout_sec=timeout_sec)
            observed = _utc_now()
            candidates = _parse_provider_quotes(raw, source=source, observed_at=observed)
            if not candidates:
                errors.append(f"{source}:invalid")
            for pair, row in candidates.items():
                previous = chosen.get(pair)
                if previous is None or row["quote_at_utc"] > previous["quote_at_utc"]:
                    chosen[pair] = row
        except Exception as exc:
            errors.append(f"{source}:{type(exc).__name__}")
    if not chosen:
        _warn(None, f"[WARN] market FX fetch failed: {'; '.join(errors)}")
        return None
    sources = {row["source"] for row in chosen.values()}
    timestamps = {pair: row["quote_at_utc"] for pair, row in chosen.items()}
    return {
        "source": next(iter(sources)) if len(sources) == 1 else "mixed",
        "rates": {pair: row["rate"] for pair, row in chosen.items()},
        "timestamp": min(timestamps.values()),
        "quote_timestamps": timestamps,
        "observed_at": observed.isoformat(),
        "pairs": chosen,
    }


def _strict_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo is not None else None


def _verified_pair(row: Any, *, now: datetime) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    source = row.get("source")
    rate = _valid_rate(row.get("rate"))
    quoted = _strict_timestamp(row.get("quote_at_utc"))
    observed = _strict_timestamp(row.get("observed_at_utc"))
    if (
        source not in {TENCENT_EXCHANGE_RATE_SOURCE, SINA_EXCHANGE_RATE_SOURCE}
        or rate is None or quoted is None or observed is None
        or quoted > observed or observed > now or quote_session_start(quoted) is None
    ):
        return None
    return {
        "rate": rate,
        "source": source,
        "quote_at_utc": quoted.isoformat(),
        "observed_at_utc": observed.isoformat(),
    }


def _verified_pairs(payload: Any, *, now: datetime) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return {}
    pairs = payload.get("pairs")
    if isinstance(pairs, Mapping):
        return {
            pair: verified
            for pair in _REQUIRED_RATES
            if (verified := _verified_pair(pairs.get(pair), now=now)) is not None
        }
    # Older observations qualify only when each pair has its own source time.
    source = payload.get("source")
    rates = payload.get("rates")
    timestamps = payload.get("quote_timestamps")
    observed = payload.get("observed_at")
    if not isinstance(rates, Mapping) or not isinstance(timestamps, Mapping):
        return {}
    return {
        pair: verified
        for pair in _REQUIRED_RATES
        if (verified := _verified_pair({
            "rate": rates.get(pair), "source": source,
            "quote_at_utc": timestamps.get(pair), "observed_at_utc": observed,
        }, now=now)) is not None
    }


def _newer_pair(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if existing is None or candidate["quote_at_utc"] > existing["quote_at_utc"]:
        return candidate
    if (candidate["quote_at_utc"] == existing["quote_at_utc"]
            and candidate["source"] == TENCENT_EXCHANGE_RATE_SOURCE
            and existing["source"] != TENCENT_EXCHANGE_RATE_SOURCE):
        return candidate
    return existing


def _pair_quality(row: dict[str, Any] | None, *, now: datetime) -> tuple[str, str]:
    if row is None:
        return "unavailable", "missing_verified_quote"
    return quote_quality(_strict_timestamp(row["quote_at_utc"]), at=now)


def shared_exchange_rate_cache_path(runtime_root: Path) -> Path:
    return Path(runtime_root).resolve() / "output_shared" / "state" / "rate_cache.json"


def verified_exchange_rate_pairs(payload: Any, *, observed_at: datetime) -> dict[str, dict[str, Any]]:
    """Original provider evidence, independent of its eligibility today."""
    return _verified_pairs(payload, now=observed_at)


def current_exchange_rate_snapshot(
    *, cache_path: Path, now: datetime | None = None, write_cache: bool = True,
) -> dict[str, Any]:
    """One request's verified quotes and purpose states; no invented quote times."""
    path = Path(cache_path).resolve()
    cached = _read_cache(path)
    fetched = fetch_market_exchange_rates()
    evaluated = (now or _utc_now()).astimezone(timezone.utc)
    selected = _verified_pairs(cached, now=evaluated)
    for pair, row in _verified_pairs(fetched, now=evaluated).items():
        selected[pair] = _newer_pair(selected.get(pair), row)
    if write_cache and selected:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A stable lock file protects the read/merge/write across processes.
        with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                latest = _verified_pairs(_read_cache(path), now=evaluated)
                for pair, row in selected.items():
                    latest[pair] = _newer_pair(latest.get(pair), row)
                atomic_write_json(path, {"schema_version": 2, "pairs": latest}, sort_keys=True)
                selected = latest
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    pairs: dict[str, dict[str, Any]] = {}
    for pair in _REQUIRED_RATES:
        row = selected.get(pair)
        quality, reason = _pair_quality(row, now=evaluated)
        pairs[pair] = {
            **(row or {}), "quality": quality, "reason": reason,
            "display_eligible": quality in {"fresh", "holiday_carried"},
            "capacity_eligible": quality in {"fresh", "holiday_carried"},
        }
    return {
        "schema_version": 2,
        "evaluated_at_utc": evaluated.isoformat(),
        "calendar": dict(CALENDAR_EVIDENCE),
        "pairs": pairs,
        "rates": {pair: row["rate"] for pair, row in pairs.items() if row["display_eligible"]},
    }


def _legacy_projection(
    snapshot: Mapping[str, Any], *, purpose: str | None = "capacity", now: datetime | None = None,
) -> dict[str, Any]:
    pairs = snapshot.get("pairs") or {}
    rates = (
        rates_for_purpose(snapshot, purpose=purpose, now=now)
        if purpose is not None else {pair: row["rate"] for pair, row in pairs.items()}
    )
    sources = {row.get("source") for row in pairs.values() if isinstance(row, Mapping) and row.get("source")}
    timestamps = {
        pair: row["quote_at_utc"] for pair, row in pairs.items()
        if isinstance(row, Mapping) and row.get("quote_at_utc")
    }
    return {
        "schema_version": 2,
        "pairs": pairs,
        "rates": rates,
        "source": next(iter(sources)) if len(sources) == 1 else "mixed",
        "timestamp": min(timestamps.values()) if timestamps else "",
        "quote_timestamps": timestamps,
        "observed_at": max(
            (row["observed_at_utc"] for row in pairs.values() if row.get("observed_at_utc")),
            default="",
        ),
    }


def rates_for_purpose(
    snapshot: Mapping[str, Any], *, purpose: str, now: datetime | None = None,
) -> dict[str, float]:
    """Recheck time-sensitive eligibility without refreshing a sealed quote."""
    if purpose not in {"display", "capacity"}:
        raise ValueError("FX purpose must be display or capacity")
    evaluated = (now or _utc_now()).astimezone(timezone.utc)
    pairs = snapshot.get("pairs") if isinstance(snapshot, Mapping) else None
    if not isinstance(pairs, Mapping):
        return {}
    out: dict[str, float] = {}
    for pair in _REQUIRED_RATES:
        row = _verified_pair(pairs.get(pair), now=evaluated)
        quality, _ = _pair_quality(row, now=evaluated)
        if row is not None and quality in {"fresh", "holiday_carried"}:
            out[pair] = row["rate"]
    return out


def project_exchange_rate_snapshot(
    snapshot: Mapping[str, Any], *, purpose: str, now: datetime | None = None,
) -> dict[str, Any]:
    """Project one quote snapshot for a consumer without fetching or changing it."""
    if purpose not in {"display", "capacity"}:
        raise ValueError("FX purpose must be display or capacity")
    evaluated = (now or _utc_now()).astimezone(timezone.utc)
    verified = _verified_pairs(snapshot, now=evaluated)
    pairs = {}
    for pair in _REQUIRED_RATES:
        row = verified.get(pair)
        quality, reason = _pair_quality(row, now=evaluated)
        pairs[pair] = {
            **(row or {}), "quality": quality, "reason": reason,
            "display_eligible": quality in {"fresh", "holiday_carried"},
            "capacity_eligible": quality in {"fresh", "holiday_carried"},
        }
    projection = _legacy_projection({"pairs": pairs}, purpose=purpose, now=evaluated)
    projection["evaluated_at_utc"] = evaluated.isoformat()
    projection["calendar"] = snapshot.get("calendar")
    return projection


def get_exchange_rates_or_fetch_latest(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    write_through_path: Path | None = None,
    log: Callable[[str], None] | None = None,
    write_cache: bool = True,
) -> dict | None:
    """Compatibility view of the single current FX owner; rates are capacity-safe."""
    del max_age_hours
    try:
        snapshot = current_exchange_rate_snapshot(cache_path=cache_path, write_cache=write_cache)
    except OSError as exc:
        _warn(log, f"[WARN] FX cache unavailable: {exc}")
        return None
    if write_through_path is not None:
        try:
            atomic_write_json(write_through_path, snapshot, sort_keys=True)
        except OSError as exc:
            _warn(log, f"[WARN] FX run copy unavailable: {exc}")
    return _legacy_projection(snapshot) if any(snapshot["pairs"][pair].get("rate") for pair in _REQUIRED_RATES) else None


def load_exchange_rate_info(
    *,
    cache_path: Path,
    max_age_hours: int | None = None,
    fetch_latest_on_miss: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    del fetch_latest_on_miss, log, max_age_hours
    cached = get_cached_exchange_rates(cache_path=cache_path)
    return project_exchange_rate_snapshot(cached, purpose="display") if cached else None


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

    The argument binds the runtime root; quote eligibility is owned by the
    shared market-rate snapshot, including verified holiday carry.
    """
    try:
        base_dir = Path(base_dir).resolve()
        obj = get_exchange_rates_or_fetch_latest(
            cache_path=shared_exchange_rate_cache_path(base_dir),
            max_age_hours=24,
        )
        usdcny = _extract_usdcny_from_rates(obj)
        if usdcny is None or usdcny <= 0:
            return None
        return 1.0 / usdcny
    except Exception:
        return None
