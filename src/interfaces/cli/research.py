from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.research import run_research_collect


def add_research_commands(subparsers: Any) -> argparse.ArgumentParser:
    research = subparsers.add_parser("research", help="collect and archive Research evidence")
    commands = research.add_subparsers(dest="research_command", required=True)

    watchlist = commands.add_parser(
        "watchlist-scope", help="preview or freeze exported OM windows/scan receipts plus 5%; no provider calls"
    )
    watchlist.add_argument("--input", required=True)
    watchlist.add_argument("--expected-input-sha256", required=True)
    watchlist.add_argument("--output-dir")
    watchlist.add_argument("--confirm", action="store_true")

    discovery = commands.add_parser(
        "watchlist-discover",
        help="preview or discover all 0--90 day expiries with OM chain owners; no strategy filtering",
    )
    for flag in (
        "input",
        "expected-input-sha256",
        "safe-window",
        "expected-safe-window-sha256",
        "budget",
        "expected-budget-sha256",
        "output-dir",
        "runtime-root",
        "opend-host",
    ):
        discovery.add_argument(f"--{flag}", required=True)
    for flag in ("budget-root", "prune-root"):
        discovery.add_argument(f"--{flag}", action="append", required=True)
    discovery.add_argument("--opend-port", type=int, required=True)
    discovery.add_argument("--confirm", action="store_true")

    daily = commands.add_parser(
        "watchlist-day-run", help="prepare one frozen daily request and reuse the existing collector"
    )
    for flag in ("request", "expected-request-sha256", "day-dir", "runtime-root", "opend-host"):
        daily.add_argument(f"--{flag}", required=True)
    for flag in ("budget-root", "prune-root"):
        daily.add_argument(f"--{flag}", action="append", required=True)
    daily.add_argument("--opend-port", type=int, required=True)
    daily.add_argument("--confirm", action="store_true")

    schedule = commands.add_parser(
        "watchlist-schedule-run", help="wake a fixed or recurring research schedule; no calls outside its windows"
    )
    for flag in ("schedule", "expected-schedule-sha256", "archive-root", "runtime-root", "opend-host"):
        schedule.add_argument(f"--{flag}", required=True)
    for flag in ("budget-root", "prune-root"):
        schedule.add_argument(f"--{flag}", action="append", required=True)
    schedule.add_argument("--opend-port", type=int, required=True)
    schedule.add_argument("--confirm", action="store_true")

    watchlist_plan = commands.add_parser(
        "watchlist-series-plan", help="freeze all watchlist shards with one explicit budget"
    )
    for flag in ("scope-dir", "expected-scope-sha256", "calendar", "policy", "budget", "start", "end", "output-dir"):
        watchlist_plan.add_argument(f"--{flag}", required=True)
    watchlist_plan.add_argument("--ancillary")
    watchlist_plan.add_argument("--expected-ancillary-sha256")
    watchlist_collect = commands.add_parser(
        "watchlist-series-collect", help="preview or collect all frozen watchlist shards"
    )
    for flag in (
        "plan-dir",
        "expected-plan-sha256",
        "safe-window",
        "expected-safe-window-sha256",
        "archive-root",
        "runtime-root",
        "opend-host",
    ):
        watchlist_collect.add_argument(f"--{flag}", required=True)
    for flag in ("budget-root", "prune-root"):
        watchlist_collect.add_argument(f"--{flag}", action="append", required=True)
    watchlist_collect.add_argument("--opend-port", required=True, type=int)
    watchlist_collect.add_argument("--confirm", action="store_true")

    series_plan = commands.add_parser(
        "observation-series-plan", help="freeze explicit offline research observations; no provider calls"
    )
    for flag in ("scope", "calendar", "policy", "start", "end", "output-dir"):
        series_plan.add_argument(f"--{flag}", required=True)

    timing = series_plan.add_mutually_exclusive_group(required=True)
    timing.add_argument("--last-trade")
    timing.add_argument("--observation-only", action="store_true")
    series_plan.add_argument("--observations", help="explicit additional execution/valuation observations JSON")

    series_collect = commands.add_parser(
        "observation-series-collect", help="preview or collect currently callable frozen research slots"
    )
    for flag in (
        "series-dir",
        "expected-plan-sha256",
        "safe-window",
        "expected-safe-window-sha256",
        "archive-root",
        "runtime-root",
        "opend-host",
    ):
        series_collect.add_argument(f"--{flag}", required=True)
    series_collect.add_argument("--prune-root", action="append", required=True)
    series_collect.add_argument("--opend-port", type=int, required=True)
    series_collect.add_argument("--ancillary-dir")
    series_collect.add_argument("--confirm", action="store_true")

    underlier_plan = commands.add_parser(
        "underlier-history-plan",
        help="preview underlier history scope from explicit offline canonical events",
    )
    underlier_plan.add_argument("--events", required=True)
    underlier_plan.add_argument("--account", required=True)
    underlier_plan.add_argument("--broker", required=True)
    underlier_plan.add_argument("--month", required=True)
    underlier_plan.add_argument("--as-of-ms", type=int, required=True)
    underlier_backfill = commands.add_parser(
        "underlier-history-backfill",
        help="preview or acquire unadjusted underlier history, calendar and corporate actions",
    )
    underlier_backfill.add_argument("--plan", required=True)
    underlier_backfill.add_argument("--events", default=None)
    underlier_backfill.add_argument("--output-dir", required=True)
    underlier_backfill.add_argument("--opend-host", required=True)
    underlier_backfill.add_argument("--opend-port", type=int, required=True)
    underlier_backfill.add_argument("--confirm", action="store_true")

    option_quotes = commands.add_parser(
        "option-quote-capture",
        help="capture current option snapshots for an explicit immutable research plan",
    )
    option_quotes.add_argument("--plan", required=True)
    option_quotes.add_argument("--output-dir", required=True)
    option_quotes.add_argument("--opend-host", required=True)
    option_quotes.add_argument("--opend-port", type=int, required=True)
    option_quotes.add_argument("--confirm", action="store_true")

    collect = commands.add_parser("collect", help="collect redacted evidence bundle")
    collect.add_argument("--scope", default="full", choices=("ledger", "candidate", "quality", "full"))
    collect.add_argument("--config-key", default=None, choices=("us", "hk"))
    collect.add_argument("--config-path", default=None)
    collect.add_argument("--accounts", nargs="*", default=None)
    collect.add_argument("--profile-path", default=None)
    collect.add_argument("--report-dir", default=None)
    collect.add_argument("--state-dir", default=None)
    collect.add_argument("--shared-state-dir", default=None)
    collect.add_argument("--accounts-root", default=None)
    collect.add_argument("--runs-root", default=None)
    collect.add_argument("--run-id", default=None)
    collect.add_argument("--run-dir", default=None)
    collect.add_argument("--runs-limit", type=int, default=None)
    collect.add_argument("--tail-limit", type=int, default=None)
    collect.add_argument("--max-run-age-minutes", type=int, default=None)
    collect.add_argument("--max-notification-chars", type=int, default=None)
    collect.add_argument("--output", default="handoff", choices=("handoff", "json", "both", "markdown", "md"))
    collect.add_argument("--scheduler-evidence-json", default=None)
    collect.add_argument("--scheduler-evidence-file", default=None)
    collect.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
    collect.add_argument("--candidate-report-dir", default=None)
    collect.add_argument(
        "--ranking-limit", type=int, default=None, help="top candidate rows per report included in ranking evidence"
    )
    collect.add_argument("--include-healthcheck", action="store_true")
    collect.add_argument("--data-config", default=None)
    collect.add_argument("--timeout-sec", type=int, default=None)
    collect.add_argument("--output-dir", default=None)
    collect.add_argument("--current-dir", default=None)
    collect.add_argument("--write-outputs", action="store_true")
    collect.add_argument("--no-write-outputs", action="store_true")
    collect.add_argument("--confirm", action="store_true")

    storage_baseline = commands.add_parser(
        "storage-baseline", help="collect a payload-free read-only runtime storage and capacity baseline"
    )
    storage_baseline.add_argument("--runtime-root", required=True)
    storage_baseline.add_argument("--ledger-sqlite", default=None)
    storage_baseline.add_argument(
        "--history-report",
        dest="history_reports",
        action="append",
        default=None,
        help="prior compatible storage baseline JSON; repeat in chronological order",
    )
    storage_baseline.add_argument("--output", default=None)
    storage_baseline.add_argument("--allow-external-ledger", action="store_true")
    storage_baseline.add_argument("--overwrite", action="store_true")

    storage_gc = commands.add_parser(
        "storage-gc-preview", help="preview reachable and orphaned canonical scan blobs without deleting"
    )
    storage_gc.add_argument("--runtime-root", required=True)

    cleanup = commands.add_parser(
        "storage-cleanup-preview", help="preview gated historical cleanup without moving or deleting data"
    )
    cleanup.add_argument("--runtime-root", required=True)
    cleanup.add_argument("--ledger-sqlite", default=None)
    cleanup.add_argument("--lifecycle-inventory", required=True)
    cleanup.add_argument("--quality-cutover-evidence", default=None)
    cleanup.add_argument("--backup-proof", default=None)
    cleanup.add_argument("--history-report", dest="history_reports", action="append", default=None)
    cleanup.add_argument("--allow-external-ledger", action="store_true")

    handoff = commands.add_parser("handoff", help="render handoff from a collected bundle")
    handoff.add_argument("--bundle", required=True)

    archive = commands.add_parser("archive", help="mirror remote Research evidence locally")
    archive_commands = archive.add_subparsers(dest="archive_command", required=True)
    series_export = archive_commands.add_parser(
        "series-export", help="export only one local research series into a new offline directory"
    )
    for flag in ("series-dir", "expected-plan-sha256", "cutoff", "output-dir"):
        series_export.add_argument(f"--{flag}", required=True)
    watchlist_export = archive_commands.add_parser(
        "watchlist-export", help="export all watchlist shards at one cutoff; no provider calls"
    )
    for flag in ("series-dir", "expected-plan-sha256", "cutoff", "output-dir"):
        watchlist_export.add_argument(f"--{flag}", required=True)
    watchlist_export.add_argument("--object-root", help="shared compressed research objects; no source deletion")
    day_pack = archive_commands.add_parser(
        "watchlist-day-pack", help="seal an ended local day; retain all source files"
    )
    for flag in ("day-dir", "expected-request-sha256", "object-root", "output-dir", "runtime-root"):
        day_pack.add_argument(f"--{flag}", required=True)
    day_pack.add_argument("--prune-root", action="append", required=True)
    day_restore = archive_commands.add_parser("watchlist-day-restore", help="restore source bytes into a new local day")
    for flag in ("manifest", "expected-manifest-sha256", "object-root", "output-dir", "runtime-root"):
        day_restore.add_argument(f"--{flag}", required=True)
    day_restore.add_argument("--prune-root", action="append", required=True)
    day_restore.add_argument("--min-free-bytes", type=int, required=True)
    inventory = archive_commands.add_parser("inventory", help="inspect the local remote-evidence archive")
    inventory.add_argument("--remote", default="prod")
    inventory.add_argument("--archive-root", default=None)

    pull = archive_commands.add_parser("pull", help="dry-run or rsync remote runtime evidence into local archive")
    pull.add_argument("--remote", default="prod")
    pull.add_argument("--archive-root", default=None)
    pull.add_argument(
        "--source-root", default=None, help="local or mounted runtime root; mutually exclusive with --ssh-target"
    )
    pull.add_argument("--ssh-target", default=None, help="ssh target such as deploy@host")
    pull.add_argument("--remote-runtime-root", default="/var/lib/options-monitor")
    pull.add_argument("--since-days", type=int, default=None)
    pull.add_argument("--run-id", dest="run_ids", action="append", default=None)
    pull.add_argument("--no-logs", action="store_true")
    pull.add_argument("--rsync-path", default="rsync")
    pull.add_argument("--write", action="store_true", help="execute rsync and write local sync/verify manifests")

    verify = archive_commands.add_parser(
        "verify", help="check local archive structure without changing source verification evidence"
    )
    verify.add_argument("--remote", default="prod")
    verify.add_argument("--archive-root", default=None)

    prune = archive_commands.add_parser(
        "prune-remote", help="guarded remote output-run cleanup after local archive verification"
    )
    prune.add_argument("--remote", default="prod")
    prune.add_argument("--archive-root", default=None)
    prune.add_argument("--ssh-target", required=True)
    prune.add_argument("--remote-repo-root", default="/opt/options-monitor/current")
    prune.add_argument("--remote-runtime-root", default="/var/lib/options-monitor")
    prune.add_argument("--keep-days", type=int, default=3)
    prune.add_argument("--keep-count", type=int, default=30)
    prune.add_argument("--no-logs", action="store_true")
    prune.add_argument("--confirm", action="store_true")
    return research


def _load_scheduler_evidence(*, json_text: str | None, file_path: str | None) -> dict[str, Any] | None:
    raw = Path(file_path).read_text(encoding="utf-8") if file_path else json_text
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message="scheduler evidence must be a JSON object")
    return payload


def _research_collect_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "scope": args.scope,
        "config_key": args.config_key,
        "config_path": args.config_path,
        "accounts": args.accounts,
        "profile_path": args.profile_path,
        "report_dir": args.report_dir,
        "state_dir": args.state_dir,
        "shared_state_dir": args.shared_state_dir,
        "accounts_root": args.accounts_root,
        "runs_root": args.runs_root,
        "run_id": args.run_id,
        "run_dir": args.run_dir,
        "runs_limit": args.runs_limit,
        "tail_limit": args.tail_limit,
        "max_run_age_minutes": args.max_run_age_minutes,
        "max_notification_chars": args.max_notification_chars,
        "output": args.output,
        "trace_paths": args.trace_paths,
        "candidate_report_dir": args.candidate_report_dir,
        "ranking_limit": args.ranking_limit,
        "include_healthcheck": bool(args.include_healthcheck),
        "data_config": args.data_config,
        "timeout_sec": args.timeout_sec,
        "research_output_dir": args.output_dir,
        "research_current_dir": args.current_dir,
        "write_outputs": bool(args.write_outputs),
        "confirm": bool(args.confirm),
    }
    if args.no_write_outputs:
        payload["write_outputs"] = False
    scheduler = _load_scheduler_evidence(json_text=args.scheduler_evidence_json, file_path=args.scheduler_evidence_file)
    if scheduler is not None:
        payload["scheduler_evidence"] = scheduler
    return {key: value for key, value in payload.items() if value not in (None, [])}


def handle_research_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    research_collect_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if args.research_command == "watchlist-schedule-run":
        from src.application.research.watchlist_day import run_watchlist_schedule

        data = run_watchlist_schedule(
            schedule_file=Path(args.schedule),
            expected_schedule_sha256=args.expected_schedule_sha256,
            archive_root=Path(args.archive_root),
            budget_roots=[Path(p) for p in args.budget_root],
            runtime_root=Path(args.runtime_root),
            prune_roots=[Path(p) for p in args.prune_root],
            host=args.opend_host,
            port=args.opend_port,
            confirm=bool(args.confirm),
        )
        return build_response(tool_name="research.watchlist-schedule-run", ok=True, data=data)
    if args.research_command == "watchlist-day-run":
        from src.application.research.watchlist_day import run_watchlist_day

        data = run_watchlist_day(
            request_file=Path(args.request),
            expected_request_sha256=args.expected_request_sha256,
            day_dir=Path(args.day_dir),
            budget_roots=[Path(p) for p in args.budget_root],
            runtime_root=Path(args.runtime_root),
            prune_roots=[Path(p) for p in args.prune_root],
            host=args.opend_host,
            port=args.opend_port,
            confirm=bool(args.confirm),
        )
        return build_response(tool_name="research.watchlist-day-run", ok=True, data=data)
    if args.research_command == "watchlist-discover":
        from src.application.research.watchlist_discovery import discover_watchlist

        data = discover_watchlist(
            input_file=Path(args.input),
            expected_input_sha256=args.expected_input_sha256,
            safe_window_file=Path(args.safe_window),
            expected_safe_window_sha256=args.expected_safe_window_sha256,
            budget_file=Path(args.budget),
            expected_budget_sha256=args.expected_budget_sha256,
            output_dir=Path(args.output_dir),
            budget_roots=[Path(p) for p in args.budget_root],
            runtime_root=Path(args.runtime_root),
            prune_roots=[Path(p) for p in args.prune_root],
            host=args.opend_host,
            port=args.opend_port,
            confirm=bool(args.confirm),
        )
        return build_response(tool_name="research.watchlist-discover", ok=True, data=data)
    if args.research_command == "watchlist-series-plan":
        from src.application.research.watchlist_series import build_watchlist_series

        data = build_watchlist_series(
            scope_dir=Path(args.scope_dir),
            expected_scope_sha256=args.expected_scope_sha256,
            calendar_file=Path(args.calendar),
            policy_file=Path(args.policy),
            budget_file=Path(args.budget),
            start_date=args.start,
            end_date=args.end,
            output_dir=Path(args.output_dir),
            ancillary_file=Path(args.ancillary) if args.ancillary else None,
            expected_ancillary_sha256=args.expected_ancillary_sha256,
        )
        return build_response(tool_name="research.watchlist-series-plan", ok=True, data=data)
    if args.research_command == "watchlist-series-collect":
        from src.application.research.watchlist_series import collect_watchlist_series

        data = collect_watchlist_series(
            plan_dir=Path(args.plan_dir),
            expected_plan_sha256=args.expected_plan_sha256,
            safe_window_file=Path(args.safe_window),
            expected_safe_window_sha256=args.expected_safe_window_sha256,
            archive_root=Path(args.archive_root),
            budget_roots=[Path(p) for p in args.budget_root],
            runtime_root=Path(args.runtime_root),
            prune_roots=[Path(p) for p in args.prune_root],
            host=args.opend_host,
            port=args.opend_port,
            confirm=bool(args.confirm),
        )
        return build_response(tool_name="research.watchlist-series-collect", ok=True, data=data)
    if args.research_command == "watchlist-scope":
        from src.application.research.watchlist_scope import build_watchlist_scope

        if args.confirm and not args.output_dir:
            raise ValueError("publishing scope requires output-dir")
        data = build_watchlist_scope(
            input_file=Path(args.input),
            expected_input_sha256=args.expected_input_sha256,
            output_dir=Path(args.output_dir) if args.confirm else None,
        )
        return build_response(tool_name="research.watchlist-scope", ok=True, data=data)
    if args.research_command == "observation-series-plan":
        from src.application.research.observation_series import build_series_plan

        if args.observation_only and args.observations is not None:
            raise ValueError("observation-only forbids additional observations")
        data = build_series_plan(
            scope_file=Path(args.scope),
            calendar_file=Path(args.calendar),
            policy_file=Path(args.policy),
            last_trade_file=Path(args.last_trade) if args.last_trade is not None else None,
            **({"observation_only": True} if args.observation_only else {}),
            start_date=args.start,
            end_date=args.end,
            output_dir=Path(args.output_dir),
            **({"observations_file": Path(args.observations)} if args.observations is not None else {}),
        )
        return build_response(tool_name="research.observation-series-plan", ok=True, data=data)
    if args.research_command == "observation-series-collect":
        from src.application.research.observation_series import collect_series

        data = collect_series(
            series_dir=Path(args.series_dir),
            expected_plan_sha256=args.expected_plan_sha256,
            safe_window_file=Path(args.safe_window),
            expected_safe_window_sha256=args.expected_safe_window_sha256,
            archive_root=Path(args.archive_root),
            runtime_root=Path(args.runtime_root),
            prune_roots=[Path(value) for value in args.prune_root],
            host=args.opend_host,
            port=args.opend_port,
            ancillary_dir=Path(args.ancillary_dir) if args.ancillary_dir else None,
            confirm=bool(args.confirm),
        )
        return build_response(tool_name="research.observation-series-collect", ok=True, data=data)
    if args.research_command in {"underlier-history-plan", "underlier-history-backfill"}:
        from src.application.research.underlier_history import (
            build_underlier_history_plan,
            collect_underlier_history,
        )

        if args.research_command == "underlier-history-plan":
            data = build_underlier_history_plan(
                args.events,
                account=args.account,
                broker=args.broker,
                month=args.month,
                as_of_ms=args.as_of_ms,
            )
        else:
            data = collect_underlier_history(
                json.loads(Path(args.plan).read_bytes()),
                output_dir=args.output_dir,
                host=args.opend_host,
                port=args.opend_port,
                confirm=args.confirm,
                events_path=args.events,
            )
        return build_response(tool_name=f"research.{args.research_command}", ok=True, data=data)
    if args.research_command == "option-quote-capture":
        from src.application.research.option_quote_capture import collect_option_quotes

        data = collect_option_quotes(
            json.loads(Path(args.plan).read_bytes()),
            output_dir=args.output_dir,
            host=args.opend_host,
            port=args.opend_port,
            confirm=args.confirm,
        )
        return build_response(tool_name="research.option-quote-capture", ok=True, data=data)
    if args.research_command == "storage-cleanup-preview":
        from src.application.research.historical_cleanup import build_historical_cleanup_preview

        data = build_historical_cleanup_preview(
            repo_root=repo_base_fn(),
            runtime_root=args.runtime_root,
            ledger_sqlite=args.ledger_sqlite,
            lifecycle_inventory=args.lifecycle_inventory,
            quality_cutover_evidence=args.quality_cutover_evidence,
            backup_proof=args.backup_proof,
            history_reports=args.history_reports,
            allow_external_ledger=bool(args.allow_external_ledger),
        )
        return build_response(tool_name="research.storage-cleanup-preview", ok=True, data=data)
    if args.research_command == "storage-gc-preview":
        from src.application.research.storage_baseline import preview_scan_blob_gc

        data = preview_scan_blob_gc(runtime_root=args.runtime_root)
        return build_response(tool_name="research.storage-gc-preview", ok=True, data=data)
    if args.research_command == "storage-baseline":
        from src.application.research.storage_baseline import collect_storage_runtime_baseline

        data = collect_storage_runtime_baseline(
            repo_root=repo_base_fn(),
            runtime_root=args.runtime_root,
            ledger_sqlite=args.ledger_sqlite,
            history_reports=args.history_reports,
            output=args.output,
            allow_external_ledger=bool(args.allow_external_ledger),
            overwrite=bool(args.overwrite),
        )
        return build_response(tool_name="research.storage-baseline", ok=True, data=data)
    if args.research_command == "collect":
        collect = research_collect_fn or (lambda payload: run_research_collect(payload, repo_base_fn=repo_base_fn))
        return collect(_research_collect_payload(args))
    if args.research_command == "handoff":
        from src.application.research.service import render_research_handoff

        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise AgentToolError(code="INPUT_ERROR", message="research bundle must be a JSON object")
        return build_response(
            tool_name="research.handoff", ok=True, data={"handoff_markdown": render_research_handoff(bundle)}
        )
    if args.research_command == "archive":
        if args.archive_command in {"watchlist-day-pack", "watchlist-day-restore"}:
            from src.application.research.watchlist_day import pack_watchlist_day, restore_watchlist_day

            common = dict(
                object_root=Path(args.object_root),
                output_dir=Path(args.output_dir),
                runtime_root=Path(args.runtime_root),
                prune_roots=[Path(p) for p in args.prune_root],
            )
            if args.archive_command == "watchlist-day-pack":
                data = pack_watchlist_day(
                    day_dir=Path(args.day_dir),
                    expected_request_sha256=args.expected_request_sha256,
                    **common,
                )
            else:
                data = restore_watchlist_day(
                    manifest_file=Path(args.manifest),
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    min_free_bytes=args.min_free_bytes,
                    **common,
                )
            return build_response(tool_name="research.archive." + args.archive_command, ok=True, data=data)
        if args.archive_command == "watchlist-export":
            from src.application.research.watchlist_day import export_watchlist_series

            storage = {"object_root": Path(args.object_root)} if args.object_root else {}
            data = export_watchlist_series(
                series_dir=Path(args.series_dir),
                expected_plan_sha256=args.expected_plan_sha256,
                cutoff=args.cutoff,
                output_dir=Path(args.output_dir),
                **storage,
            )
            return build_response(tool_name="research.archive.watchlist-export", ok=True, data=data)
        if args.archive_command == "series-export":
            from src.application.research.archive import archive_series_export

            data = archive_series_export(
                series_dir=Path(args.series_dir),
                expected_plan_sha256=args.expected_plan_sha256,
                cutoff=args.cutoff,
                output_dir=Path(args.output_dir),
            )
            return build_response(tool_name="research.archive.series-export", ok=True, data=data)
        from src.application.research.archive import (
            archive_inventory,
            archive_prune_remote,
            archive_pull,
            archive_verify,
        )

        base = repo_base_fn()
        if args.archive_command == "inventory":
            data = archive_inventory(repo_root=base, remote=args.remote, archive_root=args.archive_root)
        elif args.archive_command == "pull":
            data = archive_pull(
                repo_root=base,
                remote=args.remote,
                archive_root=args.archive_root,
                source_root=args.source_root,
                ssh_target=args.ssh_target,
                remote_runtime_root=args.remote_runtime_root,
                since_days=args.since_days,
                run_ids=args.run_ids,
                include_logs=not bool(args.no_logs),
                write=bool(args.write),
                rsync_path=args.rsync_path,
            )
        elif args.archive_command == "verify":
            data = archive_verify(repo_root=base, remote=args.remote, archive_root=args.archive_root)
        elif args.archive_command == "prune-remote":
            data = archive_prune_remote(
                repo_root=base,
                remote=args.remote,
                archive_root=args.archive_root,
                ssh_target=args.ssh_target,
                remote_repo_root=args.remote_repo_root,
                remote_runtime_root=args.remote_runtime_root,
                keep_days=args.keep_days,
                keep_count=args.keep_count,
                include_logs=not bool(args.no_logs),
                confirm=bool(args.confirm),
            )
        else:
            raise AgentToolError(
                code="INPUT_ERROR", message=f"unsupported research archive command: {args.archive_command}"
            )
        return build_response(tool_name=f"research.archive.{args.archive_command}", ok=bool(data.get("ok")), data=data)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research command: {args.research_command}")
