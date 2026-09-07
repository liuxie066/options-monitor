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
    collect.add_argument("--ranking-limit", type=int, default=None, help="top candidate rows per report included in ranking evidence")
    collect.add_argument("--include-healthcheck", action="store_true")
    collect.add_argument("--data-config", default=None)
    collect.add_argument("--timeout-sec", type=int, default=None)
    collect.add_argument("--output-dir", default=None)
    collect.add_argument("--current-dir", default=None)
    collect.add_argument("--write-outputs", action="store_true")
    collect.add_argument("--no-write-outputs", action="store_true")
    collect.add_argument("--confirm", action="store_true")

    storage_baseline = commands.add_parser("storage-baseline", help="collect a payload-free read-only runtime storage and capacity baseline")
    storage_baseline.add_argument("--runtime-root", required=True)
    storage_baseline.add_argument("--ledger-sqlite", default=None)
    storage_baseline.add_argument("--history-report", dest="history_reports", action="append", default=None, help="prior compatible storage baseline JSON; repeat in chronological order")
    storage_baseline.add_argument("--output", default=None)
    storage_baseline.add_argument("--allow-external-ledger", action="store_true")
    storage_baseline.add_argument("--overwrite", action="store_true")

    storage_gc = commands.add_parser("storage-gc-preview", help="preview reachable and orphaned canonical scan blobs without deleting")
    storage_gc.add_argument("--runtime-root", required=True)

    cleanup = commands.add_parser("storage-cleanup-preview", help="preview gated historical cleanup without moving or deleting data")
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
    inventory = archive_commands.add_parser("inventory", help="inspect the local remote-evidence archive")
    inventory.add_argument("--remote", default="prod")
    inventory.add_argument("--archive-root", default=None)

    pull = archive_commands.add_parser("pull", help="dry-run or rsync remote runtime evidence into local archive")
    pull.add_argument("--remote", default="prod")
    pull.add_argument("--archive-root", default=None)
    pull.add_argument("--source-root", default=None, help="local or mounted runtime root; mutually exclusive with --ssh-target")
    pull.add_argument("--ssh-target", default=None, help="ssh target such as deploy@host")
    pull.add_argument("--remote-runtime-root", default="/var/lib/options-monitor")
    pull.add_argument("--since-days", type=int, default=None)
    pull.add_argument("--run-id", dest="run_ids", action="append", default=None)
    pull.add_argument("--no-logs", action="store_true")
    pull.add_argument("--rsync-path", default="rsync")
    pull.add_argument("--write", action="store_true", help="execute rsync and write local sync/verify manifests")

    verify = archive_commands.add_parser("verify", help="verify local archive structure and write inventory.latest.json")
    verify.add_argument("--remote", default="prod")
    verify.add_argument("--archive-root", default=None)

    prune = archive_commands.add_parser("prune-remote", help="guarded remote output-run cleanup after local archive verification")
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
    if args.research_command == "storage-cleanup-preview":
        from src.application.research.historical_cleanup import build_historical_cleanup_preview

        data = build_historical_cleanup_preview(
            repo_root=repo_base_fn(), runtime_root=args.runtime_root,
            ledger_sqlite=args.ledger_sqlite, lifecycle_inventory=args.lifecycle_inventory,
            quality_cutover_evidence=args.quality_cutover_evidence, backup_proof=args.backup_proof,
            history_reports=args.history_reports, allow_external_ledger=bool(args.allow_external_ledger),
        )
        return build_response(tool_name="research.storage-cleanup-preview", ok=True, data=data)
    if args.research_command == "storage-gc-preview":
        from src.application.research.storage_baseline import preview_scan_blob_gc

        data = preview_scan_blob_gc(runtime_root=args.runtime_root)
        return build_response(tool_name="research.storage-gc-preview", ok=True, data=data)
    if args.research_command == "storage-baseline":
        from src.application.research.storage_baseline import collect_storage_runtime_baseline

        data = collect_storage_runtime_baseline(
            repo_root=repo_base_fn(), runtime_root=args.runtime_root, ledger_sqlite=args.ledger_sqlite,
            history_reports=args.history_reports, output=args.output,
            allow_external_ledger=bool(args.allow_external_ledger), overwrite=bool(args.overwrite),
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
        return build_response(tool_name="research.handoff", ok=True, data={"handoff_markdown": render_research_handoff(bundle)})
    if args.research_command == "archive":
        from src.application.research.archive import archive_inventory, archive_prune_remote, archive_pull, archive_verify

        base = repo_base_fn()
        if args.archive_command == "inventory":
            data = archive_inventory(repo_root=base, remote=args.remote, archive_root=args.archive_root)
        elif args.archive_command == "pull":
            data = archive_pull(
                repo_root=base, remote=args.remote, archive_root=args.archive_root,
                source_root=args.source_root, ssh_target=args.ssh_target,
                remote_runtime_root=args.remote_runtime_root, since_days=args.since_days,
                run_ids=args.run_ids, include_logs=not bool(args.no_logs),
                write=bool(args.write), rsync_path=args.rsync_path,
            )
        elif args.archive_command == "verify":
            data = archive_verify(repo_root=base, remote=args.remote, archive_root=args.archive_root)
        elif args.archive_command == "prune-remote":
            data = archive_prune_remote(
                repo_root=base, remote=args.remote, archive_root=args.archive_root,
                ssh_target=args.ssh_target, remote_repo_root=args.remote_repo_root,
                remote_runtime_root=args.remote_runtime_root, keep_days=args.keep_days,
                keep_count=args.keep_count, include_logs=not bool(args.no_logs),
                confirm=bool(args.confirm),
            )
        else:
            raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research archive command: {args.archive_command}")
        return build_response(tool_name=f"research.archive.{args.archive_command}", ok=bool(data.get("ok")), data=data)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research command: {args.research_command}")
