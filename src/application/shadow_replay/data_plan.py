from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from src.application.shadow_replay.collection import collect_shadow_replay_marks
from src.application.shadow_replay.common import (
    resolve_output_path,
    resolve_path,
    safety_payload,
    text,
    utc_now,
    write_json,
)
from src.application.shadow_replay.settlement import settle_shadow_replay_dataset
from src.application.shadow_replay.status import shadow_replay_dataset_status


DATA_PLAN_SCHEMA_VERSION = "shadow_replay_data_plan_run.v1"
DATA_PLAN_RECEIPT_SCHEMA_VERSION = "shadow_replay_data_plan_receipt.v2"
DEFAULT_ACTIONS = ("collect_marks", "settle")
SUPPORTED_ACTIONS = ("collect_marks", "settle")


def run_shadow_replay_data_plan(
    *,
    repo_root: str | Path,
    opend_base_root: str | Path | None = None,
    opend_fetch_config: dict[str, float | int] | None = None,
    dataset_root: str | Path | None = None,
    required_data_root: str | Path | None = None,
    source: str = "local",
    min_sample: int = 30,
    min_mark_points: int = 2,
    mark_stale_hours: int = 24,
    actions: Iterable[str] | None = None,
    max_datasets: int | None = None,
    write: bool = False,
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
    now_utc: str | None = None,
    fail_fast_on_opend_rate_limit: bool = False,
) -> dict[str, Any]:
    """Dry-run or execute local Shadow Replay data-maintenance actions.

    Without ``write=True`` this is a dry-run and writes no receipt.
    """

    base = Path(repo_root).expanduser().resolve()
    opend_base = (
        Path(opend_base_root).expanduser().resolve()
        if opend_base_root is not None and text(opend_base_root)
        else base
    )
    generated_at = text(now_utc) or utc_now()
    source_norm = text(source).lower() or "local"
    if source_norm not in {"local", "opend"}:
        raise ValueError("source must be local or opend")
    if not write and _has_receipt_path(receipt_output=receipt_output, receipt_dir=receipt_dir):
        raise ValueError("receipt_output and receipt_dir require write=True")
    action_set = _normalize_actions(actions)
    max_count = _normalize_max_datasets(max_datasets)
    required_root = (
        resolve_path(required_data_root, base=base)
        if required_data_root is not None and text(required_data_root)
        else (base / "output_shared" / "required_data").resolve()
    )

    before = shadow_replay_dataset_status(
        repo_root=base,
        dataset_root=dataset_root,
        required_data_root=required_root,
        min_sample=min_sample,
        min_mark_points=min_mark_points,
        mark_stale_hours=mark_stale_hours,
        now_utc=generated_at,
    )
    action_results = _run_plan_rows(
        before.get("data_plan") if isinstance(before, dict) else [],
        repo_root=base,
        opend_base_root=opend_base,
        opend_fetch_config=opend_fetch_config,
        required_data_root=required_root,
        source=source_norm,
        action_set=action_set,
        max_datasets=max_count,
        write=bool(write),
        settle_after_collect=bool(settle_after_collect),
        opend_host=opend_host,
        opend_port=opend_port,
        limit_expirations=limit_expirations,
        chain_cache=bool(chain_cache),
        chain_cache_force_refresh=bool(chain_cache_force_refresh),
        include_realized_volatility=bool(include_realized_volatility),
        max_symbols=max_symbols,
        generated_at=generated_at,
        fail_fast_on_opend_rate_limit=bool(fail_fast_on_opend_rate_limit),
    )
    receipt_path = _resolve_receipt_path(
        base=base,
        generated_at=generated_at,
        receipt_output=receipt_output,
        receipt_dir=receipt_dir,
        write=bool(write),
    )
    result = {
        "schema_version": DATA_PLAN_SCHEMA_VERSION,
        "generated_at_utc": generated_at,
        "write": bool(write),
        "source": source_norm,
        "opend_base_root": str(opend_base) if source_norm == "opend" else None,
        "dataset_root": before.get("dataset_root"),
        "required_data_root": str(required_root),
        "actions_enabled": sorted(action_set),
        "max_datasets": max_count,
        "summary": _summary(
            plan_rows=before.get("data_plan") if isinstance(before, dict) else [],
            action_results=action_results,
            write=bool(write),
            receipt_path=receipt_path,
        ),
        "status_before": before,
        "actions": action_results,
        "status_after": (
            shadow_replay_dataset_status(
                repo_root=base,
                dataset_root=dataset_root,
                required_data_root=required_root,
                min_sample=min_sample,
                min_mark_points=min_mark_points,
                mark_stale_hours=mark_stale_hours,
                now_utc=generated_at,
            )
            if write
            else None
        ),
        "receipt_path": str(receipt_path) if receipt_path is not None else None,
        "safety": _safety(
            write=bool(write),
            source=source_norm,
            receipt_path=receipt_path,
            action_results=action_results,
        ),
    }
    if receipt_path is not None:
        receipt = _compact_receipt(result)
        write_json(receipt_path, receipt)
        result["receipt_schema_version"] = DATA_PLAN_RECEIPT_SCHEMA_VERSION
        result["receipt_sha256"] = hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest()
    else:
        result["receipt_schema_version"] = None
        result["receipt_sha256"] = None
    return result


def _compact_receipt(result: dict[str, Any]) -> dict[str, Any]:
    before = result.get("status_before")
    after = result.get("status_after")
    return {
        "schema_version": DATA_PLAN_RECEIPT_SCHEMA_VERSION,
        "result_schema_version": result["schema_version"],
        "generated_at_utc": result["generated_at_utc"],
        "write": result["write"],
        "source": result["source"],
        "opend_base_root": result["opend_base_root"],
        "dataset_root": result["dataset_root"],
        "required_data_root": result["required_data_root"],
        "actions_enabled": result["actions_enabled"],
        "max_datasets": result["max_datasets"],
        "summary": result["summary"],
        "status_before_summary": _status_receipt_summary(before),
        "status_before_sha256": _canonical_sha256(before),
        "status_after_summary": _status_receipt_summary(after),
        "status_after_sha256": _canonical_sha256(after),
        "actions": [_action_receipt(row) for row in result["actions"]],
        "safety": result["safety"],
    }


def _status_receipt_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    data_plan = value.get("data_plan")
    review_queue = value.get("review_queue")
    datasets = value.get("datasets")
    blocker_counts: Counter[str] = Counter()
    for row in datasets if isinstance(datasets, list) else []:
        gaps = row.get("outcome_gaps") if isinstance(row, dict) else None
        counts = gaps.get("blocker_counts") if isinstance(gaps, dict) else None
        if isinstance(counts, dict):
            for reason, count in counts.items():
                blocker_counts[str(reason)] += int(count or 0)
    return {
        "summary": value.get("summary") if isinstance(value.get("summary"), dict) else {},
        "dataset_count": len(datasets) if isinstance(datasets, list) else 0,
        "data_plan_count": len(data_plan) if isinstance(data_plan, list) else 0,
        "review_queue_count": len(review_queue) if isinstance(review_queue, list) else 0,
        "blocker_counts": dict(sorted(blocker_counts.items())),
    }


def _action_receipt(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    operation = row.get("operation")
    operation = operation if isinstance(operation, dict) else {}
    receipt = {
        key: row.get(key)
        for key in (
            "dataset_id",
            "action",
            "reason",
            "result_status",
            "dataset_integrity_status",
            "dataset_integrity_reason",
        )
    }
    for key in ("error_type", "error_message_sha256"):
        if row.get(key):
            receipt[key] = row[key]
    return receipt | {
        "operation": {
            "schema_version": operation.get("schema_version"),
            "summary": operation.get("summary") if isinstance(operation.get("summary"), dict) else {},
            "safety": operation.get("safety") if isinstance(operation.get("safety"), dict) else {},
        }
    }


def _canonical_sha256(value: Any) -> str | None:
    if value is None:
        return None
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run_plan_rows(
    rows: Any,
    *,
    repo_root: Path,
    opend_base_root: Path,
    opend_fetch_config: dict[str, float | int] | None,
    required_data_root: Path,
    source: str,
    action_set: set[str],
    max_datasets: int | None,
    write: bool,
    settle_after_collect: bool,
    opend_host: str,
    opend_port: int,
    limit_expirations: int,
    chain_cache: bool,
    chain_cache_force_refresh: bool,
    include_realized_volatility: bool,
    max_symbols: int | None,
    generated_at: str,
    fail_fast_on_opend_rate_limit: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    executed_or_planned = 0
    opend_rate_limit_circuit_open = False
    plan_rows = rows if isinstance(rows, list) else []
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        action = text(row.get("action"))
        base = _action_base(row)
        if action not in action_set:
            out.append({**base, "result_status": "skipped", "reason": "action_not_enabled"})
            continue
        if write and not _dataset_integrity_verified(row):
            out.append(
                {
                    **base,
                    "result_status": "skipped",
                    "reason": "dataset_integrity_unverified",
                }
            )
            continue
        if action == "collect_marks" and opend_rate_limit_circuit_open:
            out.append(
                {
                    **base,
                    "result_status": "deferred",
                    "reason": "opend_rate_limit_circuit_open",
                }
            )
            continue
        if max_datasets is not None and executed_or_planned >= max_datasets:
            out.append({**base, "result_status": "skipped", "reason": "max_datasets_reached"})
            continue
        executed_or_planned += 1
        if not write:
            out.append({**base, "result_status": "planned", "reason": "dry_run"})
            continue
        result = _execute_plan_row(
            row,
            repo_root=repo_root,
            opend_base_root=opend_base_root,
            opend_fetch_config=opend_fetch_config,
            required_data_root=required_data_root,
            source=source,
            settle_after_collect=settle_after_collect,
            opend_host=opend_host,
            opend_port=opend_port,
            limit_expirations=limit_expirations,
            chain_cache=chain_cache,
            chain_cache_force_refresh=chain_cache_force_refresh,
            include_realized_volatility=include_realized_volatility,
            max_symbols=max_symbols,
            generated_at=generated_at,
            fail_fast_on_opend_rate_limit=fail_fast_on_opend_rate_limit,
        )
        out.append(result)
        if result.get("reason") == "opend_rate_limited":
            opend_rate_limit_circuit_open = True
    return out


def _execute_plan_row(
    row: dict[str, Any],
    *,
    repo_root: Path,
    opend_base_root: Path,
    opend_fetch_config: dict[str, float | int] | None,
    required_data_root: Path,
    source: str,
    settle_after_collect: bool,
    opend_host: str,
    opend_port: int,
    limit_expirations: int,
    chain_cache: bool,
    chain_cache_force_refresh: bool,
    include_realized_volatility: bool,
    max_symbols: int | None,
    generated_at: str,
    fail_fast_on_opend_rate_limit: bool,
) -> dict[str, Any]:
    base = _action_base(row)
    dataset_dir = text(row.get("dataset_dir"))
    if not dataset_dir:
        return {**base, "result_status": "error", "reason": "dataset_dir_missing"}
    action = text(row.get("action"))
    try:
        if action == "collect_marks":
            payload = collect_shadow_replay_marks(
                dataset=dataset_dir,
                required_data_root=required_data_root,
                source=source,
                repo_root=repo_root,
                opend_base_root=opend_base_root,
                opend_fetch_config=opend_fetch_config,
                as_of=None,
                write=True,
                replace=False,
                settle=settle_after_collect,
                opend_host=opend_host,
                opend_port=opend_port,
                limit_expirations=limit_expirations,
                chain_cache=chain_cache,
                chain_cache_force_refresh=chain_cache_force_refresh,
                include_realized_volatility=include_realized_volatility,
                max_symbols=max_symbols,
                fail_fast_on_opend_rate_limit=fail_fast_on_opend_rate_limit,
            )
        elif action == "settle":
            payload = settle_shadow_replay_dataset(dataset=dataset_dir, write=True, replace=False)
        else:
            return {**base, "result_status": "skipped", "reason": "unsupported_action"}
    except Exception as exc:
        error_message = str(exc)
        return {
            **base,
            "result_status": "error",
            "reason": "action_exception",
            "error": f"{type(exc).__name__}: {error_message}",
            "error_type": type(exc).__name__,
            "error_message_sha256": hashlib.sha256(
                error_message.encode("utf-8")
            ).hexdigest(),
        }
    operation = _operation_payload(payload)
    operation_summary = operation.get("summary") or {}
    operation_deferred = (
        text(operation_summary.get("status")).lower() == "deferred"
        and int(operation_summary.get("opend_non_rate_limit_error_count") or 0) == 0
    )
    if operation_deferred:
        return {
            **base,
            "result_status": "deferred",
            "reason": "opend_rate_limited",
            "operation": operation,
        }
    operation_failed = (
        text(operation_summary.get("status")).lower()
        in {"error", "failed", "partial_failed"}
        or int(operation_summary.get("error_count") or 0) > 0
        or int(operation_summary.get("opend_fetch_error_count") or 0) > 0
    )
    return {
        **base,
        "result_status": "error" if operation_failed else "ok",
        "reason": "operation_reported_failure" if operation_failed else "executed",
        "operation": operation,
    }


def _action_base(row: dict[str, Any]) -> dict[str, Any]:
    integrity = row.get("dataset_integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    return {
        "dataset_id": row.get("dataset_id"),
        "dataset_dir": row.get("dataset_dir"),
        "facet": row.get("facet"),
        "facets": list(row.get("facets") or []),
        "status": row.get("status"),
        "data_plan_reason": row.get("reason"),
        "action": row.get("action"),
        "priority": row.get("priority"),
        "state": row.get("state"),
        "last_mark_at": row.get("last_mark_at"),
        "mark_age_hours": row.get("mark_age_hours"),
        "usable_mark_point_count": row.get("usable_mark_point_count"),
        "dataset_integrity_status": text(integrity.get("status")) or "unknown",
        "dataset_integrity_reason": text(integrity.get("reason")) or None,
    }


def _dataset_integrity_verified(row: dict[str, Any]) -> bool:
    integrity = row.get("dataset_integrity")
    return isinstance(integrity, dict) and text(integrity.get("status")).lower() == "verified"


def _operation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    safety = payload.get("safety") if isinstance(payload, dict) else None
    out = {
        "schema_version": payload.get("schema_version") if isinstance(payload, dict) else None,
        "summary": summary if isinstance(summary, dict) else {},
    }
    if isinstance(safety, dict):
        out["safety"] = safety
    return out


def _normalize_actions(actions: Iterable[str] | None) -> set[str]:
    raw = list(actions or DEFAULT_ACTIONS)
    out = {text(item).lower() for item in raw if text(item)}
    invalid = sorted(out.difference(SUPPORTED_ACTIONS))
    if invalid:
        raise ValueError(f"unsupported shadow replay data-plan action(s): {', '.join(invalid)}")
    return out or set(DEFAULT_ACTIONS)


def _normalize_max_datasets(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _summary(
    *,
    plan_rows: Any,
    action_results: list[dict[str, Any]],
    write: bool,
    receipt_path: Path | None,
) -> dict[str, Any]:
    rows = plan_rows if isinstance(plan_rows, list) else []
    counts = Counter(text(row.get("result_status")) for row in action_results)
    error_count = counts.get("error", 0)
    deferred_count = counts.get("deferred", 0)
    integrity_skipped_count = sum(
        1
        for row in action_results
        if row.get("reason") == "dataset_integrity_unverified"
    )
    return {
        "status": (
            "failed"
            if error_count and counts.get("ok", 0) == 0
            else "partial_failed"
            if error_count
            else "deferred"
            if deferred_count
            else "success"
        ),
        "plan_action_count": len(rows),
        "write": bool(write),
        "planned_count": counts.get("planned", 0),
        "executed_count": counts.get("ok", 0),
        "skipped_count": counts.get("skipped", 0),
        "integrity_skipped_count": integrity_skipped_count,
        "deferred_count": deferred_count,
        "error_count": error_count,
        "receipt_written": receipt_path is not None,
        "receipt_path": str(receipt_path) if receipt_path is not None else None,
    }


def _resolve_receipt_path(
    *,
    base: Path,
    generated_at: str,
    receipt_output: str | Path | None,
    receipt_dir: str | Path | None,
    write: bool,
) -> Path | None:
    if not write:
        return None
    if receipt_output is not None and text(receipt_output):
        return resolve_output_path(receipt_output)
    directory = (
        resolve_path(receipt_dir, base=base)
        if receipt_dir is not None and text(receipt_dir)
        else (base / "output_shared" / "research" / "shadow_replay" / "receipts").resolve()
    )
    return directory / f"{_receipt_stamp(generated_at)}-data-plan.json"


def _has_receipt_path(*, receipt_output: str | Path | None, receipt_dir: str | Path | None) -> bool:
    return bool(text(receipt_output) or text(receipt_dir))


def _receipt_stamp(value: str) -> str:
    stamp = "".join(ch for ch in value if ch.isalnum())
    return stamp or "shadow-replay"


def _safety(
    *,
    write: bool,
    source: str,
    receipt_path: Path | None,
    action_results: list[dict[str, Any]],
) -> dict[str, Any]:
    dataset_wrote = bool(
        write
        and any(
            row.get("action") in {"collect_marks", "settle"}
            and row.get("result_status") in {"ok", "deferred"}
            for row in action_results
        )
    )
    collect_attempted = bool(
        write
        and any(
            row.get("action") == "collect_marks"
            and row.get("result_status") in {"ok", "error", "deferred"}
            for row in action_results
        )
    )
    safety = safety_payload(writes_local_dataset=dataset_wrote)
    targets: list[str] = []
    if dataset_wrote:
        targets.append("shadow_replay_dataset")
    if write:
        if source == "opend" and collect_attempted:
            targets.extend(["required_data_cache", "opend_rate_limit_state", "opend_cache"])
    if receipt_path is not None:
        targets.append("shadow_replay_receipt")
    safety.update(
        {
            "reads_opend": source == "opend" and collect_attempted,
            "writes_required_data_cache": source == "opend" and collect_attempted,
            "writes_persistent_outputs": bool(targets),
            "persistent_write_targets": targets,
            "writes_local_research_artifacts_only": bool(targets) if write or receipt_path is not None else False,
        }
    )
    safety["writes_local_dataset_only"] = targets == ["shadow_replay_dataset"]
    return safety
