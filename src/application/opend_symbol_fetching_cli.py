#!/usr/bin/env python3
from __future__ import annotations

"""CLI adapter for fetching required option data from Futu OpenD."""

import argparse
from datetime import datetime
from pathlib import Path
import time

from src.application.opend_symbol_fetching import (
    FetchSymbolRequest,
    append_metrics_json,
    fetch_symbol_request,
    prune_chain_cache,
    save_outputs,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


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
        prune_chain_cache(base, int(args.chain_cache_keep_days))

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
            append_metrics_json(
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
