from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from src.application.shadow_replay import build_shadow_replay_dataset, run_shadow_replay_data_plan
from src.application.shadow_replay.common import resolve_output_path, text, utc_now, write_json


UPDATE_SCHEMA_VERSION = "strategy_lab_update.v1"


def run_strategy_lab_update(
    *,
    repo_root: str | Path,
    dataset_root: str | Path | None = None,
    required_data_root: str | Path | None = None,
    source: str = "local",
    min_sample: int = 30,
    min_mark_points: int = 2,
    mark_stale_hours: int = 24,
    actions: Iterable[str] | None = None,
    latest: bool = False,
    max_datasets: int | None = None,
    build_dataset: bool = False,
    runs_root: str | Path | None = None,
    dataset_id: str | None = None,
    write: bool = False,
    output: str | Path | None = None,
    receipt_output: str | Path | None = None,
    receipt_dir: str | Path | None = None,
    settle_after_collect: bool = False,
    opend_host: str = "127.0.0.1",
    opend_port: int = 11111,
    limit_expirations: int = 8,
    chain_cache: bool = True,
    chain_cache_force_refresh: bool = False,
    include_realized_volatility: bool = False,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    effective_max = _effective_max_datasets(latest=latest, max_datasets=max_datasets)
    dataset_build = _build_latest_dataset(
        repo_root=repo_root,
        dataset_root=dataset_root,
        runs_root=runs_root,
        dataset_id=dataset_id,
        requested=bool(build_dataset),
        write=bool(write),
    )
    data_plan = run_shadow_replay_data_plan(
        repo_root=repo_root,
        dataset_root=dataset_root,
        required_data_root=required_data_root,
        source=source,
        min_sample=min_sample,
        min_mark_points=min_mark_points,
        mark_stale_hours=mark_stale_hours,
        actions=actions,
        max_datasets=effective_max,
        write=bool(write),
        receipt_output=receipt_output,
        receipt_dir=receipt_dir,
        settle_after_collect=bool(settle_after_collect),
        opend_host=opend_host,
        opend_port=opend_port,
        limit_expirations=limit_expirations,
        chain_cache=bool(chain_cache),
        chain_cache_force_refresh=bool(chain_cache_force_refresh),
        include_realized_volatility=bool(include_realized_volatility),
        max_symbols=max_symbols,
    )
    status = data_plan.get("status_after") if write and data_plan.get("status_after") else data_plan.get("status_before")
    result: dict[str, Any] = {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "summary": _summary(
            data_plan=data_plan,
            dataset_build=dataset_build,
            status=status,
            write=bool(write),
            latest=bool(latest),
        ),
        "selection": {
            "latest": bool(latest),
            "max_datasets": effective_max,
            "selection_mode": "highest_priority_due_dataset" if latest else "all_due_datasets",
            "build_dataset": bool(build_dataset),
            "build_source": "latest_scanned_run" if build_dataset else None,
            "runs_root": str(runs_root) if runs_root is not None and text(runs_root) else None,
            "dataset_id": text(dataset_id) or None,
        },
        "strategy_lab": {
            "ready_for_experiment_queue": _ready_queue(status),
            "data_plan_actions": list(data_plan.get("actions") or []),
            "next_action": _next_action(
                data_plan=data_plan,
                dataset_build=dataset_build,
                status=status,
                write=bool(write),
            ),
        },
        "shadow_replay": {
            "dataset_build": dataset_build,
            "status": status,
            "data_plan_run": data_plan,
        },
        "safety": _safety(data_plan=data_plan, dataset_build=dataset_build, output=output),
    }
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _effective_max_datasets(*, latest: bool, max_datasets: int | None) -> int | None:
    if max_datasets is not None:
        return max(0, int(max_datasets))
    if latest:
        return 1
    return None


def _build_latest_dataset(
    *,
    repo_root: str | Path,
    dataset_root: str | Path | None,
    runs_root: str | Path | None,
    dataset_id: str | None,
    requested: bool,
    write: bool,
) -> dict[str, Any]:
    if not requested:
        return {
            "requested": False,
            "executed": False,
            "reason": "not_requested",
            "source": "latest_scanned_run",
        }
    if not write:
        return {
            "requested": True,
            "executed": False,
            "reason": "requires_write",
            "source": "latest_scanned_run",
            "dataset_id": text(dataset_id) or None,
            "dataset_root": str(dataset_root) if dataset_root is not None and text(dataset_root) else None,
            "runs_root": str(runs_root) if runs_root is not None and text(runs_root) else None,
        }
    manifest = build_shadow_replay_dataset(
        repo_root=repo_root,
        runs_root=runs_root,
        dataset_root=dataset_root,
        dataset_id=dataset_id,
        latest_scanned_run=True,
    )
    return {
        "requested": True,
        "executed": True,
        "reason": "built_latest_scanned_run",
        "source": "latest_scanned_run",
        "dataset_id": manifest.get("dataset_id"),
        "dataset_dir": manifest.get("dataset_dir"),
        "source_selection": (manifest.get("source") or {}).get("latest_scanned_run_selection"),
        "manifest_summary": manifest.get("summary") or {},
        "manifest_safety": manifest.get("safety") or {},
    }


def _summary(
    *,
    data_plan: dict[str, Any],
    dataset_build: dict[str, Any],
    status: dict[str, Any] | None,
    write: bool,
    latest: bool,
) -> dict[str, Any]:
    plan_summary = data_plan.get("summary") or {}
    status_summary = (status or {}).get("summary") or {}
    error_count = int(plan_summary.get("error_count") or 0)
    executed_count = int(plan_summary.get("executed_count") or 0)
    planned_count = int(plan_summary.get("planned_count") or 0)
    build_requested = bool(dataset_build.get("requested"))
    build_executed = bool(dataset_build.get("executed"))
    return {
        "status": "error" if error_count else ("updated" if write and (executed_count or build_executed) else "planned"),
        "write": bool(write),
        "latest": bool(latest),
        "dataset_build_requested": build_requested,
        "dataset_built": build_executed,
        "built_dataset_id": dataset_build.get("dataset_id") if build_executed else None,
        "dataset_count": status_summary.get("dataset_count"),
        "data_plan_action_count": plan_summary.get("plan_action_count"),
        "planned_count": planned_count,
        "executed_count": executed_count,
        "skipped_count": int(plan_summary.get("skipped_count") or 0),
        "error_count": error_count,
        "ready_for_experiment_count": status_summary.get("review_queue_count"),
        "sampling_due_count": status_summary.get("sampling_due_count"),
        "stale_mark_count": status_summary.get("stale_mark_count"),
    }


def _ready_queue(status: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (status or {}).get("review_queue")
    if not isinstance(rows, list):
        return []
    return [
        {
            "dataset_id": row.get("dataset_id"),
            "dataset_dir": row.get("dataset_dir"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "suggested_command": row.get("suggested_command"),
        }
        for row in rows
        if isinstance(row, dict)
    ]


def _next_action(
    *,
    data_plan: dict[str, Any],
    dataset_build: dict[str, Any],
    status: dict[str, Any] | None,
    write: bool,
) -> str:
    summary = data_plan.get("summary") or {}
    if int(summary.get("error_count") or 0) > 0:
        return "inspect_data_plan_errors"
    if bool(dataset_build.get("requested")) and not write:
        return "rerun_with_write_to_build_latest_dataset"
    if not write and int(summary.get("planned_count") or 0) > 0:
        return "rerun_with_write_to_update_local_evidence"
    if int(((status or {}).get("summary") or {}).get("review_queue_count") or 0) > 0:
        return "run_strategy_lab_readiness_or_experiment_for_ready_datasets"
    if int(summary.get("executed_count") or 0) > 0:
        return "rerun_update_later_after_more_market_time"
    return "wait_for_more_replay_evidence"


def _safety(*, data_plan: dict[str, Any], dataset_build: dict[str, Any], output: str | Path | None) -> dict[str, Any]:
    safety = dict(data_plan.get("safety") or {})
    targets = list(safety.get("persistent_write_targets") or [])
    if bool(dataset_build.get("executed")):
        targets.append("shadow_replay_dataset")
    if output:
        targets.append("strategy_lab_update_artifact")
    deduped_targets = list(dict.fromkeys(targets))
    safety.update(
        {
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
            "online_ai_called": False,
            "writes_shadow_replay_dataset_build": bool(dataset_build.get("executed")),
            "writes_strategy_lab_update_artifact": bool(output),
            "persistent_write_targets": deduped_targets,
            "writes_persistent_outputs": bool(deduped_targets),
        }
    )
    if not text(safety.get("writes_runtime_config")):
        safety["writes_runtime_config"] = False
    if not text(safety.get("writes_trade_state")):
        safety["writes_trade_state"] = False
    if not text(safety.get("sends_notifications")):
        safety["sends_notifications"] = False
    return safety
