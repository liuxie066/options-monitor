#!/usr/bin/env python3
from __future__ import annotations

"""CLI adapter for fetching required option data from Futu OpenD."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from src.application.opend_symbol_fetching import (
    FetchSymbolRequest,
    fetch_symbol_request,
)
from src.application.opend_fetch_config import DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT, OpenDBatchConfig
from src.application.opend_symbol_chain_fetching import prune_chain_cache
from src.application.opend_symbol_outputs import append_metrics_json, save_outputs
from src.application.required_data_observability import extract_fetch_payload_metrics
from src.application.runtime_paths import resolve_runtime_root


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
    ap.add_argument("--side-strike-windows-json", default=None)
    ap.add_argument("--explicit-expirations", nargs="*", default=None)
    ap.add_argument(
        "--trading-date",
        default=None,
        help="Immutable YYYY-MM-DD DTE anchor for a planned fetch",
    )
    ap.add_argument("--min-dte", type=int, default=None, help="Only pick expirations with DTE >= min_dte before applying limit-expirations")
    ap.add_argument("--max-dte", type=int, default=None, help="Only pick expirations with DTE <= max_dte before applying limit-expirations")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--spot", type=float, default=None, help="override spot if OpenD has no quote right")
    ap.add_argument(
        "--underlier-observation-json",
        default=None,
        help="Frozen OpeningUnderlierObservation JSON from the parent plan",
    )
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
    ap.add_argument("--snapshot-batch-size", type=int, default=DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT)
    ap.add_argument("--snapshot-fallback-max-codes", type=int, default=100)
    ap.add_argument("--snapshot-fallback-batch-size", type=int, default=20)
    ap.add_argument("--expiration-max-wait-sec", type=float, default=30.0, help="Max seconds to wait for shared option-expiration rate-limit budget")
    ap.add_argument("--expiration-window-sec", type=float, default=30.0, help="Shared option-expiration rate-limit window seconds")
    ap.add_argument("--expiration-max-calls", type=int, default=60, help="Shared option-expiration max calls per window")
    ap.add_argument("--history-kline-max-wait-sec", type=float, default=30.0, help="Max seconds to wait for shared history-K rate-limit budget")
    ap.add_argument("--history-kline-window-sec", type=float, default=30.0, help="Shared history-K rate-limit window seconds")
    ap.add_argument("--history-kline-max-calls", type=int, default=60, help="Shared history-K max calls per window")
    ap.add_argument("--include-realized-volatility", action="store_true", help="Fetch underlier daily K-line and attach RV fields")
    ap.add_argument("--output-root", default=None, help="Output root containing raw/ and parsed/ (default: output_shared/required_data)")
    args = ap.parse_args()

    opt_types = {s.strip().lower() for s in str(args.option_types or "").split(",") if s.strip()}
    want_put = ("put" in opt_types) if opt_types else True
    want_call = ("call" in opt_types) if opt_types else True
    side_strike_windows = None
    if args.side_strike_windows_json:
        try:
            parsed_windows = json.loads(str(args.side_strike_windows_json))
            side_strike_windows = parsed_windows if isinstance(parsed_windows, dict) else None
        except Exception:
            side_strike_windows = None

    base = resolve_runtime_root(repo_root=REPO_ROOT).runtime_root
    output_root = Path(args.output_root).resolve() if args.output_root else None

    if args.chain_cache:
        prune_chain_cache(base, int(args.chain_cache_keep_days))

    underlier_observation = None
    if args.underlier_observation_json is not None:
        try:
            underlier_observation = json.loads(args.underlier_observation_json)
        except (TypeError, ValueError) as exc:
            ap.error(f"invalid --underlier-observation-json: {exc}")
        if not isinstance(underlier_observation, dict):
            ap.error("--underlier-observation-json must decode to an object")

    batch_cfg = OpenDBatchConfig.from_values(
        market_snapshot=args.snapshot_batch_size,
        market_snapshot_fallback_max_codes=args.snapshot_fallback_max_codes,
        market_snapshot_fallback_batch_size=args.snapshot_fallback_batch_size,
    )

    opend_metrics_path = (base / "output_shared" / "state" / "opend_metrics.json").resolve()

    had_error = False
    for sym in args.symbols:
        t0 = time.monotonic()
        payload = fetch_symbol_request(
            FetchSymbolRequest(
                symbol=sym,
                limit_expirations=args.limit_expirations,
                host=args.host,
                port=args.port,
                spot_override=args.spot,
                underlier_observation=underlier_observation,
                fetch_spot_if_missing=underlier_observation is None,
                base_dir=base,
                chain_cache=bool(args.chain_cache),
                chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
                option_types=("put,call" if (want_put and want_call) else ("put" if want_put else "call")),
                min_strike=args.min_strike,
                max_strike=args.max_strike,
                side_strike_windows=side_strike_windows,
                min_dte=args.min_dte,
                max_dte=args.max_dte,
                explicit_expirations=list(args.explicit_expirations or []) or None,
                trading_date=args.trading_date,
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
                snapshot_batch_size=batch_cfg.market_snapshot,
                snapshot_fallback_max_codes=batch_cfg.market_snapshot_fallback_max_codes,
                snapshot_fallback_batch_size=batch_cfg.market_snapshot_fallback_batch_size,
                expiration_max_wait_sec=float(args.expiration_max_wait_sec),
                expiration_window_sec=float(args.expiration_window_sec),
                expiration_max_calls=int(args.expiration_max_calls),
                history_kline_max_wait_sec=float(args.history_kline_max_wait_sec),
                history_kline_window_sec=float(args.history_kline_window_sec),
                history_kline_max_calls=int(args.history_kline_max_calls),
                include_realized_volatility=bool(args.include_realized_volatility),
            )
        )
        raw_path, csv_path = save_outputs(base, sym, payload, output_root=output_root)
        try:
            meta = payload.get("meta") or {}
            fetch_metrics = extract_fetch_payload_metrics(payload)
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
                    "fetch_metrics": fetch_metrics,
                },
            )
        except Exception:
            pass
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        status = str((meta or {}).get("status") or "ok").strip().lower()
        if status in {"error", "fail", "failed"}:
            had_error = True
        if not args.quiet:
            label = "ERROR" if status in {"error", "fail", "failed"} else "OK"
            print(f"[{label}] {sym} source=opend")
            print(f"  underlier={payload.get('underlier_code')} spot={payload.get('spot')}")
            print(f"  expirations={payload.get('expiration_count')} rows={len(payload.get('rows') or [])}")
            print(f"  raw={raw_path}")
            print(f"  csv={csv_path}")

    if had_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
