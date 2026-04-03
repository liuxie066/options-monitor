#!/usr/bin/env python3
from __future__ import annotations

"""Multiplier static cache.

Why:
- Contract multiplier (shares per contract) is largely static for an underlier.
- HK options may have multiplier != 100.
- Fetching option chain just to discover multiplier is wasteful and triggers OpenD rate limits.

Cache file (JSON):
{
  "0700.HK": {"multiplier": 500, "as_of_utc": "2026-03-27T05:50:00+00:00", "source": "opend"}
}

This module provides:
- load/save/get
- manual refresh via OpenD (best-effort)
"""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.io_utils import utc_now


def default_cache_path(repo_base: Path) -> Path:
    return (repo_base / "output_shared" / "state" / "multiplier_cache.json").resolve()


def load_cache(path: Path) -> dict:
    try:
        if path.exists() and path.stat().st_size > 0:
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def get_cached_multiplier(cache: dict, symbol: str) -> int | None:
    sym = normalize_symbol(symbol)
    try:
        v = cache.get(sym)
        if not isinstance(v, dict):
            return None
        m = int(v.get("multiplier") or 0)
        return m if m > 0 else None
    except Exception:
        return None


@dataclass
class RefreshResult:
    symbol: str
    ok: bool
    multiplier: int | None = None
    error: str | None = None


def refresh_via_opend(*, repo_base: Path, symbol: str, host: str = "127.0.0.1", port: int = 11111, limit_expirations: int = 1) -> RefreshResult:
    """Refresh multiplier by calling OpenD once for this underlier.

    We import and call fetch_symbol() directly to avoid writing output files.
    """
    sym = normalize_symbol(symbol)
    try:
        # Local import to avoid futu dependency for non-refresh paths.
        # Ensure repo root is on sys.path so `import scripts.*` works when running as a script.
        import sys
        if str(repo_base) not in sys.path:
            sys.path.insert(0, str(repo_base))

        from scripts.fetch_market_data_opend import fetch_symbol  # type: ignore

        payload = fetch_symbol(
            sym,
            limit_expirations=int(limit_expirations),
            host=str(host),
            port=int(port),
            spot_override=None,
            spot_from_pm=False,
            base_dir=None,
        )
        rows = payload.get("rows") or []
        m = None
        for r in rows:
            try:
                mv = r.get("multiplier")
                if mv is None:
                    continue
                m0 = int(float(mv))
                if m0 > 0:
                    m = m0
                    break
            except Exception:
                continue
        if not m:
            return RefreshResult(symbol=sym, ok=False, multiplier=None, error="multiplier_not_found")
        return RefreshResult(symbol=sym, ok=True, multiplier=int(m))
    except Exception as e:
        return RefreshResult(symbol=sym, ok=False, multiplier=None, error=f"{type(e).__name__}: {e}")


def cmd_list(cache_path: Path):
    cache = load_cache(cache_path)
    keys = sorted(cache.keys())
    print(json.dumps({k: cache[k] for k in keys}, ensure_ascii=False, indent=2))


def cmd_get(cache_path: Path, symbol: str):
    cache = load_cache(cache_path)
    sym = normalize_symbol(symbol)
    v = cache.get(sym)
    print(json.dumps({sym: v}, ensure_ascii=False, indent=2))


def cmd_refresh(cache_path: Path, symbols: list[str], *, host: str, port: int, limit_expirations: int, force: bool):
    cache = load_cache(cache_path)
    updated = 0
    results: list[RefreshResult] = []

    for s in symbols:
        sym = normalize_symbol(s)
        if not sym:
            continue
        if (not force) and get_cached_multiplier(cache, sym):
            results.append(RefreshResult(symbol=sym, ok=True, multiplier=get_cached_multiplier(cache, sym), error="cached"))
            continue

        r = refresh_via_opend(repo_base=Path(__file__).resolve().parents[1], symbol=sym, host=host, port=port, limit_expirations=limit_expirations)
        results.append(r)
        if r.ok and r.multiplier:
            cache[sym] = {"multiplier": int(r.multiplier), "as_of_utc": utc_now(), "source": "opend"}
            updated += 1

    if updated:
        save_cache(cache_path, cache)

    # summary JSON (machine-friendly)
    out = {
        "cache_path": str(cache_path),
        "updated": updated,
        "results": [r.__dict__ for r in results],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Static multiplier cache (output_shared/state/multiplier_cache.json)")
    ap.add_argument("--cache", default=None, help="Cache file path (default: <repo>/output_shared/state/multiplier_cache.json)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")

    p_get = sub.add_parser("get")
    p_get.add_argument("symbol")

    p_ref = sub.add_parser("refresh")
    p_ref.add_argument("--symbols", nargs="+", required=True)
    p_ref.add_argument("--host", default="127.0.0.1")
    p_ref.add_argument("--port", type=int, default=11111)
    p_ref.add_argument("--limit-expirations", type=int, default=1)
    p_ref.add_argument("--force", action="store_true")

    args = ap.parse_args()

    repo_base = Path(__file__).resolve().parents[1]
    cache_path = Path(args.cache).expanduser().resolve() if args.cache else default_cache_path(repo_base)

    if args.cmd == "list":
        cmd_list(cache_path)
        return
    if args.cmd == "get":
        cmd_get(cache_path, args.symbol)
        return
    if args.cmd == "refresh":
        cmd_refresh(cache_path, args.symbols, host=args.host, port=args.port, limit_expirations=args.limit_expirations, force=bool(args.force))
        return


if __name__ == "__main__":
    main()
