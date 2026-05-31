from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.tool_execution import execute_tool


def add_research_commands(subparsers: Any) -> argparse.ArgumentParser:
    research = subparsers.add_parser("research", help="collect Research evidence for MacBook Codex")
    research_sub = research.add_subparsers(dest="research_command", required=True)
    research_collect = research_sub.add_parser("collect", help="collect redacted evidence bundle")
    research_collect.add_argument("--scope", default="full", choices=("ledger", "candidate", "quality", "full"))
    research_collect.add_argument("--config-key", default=None, choices=("us", "hk"))
    research_collect.add_argument("--config-path", default=None)
    research_collect.add_argument("--accounts", nargs="*", default=None)
    research_collect.add_argument("--profile-path", default=None)
    research_collect.add_argument("--report-dir", default=None)
    research_collect.add_argument("--state-dir", default=None)
    research_collect.add_argument("--shared-state-dir", default=None)
    research_collect.add_argument("--accounts-root", default=None)
    research_collect.add_argument("--runs-root", default=None)
    research_collect.add_argument("--run-id", default=None)
    research_collect.add_argument("--run-dir", default=None)
    research_collect.add_argument("--runs-limit", type=int, default=None)
    research_collect.add_argument("--tail-limit", type=int, default=None)
    research_collect.add_argument("--max-run-age-minutes", type=int, default=None)
    research_collect.add_argument("--max-notification-chars", type=int, default=None)
    research_collect.add_argument("--output", default="handoff", choices=("handoff", "json", "both", "markdown", "md"))
    research_collect.add_argument("--scheduler-evidence-json", default=None)
    research_collect.add_argument("--scheduler-evidence-file", default=None)
    research_collect.add_argument("--candidate-path", action="append", dest="candidate_paths", default=None)
    research_collect.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
    research_collect.add_argument("--reject-log-path", action="append", dest="reject_log_paths", default=None)
    research_collect.add_argument("--mark-path", action="append", dest="mark_paths", default=None)
    research_collect.add_argument("--outcome-path", action="append", dest="outcome_paths", default=None)
    research_collect.add_argument("--candidate-report-dir", default=None)
    research_collect.add_argument(
        "--ranking-limit",
        type=int,
        default=None,
        help="top candidate rows per report included in ranking evidence",
    )
    research_collect.add_argument(
        "--shadow-replay-min-sample",
        type=int,
        default=None,
        help="minimum candidate universe sample for offline shadow replay readiness",
    )
    research_collect.add_argument("--include-healthcheck", action="store_true")
    research_collect.add_argument("--data-config", default=None)
    research_collect.add_argument("--timeout-sec", type=int, default=None)
    research_collect.add_argument("--output-dir", default=None)
    research_collect.add_argument("--current-dir", default=None)
    research_collect.add_argument("--write-outputs", action="store_true")
    research_collect.add_argument("--no-write-outputs", action="store_true")
    research_collect.add_argument("--confirm", action="store_true")
    research_handoff = research_sub.add_parser("handoff", help="render handoff from a collected bundle")
    research_handoff.add_argument("--bundle", required=True)
    research_shadow = research_sub.add_parser("shadow-replay", help="build or analyze offline shadow replay datasets")
    research_shadow_sub = research_shadow.add_subparsers(dest="shadow_replay_command", required=True)
    shadow_build = research_shadow_sub.add_parser(
        "build",
        help="build a local shadow replay dataset from existing artifacts",
    )
    shadow_build.add_argument("--run-id", default=None)
    shadow_build.add_argument("--run-dir", default=None)
    shadow_build.add_argument("--report-dir", default=None)
    shadow_build.add_argument("--candidate-path", action="append", dest="candidate_paths", default=None)
    shadow_build.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
    shadow_build.add_argument("--reject-log-path", action="append", dest="reject_log_paths", default=None)
    shadow_build.add_argument("--mark-path", action="append", dest="mark_paths", default=None)
    shadow_build.add_argument("--outcome-path", action="append", dest="outcome_paths", default=None)
    shadow_build.add_argument("--output-dir", default=None)
    shadow_build.add_argument("--dataset-id", default=None)
    shadow_analyze = research_shadow_sub.add_parser("analyze", help="analyze a local shadow replay dataset")
    shadow_analyze.add_argument("--dataset", required=True)
    shadow_analyze.add_argument("--min-sample", type=int, default=30)
    shadow_analyze.add_argument("--output", default=None)
    for command_name, help_text in (
        ("status", "summarize local shadow replay dataset readiness"),
        ("list", "list local shadow replay datasets and next actions"),
    ):
        shadow_status = research_shadow_sub.add_parser(command_name, help=help_text)
        shadow_status.add_argument(
            "--dataset-root",
            default=None,
            help="dataset root; default output_shared/research/shadow_replay/datasets",
        )
        shadow_status.add_argument("--min-sample", type=int, default=30)
        shadow_status.add_argument(
            "--min-mark-points",
            type=int,
            default=2,
            help="minimum distinct usable mark timestamps before settlement is preferred",
        )
        shadow_status.add_argument(
            "--mark-stale-hours",
            type=int,
            default=24,
            help="age threshold used to flag stale replay marks",
        )
    shadow_plan = research_shadow_sub.add_parser(
        "run-data-plan",
        help="dry-run or execute local shadow replay data-maintenance actions",
    )
    shadow_plan.add_argument(
        "--dataset-root",
        default=None,
        help="dataset root; default output_shared/research/shadow_replay/datasets",
    )
    shadow_plan.add_argument(
        "--required-data-root",
        default=None,
        help="required-data root containing raw/ and parsed/; default output_shared/required_data",
    )
    shadow_plan.add_argument("--min-sample", type=int, default=30)
    shadow_plan.add_argument("--min-mark-points", type=int, default=2)
    shadow_plan.add_argument("--mark-stale-hours", type=int, default=24)
    shadow_plan.add_argument(
        "--action",
        dest="actions",
        action="append",
        choices=("collect_marks", "settle"),
        default=None,
        help="enabled data maintenance action; repeatable; default collect_marks + settle",
    )
    shadow_plan.add_argument("--max-datasets", type=int, default=None)
    shadow_plan.add_argument(
        "--source",
        default="local",
        choices=("local", "opend"),
        help="source for collect_marks actions; opend is explicit and may refresh local required-data cache with --write",
    )
    shadow_plan.add_argument(
        "--write",
        action="store_true",
        help="execute eligible data-maintenance actions and write a local receipt",
    )
    shadow_plan.add_argument(
        "--receipt-output",
        default=None,
        help="explicit receipt JSON path; requires --write",
    )
    shadow_plan.add_argument(
        "--receipt-dir",
        default=None,
        help="receipt directory when --write is used; default output_shared/research/shadow_replay/receipts",
    )
    shadow_plan.add_argument(
        "--settle-after-collect",
        action="store_true",
        help="derive outcome_facts after a successful collect_marks write",
    )
    shadow_plan.add_argument("--opend-host", default="127.0.0.1")
    shadow_plan.add_argument("--opend-port", type=int, default=11111)
    shadow_plan.add_argument("--limit-expirations", type=int, default=8)
    shadow_plan.add_argument("--max-symbols", type=int, default=None)
    shadow_plan.add_argument("--no-chain-cache", action="store_true")
    shadow_plan.add_argument("--chain-cache-force-refresh", action="store_true")
    shadow_plan.add_argument("--include-realized-volatility", action="store_true")
    shadow_mark = research_shadow_sub.add_parser(
        "mark",
        help="generate local mark path snapshots from required-data CSV quotes",
    )
    shadow_mark.add_argument("--dataset", required=True)
    shadow_mark.add_argument(
        "--required-data-root",
        default=None,
        help="required-data root containing parsed/*_required_data.csv; default output_shared/required_data",
    )
    shadow_mark.add_argument("--as-of", default=None, help="mark timestamp label; default current UTC time")
    shadow_mark.add_argument("--output", default=None)
    shadow_mark.add_argument(
        "--write",
        action="store_true",
        help="write generated mark_path_snapshots.jsonl back to the local dataset",
    )
    shadow_mark.add_argument(
        "--replace",
        action="store_true",
        help="replace existing local mark path snapshots when used with --write",
    )
    shadow_collect = research_shadow_sub.add_parser(
        "collect-marks",
        help="collect one replay mark sample from local cache or OpenD",
    )
    shadow_collect.add_argument("--dataset", required=True)
    shadow_collect.add_argument(
        "--source",
        default="local",
        choices=("local", "opend"),
        help="local reads required-data cache; opend fetches current quotes before marking",
    )
    shadow_collect.add_argument(
        "--required-data-root",
        default=None,
        help="required-data root containing raw/ and parsed/; default output_shared/required_data",
    )
    shadow_collect.add_argument("--as-of", default=None, help="mark timestamp label; default current UTC time")
    shadow_collect.add_argument("--output", default=None)
    shadow_collect.add_argument(
        "--write",
        action="store_true",
        help="persist generated mark snapshots; with --source opend also persist required-data/cache state",
    )
    shadow_collect.add_argument(
        "--replace",
        action="store_true",
        help="replace existing local mark path snapshots when used with --write",
    )
    shadow_collect.add_argument("--settle", action="store_true", help="derive outcome_facts after writing marks")
    shadow_collect.add_argument("--opend-host", default="127.0.0.1")
    shadow_collect.add_argument("--opend-port", type=int, default=11111)
    shadow_collect.add_argument("--limit-expirations", type=int, default=8)
    shadow_collect.add_argument("--max-symbols", type=int, default=None)
    shadow_collect.add_argument("--no-chain-cache", action="store_true")
    shadow_collect.add_argument("--chain-cache-force-refresh", action="store_true")
    shadow_collect.add_argument("--include-realized-volatility", action="store_true")
    shadow_settle = research_shadow_sub.add_parser(
        "settle",
        help="derive outcome facts from a local shadow replay dataset",
    )
    shadow_settle.add_argument("--dataset", required=True)
    shadow_settle.add_argument("--output", default=None)
    shadow_settle.add_argument(
        "--write",
        action="store_true",
        help="write derived outcome_facts.jsonl back to the local dataset",
    )
    shadow_settle.add_argument(
        "--replace",
        action="store_true",
        help="replace existing local outcome facts when used with --write",
    )
    return research


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
        "candidate_paths": args.candidate_paths,
        "trace_paths": args.trace_paths,
        "reject_log_paths": args.reject_log_paths,
        "mark_paths": args.mark_paths,
        "outcome_paths": args.outcome_paths,
        "candidate_report_dir": args.candidate_report_dir,
        "ranking_limit": args.ranking_limit,
        "shadow_replay_min_sample": args.shadow_replay_min_sample,
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
    scheduler_evidence = _load_scheduler_evidence(
        json_text=args.scheduler_evidence_json,
        file_path=args.scheduler_evidence_file,
    )
    if scheduler_evidence is not None:
        payload["scheduler_evidence"] = scheduler_evidence
    return {key: value for key, value in payload.items() if value not in (None, [])}


def handle_research_command(
    args: argparse.Namespace,
    *,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]] = execute_tool,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> dict[str, Any]:
    if args.research_command == "collect":
        return execute_tool_fn("research", _research_collect_payload(args))

    if args.research_command == "handoff":
        from src.application.research.service import render_research_handoff

        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise AgentToolError(code="INPUT_ERROR", message="research bundle must be a JSON object")
        return build_response(
            tool_name="research.handoff",
            ok=True,
            data={"handoff_markdown": render_research_handoff(bundle)},
        )

    if args.research_command != "shadow-replay":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research command: {args.research_command}")

    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        collect_shadow_replay_marks,
        mark_shadow_replay_dataset,
        run_shadow_replay_data_plan,
        settle_shadow_replay_dataset,
        shadow_replay_dataset_status,
    )

    base = repo_base_fn()

    if args.shadow_replay_command == "build":
        data = build_shadow_replay_dataset(
            repo_root=base,
            run_id=args.run_id,
            run_dir=args.run_dir,
            report_dir=args.report_dir,
            candidate_paths=args.candidate_paths,
            trace_paths=args.trace_paths,
            reject_log_paths=args.reject_log_paths,
            mark_paths=args.mark_paths,
            outcome_paths=args.outcome_paths,
            output_dir=args.output_dir,
            dataset_id=args.dataset_id,
        )
        return build_response(tool_name="research.shadow-replay.build", ok=True, data=data)

    if args.shadow_replay_command == "analyze":
        data = analyze_shadow_replay_dataset(dataset=args.dataset, min_sample=args.min_sample, output=args.output)
        return build_response(tool_name="research.shadow-replay.analyze", ok=True, data=data)

    if args.shadow_replay_command in {"status", "list"}:
        data = shadow_replay_dataset_status(
            repo_root=base,
            dataset_root=args.dataset_root,
            min_sample=args.min_sample,
            min_mark_points=args.min_mark_points,
            mark_stale_hours=args.mark_stale_hours,
        )
        return build_response(tool_name=f"research.shadow-replay.{args.shadow_replay_command}", ok=True, data=data)

    if args.shadow_replay_command == "run-data-plan":
        if not bool(args.write) and (args.receipt_output or args.receipt_dir):
            raise AgentToolError(
                code="INPUT_ERROR",
                message="--receipt-output and --receipt-dir require --write for shadow-replay run-data-plan",
            )
        data = run_shadow_replay_data_plan(
            repo_root=base,
            dataset_root=args.dataset_root,
            required_data_root=args.required_data_root,
            source=args.source,
            min_sample=args.min_sample,
            min_mark_points=args.min_mark_points,
            mark_stale_hours=args.mark_stale_hours,
            actions=args.actions,
            max_datasets=args.max_datasets,
            write=bool(args.write),
            receipt_output=args.receipt_output,
            receipt_dir=args.receipt_dir,
            settle_after_collect=bool(args.settle_after_collect),
            opend_host=args.opend_host,
            opend_port=args.opend_port,
            limit_expirations=args.limit_expirations,
            chain_cache=not bool(args.no_chain_cache),
            chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
            include_realized_volatility=bool(args.include_realized_volatility),
            max_symbols=args.max_symbols,
        )
        return build_response(tool_name="research.shadow-replay.run-data-plan", ok=True, data=data)

    if args.shadow_replay_command == "mark":
        required_data_root = args.required_data_root or (base / "output_shared" / "required_data")
        data = mark_shadow_replay_dataset(
            dataset=args.dataset,
            required_data_root=required_data_root,
            as_of=args.as_of,
            repo_root=base,
            output=args.output,
            write=bool(args.write),
            replace=bool(args.replace),
        )
        return build_response(tool_name="research.shadow-replay.mark", ok=True, data=data)

    if args.shadow_replay_command == "collect-marks":
        required_data_root = args.required_data_root or (base / "output_shared" / "required_data")
        data = collect_shadow_replay_marks(
            dataset=args.dataset,
            required_data_root=required_data_root,
            source=args.source,
            repo_root=base,
            as_of=args.as_of,
            output=args.output,
            write=bool(args.write),
            replace=bool(args.replace),
            settle=bool(args.settle),
            opend_host=args.opend_host,
            opend_port=args.opend_port,
            limit_expirations=args.limit_expirations,
            chain_cache=not bool(args.no_chain_cache),
            chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
            include_realized_volatility=bool(args.include_realized_volatility),
            max_symbols=args.max_symbols,
        )
        return build_response(tool_name="research.shadow-replay.collect-marks", ok=True, data=data)

    if args.shadow_replay_command == "settle":
        data = settle_shadow_replay_dataset(
            dataset=args.dataset,
            output=args.output,
            write=bool(args.write),
            replace=bool(args.replace),
        )
        return build_response(tool_name="research.shadow-replay.settle", ok=True, data=data)

    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"unsupported research shadow-replay command: {args.shadow_replay_command}",
    )
