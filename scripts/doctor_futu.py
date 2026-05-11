#!/usr/bin/env python3
from __future__ import annotations

"""Operational CLI wrapper for the Futu data-source doctor."""

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.futu_doctor import build_human_text, run_futu_doctor_checks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Doctor Futu data source (fetch.source=futu)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--symbols", nargs="*", default=[], help="Option symbols to validate, e.g. NVDA 0700.HK")
    parser.add_argument("--ensure", action="store_true", help="Try to start OpenD using OPEND_START_SCRIPT if port is closed")
    parser.add_argument("--timeout-sec", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_futu_doctor_checks(
        host=str(args.host),
        port=int(args.port),
        symbols=[str(s) for s in args.symbols],
        ensure=bool(args.ensure),
        timeout_sec=int(args.timeout_sec),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(build_human_text(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
