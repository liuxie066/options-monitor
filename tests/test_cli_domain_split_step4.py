from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
VPY = Path(sys.executable)
TEST_ROOT = BASE / 'output' / 'state' / 'test_cli_domain_split_step4'


DOMAIN_FILES = [
    BASE / 'src' / 'application' / 'scan_scheduler.py',
    BASE / 'src' / 'application' / 'cash_headroom_query.py',
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


def test_scan_scheduler_domain_and_cli() -> None:

    from src.application.scan_scheduler import run_scheduler

    root = TEST_ROOT / 'scheduler'
    _clean_dir(root)
    cfg = root / 'cfg.json'
    state = root / 'state.json'
    cfg.write_text(
        json.dumps(
            {
                'schedule': {
                    'enabled': True,
                    'timezone': 'UTC',
                    'beijing_timezone': 'UTC',
                    'run_window': {'start': '00:00', 'end': '23:59', 'breaks': []},
                    'run_points': {'start_plus_min': 0, 'hourly_minute': 0, 'end_minus_min': 0},
                    'cron_interval_min': 10,
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

    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**_kwargs):
        return {'cash_by_currency': {'CNY': 100000.0, 'USD': 1000.0}, 'stocks_by_symbol': {}, 'portfolio_source_name': 'holdings'}

    old_load_portfolio = m.load_account_portfolio_context
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    m.load_account_portfolio_context = fake_load_account_portfolio_context
    m._load_option_position_records = lambda *_a, **_k: []
    m.build_option_positions_context = lambda *_a, **_k: {
        'cash_secured_by_symbol_by_ccy': {'AAPL': {'USD': 200.0}},
        'cash_secured_total_by_ccy': {'USD': 200.0},
        'cash_secured_total_cny': 1440.0,
    }
    try:
        out_dir = TEST_ROOT / 'cash_query'
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            market='富途',
            account='lx',
            out_dir=str(out_dir),
            no_exchange_rates=True,
        )
        assert 'cash_free_cny' in result
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m._load_option_position_records = old_load_option_position_records
        m.build_option_positions_context = old_build_context


def test_new_cli_modules_help_ok() -> None:
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
