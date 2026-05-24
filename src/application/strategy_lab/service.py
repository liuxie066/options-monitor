from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.runtime_paths import resolve_runtime_root
from src.application.strategy_lab.contracts import StrategyExperiment, StrategyPolicy, validate_strategy_type
from src.application.strategy_lab.dataset_collect import collect_strategy_lab_dataset
from src.application.strategy_lab.experiment_engine import run_strategy_lab_experiment
from src.application.strategy_lab.experiment_report import build_strategy_lab_experiment_report
from src.application.strategy_lab.evidence_loader import load_strategy_lab_evidence
from src.application.strategy_lab.historical_data.cache import (
    historical_snapshots_summary,
    load_historical_data_snapshots,
)
from src.application.strategy_lab.report import build_strategy_lab_report
from src.application.strategy_lab.simulator import run_replay_backtest
from src.application.strategy_lab.storage import StrategyLabStorage


SCHEMA_VERSION = "strategy_lab.v1"
CURRENT_SCHEMA_VERSION = "strategy_lab_current.v1"


def strategy_lab_dataset_collect_tool(
    payload: Mapping[str, Any],
    *,
    base: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    payload_dict = dict(payload)
    repo_root = Path(base or Path.cwd()).resolve()
    runtime_root = _runtime_root(payload_dict, repo_root=repo_root)
    storage = StrategyLabStorage(runtime_root)
    dataset = collect_strategy_lab_dataset(payload_dict, repo_root=repo_root, runtime_root=runtime_root, now_fn=now_fn)
    dry_run = _dry_run(payload_dict)
    dataset_path = storage.dataset_path(dataset.dataset_id)
    if not dry_run:
        dataset_path = storage.write_dataset(dataset)
    data = {
        "schema_version": "strategy_lab_dataset_collect.v1",
        "dry_run": dry_run,
        "write_applied": not dry_run,
        "backup_path": None,
        "audit_id": None,
        "rollback_hint": (
            None
            if dry_run
            else "Remove the written Strategy Lab dataset file if this collected dataset should not be used."
        ),
        "dataset": dataset.to_dict(),
        "output": {
            "written": not dry_run,
            "dataset_path": storage.relative(dataset_path),
        },
    }
    return data, list(dataset.warnings), {"runtime_root": mask_path(runtime_root), "repo_root": mask_path(repo_root)}


def strategy_lab_experiment_tool(
    payload: Mapping[str, Any],
    *,
    base: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    payload_dict = dict(payload)
    repo_root = Path(base or Path.cwd()).resolve()
    runtime_root = _runtime_root(payload_dict, repo_root=repo_root)
    storage = StrategyLabStorage(runtime_root)
    dry_run = _dry_run(payload_dict)
    dataset_id = str(payload_dict.get("dataset_id") or "").strip()
    dataset_written = False
    if dataset_id:
        dataset = storage.read_dataset(dataset_id)
    else:
        dataset = collect_strategy_lab_dataset(payload_dict, repo_root=repo_root, runtime_root=runtime_root, now_fn=now_fn)
        if not dry_run:
            storage.write_dataset(dataset)
            dataset_written = True
    result = run_strategy_lab_experiment(payload_dict, dataset=dataset, base=repo_root)
    report = build_strategy_lab_experiment_report(result, dataset=dataset)
    output = {
        "written": False,
        "dataset_written": dataset_written,
        "dataset_path": storage.relative(storage.dataset_path(dataset.dataset_id)),
        "result_path": None,
        "report_path": None,
        "current_path": storage.relative(storage.current_path()),
    }
    if not dry_run:
        output.update(storage.write_experiment(result, report_markdown=str(report["markdown"])))
        output["written"] = True
    data = {
        "schema_version": "strategy_lab_experiment_run.v1",
        "dry_run": dry_run,
        "write_applied": not dry_run,
        "backup_path": None,
        "audit_id": None,
        "rollback_hint": (
            None
            if dry_run
            else "Remove the written Strategy Lab experiment/report files and current pointer if this result should be discarded."
        ),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "summary": dataset.summary(),
            "scope": dict(dataset.scope),
        },
        "result": result,
        "report": report,
        "output": output,
    }
    warnings = list(dict.fromkeys([*dataset.warnings, *result.get("warnings", [])]))
    return data, warnings, {"runtime_root": mask_path(runtime_root), "repo_root": mask_path(repo_root)}


def strategy_lab_current_tool(
    payload: Mapping[str, Any],
    *,
    base: Path | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    payload_dict = dict(payload)
    repo_root = Path(base or Path.cwd()).resolve()
    runtime_root = _runtime_root(payload_dict, repo_root=repo_root)
    storage = StrategyLabStorage(runtime_root)
    current = storage.read_current()
    return {
        "schema_version": "strategy_lab_current_read.v1",
        "current": current,
    }, [], {"runtime_root": mask_path(runtime_root), "repo_root": mask_path(repo_root)}


def _runtime_root(payload: dict[str, Any], *, repo_root: Path) -> Path:
    return resolve_runtime_root(
        repo_root=repo_root,
        runtime_root=payload.get("runtime_root"),
    ).runtime_root


def _dry_run(payload: dict[str, Any]) -> bool:
    if _truthy(payload.get("dry_run")):
        return True
    if _truthy(payload.get("confirm")) or _truthy(payload.get("apply")) or _truthy(payload.get("yes")):
        return False
    return True


def strategy_lab_tool(
    payload: Mapping[str, Any],
    *,
    base: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Run a deterministic Strategy Lab experiment.

    This is an application boundary, not an optimizer. It does not mutate live
    strategy config, trade events, positions, notifications, or broker state.
    """

    payload_dict = dict(payload)
    repo_root = Path(base or Path.cwd()).resolve()
    experiment = _experiment_from_payload(payload_dict)
    evidence = load_strategy_lab_evidence(
        candidate_paths=_path_list(payload_dict.get("candidate_paths") or payload_dict.get("candidate_path")),
        reject_log_paths=_path_list(payload_dict.get("reject_log_paths") or payload_dict.get("reject_log_path")),
        trace_paths=_path_list(payload_dict.get("trace_paths") or payload_dict.get("trace_path")),
        replay_paths=_path_list(
            payload_dict.get("strategy_replay_paths")
            or payload_dict.get("strategy_replay_path")
            or payload_dict.get("replay_paths")
            or payload_dict.get("replay_path")
        ),
        base=repo_root,
        sample_limit=_positive_int(payload_dict.get("sample_limit"), default=5),
    )
    result = run_replay_backtest(experiment, evidence)
    report = build_strategy_lab_report(result)
    historical_snapshots, historical_warnings = load_historical_data_snapshots(
        _path_list(
            payload_dict.get("historical_snapshot_paths")
            or payload_dict.get("historical_snapshot_path")
            or payload_dict.get("historical_data_paths")
            or payload_dict.get("historical_data_path")
        ),
        base=repo_root,
    )
    historical_data = historical_snapshots_summary(historical_snapshots)
    output = _write_outputs(
        payload_dict,
        base=repo_root,
        result=result.to_dict(),
        report_markdown=report.markdown,
        historical_data=historical_data,
        now_fn=now_fn,
    )
    data = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment.experiment_id,
        "strategy_type": experiment.strategy_type,
        "dry_run": not output["written"],
        "write_applied": bool(output["written"]),
        "backup_path": None,
        "audit_id": None,
        "rollback_hint": output.get("rollback_hint"),
        "result": result.to_dict(),
        "report": report.to_dict(),
        "historical_data": historical_data,
        "output": output,
    }
    warnings = [*result.warnings, *historical_warnings]
    return data, warnings, {"base": mask_path(repo_root)}


def _experiment_from_payload(payload: dict[str, Any]) -> StrategyExperiment:
    strategy_type = validate_strategy_type(str(payload.get("strategy_type") or "sell_put"))
    experiment_id = str(payload.get("experiment_id") or "").strip() or _default_experiment_id(strategy_type=strategy_type)
    return StrategyExperiment(
        experiment_id=experiment_id,
        strategy_type=strategy_type,
        account=_text(payload.get("account"), lower=True),
        start_date=_text(payload.get("start_date")),
        end_date=_text(payload.get("end_date")),
        requested_metrics=tuple(_path_list(payload.get("requested_metrics"))),
        baseline_policy=_policy(
            payload.get("baseline_policy"),
            default_name="baseline",
            strategy_type=strategy_type,
            default_params={"selection_source": "existing"},
            fallback_params=payload.get("baseline_params"),
        ),
        candidate_policy=_policy(
            payload.get("candidate_policy"),
            default_name="candidate",
            strategy_type=strategy_type,
            default_params={"selection_source": "rules"},
            fallback_params=payload.get("candidate_params") or payload.get("policy_params"),
        ),
    )


def _policy(
    value: Any,
    *,
    default_name: str,
    strategy_type: str,
    default_params: dict[str, Any],
    fallback_params: Any = None,
) -> StrategyPolicy:
    payload = value if isinstance(value, dict) else {}
    params = dict(default_params)
    explicit_params = payload.get("params") if isinstance(payload.get("params"), dict) else None
    if isinstance(fallback_params, dict):
        params.update(fallback_params)
    if explicit_params:
        params.update(explicit_params)
    name = str(payload.get("name") or default_name).strip() or default_name
    policy_strategy_type = validate_strategy_type(str(payload.get("strategy_type") or strategy_type))
    return StrategyPolicy(name=name, strategy_type=policy_strategy_type, params=params)


def _write_outputs(
    payload: dict[str, Any],
    *,
    base: Path,
    result: dict[str, Any],
    report_markdown: str,
    historical_data: dict[str, Any],
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    if not _truthy(payload.get("write_outputs")):
        return {
            "written": False,
            "result_path": None,
            "report_path": None,
            "current_path": None,
            "rollback_hint": None,
        }
    if not _truthy(payload.get("confirm")):
        raise AgentToolError(
            code="WRITE_CONFIRMATION_REQUIRED",
            message="strategy lab output writes require confirm=true",
            hint="Re-run with write_outputs=true and confirm=true to write local experiment outputs.",
        )
    output_dir = _resolve_output_path(payload.get("output_dir"), base=base, default=base / "output_shared" / "strategy_lab")
    current_dir = _resolve_output_path(payload.get("current_dir"), base=base, default=base / "output_shared" / "state" / "current")
    output_dir.mkdir(parents=True, exist_ok=True)
    current_dir.mkdir(parents=True, exist_ok=True)
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    experiment_id = str(result.get("experiment_id") or "experiment")
    stem = _safe_stem(experiment_id)
    result_path = output_dir / f"{stem}.result.json"
    report_path = output_dir / f"{stem}.md"
    current_path = current_dir / "strategy_lab.current.json"
    output_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "result": result,
        "historical_data": historical_data,
    }
    _write_json(result_path, output_payload)
    report_path.write_text(report_markdown, encoding="utf-8")
    _write_json(
        current_path,
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "experiment_id": experiment_id,
            "strategy_type": result.get("strategy_type"),
            "conclusion": result.get("conclusion"),
            "historical_snapshot_count": int(historical_data.get("snapshot_count") or 0),
            "result_path": _relative(result_path, base=base),
            "report_path": _relative(report_path, base=base),
        },
    )
    return {
        "written": True,
        "result_path": _relative(result_path, base=base),
        "report_path": _relative(report_path, base=base),
        "current_path": _relative(current_path, base=base),
        "rollback_hint": "Remove the listed Strategy Lab output files if this local experiment output is no longer needed.",
    }


def _resolve_output_path(value: Any, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    path = default.resolve() if not raw else Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="strategy lab output directories must stay under the repo root",
            details={"path": _relative(path, base=base)},
        ) from exc
    return path


def _path_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    raw = str(value).strip()
    if not raw:
        return []
    return [part.strip() for part in raw.replace("|", ",").split(",") if part.strip()]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _text(value: Any, *, lower: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.lower() if lower else text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _default_experiment_id(*, strategy_type: str) -> str:
    return f"{strategy_type}_replay"


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return stem.strip("._-") or "strategy_lab_experiment"


def _relative(path: Path, *, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f".../{path.name}" if path.name else "..."


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
