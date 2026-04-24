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
    import pandas as pd
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_call_scan = steps.run_sell_call_scan

    def _fake_run_sell_call_scan(**kwargs):
        calls.append(kwargs)
        Path(kwargs["output"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_csv(kwargs["output"], index=False)

    steps.run_sell_call_scan = _fake_run_sell_call_scan
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
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_by_symbol={'AAPL': 100},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert out['strategy'] == 'sell_call'
    assert len(calls) >= 1
    kwargs = calls[0]
    assert kwargs['min_annualized_net_return'] == 0.123


def test_sell_call_steps_converts_min_net_income_from_cny_to_native() -> None:
    _add_repo_to_syspath()

    import scripts.sell_call_steps as steps
    import pandas as pd
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_call_scan = steps.run_sell_call_scan

    def _fake_run_sell_call_scan(**kwargs):
        calls.append(kwargs)
        Path(kwargs["output"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_csv(kwargs["output"], index=False)

    steps.run_sell_call_scan = _fake_run_sell_call_scan
    try:
        steps.run_sell_call_scan_and_summarize(
            py='python',
            base=BASE,
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
            cc={'enabled': True, 'min_net_income': 100},
            top_n=3,
            required_data_dir=BASE / 'output',
            report_dir=BASE / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            stock={'shares': 300, 'avg_cost': 100},
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_by_symbol={'AAPL': 100},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert calls
    kwargs = calls[0]
    assert kwargs['min_net_income'] == 14.000000000000002


def test_sell_call_steps_converts_hk_min_net_income_from_cny_to_hkd() -> None:
    _add_repo_to_syspath()

    import scripts.sell_call_steps as steps
    import pandas as pd
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_call_scan = steps.run_sell_call_scan

    def _fake_run_sell_call_scan(**kwargs):
        calls.append(kwargs)
        Path(kwargs["output"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_csv(kwargs["output"], index=False)

    steps.run_sell_call_scan = _fake_run_sell_call_scan
    try:
        steps.run_sell_call_scan_and_summarize(
            py='python',
            base=BASE,
            symbol='0700.HK',
            symbol_lower='0700.hk',
            symbol_cfg={'symbol': '0700.HK', 'sell_call': {}},
            cc={'enabled': True, 'min_net_income': 100},
            top_n=3,
            required_data_dir=BASE / 'output',
            report_dir=BASE / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            stock={'shares': 300, 'avg_cost': 100},
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_by_symbol={'0700.HK': 100},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert calls
    kwargs = calls[0]
    assert abs(float(kwargs['min_net_income']) - (100 / 0.92)) < 1e-9
