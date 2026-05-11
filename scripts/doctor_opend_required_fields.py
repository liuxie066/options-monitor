#!/usr/bin/env python3
from __future__ import annotations

"""Operational CLI wrapper for OpenD option field checks."""

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.futu_doctor import check_required_option_fields, required_fields_ok  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Doctor OpenD option fields")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=10, help="snapshot sample size")
    args = parser.parse_args()

    result = check_required_option_fields(
        symbols=[str(s) for s in args.symbols],
        host=str(args.host),
        port=int(args.port),
        limit=int(args.limit),
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for row in result.get("results", []):
            if not isinstance(row, dict):
                continue
            status = "OK" if row.get("ok") else "FAIL"
            print(
                f"[{status}] {row.get('symbol')} underlier={row.get('underlier_code')} "
                f"chain={row.get('chain_rows')} snap={row.get('snap_rows')} spot={row.get('spot')}"
            )
            missing = row.get("missing_snapshot_cols") if isinstance(row.get("missing_snapshot_cols"), list) else []
            if missing:
                print("  missing snapshot cols:", ",".join(str(x) for x in missing))
            if row.get("note"):
                print("  note:", row.get("note"))
            if row.get("error"):
                print("  error:", row.get("error"))

    return 0 if required_fields_ok(result, symbols=[str(s) for s in args.symbols]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
