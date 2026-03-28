#!/usr/bin/env python3
from __future__ import annotations

"""Doctor: verify OpenD provides required option fields for options-monitor.

Checks for each underlier:
- option chain exists
- snapshot returns bid/ask/last/volume + option greeks/oi/iv + contract multiplier
- spot can be fetched (or explain fallback)

Usage:
  ./.venv/bin/python scripts/doctor_opend_required_fields.py --symbols NVDA 00700.HK

Exit code:
- 0 if all ok
- 2 if any symbol fails critical requirements
"""

import argparse
import json
from dataclasses import dataclass


@dataclass
class SymResult:
    symbol: str
    underlier_code: str | None
    ok: bool
    chain_rows: int = 0
    snap_rows: int = 0
    missing_snapshot_cols: list[str] | None = None
    spot: float | None = None
    note: str | None = None
    error: str | None = None


REQUIRED_SNAPSHOT_COLS = [
    "code",
    "last_price",
    "bid_price",
    "ask_price",
    "volume",
    "option_open_interest",
    "option_implied_volatility",
    "option_delta",
    "option_contract_multiplier",
]


def main():
    ap = argparse.ArgumentParser(description="Doctor OpenD option fields")
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=10, help="snapshot sample size")
    args = ap.parse_args()

    # Allow running as a script without installation
    import sys
    from pathlib import Path
    repo_base = Path(__file__).resolve().parents[1]
    if str(repo_base) not in sys.path:
        sys.path.insert(0, str(repo_base))

    # local import to keep module lightweight
    from futu import OpenQuoteContext, RET_OK
    from scripts.opend_utils import normalize_underlier

    results: list[SymResult] = []
    any_fail = False

    for sym in args.symbols:
        u = None
        ctx = OpenQuoteContext(host=args.host, port=args.port)
        try:
            u = normalize_underlier(sym)
            ret, chain = ctx.get_option_chain(u.code)
            if ret != RET_OK or chain is None or chain.empty:
                r = SymResult(symbol=sym, underlier_code=(u.code if u else None), ok=False, error=f"get_option_chain ret={ret} empty")
                results.append(r)
                any_fail = True
                continue

            codes = [str(x) for x in chain["code"].astype(str).head(int(args.limit)).tolist() if x]
            ret2, snap = ctx.get_market_snapshot(codes)
            if ret2 != RET_OK or snap is None or snap.empty:
                r = SymResult(symbol=sym, underlier_code=u.code, ok=False, chain_rows=int(len(chain)), error=f"get_market_snapshot ret={ret2} empty")
                results.append(r)
                any_fail = True
                continue

            missing = [c for c in REQUIRED_SNAPSHOT_COLS if c not in snap.columns]

            # spot via snapshot underlier
            # NOTE: For US underliers OpenD may have no stock quote right; spot may be unavailable.
            spot = None
            try:
                if u.market != 'US':
                    ret3, s0 = ctx.get_market_snapshot([u.code])
                    if ret3 == RET_OK and s0 is not None and not s0.empty:
                        spot = float(s0.iloc[0].get("last_price")) if s0.iloc[0].get("last_price") is not None else None
            except Exception:
                spot = None

            ok = (len(missing) == 0)
            if not ok:
                any_fail = True

            note = None
            if spot is None and u.market != 'US':
                note = "spot missing via OpenD snapshot; consider spot override/fallback"
            if u.market == 'US':
                note = "US spot is not required from OpenD (often no quote right); use spot override/fallback if needed"

            results.append(SymResult(
                symbol=sym,
                underlier_code=u.code,
                ok=ok,
                chain_rows=int(len(chain)),
                snap_rows=int(len(snap)),
                missing_snapshot_cols=missing,
                spot=spot,
                note=note,
            ))
        except Exception as e:
            any_fail = True
            results.append(SymResult(symbol=sym, underlier_code=(u.code if u else None), ok=False, error=f"{type(e).__name__}: {e}"))
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    out = {
        "host": args.host,
        "port": args.port,
        "results": [r.__dict__ for r in results],
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in results:
            status = "OK" if r.ok else "FAIL"
            print(f"[{status}] {r.symbol} underlier={r.underlier_code} chain={r.chain_rows} snap={r.snap_rows} spot={r.spot}")
            if r.missing_snapshot_cols:
                print("  missing snapshot cols:", ",".join(r.missing_snapshot_cols))
            if r.note:
                print("  note:", r.note)
            if r.error:
                print("  error:", r.error)

    raise SystemExit(2 if any_fail else 0)


if __name__ == "__main__":
    main()
