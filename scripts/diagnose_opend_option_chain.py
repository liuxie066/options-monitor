#!/usr/bin/env python3
from __future__ import annotations

"""Diagnose OpenD option_chain expirations for a given underlier.

This tool prints:
- return code / error
- total rows
- unique expiration dates (strike_time)
- per-expiration row counts
- option_type counts

Usage:
  ./.venv/bin/python scripts/diagnose_opend_option_chain.py --underlier HK.09992
  ./.venv/bin/python scripts/diagnose_opend_option_chain.py --underlier HK.00700

Note:
- This script does NOT fetch snapshots; it only calls get_option_chain.
"""

import argparse
from collections import Counter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--underlier', required=True, help='e.g. HK.09992 / HK.00700 / US.NVDA')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=11111)
    ap.add_argument('--limit-expirations', type=int, default=0, help='If >0, only show first N expirations')
    args = ap.parse_args()

    from futu import OpenQuoteContext

    ctx = OpenQuoteContext(host=args.host, port=int(args.port))
    try:
        ret, df = ctx.get_option_chain(str(args.underlier))
        if ret != 0:
            print({'ok': False, 'underlier': args.underlier, 'ret': ret, 'err': str(df)})
            raise SystemExit(2)

        if df is None or df.empty:
            print({'ok': True, 'underlier': args.underlier, 'rows': 0, 'expirations': []})
            return

        # strike_time is usually 'YYYY-MM-DD'
        st = df['strike_time'].astype(str).str.slice(0, 10)
        expirations = sorted({x for x in st.tolist() if isinstance(x, str) and len(x) >= 10})
        if args.limit_expirations and int(args.limit_expirations) > 0:
            expirations = expirations[: int(args.limit_expirations)]

        c = Counter(st.tolist())
        per_exp = [{
            'expiration': e,
            'rows': int(c.get(e) or 0),
        } for e in expirations]

        ot_counts = None
        if 'option_type' in df.columns:
            ot_counts = Counter([str(x).lower() for x in df['option_type'].tolist()])
            ot_counts = {k: int(v) for k, v in ot_counts.items()}

        out = {
            'ok': True,
            'underlier': args.underlier,
            'rows': int(len(df)),
            'unique_expirations': int(len(expirations)),
            'expirations': expirations,
            'per_expiration': per_exp,
            'option_type_counts': ot_counts,
        }
        print(out)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
