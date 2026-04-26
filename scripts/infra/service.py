from __future__ import annotations

"""基础设施 service 层：统一承接外部进程与第三方 API 调用。"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from domain.domain.fetch_source import is_futu_fetch_source
from domain.domain.tool_boundary import normalize_subprocess_adapter_payload
from scripts.config_loader import resolve_watchlist_config
from scripts.feishu_bitable import FeishuError, get_tenant_access_token, http_json


DEFAULT_OPEND_HOST = '127.0.0.1'
DEFAULT_OPEND_PORT = 11111
DEFAULT_NOTIFICATION_FEISHU_APP_SECRETS = 'secrets/notifications.feishu.app.json'


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


def _resolve_notification_secrets_file(
    *,
    base: Path,
    notifications: dict[str, Any] | None = None,
    secrets_file: str | Path | None = None,
) -> Path:
    raw_path = secrets_file
    if raw_path is None and isinstance(notifications, dict):
        raw_path = notifications.get('secrets_file')
    path_value = str(raw_path or DEFAULT_NOTIFICATION_FEISHU_APP_SECRETS).strip()
    path = Path(path_value)
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def load_feishu_notification_app_config(
    *,
    base: Path,
    notifications: dict[str, Any] | None = None,
    secrets_file: str | Path | None = None,
) -> dict[str, str]:
    secrets_path = _resolve_notification_secrets_file(base=base, notifications=notifications, secrets_file=secrets_file)
    if not secrets_path.exists():
        raise ValueError(f'notification secrets file not found: {secrets_path}')

    try:
        payload = json.loads(secrets_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'notification secrets file is not valid json: {secrets_path}') from exc

    feishu = payload.get('feishu') if isinstance(payload, dict) else None
    if not isinstance(feishu, dict):
        raise ValueError(f'notification secrets missing feishu object: {secrets_path}')

    app_id = str(feishu.get('app_id') or '').strip()
    app_secret = str(feishu.get('app_secret') or '').strip()
    if not app_id or not app_secret:
        raise ValueError(f'notification secrets missing feishu.app_id/app_secret: {secrets_path}')

    return {
        'app_id': app_id,
        'app_secret': app_secret,
        'secrets_file': str(secrets_path),
    }


def send_feishu_app_message(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    receive_id_type: str = 'open_id',
) -> dict[str, Any]:
    resolved_channel = str(channel or '').strip().lower()
    if resolved_channel != 'feishu':
        raise ValueError(f'unsupported notification channel for feishu app sender: {channel}')

    receive_id = str(target or '').strip()
    if not receive_id:
        raise ValueError('notifications.target is required')
    if receive_id_type != 'open_id':
        raise ValueError(f'unsupported receive_id_type for phase1: {receive_id_type}')

    app_cfg = load_feishu_notification_app_config(base=base, notifications=notifications)
    tenant_access_token = get_tenant_access_token(app_cfg['app_id'], app_cfg['app_secret'])
    request_path = f'/open-apis/im/v1/messages?receive_id_type={receive_id_type}'
    url = f'https://open.feishu.cn{request_path}'
    headers = {
        'Authorization': f'Bearer {tenant_access_token}',
        'Content-Type': 'application/json; charset=utf-8',
    }
    payload = {
        'receive_id': receive_id,
        'msg_type': 'text',
        'content': json.dumps({'text': str(message or '')}, ensure_ascii=False),
    }

    try:
        response_json = http_json('POST', url, payload=payload, headers=headers)
        return {
            'ok': True,
            'http_status': 200,
            'request_path': request_path,
            'response_json': response_json,
            'response_tail': json.dumps(response_json, ensure_ascii=False)[-500:],
        }
    except FeishuError as exc:
        response = exc.response if isinstance(exc.response, dict) else {}
        body_text = str(response.get('body') or '')
        response_json = response if isinstance(response.get('code'), int) else None
        if body_text:
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, dict):
                    response_json = parsed
            except Exception:
                pass
        return {
            'ok': False,
            'http_status': response.get('http_status'),
            'request_path': request_path,
            'response_json': response_json,
            'response_tail': body_text[-500:],
            'error_type': type(exc).__name__,
            'error_message': str(exc),
        }


def normalize_feishu_app_send_output(*, send_result: dict[str, Any]) -> dict[str, Any]:
    result = send_result if isinstance(send_result, dict) else {}
    response_json = result.get('response_json') if isinstance(result.get('response_json'), dict) else {}
    data = response_json.get('data') if isinstance(response_json.get('data'), dict) else {}
    message_id = data.get('message_id')
    http_status = result.get('http_status')
    feishu_code = response_json.get('code') if isinstance(response_json.get('code'), int) else None
    feishu_msg = str(response_json.get('msg') or result.get('error_message') or '').strip()
    request_path = str(result.get('request_path') or '/open-apis/im/v1/messages?receive_id_type=open_id')
    response_tail = str(result.get('response_tail') or '')

    command_ok = (http_status == 200)
    delivery_confirmed = bool(command_ok and feishu_code == 0 and message_id)
    ok = delivery_confirmed

    if ok:
        message = f'message_id={message_id}'
    elif command_ok and feishu_code == 0 and not message_id:
        message = 'feishu send returned success but data.message_id is missing'
    else:
        parts = [
            f'http_status={http_status}',
            f'feishu_code={feishu_code}',
            f'feishu_msg={feishu_msg or ""}',
            f'message_id={message_id}',
            f'request_path={request_path}',
        ]
        if response_tail:
            parts.append(f'response_tail={response_tail}')
        message = ' '.join(parts)

    return normalize_subprocess_adapter_payload(
        adapter='notify',
        tool_name='feishu_app_message_send',
        returncode=(0 if command_ok else 1),
        stdout=response_tail,
        stderr='',
        ok=ok,
        message=message,
        extra={
            'command_ok': command_ok,
            'delivery_confirmed': delivery_confirmed,
            'message_id': (None if message_id is None else str(message_id)),
            'http_status': http_status,
            'feishu_code': feishu_code,
            'feishu_msg': feishu_msg,
            'request_path': request_path,
            'response_tail': response_tail,
        },
    )


def send_feishu_app_message_process(*, base: Path, channel: str, target: str, message: str, notifications: dict[str, Any] | None = None):
    send_result = send_feishu_app_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        notifications=notifications,
    )
    normalized = normalize_feishu_app_send_output(send_result=send_result)
    stdout = ''
    if isinstance(send_result, dict):
        response_json = send_result.get('response_json')
        if isinstance(response_json, dict) and response_json:
            stdout = json.dumps(response_json, ensure_ascii=False)
        elif send_result.get('response_tail'):
            stdout = str(send_result.get('response_tail') or '')
    stderr = '' if bool(normalized.get('command_ok')) else str(normalized.get('message') or '')
    return SimpleNamespace(returncode=int(normalized.get('returncode') or 0), stdout=stdout, stderr=stderr, raw=send_result)


def _resolve_opend_endpoint_for_market(cfg_obj: dict, market: str) -> tuple[str, int]:
    host = DEFAULT_OPEND_HOST
    port = DEFAULT_OPEND_PORT
    mkt = str(market or '').upper().strip()

    try:
        for sym in resolve_watchlist_config(cfg_obj):
            if not isinstance(sym, dict):
                continue
            if str(sym.get('broker') or '').upper() != mkt:
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
