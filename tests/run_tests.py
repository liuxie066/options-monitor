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


def test_run_log_writer_create_and_append() -> None:
    import sys
    from tempfile import TemporaryDirectory

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.run_log import RunLogger

    with TemporaryDirectory() as td:
        root = Path(td)
        lg = RunLogger(root)
        lg.event('run_start', 'start', data={'accounts': ['lx', 'sy'], 'symbols_count': 3})
        lg.event('run_end', 'ok', duration_ms=12)

        files = list((root / 'audit' / 'run_logs').glob('*.jsonl'))
        assert len(files) == 1
        lines = [ln for ln in files[0].read_text(encoding='utf-8').splitlines() if ln.strip()]
        assert len(lines) == 2

        rec1 = json.loads(lines[0])
        rec2 = json.loads(lines[1])
        assert rec1['run_id'] == rec2['run_id']
        assert rec1['step'] == 'run_start'
        assert rec2['step'] == 'run_end'


def test_run_log_data_small() -> None:
    import sys

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.run_log import _compact_data

    big = {
        'k1': 'x' * 2000,
        'k2': list(range(500)),
        'k3': {'a': 1, 'b': 2, 'c': 3},
    }
    out = _compact_data(big, max_chars=300)
    payload = json.dumps(out, ensure_ascii=False)
    assert len(payload) <= 300


def main() -> None:
    from test_opend_chain_cache_minimal import (
        test_chain_cache_helpers_roundtrip,
        test_chain_cache_fresh_check,
    )
    from test_opend_watchdog_alerts import (
        test_watchdog_error_code_mapping,
        test_opend_alert_rate_limit,
    )
    from test_fetch_portfolio_context_richtext import (
        test_build_context_richtext_normalization_and_hk_symbol,
    )
    from test_report_labels_no_stale import (
        test_add_sell_put_labels_overwrites_on_empty,
    )
    from test_scan_scheduler_notify_semantics import (
        test_scan_scheduler_emits_is_notify_window_open_and_backcompat_should_notify,
    )
    from test_scan_scheduler_scan_per_account import (
        test_scan_scheduler_scan_is_per_account,
    )
    from test_market_session_single_source_of_truth import (
        test_select_markets_to_run_hk_break_respected,
    )
    from test_config_loader_validation_cache import (
        test_scheduled_validation_is_cached,
    )

    tests = [
        test_parse_futu_fill_message,
        test_chain_cache_helpers_roundtrip,
        test_chain_cache_fresh_check,
        test_watchdog_error_code_mapping,
        test_opend_alert_rate_limit,
        test_run_log_writer_create_and_append,
        test_run_log_data_small,
        test_build_context_richtext_normalization_and_hk_symbol,
        test_add_sell_put_labels_overwrites_on_empty,
        test_scan_scheduler_emits_is_notify_window_open_and_backcompat_should_notify,
        test_scan_scheduler_scan_is_per_account,
        test_select_markets_to_run_hk_break_respected,
        test_scheduled_validation_is_cached,
    ]
    for t in tests:
        t()
    print(f"OK ({len(tests)} tests)")


if __name__ == '__main__':
    main()
