from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.research.facade import run_research_collect
from src.interfaces.cli.strategy_lab_top1 import add_top1_commands, handle_top1_command


def _add_candidate_impact_args(parser: Any) -> None:
    parser.add_argument("--params", required=True, help="parameter variant JSON file")
    parser.add_argument("--dataset", default=None, help="existing shadow replay dataset directory")
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--profile-path", default=None)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD, required for strict date-window checks")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD; defaults to start-date when omitted by caller")
    parser.add_argument("--account", dest="accounts", action="append", default=None)
    parser.add_argument("--market", choices=("hk", "us"), default=None)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument("--format", dest="output_format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", default=None)


def _add_candidate_impact_report_args(parser: Any) -> None:
    parser.add_argument("--params", default=None, help="parameter variant JSON file")
    parser.add_argument(
        "--params-dir",
        default=None,
        help="directory containing params.<market>.json; used when --params is omitted",
    )
    parser.add_argument("--dataset", default=None, help="existing shadow replay dataset directory")
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--profile-path", default=None)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD, required for strict date-window checks")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD; defaults to start-date when omitted by caller")
    parser.add_argument("--account", dest="accounts", action="append", default=None)
    parser.add_argument("--market", choices=("hk", "us"), required=True)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--report-id", default=None)


def add_research_commands(subparsers: Any) -> argparse.ArgumentParser:
    research = subparsers.add_parser("research", help="run offline Research evidence and Shadow Replay readiness workflows")
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
    research_collect.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
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
    storage_baseline = research_sub.add_parser(
        "storage-baseline",
        help="collect a payload-free read-only runtime storage and capacity baseline",
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
    storage_gc = research_sub.add_parser(
        "storage-gc-preview",
        help="preview reachable and orphaned canonical scan blobs without deleting",
    )
    storage_gc.add_argument("--runtime-root", required=True)
    historical_cleanup = research_sub.add_parser(
        "storage-cleanup-preview",
        help="preview gated historical cleanup without moving or deleting data",
    )
    historical_cleanup.add_argument("--runtime-root", required=True)
    historical_cleanup.add_argument("--ledger-sqlite", default=None)
    historical_cleanup.add_argument("--lifecycle-inventory", required=True)
    historical_cleanup.add_argument("--quality-cutover-evidence", default=None)
    historical_cleanup.add_argument("--backup-proof", default=None)
    historical_cleanup.add_argument(
        "--history-report",
        dest="history_reports",
        action="append",
        default=None,
    )
    historical_cleanup.add_argument("--allow-external-ledger", action="store_true")
    research_handoff = research_sub.add_parser("handoff", help="render handoff from a collected bundle")
    research_handoff.add_argument("--bundle", required=True)
    research_archive = research_sub.add_parser("archive", help="mirror remote Research evidence for local replay")
    research_archive_sub = research_archive.add_subparsers(dest="archive_command", required=True)

    archive_inventory = research_archive_sub.add_parser("inventory", help="inspect the local remote-evidence archive")
    archive_inventory.add_argument("--remote", default="prod")
    archive_inventory.add_argument("--archive-root", default=None)

    archive_pull = research_archive_sub.add_parser("pull", help="dry-run or rsync remote runtime evidence into local archive")
    archive_pull.add_argument("--remote", default="prod")
    archive_pull.add_argument("--archive-root", default=None)
    archive_pull.add_argument("--source-root", default=None, help="local or mounted runtime root; mutually exclusive with --ssh-target")
    archive_pull.add_argument("--ssh-target", default=None, help="ssh target such as deploy@host")
    archive_pull.add_argument("--remote-runtime-root", default="/var/lib/options-monitor")
    archive_pull.add_argument("--since-days", type=int, default=None)
    archive_pull.add_argument("--run-id", dest="run_ids", action="append", default=None)
    archive_pull.add_argument(
        "--require-replay-evidence",
        action="store_true",
        help="auto-select only source runs with candidate snapshot/status or trace evidence",
    )
    archive_pull.add_argument("--no-logs", action="store_true")
    archive_pull.add_argument("--rsync-path", default="rsync")
    archive_pull.add_argument("--write", action="store_true", help="execute rsync and write local sync/verify manifests")

    archive_verify = research_archive_sub.add_parser("verify", help="verify local archive structure and write inventory.latest.json")
    archive_verify.add_argument("--remote", default="prod")
    archive_verify.add_argument("--archive-root", default=None)

    archive_build = research_archive_sub.add_parser(
        "build-datasets",
        help="build local shadow replay datasets from verified archived runs",
    )
    archive_build.add_argument("--remote", default="prod")
    archive_build.add_argument("--archive-root", default=None)
    archive_build.add_argument("--dataset-root", default=None)
    archive_build.add_argument("--market", choices=("us", "hk"), default=None)
    archive_build.add_argument("--run-id", dest="run_ids", action="append", default=None)
    archive_build.add_argument("--latest-scanned", action="store_true")
    archive_build.add_argument(
        "--no-mark-from-run-required-data",
        action="store_true",
        help="do not generate initial mark_path_snapshots from archived run required_data/parsed",
    )
    archive_build.add_argument("--write", action="store_true")

    archive_prune = research_archive_sub.add_parser(
        "prune-remote",
        help="guarded remote cleanup after local archive verification",
    )
    archive_prune.add_argument("--remote", default="prod")
    archive_prune.add_argument("--archive-root", default=None)
    archive_prune.add_argument("--ssh-target", required=True)
    archive_prune.add_argument("--remote-repo-root", default="/opt/options-monitor/current")
    archive_prune.add_argument("--remote-runtime-root", default="/var/lib/options-monitor")
    archive_prune.add_argument("--keep-days", type=int, default=3)
    archive_prune.add_argument("--keep-count", type=int, default=30)
    archive_prune.add_argument(
        "--scope",
        choices=("output-runs", "shadow-replay-receipts"),
        default="output-runs",
    )
    archive_prune.add_argument("--no-logs", action="store_true")
    archive_prune.add_argument("--confirm", action="store_true")
    research_strategy_lab = research_sub.add_parser(
        "strategy-lab",
        help="run offline Strategy Lab readiness and experiment workflows",
    )
    research_strategy_lab_sub = research_strategy_lab.add_subparsers(dest="strategy_lab_command", required=True)
    add_top1_commands(research_strategy_lab_sub)
    strategy_lab_update = research_strategy_lab_sub.add_parser(
        "update",
        help="dry-run or execute Strategy Lab evidence lifecycle maintenance",
    )
    strategy_lab_update.add_argument("--latest", action="store_true")
    strategy_lab_update.add_argument(
        "--build-dataset",
        action="store_true",
        help="build a local replay dataset from the latest scanned run before running the data plan; writes only with --write",
    )
    strategy_lab_update.add_argument(
        "--include-close-decisions",
        action="store_true",
        help="also build a dataset from the latest non-empty Close Advice run; requires --build-dataset and --write to persist",
    )
    strategy_lab_update.add_argument(
        "--runs-root",
        default=None,
        help="runs root for --build-dataset; defaults to profile/runtime output_runs",
    )
    strategy_lab_update.add_argument("--dataset-root", default=None)
    strategy_lab_update.add_argument("--dataset-id", default=None)
    strategy_lab_update.add_argument("--profile-path", default=None)
    strategy_lab_update.add_argument("--runtime-root", default=None)
    strategy_lab_update.add_argument("--required-data-root", default=None)
    strategy_lab_update.add_argument("--min-sample", type=int, default=30)
    strategy_lab_update.add_argument("--min-mark-points", type=int, default=2)
    strategy_lab_update.add_argument("--mark-stale-hours", type=int, default=24)
    strategy_lab_update.add_argument(
        "--action",
        dest="actions",
        action="append",
        choices=("collect_marks", "settle"),
        default=None,
        help="enabled data maintenance action; repeatable; default collect_marks + settle",
    )
    strategy_lab_update.add_argument("--max-datasets", type=int, default=None)
    strategy_lab_update.add_argument("--source", default="local", choices=("local", "opend"))
    strategy_lab_update.add_argument("--write", action="store_true")
    strategy_lab_update.add_argument("--output", default=None)
    strategy_lab_update.add_argument("--receipt-output", default=None)
    strategy_lab_update.add_argument("--receipt-dir", default=None)
    strategy_lab_update.add_argument("--settle-after-collect", action="store_true")
    strategy_lab_update.add_argument("--opend-host", default="127.0.0.1")
    strategy_lab_update.add_argument("--opend-port", type=int, default=11111)
    strategy_lab_update.add_argument("--limit-expirations", type=int, default=8)
    strategy_lab_update.add_argument("--max-symbols", type=int, default=None)
    strategy_lab_update.add_argument("--no-chain-cache", action="store_true")
    strategy_lab_update.add_argument("--chain-cache-force-refresh", action="store_true")
    strategy_lab_update.add_argument("--include-realized-volatility", action="store_true")
    strategy_lab_readiness = research_strategy_lab_sub.add_parser(
        "readiness",
        help="analyze Strategy Lab decision-instance readiness for a replay dataset",
    )
    strategy_lab_readiness.add_argument("--dataset", default=None)
    strategy_lab_readiness.add_argument("--runs-root", default=None)
    strategy_lab_readiness.add_argument("--profile-path", default=None)
    strategy_lab_readiness.add_argument("--runtime-root", default=None)
    strategy_lab_readiness.add_argument("--start-date", default=None)
    strategy_lab_readiness.add_argument("--end-date", default=None)
    strategy_lab_readiness.add_argument("--account", dest="accounts", action="append", default=None)
    strategy_lab_readiness.add_argument("--market", choices=("hk", "us"), default=None)
    strategy_lab_readiness.add_argument("--min-sample", type=int, default=30)
    strategy_lab_readiness.add_argument("--output", default=None)
    strategy_lab_experiment = research_strategy_lab_sub.add_parser(
        "experiment",
        help="run a read-only Strategy Lab hypothesis and candidate-impact experiment",
    )
    strategy_lab_experiment.add_argument("--dataset", default=None)
    strategy_lab_experiment.add_argument("--runs-root", default=None)
    strategy_lab_experiment.add_argument("--profile-path", default=None)
    strategy_lab_experiment.add_argument("--runtime-root", default=None)
    strategy_lab_experiment.add_argument("--start-date", default=None)
    strategy_lab_experiment.add_argument("--end-date", default=None)
    strategy_lab_experiment.add_argument("--account", dest="accounts", action="append", default=None)
    strategy_lab_experiment.add_argument("--market", choices=("hk", "us"), default=None)
    strategy_lab_experiment.add_argument("--min-sample", type=int, default=30)
    strategy_lab_experiment.add_argument("--auto", action="store_true")
    strategy_lab_experiment.add_argument("--output", default=None)
    strategy_lab_proposal = research_strategy_lab_sub.add_parser(
        "proposal",
        help="build an advisory-only Strategy Lab dry-run proposal from an experiment",
    )
    strategy_lab_proposal.add_argument("--experiment", required=True)
    strategy_lab_proposal.add_argument("--output", default=None)
    strategy_lab_proposal.add_argument("--markdown-output", default=None)
    strategy_lab_llm_context = research_strategy_lab_sub.add_parser(
        "llm-context",
        help="build redacted local LLM context from Strategy Lab artifacts without calling online AI",
    )
    strategy_lab_llm_context.add_argument("--experiment", default=None)
    strategy_lab_llm_context.add_argument("--proposal", default=None)
    strategy_lab_llm_context.add_argument("--output", default=None)
    strategy_lab_llm_context.add_argument("--max-rows", type=int, default=8)
    strategy_lab_llm_context.add_argument("--max-samples", type=int, default=5)
    research_shadow = research_sub.add_parser("shadow-replay", help="build or analyze offline shadow replay datasets")
    research_shadow_sub = research_shadow.add_subparsers(dest="shadow_replay_command", required=True)
    shadow_build = research_shadow_sub.add_parser(
        "build",
        help="build a local shadow replay dataset from existing artifacts",
    )
    shadow_build.add_argument("--run-id", default=None)
    shadow_build.add_argument("--run-dir", default=None)
    shadow_build.add_argument("--runs-root", default=None)
    shadow_build.add_argument("--profile-path", default=None)
    shadow_build.add_argument("--runtime-root", default=None)
    shadow_build.add_argument(
        "--latest-scanned-run",
        action="store_true",
        help="select the newest run under runs-root/profile runtime root that has replay evidence",
    )
    shadow_build.add_argument("--report-dir", default=None)
    shadow_build.add_argument("--trace-path", action="append", dest="trace_paths", default=None)
    shadow_build.add_argument("--mark-path", action="append", dest="mark_paths", default=None)
    shadow_build.add_argument("--outcome-path", action="append", dest="outcome_paths", default=None)
    shadow_build.add_argument(
        "--include-close-decisions",
        action="store_true",
        help="explicitly add the local Close Advice episode/mark/outcome facet",
    )
    shadow_build.add_argument("--output-dir", default=None, help="exact dataset output directory")
    shadow_build.add_argument(
        "--dataset-root",
        default=None,
        help="dataset root; defaults to profile/runtime output_shared/research/shadow_replay/datasets when provided",
    )
    shadow_build.add_argument("--dataset-id", default=None)
    shadow_capture_combo = research_shadow_sub.add_parser(
        "capture-combo-variants",
        help="capture an isolated required-data superset for Combo Yield Shadow variants",
    )
    shadow_capture_combo.add_argument("--config-key", required=True, choices=("us", "hk"))
    shadow_capture_combo.add_argument("--account", required=True)
    shadow_capture_combo.add_argument("--symbols", nargs="+", required=True)
    shadow_capture_combo.add_argument("--variant-spec", required=True)
    shadow_capture_combo.add_argument(
        "--dataset-root",
        default=None,
        help="dataset root; default output_shared/research/shadow_replay/datasets",
    )
    shadow_capture_combo.add_argument("--dataset-id", default=None)
    shadow_capture_combo.add_argument("--opend-host", default=None)
    shadow_capture_combo.add_argument("--opend-port", type=int, default=None)
    shadow_capture_combo.add_argument(
        "--no-chain-cache",
        action="store_true",
        help="disable the local research option-chain rate-limit cache",
    )
    shadow_capture_combo.add_argument(
        "--chain-cache-force-refresh",
        action="store_true",
        help="ignore reusable research chain-cache entries during capture",
    )
    shadow_capture_combo.add_argument(
        "--write",
        action="store_true",
        help="write the isolated local Shadow dataset; otherwise only return the capture plan",
    )
    shadow_evaluate_combo = research_shadow_sub.add_parser(
        "evaluate-combo-variants",
        help="build baseline/proposed Combo pair decisions from an isolated capture",
    )
    shadow_evaluate_combo.add_argument("--dataset", required=True)
    shadow_evaluate_combo.add_argument(
        "--funding-put-path",
        default=None,
        help="manifest-bound Combo Funding Put decision JSONL",
    )
    shadow_evaluate_combo.add_argument("--write", action="store_true")
    shadow_prepare_combo = research_shadow_sub.add_parser(
        "prepare-combo-funding-puts",
        help="project manifest-bound Combo Funding Put decisions into a capture",
    )
    shadow_prepare_combo.add_argument("--dataset", required=True)
    shadow_prepare_combo.add_argument("--source-run-id", required=True)
    shadow_prepare_combo.add_argument("--runs-root", default=None)
    shadow_prepare_combo.add_argument("--profile-path", default=None)
    shadow_prepare_combo.add_argument("--runtime-root", default=None)
    shadow_prepare_combo.add_argument("--write", action="store_true")
    shadow_analyze = research_shadow_sub.add_parser("analyze", help="analyze a local shadow replay dataset")
    shadow_analyze.add_argument("--dataset", required=True)
    shadow_analyze.add_argument("--min-sample", type=int, default=30)
    shadow_analyze.add_argument("--output", default=None)
    shadow_impact = research_shadow_sub.add_parser(
        "candidate-impact",
        help="compare observed candidate impact for explicit threshold variants",
    )
    _add_candidate_impact_args(shadow_impact)
    shadow_impact_report = research_shadow_sub.add_parser(
        "candidate-impact-report",
        help="write paired JSON and Markdown candidate-impact reports",
    )
    _add_candidate_impact_report_args(shadow_impact_report)
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
        shadow_status.add_argument("--profile-path", default=None)
        shadow_status.add_argument("--runtime-root", default=None)
        shadow_status.add_argument(
            "--required-data-root",
            default=None,
            help="required-data root used in suggested commands",
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
    shadow_plan.add_argument("--profile-path", default=None)
    shadow_plan.add_argument("--runtime-root", default=None)
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
    shadow_mark.add_argument("--profile-path", default=None)
    shadow_mark.add_argument("--runtime-root", default=None)
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
    shadow_collect.add_argument("--profile-path", default=None)
    shadow_collect.add_argument("--runtime-root", default=None)
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
        "--lifecycle-path",
        action="append",
        dest="lifecycle_paths",
        default=None,
        help="canonical ledger lifecycle JSON/JSONL/CSV evidence; repeatable",
    )
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
        "trace_paths": args.trace_paths,
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


def _shadow_replay_profile(args: argparse.Namespace, *, base: Path) -> dict[str, Any]:
    raw = str(getattr(args, "profile_path", "") or "").strip()
    if not raw:
        return {}
    path = _resolve_shadow_path(raw, base=base)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile must be a JSON object: {path}")
    return payload


def _resolve_shadow_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def _profile_paths(profile: dict[str, Any]) -> dict[str, Any]:
    paths = profile.get("paths")
    return paths if isinstance(paths, dict) else {}


def _profile_path_value(profile: dict[str, Any], key: str, *, base: Path) -> Path | None:
    raw = _profile_paths(profile).get(key)
    if raw is None or not str(raw).strip():
        return None
    return _resolve_shadow_path(str(raw), base=base)


def _shadow_replay_runtime_root(args: argparse.Namespace, *, profile: dict[str, Any], base: Path) -> Path | None:
    raw = str(getattr(args, "runtime_root", "") or "").strip() or str(profile.get("runtime_root") or "").strip()
    if raw:
        return _resolve_shadow_path(raw, base=base)
    runs_root = _profile_path_value(profile, "runs_root", base=base)
    if runs_root is not None and runs_root.name == "output_runs":
        return runs_root.parent.resolve()
    for key in ("report_dir", "state_dir", "shared_state_dir"):
        path = _profile_path_value(profile, key, base=base)
        if path is not None and path.parent.name == "output_shared":
            return path.parent.parent.resolve()
    return None


def _shadow_replay_opend_fetch_config(
    *,
    profile: dict[str, Any],
    base: Path,
) -> dict[str, float | int] | None:
    raw_paths = profile.get("config_paths")
    if raw_paths is None:
        return None
    if not isinstance(raw_paths, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="profile config_paths must be a JSON object")
    if not raw_paths:
        return None

    from src.application.opend_fetch_config import opend_fetch_kwargs

    resolved: list[dict[str, float | int]] = []
    for config_key, raw_path in sorted(raw_paths.items()):
        if not str(raw_path or "").strip():
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"profile config path is empty: {config_key}",
            )
        path = _resolve_shadow_path(str(raw_path), base=base)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AgentToolError(code="CONFIG_ERROR", message=f"profile config not found: {path}") from exc
        except OSError as exc:
            raise AgentToolError(code="CONFIG_ERROR", message=f"profile config unreadable: {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AgentToolError(code="CONFIG_ERROR", message=f"profile config is not valid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"profile config must be a JSON object: {path}")
        resolved.append(opend_fetch_kwargs(payload))

    return _conservative_opend_fetch_config(resolved)


def _conservative_opend_fetch_config(
    configs: list[dict[str, float | int]],
) -> dict[str, float | int] | None:
    if not configs:
        return None
    merged: dict[str, float | int] = {}
    for key in configs[0]:
        values = [config[key] for config in configs if key in config]
        if not values:
            continue
        if key.endswith("max_calls"):
            merged[key] = min(int(value) for value in values)
        elif key.endswith("window_sec"):
            merged[key] = max(float(value) for value in values)
        elif key == "max_wait_sec" or key.endswith("max_wait_sec"):
            merged[key] = min(float(value) for value in values)
        else:
            merged[key] = values[0]
    return merged


def _shadow_replay_runs_root(
    args: argparse.Namespace,
    *,
    profile: dict[str, Any],
    runtime_root: Path | None,
    base: Path,
) -> Path | None:
    raw = str(getattr(args, "runs_root", "") or "").strip()
    if raw:
        return _resolve_shadow_path(raw, base=base)
    profile_root = _profile_path_value(profile, "runs_root", base=base)
    if profile_root is not None:
        return profile_root
    if runtime_root is not None:
        return (runtime_root / "output_runs").resolve()
    return None


def _shadow_replay_dataset_root(
    raw_value: str | Path | None,
    *,
    runtime_root: Path | None,
    base: Path,
) -> Path | None:
    if raw_value is not None and str(raw_value).strip():
        return _resolve_shadow_path(raw_value, base=base)
    if runtime_root is not None:
        return (runtime_root / "output_shared" / "research" / "shadow_replay" / "datasets").resolve()
    return None


def _shadow_replay_required_data_root(
    raw_value: str | Path | None,
    *,
    runtime_root: Path | None,
    base: Path,
) -> Path | None:
    if raw_value is not None and str(raw_value).strip():
        return _resolve_shadow_path(raw_value, base=base)
    if runtime_root is not None:
        return (runtime_root / "output_shared" / "required_data").resolve()
    return None


def _shadow_replay_receipt_dir(
    raw_value: str | Path | None,
    *,
    runtime_root: Path | None,
    base: Path,
) -> Path | None:
    if raw_value is not None and str(raw_value).strip():
        return _resolve_shadow_path(raw_value, base=base)
    if runtime_root is not None:
        return (runtime_root / "output_shared" / "research" / "shadow_replay" / "receipts").resolve()
    return None


def _shadow_replay_backtest_root(
    raw_value: str | Path | None,
    *,
    runtime_root: Path | None,
    base: Path,
) -> Path:
    if raw_value is not None and str(raw_value).strip():
        return _resolve_shadow_path(raw_value, base=base)
    if runtime_root is not None:
        return (runtime_root / "output_shared" / "research" / "shadow_replay" / "backtests").resolve()
    return (base / "output_shared" / "research" / "shadow_replay" / "backtests").resolve()


def _has_strategy_lab_input_scope(args: argparse.Namespace) -> bool:
    return any(
        (
            bool(str(getattr(args, "dataset", "") or "").strip()),
            bool(str(getattr(args, "runs_root", "") or "").strip()),
            bool(str(getattr(args, "start_date", "") or "").strip()),
            bool(str(getattr(args, "end_date", "") or "").strip()),
            bool(getattr(args, "accounts", None)),
            bool(str(getattr(args, "market", "") or "").strip()),
        )
    )


def _candidate_impact_report_id(args: argparse.Namespace) -> str:
    raw = str(getattr(args, "report_id", "") or "").strip()
    if raw:
        return raw
    prefix = "candidate-impact-report"
    market = str(args.market or "all").lower()
    start = str(getattr(args, "start_date", "") or "").strip() or "dataset"
    end = str(getattr(args, "end_date", "") or "").strip() or start
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{market}-{start}-to-{end}-{stamp}"


def _candidate_impact_report_params_path(args: argparse.Namespace, *, runtime_root: Path | None, base: Path) -> Path:
    if getattr(args, "params", None):
        path = _resolve_shadow_path(args.params, base=base)
    elif getattr(args, "params_dir", None):
        path = _resolve_shadow_path(args.params_dir, base=base) / f"params.{args.market}.json"
    elif runtime_root is not None:
        path = (
            runtime_root
            / "output_shared"
            / "research"
            / "shadow_replay"
            / "backtests"
            / f"params.{args.market}.json"
        ).resolve()
    else:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=(
                "candidate-impact-report requires --params or --params-dir "
                "when no runtime-root default params file is available"
            ),
        )
    if not path.exists():
        raise AgentToolError(code="INPUT_ERROR", message=f"candidate-impact params file not found: {path}")
    return path


def _candidate_impact_report_summary(result: dict[str, Any]) -> dict[str, Any]:
    coverage = result.get("coverage") or {}
    return {
        "data_mode": result.get("data_mode"),
        "selected_run_ids": coverage.get("selected_run_ids"),
        "summary": result.get("summary"),
        "gates": result.get("gates"),
        "candidate_impact": result.get("candidate_impact"),
        "recommendation": result.get("recommendation"),
        "safety": result.get("safety"),
    }


ResearchCollectFn = Callable[[dict[str, Any]], dict[str, Any]]


def handle_research_command(
    args: argparse.Namespace,
    *,
    research_collect_fn: ResearchCollectFn | None = None,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> dict[str, Any]:
    if args.research_command == "storage-cleanup-preview":
        from src.application.research.historical_cleanup import (
            build_historical_cleanup_preview,
        )

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
        return build_response(
            tool_name="research.storage-cleanup-preview",
            ok=True,
            data=data,
        )

    if args.research_command == "storage-gc-preview":
        from src.application.research.storage_baseline import preview_scan_blob_gc

        data = preview_scan_blob_gc(runtime_root=args.runtime_root)
        return build_response(tool_name="research.storage-gc-preview", ok=True, data=data)

    if args.research_command == "storage-baseline":
        from src.application.research.storage_baseline import (
            collect_storage_runtime_baseline,
        )

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
            tool_name="research.handoff",
            ok=True,
            data={"handoff_markdown": render_research_handoff(bundle)},
        )

    if args.research_command == "archive":
        from src.application.research.archive import (
            archive_build_datasets,
            archive_inventory,
            archive_prune_remote,
            archive_pull,
            archive_verify,
        )

        base = repo_base_fn()
        if args.archive_command == "inventory":
            data = archive_inventory(repo_root=base, remote=args.remote, archive_root=args.archive_root)
            return build_response(tool_name="research.archive.inventory", ok=bool(data.get("ok")), data=data)
        if args.archive_command == "pull":
            data = archive_pull(
                repo_root=base,
                remote=args.remote,
                archive_root=args.archive_root,
                source_root=args.source_root,
                ssh_target=args.ssh_target,
                remote_runtime_root=args.remote_runtime_root,
                since_days=args.since_days,
                run_ids=args.run_ids,
                require_replay_evidence=bool(args.require_replay_evidence),
                include_logs=not bool(args.no_logs),
                write=bool(args.write),
                rsync_path=args.rsync_path,
            )
            return build_response(tool_name="research.archive.pull", ok=bool(data.get("ok")), data=data)
        if args.archive_command == "verify":
            data = archive_verify(repo_root=base, remote=args.remote, archive_root=args.archive_root)
            return build_response(tool_name="research.archive.verify", ok=bool(data.get("ok")), data=data)
        if args.archive_command == "build-datasets":
            data = archive_build_datasets(
                repo_root=base,
                remote=args.remote,
                archive_root=args.archive_root,
                dataset_root=args.dataset_root,
                market=args.market,
                run_ids=args.run_ids,
                latest_scanned=bool(args.latest_scanned),
                mark_from_run_required_data=not bool(args.no_mark_from_run_required_data),
                write=bool(args.write),
            )
            return build_response(tool_name="research.archive.build-datasets", ok=bool(data.get("ok")), data=data)
        if args.archive_command == "prune-remote":
            data = archive_prune_remote(
                repo_root=base,
                remote=args.remote,
                archive_root=args.archive_root,
                ssh_target=args.ssh_target,
                remote_repo_root=args.remote_repo_root,
                remote_runtime_root=args.remote_runtime_root,
                keep_days=args.keep_days,
                keep_count=args.keep_count,
                scope=args.scope,
                include_logs=not bool(args.no_logs),
                confirm=bool(args.confirm),
            )
            return build_response(tool_name="research.archive.prune-remote", ok=bool(data.get("ok")), data=data)
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research archive command: {args.archive_command}")

    if args.research_command == "strategy-lab":
        if args.strategy_lab_command == "top1-loop":
            return handle_top1_command(args)

        from src.application.strategy_lab import (
            analyze_strategy_lab_readiness,
            build_strategy_lab_llm_context,
            build_strategy_lab_proposal,
            run_strategy_lab_experiment,
            run_strategy_lab_update,
        )

        if args.strategy_lab_command == "update":
            if not bool(args.write) and (args.receipt_output or args.receipt_dir):
                raise AgentToolError(
                    code="INPUT_ERROR",
                    message="--receipt-output and --receipt-dir require --write for strategy-lab update",
                )
            base = repo_base_fn()
            profile = _shadow_replay_profile(args, base=base)
            runtime_root = _shadow_replay_runtime_root(args, profile=profile, base=base)
            if (
                args.source == "opend"
                and bool(args.write)
                and str(args.profile_path or "").strip()
                and runtime_root is None
            ):
                raise AgentToolError(
                    code="CONFIG_ERROR",
                    message="profile runtime_root is required for persistent Strategy Lab OpenD sampling",
                )
            dataset_root = _shadow_replay_dataset_root(args.dataset_root, runtime_root=runtime_root, base=base)
            runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
            required_data_root = _shadow_replay_required_data_root(
                args.required_data_root,
                runtime_root=runtime_root,
                base=base,
            )
            opend_fetch_config = (
                _shadow_replay_opend_fetch_config(profile=profile, base=base)
                if args.source == "opend"
                else None
            )
            receipt_dir = (
                _shadow_replay_receipt_dir(args.receipt_dir, runtime_root=runtime_root, base=base)
                if bool(args.write)
                else None
            )
            try:
                data = run_strategy_lab_update(
                    repo_root=base,
                    opend_base_root=runtime_root,
                    opend_fetch_config=opend_fetch_config,
                    dataset_root=dataset_root,
                    required_data_root=required_data_root,
                    source=args.source,
                    min_sample=args.min_sample,
                    min_mark_points=args.min_mark_points,
                    mark_stale_hours=args.mark_stale_hours,
                    actions=args.actions,
                    latest=bool(args.latest),
                    max_datasets=args.max_datasets,
                    build_dataset=bool(args.build_dataset),
                    include_close_decisions=bool(args.include_close_decisions),
                    runs_root=runs_root,
                    dataset_id=args.dataset_id,
                    write=bool(args.write),
                    output=args.output,
                    receipt_output=args.receipt_output,
                    receipt_dir=receipt_dir,
                    settle_after_collect=bool(args.settle_after_collect),
                    opend_host=args.opend_host,
                    opend_port=args.opend_port,
                    limit_expirations=args.limit_expirations,
                    chain_cache=not bool(args.no_chain_cache),
                    chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
                    include_realized_volatility=bool(args.include_realized_volatility),
                    max_symbols=args.max_symbols,
                )
            except ValueError as exc:
                raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
            return build_response(
                tool_name="research.strategy-lab.update",
                ok=str((data.get("summary") or {}).get("status") or "").lower()
                not in {"error", "failed", "partial_failed"},
                data=data,
            )
        if args.strategy_lab_command == "readiness":
            if not _has_strategy_lab_input_scope(args):
                raise AgentToolError(
                    code="INPUT_ERROR",
                    message="strategy-lab readiness requires --dataset or a run-window selector",
                )
            base = repo_base_fn()
            profile = _shadow_replay_profile(args, base=base)
            runtime_root = _shadow_replay_runtime_root(args, profile=profile, base=base)
            runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
            try:
                data = analyze_strategy_lab_readiness(
                    repo_root=base,
                    dataset=args.dataset,
                    runs_root=runs_root,
                    start_date=args.start_date,
                    end_date=args.end_date or args.start_date,
                    accounts=args.accounts,
                    market=args.market,
                    min_sample=args.min_sample,
                    output=args.output,
                )
            except ValueError as exc:
                raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
            return build_response(tool_name="research.strategy-lab.readiness", ok=True, data=data)
        if args.strategy_lab_command == "experiment":
            if not _has_strategy_lab_input_scope(args):
                raise AgentToolError(
                    code="INPUT_ERROR",
                    message="strategy-lab experiment requires --dataset or a run-window selector",
                )
            base = repo_base_fn()
            profile = _shadow_replay_profile(args, base=base)
            runtime_root = _shadow_replay_runtime_root(args, profile=profile, base=base)
            runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
            try:
                data = run_strategy_lab_experiment(
                    repo_root=base,
                    dataset=args.dataset,
                    runs_root=runs_root,
                    start_date=args.start_date,
                    end_date=args.end_date or args.start_date,
                    accounts=args.accounts,
                    market=args.market,
                    min_sample=args.min_sample,
                    output=args.output,
                    auto=bool(args.auto),
                )
            except ValueError as exc:
                raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
            return build_response(tool_name="research.strategy-lab.experiment", ok=True, data=data)
        if args.strategy_lab_command == "proposal":
            data = build_strategy_lab_proposal(
                experiment=args.experiment,
                output=args.output,
                markdown_output=args.markdown_output,
            )
            return build_response(tool_name="research.strategy-lab.proposal", ok=True, data=data)
        if args.strategy_lab_command == "llm-context":
            data = build_strategy_lab_llm_context(
                experiment=args.experiment,
                proposal=args.proposal,
                output=args.output,
                max_rows=args.max_rows,
                max_samples=args.max_samples,
            )
            return build_response(tool_name="research.strategy-lab.llm-context", ok=True, data=data)
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported research strategy-lab command: {args.strategy_lab_command}",
        )

    if args.research_command != "shadow-replay":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported research command: {args.research_command}")

    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        capture_combo_variants,
        collect_shadow_replay_marks,
        evaluate_combo_variant_dataset,
        mark_shadow_replay_dataset,
        prepare_combo_funding_puts,
        run_shadow_replay_candidate_impact,
        run_shadow_replay_data_plan,
        settle_shadow_replay_dataset,
        shadow_replay_dataset_status,
    )

    base = repo_base_fn()
    profile = _shadow_replay_profile(args, base=base)
    runtime_root = _shadow_replay_runtime_root(args, profile=profile, base=base)

    if args.shadow_replay_command == "capture-combo-variants":
        dataset_root = _shadow_replay_dataset_root(args.dataset_root, runtime_root=runtime_root, base=base)
        try:
            data = capture_combo_variants(
                repo_root=base,
                config_key=args.config_key,
                account=args.account,
                symbols=args.symbols,
                variant_spec_path=_resolve_shadow_path(args.variant_spec, base=base),
                dataset_root=dataset_root,
                dataset_id=args.dataset_id,
                write=bool(args.write),
                opend_host=args.opend_host,
                opend_port=args.opend_port,
                chain_cache=not bool(args.no_chain_cache),
                chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(
            tool_name="research.shadow-replay.capture-combo-variants",
            ok=True,
            data=data,
        )

    if args.shadow_replay_command == "evaluate-combo-variants":
        dataset = _resolve_shadow_path(args.dataset, base=base)
        funding_put_path = (
            _resolve_shadow_path(args.funding_put_path, base=base)
            if args.funding_put_path
            else dataset / "combo_owned_funding_put_decisions.v1.jsonl"
        )
        try:
            data = evaluate_combo_variant_dataset(
                dataset=dataset,
                funding_put_path=funding_put_path,
                write=bool(args.write),
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(
            tool_name="research.shadow-replay.evaluate-combo-variants",
            ok=True,
            data=data,
        )

    if args.shadow_replay_command == "prepare-combo-funding-puts":
        runs_root = _shadow_replay_runs_root(
            args,
            profile=profile,
            runtime_root=runtime_root,
            base=base,
        )
        if runs_root is None:
            raise AgentToolError(
                code="INPUT_ERROR",
                message="--runs-root, --runtime-root, or a profile runs_root is required",
            )
        try:
            data = prepare_combo_funding_puts(
                dataset=_resolve_shadow_path(args.dataset, base=base),
                source_run_id=args.source_run_id,
                source_runs_root=runs_root,
                write=bool(args.write),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(
            tool_name="research.shadow-replay.prepare-combo-funding-puts",
            ok=True,
            data=data,
        )

    if args.shadow_replay_command == "build":
        if bool(args.latest_scanned_run) and (args.run_id or args.run_dir):
            raise AgentToolError(
                code="INPUT_ERROR",
                message="--latest-scanned-run cannot be combined with --run-id or --run-dir",
        )
        runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
        dataset_root = _shadow_replay_dataset_root(args.dataset_root, runtime_root=runtime_root, base=base)
        try:
            data = build_shadow_replay_dataset(
                repo_root=base,
                run_id=args.run_id,
                runs_root=runs_root,
                run_dir=args.run_dir,
                report_dir=args.report_dir,
                trace_paths=args.trace_paths,
                mark_paths=args.mark_paths,
                outcome_paths=args.outcome_paths,
                include_close_decisions=bool(args.include_close_decisions),
                output_dir=args.output_dir,
                dataset_root=dataset_root,
                dataset_id=args.dataset_id,
                latest_scanned_run=bool(args.latest_scanned_run),
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(tool_name="research.shadow-replay.build", ok=True, data=data)

    if args.shadow_replay_command == "analyze":
        data = analyze_shadow_replay_dataset(dataset=args.dataset, min_sample=args.min_sample, output=args.output)
        return build_response(tool_name="research.shadow-replay.analyze", ok=True, data=data)

    if args.shadow_replay_command == "candidate-impact":
        runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
        try:
            data = run_shadow_replay_candidate_impact(
                repo_root=base,
                params=args.params,
                dataset=args.dataset,
                runs_root=runs_root,
                start_date=args.start_date,
                end_date=args.end_date or args.start_date,
                accounts=args.accounts,
                market=args.market,
                min_sample=args.min_sample,
                output_format=args.output_format,
                output=args.output,
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(tool_name=f"research.shadow-replay.{args.shadow_replay_command}", ok=True, data=data)

    if args.shadow_replay_command == "candidate-impact-report":
        runs_root = _shadow_replay_runs_root(args, profile=profile, runtime_root=runtime_root, base=base)
        params_path = _candidate_impact_report_params_path(args, runtime_root=runtime_root, base=base)
        output_root = _shadow_replay_backtest_root(args.output_dir, runtime_root=runtime_root, base=base)
        output_dir = output_root if args.output_dir else output_root / _candidate_impact_report_id(args)
        output_dir.mkdir(parents=True, exist_ok=True)
        market = str(args.market).lower()
        json_output = output_dir / f"result.{market}.json"
        markdown_output = output_dir / f"result.{market}.md"
        try:
            result = run_shadow_replay_candidate_impact(
                repo_root=base,
                params=params_path,
                dataset=args.dataset,
                runs_root=runs_root,
                start_date=args.start_date,
                end_date=args.end_date or args.start_date,
                accounts=args.accounts,
                market=args.market,
                min_sample=args.min_sample,
                output_format="json",
                output=json_output,
            )
            run_shadow_replay_candidate_impact(
                repo_root=base,
                params=params_path,
                dataset=args.dataset,
                runs_root=runs_root,
                start_date=args.start_date,
                end_date=args.end_date or args.start_date,
                accounts=args.accounts,
                market=args.market,
                min_sample=args.min_sample,
                output_format="markdown",
                output=markdown_output,
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        data = {
            "schema_version": "shadow_replay_candidate_impact_report.v1",
            "market": market,
            "params_path": str(params_path),
            "output_dir": str(output_dir),
            "json_output": str(json_output),
            "markdown_output": str(markdown_output),
            "candidate_impact_result": _candidate_impact_report_summary(result),
        }
        return build_response(tool_name=f"research.shadow-replay.{args.shadow_replay_command}", ok=True, data=data)

    if args.shadow_replay_command in {"status", "list"}:
        dataset_root = _shadow_replay_dataset_root(args.dataset_root, runtime_root=runtime_root, base=base)
        required_data_root = _shadow_replay_required_data_root(args.required_data_root, runtime_root=runtime_root, base=base)
        data = shadow_replay_dataset_status(
            repo_root=base,
            dataset_root=dataset_root,
            required_data_root=required_data_root,
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
        dataset_root = _shadow_replay_dataset_root(args.dataset_root, runtime_root=runtime_root, base=base)
        required_data_root = _shadow_replay_required_data_root(args.required_data_root, runtime_root=runtime_root, base=base)
        receipt_dir = (
            _shadow_replay_receipt_dir(args.receipt_dir, runtime_root=runtime_root, base=base)
            if bool(args.write)
            else None
        )
        data = run_shadow_replay_data_plan(
            repo_root=base,
            dataset_root=dataset_root,
            required_data_root=required_data_root,
            source=args.source,
            min_sample=args.min_sample,
            min_mark_points=args.min_mark_points,
            mark_stale_hours=args.mark_stale_hours,
            actions=args.actions,
            max_datasets=args.max_datasets,
            write=bool(args.write),
            receipt_output=args.receipt_output,
            receipt_dir=receipt_dir,
            settle_after_collect=bool(args.settle_after_collect),
            opend_host=args.opend_host,
            opend_port=args.opend_port,
            limit_expirations=args.limit_expirations,
            chain_cache=not bool(args.no_chain_cache),
            chain_cache_force_refresh=bool(args.chain_cache_force_refresh),
            include_realized_volatility=bool(args.include_realized_volatility),
            max_symbols=args.max_symbols,
        )
        return build_response(
            tool_name="research.shadow-replay.run-data-plan",
            ok=int((data.get("summary") or {}).get("error_count") or 0) == 0,
            data=data,
        )

    if args.shadow_replay_command == "mark":
        required_data_root = _shadow_replay_required_data_root(args.required_data_root, runtime_root=runtime_root, base=base) or (
            base / "output_shared" / "required_data"
        )
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
        required_data_root = _shadow_replay_required_data_root(args.required_data_root, runtime_root=runtime_root, base=base) or (
            base / "output_shared" / "required_data"
        )
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
        return build_response(
            tool_name="research.shadow-replay.collect-marks",
            ok=str((data.get("summary") or {}).get("status") or "").lower()
            not in {"error", "failed", "partial_failed"},
            data=data,
        )

    if args.shadow_replay_command == "settle":
        data = settle_shadow_replay_dataset(
            dataset=args.dataset,
            output=args.output,
            write=bool(args.write),
            replace=bool(args.replace),
            lifecycle_paths=args.lifecycle_paths,
        )
        return build_response(tool_name="research.shadow-replay.settle", ok=True, data=data)

    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"unsupported research shadow-replay command: {args.shadow_replay_command}",
    )
