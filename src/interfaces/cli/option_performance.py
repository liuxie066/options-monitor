from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    backfill_cash_conversions,
    correct_superseded_cash_conversions,
    open_performance_evidence_repository,
    open_position_ledger_from_data_config,
)
from src.application.performance.service import build_option_period_performance
from src.interfaces.cli.ledger_write_safety import (
    add_write_flags,
    guard_ledger_write,
    resolve_cli_write_control,
)


_REPORT_TZ = ZoneInfo("Asia/Shanghai")


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-key", choices=["us", "hk"], default="us")
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--data-config", default=None)


def _add_config_scope_args(parser: argparse.ArgumentParser) -> None:
    _add_config_args(parser)
    parser.add_argument("--account", default=None)
    parser.add_argument("--broker", default=None)


def _add_apply_flags(
    parser: argparse.ArgumentParser,
    *,
    apply_help: str = "write validated evidence to the local ledger SQLite file",
) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="validate and preview without writing (default)")
    group.add_argument("--apply", action="store_true", help=apply_help)


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

    cash_conversion = sub.add_parser(
        "cash-conversion",
        help="inspect or backfill immutable event-time CNY cash conversions",
    )
    cash_conversion_sub = cash_conversion.add_subparsers(
        dest="cash_conversion_command",
        required=True,
    )
    backfill = cash_conversion_sub.add_parser(
        "backfill",
        help="backfill missing/pending cash_conversion.v1 from persisted FX evidence",
    )
    _add_config_scope_args(backfill)
    backfill.add_argument("--start-date", default=None, help="inclusive event date YYYY-MM-DD")
    backfill.add_argument("--end-date", default=None, help="inclusive event date YYYY-MM-DD")
    backfill.add_argument("--include-rows", action="store_true", help="include per-fact changes and unresolved rows")
    _add_apply_flags(
        backfill,
        apply_help="atomically enrich canonical ledger events and write an audit receipt",
    )
    correct = cash_conversion_sub.add_parser(
        "correct",
        help="replace observed conversions only when persisted FX evidence explicitly supersedes their rate fact",
    )
    _add_config_scope_args(correct)
    correct.add_argument("--start-date", default=None, help="inclusive event date YYYY-MM-DD")
    correct.add_argument("--end-date", default=None, help="inclusive event date YYYY-MM-DD")
    correct.add_argument("--include-rows", action="store_true", help="include per-fact corrections")
    add_write_flags(correct, high_risk=True)


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


def _date_boundary_ms(value: str | None, *, end: bool) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise AgentToolError("INVALID_ARGUMENT", f"invalid date {raw!r}; expected YYYY-MM-DD") from exc
    instant = datetime.combine(parsed, time.min, tzinfo=_REPORT_TZ)
    if end:
        instant = instant + timedelta(days=1) - timedelta(milliseconds=1)
    return int(instant.timestamp() * 1000)


def _migrate_cash_conversion(args: argparse.Namespace, *, correct: bool) -> dict[str, Any]:
    if correct and (bool(args.confirm) or bool(args.yes)) and not bool(args.apply):
        raise SystemExit(
            "cash-conversion correct writes require --apply together with --confirm or --yes"
        )
    apply = bool(args.apply)
    if correct:
        apply = bool(
            resolve_cli_write_control(
                args,
                command_name="option-performance cash-conversion correct",
                high_risk=True,
            )["write_requested"]
        )
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
    if correct and apply and guard_ledger_write(
        data_config=data_config_path,
        args=args,
        as_json=True,
    ) is None:
        raise SystemExit(2)
    _resolved_data_config, repo = open_position_ledger_from_data_config(
        base=repo_base(),
        data_config=data_config_path,
    )
    evidence_repo = open_performance_evidence_repository(repo)
    start_ms = _date_boundary_ms(args.start_date, end=False)
    end_ms = _date_boundary_ms(args.end_date, end=True)
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise AgentToolError("INVALID_ARGUMENT", "start-date must be on or before end-date")
    migrated_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        migrate = (
            correct_superseded_cash_conversions
            if correct
            else backfill_cash_conversions
        )
        result = migrate(
            repo,
            evidence_repo,
            account=str(args.account or "").strip().lower() or None,
            broker=str(args.broker or "").strip() or None,
            start_ms=start_ms,
            end_ms=end_ms,
            apply=apply,
            migrated_at_ms=migrated_at_ms,
        )
    except (TypeError, ValueError) as exc:
        raise AgentToolError("INVALID_ARGUMENT", str(exc)) from exc
    data = result.to_dict()
    unresolved_by_reason: dict[str, int] = {}
    for item in data.get("unresolved") or []:
        reason = str(item.get("reason") or "unknown")
        unresolved_by_reason[reason] = unresolved_by_reason.get(reason, 0) + 1
    data["unresolved_by_reason"] = [
        {"reason": reason, "count": count}
        for reason, count in sorted(unresolved_by_reason.items())
    ]
    data["include_rows"] = bool(args.include_rows)
    if not args.include_rows:
        data.pop("unresolved", None)
        data.pop("changes", None)
    data["schema_version"] = (
        "option_performance_cash_conversion_correction.output.v1"
        if correct
        else "option_performance_cash_conversion_backfill.output.v1"
    )
    data["dry_run"] = not apply
    if correct:
        data["correctable_conversion_count"] = data["preview_conversion_count"]
        data["corrected_conversion_count"] = data["migrated_conversion_count"]
        data["audit_id"] = data.get("batch_id") if apply else None
    data["scope"] = {
        "config_key": payload["config_key"],
        "account": str(args.account or "").strip().lower() or None,
        "broker": str(args.broker or "").strip() or None,
        "start_date": str(args.start_date or "").strip() or None,
        "end_date": str(args.end_date or "").strip() or None,
    }
    data["meta"] = {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
    }
    return data


def _backfill_cash_conversion(args: argparse.Namespace) -> dict[str, Any]:
    return _migrate_cash_conversion(args, correct=False)


def _correct_cash_conversion(args: argparse.Namespace) -> dict[str, Any]:
    return _migrate_cash_conversion(args, correct=True)


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

    if (
        args.option_performance_command == "cash-conversion"
        and args.cash_conversion_command == "backfill"
    ):
        return _backfill_cash_conversion(args)

    if (
        args.option_performance_command == "cash-conversion"
        and args.cash_conversion_command == "correct"
    ):
        return _correct_cash_conversion(args)

    raise AgentToolError("INVALID_ARGUMENT", "unsupported option-performance command")


__all__ = ["add_option_performance_commands", "handle_option_performance_command"]
