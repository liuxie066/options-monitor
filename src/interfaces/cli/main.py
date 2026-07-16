from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.pipeline_runtime import main as run_scan_pipeline
from src.application.settings import bootstrap_process_env
from src.application.version_check import check_version_update
from src.interfaces.cli.account_ops import (
    add_account,
    add_account_commands,
    edit_account,
    handle_account_command,
    remove_account,
)
from src.interfaces.cli.assistant_ops import (
    add_assistant_commands,
    check_assistant_llm,
    handle_assistant_command,
    handle_assistant_turn,
)
from src.interfaces.cli.channel_ops import add_channel_commands, handle_channel_command
from src.interfaces.cli.inbound_ops import (
    add_inbound_commands,
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_payload,
    handle_inbound_command,
    serve_feishu_ws,
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
    preview_config_yaml_migration,
    set_yaml_symbol_config,
    validate_yaml_runtime_config,
)
from src.interfaces.cli.copilot_ops import add_copilot_commands, handle_copilot_command
from src.interfaces.cli.event_source_ops import add_event_source_commands, handle_event_source_command
from src.interfaces.cli.operator_ops import (
    add_operator_commands,
    handle_operator_command,
    preview_notification,
    run_close_advice,
    run_scan,
)
from src.interfaces.cli.observability_ops import (
    add_diagnostic_commands,
    add_runtime_observability_commands,
    collect_runtime_logs,
    collect_runtime_runs,
    execute_tool,
    format_runtime_logs,
    format_runtime_runs,
    format_runtime_status_summary,
    handle_observability_command,
    run_healthcheck,
    runtime_status_payload_from_args,
    support_bundle_response,
)
from src.interfaces.cli.research import add_research_commands, handle_research_command
from src.interfaces.cli.run_ops import add_run_commands, handle_run_command, run_tick, run_tick_cron
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
    service_upgrade_verify,
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor unified CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    add_diagnostic_commands(sub)

    add_assistant_commands(sub.add_parser("assistant", help="inspect optional conversational assistant runtime"))

    add_copilot_commands(sub)

    add_inbound_commands(sub)
    add_runtime_observability_commands(sub)

    add_research_commands(sub)

    add_operator_commands(sub)

    add_channel_commands(sub)

    add_event_source_commands(sub)

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

    add_run_commands(sub)

    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


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
    if args.command not in {"healthcheck", "doctor", "status", "inbound", "assistant", "copilot"}:
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
        if args.command in {"healthcheck", "doctor", "support", "status", "runs", "logs"}:
            return handle_observability_command(
                args,
                repo_base_fn=repo_base,
                run_healthcheck_fn=run_healthcheck,
                support_bundle_response_fn=support_bundle_response,
                execute_tool_fn=execute_tool,
                runtime_status_payload_from_args_fn=runtime_status_payload_from_args,
                format_runtime_status_summary_fn=format_runtime_status_summary,
                collect_runtime_runs_fn=collect_runtime_runs,
                format_runtime_runs_fn=format_runtime_runs,
                collect_runtime_logs_fn=collect_runtime_logs,
                format_runtime_logs_fn=format_runtime_logs,
            )

        if args.command == "assistant":
            return handle_assistant_command(
                args,
                repo_base_fn=repo_base,
                check_assistant_llm_fn=check_assistant_llm,
                handle_assistant_turn_fn=handle_assistant_turn,
            )

        if args.command == "copilot":
            return _print(handle_copilot_command(args))

        if args.command == "inbound":
            return handle_inbound_command(
                args,
                handle_feishu_payload_fn=handle_feishu_payload,
                build_feishu_ws_settings_fn=build_feishu_ws_settings,
                check_feishu_ws_settings_fn=check_feishu_ws_settings,
                serve_feishu_ws_fn=serve_feishu_ws,
            )

        if args.command == "research":
            return _print(handle_research_command(
                args,
                repo_base_fn=repo_base,
            ))

        if args.command in {"scan", "close-advice", "notify"}:
            return _print(handle_operator_command(
                args,
                run_scan_fn=run_scan,
                run_close_advice_fn=run_close_advice,
                preview_notification_fn=preview_notification,
            ))

        if args.command == "channel":
            return _print(handle_channel_command(args, repo_base_fn=repo_base))

        if args.command == "event-source":
            return _print(handle_event_source_command(args))

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
                set_yaml_symbol_config_fn=set_yaml_symbol_config,
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
                service_upgrade_verify_fn=service_upgrade_verify,
                service_upgrade_fn=service_upgrade,
                service_rollback_fn=service_rollback,
            ))

        if args.command in {"setup", "multiplier-cache"}:
            return _print(handle_setup_command(
                args,
                repo_base_fn=repo_base,
                run_setup_check_fn=run_setup_check,
            ))

        if args.command == "run":
            return handle_run_command(args, run_tick_fn=run_tick, run_tick_cron_fn=run_tick_cron)
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
