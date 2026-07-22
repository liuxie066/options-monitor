from __future__ import annotations

from collections import Counter
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
DEFAULT_ACTIONS = ("collect_marks", "settle")
SUPPORTED_ACTIONS = ("collect_marks", "settle")


def run_shadow_replay_data_plan(
    *,
    repo_root: str | Path,
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
) -> dict[str, Any]:
    """Dry-run or execute local Shadow Replay data-maintenance actions.

    Without ``write=True`` this is a dry-run and writes no receipt.
    """

    base = Path(repo_root).expanduser().resolve()
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
        write_json(receipt_path, result)
    return result


def _run_plan_rows(
    rows: Any,
    *,
    repo_root: Path,
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
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    executed_or_planned = 0
    plan_rows = rows if isinstance(rows, list) else []
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        action = text(row.get("action"))
        base = _action_base(row)
        if action not in action_set:
            out.append({**base, "result_status": "skipped", "reason": "action_not_enabled"})
            continue
        if max_datasets is not None and executed_or_planned >= max_datasets:
            out.append({**base, "result_status": "skipped", "reason": "max_datasets_reached"})
            continue
        executed_or_planned += 1
        if not write:
            out.append({**base, "result_status": "planned", "reason": "dry_run"})
            continue
        out.append(
            _execute_plan_row(
                row,
                repo_root=repo_root,
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
            )
        )
    return out


def _execute_plan_row(
    row: dict[str, Any],
    *,
    repo_root: Path,
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
            )
        elif action == "settle":
            payload = settle_shadow_replay_dataset(dataset=dataset_dir, write=True, replace=False)
        else:
            return {**base, "result_status": "skipped", "reason": "unsupported_action"}
    except Exception as exc:
        return {
            **base,
            "result_status": "error",
            "reason": "action_exception",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        **base,
        "result_status": "ok",
        "reason": "executed",
        "operation": _operation_payload(payload),
    }


def _action_base(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": row.get("dataset_id"),
        "dataset_dir": row.get("dataset_dir"),
        "status": row.get("status"),
        "data_plan_reason": row.get("reason"),
        "action": row.get("action"),
        "priority": row.get("priority"),
        "state": row.get("state"),
        "last_mark_at": row.get("last_mark_at"),
        "mark_age_hours": row.get("mark_age_hours"),
        "usable_mark_point_count": row.get("usable_mark_point_count"),
    }


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
    return {
        "plan_action_count": len(rows),
        "write": bool(write),
        "planned_count": counts.get("planned", 0),
        "executed_count": counts.get("ok", 0),
        "skipped_count": counts.get("skipped", 0),
        "error_count": counts.get("error", 0),
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
            row.get("action") in {"collect_marks", "settle"} and row.get("result_status") == "ok"
            for row in action_results
        )
    )
    collect_attempted = bool(
        write
        and any(
            row.get("action") == "collect_marks" and row.get("result_status") in {"ok", "error"}
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
