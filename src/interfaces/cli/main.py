from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.config_validator import validate_config
from src.application.account_management import add_account, edit_account, remove_account
from src.application.close_advice_pipeline import run_close_advice
from src.application.config_edit import get_runtime_config_value, set_runtime_config_value
from src.application.healthcheck import run_healthcheck
from src.application.inbound import (
    InboundRequest,
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_payload,
    handle_inbound_request,
    serve_feishu_ws,
)
from src.application.layered_config import build_layered_runtime_config_file, explain_layered_runtime_config_key
from src.application.multi_account_tick import run_tick
from src.application.notification_pipeline import preview_notification
from src.application.pipeline_runtime import main as run_scan_pipeline
from src.application.runtime_paths import resolve_runtime_root
from src.application.runtime_setup import init_runtime
from src.application.scan_pipeline import run_scan
from src.application.scan_scheduler import run_scheduler
from src.application.service_deploy import (
    load_service_profile,
    repair_output_symlink,
    render_service_bundle,
    service_preflight,
    service_status_from_profile,
    write_service_bundle,
)
from src.application.service_upgrade import service_rollback, service_upgrade, service_upgrade_check
from src.application.strategy_replay import analyze_strategy_replay, read_strategy_replay_file
from src.application.tick_cron import run_tick_cron
from src.application.tool_execution import execute_tool
from src.application.runtime_config_freshness import RuntimeConfigFreshnessError, ensure_runtime_config_freshness
from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs
from src.application.runtime_runs_cli import collect_runtime_runs, format_runtime_runs
from src.application.runtime_status_cli import format_runtime_status_summary, runtime_status_payload_from_args
from domain.domain.config_contract import ensure_runtime_schedule_matches_market
from src.application.version_check import check_version_update
from src.application.cash_headroom_query import query_sell_put_cash


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor unified CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("healthcheck", help="run readiness checks")
    health.add_argument("--config-key", default=None, choices=("us", "hk"))
    health.add_argument("--config-path", default=None)
    health.add_argument("--accounts", nargs="*", default=None)
    health.add_argument("--opend-telnet-host", default=None)
    health.add_argument("--opend-telnet-port", type=int, default=None)

    doctor = sub.add_parser("doctor", help="diagnose runtime readiness and common operator issues")
    doctor.add_argument("--config-key", default=None, choices=("us", "hk"))
    doctor.add_argument("--config-path", default=None)
    doctor.add_argument("--accounts", nargs="*", default=None)
    doctor.add_argument("--opend-telnet-host", default=None)
    doctor.add_argument("--opend-telnet-port", type=int, default=None)

    inbound = sub.add_parser("inbound", help="handle controlled inbound remote commands")
    inbound_sub = inbound.add_subparsers(dest="inbound_command", required=True)
    inbound_handle = inbound_sub.add_parser("handle", help="parse, authorize, audit, and execute one inbound command")
    inbound_handle.add_argument("--text", required=True)
    inbound_handle.add_argument("--sender", dest="sender_id", default="local")
    inbound_handle.add_argument("--channel", default="local")
    inbound_handle.add_argument("--message-id", default=None)
    inbound_handle.add_argument("--config-key", default="us", choices=("us", "hk"))
    inbound_handle.add_argument("--config-path", default=None)
    inbound_handle.add_argument("--audit-db", default=None)
    inbound_handle.add_argument("--format", choices=("json", "text"), default="json")
    inbound_feishu = inbound_sub.add_parser("feishu", help="handle one Feishu event payload through inbound control")
    feishu_input = inbound_feishu.add_mutually_exclusive_group(required=True)
    feishu_input.add_argument("--input-json", default=None)
    feishu_input.add_argument("--input-file", default=None)
    feishu_input.add_argument("--stdin", action="store_true")
    inbound_feishu.add_argument("--config-key", default="us", choices=("us", "hk"))
    inbound_feishu.add_argument("--config-path", default=None)
    inbound_feishu.add_argument("--audit-db", default=None)
    inbound_feishu.add_argument("--format", choices=("json", "text"), default="json")
    inbound_ws = inbound_sub.add_parser("feishu-ws", help="serve the Feishu App long-connection inbound client")
    inbound_ws.add_argument("--config-key", default="us", choices=("us", "hk"))
    inbound_ws.add_argument("--config-path", default=None)
    inbound_ws.add_argument("--audit-db", default=None)
    inbound_ws.add_argument("--no-reply", action="store_true")
    inbound_ws.add_argument("--reply-in-thread", action="store_true", default=None)
    inbound_ws.add_argument("--max-reply-chars", type=int, default=None)
    inbound_ws.add_argument("--queue-size", type=int, default=None)
    inbound_ws.add_argument("--lock-path", default=None)
    inbound_ws.add_argument("--check", action="store_true", help="validate and print redacted long-connection configuration without starting the client")

    status = sub.add_parser("status", help="summarize runtime status")
    status.add_argument("--config-key", default=None, choices=("us", "hk"))
    status.add_argument("--config-path", default=None)
    status.add_argument("--accounts", nargs="*", default=None)
    status.add_argument("--profile-path", default=None)
    status.add_argument("--run-id", default=None)
    status.add_argument("--run-dir", default=None)
    status.add_argument("--report-dir", default=None)
    status.add_argument("--state-dir", default=None)
    status.add_argument("--shared-state-dir", default=None)
    status.add_argument("--accounts-root", default=None)
    status.add_argument("--runs-root", default=None)
    status.add_argument("--max-run-age-minutes", type=int, default=None)
    status.add_argument("--max-notification-chars", type=int, default=None)
    status.add_argument("--json", action="store_true", help="print raw runtime_status JSON envelope")

    runs = sub.add_parser("runs", help="list runtime run snapshots")
    runs.add_argument("--runs-root", default=None)
    runs.add_argument("--profile-path", default=None)
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--run-id", default=None)
    runs.add_argument("--run-dir", default=None)
    runs.add_argument("--scanned-only", action="store_true")
    runs.add_argument("--json", action="store_true", help="print JSON envelope")

    logs = sub.add_parser("logs", help="tail runtime logs and run audit files")
    logs.add_argument("--runs-root", default=None)
    logs.add_argument("--logs-root", default=None)
    logs.add_argument("--profile-path", default=None)
    logs.add_argument("--run-id", default=None)
    logs.add_argument("--run-dir", default=None)
    logs.add_argument("--kind", default="all", choices=("all", "audit", "tool", "tick", "service"))
    logs.add_argument("--lines", type=int, default=50)
    logs.add_argument("--file", dest="log_file", default=None)
    logs.add_argument("--json", action="store_true", help="print JSON envelope")

    ai_cofunder = sub.add_parser("ai-cofunder", help="collect AI Cofunder evidence for MacBook Codex")
    ai_cofunder_sub = ai_cofunder.add_subparsers(dest="ai_cofunder_command", required=True)
    ai_collect = ai_cofunder_sub.add_parser("collect", help="collect redacted evidence bundle")
    ai_collect.add_argument("--scope", default="full", choices=("ledger", "account-strategy", "quality", "strategy", "full"))
    ai_collect.add_argument("--config-key", default=None, choices=("us", "hk"))
    ai_collect.add_argument("--config-path", default=None)
    ai_collect.add_argument("--accounts", nargs="*", default=None)
    ai_collect.add_argument("--profile-path", default=None)
    ai_collect.add_argument("--output", default="handoff", choices=("handoff", "json", "both", "markdown", "md"))
    ai_collect.add_argument("--scheduler-evidence-json", default=None)
    ai_collect.add_argument("--scheduler-evidence-file", default=None)
    ai_collect.add_argument("--candidate-path", action="append", dest="candidate_paths", default=None)
    ai_collect.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
    ai_collect.add_argument("--strategy-replay-path", action="append", dest="strategy_replay_paths", default=None)
    ai_collect.add_argument("--strategy-report-dir", default=None)
    ai_collect.add_argument("--ranking-limit", type=int, default=None, help="top candidate rows per report included in ranking evidence")
    ai_collect.add_argument("--include-healthcheck", action="store_true")
    ai_collect.add_argument("--data-config", default=None)
    ai_collect.add_argument("--timeout-sec", type=int, default=None)
    ai_collect.add_argument("--output-dir", default=None)
    ai_collect.add_argument("--current-dir", default=None)
    ai_collect.add_argument("--write-outputs", action="store_true")
    ai_collect.add_argument("--no-write-outputs", action="store_true")
    ai_collect.add_argument("--confirm", action="store_true")
    ai_handoff = ai_cofunder_sub.add_parser("handoff", help="render handoff from a collected bundle")
    ai_handoff.add_argument("--bundle", required=True)

    scan = sub.add_parser("scan", help="run opportunity scan")
    scan.add_argument("--config-key", default=None, choices=("us", "hk"))
    scan.add_argument("--config-path", default=None)
    scan.add_argument("--symbols", default=None, help="comma-separated symbols")
    scan.add_argument("--top-n", type=int, default=None)
    scan.add_argument("--no-context", action="store_true")

    close_advice = sub.add_parser("close-advice", help="run close advice flow")
    close_advice.add_argument("--config-key", default=None, choices=("us", "hk"))
    close_advice.add_argument("--config-path", default=None)
    close_advice.add_argument("--account", default=None)
    close_advice.add_argument("--output-dir", default=None)

    notify = sub.add_parser("notify", help="notification helpers")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    preview = notify_sub.add_parser("preview", help="preview notification content")
    preview.add_argument("--alerts-path", default=None)
    preview.add_argument("--changes-path", default=None)
    preview.add_argument("--alerts-text", default=None)
    preview.add_argument("--changes-text", default=None)
    preview.add_argument("--account-label", default=None)

    accounts = sub.add_parser("accounts", help="manage runtime accounts")
    account_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    add = account_sub.add_parser("add", help="add account")
    add.add_argument("--market", required=True, choices=("us", "hk"))
    add.add_argument("--account-label", required=True)
    add.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    add.add_argument("--config-path", default=None)
    add.add_argument("--futu-acc-id", default=None)
    add.add_argument("--holdings-account", default=None)
    edit = account_sub.add_parser("edit", help="edit account")
    edit.add_argument("--market", required=True, choices=("us", "hk"))
    edit.add_argument("--account-label", required=True)
    edit.add_argument("--config-path", default=None)
    edit.add_argument("--account-type", default=None, choices=("futu", "external_holdings"))
    edit.add_argument("--futu-acc-id", default=None)
    edit.add_argument("--holdings-account", default=None)
    edit.add_argument("--clear-holdings-account", action="store_true")
    remove = account_sub.add_parser("remove", help="remove account")
    remove.add_argument("--market", required=True, choices=("us", "hk"))
    remove.add_argument("--account-label", required=True)
    remove.add_argument("--config-path", default=None)

    config = sub.add_parser("config", help="config operations")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", help="validate runtime config")
    validate.add_argument("--config-key", default=None, choices=("us", "hk"))
    validate.add_argument("--config-path", default=None)
    validate.add_argument("--market", default=None, choices=("us", "hk"))
    build = config_sub.add_parser("build", help="build canonical runtime config from system/user config")
    build.add_argument("--market", required=True, choices=("us", "hk"))
    build.add_argument("--system-config", default=None)
    build.add_argument("--common-user-config", default=None)
    build.add_argument("--no-common-user-config", action="store_true")
    build.add_argument("--user-config", default=None)
    build.add_argument("--output", default=None)
    build.add_argument("--dry-run", action="store_true")
    explain = config_sub.add_parser("explain", help="explain a layered config key")
    explain.add_argument("--market", required=True, choices=("us", "hk"))
    explain.add_argument("--key", required=True)
    explain.add_argument("--system-config", default=None)
    explain.add_argument("--common-user-config", default=None)
    explain.add_argument("--no-common-user-config", action="store_true")
    explain.add_argument("--user-config", default=None)
    get_config = config_sub.add_parser("get", help="read a runtime config value by dot path")
    get_config.add_argument("--config-key", default=None, choices=("us", "hk"))
    get_config.add_argument("--config-path", default=None)
    get_config.add_argument("--key", required=True)
    set_config = config_sub.add_parser("set", help="preview or write a runtime config value by dot path")
    set_config.add_argument("--config-key", default=None, choices=("us", "hk"))
    set_config.add_argument("--config-path", default=None)
    set_config.add_argument("--key", required=True)
    set_value = set_config.add_mutually_exclusive_group(required=True)
    set_value.add_argument("--value", default=None, help="string value to write")
    set_value.add_argument("--json-value", default=None, help="JSON value to write, for numbers, booleans, arrays, or objects")
    set_config.add_argument("--apply", action="store_true", help="write the change; omitted means dry-run preview")
    set_config.add_argument("--confirm", action="store_true", help="required together with --apply")
    set_config.add_argument("--no-backup", action="store_true", help="do not write a .bak timestamp copy before applying")

    sub.add_parser("version", help="check latest released version from git tags")

    scheduler = sub.add_parser("scheduler", help="scan scheduler / frequency controller")
    scheduler.add_argument("--config", required=True)
    scheduler.add_argument("--state-dir", default=None)
    scheduler.add_argument("--state", default=None)
    scheduler.add_argument("--schedule-key", default="schedule")
    scheduler.add_argument("--account", default=None)
    scheduler.add_argument("--run-if-due", action="store_true")
    scheduler.add_argument("--mark-notified", action="store_true")
    scheduler.add_argument("--mark-scanned", action="store_true")
    scheduler.add_argument("--jsonl", action="store_true")
    scheduler.add_argument("--force", action="store_true")

    sell_put_cash = sub.add_parser("sell-put-cash", help="query cash headroom for sell-put")
    sell_put_cash.add_argument("--config", default=None)
    sell_put_cash.add_argument("--data-config", default=None)
    sell_put_cash.add_argument("--market", default="富途")
    sell_put_cash.add_argument("--account", default=None)
    sell_put_cash.add_argument("--format", choices=("text", "json"), default="text")
    sell_put_cash.add_argument("--top", type=int, default=10)
    sell_put_cash.add_argument("--no-exchange-rates", action="store_true")
    sell_put_cash.add_argument("--out-dir", default=None)

    strategy_replay = sub.add_parser("strategy-replay", help="offline strategy replay analysis")
    strategy_replay_sub = strategy_replay.add_subparsers(dest="strategy_replay_command", required=True)
    strategy_replay_analyze = strategy_replay_sub.add_parser("analyze", help="analyze replay rows for parameter learning")
    strategy_replay_analyze.add_argument("--replay-path", action="append", required=True, help="CSV/JSON/JSONL replay file; can be repeated")
    strategy_replay_analyze.add_argument("--min-sample", type=int, default=5)
    strategy_replay_analyze.add_argument("--win-return-threshold", type=float, default=0.0)
    strategy_replay_analyze.add_argument("--bad-drawdown-threshold", type=float, default=-0.15)

    service = sub.add_parser("service", help="render and inspect platform service definitions")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_render = service_sub.add_parser("render", help="render systemd or launchd service files")
    service_render.add_argument("--target", required=True, choices=("systemd", "launchd"))
    service_render.add_argument("--repo-root", default=None)
    service_render.add_argument("--runtime-root", default=None)
    service_render.add_argument("--accounts", nargs="+", default=None)
    service_render.add_argument("--markets", nargs="+", choices=("us", "hk"), default=None)
    service_render.add_argument("--config-us", default=None)
    service_render.add_argument("--config-hk", default=None)
    service_render.add_argument("--env-file", default=None, help="systemd EnvironmentFile path for local secrets/env values")
    service_render.add_argument("--deploy-user", default=None, help="systemd User= identity; also accepted from OM_DEPLOY_USER/DEPLOY_USER")
    service_render.add_argument("--deploy-home", default=None, help="systemd HOME environment; defaults to /home/<deploy-user>")
    service_render.add_argument("--timeout", dest="timeout_seconds", type=int, default=600)
    service_render.add_argument("--include-auto-upgrade", action="store_true", help="render an opt-in daily auto-upgrade service/timer")
    service_render.add_argument("--include-feishu-ws", action="store_true", help="render the long-running Feishu long-connection inbound service")
    service_render.add_argument("--feishu-ws-config-key", default="us", choices=("us", "hk"))
    service_render.add_argument("--output-dir", default=None, help="write rendered files under this directory")
    service_render.add_argument("--no-content", action="store_true", help="omit file contents from JSON output")
    service_preflight_cmd = service_sub.add_parser("preflight", help="check Linux runtime root before installing/running services")
    service_preflight_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_preflight_cmd.add_argument("--accounts", nargs="+", default=None)
    service_preflight_cmd.add_argument("--default-account", default=None)
    service_preflight_cmd.add_argument("--config-us", default=None)
    service_preflight_cmd.add_argument("--config-hk", default=None)
    service_preflight_cmd.add_argument("--env-file", default=None)
    service_repair_output = service_sub.add_parser("repair-output", help="migrate a real runtime output directory to output_accounts and create the output symlink")
    service_repair_output.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_repair_output.add_argument("--default-account", required=True)
    service_repair_output.add_argument("--confirm", action="store_true", help="apply the migration; without this the command is a dry run")
    service_status = service_sub.add_parser("status", help="summarize a rendered service profile")
    service_status.add_argument("--profile-path", required=True)
    service_status.add_argument("--include-service-status", action="store_true")
    service_upgrade_check_cmd = service_sub.add_parser("upgrade-check", help="check whether a newer released version is available")
    service_upgrade_check_cmd.add_argument("--repo-root", default=None)
    service_upgrade_check_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_upgrade_check_cmd.add_argument("--remote-name", default="origin")
    service_upgrade_cmd = service_sub.add_parser("upgrade", help="upgrade a current symlink to a released version")
    service_upgrade_cmd.add_argument("--repo-root", default=None)
    service_upgrade_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_upgrade_cmd.add_argument("--releases-root", default=None)
    service_upgrade_cmd.add_argument("--target-version", default=None)
    service_upgrade_cmd.add_argument("--remote-name", default="origin")
    service_upgrade_cmd.add_argument("--auto", action="store_true")
    service_upgrade_cmd.add_argument("--allow-major", action="store_true")
    service_upgrade_cmd.add_argument("--confirm", action="store_true", help="apply upgrade; without this the command is a dry run")
    service_upgrade_cmd.add_argument("--no-restart-services", action="store_true")
    service_rollback_cmd = service_sub.add_parser("rollback", help="switch current symlink back to a prior released version")
    service_rollback_cmd.add_argument("--repo-root", default=None)
    service_rollback_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_rollback_cmd.add_argument("--releases-root", default=None)
    service_rollback_cmd.add_argument("--to-version", default=None)
    service_rollback_cmd.add_argument("--confirm", action="store_true", help="apply rollback; without this the command is a dry run")
    service_rollback_cmd.add_argument("--no-restart-services", action="store_true")

    update = sub.add_parser("update", help="check, apply, or roll back released versions")
    update_sub = update.add_subparsers(dest="update_command", required=True)
    update_check = update_sub.add_parser("check", help="check whether a newer released version is available")
    update_check.add_argument("--repo-root", default=None)
    update_check.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_check.add_argument("--remote-name", default="origin")
    update_apply = update_sub.add_parser("apply", help="upgrade a current symlink to a released version")
    update_apply.add_argument("--repo-root", default=None)
    update_apply.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_apply.add_argument("--releases-root", default=None)
    update_apply.add_argument("--target-version", default=None)
    update_apply.add_argument("--remote-name", default="origin")
    update_apply.add_argument("--auto", action="store_true")
    update_apply.add_argument("--allow-major", action="store_true")
    update_apply.add_argument("--confirm", action="store_true", help="apply upgrade; without this the command is a dry run")
    update_apply.add_argument("--no-restart-services", action="store_true")
    update_rollback = update_sub.add_parser("rollback", help="switch current symlink back to a prior released version")
    update_rollback.add_argument("--repo-root", default=None)
    update_rollback.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_rollback.add_argument("--releases-root", default=None)
    update_rollback.add_argument("--to-version", default=None)
    update_rollback.add_argument("--confirm", action="store_true", help="apply rollback; without this the command is a dry run")
    update_rollback.add_argument("--no-restart-services", action="store_true")

    sub.add_parser("symbols", help="manage monitored symbols")
    sub.add_parser("option-positions", help="option position operations")
    sub.add_parser("trade-events", help="review, repair, replay, and void trade events")

    init_cmd = sub.add_parser("init", help="initialize runtime config")
    init_sub = init_cmd.add_subparsers(dest="init_command", required=True)
    runtime = init_sub.add_parser("runtime", help="generate runtime config")
    runtime.add_argument("--market", required=True, choices=("us", "hk"))
    runtime.add_argument("--futu-acc-id", required=True)
    runtime.add_argument("--account-label", default="user1")
    runtime.add_argument("--config-path", default=None)
    runtime.add_argument("--data-config-path", default=None)
    runtime.add_argument("--symbol", action="append", dest="symbols", default=None)
    runtime.add_argument("--holdings-account", default=None)
    runtime.add_argument("--opend-host", default="127.0.0.1")
    runtime.add_argument("--opend-port", type=int, default=11111)
    runtime.add_argument("--force", action="store_true")

    setup = sub.add_parser("setup", help="set up a starter runtime config")
    setup.add_argument("--market", required=True, choices=("us", "hk"))
    setup.add_argument("--futu-acc-id", required=True)
    setup.add_argument("--account-label", default="user1")
    setup.add_argument("--config-path", default=None)
    setup.add_argument("--data-config-path", default=None)
    setup.add_argument("--symbol", action="append", dest="symbols", default=None)
    setup.add_argument("--holdings-account", default=None)
    setup.add_argument("--opend-host", default="127.0.0.1")
    setup.add_argument("--opend-port", type=int, default=11111)
    setup.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="run long-lived workflows")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    tick = run_sub.add_parser("tick", help="multi-account tick orchestration")
    tick.add_argument("--config", default="config.us.json")
    tick.add_argument("--accounts", nargs="+", default=None)
    tick.add_argument("--default-account", default=None)
    tick.add_argument("--market-config", default="auto", choices=["auto", "hk", "us", "all"])
    tick.add_argument("--no-send", action="store_true")
    tick.add_argument("--smoke", action="store_true")
    tick.add_argument("--force", action="store_true")
    tick.add_argument("--debug", action="store_true")
    tick.add_argument("--opend-phone-verify-continue", action="store_true")
    tick.add_argument("--allow-stale-config", action="store_true")
    tick_cron = run_sub.add_parser("tick-cron", help="cron-safe tick wrapper with lock, timeout, and trigger diagnostics")
    tick_cron.add_argument("--market", required=True, choices=("us", "hk"))
    tick_cron.add_argument("--accounts", nargs="+", default=None)
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
    trade_intake = run_sub.add_parser("trade-intake", help="run OpenD trade intake listener")
    trade_intake.add_argument("--config", default="config.us.json")
    trade_intake.add_argument("--data-config", default=None)
    trade_intake.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    trade_intake.add_argument("--state-path", default=None)
    trade_intake.add_argument("--audit-path", default=None)
    trade_intake.add_argument("--status-path", default=None)
    trade_intake.add_argument("--host", default="127.0.0.1")
    trade_intake.add_argument("--port", type=int, default=11111)
    trade_intake.add_argument("--once", action="store_true")
    trade_intake.add_argument("--deal-json", default=None)

    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _load_scheduler_evidence(*, json_text: str | None, file_path: str | None) -> dict[str, Any] | None:
    if file_path:
        payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise AgentToolError(code="INPUT_ERROR", message="scheduler evidence file must contain a JSON object")
        return payload
    if json_text:
        payload = json.loads(json_text)
        if not isinstance(payload, dict):
            raise AgentToolError(code="INPUT_ERROR", message="scheduler evidence JSON must be an object")
        return payload
    return None


def _load_json_payload(*, json_text: str | None, file_path: str | None, stdin_enabled: bool = False) -> dict[str, Any]:
    try:
        if file_path:
            payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        elif stdin_enabled:
            payload = json.loads(sys.stdin.read())
        elif json_text:
            payload = json.loads(json_text)
        else:
            raise AgentToolError(code="INPUT_ERROR", message="missing JSON payload")
    except AgentToolError:
        raise
    except Exception as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to parse JSON payload",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message="JSON payload must be an object")
    return payload


def _validate_runtime_config(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    path, cfg = load_runtime_config(config_key=config_key, config_path=config_path)
    validate_config(dict(cfg))
    freshness = None
    schedule_contract = None
    if market:
        try:
            schedule_contract = ensure_runtime_schedule_matches_market(
                cfg,
                config_path=path,
                market_config=market,
            )
        except SystemExit as exc:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=str(exc),
                details={"config_path": str(path), "market": str(market)},
            ) from exc
        try:
            freshness = ensure_runtime_config_freshness(
                cfg,
                repo_root=repo_base(),
                market=market,
                runtime_config_path=path,
            )
        except RuntimeConfigFreshnessError as exc:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=str(exc),
                details=exc.result,
            ) from exc
    return {
        "ok": True,
        "config_path": str(path),
        "config_key": str(config_key or "").strip().lower() or None,
        "market": str(market or "").strip().lower() or None,
        "schedule_contract": schedule_contract,
        "freshness": freshness,
    }


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if actual_argv and actual_argv[0] == "scan-pipeline":
        return int(run_scan_pipeline(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "option-positions":
        from src.interfaces.cli.option_positions import main as run_option_positions_cli

        return int(run_option_positions_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "trade-events":
        from src.interfaces.cli.trade_events import main as run_trade_events_cli

        return int(run_trade_events_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "symbols":
        from src.interfaces.cli.symbols import main as run_symbols_cli

        return int(run_symbols_cli(actual_argv[1:]))

    args = parse_args(actual_argv)
    try:
        if args.command == "healthcheck":
            return _print(
                run_healthcheck(
                    config_key=args.config_key,
                    config_path=args.config_path,
                    accounts=args.accounts,
                    opend_telnet_host=args.opend_telnet_host,
                    opend_telnet_port=args.opend_telnet_port,
                )
            )

        if args.command == "doctor":
            healthcheck = run_healthcheck(
                config_key=args.config_key,
                config_path=args.config_path,
                accounts=args.accounts,
                opend_telnet_host=args.opend_telnet_host,
                opend_telnet_port=args.opend_telnet_port,
            )
            return _print(build_response(
                tool_name="doctor",
                ok=bool(healthcheck.get("ok", True)),
                data={"healthcheck": healthcheck},
            ))

        if args.command == "inbound" and args.inbound_command == "handle":
            out = handle_inbound_request(
                InboundRequest(
                    text=args.text,
                    sender_id=args.sender_id,
                    channel=args.channel,
                    message_id=args.message_id,
                    config_key=args.config_key,
                    config_path=args.config_path,
                    audit_db=args.audit_db,
                )
            )
            if args.format == "text":
                data_raw = out.get("data")
                data: dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
                text = str(data.get("response_text") or "").strip() or _dumps(out)
                sys.stdout.write(text + "\n")
                return 0 if out.get("ok", True) else 2
            return _print(out)

        if args.command == "inbound" and args.inbound_command == "feishu":
            out = handle_feishu_payload(
                _load_json_payload(
                    json_text=args.input_json,
                    file_path=args.input_file,
                    stdin_enabled=bool(args.stdin),
                ),
                config_key=args.config_key,
                config_path=args.config_path,
                audit_db=args.audit_db,
            )
            if args.format == "text":
                data_raw = out.get("data")
                data = data_raw if isinstance(data_raw, dict) else {}
                text = str(data.get("response_text") or data.get("challenge") or "").strip() or _dumps(out)
                sys.stdout.write(text + "\n")
                return 0 if out.get("ok", True) else 2
            return _print(out)

        if args.command == "inbound" and args.inbound_command == "feishu-ws":
            settings = build_feishu_ws_settings(
                config_key=args.config_key,
                config_path=args.config_path,
                audit_db=args.audit_db,
                reply_enabled=not bool(args.no_reply),
                reply_in_thread=args.reply_in_thread,
                max_reply_chars=args.max_reply_chars,
                queue_size=args.queue_size,
            )
            if args.check:
                return _print(check_feishu_ws_settings(settings))
            serve_feishu_ws(settings, lock_path=args.lock_path)
            return 0

        if args.command == "status":
            out = execute_tool("runtime_status", runtime_status_payload_from_args(args))
            if args.json:
                return _print(out)
            sys.stdout.write(format_runtime_status_summary(out))
            return 0 if out.get("ok", True) else 2

        if args.command == "runs":
            data = collect_runtime_runs(
                repo_root=repo_base(),
                runs_root=args.runs_root,
                profile_path=args.profile_path,
                limit=int(args.limit),
                run_id=args.run_id,
                run_dir=args.run_dir,
                scanned_only=bool(args.scanned_only),
            )
            envelope = build_response(
                tool_name="runs",
                ok=bool(data.get("summary", {}).get("ok", True)),
                data=data,
            )
            if args.json:
                return _print(envelope)
            sys.stdout.write(format_runtime_runs(data))
            return 0 if envelope.get("ok", True) else 2

        if args.command == "logs":
            data = collect_runtime_logs(
                repo_root=repo_base(),
                runs_root=args.runs_root,
                logs_root=args.logs_root,
                profile_path=args.profile_path,
                run_id=args.run_id,
                run_dir=args.run_dir,
                kind=args.kind,
                lines=int(args.lines),
                log_file=args.log_file,
            )
            envelope = build_response(
                tool_name="logs",
                ok=bool(data.get("summary", {}).get("ok", True)),
                data=data,
            )
            if args.json:
                return _print(envelope)
            sys.stdout.write(format_runtime_logs(data))
            return 0 if envelope.get("ok", True) else 2

        if args.command == "ai-cofunder" and args.ai_cofunder_command == "collect":
            payload = {
                "scope": args.scope,
                "config_key": args.config_key,
                "config_path": args.config_path,
                "accounts": args.accounts,
                "profile_path": args.profile_path,
                "output": args.output,
                "candidate_paths": args.candidate_paths,
                "trace_paths": args.trace_paths,
                "strategy_replay_paths": args.strategy_replay_paths,
                "strategy_report_dir": args.strategy_report_dir,
                "ranking_limit": args.ranking_limit,
                "include_healthcheck": bool(args.include_healthcheck),
                "data_config": args.data_config,
                "timeout_sec": args.timeout_sec,
                "ai_cofunder_output_dir": args.output_dir,
                "ai_cofunder_current_dir": args.current_dir,
                "write_outputs": bool(args.write_outputs),
                "confirm": bool(args.confirm),
            }
            if args.no_write_outputs:
                payload["write_outputs"] = False
            scheduler_evidence = _load_scheduler_evidence(
                json_text=args.scheduler_evidence_json,
                file_path=args.scheduler_evidence_file,
            )
            if scheduler_evidence is not None:
                payload["scheduler_evidence"] = scheduler_evidence
            payload = {key: value for key, value in payload.items() if value not in (None, [])}
            return _print(execute_tool("ai_cofunder", payload))

        if args.command == "ai-cofunder" and args.ai_cofunder_command == "handoff":
            from src.application.ai_cofunder.service import render_ai_cofunder_handoff

            bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
            if not isinstance(bundle, dict):
                raise AgentToolError(code="INPUT_ERROR", message="ai-cofunder bundle must be a JSON object")
            return _print(build_response(
                tool_name="ai-cofunder.handoff",
                ok=True,
                data={"handoff_markdown": render_ai_cofunder_handoff(bundle)},
            ))

        if args.command == "scan":
            symbols = [s.strip().upper() for s in str(args.symbols or "").split(",") if s.strip()] or None
            return _print(run_scan(config_key=args.config_key, config_path=args.config_path, symbols=symbols, top_n=args.top_n, no_context=bool(args.no_context)))

        if args.command == "close-advice":
            return _print(run_close_advice(config_key=args.config_key, config_path=args.config_path, account=args.account, output_dir=args.output_dir))

        if args.command == "notify" and args.notify_command == "preview":
            return _print(preview_notification(
                alerts_path=args.alerts_path,
                changes_path=args.changes_path,
                alerts_text=args.alerts_text,
                changes_text=args.changes_text,
                account_label=args.account_label,
            ))

        if args.command == "accounts" and args.accounts_command == "add":
            return _print(build_response(tool_name="accounts.add", ok=True, data=add_account(
                market=args.market,
                account_label=args.account_label,
                account_type=args.account_type,
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
            )))

        if args.command == "accounts" and args.accounts_command == "edit":
            return _print(build_response(tool_name="accounts.edit", ok=True, data=edit_account(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
                account_type=args.account_type,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
            )))

        if args.command == "accounts" and args.accounts_command == "remove":
            return _print(build_response(tool_name="accounts.remove", ok=True, data=remove_account(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
            )))

        if args.command == "config" and args.config_command == "validate":
            return _print(_validate_runtime_config(config_key=args.config_key, config_path=args.config_path, market=args.market))

        if args.command == "config" and args.config_command == "build":
            return _print(build_layered_runtime_config_file(
                repo_root=repo_base(),
                market=args.market,
                system_config_path=args.system_config,
                common_user_config_path=args.common_user_config,
                include_common_user_config=not bool(args.no_common_user_config),
                user_config_path=args.user_config,
                output_config_path=args.output,
                dry_run=bool(args.dry_run),
            ))

        if args.command == "config" and args.config_command == "explain":
            return _print(explain_layered_runtime_config_key(
                repo_root=repo_base(),
                market=args.market,
                key=args.key,
                system_config_path=args.system_config,
                common_user_config_path=args.common_user_config,
                include_common_user_config=not bool(args.no_common_user_config),
                user_config_path=args.user_config,
            ))

        if args.command == "config" and args.config_command == "get":
            return _print(build_response(
                tool_name="config.get",
                ok=True,
                data=get_runtime_config_value(
                    config_key=args.config_key,
                    config_path=args.config_path,
                    key=args.key,
                ),
            ))

        if args.command == "config" and args.config_command == "set":
            return _print(build_response(
                tool_name="config.set",
                ok=True,
                data=set_runtime_config_value(
                    config_key=args.config_key,
                    config_path=args.config_path,
                    key=args.key,
                    value=args.value,
                    json_value=args.json_value,
                    apply=bool(args.apply),
                    confirm=bool(args.confirm),
                    backup=not bool(args.no_backup),
                ),
            ))

        if args.command == "version":
            sys.stdout.write(_dumps(check_version_update()))
            return 0

        if args.command == "scheduler":
            runtime_root = resolve_runtime_root(repo_root=repo_base()).runtime_root
            run_scheduler(
                config=args.config,
                state_dir=args.state_dir or str((runtime_root / "output" / "state").resolve()),
                state=args.state,
                schedule_key=args.schedule_key,
                account=args.account,
                run_if_due=bool(args.run_if_due),
                mark_notified=bool(args.mark_notified),
                mark_scanned=bool(args.mark_scanned),
                jsonl=bool(args.jsonl),
                force=bool(args.force),
            )
            return 0

        if args.command == "sell-put-cash":
            runtime_root = resolve_runtime_root(repo_root=repo_base()).runtime_root
            query_sell_put_cash(
                config=args.config,
                data_config=args.data_config,
                market=args.market,
                account=args.account,
                output_format=args.format,
                top=args.top,
                no_exchange_rates=bool(args.no_exchange_rates),
                out_dir=args.out_dir or str((runtime_root / "output" / "state").resolve()),
            )
            return 0

        if args.command == "strategy-replay" and args.strategy_replay_command == "analyze":
            rows: list[dict[str, Any]] = []
            for replay_path in args.replay_path:
                try:
                    rows.extend(read_strategy_replay_file(Path(replay_path)))
                except Exception as exc:
                    raise AgentToolError(
                        code="INPUT_ERROR",
                        message=f"failed to read strategy replay file: {Path(replay_path).name}",
                        details={"error": f"{type(exc).__name__}: {exc}"},
                    ) from exc
            data = analyze_strategy_replay(
                rows,
                min_sample=args.min_sample,
                win_return_threshold=args.win_return_threshold,
                bad_drawdown_threshold=args.bad_drawdown_threshold,
            )
            return _print(build_response(tool_name="strategy-replay.analyze", ok=True, data=data))

        if args.command == "service" and args.service_command == "render":
            config_paths = {
                key: value
                for key, value in {
                    "us": args.config_us,
                    "hk": args.config_hk,
                }.items()
                if value
            }
            bundle = render_service_bundle(
                target=args.target,
                repo_root=args.repo_root,
                runtime_root=args.runtime_root,
                accounts=args.accounts,
                markets=args.markets,
                config_paths=config_paths,
                env_file=args.env_file,
                deploy_user=args.deploy_user,
                deploy_home=args.deploy_home,
                timeout_seconds=args.timeout_seconds,
                include_auto_upgrade=bool(args.include_auto_upgrade),
                include_feishu_ws=bool(args.include_feishu_ws),
                feishu_ws_config_key=args.feishu_ws_config_key,
                include_content=(not bool(args.no_content)) or bool(args.output_dir),
            )
            if args.output_dir:
                bundle["written_files"] = write_service_bundle(bundle, args.output_dir)
                if bool(args.no_content):
                    for item in bundle.get("files", []):
                        if isinstance(item, dict):
                            item.pop("content", None)
            return _print(build_response(tool_name="service.render", ok=True, data=bundle))

        if args.command == "service" and args.service_command == "preflight":
            config_paths = {
                key: value
                for key, value in {
                    "us": args.config_us,
                    "hk": args.config_hk,
                }.items()
                if value
            }
            data = service_preflight(
                runtime_root=args.runtime_root,
                env_file=args.env_file,
                accounts=args.accounts,
                default_account=args.default_account,
                config_paths=config_paths,
            )
            return _print(build_response(tool_name="service.preflight", ok=bool(data["summary"]["ok"]), data=data))

        if args.command == "service" and args.service_command == "repair-output":
            data = repair_output_symlink(
                runtime_root=args.runtime_root,
                default_account=args.default_account,
                confirm=bool(args.confirm),
            )
            return _print(build_response(tool_name="service.repair_output", ok=True, data=data))

        if args.command == "service" and args.service_command == "status":
            profile = load_service_profile(args.profile_path)
            data = service_status_from_profile(profile, include_status=bool(args.include_service_status))
            return _print(build_response(tool_name="service.status", ok=True, data=data))

        if args.command == "service" and args.service_command == "upgrade-check":
            data = service_upgrade_check(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                remote_name=args.remote_name,
            )
            return _print(build_response(tool_name="service.upgrade_check", ok=bool(data.get("ok")), data=data))

        if args.command == "service" and args.service_command == "upgrade":
            data = service_upgrade(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                releases_root=args.releases_root,
                target_version=args.target_version,
                remote_name=args.remote_name,
                confirm=bool(args.confirm),
                auto=bool(args.auto),
                allow_major=bool(args.allow_major),
                restart_services=not bool(args.no_restart_services),
            )
            return _print(build_response(tool_name="service.upgrade", ok=bool(data.get("ok")), data=data))

        if args.command == "service" and args.service_command == "rollback":
            data = service_rollback(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                releases_root=args.releases_root,
                to_version=args.to_version,
                confirm=bool(args.confirm),
                restart_services=not bool(args.no_restart_services),
            )
            return _print(build_response(tool_name="service.rollback", ok=bool(data.get("ok")), data=data))

        if args.command == "update" and args.update_command == "check":
            data = service_upgrade_check(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                remote_name=args.remote_name,
            )
            return _print(build_response(tool_name="update.check", ok=bool(data.get("ok")), data=data))

        if args.command == "update" and args.update_command == "apply":
            data = service_upgrade(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                releases_root=args.releases_root,
                target_version=args.target_version,
                remote_name=args.remote_name,
                confirm=bool(args.confirm),
                auto=bool(args.auto),
                allow_major=bool(args.allow_major),
                restart_services=not bool(args.no_restart_services),
            )
            return _print(build_response(tool_name="update.apply", ok=bool(data.get("ok")), data=data))

        if args.command == "update" and args.update_command == "rollback":
            data = service_rollback(
                repo_root=args.repo_root or repo_base(),
                runtime_root=args.runtime_root,
                releases_root=args.releases_root,
                to_version=args.to_version,
                confirm=bool(args.confirm),
                restart_services=not bool(args.no_restart_services),
            )
            return _print(build_response(tool_name="update.rollback", ok=bool(data.get("ok")), data=data))

        if args.command == "init" and args.init_command == "runtime":
            return _print(build_response(tool_name="init.runtime", ok=True, data=init_runtime(
                market=args.market,
                futu_acc_id=args.futu_acc_id,
                account_label=args.account_label,
                config_path=args.config_path,
                data_config_path=args.data_config_path,
                symbols=args.symbols,
                holdings_account=args.holdings_account,
                opend_host=args.opend_host,
                opend_port=args.opend_port,
                force=bool(args.force),
            )))

        if args.command == "setup":
            return _print(build_response(tool_name="setup", ok=True, data=init_runtime(
                market=args.market,
                futu_acc_id=args.futu_acc_id,
                account_label=args.account_label,
                config_path=args.config_path,
                data_config_path=args.data_config_path,
                symbols=args.symbols,
                holdings_account=args.holdings_account,
                opend_host=args.opend_host,
                opend_port=args.opend_port,
                force=bool(args.force),
            )))

        if args.command == "run" and args.run_command == "tick":
            tick_argv: list[str] = ["--config", str(args.config)]
            if args.accounts:
                tick_argv.extend(["--accounts", *[str(x) for x in args.accounts]])
            if args.default_account:
                tick_argv.extend(["--default-account", str(args.default_account)])
            if args.market_config:
                tick_argv.extend(["--market-config", str(args.market_config)])
            if args.no_send:
                tick_argv.append("--no-send")
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
            return int(run_tick(tick_argv))

        if args.command == "run" and args.run_command == "tick-cron":
            out = run_tick_cron(
                market=args.market,
                accounts=args.accounts,
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

        if args.command == "run" and args.run_command == "trade-intake":
            from src.application.trades.auto_intake import main as run_trade_intake

            intake_argv: list[str] = ["--config", str(args.config)]
            if args.data_config:
                intake_argv.extend(["--data-config", str(args.data_config)])
            if args.mode:
                intake_argv.extend(["--mode", str(args.mode)])
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
            return int(run_trade_intake(intake_argv))
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
