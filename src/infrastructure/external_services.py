from __future__ import annotations

"""基础设施 service 层：统一承接外部进程与第三方 API 调用。"""

import base64
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.opend_watchdog import run_watchdog_check


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
    force: bool = False,
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
    if force:
        cmd.append('--force')
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
    symbols_arg: str | None = None,
    position_advice_account_run_id: str | None = None,
    required_data_snapshot_manifest: Path | None = None,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
    account_config_base: Path | None = None,
    account_config_run_id: str | None = None,
    account_config_account: str | None = None,
    account_config_compatibility_path: Path | None = None,
    account_config_sha256: str | None = None,
    account_config_canonical_bytes: bytes | None = None,
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
    if str(symbols_arg or '').strip():
        cmd.extend(['--symbols', str(symbols_arg).strip()])
    if str(position_advice_account_run_id or "").strip():
        cmd.extend(
            [
                "--position-advice-account-run-id",
                str(position_advice_account_run_id).strip(),
            ]
        )
    if required_data_snapshot_manifest is not None:
        cmd.extend(
            [
                "--required-data-snapshot-manifest",
                str(Path(required_data_snapshot_manifest).resolve()),
            ]
        )
    if prepared_portfolio_context_manifest is not None:
        digest = str(prepared_portfolio_context_manifest_sha256 or "").strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                "prepared portfolio context manifest requires its retained SHA-256"
            )
        cmd.extend(
            [
                "--prepared-portfolio-context-manifest",
                str(Path(prepared_portfolio_context_manifest).resolve()),
                "--prepared-portfolio-context-manifest-sha256",
                digest,
            ]
        )
    elif prepared_portfolio_context_manifest_sha256 is not None:
        raise ValueError(
            "prepared portfolio context manifest SHA-256 requires a manifest"
        )
    if prepared_option_positions_context_manifest is not None:
        digest = str(
            prepared_option_positions_context_manifest_sha256 or ""
        ).strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                "prepared option context manifest requires its retained SHA-256"
            )
        cmd.extend(
            [
                "--prepared-option-positions-context-manifest",
                str(
                    Path(
                        prepared_option_positions_context_manifest
                    ).resolve()
                ),
                "--prepared-option-positions-context-manifest-sha256",
                digest,
            ]
        )
    elif prepared_option_positions_context_manifest_sha256 is not None:
        raise ValueError(
            "prepared option context manifest SHA-256 requires a manifest"
        )
    account_config_authority = (
        account_config_base,
        account_config_run_id,
        account_config_account,
        account_config_compatibility_path,
        account_config_sha256,
        account_config_canonical_bytes,
    )
    if any(value is not None for value in account_config_authority):
        if not all(
            value is not None and str(value).strip()
            for value in account_config_authority
        ):
            raise ValueError("account config authority arguments must be complete")
        assert account_config_base is not None
        assert account_config_run_id is not None
        assert account_config_account is not None
        assert account_config_compatibility_path is not None
        assert account_config_sha256 is not None
        if not isinstance(account_config_canonical_bytes, bytes) or not account_config_canonical_bytes:
            raise ValueError(
                "account config authority requires retained canonical bytes"
            )
        compatibility_authority_path = Path(
            account_config_compatibility_path
        ).expanduser()
        if not compatibility_authority_path.is_absolute():
            raise ValueError(
                "account config compatibility authority path must be absolute"
            )
        compatibility_authority_path = Path(
            os.path.abspath(str(compatibility_authority_path))
        )
        cmd.extend(
            [
                "--account-config-base",
                str(Path(account_config_base).resolve()),
                "--account-config-run-id",
                str(account_config_run_id).strip(),
                "--account-config-account",
                str(account_config_account).strip(),
                "--account-config-compatibility-path",
                str(compatibility_authority_path),
                "--account-config-sha256",
                str(account_config_sha256).strip().lower(),
            ]
        )
        child_env = dict(os.environ if env is None else env)
        child_env["OM_ACCOUNT_CONFIG_CANONICAL_B64"] = base64.b64encode(
            account_config_canonical_bytes
        ).decode("ascii")
    else:
        child_env = env
    return run_command(
        cmd,
        cwd=base,
        capture_output=capture_output,
        text=text,
        env=child_env,
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
    required_capability: str = "both",
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
        required_capability=required_capability,
    )
    return health.to_payload()


def trading_day_via_futu(
    *,
    host: str,
    port: int,
    market: str,
) -> tuple[bool | None, str]:
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
        ctx = OpenQuoteContext(host=str(host), port=int(port))
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
