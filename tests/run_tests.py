#!/usr/bin/env python3
"""Tiny test runner (no pytest dependency).

We keep this repo runnable in minimal environments.

Usage:
  ./.venv/bin/python tests/run_tests.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
VPY = BASE / '.venv' / 'bin' / 'python'


def run_parser(text: str) -> dict:
    p = subprocess.run(
        [str(VPY), 'scripts/parse_option_message.py', '--text', text],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    # Some scripts may print logs; extract the JSON block from the first '{' to the last '}'.
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('empty stdout')
    s = out.find('{')
    e = out.rfind('}')
    if s < 0 or e < 0 or e <= s:
        raise RuntimeError(f'cannot find json in stdout: {out[:200]}')
    j = out[s : e + 1]
    return json.loads(j)


def test_parse_futu_fill_message() -> None:
    msg = "【成交提醒】成功卖出2张$中海油 260330 30.00 购$，成交价格：0.24，此笔订单委托已全部成交，2026/03/25 14:16:53 (香港)。【富途证券(香港)】 lx"
    out = run_parser(msg)
    assert out['ok'] is True
    parsed = out['parsed']
    assert parsed['symbol'] == '0883.HK'
    assert parsed['exp'] == '2026-03-30'
    assert parsed['option_type'] == 'call'
    assert parsed['side'] == 'short'
    assert parsed['strike'] == 30.0
    assert parsed['multiplier'] == 1000
    assert parsed['premium_per_share'] == 0.24
    assert parsed['contracts'] == 2
    assert parsed['account'] == 'lx'
    assert parsed['currency'] == 'HKD'


def main() -> None:
    from test_opend_chain_cache_minimal import (
        test_chain_cache_helpers_roundtrip,
        test_chain_cache_fresh_check,
    )

    tests = [
        test_parse_futu_fill_message,
        test_chain_cache_helpers_roundtrip,
        test_chain_cache_fresh_check,
    ]
    for t in tests:
        t()
    print(f"OK ({len(tests)} tests)")


if __name__ == '__main__':
    main()
