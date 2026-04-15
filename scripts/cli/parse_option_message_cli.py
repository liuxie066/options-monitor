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
from scripts.account_config import accounts_from_config_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Parse option intake message')
    parser.add_argument('--text', required=True)
    parser.add_argument('--config', default=None, help='optional options-monitor config used to resolve account labels')
    parser.add_argument('--accounts', nargs='*', default=None, help='optional account labels to recognize')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """parse_option_message CLI 入口。"""
    args = parse_args(argv)
    accounts = args.accounts
    if accounts is None and args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (repo_base / cfg_path).resolve()
        accounts = accounts_from_config_path(cfg_path)
    out = parse_option_message_text(args.text, accounts=accounts)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
