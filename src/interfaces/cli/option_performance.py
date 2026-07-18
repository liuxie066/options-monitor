from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.agent_tools.materialization_impl import (
    capture_option_performance_evidence,
    option_performance_report_tool,
)
from src.application.agent_tools.runtime_helpers import (
    normalize_broker,
    resolve_public_data_config_path,
)
from src.application.ledger.api import (
    open_performance_evidence_repository,
    open_position_ledger_from_data_config,
)
from src.application.performance.service import build_option_period_performance


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-key", choices=["us", "hk"], default="us")
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--data-config", default=None)


def _add_config_scope_args(parser: argparse.ArgumentParser) -> None:
    _add_config_args(parser)
    parser.add_argument("--account", default=None)
    parser.add_argument("--broker", default=None)


def _add_apply_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="validate and preview without writing (default)")
    group.add_argument("--apply", action="store_true", help="write validated evidence to the local ledger SQLite file")


def add_option_performance_commands(subparsers: argparse._SubParsersAction) -> None:
    root = subparsers.add_parser(
        "option-performance",
        help="period option performance reports and explicit evidence lifecycle",
    )
    sub = root.add_subparsers(dest="option_performance_command", required=True)

    report = sub.add_parser("report", help="build a read-only option performance report")
    _add_config_scope_args(report)
    report.add_argument(
        "--period",
        choices=["mtd", "ytd", "month", "year", "range"],
        default="mtd",
    )
    report.add_argument("--as-of-date", default=None, help="MTD/YTD inclusive YYYY-MM-DD")
    report.add_argument("--month", default=None, help="natural month YYYY-MM")
    report.add_argument("--year", default=None, help="natural year YYYY")
    report.add_argument("--start-date", default=None, help="range start YYYY-MM-DD")
    report.add_argument("--end-date", default=None, help="range end YYYY-MM-DD")
    report.add_argument("--include-rows", action="store_true")
    refresh = report.add_mutually_exclusive_group()
    refresh.add_argument("--refresh-quotes", dest="refresh_quotes", action="store_true")
    refresh.add_argument("--no-refresh-quotes", dest="refresh_quotes", action="store_false")
    report.set_defaults(refresh_quotes=True)

    evidence = sub.add_parser("evidence", help="validate, import, or capture performance evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)

    import_cmd = evidence_sub.add_parser("import", help="import a v1 evidence envelope")
    _add_config_args(import_cmd)
    import_cmd.add_argument("--file", required=True, help="JSON v1 evidence envelope")
    _add_apply_flags(import_cmd)

    capture = evidence_sub.add_parser("capture", help="capture current live evidence")
    _add_config_scope_args(capture)
    _add_apply_flags(capture)


def _scope_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "config_key": args.config_key,
        "config_path": args.config_path,
        "data_config": args.data_config,
        "account": args.account,
        "broker": args.broker,
    }


def _report_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **_scope_payload(args),
        "period": args.period,
        "as_of_date": args.as_of_date,
        "month": args.month,
        "year": args.year,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "include_rows": bool(args.include_rows),
        "refresh_quotes": bool(args.refresh_quotes),
    }


def _import_evidence(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "config_key": args.config_key,
        "config_path": args.config_path,
        "data_config": args.data_config,
    }
    config_path, cfg = load_runtime_config(
        config_key=payload["config_key"],
        config_path=payload.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    _resolved_data_config, repo = open_position_ledger_from_data_config(
        base=repo_base(),
        data_config=data_config_path,
    )
    evidence_repo = open_performance_evidence_repository(repo)
    file_path = Path(args.file).expanduser()
    if not file_path.is_absolute():
        file_path = file_path.resolve()
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError("INVALID_ARGUMENT", f"evidence file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentToolError("INVALID_ARGUMENT", f"evidence file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentToolError("INVALID_ARGUMENT", "evidence file must contain a JSON object")
    try:
        result = evidence_repo.import_envelope(
            raw,
            apply=bool(args.apply),
            migrated_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
    except ValueError as exc:
        raise AgentToolError("INVALID_ARGUMENT", str(exc)) from exc
    data = result.to_dict()
    data["schema_version"] = "option_performance_evidence_import.output.v1"
    data["dry_run"] = not bool(args.apply)
    data["source_file"] = mask_path(file_path)
    data["scope"] = {
        "config_key": payload["config_key"],
    }
    data["meta"] = {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
    }
    return data


def handle_option_performance_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.option_performance_command == "report":
        data, _warnings, _meta = option_performance_report_tool(
            _report_payload(args),
            load_runtime_config=load_runtime_config,
            resolve_public_data_config_path=resolve_public_data_config_path,
            normalize_broker=normalize_broker,
            resolve_option_positions_repo=open_position_ledger_from_data_config,
            open_performance_evidence_repository=open_performance_evidence_repository,
            build_option_period_performance=build_option_period_performance,
            repo_base=repo_base,
            mask_path=mask_path,
        )
        return data

    if args.option_performance_command == "evidence" and args.evidence_command == "import":
        return _import_evidence(args)

    if args.option_performance_command == "evidence" and args.evidence_command == "capture":
        data, _warnings, _meta = capture_option_performance_evidence(
            _scope_payload(args),
            apply=bool(args.apply),
            load_runtime_config=load_runtime_config,
            resolve_public_data_config_path=resolve_public_data_config_path,
            normalize_broker=normalize_broker,
            resolve_option_positions_repo=open_position_ledger_from_data_config,
            open_performance_evidence_repository=open_performance_evidence_repository,
            repo_base=repo_base,
            mask_path=mask_path,
        )
        return data

    raise AgentToolError("INVALID_ARGUMENT", "unsupported option-performance command")


__all__ = ["add_option_performance_commands", "handle_option_performance_command"]
