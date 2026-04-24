from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
VPY = BASE / '.venv' / 'bin' / 'python'
TEST_ROOT = BASE / 'output' / 'state' / 'test_cli_domain_split_step4'


DOMAIN_FILES = [
    BASE / 'scripts' / 'scan_scheduler.py',
    BASE / 'scripts' / 'query_sell_put_cash.py',
]


def _clean_dir(path: Path) -> None:
    if path.exists():
        for p in sorted(path.rglob('*'), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                p.rmdir()
    path.mkdir(parents=True, exist_ok=True)


def test_stage4_domain_files_without_argparse_or_main() -> None:
    for path in DOMAIN_FILES:
        text = path.read_text(encoding='utf-8')
        assert 'import argparse' not in text
        assert '__main__' not in text


def test_render_alerts_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.render_sell_put_alerts import render_sell_put_alerts
    from scripts.render_sell_call_alerts import render_sell_call_alerts

    root = TEST_ROOT / 'render'
    _clean_dir(root)
    put_in = root / 'put.csv'
    call_in = root / 'call.csv'
    put_out = root / 'put.txt'
    call_out = root / 'call.txt'

    pd.DataFrame([
        {
            'symbol': 'AAPL',
            'expiration': '2026-12-18',
            'strike': 150.0,
            'spot': 170.0,
            'dte': 30,
            'mid': 2.5,
            'net_income': 250.0,
            'annualized_net_return_on_cash_basis': 0.2,
            'otm_pct': 0.1,
            'risk_label': '中性',
            'open_interest': 100,
            'volume': 10,
            'spread_ratio': 0.1,
            'currency': 'USD',
        }
    ]).to_csv(put_in, index=False)

    pd.DataFrame([
        {
            'symbol': 'AAPL',
            'expiration': '2026-12-18',
            'strike': 190.0,
            'spot': 170.0,
            'dte': 30,
            'mid': 1.5,
            'avg_cost': 120.0,
            'shares_total': 100,
            'shares_locked': 0,
            'shares_available_for_cover': 100,
            'covered_contracts_available': 1,
            'is_fully_covered_available': True,
            'net_income': 150.0,
            'annualized_net_premium_return': 0.12,
            'if_exercised_total_return': 0.2,
            'strike_above_spot_pct': 0.1,
            'strike_above_cost_pct': 0.58,
            'risk_label': '中性',
            'open_interest': 100,
            'volume': 10,
            'spread_ratio': 0.1,
            'currency': 'USD',
        }
    ]).to_csv(call_in, index=False)

    put_result = render_sell_put_alerts(input_path=str(put_in), output_path=str(put_out), symbol='AAPL')
    call_result = render_sell_call_alerts(input_path=str(call_in), output_path=str(call_out), symbol='AAPL')

    assert '[Sell Put 候选]' in put_result
    assert '[Sell Call 候选]' in call_result
    assert put_out.exists()
    assert call_out.exists()

    p = subprocess.run(
        [
            str(VPY),
            'scripts/render_sell_put_alerts.py',
            '--input',
            str(put_in),
            '--output',
            str(put_out),
            '--symbol',
            'AAPL',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    assert '[DONE] alerts ->' in p.stdout


def test_scan_scheduler_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.scan_scheduler import run_scheduler

    root = TEST_ROOT / 'scheduler'
    _clean_dir(root)
    cfg = root / 'cfg.json'
    state = root / 'state.json'
    cfg.write_text(
        json.dumps(
            {
                'schedule': {
                    'enabled': True,
                    'market_timezone': 'UTC',
                    'beijing_timezone': 'UTC',
                    'market_open': '00:00',
                    'market_close': '23:59',
                    'market_dense_interval_min': 1,
                }
            },
            ensure_ascii=False,
        )
        + '\n',
        encoding='utf-8',
    )

    out = run_scheduler(config=str(cfg), state=str(state), state_dir=str(root), schedule_key='schedule')
    assert 'should_run_scan' in out

    p = subprocess.run(
        [
            str(VPY),
            '-m',
            'src.interfaces.cli.main',
            'scheduler',
            '--config',
            str(cfg),
            '--state',
            str(state),
            '--jsonl',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads((p.stdout or '').strip())
    assert 'should_run_scan' in payload


def test_query_sell_put_cash_domain_minimal() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    import scripts.query_sell_put_cash as m

    def fake_load_account_portfolio_context(**_kwargs):
        return {'cash_by_currency': {'CNY': 100000.0, 'USD': 1000.0}, 'stocks_by_symbol': {}, 'portfolio_source_name': 'holdings'}

    old_load_portfolio = m.load_account_portfolio_context
    old_load_option_position_records = m.load_option_position_records
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    old_build_context = m.build_option_positions_context
    m.load_account_portfolio_context = fake_load_account_portfolio_context
    m.load_option_position_records = lambda *_a, **_k: []
    m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {'rates': {'USDCNY': 7.2, 'HKDCNY': 0.92}}
    m.build_option_positions_context = lambda *_a, **_k: {
        'cash_secured_by_symbol_by_ccy': {'AAPL': {'USD': 200.0}},
        'cash_secured_total_by_ccy': {'USD': 200.0},
        'cash_secured_total_cny': 1440.0,
    }
    try:
        out_dir = TEST_ROOT / 'cash_query'
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(market='富途', account='lx', out_dir=str(out_dir))
        assert 'cash_free_cny' in result
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.load_option_position_records = old_load_option_position_records
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates
        m.build_option_positions_context = old_build_context


def test_new_cli_files_exist_and_help_ok() -> None:
    cli_files = [
        'scripts/render_sell_put_alerts.py',
        'scripts/render_sell_call_alerts.py',
    ]
    for rel in cli_files:
        p = subprocess.run(
            [str(VPY), rel, '--help'],
            cwd=str(BASE),
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0
    for argv in (
        ['-m', 'src.interfaces.cli.main', 'scheduler', '--help'],
        ['-m', 'src.interfaces.cli.main', 'sell-put-cash', '--help'],
    ):
        p = subprocess.run(
            [str(VPY), *argv],
            cwd=str(BASE),
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0
