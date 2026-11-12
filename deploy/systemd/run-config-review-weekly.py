#!/usr/bin/env python3
"""Run the public report command once; require a confirmed scheduled delivery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = subprocess.run(
        [
            str(args.source_root.resolve() / "om"), "report", "run", "config_review",
            "--runtime-root", str(args.runtime_root.resolve()), "--trigger", "schedule", "--json",
        ],
        cwd=args.runtime_root.resolve(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.returncode != 0:
        return result.returncode
    try:
        outcome = json.loads(result.stdout)
        delivery = outcome.get("delivery") or {}
        confirmed = (
            outcome.get("ok") is True and outcome.get("status") == "succeeded"
            and delivery.get("status") == "delivered" and bool(delivery.get("message_id"))
        )
    except (ValueError, AttributeError, TypeError):
        confirmed = False
    if not confirmed:
        sys.stderr.write("Weekly config review did not confirm delivery; inspect the persisted run.json receipt.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
