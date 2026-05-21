from __future__ import annotations

"""基础设施 service 层：统一承接外部进程与第三方 API 调用。"""

import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.fetch_source import is_futu_fetch_source
from domain.domain.multi_tick import resolve_openclaw_transport_channel
from src.infrastructure.opend_watchdog import run_watchdog_check


DEFAULT_OPEND_HOST = '127.0.0.1'
DEFAULT_OPEND_PORT = 11111
DEFAULT_NOTIFICATION_SEND_TIMEOUT_SEC = 60
MAX_NOTIFICATION_SEND_TIMEOUT_SEC = 300


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
    text: bool = False,
    timeout_sec: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
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
) -> subprocess.CompletedProcess[Any]:
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
) -> subprocess.CompletedProcess[Any]:
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
    retry_enabled: bool = False,
    retry_interval_sec: float = 3.0,
    retry_timeout_sec: float = 25.0,
    success_threshold: int = 2,
) -> dict[str, Any]:
    del vpy, base, timeout_sec
    health = run_watchdog_check(
        host=str(host),
        port=int(port),
        ensure=bool(ensure),
        retry_enabled=bool(retry_enabled),
        retry_interval_sec=float(retry_interval_sec),
        retry_timeout_sec=float(retry_timeout_sec),
        success_threshold=int(success_threshold),
    )
    return health.to_payload()


def _resolve_notification_send_timeout_sec(
    notifications: dict[str, Any] | None,
    *,
    default: int = DEFAULT_NOTIFICATION_SEND_TIMEOUT_SEC,
    max_value: int = MAX_NOTIFICATION_SEND_TIMEOUT_SEC,
) -> int:
    raw_value = notifications.get('send_timeout_sec') if isinstance(notifications, dict) else None
    try:
        timeout_sec = int(raw_value or default)
    except Exception:
        timeout_sec = int(default)
    return max(1, min(int(timeout_sec), int(max_value)))


def send_openclaw_message(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    timeout_sec: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    transport_channel = resolve_openclaw_transport_channel(channel)
    cmd = [
        'openclaw',
        'message',
        'send',
        '--channel',
        str(transport_channel),
        '--target',
        str(target),
        '--message',
        str(message),
        '--json',
    ]
    return run_command(cmd, cwd=base, capture_output=True, text=True, timeout_sec=timeout_sec)


def send_openclaw_message_process(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> subprocess.CompletedProcess[Any]:
    del idempotency_key
    return send_openclaw_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        timeout_sec=_resolve_notification_send_timeout_sec(notifications),
    )


def _resolve_watchlist_config(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        return []
    out: list[dict[str, Any]] = []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        broker = str(item.get("broker") or "").strip() or str(item.get("market") or "").strip()
        if broker:
            normalized["broker"] = broker
        normalized.pop("market", None)
        out.append(normalized)
    return out


def _resolve_opend_endpoint_for_market(cfg_obj: dict[str, Any], market: str) -> tuple[str, int]:
    host = DEFAULT_OPEND_HOST
    port = DEFAULT_OPEND_PORT
    mkt = str(market or '').upper().strip()

    try:
        for sym in _resolve_watchlist_config(cfg_obj):
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


def trading_day_via_futu(cfg_obj: dict[str, Any], market: str) -> tuple[bool | None, str]:
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

    host, port = _resolve_opend_endpoint_for_market(cfg_obj, market_used)

    try:
        ctx = OpenQuoteContext(host=host, port=port)
    except Exception:
        return (None, market_used)

    try:
        return _is_trading_day_via_futu(ctx, market_used)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _market_to_futu_trade_date_market(market: str) -> Any:
    try:
        from futu import TradeDateMarket
    except Exception:
        return None

    mapping = {
        "HK": "HK",
        "US": "US",
        "CN": "CN",
    }
    key = mapping.get(str(market or "").upper().strip())
    return getattr(TradeDateMarket, key, None) if key else None


def _trading_date(market: str) -> date:
    mkt = str(market or "").upper().strip()
    if mkt == "US":
        return datetime.now(ZoneInfo("America/New_York")).date()
    if mkt == "HK":
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    if mkt == "CN":
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return datetime.now(ZoneInfo("UTC")).date()


def _is_trading_day_via_futu(ctx: Any, market: str) -> tuple[bool | None, str]:
    market_used = str(market or "").upper().strip()
    futu_market = _market_to_futu_trade_date_market(market_used)
    if futu_market is None:
        return (None, market_used)

    trading_date = _trading_date(market_used)
    trading_date_text = trading_date.strftime("%Y-%m-%d")
    try:
        ret, data = ctx.request_trading_days(market=futu_market, start=trading_date_text, end=trading_date_text)
    except Exception:
        return (None, market_used)

    if ret != 0:
        return (None, market_used)

    rows = []
    if isinstance(data, list):
        rows = data
    elif hasattr(data, "to_dict"):
        try:
            rows = data.to_dict("records")  # type: ignore[attr-defined]
        except Exception:
            rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("time") or "") != trading_date_text:
            continue
        trade_date_type = str(row.get("trade_date_type") or "").upper()
        if trade_date_type in ("WHOLE", "MORNING", "AFTERNOON", "TRADING"):
            return (True, market_used)
    return (False, market_used)
