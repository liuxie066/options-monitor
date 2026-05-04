#!/usr/bin/env python3
from __future__ import annotations

"""Multiplier cache and resolver.

Why:
- Contract multiplier (shares per contract) is largely static for an underlier.
- HK options may have multiplier != 100.
- Fetching option chain just to discover multiplier is wasteful and triggers OpenD rate limits.

Cache file (JSON):
{
  "0700.HK": {"multiplier": 500, "as_of_utc": "2026-03-27T05:50:00+00:00", "source": "opend"}
}

This module provides:
- cache read/write helpers
- unified multiplier resolution
- optional OpenD refresh when cache is missing
"""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]

from scripts.io_utils import utc_now
from scripts.opend_utils import resolve_underlier_alias
from src.application.opend_fetch_config import filter_opend_fetch_kwargs


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


def _cache_lock_path(path: Path) -> Path:
    return Path(path).with_suffix(Path(path).suffix + ".lock")


def _write_cache_unlocked(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_cache(path: Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _cache_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is not None:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            _write_cache_unlocked(path, cache)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def merge_cache_updates(path: Path, updates: dict) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _cache_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is not None:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            cache = load_cache(path)
            cache.update({k: v for k, v in dict(updates or {}).items() if k})
            _write_cache_unlocked(path, cache)
            return cache
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def normalize_symbol(symbol: str) -> str:
    sym = resolve_underlier_alias(str(symbol or ""))
    if sym.endswith(".HK"):
        code = sym[:-3]
        if code.isdigit():
            return f"{int(code):04d}.HK"
    return sym


def _symbol_aliases(symbol: str) -> list[str]:
    sym = normalize_symbol(symbol)
    out = [sym]
    if sym.endswith(".HK"):
        code = sym[:-3]
        if code.isdigit():
            out.append(f"{int(code):05d}.HK")
    return list(dict.fromkeys(out))


def get_cached_multiplier(cache: dict, symbol: str) -> int | None:
    for sym in _symbol_aliases(symbol):
        try:
            v = cache.get(sym)
            if not isinstance(v, dict):
                continue
            m = int(v.get("multiplier") or 0)
            if m > 0:
                return m
        except Exception:
            continue
    return None


@dataclass
class RefreshResult:
    symbol: str
    ok: bool
    multiplier: int | None = None
    error: str | None = None


def get_cached_multiplier_source(cache: dict, symbol: str) -> str | None:
    for sym in _symbol_aliases(symbol):
        item = cache.get(sym)
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("multiplier") or 0) > 0:
                return str(item.get("source") or "cache")
        except Exception:
            continue
    return None


def _positive_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        out = int(float(value))
        if out > 0:
            return out
    except Exception:
        return None
    return None


def _intake_config_candidates(
    *,
    repo_base: Path,
    config: dict[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    cfg = config if isinstance(config, dict) else {}
    intake = cfg.get("intake") if isinstance(cfg.get("intake"), dict) else None
    if isinstance(intake, dict):
        candidates.append(("config", intake))
    else:
        candidates.append(("config", {}))

    for filename in ("config.hk.json", "config.us.json"):
        path = Path(repo_base) / filename
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        file_intake = obj.get("intake") if isinstance(obj, dict) else None
        if isinstance(file_intake, dict):
            candidates.append((f"config-file:{filename}", file_intake))
    return candidates


def _static_config_multiplier(
    *,
    repo_base: Path,
    config: dict[str, Any] | None,
    symbol: str,
) -> tuple[int | None, str | None, dict[str, Any]]:
    diagnostics: dict[str, Any] = {"attempted_sources": []}
    sym = normalize_symbol(symbol)

    for prefix, intake in _intake_config_candidates(repo_base=repo_base, config=config):
        by_symbol = intake.get("multiplier_by_symbol")
        source = f"{prefix}:intake.multiplier_by_symbol"
        if isinstance(by_symbol, dict):
            normalized_map = {
                normalize_symbol(str(key or "")): value
                for key, value in by_symbol.items()
                if str(key or "").strip()
            }
            value = _positive_int(normalized_map.get(sym))
            if value is not None:
                diagnostics["attempted_sources"].append({"source": source, "status": "resolved", "value": value})
                return value, source, diagnostics
            diagnostics["attempted_sources"].append({"source": source, "status": "miss", "symbol": sym})
        else:
            diagnostics["attempted_sources"].append({"source": source, "status": "missing_config"})

        default_key = "default_multiplier_hk" if sym.endswith(".HK") else "default_multiplier_us"
        source = f"{prefix}:intake.{default_key}"
        value = _positive_int(intake.get(default_key))
        if value is not None:
            diagnostics["attempted_sources"].append({"source": source, "status": "resolved", "value": value})
            return value, source, diagnostics
        diagnostics["attempted_sources"].append({"source": source, "status": "missing_or_invalid"})
    return None, None, diagnostics


def resolve_multiplier_with_source_and_diagnostics(
    *,
    repo_base: Path,
    symbol: str | None,
    multiplier: int | float | None = None,
    allow_opend_refresh: bool = False,
    host: str = "127.0.0.1",
    port: int = 11111,
    limit_expirations: int = 1,
    opend_fetch_config: dict[str, float | int] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[int | None, str | None, dict[str, Any]]:
    sym = normalize_symbol(str(symbol or ""))
    diagnostics: dict[str, Any] = {
        "canonical_symbol": sym,
        "selected_source": None,
        "attempted_sources": [],
    }

    explicit = _positive_int(multiplier)
    if explicit is not None:
        diagnostics["selected_source"] = "payload"
        diagnostics["attempted_sources"].append({"source": "payload", "status": "resolved", "value": explicit})
        return explicit, "payload", diagnostics
    diagnostics["attempted_sources"].append(
        {
            "source": "payload",
            "status": "missing" if multiplier in (None, "") else "invalid",
        }
    )

    if not sym:
        diagnostics["attempted_sources"].append({"source": "contract_metadata", "status": "skipped_no_symbol"})
        return None, None, diagnostics

    cache_path = default_cache_path(repo_base)
    cache = load_cache(cache_path)
    cached = get_cached_multiplier(cache, sym)
    if cached:
        source = get_cached_multiplier_source(cache, sym) or "cache"
        diagnostics["selected_source"] = source
        diagnostics["attempted_sources"].append({"source": "cache", "status": "resolved", "value": int(cached)})
        return int(cached), source, diagnostics
    diagnostics["attempted_sources"].append({"source": "cache", "status": "miss"})

    if allow_opend_refresh:
        refreshed = refresh_via_opend(
            repo_base=repo_base,
            symbol=sym,
            host=host,
            port=port,
            limit_expirations=limit_expirations,
            opend_fetch_config=opend_fetch_config,
        )
        if refreshed.ok and refreshed.multiplier and int(refreshed.multiplier) > 0:
            update = store_multiplier({}, sym, int(refreshed.multiplier), source="opend")
            merge_cache_updates(cache_path, update)
            diagnostics["selected_source"] = "opend"
            diagnostics["attempted_sources"].append({"source": "opend", "status": "resolved", "value": int(refreshed.multiplier)})
            return int(refreshed.multiplier), "opend", diagnostics
        diagnostics["attempted_sources"].append(
            {
                "source": "opend",
                "status": "miss" if not refreshed.error else "error",
                "error": refreshed.error,
            }
        )
    else:
        diagnostics["attempted_sources"].append({"source": "opend", "status": "skipped"})

    static_value, static_source, static_diagnostics = _static_config_multiplier(
        repo_base=repo_base,
        config=config,
        symbol=sym,
    )
    diagnostics["attempted_sources"].extend(static_diagnostics.get("attempted_sources") or [])
    if static_value is not None and static_source:
        diagnostics["selected_source"] = static_source
        return int(static_value), static_source, diagnostics

    return None, None, diagnostics


def refresh_via_opend(
    *,
    repo_base: Path,
    symbol: str,
    host: str = "127.0.0.1",
    port: int = 11111,
    limit_expirations: int = 1,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> RefreshResult:
    """Refresh multiplier by calling OpenD once for this underlier.

    We call the request-based OpenD fetcher directly to avoid writing output files.
    """
    sym = normalize_symbol(symbol)
    try:
        # Local import to avoid futu dependency for non-refresh paths.
        # Ensure repo root is on sys.path so `import scripts.*` works when running as a script.
        import sys
        if str(repo_base) not in sys.path:
            sys.path.insert(0, str(repo_base))

        from src.application.opend_symbol_fetching import FetchSymbolRequest, fetch_symbol_request  # type: ignore

        payload = fetch_symbol_request(
            FetchSymbolRequest(
                symbol=sym,
                limit_expirations=int(limit_expirations),
                host=str(host),
                port=int(port),
                spot_override=None,
                base_dir=Path(repo_base),
                **filter_opend_fetch_kwargs(opend_fetch_config),
            )
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


def store_multiplier(cache: dict, symbol: str, multiplier: int, *, source: str = "opend") -> dict:
    cache[normalize_symbol(symbol)] = {
        "multiplier": int(multiplier),
        "as_of_utc": utc_now(),
        "source": str(source),
    }
    return cache


def resolve_multiplier_with_source(
    *,
    repo_base: Path,
    symbol: str | None,
    multiplier: int | float | None = None,
    allow_opend_refresh: bool = False,
    host: str = "127.0.0.1",
    port: int = 11111,
    limit_expirations: int = 1,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> tuple[int | None, str | None]:
    value, source, _diagnostics = resolve_multiplier_with_source_and_diagnostics(
        repo_base=repo_base,
        symbol=symbol,
        multiplier=multiplier,
        allow_opend_refresh=allow_opend_refresh,
        host=host,
        port=port,
        limit_expirations=limit_expirations,
        opend_fetch_config=opend_fetch_config,
    )
    return value, source


def resolve_multiplier(
    *,
    repo_base: Path,
    symbol: str | None,
    multiplier: int | float | None = None,
    allow_opend_refresh: bool = False,
    host: str = "127.0.0.1",
    port: int = 11111,
    limit_expirations: int = 1,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> int | None:
    value, _source = resolve_multiplier_with_source(
        repo_base=repo_base,
        symbol=symbol,
        multiplier=multiplier,
        allow_opend_refresh=allow_opend_refresh,
        host=host,
        port=port,
        limit_expirations=limit_expirations,
        opend_fetch_config=opend_fetch_config,
    )
    return value


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
            store_multiplier(cache, sym, int(r.multiplier), source="opend")
            updated += 1

    if updated:
        updates = {
            normalize_symbol(r.symbol): cache[normalize_symbol(r.symbol)]
            for r in results
            if r.ok and r.multiplier and normalize_symbol(r.symbol) in cache
        }
        merge_cache_updates(cache_path, updates)

    # summary JSON (machine-friendly)
    out = {
        "cache_path": str(cache_path),
        "updated": updated,
        "results": [r.__dict__ for r in results],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Multiplier cache and resolver (output_shared/state/multiplier_cache.json)")
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
