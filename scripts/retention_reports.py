#!/usr/bin/env python3
from __future__ import annotations

"""Retention policy for output/reports.

Goal: keep the reports directory small and readable.
- Keep key artifacts always: symbols_summary.csv, symbols_notification.txt
- Move older, verbose artifacts into output/archives

Usage:
  ./scripts/retention_reports.py --keep-days 7
  ./scripts/retention_reports.py --keep-days 3 --dry-run

Env:
  REPORTS_KEEP_DAYS: default keep-days
"""

import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path


KEEP_ALWAYS = {
    'symbols_summary.csv',
    'symbols_notification.txt',
}


def main():
    ap = argparse.ArgumentParser(description='Retention for output/reports')
    ap.add_argument('--reports', default='output/reports')
    ap.add_argument('--keep-days', type=int, default=int(os.getenv('REPORTS_KEEP_DAYS', '7')))
    ap.add_argument('--archive-dir', default='output/archives')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    reports = (base / args.reports).resolve()
    archive = (base / args.archive_dir).resolve()

    if not reports.exists():
        return

    cutoff = datetime.utcnow().timestamp() - int(args.keep_days) * 86400
    archive.mkdir(parents=True, exist_ok=True)

    moved = 0
    for p in sorted(reports.glob('*')):
        try:
            if not p.is_file():
                continue
            if p.name in KEEP_ALWAYS:
                continue
            if p.stat().st_mtime >= cutoff:
                continue

            dst = archive / p.name
            if args.dry_run:
                print(f"[DRY] move {p} -> {dst}")
            else:
                # overwrite if exists
                if dst.exists():
                    dst.unlink()
                shutil.move(str(p), str(dst))
            moved += 1
        except Exception:
            continue

    if args.dry_run:
        print(f"[DRY] moved {moved}")
    else:
        print(f"[DONE] moved {moved}")


if __name__ == '__main__':
    main()
