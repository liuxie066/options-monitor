#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.application.release_test_plan import build_release_test_plan, changed_files_from_git


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="print a read-only release test plan for the current changes")
    parser.add_argument("--base", default="origin/main", help="git base ref used for committed changes")
    parser.add_argument("--mode", choices=("fast", "standard", "full"), default="standard")
    parser.add_argument("--tag", default=None, help="release tag/version to validate, such as v1.2.183")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=None,
        help="explicit changed file; repeat to bypass git diff discovery",
    )
    return parser.parse_args()


def _default_tag() -> str | None:
    version_path = BASE_DIR / "VERSION"
    if not version_path.exists():
        return None
    version = version_path.read_text(encoding="utf-8").strip()
    return f"v{version}" if version else None


def main() -> int:
    args = parse_args()
    try:
        changed_files = args.changed_file or changed_files_from_git(base_ref=args.base, cwd=BASE_DIR)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"Failed to discover changed files: {exc}\n")
        if exc.stderr:
            sys.stderr.write(str(exc.stderr).rstrip() + "\n")
        return 2
    plan = build_release_test_plan(
        changed_files=changed_files,
        mode=args.mode,
        version=args.tag or _default_tag(),
        base_ref=None if args.changed_file else args.base,
    )
    sys.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0 if plan.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
