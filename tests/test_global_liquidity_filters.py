from __future__ import annotations

import sys
from pathlib import Path


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_validate_config_rejects_symbol_level_strategy_filter_keys() -> None:
    _add_repo_to_syspath()
    from scripts.validate_config import validate_config

    cfg = {
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
            'call_base': {'sell_call': {'min_open_interest': 50, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
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

    cfg['symbols'][0]['sell_put'].pop('min_iv')
    cfg['symbols'][0]['sell_call'] = {
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


def test_validate_config_rejects_removed_global_strategy_filter_keys() -> None:
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
        'symbols': [
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


def test_validate_config_rejects_fees_config() -> None:
    _add_repo_to_syspath()
    from scripts.validate_config import validate_config

    cfg = {
        'fees': {'US': {'model': 'futu_us_simplified'}},
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
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
        assert 'fees is no longer supported' in msg


def test_validate_config_accepts_external_holdings_account_settings() -> None:
    _add_repo_to_syspath()
    from scripts.validate_config import validate_config

    cfg = {
        'accounts': ['user1', 'ext1'],
        'account_settings': {
            'user1': {'type': 'futu'},
            'ext1': {'type': 'external_holdings', 'holdings_account': 'Feishu EXT'},
        },
        'portfolio': {
            'source': 'futu',
            'source_by_account': {'ext1': 'holdings'},
        },
        'trade_intake': {
            'account_mapping': {
                'futu': {'REAL_1': 'user1'},
            }
        },
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
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

    validate_config(cfg)


def test_sell_put_steps_use_global_liquidity_filters_only() -> None:
    base = _add_repo_to_syspath()
    import scripts.sell_put_steps as steps
    import pandas as pd
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_put_scan = steps.run_sell_put_scan
    orig_add_labels = steps.add_sell_put_labels

    def _fake_run_sell_put_scan(**kwargs):
        calls.append(kwargs)
        Path(kwargs["output"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_csv(kwargs["output"], index=False)

    steps.run_sell_put_scan = _fake_run_sell_put_scan
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
            exchange_rate_converter=CurrencyConverter(ExchangeRates()),
            portfolio_ctx=None,
            global_sell_put_liquidity={
                'min_open_interest': 50,
                'min_volume': 12,
                'max_spread_ratio': 0.31,
                'min_iv': 0.15,
                'require_bid_ask': True,
            },
        )
    finally:
        steps.run_sell_put_scan = orig_run_sell_put_scan
        steps.add_sell_put_labels = orig_add_labels

    assert out['strategy'] == 'sell_put'
    assert calls
    kwargs = calls[0]
    assert kwargs['min_open_interest'] == 50.0
    assert kwargs['min_volume'] == 12.0
    assert kwargs['max_spread_ratio'] == 0.31
    assert 'min_iv' not in kwargs
    assert 'require_bid_ask' not in kwargs


def test_sell_call_steps_use_global_liquidity_filters_only() -> None:
    base = _add_repo_to_syspath()
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
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_by_symbol={'AAPL': 0},
            global_sell_call_liquidity={
                'min_open_interest': 60,
                'min_volume': 8,
                'max_spread_ratio': 0.22,
                'min_delta': 0.1,
            },
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert out['strategy'] == 'sell_call'
    assert calls
    kwargs = calls[0]
    assert kwargs['min_open_interest'] == 60.0
    assert kwargs['min_volume'] == 8.0
    assert kwargs['max_spread_ratio'] == 0.22
    assert 'min_delta' not in kwargs
    assert 'max_delta' not in kwargs


def test_sell_put_steps_fallback_to_global_min_net_income() -> None:
    base = _add_repo_to_syspath()
    import scripts.sell_put_steps as steps
    import pandas as pd
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_put_scan = steps.run_sell_put_scan
    orig_add_labels = steps.add_sell_put_labels

    def _fake_run_sell_put_scan(**kwargs):
        calls.append(kwargs)
        Path(kwargs["output"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_csv(kwargs["output"], index=False)

    steps.run_sell_put_scan = _fake_run_sell_put_scan
    steps.add_sell_put_labels = lambda *args, **kwargs: None
    try:
        out = steps.run_sell_put_scan_and_summarize(
            py='python',
            base=base,
            sym='AAPL',
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL', 'sell_put': {}},
            sp={
                'enabled': True,
                'min_dte': 7,
                'max_dte': 45,
                'min_annualized_net_return': 0.1,
            },
            top_n=3,
            required_data_dir=base / 'output',
            report_dir=base / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            portfolio_ctx=None,
            global_sell_put_liquidity={'min_net_income': 100},
        )
    finally:
        steps.run_sell_put_scan = orig_run_sell_put_scan
        steps.add_sell_put_labels = orig_add_labels

    assert out['strategy'] == 'sell_put'
    assert calls
    kwargs = calls[0]
    assert kwargs['min_net_income'] == 14.000000000000002


def test_sell_call_steps_fallback_to_global_min_net_income() -> None:
    base = _add_repo_to_syspath()
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
            base=base,
            symbol='AAPL',
            symbol_lower='aapl',
            symbol_cfg={'symbol': 'AAPL'},
            cc={'enabled': True},
            top_n=3,
            required_data_dir=base / 'output',
            report_dir=base / 'output' / 'reports',
            timeout_sec=10,
            is_scheduled=True,
            stock={'shares': 200, 'avg_cost': 100.0},
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_by_symbol={'AAPL': 0},
            global_sell_call_liquidity={'min_net_income': 100},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert out['strategy'] == 'sell_call'
    assert calls
    kwargs = calls[0]
    assert kwargs['min_net_income'] == 14.000000000000002


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
        assert set(['engine_reject_stage', 'engine_reject_reason']).issubset(set(reject_log.columns))
