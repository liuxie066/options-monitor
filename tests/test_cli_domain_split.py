from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

BASE = Path(__file__).resolve().parents[1]


def test_parse_option_message_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.parse_option_message import parse_option_message_text

    text = '期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD'
    out = parse_option_message_text(text, accounts=['lx', 'sy'])
    assert out['ok'] is True
    assert out['parsed']['symbol'] == '0700.HK'

    p = subprocess.run(
        [str(BASE / '.venv' / 'bin' / 'python'), 'scripts/parse_option_message.py', '--text', text, '--accounts', 'lx', 'sy'],
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

    from scripts.alert_engine import run_alert_engine

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
                str(BASE / '.venv' / 'bin' / 'python'),
                'scripts/alert_engine.py',
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
        BASE / 'scripts' / 'scan_scheduler.py',
        BASE / 'scripts' / 'render_sell_put_alerts.py',
        BASE / 'scripts' / 'render_sell_call_alerts.py',
        BASE / 'scripts' / 'query_sell_put_cash.py',
    ]
    for path in targets:
        text = path.read_text(encoding='utf-8')
        if path.name in ('scan_scheduler.py', 'query_sell_put_cash.py'):
            assert 'import argparse' not in text
            assert '__main__' not in text


def test_scan_scheduler_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.scan_scheduler import run_scheduler

    with TemporaryDirectory() as td:
        root = Path(td)
        cfg = root / 'scheduler_config.json'
        state = root / 'scheduler_state.json'

        cfg.write_text(
            json.dumps(
                {
                    'schedule': {
                        'enabled': True,
                        'market_timezone': 'UTC',
                        'beijing_timezone': 'UTC',
                        'market_open': '00:00',
                        'market_close': '23:59',
                        'monitor_off_hours': True,
                        'market_hours_interval_min': 10,
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
                str(BASE / '.venv' / 'bin' / 'python'),
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


def test_render_sell_put_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.render_sell_put_alerts import render_sell_put_alerts

    with TemporaryDirectory() as td:
        root = Path(td)
        csv_path = root / 'sell_put_candidates_labeled.csv'
        out_path = root / 'sell_put_alerts.txt'

        pd.DataFrame(
            [
                {
                    'symbol': 'AAPL',
                    'expiration': '2026-05-15',
                    'strike': 180.0,
                    'spot': 200.0,
                    'dte': 30,
                    'mid': 2.5,
                    'net_income': 250.0,
                    'annualized_net_return_on_cash_basis': 0.2,
                    'otm_pct': 0.1,
                    'risk_label': '中性',
                    'spread_ratio': 0.1,
                    'open_interest': 100,
                    'volume': 50,
                }
            ]
        ).to_csv(csv_path, index=False)

        text = render_sell_put_alerts(input_path=csv_path, output_path=out_path, top=1, layered=True, base_dir=BASE)
        assert '[Sell Put 候选]' in text
        assert out_path.exists()

        subprocess.run(
            [
                str(BASE / '.venv' / 'bin' / 'python'),
                'scripts/render_sell_put_alerts.py',
                '--input',
                str(csv_path),
                '--output',
                str(out_path),
                '--top',
                '1',
                '--layered',
            ],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            check=True,
        )
        assert '[Sell Put 候选]' in out_path.read_text(encoding='utf-8')


def test_render_sell_call_domain_and_cli() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.render_sell_call_alerts import render_sell_call_alerts

    with TemporaryDirectory() as td:
        root = Path(td)
        csv_path = root / 'sell_call_candidates.csv'
        out_path = root / 'sell_call_alerts.txt'

        pd.DataFrame(
            [
                {
                    'symbol': 'AAPL',
                    'expiration': '2026-05-15',
                    'strike': 220.0,
                    'spot': 200.0,
                    'dte': 30,
                    'mid': 2.5,
                    'net_income': 250.0,
                    'annualized_net_premium_return': 0.12,
                    'if_exercised_total_return': 0.2,
                    'strike_above_spot_pct': 0.1,
                    'strike_above_cost_pct': 0.15,
                    'risk_label': '中性',
                    'spread_ratio': 0.1,
                    'open_interest': 100,
                    'volume': 50,
                    'shares_total': 200,
                    'shares_locked': 0,
                    'shares_available_for_cover': 200,
                    'covered_contracts_available': 2,
                    'is_fully_covered_available': True,
                }
            ]
        ).to_csv(csv_path, index=False)

        text = render_sell_call_alerts(input_path=csv_path, output_path=out_path, top=1, layered=True, base_dir=BASE)
        assert '[Sell Call 候选]' in text
        assert out_path.exists()

        subprocess.run(
            [
                str(BASE / '.venv' / 'bin' / 'python'),
                'scripts/render_sell_call_alerts.py',
                '--input',
                str(csv_path),
                '--output',
                str(out_path),
                '--top',
                '1',
                '--layered',
            ],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            check=True,
        )
        assert '[Sell Call 候选]' in out_path.read_text(encoding='utf-8')
