#!/usr/bin/env python3
"""Build or verify the official plugin assets locally; never publish or install."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from src.application.plugin_release import build, verify_publication


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if args.verify_only:
        import subprocess
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        catalog = verify_publication(output, tag="v" + (source / "VERSION").read_text().strip(), commit=commit)
    else:
        catalog = build(source=source, output=output)
    print(json.dumps(catalog, indent=2))


if __name__ == "__main__":
    main()
