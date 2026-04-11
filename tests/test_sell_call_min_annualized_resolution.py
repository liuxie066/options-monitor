from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
VPY = BASE / '.venv' / 'bin' / 'python'


def _add_repo_to_syspath() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))


def test_symbol_sell_call_min_overrides_template() -> None:
    _add_repo_to_syspath()
    from scripts.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': 0.12},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': 0.08}}}

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.12


def test_template_sell_call_min_overrides_default() -> None:
    _add_repo_to_syspath()
    from scripts.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': None},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': 0.09}}}

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.09


def test_none_sell_call_min_uses_default() -> None:
    _add_repo_to_syspath()
    from scripts.sell_call_config import (
        DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN,
        resolve_min_annualized_net_premium_return,
    )

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': None},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': None}}}

    assert (
        resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles)
        == DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN
    )


def test_legacy_sell_call_field_still_works() -> None:
    _add_repo_to_syspath()
    from scripts.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'sell_call': {'min_annualized_net_return': 0.11},
    }

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles={}) == 0.11


def test_invalid_sell_call_min_raises() -> None:
    _add_repo_to_syspath()
    from scripts.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'sell_call': {'min_annualized_net_premium_return': 1.2},
    }

    try:
        resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles={})
    except ValueError as e:
        assert 'within [0, 1]' in str(e)
    else:
        raise AssertionError('expected ValueError for invalid min_annualized_net_premium_return')


def test_scan_sell_call_requires_min_annualized_arg() -> None:
    p = subprocess.run(
        [
            str(VPY),
            'scripts/scan_sell_call.py',
            '--symbols',
            'AAPL',
            '--avg-cost',
            '100',
            '--shares',
            '100',
            '--quiet',
            '--output',
            '/tmp/sell_call_candidates_test.csv',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert p.returncode != 0
    assert '[ARG_ERROR]' in (p.stderr or '')
    assert '--min-annualized-net-return' in (p.stderr or '')


def test_scan_sell_call_rejects_out_of_range_arg() -> None:
    p = subprocess.run(
        [
            str(VPY),
            'scripts/scan_sell_call.py',
            '--symbols',
            'AAPL',
            '--avg-cost',
            '100',
            '--shares',
            '100',
            '--min-annualized-net-return',
            '1.2',
            '--quiet',
            '--output',
            '/tmp/sell_call_candidates_test.csv',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert p.returncode != 0
    assert '[ARG_ERROR]' in (p.stderr or '')
    assert 'within [0, 1]' in (p.stderr or '')


def test_sell_call_steps_passes_resolved_threshold_to_scanner() -> None:
    _add_repo_to_syspath()

    import scripts.sell_call_steps as steps

    calls: list[list[str]] = []
    orig_run_cmd = steps.run_cmd

    def _fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)

    steps.run_cmd = _fake_run_cmd
    try:
        out = steps.run_sell_call_scan_and_summarize(
            py='python',
            base=BASE,
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
            cc={'enabled': True, 'min_annualized_net_premium_return': 0.123},
            top_n=3,
            required_data_dir=BASE / 'output',
            report_dir=BASE / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            stock={'shares': 300, 'avg_cost': 100},
            locked_shares_by_symbol={'AAPL': 100},
        )
    finally:
        steps.run_cmd = orig_run_cmd

    assert out['strategy'] == 'sell_call'
    assert len(calls) >= 1
    cmd = calls[0]
    i = cmd.index('--min-annualized-net-return')
    assert cmd[i + 1] == '0.123'
