from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.pipeline_runtime import main as run_scan_pipeline
from src.application.version_check import check_version_update
from src.interfaces.cli.account_ops import (
    add_account,
    add_account_commands,
    edit_account,
    handle_account_command,
    remove_account,
)
from src.interfaces.cli.bot_control_ops import (
    check_bot_llm,
    handle_bot_control_command,
    handle_bot_turn,
)
from src.interfaces.cli.channel_ops import add_channel_commands, handle_channel_command
from src.interfaces.cli.command_environment import command_environment
from src.interfaces.cli.feature_ops import add_holdings_commands, run_feature_configure
from src.interfaces.cli.home import command_guide, interactive_home, render_credential_readiness, render_settings_doctor, render_setup_check
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
    build_yaml_bot_config_file,
    build_yaml_runtime_config_file,
    explain_yaml_config_key,
    get_runtime_config_value,
    handle_config_command,
    init_yaml_config,
    set_yaml_symbol_config,
    validate_yaml_runtime_config,
)
from src.interfaces.cli.bot_ops import add_bot_commands, handle_bot_command
from src.interfaces.cli.decision_history_ops import add_decision_history_commands, handle_decision_history_command
from src.interfaces.cli.daily_brief_ops import add_daily_brief_commands, handle_daily_brief_command
from src.interfaces.cli.operator_ops import (
    add_operator_commands,
    handle_operator_command,
    preview_notification,
    run_scan,
)
from src.interfaces.cli.option_performance import (
    add_option_performance_commands,
    handle_option_performance_command,
)
from src.interfaces.cli.portfolio_ops import (
    add_portfolio_commands,
    handle_portfolio_command,
)
from src.interfaces.quality.cli import add_quality_commands, handle_quality_command
from src.interfaces.cli.observability_ops import (
    add_diagnostic_commands,
    add_runtime_observability_commands,
    collect_runtime_logs,
    collect_runtime_runs,
    execute_tool,
    format_runtime_logs,
    format_runtime_runs,
    format_runtime_status_journal_summary,
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
from src.interfaces.cli.secret_ops import add_secret_commands, read_credential_readiness, run_store_command
from src.interfaces.cli.service_ops import (
    add_service_update_commands,
    capture_preserved_timer_activation_states,
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
from src.interfaces.cli.setup_ops import add_setup_commands, handle_setup_command, run_setup_check, run_setup_init


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is not None:
        argv = list(argv)
        if argv and argv[0] in {"assistant", "agent"}:
            argv[0] = "bot"
        if argv[:2] == ["config", "build-assistant"]:
            argv[1] = "build-bot"
        argv = ["--bot-config" if arg == "--assistant-config" else arg for arg in argv]
    parser = argparse.ArgumentParser(prog="om", description="options-monitor operator CLI; use `om help` for tasks, `om-agent spec` for structured tools")
    sub = parser.add_subparsers(dest="command", required=True)

    add_diagnostic_commands(sub)


    add_bot_commands(sub)

    add_inbound_commands(sub)
    add_runtime_observability_commands(sub)

    add_research_commands(sub)

    add_operator_commands(sub)

    add_channel_commands(sub)
    add_holdings_commands(sub)

    add_account_commands(sub)

    add_config_commands(sub)

    add_settings_commands(sub)

    add_secret_commands(sub)

    sub.add_parser("version", help="check latest released version from git tags")

    add_scheduler_commands(sub)

    add_service_update_commands(sub)

    add_setup_commands(sub)

    add_option_performance_commands(sub)

    add_portfolio_commands(sub)

    add_daily_brief_commands(sub)
    add_decision_history_commands(sub)
    add_quality_commands(sub)

    sub.add_parser("symbols", help="manage monitored symbols in config.yaml")
    sub.add_parser("option-positions", help="option position operations")
    sub.add_parser("wheel", help="Wheel lifecycle operations")
    sub.add_parser("trade-events", help="review, repair, replay, and void trade events")

    add_run_commands(sub)

    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if not actual_argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return interactive_home(lambda command: main(command, discover_local=True))
        sys.stdout.write(command_guide())
        return 0
    if actual_argv in (["help"], ["--help"], ["-h"]):
        sys.stdout.write(command_guide())
        return 0
    if actual_argv == ["help", "all"]:
        try:
            parse_args(["--help"])
        except SystemExit as exc:
            return int(exc.code)
    if actual_argv and actual_argv[0] == "agent":
        actual_argv[0] = "bot"
    if actual_argv and actual_argv[0] == "scan-pipeline":
        return int(run_scan_pipeline(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "option-positions":
        from src.interfaces.cli.option_positions import main as run_option_positions_cli

        return int(run_option_positions_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "wheel":
        from src.interfaces.cli.wheel import main as run_wheel_cli

        return int(run_wheel_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "trade-events":
        from src.interfaces.cli.trade_events import main as run_trade_events_cli

        return int(run_trade_events_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "symbols":
        from src.interfaces.cli.symbols import main as run_symbols_cli

        return int(run_symbols_cli(actual_argv[1:]))
    if actual_argv and actual_argv[0] == "assistant":
        actual_argv[0] = "bot"
    args = parse_args(actual_argv)
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
                format_runtime_status_journal_summary_fn=format_runtime_status_journal_summary,
                collect_runtime_runs_fn=collect_runtime_runs,
                format_runtime_runs_fn=format_runtime_runs,
                collect_runtime_logs_fn=collect_runtime_logs,
                format_runtime_logs_fn=format_runtime_logs,
            )

        if args.command == "bot" and getattr(args, "bot_control_command", None):
            return handle_bot_control_command(
                args,
                repo_base_fn=repo_base,
                check_bot_llm_fn=check_bot_llm,
                handle_bot_turn_fn=handle_bot_turn,
            )

        if args.command == "bot":
            result = handle_bot_command(args)
            return result if isinstance(result, int) else _print(result)

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
                preview_notification_fn=preview_notification,
            ))

        if args.command == "channel":
            result = handle_channel_command(args, repo_base_fn=repo_base)
            return result if isinstance(result, int) else _print(result)

        if args.command == "holdings":
            return _print(run_feature_configure(args, repo_base_fn=repo_base))

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
                build_yaml_bot_config_file_fn=build_yaml_bot_config_file,
                explain_yaml_config_key_fn=explain_yaml_config_key,
                init_yaml_config_fn=init_yaml_config,
                get_runtime_config_value_fn=get_runtime_config_value,
                set_yaml_symbol_config_fn=set_yaml_symbol_config,
            ))

        if args.command == "settings":
            result = handle_settings_command(
                args,
                repo_base_fn=repo_base,
                inspect_effective_settings_fn=inspect_effective_settings,
                diagnose_effective_settings_fn=diagnose_effective_settings,
                explain_effective_setting_fn=explain_effective_setting,
            )
            if args.settings_command == "doctor" and args.format == "text":
                sys.stdout.write(render_settings_doctor(result["data"]))
                return 0 if result.get("ok", True) else 2
            return _print(result)

        if args.command == "secrets":
            if args.store_action == "status" and args.format == "text":
                sys.stdout.write(render_credential_readiness(read_credential_readiness(args)))
                return 0
            return _print(run_store_command(args))

        if args.command == "version":
            sys.stdout.write(_dumps(check_version_update()))
            return 0

        if args.command == "option-performance":
            return _print(handle_option_performance_command(args))

        if args.command == "portfolio":
            return handle_portfolio_command(args)

        if args.command == "decision-history":
            return handle_decision_history_command(args, repo_base_fn=repo_base)
        if args.command == "daily-brief":
            return handle_daily_brief_command(args, repo_base_fn=repo_base)

        if args.command == "quality":
            result = handle_quality_command(args)
            return int(result) if isinstance(result, int) else _print(result)

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
                capture_preserved_timer_activation_states_fn=capture_preserved_timer_activation_states,
                service_cleanup_fn=service_cleanup,
                service_upgrade_check_fn=service_upgrade_check,
                service_upgrade_verify_fn=service_upgrade_verify,
                service_upgrade_fn=service_upgrade,
                service_rollback_fn=service_rollback,
            ))

        if args.command in {"setup", "multiplier-cache"}:
            if args.command == "setup" and args.setup_command == "init":
                if sys.stdin.isatty() and not (args.dry_run or args.apply):
                    from src.interfaces.cli.journeys import first_install
                    return first_install(args, lambda command: main(command, discover_local=True),
                                         base_init=run_setup_init, repo_base_fn=repo_base)
                output, _applied = run_setup_init(args, repo_base_fn=repo_base)
                sys.stdout.write(output)
                return 0
            result = handle_setup_command(
                args,
                repo_base_fn=repo_base,
                run_setup_check_fn=run_setup_check,
            )
            if args.command == "setup" and args.format == "text":
                sys.stdout.write(render_setup_check(result["data"]))
                return 0 if result.get("ok", True) else 2
            return _print(result)

        if args.command == "run":
            return handle_run_command(args, run_tick_fn=run_tick, run_tick_cron_fn=run_tick_cron)
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))

    raise SystemExit(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None, *, discover_local: bool = False) -> int:
    try:
        with command_environment(
            list(sys.argv[1:] if argv is None else argv), repo_root=repo_base(),
            discover_local=argv is None or discover_local,
        ):
            return _main(argv)
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))
    except ValueError as exc:
        err = AgentToolError(code="CONFIG_ERROR", message=str(exc))
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))


if __name__ == "__main__":
    raise SystemExit(main())
