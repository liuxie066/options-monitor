#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.parse_option_message import parse_option_message_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Parse option intake message')
    parser.add_argument('--text', required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """parse_option_message CLI 入口。"""
    args = parse_args(argv)
    out = parse_option_message_text(args.text)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
