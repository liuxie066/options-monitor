from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[1]
VPY = Path(sys.executable)


def _cli_stdout(*args: str) -> str:
    """Run the CLI in a subprocess and return its stdout."""
    completed = subprocess.run([str(VPY), *args], cwd=str(BASE), capture_output=True, text=True, check=True)
    return completed.stdout or ''


def test_parse_option_message_domain_and_cli() -> None:

    from src.application.parse_option_message import parse_option_message_text

    text = '期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD'
    out = parse_option_message_text(text, accounts=['lx', 'sy'])
    assert out['ok'] is True
    assert out['parsed']['symbol'] == '0700.HK'

    stdout = _cli_stdout('-m', 'src.application.parse_option_message', '--text', text, '--accounts', 'lx', 'sy')
    payload = json.loads(stdout)
    assert payload['ok'] is True
    assert payload['parsed']['symbol'] == '0700.HK'


def _call_alert_engine(entry: str, paths: dict[str, Path], *, update_snapshot: bool):
    if entry == 'cli':
        args = [str(VPY), '-m', 'src.application.alert_engine']
        for key, path in paths.items():
            args.extend(['--' + key.replace('_', '-'), str(path)])
        if update_snapshot:
            args.append('--update-snapshot')
        return subprocess.run(args, cwd=BASE, capture_output=True, text=True, timeout=30)
    if entry == 'stage':
        from src.application.pipeline_reporting import run_pipeline_alert_stage

        return run_pipeline_alert_stage(**paths, update_snapshot=update_snapshot)

    from src.application.alert_engine import run_alert_engine

    return run_alert_engine(**{key: str(path) for key, path in paths.items()}, update_snapshot=update_snapshot)


@pytest.mark.parametrize('entry', ['api', 'cli', 'stage'])
@pytest.mark.parametrize('update_snapshot', [False, True])
@pytest.mark.parametrize('existing_outputs', [False, True])
@pytest.mark.parametrize('current_input', ['missing', 'zero-byte', 'whitespace'])
def test_alert_engine_rejects_unavailable_current_summary_without_writes(
    tmp_path: Path, entry: str, update_snapshot: bool, existing_outputs: bool, current_input: str,
) -> None:
    paths = {
        'summary_input': tmp_path / 'input' / 'symbols_summary.csv',
        'output': tmp_path / 'reports' / 'symbols_alerts.txt',
        'changes_output': tmp_path / 'reports' / 'symbols_changes.txt',
        'previous_summary': tmp_path / 'state' / 'symbols_summary_prev.csv',
    }
    if current_input != 'missing':
        paths['summary_input'].parent.mkdir()
        paths['summary_input'].write_bytes(b'' if current_input == 'zero-byte' else b' \n\t\n')
    if existing_outputs:
        for key in ('output', 'changes_output', 'previous_summary'):
            paths[key].parent.mkdir(parents=True, exist_ok=True)
        paths['output'].write_bytes(b'previous alerts\n')
        paths['changes_output'].write_bytes(b'previous changes\n')
        paths['previous_summary'].write_bytes(b'symbol,strategy,candidate_count\n0700.HK,sell_put,1\n')
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    error = FileNotFoundError if current_input == 'missing' else pd.errors.EmptyDataError

    if entry == 'cli':
        result = _call_alert_engine(entry, paths, update_snapshot=update_snapshot)
        assert result.returncode != 0
        assert error.__name__ in result.stderr
        assert '[DONE]' not in result.stdout
        assert 'snapshot updated' not in result.stdout
    else:
        with pytest.raises(error):
            _call_alert_engine(entry, paths, update_snapshot=update_snapshot)

    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('entry', ['api', 'cli', 'stage'])
@pytest.mark.parametrize('update_snapshot', [False, True])
@pytest.mark.parametrize('previous_input', ['missing', 'zero-byte', 'header-only'])
def test_alert_engine_accepts_header_only_current_summary_and_optional_previous(
    tmp_path: Path, entry: str, update_snapshot: bool, previous_input: str,
) -> None:
    paths = {
        'summary_input': tmp_path / 'symbols_summary.csv',
        'output': tmp_path / 'reports' / 'symbols_alerts.txt',
        'changes_output': tmp_path / 'reports' / 'symbols_changes.txt',
        'previous_summary': tmp_path / 'state' / 'symbols_summary_prev.csv',
    }
    current_bytes = b'symbol,strategy,candidate_count\n'
    paths['summary_input'].write_bytes(current_bytes)
    if previous_input != 'missing':
        paths['previous_summary'].parent.mkdir()
        paths['previous_summary'].write_bytes(b'' if previous_input == 'zero-byte' else current_bytes)
    previous_bytes = paths['previous_summary'].read_bytes() if paths['previous_summary'].exists() else None

    result = _call_alert_engine(entry, paths, update_snapshot=update_snapshot)
    if entry == 'cli':
        assert result.returncode == 0, result.stderr
        assert '[DONE] alerts' in result.stdout
        assert ('snapshot updated' in result.stdout) is update_snapshot
    elif entry == 'api':
        assert result['snapshot_updated'] is update_snapshot
    assert paths['output'].read_text(encoding='utf-8') == '# Symbols Alerts\n\n无提醒。\n'
    assert paths['changes_output'].read_text(encoding='utf-8') == '# Symbols Changes\n\n无变化。\n'
    assert paths['summary_input'].read_bytes() == current_bytes
    if update_snapshot:
        assert paths['previous_summary'].read_bytes() == current_bytes
    elif previous_bytes is None:
        assert not paths['previous_summary'].exists()
    else:
        assert paths['previous_summary'].read_bytes() == previous_bytes


def test_alert_engine_domain_and_cli(tmp_path: Path) -> None:

    from src.application.alert_engine import run_alert_engine

    td = tmp_path
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

    _cli_stdout(
        '-m', 'src.application.alert_engine', '--summary-input', str(summary_path),
        '--output', str(out_path), '--changes-output', str(changes_path), '--previous-summary', str(prev_path),
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


def test_scan_scheduler_domain_and_cli(tmp_path: Path) -> None:

    from src.application.scan_scheduler import run_scheduler

    td = tmp_path
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

    stdout = _cli_stdout(
        '-m', 'src.interfaces.cli.main', 'scheduler', '--config', str(cfg), '--state', str(state), '--jsonl',
    )
    line = (stdout or '').strip().splitlines()[-1]
    cli_payload = json.loads(line)
    assert 'should_run_scan' in cli_payload
    assert 'should_notify' in cli_payload
