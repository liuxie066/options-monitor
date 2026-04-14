from __future__ import annotations

import sys
from pathlib import Path


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_validate_config_rejects_symbol_level_d3_keys() -> None:
    _add_repo_to_syspath()
    from scripts.validate_config import validate_config

    cfg = {
        'profiles': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
            'call_base': {'sell_call': {'min_open_interest': 50, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'watchlist': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                    'min_iv': 0.2,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'AAPL.sell_put' in msg
        assert 'min_iv' in msg

    cfg['watchlist'][0]['sell_put'].pop('min_iv')
    cfg['watchlist'][0]['sell_call'] = {
        'enabled': True,
        'min_dte': 7,
        'max_dte': 45,
        'min_strike': 120,
        'max_delta': 0.35,
    }
    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'AAPL.sell_call' in msg
        assert 'max_delta' in msg


def test_validate_config_rejects_removed_global_d3_keys() -> None:
    _add_repo_to_syspath()
    from scripts.validate_config import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'min_open_interest': 60,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                    'min_iv': 0.2,
                }
            }
        },
        'watchlist': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'templates.put_base.sell_put' in msg
        assert 'only min_open_interest, min_volume, max_spread_ratio are allowed' in msg
        assert 'min_iv' in msg


def test_sell_put_steps_use_global_d3_only() -> None:
    base = _add_repo_to_syspath()
    import scripts.sell_put_steps as steps
    from scripts.fx_rates import CurrencyConverter, FxRates

    calls: list[list[str]] = []
    orig_run_cmd = steps.run_cmd
    orig_add_labels = steps.add_sell_put_labels

    def _fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)

    steps.run_cmd = _fake_run_cmd
    steps.add_sell_put_labels = lambda *args, **kwargs: None
    try:
        out = steps.run_sell_put_scan_and_summarize(
            py='python',
            base=base,
            sym='AAPL',
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL', 'sell_put': {'min_open_interest': 999}},
            sp={
                'enabled': True,
                'min_dte': 7,
                'max_dte': 45,
                'min_strike': 1,
                'max_strike': 200,
                'min_annualized_net_return': 0.1,
                'min_open_interest': 999,
            },
            top_n=3,
            required_data_dir=base / 'output',
            report_dir=base / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            fx=CurrencyConverter(FxRates()),
            portfolio_ctx=None,
            global_sell_put_d3={
                'min_open_interest': 50,
                'min_volume': 12,
                'max_spread_ratio': 0.31,
                'min_iv': 0.15,
                'require_bid_ask': True,
            },
        )
    finally:
        steps.run_cmd = orig_run_cmd
        steps.add_sell_put_labels = orig_add_labels

    assert out['strategy'] == 'sell_put'
    assert calls
    cmd = calls[0]
    i_oi = cmd.index('--min-open-interest')
    assert cmd[i_oi + 1] == '50'
    i_vol = cmd.index('--min-volume')
    assert cmd[i_vol + 1] == '12'
    i_spread = cmd.index('--max-spread-ratio')
    assert cmd[i_spread + 1] == '0.31'
    assert '--min-iv' not in cmd
    assert '--require-bid-ask' not in cmd


def test_sell_call_steps_use_global_d3_only() -> None:
    base = _add_repo_to_syspath()
    import scripts.sell_call_steps as steps

    calls: list[list[str]] = []
    orig_run_cmd = steps.run_cmd

    def _fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)

    steps.run_cmd = _fake_run_cmd
    try:
        out = steps.run_sell_call_scan_and_summarize(
            py='python',
            base=base,
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL'},
            cc={
                'enabled': True,
                'min_dte': 7,
                'max_dte': 45,
                'min_strike': 110,
                'min_open_interest': 999,
                'min_annualized_net_premium_return': 0.12,
            },
            top_n=3,
            required_data_dir=base / 'output',
            report_dir=base / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            stock={'shares': 200, 'avg_cost': 100.0},
            locked_shares_by_symbol={'AAPL': 0},
            global_sell_call_d3={
                'min_open_interest': 60,
                'min_volume': 8,
                'max_spread_ratio': 0.22,
                'min_delta': 0.1,
            },
        )
    finally:
        steps.run_cmd = orig_run_cmd

    assert out['strategy'] == 'sell_call'
    assert calls
    cmd = calls[0]
    i_oi = cmd.index('--min-open-interest')
    assert cmd[i_oi + 1] == '60'
    i_vol = cmd.index('--min-volume')
    assert cmd[i_vol + 1] == '8'
    i_spread = cmd.index('--max-spread-ratio')
    assert cmd[i_spread + 1] == '0.22'
    assert '--min-delta' not in cmd
    assert '--max-delta' not in cmd


def test_sell_put_reject_stage_is_strategy_gate() -> None:
    _add_repo_to_syspath()
    from tempfile import TemporaryDirectory

    import pandas as pd
    from scripts.scan_sell_put import run_sell_put_scan

    with TemporaryDirectory() as td:
        root = Path(td)
        parsed = root / 'parsed'
        parsed.mkdir(parents=True, exist_ok=True)
        out_path = root / 'sell_put_candidates.csv'

        pd.DataFrame(
            [
                {
                    'symbol': 'AAPL',
                    'option_type': 'put',
                    'expiration': '2026-05-15',
                    'dte': 30,
                    'contract_symbol': 'PASS',
                    'multiplier': 100,
                    'currency': 'USD',
                    'strike': 90.0,
                    'spot': 100.0,
                    'bid': 1.4,
                    'ask': 1.6,
                    'last_price': 1.5,
                    'mid': 1.5,
                    'open_interest': 200,
                    'volume': 50,
                    'implied_volatility': 0.30,
                    'delta': -0.22,
                },
                {
                    'symbol': 'AAPL',
                    'option_type': 'put',
                    'expiration': '2026-05-15',
                    'dte': 30,
                    'contract_symbol': 'FAIL_MIN_NET',
                    'multiplier': 100,
                    'currency': 'USD',
                    'strike': 85.0,
                    'spot': 100.0,
                    'bid': 0.9,
                    'ask': 1.1,
                    'last_price': 1.0,
                    'mid': 1.0,
                    'open_interest': 200,
                    'volume': 50,
                    'implied_volatility': 0.30,
                    'delta': -0.18,
                },
            ]
        ).to_csv(parsed / 'AAPL_required_data.csv', index=False)

        out = run_sell_put_scan(
            symbols=['AAPL'],
            input_root=root,
            output=out_path,
            min_annualized_net_return=0.01,
            min_net_income=120.0,
            min_open_interest=10,
            quiet=True,
        )

        assert list(out['contract_symbol']) == ['PASS']
        reject_log = pd.read_csv(out_path.with_name(f'{out_path.stem}_reject_log.csv'))
        assert not reject_log.empty
        assert set(reject_log['reject_stage'].dropna().astype(str).tolist()) == {'step3_risk_gate'}
