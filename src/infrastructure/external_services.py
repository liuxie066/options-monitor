from __future__ import annotations

"""基础设施 service 层：统一承接外部进程与第三方 API 调用。"""

import base64
import os
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.infrastructure.futu_gateway import build_futu_gateway, check_fetch_control
from src.infrastructure.opend_watchdog import classify_watchdog_result, port_open


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
    text: bool = False,
    timeout_sec: float | None = None,
    env: dict[str, str] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[Any]:
    check_fetch_control(deadline, cancelled)
    if deadline is not None:
        remaining = max(0.0, deadline - time.monotonic())
        timeout_sec = remaining if timeout_sec is None else min(timeout_sec, remaining)
    if cancelled is None:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=capture_output, text=text,
            timeout=timeout_sec, env=env,
        )
        check_fetch_control(deadline, cancelled)
        return result

    # communicate() remains interruptible when the caller supplies cancellation.
    stop = None if timeout_sec is None else time.monotonic() + timeout_sec
    with subprocess.Popen(
        cmd, cwd=str(cwd), text=text, env=env,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    ) as process:
        try:
            while True:
                check_fetch_control(deadline, cancelled)
                remaining = .05 if stop is None else stop - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout_sec)
                try:
                    stdout, stderr = process.communicate(timeout=min(.05, remaining))
                except subprocess.TimeoutExpired:
                    continue
                check_fetch_control(deadline, cancelled)
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        except BaseException:
            process.kill()
            process.communicate()
            raise


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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
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
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    return run_command(cmd, cwd=base, capture_output=capture_output, text=True, **controls)


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
    source_account_run_id: str | None = None,
    required_data_snapshot_manifest: Path | None = None,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
    account_config_base: Path | None = None,
    account_config_run_id: str | None = None,
    account_config_account: str | None = None,
    account_config_sha256: str | None = None,
    account_config_canonical_bytes: bytes | None = None,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    experience: bool = False,
    account_display_name: str | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
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
    if str(source_account_run_id or "").strip():
        cmd.extend(
            [
                "--source-account-run-id",
                str(source_account_run_id).strip(),
            ]
        )
    if experience:
        display_name = str(account_display_name or "").strip()
        if not display_name:
            raise ValueError("experience pipeline requires an account display name")
        cmd.extend(
            [
                "--experience",
                "--account-display-name",
                display_name,
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
        assert account_config_sha256 is not None
        if not isinstance(account_config_canonical_bytes, bytes) or not account_config_canonical_bytes:
            raise ValueError(
                "account config authority requires retained canonical bytes"
            )
        cmd.extend(
            [
                "--account-config-base",
                str(Path(account_config_base).resolve()),
                "--account-config-run-id",
                str(account_config_run_id).strip(),
                "--account-config-account",
                str(account_config_account).strip(),
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
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    return run_command(
        cmd,
        cwd=base,
        capture_output=capture_output,
        text=text,
        env=child_env,
        **controls,
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
    expected_account_ids: list[str] | None = None,
    trd_env: str = "REAL",
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[Any] | dict[str, Any]:
    command = [
        str(vpy), "-m", "src.infrastructure.opend_watchdog", "--json",
        "--host", str(host), "--port", str(port),
        "--retry-interval-sec", str(retry_interval_sec),
        "--retry-timeout-sec", str(retry_timeout_sec),
        "--success-threshold", str(success_threshold),
        "--required-capability", required_capability,
    ]
    if ensure:
        command.append("--ensure")
    if retry_enabled:
        command.append("--retry-enabled")
    if required_capability == "broker":
        command.extend(["--trd-env", trd_env])
        for account_id in expected_account_ids or []:
            command.extend(["--expected-account-id", str(account_id)])
    try:
        controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
        return run_command(command, cwd=base, capture_output=True, text=True, timeout_sec=timeout_sec, **controls)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part)
            for part in (exc.stdout, exc.stderr) if part
        )
        code, message = classify_watchdog_result(None, output)
        if code == "OPEND_LOGIN_INVALID":
            return {"ok": False, "error_code": code, "message": message}
        raise


def trading_day_via_futu(
    *,
    host: str,
    port: int,
    market: str,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool | None, str]:
    """读取交易日状态。

    返回值：
    - `(True/False, market)`：成功得到交易日判断
    - `(None, market)`：外部依赖不可用/调用失败，调用方应按“不中断主流程”策略处理
    """
    market_used = str(market or '').upper().strip() or 'US'

    check_fetch_control(deadline, cancelled)
    probe = {} if deadline is None else {"timeout": min(.8, max(0.0, deadline - time.monotonic()))}
    reachable = port_open(str(host), int(port), **probe)
    check_fetch_control(deadline, cancelled)
    if not reachable:
        return (None, market_used)

    gateway = None
    try:
        gateway = build_futu_gateway(
            host=str(host),
            port=int(port),
            is_option_chain_cache_enabled=False,
        )
        trading_date_text = _trading_date(market_used).isoformat()
        rows = gateway.get_trading_days(
            market=market_used,
            start=trading_date_text,
            end=trading_date_text,
            **({"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}),
        )
        check_fetch_control(deadline, cancelled)
        return _is_trading_day_rows(
            rows,
            market=market_used,
            trading_date_text=trading_date_text,
        )
    except Exception:
        check_fetch_control(deadline, cancelled)
        return (None, market_used)
    finally:
        if gateway is not None:
            gateway.close()


def _trading_date(market: str) -> date:
    mkt = str(market or "").upper().strip()
    if mkt == "US":
        return datetime.now(ZoneInfo("America/New_York")).date()
    if mkt == "HK":
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    if mkt == "CN":
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return datetime.now(ZoneInfo("UTC")).date()


def _is_trading_day_rows(
    data: Any,
    *,
    market: str,
    trading_date_text: str,
) -> tuple[bool, str]:
    market_used = str(market or "").upper().strip()
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
