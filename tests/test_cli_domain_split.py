from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
VPY = Path(sys.executable)


def test_parse_option_message_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from src.application.parse_option_message import parse_option_message_text

    text = '期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD'
    out = parse_option_message_text(text, accounts=['lx', 'sy'])
    assert out['ok'] is True
    assert out['parsed']['symbol'] == '0700.HK'

    p = subprocess.run(
        [
                str(VPY),
            '-m',
            'src.application.parse_option_message',
            '--text',
            text,
            '--accounts',
            'lx',
            'sy',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(p.stdout)
    assert payload['ok'] is True
    assert payload['parsed']['symbol'] == '0700.HK'


def test_alert_engine_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from src.application.alert_engine import run_alert_engine

    with TemporaryDirectory() as td:
        root = Path(td)
        summary_path = root / 'symbols_summary.csv'
        out_path = root / 'symbols_alerts.txt'
        changes_path = root / 'symbols_changes.txt'
        prev_path = root / 'symbols_summary_prev.csv'

        pd.DataFrame([
            {
                'symbol': '0700.HK',
                'strategy': 'sell_put',
                'candidate_count': 1,
                'top_contract': '0700.HK240101P500000',
                'annualized_return': 0.15,
                'net_income': 120.0,
                'dte': 20,
                'strike': 500,
                'risk_label': 'ok',
            }
        ]).to_csv(summary_path, index=False)

        result = run_alert_engine(
            summary_input=str(summary_path),
            output=str(out_path),
            changes_output=str(changes_path),
            previous_summary=str(prev_path),
        )
        assert '# Symbols Alerts' in result['alert_text']
        assert out_path.exists()

        subprocess.run(
            [
                str(VPY),
                '-m',
                'src.application.alert_engine',
                '--summary-input', str(summary_path),
                '--output', str(out_path),
                '--changes-output', str(changes_path),
                '--previous-summary', str(prev_path),
            ],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            check=True,
        )
        assert changes_path.exists()


def test_step4_domain_files_no_argparse_or_main() -> None:
    targets = [
        BASE / 'src' / 'application' / 'scan_scheduler.py',
        BASE / 'src' / 'application' / 'cash_headroom_query.py',
    ]
    for path in targets:
        text = path.read_text(encoding='utf-8')
        if path.name in ('scan_scheduler.py', 'cash_headroom_query.py'):
            assert 'import argparse' not in text
            assert '__main__' not in text
        if path.name == 'scan_scheduler.py':
            assert 'import subprocess' not in text
            assert 'scan-pipeline' not in text


def test_scan_scheduler_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from src.application.scan_scheduler import run_scheduler

    with TemporaryDirectory() as td:
        root = Path(td)
        cfg = root / 'scheduler_config.json'
        state = root / 'scheduler_state.json'

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
            ) + '\n',
            encoding='utf-8',
        )

        payload = run_scheduler(config=cfg, state=state, jsonl=True, base_dir=BASE)
        assert 'should_run_scan' in payload
        assert 'should_notify' in payload

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
        line = (p.stdout or '').strip().splitlines()[-1]
        cli_payload = json.loads(line)
        assert 'should_run_scan' in cli_payload
        assert 'should_notify' in cli_payload
