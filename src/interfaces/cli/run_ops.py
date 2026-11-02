from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_response
from domain.domain.daily_decision_brief import effective_daily_brief_actionability
from src.application.daily_decision_brief_renderer import render_query_brief
from src.application.daily_decision_brief_repository import read_run_daily_decision_brief
from src.application.multi_account_tick import run_tick
from src.application.multi_account_tick import TickExecutionResult, execute_tick
from src.application.tick_cron import run_tick_cron
from src.application.service_failure_alert import alert_failed_service, check_trade_intake_heartbeat


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def add_run_commands(subparsers: Any) -> None:
    run = subparsers.add_parser("run", help="run long-lived workflows")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    tick = run_sub.add_parser("tick", help="multi-account scan orchestration; direct/manual runs do not auto-send ordinary Tick notifications")
    tick.add_argument("--config", required=True)
    tick.add_argument("--accounts", nargs="+", default=None)
    tick.add_argument("--symbols", nargs="+", default=None)
    tick.add_argument("--default-account", default=None)
    tick.add_argument("--market-config", default="auto", choices=["auto", "hk", "us", "all"])
    tick.add_argument("--no-send", action="store_true")
    tick.add_argument(
        "--experience",
        action="store_true",
        help="run a non-executable simulated-account experience scan; requires --no-send",
    )
    tick.add_argument("--smoke", action="store_true")
    tick.add_argument("--force", action="store_true", help="force scanning outside normal run points; does not auto-send ordinary Tick notifications")
    tick.add_argument("--debug", action="store_true")
    tick.add_argument("--opend-phone-verify-continue", action="store_true")
    tick.add_argument("--allow-stale-config", action="store_true")
    tick_cron = run_sub.add_parser("tick-cron", help="cron-safe tick wrapper with lock, timeout, and trigger diagnostics")
    tick_cron.add_argument("--market", required=True, choices=("us", "hk"))
    tick_cron.add_argument("--accounts", nargs="+", default=None)
    tick_cron.add_argument("--symbols", nargs="+", default=None)
    tick_cron.add_argument("--timeout", dest="timeout_seconds", type=int, default=600)
    tick_cron.add_argument("--config", default=None)
    tick_cron.add_argument("--lock-path", default=None)
    tick_cron.add_argument("--trigger-job-id", default=None)
    tick_cron.add_argument("--trigger-job-name", default=None)
    tick_cron.add_argument("--trigger-schedule", default=None)
    tick_cron.add_argument("--dry-run-command", action="store_true")
    tick_cron.add_argument("--no-send", action="store_true")
    tick_cron.add_argument("--force", action="store_true")
    tick_cron.add_argument("--debug", action="store_true")
    tick_cron.add_argument("--allow-stale-config", action="store_true")
    failure_alert = run_sub.add_parser("service-failure-alert", help="report a failed managed service")
    failure_alert.add_argument("--unit", required=True)
    failure_alert.add_argument("--market", required=True, choices=("us", "hk"))
    failure_alert.add_argument("--config", required=True)
    failure_alert.add_argument("--runtime-root", required=True)
    heartbeat_check = run_sub.add_parser("trade-intake-heartbeat-check", help="alert on a stale trade intake heartbeat")
    heartbeat_check.add_argument("--unit", required=True)
    heartbeat_check.add_argument("--market", required=True, choices=("us", "hk"))
    heartbeat_check.add_argument("--config", required=True)
    heartbeat_check.add_argument("--runtime-root", required=True)
    trade_intake = run_sub.add_parser("trade-intake", help="run OpenD trade intake listener")
    trade_intake.add_argument("action", nargs="?", default="listen", choices=["listen", "attribution-migrate"])
    trade_intake.add_argument("--effective-from-ms", type=int)
    trade_intake.add_argument("--actor")
    trade_intake.add_argument("--manifest")
    trade_intake.add_argument("--backup-path")
    trade_intake.add_argument("--writers-stopped", action="store_true")
    trade_intake.add_argument("--config", required=True)
    trade_intake.add_argument("--data-config", default=None)
    trade_intake.add_argument("--runtime-root", default=None)
    trade_intake.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    trade_intake.add_argument("--confirm", action="store_true")
    trade_intake.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    trade_intake.add_argument("--state-path", default=None)
    trade_intake.add_argument("--audit-path", default=None)
    trade_intake.add_argument("--status-path", default=None)
    trade_intake.add_argument("--host", default=None)
    trade_intake.add_argument("--port", type=int, default=None)
    trade_intake.add_argument("--once", action="store_true")
    trade_intake.add_argument("--deal-json", default=None)
    trade_intake.add_argument("--execution-file", default=None)
    trade_intake.add_argument("--inbox-id", default=None)
    trade_intake.add_argument("--recover-skipped", action="store_true")
    trade_intake.add_argument("--expected-recovery-hash", default=None)
    trade_intake.add_argument("--retry-failed", action="store_true")
    trade_intake.add_argument("--reconcile-state", action="store_true")
    trade_intake.add_argument("--compensate-receipts", action="store_true")
    trade_intake.add_argument(
        "--compensation-reason",
        default="legacy_false_outbox_marker",
    )
    trade_intake.add_argument("--expected-payload-hash", default=None)
    trade_intake.add_argument("--account", default=None)
    trade_intake.add_argument("--deal-id", action="append", default=None)
    trade_intake.add_argument("--apply", action="store_true")
    trade_intake.add_argument("--dry-run", action="store_true")


def _tick_argv(args: argparse.Namespace) -> list[str]:
    tick_argv: list[str] = ["--config", str(args.config)]
    if args.accounts:
        tick_argv.extend(["--accounts", *[str(x) for x in args.accounts]])
    if args.symbols:
        tick_argv.extend(["--symbols", ",".join(str(x) for x in args.symbols)])
    if args.default_account:
        tick_argv.extend(["--default-account", str(args.default_account)])
    if args.market_config:
        tick_argv.extend(["--market-config", str(args.market_config)])
    if args.no_send:
        tick_argv.append("--no-send")
    if args.experience:
        tick_argv.append("--experience")
    if args.smoke:
        tick_argv.append("--smoke")
    if args.force:
        tick_argv.append("--force")
    if args.debug:
        tick_argv.append("--debug")
    if args.opend_phone_verify_continue:
        tick_argv.append("--opend-phone-verify-continue")
    if args.allow_stale_config:
        tick_argv.append("--allow-stale-config")
    return tick_argv


def _trade_intake_argv(args: argparse.Namespace) -> list[str]:
    intake_argv: list[str] = ["--config", str(args.config)]
    if getattr(args, "action", "listen") != "listen":
        intake_argv.append(args.action)
    for name in ("effective_from_ms", "actor", "manifest", "backup_path"):
        value = getattr(args, name, None)
        if value is not None:
            intake_argv.extend(["--" + name.replace("_", "-"), str(value)])
    if getattr(args, "writers_stopped", False):
        intake_argv.append("--writers-stopped")
    if args.data_config:
        intake_argv.extend(["--data-config", str(args.data_config)])
    if args.runtime_root:
        intake_argv.extend(["--runtime-root", str(args.runtime_root)])
    if args.mode:
        intake_argv.extend(["--mode", str(args.mode)])
    if args.confirm:
        intake_argv.append("--confirm")
    if args.yes:
        intake_argv.append("--yes")
    if args.state_path:
        intake_argv.extend(["--state-path", str(args.state_path)])
    if args.audit_path:
        intake_argv.extend(["--audit-path", str(args.audit_path)])
    if args.status_path:
        intake_argv.extend(["--status-path", str(args.status_path)])
    if args.host:
        intake_argv.extend(["--host", str(args.host)])
    if args.port:
        intake_argv.extend(["--port", str(args.port)])
    if args.once:
        intake_argv.append("--once")
    if args.deal_json:
        intake_argv.extend(["--deal-json", str(args.deal_json)])
    if args.execution_file:
        intake_argv.extend(["--execution-file", str(args.execution_file)])
    if args.inbox_id:
        intake_argv.extend(["--inbox-id", str(args.inbox_id)])
    if args.recover_skipped:
        intake_argv.append("--recover-skipped")
    if args.expected_recovery_hash:
        intake_argv.extend(["--expected-recovery-hash", str(args.expected_recovery_hash)])
    if args.retry_failed:
        intake_argv.append("--retry-failed")
    if args.reconcile_state:
        intake_argv.append("--reconcile-state")
    if args.compensate_receipts:
        intake_argv.append("--compensate-receipts")
        intake_argv.extend(
            ["--compensation-reason", str(args.compensation_reason)]
        )
    if args.account:
        intake_argv.extend(["--account", str(args.account)])
    for deal_id in args.deal_id or []:
        intake_argv.extend(["--deal-id", str(deal_id)])
    if args.expected_payload_hash:
        intake_argv.extend(
            ["--expected-payload-hash", str(args.expected_payload_hash)]
        )
    if args.apply:
        intake_argv.append("--apply")
    if args.dry_run:
        intake_argv.append("--dry-run")
    return intake_argv


def handle_run_command(
    args: argparse.Namespace,
    *,
    run_tick_fn: Callable[[list[str]], int] = run_tick,
    run_tick_cron_fn: Callable[..., int | dict[str, Any]] = run_tick_cron,
    run_trade_intake_fn: Callable[[list[str]], int] | None = None,
) -> int:
    if args.run_command == "tick":
        if run_tick_fn is not run_tick:
            return int(run_tick_fn(_tick_argv(args)))
        result = execute_tick(_tick_argv(args))
        display_rc = _display_tick_result(result)
        return result.rc if args.smoke else display_rc

    if args.run_command == "tick-cron":
        out = run_tick_cron_fn(
            market=args.market,
            accounts=args.accounts,
            symbols=args.symbols,
            timeout_seconds=args.timeout_seconds,
            config_path=args.config,
            lock_path=args.lock_path,
            trigger_job_id=args.trigger_job_id,
            trigger_job_name=args.trigger_job_name,
            trigger_schedule=args.trigger_schedule,
            dry_run_command=bool(args.dry_run_command),
            no_send=bool(args.no_send),
            force=bool(args.force),
            debug=bool(args.debug),
            allow_stale_config=bool(args.allow_stale_config),
        )
        if isinstance(out, dict):
            return _print(build_response(tool_name="run.tick-cron", ok=True, data=out))
        return int(out)

    if args.run_command == "trade-intake":
        if run_trade_intake_fn is None:
            from src.application.trades.process_supervisor import run_trade_intake_process as run_trade_intake_fn

        return int(run_trade_intake_fn(_trade_intake_argv(args)))

    if args.run_command == "service-failure-alert":
        return alert_failed_service(
            unit=args.unit, market=args.market, config_path=args.config,
            runtime_root=args.runtime_root,
        )

    if args.run_command == "trade-intake-heartbeat-check":
        return check_trade_intake_heartbeat(
            unit=args.unit, market=args.market, config_path=args.config,
            runtime_root=args.runtime_root,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported run command: {args.run_command}")


def _display_tick_result(result: TickExecutionResult) -> int:
    """Keep execution failures; unreadable/unfinished human results also fail."""
    if not result.run_created:
        sys.stdout.write(f"本次未创建运行（退出码 {result.rc}）。\n")
        if result.error:
            sys.stdout.write(result.error + "\n")
        return result.rc or 2
    state = "执行结束" if result.rc == 0 else "执行失败／未完成"
    sys.stdout.write(f"本次{state}｜运行 {result.run_id}｜退出码 {result.rc}\n")
    if result.error:
        sys.stdout.write(result.error + "\n")
    if result.experience:
        state = "完成" if result.rc == 0 and result.experience_completed else "失败／未完成"
        sys.stdout.write(
            f"模拟体验{state}｜账户 {', '.join(result.accounts)}｜市场 {result.market or '未知'}\n"
            "体验结果使用模拟假设，不代表真实账户容量；本次不生成普通决策简报。\n"
        )
        return result.rc or (0 if result.experience_completed else 2)
    if result.runtime_root is None or result.run_id is None or result.market is None:
        sys.stdout.write("本次结果来源不可用：运行范围未完整绑定。\n")
        return result.rc or 2
    now = datetime.now(timezone.utc)
    complete = bool(result.accounts)
    for account in result.accounts:
        data = read_run_daily_decision_brief(
            base=result.runtime_root, account=account, market=result.market, run_id=result.run_id,
        )
        if not data["available"]:
            if (result.rc == 0 and data["reason"] == "not_found"
                    and account in result.scheduler_skipped_accounts):
                sys.stdout.write(f"账户 {account}｜{result.market}｜本次调度跳过，无需生成新扫描结果。\n")
                continue
            complete = False
            sys.stdout.write(f"账户 {account}｜{result.market}｜本次结果来源不可用（{data['reason']}）。\n")
            continue
        brief = dict(data["brief"])
        brief["actionability"] = effective_daily_brief_actionability(brief, now_utc=now)
        if data["reason"] == "scan_failed":
            complete = False
            brief["actionability"] = "blocked"
            sys.stdout.write(f"账户 {account}｜本次扫描失败。\n")
        elif brief.get("status") == "degraded":
            sys.stdout.write(f"账户 {account}｜本次部分数据不可用，候选结果不完整；请查看简报缺口。\n")
        sys.stdout.write(render_query_brief(brief, context={"query_time_utc": now.isoformat()}).rstrip() + "\n")
    sys.stdout.write("阅读提示｜多个 CSP 候选共享现金，最多手数不能相加；年化非收益承诺，估算非成交，请自行复核。\n")
    return result.rc or (0 if complete else 2)
