from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.shadow_replay import build_shadow_replay_dataset, mark_shadow_replay_dataset


SCHEMA_VERSION = "research_archive.v1"
DEFAULT_REMOTE = "prod"
DEFAULT_REMOTE_RUNTIME_ROOT = "/var/lib/options-monitor"
DEFAULT_REMOTE_REPO_ROOT = "/opt/options-monitor/current"
SYNC_RELATIVE_DIRS = ("output_shared/research", "output_shared/required_data")
LOGS_RELATIVE_DIR = "logs"

REMOTE_INVENTORY_SCRIPT = r"""
import json
import os
import sys
import time
from pathlib import Path

runtime = Path(sys.argv[1])
since_raw = sys.argv[2] if len(sys.argv) > 2 else ""
require_replay = (sys.argv[3].strip().lower() in {"1", "true", "yes"}) if len(sys.argv) > 3 else False
since_days = int(since_raw) if since_raw else None
cutoff = None if since_days is None else time.time() - max(since_days, 0) * 86400
runs_root = runtime / "output_runs"
runs = []

def relative_matches(root, patterns):
    out = []
    for pattern in patterns:
        out.extend(str(path.relative_to(root)) for path in root.rglob(pattern) if path.is_file())
    return sorted(set(out))

def read_json(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

def critical_files(run_dir):
    candidate_files = relative_matches(run_dir, ("*_candidates.csv", "*_candidates_labeled.csv"))
    trace_files = relative_matches(run_dir, ("candidate_filter_trace.jsonl",))
    reject_logs = relative_matches(run_dir, ("*_reject_log.csv", "*_candidates_reject_log.csv"))
    state_files = relative_matches(run_dir, ("last_run.json", "tick_metrics.json", "scheduler_decision.json"))
    return {
        "candidate_files": candidate_files,
        "trace_files": trace_files,
        "reject_log_files": reject_logs,
        "state_files": state_files,
    }

def ran_scan(run_dir):
    state = run_dir / "state"
    tick_metrics = read_json(state / "tick_metrics.json")
    last_run = read_json(state / "last_run.json")
    if tick_metrics.get("ran_scan") is True or last_run.get("ran_scan") is True:
        return True
    raw_accounts = tick_metrics.get("accounts")
    if isinstance(raw_accounts, list):
        return any(isinstance(item, dict) and item.get("ran_scan") is True for item in raw_accounts)
    if isinstance(raw_accounts, dict):
        return any(isinstance(item, dict) and item.get("ran_scan") is True for item in raw_accounts.values())
    return False

def scheduler_summary(run_dir):
    state = run_dir / "state"
    tick_metrics = read_json(state / "tick_metrics.json")
    scheduler_file = read_json(state / "scheduler_decision.json")
    scheduler = tick_metrics.get("scheduler_decision") if isinstance(tick_metrics.get("scheduler_decision"), dict) else {}
    if not scheduler:
        payload = scheduler_file.get("payload") if isinstance(scheduler_file.get("payload"), dict) else {}
        scheduler = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    return {
        "should_run_scan": scheduler.get("should_run_scan"),
        "should_notify": scheduler.get("should_notify"),
        "reason": scheduler.get("reason"),
        "next_run_utc": scheduler.get("next_run_utc"),
        "now_utc": scheduler.get("now_utc"),
    }

if runs_root.exists() and runs_root.is_dir():
    for item in sorted(runs_root.iterdir(), key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
        try:
            st = item.stat()
        except OSError:
            continue
        if not item.is_dir() or item.is_symlink():
            continue
        if cutoff is not None and st.st_mtime < cutoff:
            continue
        critical = critical_files(item)
        has_replay = bool(critical["candidate_files"] or critical["trace_files"] or critical["reject_log_files"])
        if require_replay and not has_replay:
            continue
        runs.append({
            "run_id": item.name,
            "mtime": st.st_mtime,
            "has_replay_evidence": has_replay,
            "ran_scan": ran_scan(item),
            "scheduler": scheduler_summary(item),
            "critical_files": critical,
        })
paths = {}
for rel in ("output_shared/research", "output_shared/required_data", "logs"):
    path = runtime / rel
    paths[rel] = {"exists": path.exists(), "is_dir": path.is_dir()}
print(json.dumps({"runtime_root": str(runtime), "runs_root": str(runs_root), "require_replay_evidence": require_replay, "runs": runs, "paths": paths}))
""".strip()


def archive_root_for(repo_root: Path, *, remote: str = DEFAULT_REMOTE, archive_root: str | Path | None = None) -> Path:
    base = repo_root.resolve()
    if archive_root is not None and str(archive_root).strip():
        return _resolve_path(archive_root, base=base)
    safe_remote = _safe_label(remote or DEFAULT_REMOTE)
    return (base / "output_shared" / "research" / "remote_archive" / safe_remote).resolve()


def archive_inventory(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    latest = _load_latest_inventory(root)
    runs = _run_inventory(root / "output_runs", base=base)
    manifests = _manifest_inventory(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "inventory",
        "ok": True,
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(root),
        "archive_root_exists": root.exists() and root.is_dir(),
        "summary": {
            "run_count": len(runs),
            "verified_run_count": sum(1 for item in runs if item.get("verified")),
            "replay_evidence_run_count": sum(1 for item in runs if item.get("has_replay_evidence")),
            "manifest_count": len(manifests),
            "latest_inventory_path": str(root / "manifests" / "inventory.latest.json"),
            "latest_inventory_exists": bool(latest),
        },
        "runs": runs,
        "manifests": manifests,
        "latest_inventory": latest,
    }


def archive_pull(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
    source_root: str | Path | None = None,
    ssh_target: str | None = None,
    remote_runtime_root: str | Path = DEFAULT_REMOTE_RUNTIME_ROOT,
    since_days: int | None = None,
    run_ids: list[str] | tuple[str, ...] | None = None,
    require_replay_evidence: bool = False,
    include_logs: bool = True,
    write: bool = False,
    rsync_path: str = "rsync",
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    source = _source_context(source_root=source_root, ssh_target=ssh_target, remote_runtime_root=remote_runtime_root)
    selected_runs, inventory_operation = _select_source_runs(
        source=source,
        since_days=since_days,
        run_ids=run_ids,
        require_replay_evidence=require_replay_evidence,
        run_cmd=run_cmd,
    )
    rel_dirs = [f"output_runs/{run_id}" for run_id in selected_runs]
    rel_dirs.extend(SYNC_RELATIVE_DIRS)
    if include_logs:
        rel_dirs.append(LOGS_RELATIVE_DIR)

    operations: list[dict[str, Any]] = []
    if inventory_operation:
        operations.append(inventory_operation)
    for rel in rel_dirs:
        destination = root / rel
        if write:
            destination.mkdir(parents=True, exist_ok=True)
        command = _rsync_command(
            rsync_path=rsync_path,
            source=_source_uri(source, rel),
            destination=destination,
            dry_run=not write,
        )
        operation = _run_command(command, run_cmd=run_cmd, timeout=600)
        if not operation.get("ok") and _optional_sync_dir(rel) and _rsync_missing_source(operation):
            operation = {**operation, "ok": True, "skipped": True, "reason": "source_dir_missing"}
        operations.append(operation)

    ok = all(bool(item.get("ok")) for item in operations)
    manifest: dict[str, Any] | None = None
    if write and ok:
        verify = archive_verify(repo_root=base, remote=remote, archive_root=root, now_fn=now_fn)
        manifest = _sync_manifest(
            repo_root=base,
            remote=remote,
            archive_root=root,
            source=source,
            since_days=since_days,
            selected_runs=selected_runs,
            require_replay_evidence=require_replay_evidence,
            include_logs=include_logs,
            operations=operations,
            verify=verify,
            now_fn=now_fn,
        )
        _write_sync_manifest(root, manifest)

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "pull",
        "ok": bool(ok),
        "changed": bool(write and ok),
        "dry_run": not bool(write),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(root),
        "source": _source_summary(source),
        "since_days": since_days,
        "require_replay_evidence": bool(require_replay_evidence),
        "selected_run_ids": selected_runs,
        "include_logs": bool(include_logs),
        "operations": operations,
        "manifest": manifest,
    }


def archive_verify(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    now = _now(now_fn)
    runs = _run_inventory(root / "output_runs", base=base)
    shared = {
        rel: _dir_payload(root / rel, base=base)
        for rel in (*SYNC_RELATIVE_DIRS, LOGS_RELATIVE_DIR)
    }
    data = {
        "schema_version": SCHEMA_VERSION,
        "action": "verify",
        "ok": root.exists() and root.is_dir(),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "verified_at_utc": now,
        "archive_root": str(root),
        "summary": {
            "run_count": len(runs),
            "verified_run_count": sum(1 for item in runs if item.get("verified")),
            "replay_evidence_run_count": sum(1 for item in runs if item.get("has_replay_evidence")),
            "shared_existing_count": sum(1 for item in shared.values() if item.get("exists")),
        },
        "runs": runs,
        "shared": shared,
    }
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    _write_json(manifests / "inventory.latest.json", data)
    return data


def archive_build_datasets(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
    dataset_root: str | Path | None = None,
    market: str | None = None,
    run_ids: list[str] | tuple[str, ...] | None = None,
    latest_scanned: bool = False,
    mark_from_run_required_data: bool = True,
    write: bool = False,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    inventory = _load_latest_inventory(root) or archive_verify(repo_root=base, remote=remote, archive_root=root)
    runs = [item for item in inventory.get("runs", []) if isinstance(item, dict)]
    selected = _select_verified_runs(runs, run_ids=run_ids, require_replay=True)
    market_filter = _filter_runs_by_market(selected, archive_root=root, market=market)
    selected = market_filter["selected"]
    if latest_scanned and selected:
        selected = sorted(selected, key=lambda item: str(item.get("mtime_utc") or ""), reverse=True)[:1]
    ds_root = _resolve_path(dataset_root, base=base) if dataset_root else (
        base / "output_shared" / "research" / "shadow_replay" / "datasets"
    ).resolve()
    plans: list[dict[str, Any]] = []
    built: list[dict[str, Any]] = []
    prefix = _dataset_prefix(remote=remote, market=market)
    for item in selected:
        run_id = str(item.get("run_id") or "").strip()
        dataset_id = _safe_label(f"{prefix}-{run_id}")
        plan = {
            "run_id": run_id,
            "dataset_id": dataset_id,
            "dataset_dir": str((ds_root / dataset_id).resolve()),
            "source_run_dir": str((root / "output_runs" / run_id).resolve()),
            "source_run_required_data_dir": str((root / "output_runs" / run_id / "required_data").resolve()),
            "mark_from_run_required_data": bool(mark_from_run_required_data),
            "inferred_market": item.get("inferred_market"),
        }
        plans.append(plan)
        if not write:
            continue
        try:
            manifest = build_shadow_replay_dataset(
                repo_root=base,
                runs_root=root / "output_runs",
                run_id=run_id,
                dataset_root=ds_root,
                dataset_id=dataset_id,
            )
            if mark_from_run_required_data:
                manifest = {
                    **manifest,
                    "post_build_marking": _mark_dataset_from_run_required_data(
                        repo_root=base,
                        dataset_dir=Path(str(manifest.get("dataset_dir") or plan["dataset_dir"])),
                        required_data_dir=root / "output_runs" / run_id / "required_data",
                        as_of=str(item.get("mtime_utc") or ""),
                    ),
                }
            built.append(manifest)
        except ValueError as exc:
            built.append({"run_id": run_id, "dataset_id": dataset_id, "ok": False, "error": str(exc)})
    ok = (
        all(bool(item.get("schema_version")) and _post_build_marking_ok(item) for item in built)
        if write
        else True
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "build-datasets",
        "ok": bool(ok),
        "changed": bool(write and built),
        "dry_run": not bool(write),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "mark_from_run_required_data": bool(mark_from_run_required_data),
        "market_filter": {
            "requested_market": _normalize_market(market),
            "selected_run_count": len(selected),
            "skipped_run_count": len(market_filter["skipped"]),
            "skipped_runs": market_filter["skipped"],
        },
        "archive_root": str(root),
        "dataset_root": str(ds_root),
        "selected_run_ids": [item["run_id"] for item in plans],
        "plans": plans,
        "built": built,
    }


def _mark_dataset_from_run_required_data(
    *,
    repo_root: Path,
    dataset_dir: Path,
    required_data_dir: Path,
    as_of: str,
) -> dict[str, Any]:
    required_root = required_data_dir.resolve()
    base_payload = {
        "required_data_root": str(required_root),
        "dataset_dir": str(dataset_dir.resolve()),
    }
    if not required_root.exists() or not required_root.is_dir():
        return {**base_payload, "ok": True, "status": "skipped", "reason": "run_required_data_missing"}
    if not _has_required_data_csv(required_root):
        return {**base_payload, "ok": True, "status": "skipped", "reason": "run_required_data_csv_missing"}
    try:
        marking = mark_shadow_replay_dataset(
            dataset=dataset_dir,
            required_data_root=required_root,
            as_of=as_of or None,
            repo_root=repo_root,
            write=True,
            replace=False,
        )
    except Exception as exc:
        return {
            **base_payload,
            "ok": False,
            "status": "error",
            "reason": "mark_from_run_required_data_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    payload = {
        **base_payload,
        "ok": True,
        "status": "marked",
        "reason": "marked_from_run_required_data",
        "summary": marking.get("summary") if isinstance(marking.get("summary"), dict) else {},
    }
    _annotate_dataset_manifest(dataset_dir, "mark_from_run_required_data", payload)
    return payload


def _has_required_data_csv(required_root: Path) -> bool:
    parsed = required_root / "parsed"
    source = parsed if parsed.exists() and parsed.is_dir() else required_root
    return any(path.is_file() for path in source.glob("*_required_data.csv"))


def _annotate_dataset_manifest(dataset_dir: Path, key: str, payload: dict[str, Any]) -> None:
    manifest_path = dataset_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return
    post_build = manifest.get("post_build")
    if not isinstance(post_build, dict):
        post_build = {}
    post_build[str(key)] = payload
    manifest["post_build"] = post_build
    _write_json(manifest_path, manifest)


def _post_build_marking_ok(item: dict[str, Any]) -> bool:
    marking = item.get("post_build_marking")
    if not isinstance(marking, dict):
        return True
    return bool(marking.get("ok"))


def archive_prune_remote(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
    ssh_target: str | None = None,
    remote_repo_root: str | Path = DEFAULT_REMOTE_REPO_ROOT,
    remote_runtime_root: str | Path = DEFAULT_REMOTE_RUNTIME_ROOT,
    keep_days: int = 3,
    keep_count: int = 30,
    include_logs: bool = True,
    confirm: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    target = _validate_ssh_target(ssh_target)
    latest = _load_latest_inventory(root)
    if not latest:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "prune-remote",
            "ok": False,
            "changed": False,
            "remote": _safe_label(remote or DEFAULT_REMOTE),
            "archive_root": str(root),
            "status": "missing_verified_inventory",
            "reason": "run archive verify before remote pruning",
        }
    verified_run_ids = {
        str(item.get("run_id"))
        for item in latest.get("runs", [])
        if isinstance(item, dict) and item.get("verified") and item.get("run_id")
    }
    dry_command = _remote_cleanup_command(
        ssh_target=target,
        remote_repo_root=remote_repo_root,
        remote_runtime_root=remote_runtime_root,
        keep_days=keep_days,
        keep_count=keep_count,
        include_logs=include_logs,
        confirm=False,
    )
    dry_operation = _run_command(dry_command, run_cmd=run_cmd, timeout=600, stdout_limit=2_000_000)
    dry_payload = _parse_cli_json(dry_operation.get("stdout"))
    planned_delete_runs = _planned_delete_runs(dry_payload)
    unverified = [run_id for run_id in planned_delete_runs if run_id not in verified_run_ids]
    guard = {
        "verified_inventory_path": str(root / "manifests" / "inventory.latest.json"),
        "verified_run_count": len(verified_run_ids),
        "planned_delete_run_ids": planned_delete_runs,
        "unverified_delete_run_ids": unverified,
        "confirmable": bool(dry_operation.get("ok")) and not unverified,
    }
    operations = [dry_operation]
    if confirm and guard["confirmable"]:
        confirm_command = _remote_cleanup_command(
            ssh_target=target,
            remote_repo_root=remote_repo_root,
            remote_runtime_root=remote_runtime_root,
            keep_days=keep_days,
            keep_count=keep_count,
            include_logs=include_logs,
            confirm=True,
        )
        operations.append(_run_command(confirm_command, run_cmd=run_cmd, timeout=900, stdout_limit=2_000_000))
    elif confirm and not guard["confirmable"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "prune-remote",
            "ok": False,
            "changed": False,
            "remote": _safe_label(remote or DEFAULT_REMOTE),
            "archive_root": str(root),
            "dry_run": False,
            "status": "remote_prune_guard_failed",
            "deletion_guard": guard,
            "operations": operations,
        }
    ok = all(bool(item.get("ok")) for item in operations)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "prune-remote",
        "ok": bool(ok),
        "changed": bool(confirm and ok and len(operations) > 1),
        "dry_run": not bool(confirm),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(root),
        "remote_repo_root": str(remote_repo_root),
        "remote_runtime_root": str(remote_runtime_root),
        "keep_days": max(0, int(keep_days)),
        "keep_count": max(1, int(keep_count)),
        "include_logs": bool(include_logs),
        "deletion_guard": guard,
        "operations": operations,
        "remote_cleanup_preview": dry_payload,
    }


def _select_source_runs(
    *,
    source: dict[str, Any],
    since_days: int | None,
    run_ids: list[str] | tuple[str, ...] | None,
    require_replay_evidence: bool,
    run_cmd: Callable[..., Any],
) -> tuple[list[str], dict[str, Any] | None]:
    explicit = [_safe_run_id(value) for value in (run_ids or []) if str(value or "").strip()]
    if explicit:
        return explicit, None
    if source["kind"] == "local":
        runs = _source_run_dirs(
            Path(source["runtime_root"]) / "output_runs",
            since_days=since_days,
            require_replay_evidence=require_replay_evidence,
        )
        return [item["run_id"] for item in runs], None
    operation, payload = _remote_inventory(
        source,
        since_days=since_days,
        require_replay_evidence=require_replay_evidence,
        run_cmd=run_cmd,
    )
    raw_runs = payload.get("runs") if isinstance(payload, dict) else []
    runs = raw_runs if isinstance(raw_runs, list) else []
    return [str(item.get("run_id")) for item in runs if isinstance(item, dict) and item.get("run_id")], operation


def _source_context(
    *,
    source_root: str | Path | None,
    ssh_target: str | None,
    remote_runtime_root: str | Path,
) -> dict[str, Any]:
    source_text = str(source_root).strip() if source_root is not None else ""
    target_text = str(ssh_target).strip() if ssh_target is not None else ""
    has_local = bool(source_text)
    has_remote = bool(target_text)
    if has_local == has_remote:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="archive pull requires exactly one of --source-root or --ssh-target",
        )
    if has_local:
        return {"kind": "local", "runtime_root": str(Path(source_text).expanduser().resolve())}
    return {
        "kind": "ssh",
        "ssh_target": _validate_ssh_target(ssh_target),
        "runtime_root": _validate_remote_path(remote_runtime_root),
    }


def _remote_inventory(
    source: dict[str, Any],
    *,
    since_days: int | None,
    require_replay_evidence: bool,
    run_cmd: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    remote_command = " ".join(
        [
            "python3",
            "-c",
            shlex.quote(REMOTE_INVENTORY_SCRIPT),
            shlex.quote(str(source["runtime_root"])),
            shlex.quote("" if since_days is None else str(max(0, int(since_days)))),
            shlex.quote("1" if require_replay_evidence else "0"),
        ]
    )
    command = ["ssh", str(source["ssh_target"]), remote_command]
    operation = _run_command(command, run_cmd=run_cmd, timeout=120, stdout_limit=2_000_000)
    payload = _parse_json(operation.get("stdout"))
    return operation, payload


def _source_run_dirs(
    runs_root: Path,
    *,
    since_days: int | None,
    require_replay_evidence: bool = False,
) -> list[dict[str, Any]]:
    if not runs_root.exists() or not runs_root.is_dir():
        return []
    cutoff = None
    if since_days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - max(0, int(since_days)) * 86400
    out: list[dict[str, Any]] = []
    for item in sorted(runs_root.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
        if not item.is_dir() or item.is_symlink():
            continue
        mtime = item.stat().st_mtime
        if cutoff is not None and mtime < cutoff:
            continue
        critical = _critical_files(item)
        has_replay = bool(critical["candidate_files"] or critical["trace_files"] or critical["reject_log_files"])
        if require_replay_evidence and not has_replay:
            continue
        out.append(
            {
                "run_id": item.name,
                "mtime": mtime,
                "has_replay_evidence": has_replay,
                "critical_files": critical,
            }
        )
    return out


def _rsync_command(*, rsync_path: str, source: str, destination: Path, dry_run: bool) -> list[str]:
    command = [str(rsync_path or "rsync"), "-az", "--partial", "--stats"]
    if dry_run:
        command.append("--dry-run")
    command.extend([source, str(destination) + "/"])
    return command


def _optional_sync_dir(rel: str) -> bool:
    return rel in {*SYNC_RELATIVE_DIRS, LOGS_RELATIVE_DIR}


def _rsync_missing_source(operation: dict[str, Any]) -> bool:
    stderr = str(operation.get("stderr") or "").lower()
    if "no such file or directory" not in stderr:
        return False
    return any(token in stderr for token in ("(l)stat", "change_dir", "link_stat"))


def _source_uri(source: dict[str, Any], rel: str) -> str:
    rel_path = rel.strip("/")
    if source["kind"] == "local":
        return str((Path(source["runtime_root"]) / rel_path).resolve()) + "/"
    return f"{source['ssh_target']}:{str(source['runtime_root']).rstrip('/')}/{rel_path}/"


def _sync_manifest(
    *,
    repo_root: Path,
    remote: str,
    archive_root: Path,
    source: dict[str, Any],
    since_days: int | None,
    selected_runs: list[str],
    require_replay_evidence: bool,
    include_logs: bool,
    operations: list[dict[str, Any]],
    verify: dict[str, Any],
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    generated_at = _now(now_fn)
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "sync",
        "generated_at_utc": generated_at,
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(archive_root),
        "archive_root_display": _display_path(archive_root, base=repo_root),
        "source": _source_summary(source),
        "since_days": since_days,
        "require_replay_evidence": bool(require_replay_evidence),
        "selected_run_ids": selected_runs,
        "include_logs": bool(include_logs),
        "operation_count": len(operations),
        "operation_ok": all(bool(item.get("ok")) for item in operations),
        "verify_summary": verify.get("summary"),
    }


def _write_sync_manifest(root: Path, manifest: dict[str, Any]) -> None:
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    stamp = str(manifest.get("generated_at_utc") or _now(None)).replace(":", "").replace("-", "")
    path = manifests / f"sync-{stamp}.json"
    _write_json(path, manifest)


def _run_inventory(runs_root: Path, *, base: Path) -> list[dict[str, Any]]:
    if not runs_root.exists() or not runs_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for run_dir in sorted(
        [item.resolve() for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()],
        key=lambda path: (_mtime_utc(path), path.name),
        reverse=True,
    ):
        files = [path for path in run_dir.rglob("*") if path.is_file() and not path.is_symlink()]
        critical = _critical_files(run_dir)
        has_replay = bool(critical["candidate_files"] or critical["trace_files"] or critical["reject_log_files"])
        out.append(
            {
                "run_id": run_dir.name,
                "path": str(run_dir),
                "path_display": _display_path(run_dir, base=base),
                "mtime_utc": _mtime_utc(run_dir),
                "file_count": len(files),
                "size_bytes": sum(_file_size(path) for path in files),
                "metadata_digest": _metadata_digest(run_dir, files),
                "verified": bool(files),
                "has_replay_evidence": has_replay,
                "critical_files": critical,
            }
        )
    return out


def _critical_files(run_dir: Path) -> dict[str, Any]:
    candidate_files = _relative_matches(run_dir, ("*_candidates.csv", "*_candidates_labeled.csv"))
    trace_files = _relative_matches(run_dir, ("candidate_filter_trace.jsonl",))
    reject_logs = _relative_matches(run_dir, ("*_reject_log.csv", "*_candidates_reject_log.csv"))
    state_files = _relative_matches(run_dir, ("last_run.json", "tick_metrics.json", "scheduler_decision.json"))
    return {
        "candidate_files": candidate_files,
        "trace_files": trace_files,
        "reject_log_files": reject_logs,
        "state_files": state_files,
    }


def _relative_matches(root: Path, patterns: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pattern in patterns:
        out.extend(str(path.relative_to(root)) for path in root.rglob(pattern) if path.is_file())
    return sorted(set(out))


def _dir_payload(path: Path, *, base: Path) -> dict[str, Any]:
    exists = path.exists() and path.is_dir()
    files = [item for item in path.rglob("*") if item.is_file() and not item.is_symlink()] if exists else []
    return {
        "path": str(path),
        "path_display": _display_path(path, base=base),
        "exists": exists,
        "file_count": len(files),
        "size_bytes": sum(_file_size(item) for item in files),
    }


def _manifest_inventory(root: Path) -> list[dict[str, Any]]:
    manifests = root / "manifests"
    if not manifests.exists() or not manifests.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(manifests.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name), reverse=True):
        out.append({"path": str(path), "name": path.name, "mtime_utc": _mtime_utc(path), "size_bytes": _file_size(path)})
    return out


def _select_verified_runs(
    runs: list[dict[str, Any]],
    *,
    run_ids: list[str] | tuple[str, ...] | None,
    require_replay: bool,
) -> list[dict[str, Any]]:
    wanted = {_safe_run_id(value) for value in (run_ids or []) if str(value or "").strip()}
    selected: list[dict[str, Any]] = []
    for item in runs:
        run_id = str(item.get("run_id") or "")
        if wanted and run_id not in wanted:
            continue
        if not item.get("verified"):
            continue
        if require_replay and not item.get("has_replay_evidence"):
            continue
        selected.append(item)
    return selected


def _filter_runs_by_market(
    runs: list[dict[str, Any]],
    *,
    archive_root: Path,
    market: str | None,
) -> dict[str, Any]:
    requested = _normalize_market(market)
    if requested is None:
        return {"selected": runs, "skipped": []}
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in runs:
        run_id = str(item.get("run_id") or "").strip()
        inferred = _infer_run_market(item, run_dir=archive_root / "output_runs" / run_id)
        if inferred == requested:
            selected.append({**item, "inferred_market": inferred})
            continue
        skipped.append(
            {
                "run_id": run_id,
                "inferred_market": inferred,
                "reason": "market_mismatch" if inferred in {"us", "hk"} else "market_unknown_or_mixed",
            }
        )
    return {"selected": selected, "skipped": skipped}


def _normalize_market(market: str | None) -> str | None:
    text = str(market or "").strip().lower()
    return text if text in {"us", "hk"} else None


def _infer_run_market(item: dict[str, Any], *, run_dir: Path) -> str:
    critical = item.get("critical_files") if isinstance(item.get("critical_files"), dict) else _critical_files(run_dir)
    file_names: list[str] = []
    for key in ("candidate_files", "reject_log_files"):
        raw = critical.get(key) if isinstance(critical, dict) else None
        if isinstance(raw, list):
            file_names.extend(str(value).lower() for value in raw)
    markets: set[str] = set()
    has_hk = any(".hk_" in name or ".hk-" in name or ".hk." in name for name in file_names)
    has_us = any(
        ("_sell_put_candidates" in name or "_sell_call_candidates" in name or "_yield_enhancement" in name)
        and ".hk_" not in name
        and ".hk-" not in name
        and ".hk." not in name
        for name in file_names
    )
    if has_hk:
        markets.add("hk")
    if has_us:
        markets.add("us")
    raw_trace_files = critical.get("trace_files") if isinstance(critical, dict) else None
    if isinstance(raw_trace_files, list):
        markets.update(_infer_markets_from_trace_files(run_dir, raw_trace_files))
    if len(markets) > 1:
        return "mixed"
    if markets:
        return next(iter(markets))
    return "unknown"


def _infer_markets_from_trace_files(run_dir: Path, trace_files: list[Any]) -> set[str]:
    markets: set[str] = set()
    run_root = run_dir.resolve()
    for raw in trace_files:
        path = (run_root / str(raw)).resolve()
        try:
            path.relative_to(run_root)
        except ValueError:
            continue
        try:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    market = _infer_market_from_trace_row(row)
                    if market is not None:
                        markets.add(market)
                    if len(markets) > 1:
                        return markets
        except OSError:
            continue
    return markets


def _infer_market_from_trace_row(row: dict[str, Any]) -> str | None:
    values = [
        str(row.get("symbol") or "").strip(),
        str(row.get("underlying_symbol") or "").strip(),
        str(row.get("contract_symbol") or "").strip(),
        str(row.get("option_symbol") or "").strip(),
    ]
    non_empty = [value.upper() for value in values if value]
    if not non_empty:
        return None
    if any(_looks_like_hk_identifier(value) for value in non_empty):
        return "hk"
    if any(_looks_like_us_identifier(value) for value in non_empty):
        return "us"
    return None


def _looks_like_hk_identifier(value: str) -> bool:
    return value.endswith(".HK") or value.startswith("HK.") or ".HK_" in value or ".HK-" in value


def _looks_like_us_identifier(value: str) -> bool:
    if _looks_like_hk_identifier(value):
        return False
    return any("A" <= char <= "Z" for char in value)


def _remote_cleanup_command(
    *,
    ssh_target: str,
    remote_repo_root: str | Path,
    remote_runtime_root: str | Path,
    keep_days: int,
    keep_count: int,
    include_logs: bool,
    confirm: bool,
) -> list[str]:
    args = [
        "./om",
        "service",
        "cleanup",
        "--repo-root",
        str(remote_repo_root),
        "--runtime-root",
        str(remote_runtime_root),
        "--cleanup-output-runs",
        "--output-runs-keep-days",
        str(max(0, int(keep_days))),
        "--output-runs-keep-count",
        str(max(1, int(keep_count))),
    ]
    if include_logs:
        args.extend(["--cleanup-runtime-logs", "--runtime-logs-keep-days", str(max(0, int(keep_days)))])
    if confirm:
        args.extend(["--confirm", "--yes"])
    remote_command = "cd " + shlex.quote(str(remote_repo_root)) + " && " + " ".join(shlex.quote(arg) for arg in args)
    return ["ssh", ssh_target, remote_command]


def _planned_delete_runs(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    cleanup = data.get("output_runs_cleanup") if isinstance(data, dict) else {}
    rows = cleanup.get("delete_runs") if isinstance(cleanup, dict) else []
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw_path = str(row.get("path") or "").rstrip("/")
        if raw_path:
            out.append(Path(raw_path).name)
    return out


def _run_command(
    command: list[str],
    *,
    run_cmd: Callable[..., Any],
    timeout: int,
    stdout_limit: int | None = 4000,
    stderr_limit: int | None = 4000,
) -> dict[str, Any]:
    try:
        proc = run_cmd(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        return {
            "command": command,
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    return {
        "command": command,
        "ok": int(getattr(proc, "returncode", 1)) == 0,
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout": _limit_text(str(getattr(proc, "stdout", "") or ""), stdout_limit),
        "stderr": _limit_text(str(getattr(proc, "stderr", "") or ""), stderr_limit),
    }


def _limit_text(value: str, limit: int | None) -> str:
    if limit is None or int(limit) <= 0:
        return value
    return value[-int(limit):]


def _parse_cli_json(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"parse_error": "stdout was not valid JSON", "stdout_tail": text[-1000:]}
    return payload if isinstance(payload, dict) else {}


def _parse_json(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_latest_inventory(root: Path) -> dict[str, Any]:
    path = root / "manifests" / "inventory.latest.json"
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _metadata_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        try:
            rel = path.relative_to(root).as_posix()
            st = path.stat()
        except OSError:
            continue
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def _now(now_fn: Callable[[], datetime] | None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_path(value: str | Path | None, *, base: Path) -> Path:
    if value is None:
        return base
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def _display_path(path: Path, *, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def _safe_label(value: Any) -> str:
    text = str(value or DEFAULT_REMOTE).strip().lower().replace("_", "-")
    allowed = []
    for ch in text:
        allowed.append(ch if ch.isalnum() or ch in {"-", "."} else "-")
    out = "".join(allowed).strip("-.")
    return out or DEFAULT_REMOTE


def _safe_run_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or Path(text).name != text or text.startswith("."):
        raise AgentToolError(code="INPUT_ERROR", message=f"unsafe run id: {value}")
    return text


def _dataset_prefix(*, remote: str, market: str | None) -> str:
    parts = [_safe_label(remote or DEFAULT_REMOTE)]
    if market:
        parts.append(_safe_label(market))
    return "-".join(parts)


def _validate_ssh_target(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="INPUT_ERROR", message="ssh target is required")
    if text.startswith("-") or any(ch.isspace() for ch in text):
        raise AgentToolError(code="INPUT_ERROR", message=f"unsafe ssh target: {text}")
    return text


def _validate_remote_path(value: str | Path) -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or "\n" in text or "\r" in text:
        raise AgentToolError(code="INPUT_ERROR", message=f"remote path must be absolute: {text}")
    return text.rstrip("/") or "/"


def _source_summary(source: dict[str, Any]) -> dict[str, Any]:
    out = {"kind": source.get("kind"), "runtime_root": source.get("runtime_root")}
    if source.get("ssh_target"):
        out["ssh_target"] = source.get("ssh_target")
    return out


__all__ = [
    "archive_build_datasets",
    "archive_inventory",
    "archive_prune_remote",
    "archive_pull",
    "archive_root_for",
    "archive_verify",
]
