from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.assistant import (
    capability_catalog_payload,
    capability_catalog_text,
    command_catalog_payload,
)
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.config_loader import load_assistant_config
from src.application.assistant.diagnostics import check_llm_translator
from src.application.assistant.llm_model_profiles import (
    add_model_profile_to_config,
    configured_model_profiles_payload,
    current_model_payload,
    model_catalog,
    parse_model_profiles,
    switch_active_model_profile,
    write_model_config_update,
)
from src.application.assistant.runtime import handle_assistant_message
from src.application.config_yaml import (
    default_yaml_config_path,
    load_yaml_config_file,
)
from src.application.healthcheck import run_healthcheck
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.operation_diagnostics import collect_pending_operations, collect_recent_audit
from src.application.inbound import (
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_payload,
    serve_feishu_ws,
)
from src.application.assistant.upgrade_operations import run_confirmed_upgrade_operation
from src.application.multi_account_tick import run_tick
from src.application.pipeline_runtime import main as run_scan_pipeline
from src.application.tick_cron import run_tick_cron
from src.application.tool_execution import execute_tool
from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs
from src.application.runtime_runs_cli import collect_runtime_runs, format_runtime_runs
from src.application.runtime_status_cli import format_runtime_status_summary, runtime_status_payload_from_args
from src.application.settings import bootstrap_process_env
from src.application.support_bundle import support_bundle_response
from src.application.version_check import check_version_update
from src.interfaces.cli.account_ops import (
    add_account,
    add_account_commands,
    edit_account,
    handle_account_command,
    remove_account,
)
from src.interfaces.cli.config_ops import (
    _validate_runtime_config,
    add_config_commands,
    build_yaml_assistant_config_file,
    build_yaml_runtime_config_file,
    explain_yaml_config_key,
    get_runtime_config_value,
    handle_config_command,
    init_yaml_config,
    load_runtime_config,
    preview_config_yaml_migration,
    validate_config,
    validate_yaml_runtime_config,
)
from src.interfaces.cli.operator_ops import (
    add_operator_commands,
    handle_operator_command,
    preview_notification,
    run_close_advice,
    run_scan,
)
from src.interfaces.cli.research import add_research_commands, handle_research_command
from src.interfaces.cli.scheduler_ops import (
    add_scheduler_commands,
    handle_scheduler_command,
    query_sell_put_cash,
    run_scheduler,
)
from src.interfaces.cli.service_ops import (
    add_service_update_commands,
    handle_service_update_command,
    load_service_profile,
    render_service_bundle,
    service_cleanup,
    service_drift,
    service_preflight,
    service_rollback,
    service_status_from_profile,
    service_upgrade,
    service_upgrade_check,
    write_service_bundle,
)
from src.interfaces.cli.settings_ops import (
    add_settings_commands,
    diagnose_effective_settings,
    explain_effective_setting,
    handle_settings_command,
    inspect_effective_settings,
)
from src.interfaces.cli.setup_ops import add_setup_commands, handle_setup_command, run_setup_check


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _assistant_settings_for_cli(
    *,
    config_key: str | None,
    config_path: str | None,
    assistant_config_path: str | None = None,
    force_enabled: bool | None = None,
) -> AssistantSettings:
    del config_key, config_path
    assistant_explicit = bool(assistant_config_path is not None and str(assistant_config_path).strip())
    _assistant_path, assistant_cfg = load_assistant_config(config_path=assistant_config_path, missing_ok=not assistant_explicit)
    if assistant_cfg:
        configured = AssistantSettings.from_runtime_config(assistant_cfg)
        return AssistantSettings(
            mode=configured.mode,
            enabled=configured.enabled if force_enabled is None else bool(force_enabled),
            context_window_messages=configured.context_window_messages,
            default_market_scope=configured.default_market_scope,
            llm=configured.llm,
        )
    return AssistantSettings(enabled=True if force_enabled is None else bool(force_enabled))


def _add_assistant_commands(parser: argparse.ArgumentParser) -> None:
    assistant_sub = parser.add_subparsers(dest="assistant_command", required=True)
    assistant_handle = assistant_sub.add_parser("handle", help="handle one local or remote assistant message")
    assistant_handle.add_argument("--text", required=True)
    assistant_handle.add_argument("--sender", dest="sender_id", default="local")
    assistant_handle.add_argument("--channel", default="local")
    assistant_handle.add_argument("--message-id", default=None)
    assistant_handle.add_argument("--conversation-id", default=None)
    assistant_handle.add_argument("--config-key", default=None, choices=("us", "hk"))
    assistant_handle.add_argument("--config-path", default=None)
    assistant_handle.add_argument("--assistant-config", default=None)
    assistant_handle.add_argument("--audit-db", default=None)
    assistant_handle.add_argument("--env-file", default=None)
    assistant_handle.add_argument("--no-local-env-file", action="store_true")
    assistant_handle.add_argument("--format", choices=("json", "text"), default="json")
    assistant_commands = assistant_sub.add_parser("commands", help="list supported assistant commands and intents")
    assistant_commands.add_argument("--format", choices=("json", "text"), default="json")
    assistant_capabilities = assistant_sub.add_parser("capabilities", help="list supported assistant capabilities and LLM routing surface")
    assistant_capabilities.add_argument("--format", choices=("json", "text"), default="json")
    assistant_llm_check = assistant_sub.add_parser("llm-check", help="check optional LLM intent translator configuration")
    assistant_llm_check.add_argument("--assistant-config", default=None)
    assistant_llm_check.add_argument("--env-file", default=None)
    assistant_llm_check.add_argument("--no-local-env-file", action="store_true")
    assistant_llm_check.add_argument("--live", action="store_true", help="run one read-only provider translation probe")
    assistant_llm_check.add_argument("--text", default=None, help="probe text used with --live")
    assistant_model = assistant_sub.add_parser("model", help="manage optional assistant LLM model profiles")
    assistant_model_sub = assistant_model.add_subparsers(dest="assistant_model_command", required=True)
    assistant_model_catalog = assistant_model_sub.add_parser("catalog", help="list built-in supported LLM providers")
    assistant_model_catalog.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_list = assistant_model_sub.add_parser("list", help="list configured assistant model profiles")
    assistant_model_list.add_argument("--config-yaml", default=None)
    assistant_model_list.add_argument("--env-file", default=None)
    assistant_model_list.add_argument("--no-local-env-file", action="store_true")
    assistant_model_list.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_current = assistant_model_sub.add_parser("current", help="show authoring and runtime active model")
    assistant_model_current.add_argument("--config-yaml", default=None)
    assistant_model_current.add_argument("--assistant-config", default=None)
    assistant_model_current.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_add = assistant_model_sub.add_parser("add", help="add or update one assistant model profile")
    assistant_model_add.add_argument("name")
    assistant_model_add.add_argument("--config-yaml", default=None)
    assistant_model_add.add_argument("--provider", required=True)
    assistant_model_add.add_argument("--model", required=True)
    assistant_model_add.add_argument("--base-url", default=None)
    assistant_model_add.add_argument("--api-key-env", default=None)
    assistant_model_add.add_argument("--confidence-min", type=float, default=None)
    assistant_model_add.add_argument("--timeout-seconds", type=int, default=None)
    assistant_model_add.add_argument("--max-output-tokens", type=int, default=None)
    assistant_model_add.add_argument("--replace", action="store_true")
    assistant_model_add.add_argument("--activate", action="store_true")
    assistant_model_add.add_argument("--apply", action="store_true")
    assistant_model_use = assistant_model_sub.add_parser("use", help="switch assistant.active_model")
    assistant_model_use.add_argument("name")
    assistant_model_use.add_argument("--config-yaml", default=None)
    assistant_model_use.add_argument("--apply", action="store_true")
    assistant_model_check = assistant_model_sub.add_parser("check", help="check one configured model profile")
    assistant_model_check.add_argument("name", nargs="?")
    assistant_model_check.add_argument("--active", action="store_true")
    assistant_model_check.add_argument("--config-yaml", default=None)
    assistant_model_check.add_argument("--env-file", default=None)
    assistant_model_check.add_argument("--no-local-env-file", action="store_true")
    assistant_model_check.add_argument("--live", action="store_true")
    assistant_model_check.add_argument("--text", default=None, help="probe text used with --live")
    assistant_model_check.add_argument("--format", choices=("json", "text"), default="json")
    assistant_pending = assistant_sub.add_parser("pending", help="inspect pending assistant operations")
    assistant_pending_sub = assistant_pending.add_subparsers(dest="assistant_pending_command", required=True)
    assistant_pending_list = assistant_pending_sub.add_parser("list", help="list previewed operations awaiting confirmation")
    assistant_pending_list.add_argument("--sender", dest="sender_id", default=None)
    assistant_pending_list.add_argument("--channel", default=None)
    assistant_pending_list.add_argument("--conversation-id", default=None)
    assistant_pending_list.add_argument("--operation-type", action="append", dest="operation_types", default=None)
    assistant_pending_list.add_argument("--include-expired", action="store_true")
    assistant_pending_list.add_argument("--limit", type=int, default=20)
    assistant_pending_list.add_argument("--audit-db", default=None)
    assistant_pending_list.add_argument("--format", choices=("json", "text"), default="json")
    assistant_audit = assistant_sub.add_parser("audit", help="inspect assistant audit records")
    assistant_audit_sub = assistant_audit.add_subparsers(dest="assistant_audit_command", required=True)
    assistant_audit_recent = assistant_audit_sub.add_parser("recent", help="show recent assistant audit records")
    assistant_audit_recent.add_argument("--sender", dest="sender_id", default=None)
    assistant_audit_recent.add_argument("--channel", default=None)
    assistant_audit_recent.add_argument("--conversation-id", default=None)
    assistant_audit_recent.add_argument("--limit", type=int, default=20)
    assistant_audit_recent.add_argument("--audit-db", default=None)
    assistant_audit_recent.add_argument("--format", choices=("json", "text"), default="json")
    assistant_upgrade_worker = assistant_sub.add_parser("upgrade-worker", help="run one confirmed assistant upgrade operation")
    assistant_upgrade_worker.add_argument("--operation-id", required=True)
    assistant_upgrade_worker.add_argument("--audit-db", default=None)
    assistant_upgrade_worker.add_argument("--env-file", default=None)
    assistant_upgrade_worker.add_argument("--no-local-env-file", action="store_true")
    assistant_upgrade_worker.add_argument("--no-final-receipt", action="store_true")
    assistant_upgrade_worker.add_argument("--format", choices=("json", "text"), default="json")


def _add_candidate_evidence_diagnostic_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-report-dir", default=None, help="directory containing candidate diagnostic evidence files")
    parser.add_argument("--candidate-path", action="append", dest="candidate_paths", default=None)
    parser.add_argument("--candidate-reject-log-path", action="append", dest="candidate_reject_log_paths", default=None)
    parser.add_argument("--candidate-trace-path", action="append", dest="candidate_trace_paths", default=None)
    parser.add_argument("--candidate-evidence-min-sample", type=int, default=None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor unified CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("healthcheck", help="run readiness checks")
    health.add_argument("--config-key", default=None, choices=("us", "hk"))
    health.add_argument("--config-path", default=None)
    health.add_argument("--accounts", nargs="*", default=None)
    health.add_argument("--opend-telnet-host", default=None)
    health.add_argument("--opend-telnet-port", type=int, default=None)
    health.add_argument("--audit-db", default=None)
    health.add_argument("--profile-path", default=None)
    health.add_argument("--env-file", default=None)
    health.add_argument("--no-local-env-file", action="store_true")
    health.add_argument("--include-service-status", action="store_true")
    _add_candidate_evidence_diagnostic_args(health)

    doctor = sub.add_parser("doctor", help="diagnose runtime readiness and common operator issues")
    doctor.add_argument("--config-key", default=None, choices=("us", "hk"))
    doctor.add_argument("--config-path", default=None)
    doctor.add_argument("--accounts", nargs="*", default=None)
    doctor.add_argument("--opend-telnet-host", default=None)
    doctor.add_argument("--opend-telnet-port", type=int, default=None)
    doctor.add_argument("--audit-db", default=None)
    doctor.add_argument("--profile-path", default=None)
    doctor.add_argument("--env-file", default=None)
    doctor.add_argument("--no-local-env-file", action="store_true")
    doctor.add_argument("--include-service-status", action="store_true")
    _add_candidate_evidence_diagnostic_args(doctor)

    support = sub.add_parser("support", help="collect redacted support diagnostics")
    support_sub = support.add_subparsers(dest="support_command", required=True)
    support_bundle = support_sub.add_parser("bundle", help="write a redacted support bundle JSON")
    support_bundle.add_argument("--config-key", default=None, choices=("us", "hk"))
    support_bundle.add_argument("--config-path", default=None)
    support_bundle.add_argument("--accounts", nargs="*", default=None)
    support_bundle.add_argument("--profile-path", default=None)
    support_bundle.add_argument("--env-file", default=None)
    support_bundle.add_argument("--no-local-env-file", action="store_true")
    support_bundle.add_argument("--include-healthcheck", action="store_true")
    support_bundle.add_argument("--runtime-root", default=None)
    support_bundle.add_argument("--output-dir", default=None)

    _add_assistant_commands(sub.add_parser("assistant", help="inspect optional conversational assistant runtime"))

    inbound = sub.add_parser("inbound", help="handle channel transport adapters")
    inbound_sub = inbound.add_subparsers(dest="inbound_command", required=True)
    inbound_feishu = inbound_sub.add_parser("feishu", help="handle one Feishu event payload through assistant control")
    feishu_input = inbound_feishu.add_mutually_exclusive_group(required=True)
    feishu_input.add_argument("--input-json", default=None)
    feishu_input.add_argument("--input-file", default=None)
    feishu_input.add_argument("--stdin", action="store_true")
    inbound_feishu.add_argument("--config-key", default=None, choices=("us", "hk"))
    inbound_feishu.add_argument("--config-path", default=None)
    inbound_feishu.add_argument("--assistant-config", default=None)
    inbound_feishu.add_argument("--audit-db", default=None)
    inbound_feishu.add_argument("--env-file", default=None)
    inbound_feishu.add_argument("--no-local-env-file", action="store_true")
    inbound_feishu.add_argument("--format", choices=("json", "text"), default="json")
    inbound_ws = inbound_sub.add_parser("feishu-ws", help="serve the Feishu App long-connection inbound client")
    inbound_ws.add_argument("--config-key", default=None, choices=("us", "hk"))
    inbound_ws.add_argument("--config-path", default=None)
    inbound_ws.add_argument("--assistant-config", default=None)
    inbound_ws.add_argument("--audit-db", default=None)
    inbound_ws.add_argument("--env-file", default=None)
    inbound_ws.add_argument("--no-local-env-file", action="store_true")
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
    status.add_argument("--env-file", default=None)
    status.add_argument("--no-local-env-file", action="store_true")
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

    add_research_commands(sub)

    add_operator_commands(sub)

    add_account_commands(sub)

    add_config_commands(sub)

    add_settings_commands(sub)

    sub.add_parser("version", help="check latest released version from git tags")

    add_scheduler_commands(sub)

    add_service_update_commands(sub)

    add_setup_commands(sub)

    sub.add_parser("symbols", help="manage monitored symbols")
    sub.add_parser("option-positions", help="option position operations")
    sub.add_parser("trade-events", help="review, repair, replay, and void trade events")

    run = sub.add_parser("run", help="run long-lived workflows")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    tick = run_sub.add_parser("tick", help="multi-account tick orchestration")
    tick.add_argument("--config", required=True)
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
    trade_intake.add_argument("--config", required=True)
    trade_intake.add_argument("--data-config", default=None)
    trade_intake.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    trade_intake.add_argument("--confirm", action="store_true")
    trade_intake.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    trade_intake.add_argument("--state-path", default=None)
    trade_intake.add_argument("--audit-path", default=None)
    trade_intake.add_argument("--status-path", default=None)
    trade_intake.add_argument("--host", default="127.0.0.1")
    trade_intake.add_argument("--port", type=int, default=11111)
    trade_intake.add_argument("--once", action="store_true")
    trade_intake.add_argument("--deal-json", default=None)
    trade_intake.add_argument("--retry-failed", action="store_true")
    trade_intake.add_argument("--reconcile-state", action="store_true")
    trade_intake.add_argument("--deal-id", action="append", default=None)
    trade_intake.add_argument("--apply", action="store_true")
    trade_intake.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _model_config_yaml_path(raw: str | None) -> Path:
    if raw is not None and str(raw).strip():
        return Path(raw).expanduser().resolve()
    return default_yaml_config_path(repo_root=repo_base())


def _load_model_authoring_config(raw: str | None) -> tuple[Path, dict[str, Any]]:
    path = _model_config_yaml_path(raw)
    return path, load_yaml_config_file(path)


def _assistant_model_text(data: dict[str, Any], *, command: str) -> str:
    if command == "catalog":
        return "\n".join(
            f"{item['provider']}: {', '.join(item.get('recommended_models') or [])}"
            for item in data.get("providers", [])
        )
    if command == "list":
        rows = data.get("models") or []
        if not rows:
            return "No assistant model profiles configured."
        return "\n".join(
            f"{'*' if item.get('active') else ' '} {item.get('name')} "
            f"{item.get('provider')}/{item.get('model')} "
            f"credential_configured={bool(item.get('api_key_configured'))}"
            for item in rows
        )
    if command == "current":
        summary = data.get("summary") or {}
        authoring = data.get("authoring") or {}
        runtime = data.get("runtime") or {}
        return "\n".join(
            [
                f"active_model: {summary.get('active_model') or '-'}",
                f"authoring: {_llm_text(authoring.get('llm') if isinstance(authoring, dict) else {})}",
                f"runtime: {_llm_text(runtime.get('llm') if isinstance(runtime, dict) else {})}",
                f"drift: {bool(summary.get('drift'))}",
            ]
        )
    if command == "check":
        summary = data.get("summary") or {}
        llm = data.get("llm") or {}
        return "\n".join(
            [
                f"status: {summary.get('status')}",
                f"ok: {bool(summary.get('ok'))}",
                f"model: {_llm_text(llm)}",
            ]
        )
    return _dumps(data).strip()


def _llm_text(raw: Any) -> str:
    llm = raw if isinstance(raw, dict) else {}
    provider = str(llm.get("provider") or "").strip() or "-"
    model = str(llm.get("model") or "").strip() or "-"
    base_url = str(llm.get("base_url") or "").strip()
    return f"{provider}/{model}" + (f" base_url={base_url}" if base_url else "")


def _check_assistant_model_profile(args: argparse.Namespace) -> dict[str, Any]:
    config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml)
    assistant = config_doc.get("assistant") if isinstance(config_doc.get("assistant"), dict) else {}
    profiles = parse_model_profiles(assistant.get("models") if isinstance(assistant, dict) else None)
    if args.name and args.active:
        raise AgentToolError(code="INPUT_ERROR", message="pass either a model profile name or --active, not both")
    profile_name = str(args.name or "").strip()
    if not profile_name:
        profile_name = str(assistant.get("active_model") or "").strip() if isinstance(assistant, dict) else ""
    if not profile_name:
        raise AgentToolError(code="INPUT_ERROR", message="no assistant active model is configured")
    profile = profiles.get(profile_name)
    if profile is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant model profile does not exist: {profile_name}",
            details={"available_models": sorted(profiles)},
        )
    runtime_cfg = {
        "assistant": {
            "mode": "llm_router",
            "context_window_messages": 8,
            "llm": profile.llm_config(),
        }
    }
    with tempfile.TemporaryDirectory(prefix="om-assistant-model-check-") as tmp_dir:
        assistant_config_path = Path(tmp_dir) / "config.assistant.json"
        assistant_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False), encoding="utf-8")
        data = check_llm_translator(
            repo_root=repo_base(),
            config_path=assistant_config_path,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
            live=bool(args.live),
            live_text=args.text or "状态",
        )
    data["profile"] = profile.public_payload(active=profile.name == str(assistant.get("active_model") or "").strip())
    data["config_yaml_path"] = str(config_yaml_path)
    if isinstance(data.get("config"), dict):
        data["config"]["config_path"] = str(config_yaml_path)
        data["config"]["model_profile"] = profile.name
    return data


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


def _load_json_object_arg(value: str | None, *, name: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except Exception as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"{name} must be a JSON object",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message=f"{name} must be a JSON object")
    return payload


def _should_bootstrap_process_env(actual_argv: list[str]) -> bool:
    if "--no-local-env-file" in actual_argv:
        return False
    if "--env-file" in actual_argv:
        return False
    if actual_argv and actual_argv[0] in {"settings", "setup"}:
        return False
    return True


def _bootstrap_runtime_env_from_args(args: argparse.Namespace) -> None:
    if not hasattr(args, "env_file"):
        return
    if not getattr(args, "env_file", None):
        return
    if args.command not in {"healthcheck", "doctor", "status", "inbound", "assistant"}:
        return
    bootstrap_process_env(
        repo_root=repo_base(),
        env_file=getattr(args, "env_file", None),
        include_local_env_file=not bool(getattr(args, "no_local_env_file", False)),
    )


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if argv is None and _should_bootstrap_process_env(actual_argv):
        bootstrap_process_env(repo_root=repo_base(), include_local_env_file=True)
    if actual_argv and actual_argv[0] == "agent":
        actual_argv[0] = "assistant"
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
    _bootstrap_runtime_env_from_args(args)
    try:
        if args.command == "healthcheck":
            return _print(
                run_healthcheck(
                    config_key=args.config_key,
                    config_path=args.config_path,
                    accounts=args.accounts,
                    opend_telnet_host=args.opend_telnet_host,
                    opend_telnet_port=args.opend_telnet_port,
                    audit_db=args.audit_db,
                    profile_path=args.profile_path,
                    include_service_status=bool(args.include_service_status),
                    env_file=args.env_file,
                    candidate_report_dir=args.candidate_report_dir,
                    candidate_paths=args.candidate_paths,
                    candidate_reject_log_paths=args.candidate_reject_log_paths,
                    candidate_trace_paths=args.candidate_trace_paths,
                    candidate_evidence_min_sample=args.candidate_evidence_min_sample,
                )
            )

        if args.command == "doctor":
            healthcheck = run_healthcheck(
                config_key=args.config_key,
                config_path=args.config_path,
                accounts=args.accounts,
                opend_telnet_host=args.opend_telnet_host,
                opend_telnet_port=args.opend_telnet_port,
                audit_db=args.audit_db,
                profile_path=args.profile_path,
                include_service_status=bool(args.include_service_status),
                env_file=args.env_file,
                candidate_report_dir=args.candidate_report_dir,
                candidate_paths=args.candidate_paths,
                candidate_reject_log_paths=args.candidate_reject_log_paths,
                candidate_trace_paths=args.candidate_trace_paths,
                candidate_evidence_min_sample=args.candidate_evidence_min_sample,
            )
            return _print(build_response(
                tool_name="doctor",
                ok=bool(healthcheck.get("ok", True)),
                data={"healthcheck": healthcheck},
            ))

        if args.command == "support" and args.support_command == "bundle":
            return _print(support_bundle_response(
                repo_root=repo_base(),
                config_key=args.config_key,
                config_path=args.config_path,
                accounts=args.accounts,
                profile_path=args.profile_path,
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
                include_healthcheck=bool(args.include_healthcheck),
                output_dir=args.output_dir,
                runtime_root=args.runtime_root,
            ))

        if args.command == "assistant" and args.assistant_command == "llm-check":
            data = check_llm_translator(
                repo_root=repo_base(),
                config_path=args.assistant_config,
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
                live=bool(args.live),
                live_text=args.text or "状态",
            )
            return _print(build_response(
                tool_name="assistant.llm_check",
                ok=bool(data.get("summary", {}).get("ok", True)),
                data=data,
            ))

        if args.command == "assistant" and args.assistant_command == "model":
            if args.assistant_model_command == "catalog":
                data = model_catalog()
                if args.format == "text":
                    sys.stdout.write(_assistant_model_text(data, command="catalog") + "\n")
                    return 0
                return _print(build_response(tool_name="assistant.model.catalog", ok=True, data=data))

            if args.assistant_model_command == "list":
                config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml)
                data = configured_model_profiles_payload(
                    config_doc=config_doc,
                    repo_root=repo_base(),
                    env_file=args.env_file,
                    include_local_env_file=not bool(args.no_local_env_file),
                )
                data["config_yaml_path"] = str(config_yaml_path)
                if args.format == "text":
                    sys.stdout.write(_assistant_model_text(data, command="list") + "\n")
                    return 0
                return _print(build_response(tool_name="assistant.model.list", ok=True, data=data))

            if args.assistant_model_command == "current":
                config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml)
                explicit_assistant_path = bool(args.assistant_config is not None and str(args.assistant_config).strip())
                assistant_config_path, assistant_cfg = load_assistant_config(
                    config_path=args.assistant_config,
                    repo_root=repo_base(),
                    missing_ok=not explicit_assistant_path,
                )
                data = current_model_payload(config_doc=config_doc, runtime_assistant_config=assistant_cfg)
                data["config_yaml_path"] = str(config_yaml_path)
                data["assistant_config_path"] = str(assistant_config_path)
                if args.format == "text":
                    sys.stdout.write(_assistant_model_text(data, command="current") + "\n")
                    return 0
                return _print(build_response(tool_name="assistant.model.current", ok=True, data=data))

            if args.assistant_model_command == "add":
                config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml)
                after_doc, profile = add_model_profile_to_config(
                    config_doc,
                    name=args.name,
                    provider=args.provider,
                    model=args.model,
                    base_url=args.base_url,
                    api_key_env=args.api_key_env,
                    confidence_min=args.confidence_min,
                    timeout_seconds=args.timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                    replace=bool(args.replace),
                    activate=bool(args.activate),
                )
                data = write_model_config_update(
                    config_path=config_yaml_path,
                    before_doc=config_doc,
                    after_doc=after_doc,
                    apply=bool(args.apply),
                    action="add",
                    payload={
                        "profile": profile.public_payload(active=bool(args.activate)),
                        "active_model": (
                            str(after_doc.get("assistant", {}).get("active_model") or "")
                            if isinstance(after_doc.get("assistant"), dict)
                            else None
                        ),
                    },
                )
                return _print(build_response(tool_name="assistant.model.add", ok=True, data=data))

            if args.assistant_model_command == "use":
                config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml)
                after_doc, profile = switch_active_model_profile(config_doc, name=args.name)
                data = write_model_config_update(
                    config_path=config_yaml_path,
                    before_doc=config_doc,
                    after_doc=after_doc,
                    apply=bool(args.apply),
                    action="use",
                    payload={
                        "profile": profile.public_payload(active=True),
                        "active_model": profile.name,
                        "rebuild_hint": "run `om config build-assistant --source yaml` after applying this change",
                    },
                )
                return _print(build_response(tool_name="assistant.model.use", ok=True, data=data))

            if args.assistant_model_command == "check":
                data = _check_assistant_model_profile(args)
                if args.format == "text":
                    sys.stdout.write(_assistant_model_text(data, command="check") + "\n")
                    return 0 if data.get("summary", {}).get("ok", True) else 2
                return _print(build_response(
                    tool_name="assistant.model.check",
                    ok=bool(data.get("summary", {}).get("ok", True)),
                    data=data,
                ))

        if args.command == "assistant" and args.assistant_command in {"commands", "capabilities"}:
            data = (
                capability_catalog_payload()
                if args.assistant_command == "capabilities"
                else command_catalog_payload()
            )
            if args.format == "text":
                text = (
                    capability_catalog_text(data)
                    if args.assistant_command == "capabilities"
                    else str(data.get("help_text") or "")
                )
                sys.stdout.write(text.strip() + "\n")
                return 0
            tool_name = (
                "assistant.capabilities"
                if args.assistant_command == "capabilities"
                else "assistant.commands"
            )
            return _print(build_response(tool_name=tool_name, ok=True, data=data))

        if args.command == "assistant" and args.assistant_command == "handle":
            assistant_settings = _assistant_settings_for_cli(
                config_key=args.config_key,
                config_path=args.config_path,
                assistant_config_path=args.assistant_config,
                force_enabled=None,
            )
            request = AssistantRequest(
                text=args.text,
                sender_id=args.sender_id,
                channel=args.channel,
                message_id=args.message_id,
                conversation_id=args.conversation_id,
                config_key=args.config_key,
                config_path=args.config_path,
                audit_db=args.audit_db,
            )
            out = handle_assistant_message(request, settings=assistant_settings)
            if args.format == "text":
                data_raw = out.get("data")
                data: dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
                text = str(data.get("response_text") or "").strip() or _dumps(out)
                sys.stdout.write(text + "\n")
                return 0 if out.get("ok", True) else 2
            return _print(out)

        if args.command == "assistant" and args.assistant_command == "pending" and args.assistant_pending_command == "list":
            data = collect_pending_operations(
                audit_db=args.audit_db,
                channel=args.channel,
                sender_id=args.sender_id,
                conversation_id=args.conversation_id,
                operation_types=args.operation_types,
                include_expired=bool(args.include_expired),
                limit=int(args.limit),
            )
            out = build_response(tool_name="assistant.pending.list", ok=True, data=data)
            if args.format == "text":
                sys.stdout.write(str(data.get("response_text") or "").strip() + "\n")
                return 0
            return _print(out)

        if args.command == "assistant" and args.assistant_command == "audit" and args.assistant_audit_command == "recent":
            data = collect_recent_audit(
                audit_db=args.audit_db,
                channel=args.channel,
                sender_id=args.sender_id,
                conversation_id=args.conversation_id,
                limit=int(args.limit),
            )
            out = build_response(tool_name="assistant.audit.recent", ok=True, data=data)
            if args.format == "text":
                sys.stdout.write(str(data.get("response_text") or "").strip() + "\n")
                return 0
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
                assistant_config_path=args.assistant_config,
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
                assistant_config_path=args.assistant_config,
                audit_db=args.audit_db,
                reply_enabled=False if bool(args.no_reply) else None,
                reply_in_thread=args.reply_in_thread,
                max_reply_chars=args.max_reply_chars,
                queue_size=args.queue_size,
                environ=os.environ,
                env_file=args.env_file,
            )
            if args.check:
                return _print(check_feishu_ws_settings(settings))
            serve_feishu_ws(settings, lock_path=args.lock_path)
            return 0

        if args.command == "assistant" and args.assistant_command == "upgrade-worker":
            out = run_confirmed_upgrade_operation(
                operation_id=args.operation_id,
                audit_db=args.audit_db,
                send_receipt=not bool(args.no_final_receipt),
            )
            if args.format == "text":
                data_raw = out.get("data")
                data = data_raw if isinstance(data_raw, dict) else {}
                text = str(data.get("response_text") or "").strip() or _dumps(out)
                sys.stdout.write(text + "\n")
                return 0 if out.get("ok", True) else 2
            return _print(out)

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

        if args.command == "research":
            return _print(handle_research_command(args, execute_tool_fn=execute_tool, repo_base_fn=repo_base))

        if args.command in {"scan", "close-advice", "notify"}:
            return _print(handle_operator_command(
                args,
                run_scan_fn=run_scan,
                run_close_advice_fn=run_close_advice,
                preview_notification_fn=preview_notification,
            ))

        if args.command == "accounts":
            return _print(handle_account_command(
                args,
                add_account_fn=add_account,
                edit_account_fn=edit_account,
                remove_account_fn=remove_account,
            ))

        if args.command == "config":
            return _print(handle_config_command(
                args,
                repo_base_fn=repo_base,
                validate_runtime_config_fn=_validate_runtime_config,
                validate_yaml_runtime_config_fn=validate_yaml_runtime_config,
                build_yaml_runtime_config_file_fn=build_yaml_runtime_config_file,
                build_yaml_assistant_config_file_fn=build_yaml_assistant_config_file,
                explain_yaml_config_key_fn=explain_yaml_config_key,
                preview_config_yaml_migration_fn=preview_config_yaml_migration,
                init_yaml_config_fn=init_yaml_config,
                get_runtime_config_value_fn=get_runtime_config_value,
            ))

        if args.command == "settings":
            return _print(handle_settings_command(
                args,
                repo_base_fn=repo_base,
                inspect_effective_settings_fn=inspect_effective_settings,
                diagnose_effective_settings_fn=diagnose_effective_settings,
                explain_effective_setting_fn=explain_effective_setting,
            ))

        if args.command == "version":
            sys.stdout.write(_dumps(check_version_update()))
            return 0

        if args.command in {"scheduler", "sell-put-cash"}:
            return handle_scheduler_command(
                args,
                repo_base_fn=repo_base,
                run_scheduler_fn=run_scheduler,
                query_sell_put_cash_fn=query_sell_put_cash,
            )

        if args.command in {"service", "update"}:
            return _print(handle_service_update_command(
                args,
                repo_base_fn=repo_base,
                load_service_profile_fn=load_service_profile,
                render_service_bundle_fn=render_service_bundle,
                service_preflight_fn=service_preflight,
                service_status_from_profile_fn=service_status_from_profile,
                write_service_bundle_fn=write_service_bundle,
                service_drift_fn=service_drift,
                service_cleanup_fn=service_cleanup,
                service_upgrade_check_fn=service_upgrade_check,
                service_upgrade_fn=service_upgrade,
                service_rollback_fn=service_rollback,
            ))

        if args.command in {"setup", "multiplier-cache"}:
            return _print(handle_setup_command(
                args,
                repo_base_fn=repo_base,
                run_setup_check_fn=run_setup_check,
            ))

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
            if args.retry_failed:
                intake_argv.append("--retry-failed")
            if args.reconcile_state:
                intake_argv.append("--reconcile-state")
            for deal_id in args.deal_id or []:
                intake_argv.extend(["--deal-id", str(deal_id)])
            if args.apply:
                intake_argv.append("--apply")
            if args.dry_run:
                intake_argv.append("--dry-run")
            return int(run_trade_intake(intake_argv))
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
