#!/usr/bin/env python3
from __future__ import annotations

"""CLI adapter for fetching required option data from Futu OpenD."""

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application import opend_symbol_fetching as _fetching

FetchSymbolRequest = _fetching.FetchSymbolRequest
COLUMNS = _fetching.COLUMNS
_append_metrics_json = _fetching._append_metrics_json
_chain_cache_covers_explicit_expirations = _fetching._chain_cache_covers_explicit_expirations
_chain_cache_path = _fetching._chain_cache_path
_is_chain_cache_fresh = _fetching._is_chain_cache_fresh
_load_chain_cache = _fetching._load_chain_cache
_prune_chain_cache = _fetching._prune_chain_cache
_save_chain_cache = _fetching._save_chain_cache
calc_mid = _fetching.calc_mid
get_spot_opend = _fetching.get_spot_opend
rate_limited_opend_call = _fetching.rate_limited_opend_call
save_outputs = _fetching.save_outputs

# Backward-compatible monkeypatch surface for tests and ad-hoc callers that patch
# dependencies on this script module.
build_ready_futu_gateway = _fetching.build_ready_futu_gateway
retry_futu_gateway_call = _fetching.retry_futu_gateway_call
normalize_underlier = _fetching.normalize_underlier
get_trading_date = _fetching.get_trading_date


def _call_with_compat_hooks(fn, *args, **kwargs):
    hooks = {
        "build_ready_futu_gateway": build_ready_futu_gateway,
        "retry_futu_gateway_call": retry_futu_gateway_call,
        "normalize_underlier": normalize_underlier,
        "get_trading_date": get_trading_date,
        "rate_limited_opend_call": rate_limited_opend_call,
        "get_spot_opend": get_spot_opend,
    }
    old = {name: getattr(_fetching, name) for name in hooks}
    try:
        for name, value in hooks.items():
            setattr(_fetching, name, value)
        return fn(*args, **kwargs)
    finally:
        for name, value in old.items():
            setattr(_fetching, name, value)


def fetch_symbol_request(request: FetchSymbolRequest) -> dict:
    return _call_with_compat_hooks(_fetching.fetch_symbol_request, request)


def get_underlier_spot(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    base_dir: Path | None = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
) -> float | None:
    return _call_with_compat_hooks(
        _fetching.get_underlier_spot,
        symbol,
        host=host,
        port=port,
        base_dir=base_dir,
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    )


def list_option_expirations(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    base_dir: Path | None = None,
    expiration_max_wait_sec: float = 30.0,
    expiration_window_sec: float = 30.0,
    expiration_max_calls: int = 30,
) -> list[str]:
    return _call_with_compat_hooks(
        _fetching.list_option_expirations,
        symbol,
        host=host,
        port=port,
        base_dir=base_dir,
        expiration_max_wait_sec=expiration_max_wait_sec,
        expiration_window_sec=expiration_window_sec,
        expiration_max_calls=expiration_max_calls,
    )


def fetch_symbol(
    symbol: str,
    limit_expirations: int | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    spot_override: float | None = None,
    *,
    base_dir: Path | None = None,
    option_types: str = "put,call",
    min_strike: float | None = None,
    max_strike: float | None = None,
    side_strike_windows: dict[str, dict[str, float | None]] | None = None,
    min_dte: int | None = None,
    max_dte: int | None = None,
    explicit_expirations: list[str] | None = None,
    retry_max_attempts: int = 4,
    retry_time_budget_sec: float = 8.0,
    retry_base_delay_sec: float = 0.8,
    retry_max_delay_sec: float = 6.0,
    no_retry: bool = False,
    chain_cache: bool = False,
    chain_cache_force_refresh: bool = False,
    freshness_policy: str = "cache_first",
    max_wait_sec: float = 90.0,
    option_chain_window_sec: float = 30.0,
    option_chain_max_calls: int = 10,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
    expiration_max_wait_sec: float = 30.0,
    expiration_window_sec: float = 30.0,
    expiration_max_calls: int = 30,
) -> dict:
    return fetch_symbol_request(
        FetchSymbolRequest(
            symbol=symbol,
            limit_expirations=limit_expirations,
            host=host,
            port=port,
            spot_override=spot_override,
            base_dir=base_dir,
            option_types=option_types,
            min_strike=min_strike,
            max_strike=max_strike,
            side_strike_windows=side_strike_windows,
            min_dte=min_dte,
            max_dte=max_dte,
            explicit_expirations=explicit_expirations,
            retry_max_attempts=retry_max_attempts,
            retry_time_budget_sec=retry_time_budget_sec,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            no_retry=no_retry,
            chain_cache=chain_cache,
            chain_cache_force_refresh=chain_cache_force_refresh,
            freshness_policy=freshness_policy,
            max_wait_sec=max_wait_sec,
            option_chain_window_sec=option_chain_window_sec,
            option_chain_max_calls=option_chain_max_calls,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
            expiration_max_wait_sec=expiration_max_wait_sec,
            expiration_window_sec=expiration_window_sec,
            expiration_max_calls=expiration_max_calls,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch required option data from Futu OpenD")
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--limit-expirations", type=int, default=2)
    ap.add_argument("--chain-cache", action="store_true", help="Enable option_chain day-cache (per underlier) to reduce OpenD calls")
    ap.add_argument("--chain-cache-force-refresh", action="store_true", help="Force refresh option_chain even if cache is fresh")
    ap.add_argument("--chain-cache-keep-days", type=int, default=7, help="Keep N days of option_chain cache files (default: 7)")
    ap.add_argument("--option-types", default="put,call", help="Comma-separated option types to include: put,call (default: put,call)")
    ap.add_argument("--min-strike", type=float, default=None)
    ap.add_argument("--max-strike", type=float, default=None)
    ap.add_argument("--min-dte", type=int, default=None, help="Only pick expirations with DTE >= min_dte before applying limit-expirations")
    ap.add_argument("--max-dte", type=int, default=None, help="Only pick expirations with DTE <= max_dte before applying limit-expirations")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--spot", type=float, default=None, help="override spot if OpenD has no quote right")
    ap.add_argument("--quiet", action="store_true", help="quiet mode: suppress non-critical prints")
    ap.add_argument("--no-retry", action="store_true", help="Disable OpenD retries/backoff")
    ap.add_argument("--retry-max-attempts", type=int, default=4)
    ap.add_argument("--retry-time-budget-sec", type=float, default=8.0)
    ap.add_argument("--retry-base-delay-sec", type=float, default=0.8)
    ap.add_argument("--retry-max-delay-sec", type=float, default=6.0)
    ap.add_argument("--option-chain-max-wait-sec", type=float, default=90.0, help="Max seconds to wait for shared option-chain rate-limit budget")
    ap.add_argument("--option-chain-window-sec", type=float, default=30.0, help="Shared option-chain rate-limit window seconds")
    ap.add_argument("--option-chain-max-calls", type=int, default=10, help="Shared option-chain max calls per window")
    ap.add_argument("--snapshot-max-wait-sec", type=float, default=30.0, help="Max seconds to wait for shared market-snapshot rate-limit budget")
    ap.add_argument("--snapshot-window-sec", type=float, default=30.0, help="Shared market-snapshot rate-limit window seconds")
    ap.add_argument("--snapshot-max-calls", type=int, default=60, help="Shared market-snapshot max calls per window")
    ap.add_argument("--expiration-max-wait-sec", type=float, default=30.0, help="Max seconds to wait for shared option-expiration rate-limit budget")
    ap.add_argument("--expiration-window-sec", type=float, default=30.0, help="Shared option-expiration rate-limit window seconds")
    ap.add_argument("--expiration-max-calls", type=int, default=30, help="Shared option-expiration max calls per window")
    ap.add_argument("--output-root", default=None, help="Output root containing raw/ and parsed/ (default: ./output)")
    args = ap.parse_args()

    opt_types = {s.strip().lower() for s in str(args.option_types or "").split(",") if s.strip()}
    want_put = ("put" in opt_types) if opt_types else True
    want_call = ("call" in opt_types) if opt_types else True

    base = REPO_ROOT
    output_root = Path(args.output_root).resolve() if args.output_root else None

    if args.chain_cache:
        _prune_chain_cache(base, int(args.chain_cache_keep_days))

    opend_metrics_path = (base / "output_shared" / "state" / "opend_metrics.json").resolve()

    for sym in args.symbols:
        t0 = time.monotonic()
        payload = fetch_symbol_request(
            FetchSymbolRequest(
                symbol=sym,
                limit_expirations=args.limit_expirations,
                host=args.host,
                port=args.port,
                spot_override=args.spot,
                base_dir=base,
                chain_cache=bool(args.chain_cache),
                chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
                option_types=("put,call" if (want_put and want_call) else ("put" if want_put else "call")),
                min_strike=args.min_strike,
                max_strike=args.max_strike,
                min_dte=args.min_dte,
                max_dte=args.max_dte,
                retry_max_attempts=int(args.retry_max_attempts),
                retry_time_budget_sec=float(args.retry_time_budget_sec),
                retry_base_delay_sec=float(args.retry_base_delay_sec),
                retry_max_delay_sec=float(args.retry_max_delay_sec),
                no_retry=bool(args.no_retry),
                max_wait_sec=float(args.option_chain_max_wait_sec),
                option_chain_window_sec=float(args.option_chain_window_sec),
                option_chain_max_calls=int(args.option_chain_max_calls),
                snapshot_max_wait_sec=float(args.snapshot_max_wait_sec),
                snapshot_window_sec=float(args.snapshot_window_sec),
                snapshot_max_calls=int(args.snapshot_max_calls),
                expiration_max_wait_sec=float(args.expiration_max_wait_sec),
                expiration_window_sec=float(args.expiration_window_sec),
                expiration_max_calls=int(args.expiration_max_calls),
            )
        )
        raw_path, csv_path = save_outputs(base, sym, payload, output_root=output_root)
        try:
            meta = payload.get("meta") or {}
            _append_metrics_json(
                opend_metrics_path,
                {
                    "as_of_utc": datetime.now().astimezone().isoformat(),
                    "symbol": sym,
                    "ms": int((time.monotonic() - t0) * 1000),
                    "rows": int(len(payload.get("rows") or [])),
                    "expiration_count": int(payload.get("expiration_count") or 0),
                    "underlier_code": payload.get("underlier_code"),
                    "host": meta.get("host"),
                    "port": meta.get("port"),
                    "error": meta.get("error"),
                },
            )
        except Exception:
            pass
        if not args.quiet:
            print(f"[OK] {sym} source=opend")
            print(f"  underlier={payload.get('underlier_code')} spot={payload.get('spot')}")
            print(f"  expirations={payload.get('expiration_count')} rows={len(payload.get('rows') or [])}")
            print(f"  raw={raw_path}")
            print(f"  csv={csv_path}")


if __name__ == "__main__":
    main()
