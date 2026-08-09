from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.healthcheck import run_healthcheck
from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs
from src.application.runtime_runs_cli import collect_runtime_runs, format_runtime_runs
from src.application.runtime_status_cli import (
    format_runtime_status_journal_summary,
    format_runtime_status_summary,
    runtime_status_payload_from_args,
)
from src.application.support_bundle import support_bundle_response
from src.application.tool_execution import execute_tool


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _healthcheck_readiness_ok(payload: dict[str, Any]) -> bool:
    if payload.get("ok") is not True:
        return False
    data = payload.get("data")
    summary = data.get("summary") if isinstance(data, dict) else None
    return not isinstance(summary, dict) or summary.get("ok") is not False


def _print_healthcheck(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if _healthcheck_readiness_ok(payload) else 2


def add_diagnostic_commands(subparsers: Any) -> None:
    health = subparsers.add_parser("healthcheck", help="run readiness checks")
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

    doctor = subparsers.add_parser("doctor", help="diagnose runtime readiness and common operator issues")
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

    support = subparsers.add_parser("support", help="collect redacted support diagnostics")
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


def add_runtime_observability_commands(subparsers: Any) -> None:
    status = subparsers.add_parser("status", help="summarize runtime status")
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
    status_output = status.add_mutually_exclusive_group()
    status_output.add_argument("--json", action="store_true", help="print raw runtime_status JSON envelope")
    status_output.add_argument(
        "--journal-summary",
        action="store_true",
        help="print a bounded summary suitable for service journals",
    )

    runs = subparsers.add_parser("runs", help="list runtime run snapshots")
    runs.add_argument("--runs-root", default=None)
    runs.add_argument("--profile-path", default=None)
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--run-id", default=None)
    runs.add_argument("--run-dir", default=None)
    runs.add_argument("--scanned-only", action="store_true")
    runs.add_argument("--json", action="store_true", help="print JSON envelope")

    logs = subparsers.add_parser("logs", help="tail runtime logs and run audit files")
    logs.add_argument("--runs-root", default=None)
    logs.add_argument("--logs-root", default=None)
    logs.add_argument("--profile-path", default=None)
    logs.add_argument("--run-id", default=None)
    logs.add_argument("--run-dir", default=None)
    logs.add_argument("--kind", default="all", choices=("all", "audit", "tool", "tick", "service"))
    logs.add_argument("--lines", type=int, default=50)
    logs.add_argument("--file", dest="log_file", default=None)
    logs.add_argument("--json", action="store_true", help="print JSON envelope")


def _healthcheck_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "config_key": args.config_key,
        "config_path": args.config_path,
        "accounts": args.accounts,
        "opend_telnet_host": args.opend_telnet_host,
        "opend_telnet_port": args.opend_telnet_port,
        "audit_db": args.audit_db,
        "profile_path": args.profile_path,
        "include_service_status": bool(args.include_service_status),
        "env_file": args.env_file,
    }


def handle_observability_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    run_healthcheck_fn: Callable[..., dict[str, Any]] = run_healthcheck,
    support_bundle_response_fn: Callable[..., dict[str, Any]] = support_bundle_response,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]] = execute_tool,
    runtime_status_payload_from_args_fn: Callable[[argparse.Namespace], dict[str, Any]] = runtime_status_payload_from_args,
    format_runtime_status_summary_fn: Callable[[dict[str, Any]], str] = format_runtime_status_summary,
    format_runtime_status_journal_summary_fn: Callable[[dict[str, Any]], str] = format_runtime_status_journal_summary,
    collect_runtime_runs_fn: Callable[..., dict[str, Any]] = collect_runtime_runs,
    format_runtime_runs_fn: Callable[[dict[str, Any]], str] = format_runtime_runs,
    collect_runtime_logs_fn: Callable[..., dict[str, Any]] = collect_runtime_logs,
    format_runtime_logs_fn: Callable[[dict[str, Any]], str] = format_runtime_logs,
) -> int:
    if args.command == "healthcheck":
        return _print_healthcheck(
            run_healthcheck_fn(**_healthcheck_kwargs(args))
        )

    if args.command == "doctor":
        healthcheck = run_healthcheck_fn(**_healthcheck_kwargs(args))
        execution_ok = healthcheck.get("ok") is True
        readiness_ok = _healthcheck_readiness_ok(healthcheck)
        return _print(build_response(
            tool_name="doctor",
            ok=bool(execution_ok and readiness_ok),
            data={
                "execution_ok": execution_ok,
                "readiness_ok": readiness_ok,
                "healthcheck": healthcheck,
            },
        ))

    if args.command == "support" and args.support_command == "bundle":
        return _print(support_bundle_response_fn(
            repo_root=repo_base_fn(),
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

    if args.command == "status":
        out = execute_tool_fn("runtime_status", runtime_status_payload_from_args_fn(args))
        if args.json:
            return _print(out)
        if args.journal_summary:
            sys.stdout.write(format_runtime_status_journal_summary_fn(out))
        else:
            sys.stdout.write(format_runtime_status_summary_fn(out))
        return 0 if out.get("ok", True) else 2

    if args.command == "runs":
        data = collect_runtime_runs_fn(
            repo_root=repo_base_fn(),
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
        sys.stdout.write(format_runtime_runs_fn(data))
        return 0 if envelope.get("ok", True) else 2

    if args.command == "logs":
        data = collect_runtime_logs_fn(
            repo_root=repo_base_fn(),
            runs_root=args.runs_root,
            logs_root=args.logs_root,
            profile_path=args.profile_path,
            run_id=args.run_id,
            run_dir=args.run_dir,
            kind=args.kind,
            lines=int(args.lines),
            log_file=args.log_file,
            allow_explicit_file_outside_roots=True,
        )
        envelope = build_response(
            tool_name="logs",
            ok=bool(data.get("summary", {}).get("ok", True)),
            data=data,
        )
        if args.json:
            return _print(envelope)
        sys.stdout.write(format_runtime_logs_fn(data))
        return 0 if envelope.get("ok", True) else 2

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported observability command: {args.command}")
