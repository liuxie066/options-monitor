from __future__ import annotations

"""基础设施 service 层：统一承接外部进程与第三方 API 调用。"""

import subprocess
from pathlib import Path

from domain.domain.fetch_source import is_futu_fetch_source
from scripts.config_loader import resolve_watchlist_config


DEFAULT_OPEND_HOST = '127.0.0.1'
DEFAULT_OPEND_PORT = 11111


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
    text: bool = False,
    timeout_sec: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=capture_output,
        text=text,
        timeout=timeout_sec,
        env=env,
    )


def run_scan_scheduler_cli(
    *,
    vpy: Path,
    base: Path,
    config: Path,
    state: Path,
    jsonl: bool = False,
    schedule_key: str | None = None,
    account: str | None = None,
    state_dir: Path | None = None,
    mark_scanned: bool = False,
    mark_notified: bool = False,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    cmd = [
        str(vpy),
        '-m',
        'src.interfaces.cli.main',
        'scheduler',
        '--config',
        str(config),
        '--state',
        str(state),
    ]
    if jsonl:
        cmd.append('--jsonl')
    if schedule_key:
        cmd.extend(['--schedule-key', str(schedule_key)])
    if account:
        cmd.extend(['--account', str(account)])
    if state_dir is not None:
        cmd.extend(['--state-dir', str(state_dir)])
    if mark_scanned:
        cmd.append('--mark-scanned')
    if mark_notified:
        cmd.append('--mark-notified')
    return run_command(cmd, cwd=base, capture_output=capture_output, text=True)


def run_pipeline_script(
    *,
    vpy: Path,
    base: Path,
    config: Path,
    report_dir: Path,
    state_dir: Path,
    mode: str = 'scheduled',
    shared_required_data: Path | None = None,
    shared_context_dir: Path | None = None,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        str(vpy),
        '-m',
        'src.interfaces.cli.main',
        'scan-pipeline',
        '--config',
        str(config),
        '--mode',
        str(mode),
        '--report-dir',
        str(report_dir),
        '--state-dir',
        str(state_dir),
    ]
    if shared_required_data is not None:
        cmd.extend(['--shared-required-data', str(shared_required_data)])
    if shared_context_dir is not None:
        cmd.extend(['--shared-context-dir', str(shared_context_dir)])
    return run_command(
        cmd,
        cwd=base,
        capture_output=capture_output,
        text=text,
        env=env,
    )


def run_opend_watchdog(
    *,
    vpy: Path,
    base: Path,
    host: str,
    port: int,
    ensure: bool = True,
    timeout_sec: int = 35,
) -> subprocess.CompletedProcess:
    cmd = [
        str(vpy),
        'scripts/opend_watchdog.py',
        '--host',
        str(host),
        '--port',
        str(port),
        '--json',
    ]
    if ensure:
        cmd.append('--ensure')
    return run_command(
        cmd,
        cwd=base,
        capture_output=True,
        text=True,
        timeout_sec=timeout_sec,
    )


def send_openclaw_message(*, base: Path, channel: str, target: str, message: str) -> subprocess.CompletedProcess:
    cmd = [
        'openclaw',
        'message',
        'send',
        '--channel',
        str(channel),
        '--target',
        str(target),
        '--message',
        str(message),
        '--json',
    ]
    return run_command(cmd, cwd=base, capture_output=True, text=True)


def _resolve_opend_endpoint_for_market(cfg_obj: dict, market: str) -> tuple[str, int]:
    host = DEFAULT_OPEND_HOST
    port = DEFAULT_OPEND_PORT
    mkt = str(market or '').upper().strip()

    try:
        for sym in resolve_watchlist_config(cfg_obj):
            if not isinstance(sym, dict):
                continue
            if str(sym.get('market') or '').upper() != mkt:
                continue
            fetch = (sym.get('fetch') or {})
            if not is_futu_fetch_source(fetch.get('source')):
                continue
            host = str(fetch.get('host') or host)
            port = int(fetch.get('port') or port)
            break
    except Exception:
        pass

    return host, port


def trading_day_via_futu(cfg_obj: dict, market: str) -> tuple[bool | None, str]:
    """读取交易日状态。

    返回值：
    - `(True/False, market)`：成功得到交易日判断
    - `(None, market)`：外部依赖不可用/调用失败，调用方应按“不中断主流程”策略处理
    """
    market_used = str(market or '').upper().strip() or 'US'

    try:
        from futu import OpenQuoteContext
    except Exception:
        return (None, market_used)

    try:
        from scripts.opend_utils import is_trading_day_via_futu
    except Exception:
        return (None, market_used)

    host, port = _resolve_opend_endpoint_for_market(cfg_obj, market_used)

    try:
        ctx = OpenQuoteContext(host=host, port=port)
    except Exception:
        return (None, market_used)

    try:
        return is_trading_day_via_futu(ctx, market_used)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
