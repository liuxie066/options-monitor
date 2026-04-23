#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.agent_plugin.main import build_spec, dumps_json, run_tool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor public local agent tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("spec", help="print public tool manifest")

    run_parser = sub.add_parser("run", help="run one public tool and print JSON envelope")
    run_parser.add_argument("--tool", required=True)
    run_parser.add_argument("--input-json", default="{}")
    run_parser.add_argument("--input-file", default=None, help="optional JSON file; overrides --input-json")
    return parser.parse_args(argv)


def _load_input_payload(args: argparse.Namespace) -> dict:
    if args.input_file:
        return json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    raw = str(args.input_json or "{}").strip() or "{}"
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "spec":
        sys.stdout.write(dumps_json(build_spec()))
        return 0

    payload = _load_input_payload(args)
    out = run_tool(str(args.tool), payload)
    sys.stdout.write(dumps_json(out))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
