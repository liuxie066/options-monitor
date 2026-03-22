#!/usr/bin/env python3
from __future__ import annotations

"""FX rates helper (with local + shared cache).

Goal: reuse portfolio-management's rate cache format (.data/rate_cache.json) to avoid
extra network calls and keep a single source of truth for USDCNY/HKDCNY.

Cache format (same as portfolio-management):
{
  "rates": {"USDCNY": 6.89, "HKDCNY": 0.88},
  "timestamp": "2026-03-22T01:00:00"
}

Notes:
- We only need USDCNY (CNY per 1 USD). USD-per-CNY = 1/USDCNY.
- Fetch uses exchangerate-api.com first; if blocked, tries a Sina FX fallback.
- No API keys required.
"""

import argparse
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class RateCache:
    rates: dict[str, float]
    timestamp: datetime | None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both naive and offset timestamps.
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_rate_cache(path: Path) -> RateCache | None:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        rates = data.get("rates") or {}
        ts = _parse_dt(data.get("timestamp"))
        if not isinstance(rates, dict) or not rates:
            return None
        out: dict[str, float] = {}
        for k, v in rates.items():
            try:
                out[str(k).strip().upper()] = float(v)
            except Exception:
                continue
        return RateCache(rates=out, timestamp=ts)
    except Exception:
        return None


def save_rate_cache(path: Path, rates: dict[str, float]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "rates": rates,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _http_json(url: str, timeout: int = 12) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_usdcny_exchangerate_api() -> float:
    # Returns CNY per 1 USD
    data = _http_json("https://api.exchangerate-api.com/v4/latest/USD")
    return float(data["rates"]["CNY"])


def fetch_hkdcny_exchangerate_api() -> float:
    data = _http_json("https://api.exchangerate-api.com/v4/latest/HKD")
    return float(data["rates"]["CNY"])


def fetch_usdcny_sina() -> float:
    # Sina: var hq_str_fx_susdcny="buy,?,?,sell,..."
    url = "https://hq.sinajs.cn/list=fx_susdcny"
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        text = resp.read().decode("gbk", errors="ignore")
    if '"' not in text:
        raise ValueError("sina fx: unexpected response")
    raw = text.split('"', 1)[1].split('"', 1)[0]
    parts = raw.split(',')
    if len(parts) < 3:
        raise ValueError("sina fx: parse failed")
    buy = float(parts[0])
    sell = float(parts[2])
    return (buy + sell) / 2.0


def fetch_hkdcny_sina() -> float:
    url = "https://hq.sinajs.cn/list=fx_shkdcny"
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        text = resp.read().decode("gbk", errors="ignore")
    if '"' not in text:
        raise ValueError("sina fx: unexpected response")
    raw = text.split('"', 1)[1].split('"', 1)[0]
    parts = raw.split(',')
    if len(parts) < 3:
        raise ValueError("sina fx: parse failed")
    buy = float(parts[0])
    sell = float(parts[2])
    return (buy + sell) / 2.0


def get_rates(cache_path: Path, shared_cache_path: Path | None = None, max_age_hours: int = 24) -> dict[str, float]:
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=max_age_hours)

    candidates: list[tuple[Path, RateCache]] = []
    for p in [cache_path, shared_cache_path]:
        if not p:
            continue
        c = load_rate_cache(p)
        if not c:
            continue
        candidates.append((p, c))

    # pick freshest valid within max_age
    best: RateCache | None = None
    for _, c in candidates:
        if c.timestamp and (now - c.timestamp) <= max_age and c.rates.get('USDCNY'):
            if (best is None) or (best.timestamp is None) or (c.timestamp and c.timestamp > best.timestamp):
                best = c

    if best:
        return best.rates

    # fetch realtime (minimal subset)
    last_err = None
    for attempt in range(2):
        try:
            usdcny = fetch_usdcny_exchangerate_api()
            hkdcny = fetch_hkdcny_exchangerate_api()
            rates = {"USDCNY": round(usdcny, 4), "HKDCNY": round(hkdcny, 4)}
            save_rate_cache(cache_path, rates)
            return rates
        except Exception as e:
            last_err = e

    # fallback to sina
    try:
        rates = {"USDCNY": round(fetch_usdcny_sina(), 4), "HKDCNY": round(fetch_hkdcny_sina(), 4)}
        save_rate_cache(cache_path, rates)
        return rates
    except Exception as e:
        last_err = e

    # final fallback: any cached even if stale
    for _, c in candidates:
        if c.rates.get('USDCNY'):
            return c.rates

    raise RuntimeError(f"failed to get FX rates: {last_err}")


def usd_per_cny(rates: dict[str, float]) -> float | None:
    v = rates.get('USDCNY')
    if not v:
        return None
    try:
        v = float(v)
        if v <= 0:
            return None
        return 1.0 / v
    except Exception:
        return None


def get_usd_per_cny(
    base_dir: Path,
    max_age_hours: int = 24,
    out_rel: str = 'output/state/rate_cache.json',
) -> float | None:
    """Get USD-per-CNY using shared (portfolio-management) cache when available."""
    out_path = (base_dir / out_rel).resolve()
    workspace = Path(__file__).resolve().parents[2]
    shared_path = workspace / 'portfolio-management' / '.data' / 'rate_cache.json'
    rates = get_rates(out_path, shared_cache_path=shared_path, max_age_hours=max_age_hours)
    return usd_per_cny(rates)


def main():
    parser = argparse.ArgumentParser(description="Fetch FX rates (USDCNY/HKDCNY) with cache")
    parser.add_argument('--out', default='output/state/rate_cache.json')
    parser.add_argument('--max-age-hours', type=int, default=24)
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    out_path = base / args.out

    workspace = Path(__file__).resolve().parents[2]
    shared_path = workspace / 'portfolio-management' / '.data' / 'rate_cache.json'

    rates = get_rates(out_path, shared_cache_path=shared_path, max_age_hours=args.max_age_hours)
    print(json.dumps({'rates': rates, 'usd_per_cny': usd_per_cny(rates)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
