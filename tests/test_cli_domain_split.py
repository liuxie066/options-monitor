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
    out = parse_option_message_text(text)
    assert out['ok'] is True
    assert out['parsed']['symbol'] == '0700.HK'

    p = subprocess.run(
        [str(BASE / '.venv' / 'bin' / 'python'), 'scripts/cli/parse_option_message_cli.py', '--text', text],
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
                'scripts/cli/alert_engine_cli.py',
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
