#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.query_sell_put_cash import query_sell_put_cash


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Query cash headroom for sell-put (cash-secured) strategy')
    parser.add_argument('--config', default=None, help='optional options-monitor runtime config for portfolio source/account mapping')
    parser.add_argument('--pm-config', default=None, help='Feishu/Bitable credential config path; auto-resolves when omitted')
    parser.add_argument('--market', default='富途')
    parser.add_argument('--account', default=None)
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--top', type=int, default=10, help='top N symbols in cash-secured breakdown')
    parser.add_argument('--no-fx', action='store_true', help='do not fetch FX rates / CNY equivalent')
    parser.add_argument(
        '--out-dir',
        default='output/state',
        help='Directory to write intermediate JSON state (portfolio_context/option_positions_context/rate_cache). Default: output/state',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """query_sell_put_cash CLI 入口。"""
    args = parse_args(argv)
    query_sell_put_cash(
        config=args.config,
        pm_config=args.pm_config,
        market=args.market,
        account=args.account,
        output_format=args.format,
        top=args.top,
        no_fx=args.no_fx,
        out_dir=args.out_dir,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
