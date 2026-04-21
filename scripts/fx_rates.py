"""FX conversion utilities (Stage 2).

Goal: centralize FX math so that call-sites don't replicate USD/HKD/CNY conversions.

Conventions:
- fx_usd_per_cny: USD per 1 CNY (e.g., 0.14)
- hkdcny: CNY per 1 HKD (e.g., 0.92)

This module is intentionally minimal; expand only as needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import importlib.util
import json
import sys
from typing import Callable


@dataclass(frozen=True)
class FxRates:
    usd_per_cny: float | None = None
    cny_per_hkd: float | None = None


@dataclass(frozen=True)
class CurrencyConverter:
    """Convert between base CNY and option native currencies (USD/HKD)."""

    rates: FxRates

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
        # hkdcny is CNY per 1 HKD
        hkdcny = self.rates.cny_per_hkd
        if hkdcny is None or hkdcny <= 0:
            return None
        return float(cny) / float(hkdcny)

    def hkd_to_cny(self, hkd: float) -> float | None:
        hkdcny = self.rates.cny_per_hkd
        if hkdcny is None or hkdcny <= 0:
            return None
        return float(hkd) * float(hkdcny)

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


def get_rates(
    *,
    cache_path: Path,
    shared_cache_path: Path | None = None,
    max_age_hours: int | None = None,
) -> dict | None:
    """Read cached FX rates (best-effort).

    Backward compatible with existing call-sites that pass:
      - cache_path=...
      - shared_cache_path=...
      - max_age_hours=...

    We currently ignore max_age_hours (the callers already treat FX as best-effort).
    Resolution preference:
      1) local cache_path
      2) shared_cache_path (portfolio-management)
    """
    def _read(p: Path) -> dict | None:
        try:
            p = Path(p).resolve()
            if not p.exists() or p.stat().st_size <= 0:
                return None
            obj = json.loads(p.read_text(encoding='utf-8'))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    r = _read(Path(cache_path))
    if r is not None:
        return r
    if shared_cache_path:
        return _read(Path(shared_cache_path))
    return None


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
        _warn(log, f"[WARN] fx cache write failed: path={path} error={exc}")


def _fetch_latest_rates_from_portfolio_management(*, log: Callable[[str], None] | None = None) -> dict | None:
    try:
        base_dir = Path(__file__).resolve().parents[2]
        pm_root = (base_dir / "portfolio-management").resolve()
        src_root = (pm_root / "src").resolve()
        if not src_root.exists():
            _warn(log, f"[WARN] fx external ref missing: portfolio-management src not found at {src_root}")
            return None

        if str(pm_root) not in sys.path:
            sys.path.insert(0, str(pm_root))

        mod_path = src_root / "price_fetcher.py"
        spec = importlib.util.spec_from_file_location("pm_price_fetcher", mod_path)
        if spec is None or spec.loader is None:
            _warn(log, f"[WARN] fx external import failed: cannot load spec for {mod_path}")
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "PriceFetcher"):
            _warn(log, f"[WARN] fx external interface changed: PriceFetcher missing in {mod_path}")
            return None
        fetcher = mod.PriceFetcher(storage=None, use_cache=False)
        if not hasattr(fetcher, "_fetch_exchange_rates"):
            _warn(log, "[WARN] fx external interface changed: PriceFetcher._fetch_exchange_rates missing")
            return None
        rates = fetcher._fetch_exchange_rates()
        if not isinstance(rates, dict):
            _warn(log, f"[WARN] fx external payload invalid: expected dict got {type(rates).__name__}")
            return None
        if rates.get("USDCNY") is None or rates.get("HKDCNY") is None:
            _warn(log, f"[WARN] fx external payload incomplete: keys={sorted(rates.keys())}")
            return None
        return {
            "rates": {
                "USDCNY": rates.get("USDCNY"),
                "HKDCNY": rates.get("HKDCNY"),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        _warn(log, f"[WARN] fx external fetch failed: error={exc}")
        return None


def get_rates_or_fetch_latest(
    *,
    cache_path: Path,
    shared_cache_path: Path | None = None,
    max_age_hours: int | None = None,
    write_through_path: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    cached = get_rates(
        cache_path=cache_path,
        shared_cache_path=shared_cache_path,
        max_age_hours=max_age_hours,
    )
    if cached is not None:
        return cached

    _warn(
        log,
        f"[WARN] fx cache miss: cache_path={Path(cache_path).resolve()} shared_cache_path={Path(shared_cache_path).resolve() if shared_cache_path else '-'}; trying latest fetch",
    )
    latest = _fetch_latest_rates_from_portfolio_management(log=log)
    if latest is None:
        _warn(log, "[WARN] fx latest fetch unavailable: no cache and external fallback failed")
        return None

    rates = latest.get("rates")
    if isinstance(rates, dict):
        target = write_through_path or cache_path
        _save_rates(Path(target), rates, log=log)
    return latest


def load_fx_info(
    *,
    cache_path: Path,
    shared_cache_path: Path | None = None,
    max_age_hours: int | None = None,
    fetch_latest_on_miss: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    if fetch_latest_on_miss:
        return get_rates_or_fetch_latest(
            cache_path=cache_path,
            shared_cache_path=shared_cache_path,
            max_age_hours=max_age_hours,
            log=log,
        )
    return get_rates(
        cache_path=cache_path,
        shared_cache_path=shared_cache_path,
        max_age_hours=max_age_hours,
    )


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


def get_usd_per_cny(base_dir: Path) -> float | None:
    """Return USD per 1 CNY from rate_cache.json.

    rate_cache stores USDCNY (CNY per 1 USD). We invert it.

    NOTE: For backward compatibility this function keeps the original signature.
    It will try multiple cache locations:
      1) <base_dir>/output/state/rate_cache.json (legacy)
      2) <base_dir>/output_shared/state/rate_cache.json (shared)
      3) <base_dir>/../portfolio-management/.data/rate_cache.json (shared)
    """
    try:
        base_dir = Path(base_dir).resolve()
        obj = get_rates_or_fetch_latest(
            cache_path=(base_dir / 'output' / 'state' / 'rate_cache.json').resolve(),
            shared_cache_path=(base_dir / 'output_shared' / 'state' / 'rate_cache.json').resolve(),
            max_age_hours=24,
        )
        usdcny = _extract_usdcny_from_rates(obj)
        if usdcny is None or usdcny <= 0:
            return None
        return 1.0 / usdcny
    except Exception:
        return None
