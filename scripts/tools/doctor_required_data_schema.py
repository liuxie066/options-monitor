#!/usr/bin/env python3
"""Doctor: validate required_data CSV schema for options-monitor.

Why:
- The pipeline depends on a small set of columns.
- When adding new data sources (e.g. OpenD), we must not break existing consumers.

Checks:
- required columns exist
- basic type sanity (numeric columns parseable)
- missing-rate summary for key columns

Usage:
  python3 scripts/doctor_required_data_schema.py --dir output/parsed
  python3 scripts/doctor_required_data_schema.py --file output/parsed/NVDA_required_data.csv
  python3 scripts/doctor_required_data_schema.py --json

Exit codes:
  0: OK
  2: missing required columns
  3: no files
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


REQUIRED_COLS = [
    'symbol',
    'option_type',
    'expiration',
    'dte',
    'contract_symbol',
    'strike',
    'spot',
    'bid',
    'ask',
    'last_price',
    'mid',
    'volume',
    'open_interest',
    'implied_volatility',
    'delta',
]

NUMERIC_COLS = [
    'dte',
    'strike',
    'spot',
    'bid',
    'ask',
    'last_price',
    'mid',
    'volume',
    'open_interest',
    'implied_volatility',
    'delta',
]


def _to_float(s: str):
    if s is None:
        return None
    s2 = str(s).strip()
    if s2 == '' or s2.lower() == 'nan':
        return None
    try:
        return float(s2)
    except Exception:
        return 'PARSE_FAIL'


def check_file(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        missing_cols = [c for c in REQUIRED_COLS if c not in cols]
        stats = {
            'file': str(path),
            'columns': cols,
            'missing_required_columns': missing_cols,
            'row_count': 0,
            'missing_rate': {},
            'parse_fail_rate': {},
        }

        if missing_cols:
            return stats

        missing_cnt = {c: 0 for c in NUMERIC_COLS}
        parse_fail_cnt = {c: 0 for c in NUMERIC_COLS}

        for row in reader:
            stats['row_count'] += 1
            for c in NUMERIC_COLS:
                v = _to_float(row.get(c))
                if v is None:
                    missing_cnt[c] += 1
                elif v == 'PARSE_FAIL':
                    parse_fail_cnt[c] += 1

        n = max(stats['row_count'], 1)
        stats['missing_rate'] = {c: round(missing_cnt[c] / n, 6) for c in NUMERIC_COLS}
        stats['parse_fail_rate'] = {c: round(parse_fail_cnt[c] / n, 6) for c in NUMERIC_COLS}
        return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='output/parsed', help='directory containing *_required_data.csv')
    ap.add_argument('--file', default=None, help='check a single CSV file')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    paths: list[Path] = []
    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = (base / p).resolve()
        paths = [p]
    else:
        d = Path(args.dir)
        if not d.is_absolute():
            d = (base / d).resolve()
        paths = sorted(d.glob('*_required_data.csv'))

    if not paths:
        out = {'ok': False, 'error': 'no files', 'paths': [str(p) for p in paths]}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print('[ERROR] no *_required_data.csv found')
        return 3

    results = [check_file(p) for p in paths]
    bad = [r for r in results if r.get('missing_required_columns')]

    ok = (len(bad) == 0)
    out = {
        'ok': ok,
        'required_columns': REQUIRED_COLS,
        'files_checked': len(results),
        'bad_files': bad,
        'results': results,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] files_checked={len(results)}" if ok else f"[FAIL] bad_files={len(bad)}/{len(results)}")
        if bad:
            for r in bad:
                print(f"- {r['file']}: missing {r['missing_required_columns']}")
        # show a compact summary of missing rates for the first file
        r0 = results[0]
        print(f"\nExample: {r0['file']}")
        print(f"rows={r0['row_count']}")
        keys = ['spot','mid','bid','ask','open_interest','volume','implied_volatility','delta']
        mr = r0.get('missing_rate', {})
        pfr = r0.get('parse_fail_rate', {})
        for k in keys:
            print(f"- {k}: missing_rate={mr.get(k)} parse_fail_rate={pfr.get(k)}")

    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
