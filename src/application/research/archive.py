from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.candidate_evidence_history import (
    NOT_SCANNED,
    summarize_run_candidate_evidence,
)
from src.application.shadow_replay import build_shadow_replay_dataset, mark_shadow_replay_dataset
from src.application.required_data_blobs import (
    RequiredDataBlobError,
    load_required_data_scan_blob,
    required_data_scan_blob_ref_identity,
    validate_required_data_scan_blob_ref,
)


SCHEMA_VERSION = "research_archive.v2"
DEFAULT_REMOTE = "prod"
DEFAULT_REMOTE_RUNTIME_ROOT = "/var/lib/options-monitor"
DEFAULT_REMOTE_REPO_ROOT = "/opt/options-monitor/current"
SYNC_RELATIVE_DIRS = ("output_shared/research", "output_shared/required_data")
LOGS_RELATIVE_DIR = "logs"

REMOTE_INVENTORY_SCRIPT = r"""
import json
import os
import hashlib
import socket
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

def scan_blob_refs(run_dir):
    manifest_path = run_dir / "state" / "required_data_snapshot_manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return [], "absent", None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return [], "invalid", "required-data snapshot manifest is unsafe"
    manifest = read_json(manifest_path)
    symbols = manifest.get("symbols")
    if not isinstance(symbols, dict):
        return [], "invalid", "required-data snapshot symbols are invalid"
    refs = {}
    try:
        for entry in symbols.values():
            if not isinstance(entry, dict) or "scan_blob_ref" not in entry:
                continue
            ref = entry["scan_blob_ref"]
            digest = str(ref.get("blob_sha256") or "") if isinstance(ref, dict) else ""
            relpath = str(ref.get("blob_relpath") or "") if isinstance(ref, dict) else ""
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("blob hash is invalid")
            if relpath != f"output_shared/blobs/sha256/{digest[:2]}/{digest}.json.gz":
                raise ValueError("blob path is invalid")
            previous = refs.get(digest)
            stable = {key: value for key, value in ref.items() if key != "published_at_utc"}
            previous_stable = (
                {key: value for key, value in previous.items() if key != "published_at_utc"}
                if isinstance(previous, dict)
                else None
            )
            if previous_stable is not None and previous_stable != stable:
                raise ValueError("blob references conflict")
            if previous is None or str(ref.get("published_at_utc") or "") > str(previous.get("published_at_utc") or ""):
                refs[digest] = ref
    except (TypeError, ValueError) as exc:
        return [], "invalid", str(exc)
    return [refs[key] for key in sorted(refs)], "ready", None

def critical_files(run_dir):
    candidate_manifests = relative_matches(run_dir, ("candidate_snapshot_manifest.v1.json",))
    candidate_snapshots = relative_matches(run_dir, ("opening_candidate_snapshot.json", "combo_yield_candidate_snapshot.json", "cc_lp_candidate_snapshot.json"))
    candidate_status = relative_matches(run_dir, ("strategy_scan_status_index.v1.json", "strategy_scan_status_index.v2.json"))
    trace_files = relative_matches(run_dir, ("candidate_filter_trace.jsonl",))
    legacy_candidate_files = relative_matches(run_dir, ("*_candidates.csv", "*_candidates_labeled.csv", "*_candidates_reject_log.csv", "*_reject_log.csv", "*_pair_diagnostics.csv", "*_rank_shadow.csv", "*_put_universe.csv", "*_put_universe_labeled.csv", "*_put_universe_cash_filtered.csv", "*_put_universe_underwritten.csv"))
    state_files = relative_matches(run_dir, ("last_run.json", "tick_metrics.json", "scheduler_decision.json"))
    return {
        "candidate_manifest_files": candidate_manifests,
        "candidate_snapshot_files": candidate_snapshots,
        "candidate_status_files": candidate_status,
        "trace_files": trace_files,
        "legacy_candidate_files": legacy_candidate_files,
        "state_files": state_files,
    }

def has_replay_evidence(critical):
    return bool(
        critical["candidate_manifest_files"]
        or critical["candidate_snapshot_files"]
        or critical["candidate_status_files"]
        or critical["trace_files"]
    )

def file_manifest(run_dir):
    rows = []
    for path in sorted(item for item in run_dir.rglob("*") if item.is_file() and not item.is_symlink()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        rows.append({
            "path": str(path.relative_to(run_dir)),
            "size_bytes": stat.st_size,
            "sha256": digest.hexdigest(),
        })
    return rows

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
        files = file_manifest(item)
        blob_refs, blob_status, blob_error = scan_blob_refs(item)
        has_replay = has_replay_evidence(critical)
        if require_replay and not has_replay:
            continue
        runs.append({
            "run_id": item.name,
            "mtime": st.st_mtime,
            "has_replay_evidence": has_replay,
            "has_legacy_candidate_metadata": bool(critical["legacy_candidate_files"]),
            "ran_scan": ran_scan(item),
            "scheduler": scheduler_summary(item),
            "critical_files": critical,
            "file_manifest": files,
            "content_digest": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "scan_blob_refs": blob_refs,
            "scan_blob_reference_status": blob_status,
            "scan_blob_reference_error": blob_error,
        })
paths = {}
for rel in ("output_shared/research", "output_shared/required_data", "logs"):
    path = runtime / rel
    paths[rel] = {"exists": path.exists(), "is_dir": path.is_dir()}
receipt_root = runtime / "output_shared/research/shadow_replay/receipts"
receipts = []
if receipt_root.exists() and receipt_root.is_dir() and not receipt_root.is_symlink():
    for path in sorted(receipt_root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        receipts.append({
            "path": str(path.relative_to(runtime)),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": digest.hexdigest(),
        })
print(json.dumps({"runtime_root": str(runtime), "runs_root": str(runs_root), "source_host": socket.getfqdn(), "require_replay_evidence": require_replay, "runs": runs, "shadow_replay_receipts": receipts, "paths": paths}))
""".strip()

REMOTE_RECEIPT_PRUNE_SCRIPT = r"""
import hashlib
import json
import sys
import time
from pathlib import Path

runtime = Path(sys.argv[1])
keep_days = max(0, int(sys.argv[2]))
keep_count = max(1, int(sys.argv[3]))
confirm = sys.argv[4] == "confirm"
expected = sys.argv[5].strip().lower() if len(sys.argv) > 5 else ""
root = runtime / "output_shared/research/shadow_replay/receipts"
blockers = []
rows = []
if root.is_symlink():
    blockers.append("receipt_root_is_symlink")
elif root.exists() and not root.is_dir():
    blockers.append("receipt_root_is_not_directory")
elif root.is_dir():
    for path in root.iterdir():
        if path.suffix != ".json":
            continue
        if path.is_symlink() or not path.is_file():
            blockers.append("unsafe_receipt_entry:" + path.name)
            continue
        stat = path.stat()
        rows.append({"path": str(path.relative_to(runtime)), "size_bytes": stat.st_size, "mtime": stat.st_mtime, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
rows.sort(key=lambda row: (row["mtime"], row["path"]), reverse=True)
cutoff = time.time() - keep_days * 86400
candidates = [row for index, row in enumerate(rows) if index >= keep_count and row["mtime"] < cutoff]
stable_candidates = [{key: row[key] for key in ("path", "size_bytes", "sha256")} for row in candidates]
plan = {"schema_version": "shadow_replay_receipt_prune.v1", "runtime_root": str(runtime), "keep_days": keep_days, "keep_count": keep_count, "candidates": stable_candidates}
plan_sha256 = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
payload = {**plan, "plan_sha256": plan_sha256, "blockers": blockers, "changed": False, "deleted_paths": []}
if confirm:
    if blockers:
        payload.update({"ok": False, "status": "blocked"})
    elif expected != plan_sha256:
        payload.update({"ok": False, "status": "plan_changed"})
    else:
        verified = True
        for row in stable_candidates:
            path = runtime / row["path"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row["size_bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                verified = False
                break
        if not verified:
            payload.update({"ok": False, "status": "content_changed"})
        else:
            for row in stable_candidates:
                (runtime / row["path"]).unlink()
            payload.update({"ok": True, "status": "applied", "changed": bool(stable_candidates), "deleted_paths": [row["path"] for row in stable_candidates]})
else:
    payload.update({"ok": not blockers, "status": "preview" if not blockers else "blocked"})
print(json.dumps(payload, sort_keys=True))
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
    (
        selected_runs,
        inventory_operation,
        source_run_inventory,
        source_inventory_metadata,
    ) = _select_source_runs(
        source=source,
        since_days=since_days,
        run_ids=run_ids,
        require_replay_evidence=require_replay_evidence,
        run_cmd=run_cmd,
    )
    if source_inventory_metadata.get("source_host"):
        source["source_host"] = source_inventory_metadata["source_host"]
    scan_blob_refs = _selected_scan_blob_refs(
        source=source,
        source_run_inventory=source_run_inventory,
    )
    rel_dirs = [f"output_runs/{run_id}" for run_id in selected_runs]
    rel_dirs.extend(SYNC_RELATIVE_DIRS)
    if include_logs:
        rel_dirs.append(LOGS_RELATIVE_DIR)

    operations: list[dict[str, Any]] = []
    staging_root = (
        root / ".staging" / f"pull-{uuid.uuid4().hex}"
        if write
        else None
    )
    if inventory_operation:
        operations.append(inventory_operation)
    sync_items = [(rel, True) for rel in rel_dirs]
    sync_items.extend((str(ref["blob_relpath"]), False) for ref in scan_blob_refs)
    for rel, is_directory in sync_items:
        if staging_root is not None and rel.startswith("output_runs/"):
            destination = staging_root / rel
        elif is_directory:
            destination = root / rel
        else:
            destination = (root / rel).parent
        if write:
            destination.mkdir(parents=True, exist_ok=True)
        command = _rsync_command(
            rsync_path=rsync_path,
            source=_source_uri(source, rel, directory=is_directory),
            destination=destination,
            dry_run=not write,
        )
        operation = _run_command(command, run_cmd=run_cmd, timeout=600)
        if not operation.get("ok") and _optional_sync_dir(rel) and _rsync_missing_source(operation):
            operation = {**operation, "ok": True, "skipped": True, "reason": "source_dir_missing"}
        operations.append(
            {
                **operation,
                "relative_path": rel,
                "sync_kind": "directory" if is_directory else "scan_blob",
            }
        )

    if write and all(bool(item.get("ok")) for item in operations):
        for ref in scan_blob_refs:
            try:
                load_required_data_scan_blob(runtime_root=root, blob_ref=ref)
                operation = {
                    "ok": True,
                    "sync_kind": "scan_blob_verify",
                    "relative_path": str(ref["blob_relpath"]),
                }
            except RequiredDataBlobError as exc:
                operation = {
                    "ok": False,
                    "sync_kind": "scan_blob_verify",
                    "relative_path": str(ref["blob_relpath"]),
                    "reason": "scan_blob_integrity_failed",
                    "error": str(exc),
                }
            operations.append(operation)

    ok = all(bool(item.get("ok")) for item in operations)
    manifest: dict[str, Any] | None = None
    if write and ok:
        assert staging_root is not None
        staged_verify = archive_verify(
            repo_root=base,
            remote=remote,
            archive_root=staging_root,
            now_fn=now_fn,
            source_identity=_source_summary(source),
            source_run_inventory=source_run_inventory,
        )
        deletion_verified_ids = {
            str(item.get("run_id"))
            for item in staged_verify.get("runs") or []
            if isinstance(item, dict) and item.get("deletion_verified")
        }
        if set(selected_runs) - deletion_verified_ids:
            ok = False
        if ok:
            for run_id in selected_runs:
                staged_run = staging_root / "output_runs" / run_id
                published_run = root / "output_runs" / run_id
                if published_run.exists():
                    staged_item = next(
                        (
                            item
                            for item in staged_verify.get("runs") or []
                            if isinstance(item, dict) and item.get("run_id") == run_id
                        ),
                        {},
                    )
                    live_item = next(
                        (
                            item
                            for item in _run_inventory(root / "output_runs", base=base)
                            if item.get("run_id") == run_id
                        ),
                        {},
                    )
                    if staged_item.get("content_digest") != live_item.get("content_digest"):
                        ok = False
                        operations.append(
                            {
                                "ok": False,
                                "reason": "archive_run_conflict",
                                "run_id": run_id,
                            }
                        )
                        break
                    shutil.rmtree(staged_run)
                    continue
                published_run.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_run, published_run)
        previous = _load_latest_inventory(root)
        previous_source = previous.get("source_identity")
        combined_source_inventory = list(source_run_inventory)
        if previous_source == _source_summary(source):
            known = {str(item.get("run_id")) for item in combined_source_inventory}
            combined_source_inventory.extend(
                item
                for item in previous.get("runs") or []
                if isinstance(item, dict)
                and item.get("deletion_verified")
                and str(item.get("run_id")) not in known
            )
        verify = archive_verify(
            repo_root=base,
            remote=remote,
            archive_root=root,
            now_fn=now_fn,
            source_identity=_source_summary(source),
            source_run_inventory=combined_source_inventory,
        )
        if ok:
            manifest = _sync_manifest(
                repo_root=base,
                remote=remote,
                archive_root=root,
                source=source,
                since_days=since_days,
                selected_runs=selected_runs,
                require_replay_evidence=require_replay_evidence,
                include_logs=include_logs,
                scan_blob_refs=scan_blob_refs,
                operations=operations,
                verify=verify,
                now_fn=now_fn,
            )
            _write_sync_manifest(root, manifest)
    if staging_root is not None and staging_root.exists():
        shutil.rmtree(staging_root)

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
        "scan_blob_refs": scan_blob_refs,
        "operations": operations,
        "manifest": manifest,
    }


def archive_verify(
    *,
    repo_root: str | Path,
    remote: str = DEFAULT_REMOTE,
    archive_root: str | Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
    source_identity: dict[str, Any] | None = None,
    source_run_inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve()
    root = archive_root_for(base, remote=remote, archive_root=archive_root)
    now = _now(now_fn)
    runs = _run_inventory(root / "output_runs", base=base)
    source_by_run = {
        str(item.get("run_id")): item
        for item in source_run_inventory or []
        if isinstance(item, dict) and item.get("run_id")
    }
    for item in runs:
        expected = source_by_run.get(str(item.get("run_id")))
        source_match = bool(
            expected
            and expected.get("content_digest")
            and expected.get("content_digest") == item.get("content_digest")
            and expected.get("file_manifest") == item.get("file_manifest")
        )
        item["source_content_verified"] = source_match
        item["deletion_verified"] = bool(item.get("verified")) and source_match
    shared = {
        rel: _dir_payload(root / rel, base=base)
        for rel in (*SYNC_RELATIVE_DIRS, LOGS_RELATIVE_DIR)
    }
    shadow_replay_receipts = _shadow_replay_receipt_inventory(root)
    data = {
        "schema_version": SCHEMA_VERSION,
        "action": "verify",
        "ok": root.exists() and root.is_dir(),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "verified_at_utc": now,
        "archive_root": str(root),
        "source_identity": dict(source_identity or {}),
        "summary": {
            "run_count": len(runs),
            "verified_run_count": sum(1 for item in runs if item.get("verified")),
            "deletion_verified_run_count": sum(
                1 for item in runs if item.get("deletion_verified")
            ),
            "replay_evidence_run_count": sum(1 for item in runs if item.get("has_replay_evidence")),
            "shared_existing_count": sum(1 for item in shared.values() if item.get("exists")),
        },
        "runs": runs,
        "shared": shared,
        "shadow_replay_receipts": shadow_replay_receipts,
    }
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    _write_json(manifests / "inventory.latest.json", data)
    return data


def _shadow_replay_receipt_inventory(root: Path) -> list[dict[str, Any]]:
    receipt_root = root / "output_shared/research/shadow_replay/receipts"
    if receipt_root.is_symlink() or not receipt_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(receipt_root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "sha256": digest.hexdigest(),
            }
        )
    return rows


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
    blob_refs, blob_status, blob_error = _run_scan_blob_refs(
        required_root.parent
    )
    base_payload = {
        "required_data_root": str(required_root),
        "dataset_dir": str(dataset_dir.resolve()),
        "scan_blob_refs": blob_refs,
        "scan_blob_reference_status": blob_status,
    }
    if blob_error:
        base_payload["scan_blob_reference_error"] = blob_error
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
    manifest_path = (
        required_root.parent
        / "state"
        / "required_data_snapshot_manifest.json"
    )
    if manifest_path.exists() or manifest_path.is_symlink():
        return True
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
    scope: str = "output-runs",
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
    source_identity = latest.get("source_identity")
    source_identity = source_identity if isinstance(source_identity, dict) else {}
    source = {
        "kind": "ssh",
        "ssh_target": target,
        "runtime_root": str(remote_runtime_root),
    }
    remote_inventory_operation, remote_inventory = _remote_inventory(
        source,
        since_days=None,
        require_replay_evidence=False,
        run_cmd=run_cmd,
    )
    remote_runs_raw = (
        remote_inventory.get("runs")
        if isinstance(remote_inventory, dict)
        else None
    )
    remote_runs = remote_runs_raw if isinstance(remote_runs_raw, list) else []
    current_remote_runs = {
        str(item.get("run_id")): item
        for item in remote_runs
        if isinstance(item, dict) and item.get("run_id")
    }
    current_source_host = str(
        remote_inventory.get("source_host")
        if isinstance(remote_inventory, dict)
        else ""
    ).strip()
    inventory_runtime_root = str(
        remote_inventory.get("runtime_root")
        if isinstance(remote_inventory, dict)
        else ""
    ).rstrip("/")
    remote_inventory_ok = (
        bool(remote_inventory_operation.get("ok"))
        and isinstance(remote_runs_raw, list)
        and bool(current_source_host)
        and inventory_runtime_root == str(remote_runtime_root).rstrip("/")
    )
    source_binding_ok = (
        remote_inventory_ok
        and source_identity.get("kind") == "ssh"
        and source_identity.get("ssh_target") == target
        and str(source_identity.get("runtime_root") or "").rstrip("/")
        == str(remote_runtime_root).rstrip("/")
        and str(source_identity.get("source_host") or "").strip()
        == current_source_host
    )
    normalized_scope = str(scope or "output-runs").strip().lower()
    if normalized_scope == "shadow-replay-receipts":
        return _archive_prune_remote_receipts(
            root=root,
            remote=remote,
            target=target,
            remote_runtime_root=remote_runtime_root,
            keep_days=keep_days,
            keep_count=keep_count,
            confirm=confirm,
            latest=latest,
            remote_inventory=remote_inventory,
            remote_inventory_operation=remote_inventory_operation,
            remote_inventory_ok=remote_inventory_ok,
            source_binding_ok=source_binding_ok,
            run_cmd=run_cmd,
        )
    if normalized_scope != "output-runs":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported archive prune scope: {scope}")
    current_runs = {
        str(item.get("run_id")): item
        for item in _run_inventory(root / "output_runs", base=base)
        if item.get("run_id")
    }
    verified_run_ids: set[str] = set()
    mutated_archive_run_ids: list[str] = []
    changed_remote_run_ids: list[str] = []
    for item in latest.get("runs", []):
        if not isinstance(item, dict) or not item.get("run_id"):
            continue
        run_id = str(item["run_id"])
        current = current_runs.get(run_id)
        content_matches = bool(
            current
            and current.get("content_digest")
            and current.get("content_digest") == item.get("content_digest")
            and current.get("file_manifest") == item.get("file_manifest")
        )
        current_remote = current_remote_runs.get(run_id)
        remote_content_matches = bool(
            current_remote
            and current_remote.get("content_digest")
            and current_remote.get("content_digest") == item.get("content_digest")
            and current_remote.get("file_manifest") == item.get("file_manifest")
        )
        if (
            source_binding_ok
            and item.get("deletion_verified")
            and content_matches
            and remote_content_matches
        ):
            verified_run_ids.add(run_id)
        elif item.get("deletion_verified") and not content_matches:
            mutated_archive_run_ids.append(run_id)
        if item.get("deletion_verified") and not remote_content_matches:
            changed_remote_run_ids.append(run_id)
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
    preview_validation = _validate_cleanup_preview(
        dry_payload,
        remote_runtime_root=remote_runtime_root,
    )
    planned_delete_runs = list(preview_validation.get("planned_delete_run_ids") or [])
    plan_sha256 = str(preview_validation.get("plan_sha256") or "")
    unverified = [run_id for run_id in planned_delete_runs if run_id not in verified_run_ids]
    guard = {
        "verified_inventory_path": str(root / "manifests" / "inventory.latest.json"),
        "verified_run_count": len(verified_run_ids),
        "source_binding_ok": source_binding_ok,
        "remote_inventory_ok": remote_inventory_ok,
        "current_source_host": current_source_host or None,
        "source_identity": source_identity,
        "mutated_or_missing_archive_run_ids": sorted(mutated_archive_run_ids),
        "changed_or_missing_remote_run_ids": sorted(changed_remote_run_ids),
        "planned_delete_run_ids": planned_delete_runs,
        "plan_sha256": plan_sha256 or None,
        "preview_validation": preview_validation,
        "unverified_delete_run_ids": unverified,
        "confirmable": bool(dry_operation.get("ok"))
        and bool(preview_validation.get("ok"))
        and source_binding_ok
        and not unverified,
    }
    operations = [remote_inventory_operation, dry_operation]
    if confirm and guard["confirmable"]:
        confirm_command = _remote_cleanup_command(
            ssh_target=target,
            remote_repo_root=remote_repo_root,
            remote_runtime_root=remote_runtime_root,
            keep_days=keep_days,
            keep_count=keep_count,
            include_logs=include_logs,
            confirm=True,
            expected_output_runs_plan_sha256=plan_sha256,
        )
        confirm_operation = _run_command(
            confirm_command,
            run_cmd=run_cmd,
            timeout=900,
            stdout_limit=2_000_000,
        )
        confirm_payload = _parse_cli_json(confirm_operation.get("stdout"))
        confirm_validation = _validate_cleanup_confirm(
            confirm_payload,
            expected_plan_sha256=plan_sha256,
        )
        confirm_operation["payload_validation"] = confirm_validation
        confirm_operation["ok"] = bool(confirm_operation.get("ok")) and bool(
            confirm_validation.get("ok")
        )
        operations.append(confirm_operation)
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
        "scope": "output-runs",
        "ok": bool(ok),
        "changed": bool(confirm and ok and len(operations) > 1),
        "dry_run": not bool(confirm),
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(root),
        "remote_repo_root": str(remote_repo_root),
        "remote_runtime_root": str(remote_runtime_root),
        "keep_days": max(0, int(keep_days)),
        "keep_count": max(1, int(keep_count)),
        "include_logs": False,
        "include_logs_requested": bool(include_logs),
        "limitations": [
            "runtime_log_pruning_disabled_until_logs_have_source_bound_content_manifest_and_apply_time_cas"
        ]
        if include_logs
        else [],
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
) -> tuple[
    list[str],
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any],
]:
    explicit = [_safe_run_id(value) for value in (run_ids or []) if str(value or "").strip()]
    if source["kind"] == "local":
        runs = _source_run_dirs(
            Path(source["runtime_root"]) / "output_runs",
            since_days=since_days,
            require_replay_evidence=require_replay_evidence,
        )
        selected = [
            item for item in runs if not explicit or str(item.get("run_id")) in set(explicit)
        ]
        return [str(item["run_id"]) for item in selected], None, selected, {}
    operation, payload = _remote_inventory(
        source,
        since_days=since_days,
        require_replay_evidence=require_replay_evidence,
        run_cmd=run_cmd,
    )
    raw_runs = payload.get("runs") if isinstance(payload, dict) else []
    runs = raw_runs if isinstance(raw_runs, list) else []
    selected = [
        item
        for item in runs
        if isinstance(item, dict)
        and item.get("run_id")
        and (not explicit or str(item.get("run_id")) in set(explicit))
    ]
    return (
        [str(item.get("run_id")) for item in selected],
        operation,
        selected,
        {
            "runtime_root": payload.get("runtime_root"),
            "source_host": payload.get("source_host"),
        },
    )


def _selected_scan_blob_refs(
    *,
    source: dict[str, Any],
    source_run_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_hash: dict[str, dict[str, Any]] = {}
    for run in source_run_inventory:
        if run.get("scan_blob_reference_status") == "invalid":
            raise AgentToolError(
                code="SOURCE_INVENTORY_ERROR",
                message=(
                    f"invalid required-data blob refs for {run.get('run_id')}: "
                    f"{run.get('scan_blob_reference_error') or 'unknown error'}"
                ),
            )
        refs = run.get("scan_blob_refs") or []
        if not isinstance(refs, list):
            raise AgentToolError(
                code="SOURCE_INVENTORY_ERROR",
                message="required-data blob ref inventory is invalid",
            )
        for raw_ref in refs:
            try:
                ref = validate_required_data_scan_blob_ref(raw_ref)
            except (TypeError, RequiredDataBlobError) as exc:
                raise AgentToolError(
                    code="SOURCE_INVENTORY_ERROR",
                    message="required-data blob ref inventory is invalid",
                ) from exc
            digest = str(ref["blob_sha256"])
            previous = by_hash.get(digest)
            if previous is not None and (
                required_data_scan_blob_ref_identity(previous)
                != required_data_scan_blob_ref_identity(ref)
            ):
                raise AgentToolError(
                    code="SOURCE_INVENTORY_ERROR",
                    message="required-data blob ref inventory conflicts",
                )
            if previous is None or ref["published_at_utc"] > previous["published_at_utc"]:
                by_hash[digest] = ref
    refs = [by_hash[key] for key in sorted(by_hash)]
    if source["kind"] == "local":
        source_root = Path(source["runtime_root"])
        for ref in refs:
            try:
                load_required_data_scan_blob(
                    runtime_root=source_root,
                    blob_ref=ref,
                )
            except RequiredDataBlobError as exc:
                raise AgentToolError(
                    code="SOURCE_INVENTORY_ERROR",
                    message=(
                        "referenced required-data blob is missing or corrupt: "
                        f"{ref['blob_relpath']}"
                    ),
                ) from exc
    return refs


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
        files = _file_manifest(item)
        blob_refs, blob_status, blob_error = _run_scan_blob_refs(item)
        has_replay = _has_replay_evidence(critical)
        if require_replay_evidence and not has_replay:
            continue
        out.append(
            {
                "run_id": item.name,
                "mtime": mtime,
                "has_replay_evidence": has_replay,
                "has_legacy_candidate_metadata": bool(
                    critical["legacy_candidate_files"]
                ),
                "critical_files": critical,
                "file_manifest": files,
                "content_digest": _content_digest(files),
                "scan_blob_refs": blob_refs,
                "scan_blob_reference_status": blob_status,
                "scan_blob_reference_error": blob_error,
            }
        )
    return out


def _run_scan_blob_refs(
    run_dir: Path,
) -> tuple[list[dict[str, Any]], str, str | None]:
    manifest_path = run_dir / "state" / "required_data_snapshot_manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return [], "absent", None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return [], "invalid", "required-data snapshot manifest is unsafe"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, dict):
            raise ValueError("required-data snapshot symbols are invalid")
        by_hash: dict[str, dict[str, Any]] = {}
        for entry in symbols.values():
            if not isinstance(entry, dict) or "scan_blob_ref" not in entry:
                continue
            ref = validate_required_data_scan_blob_ref(entry["scan_blob_ref"])
            digest = str(ref["blob_sha256"])
            previous = by_hash.get(digest)
            if previous is not None and (
                required_data_scan_blob_ref_identity(previous)
                != required_data_scan_blob_ref_identity(ref)
            ):
                raise ValueError("required-data blob references conflict")
            if previous is None or ref["published_at_utc"] > previous["published_at_utc"]:
                by_hash[digest] = ref
        return [by_hash[key] for key in sorted(by_hash)], "ready", None
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RequiredDataBlobError,
    ) as exc:
        return [], "invalid", f"{type(exc).__name__}: {exc}"


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


def _source_uri(
    source: dict[str, Any],
    rel: str,
    *,
    directory: bool = True,
) -> str:
    rel_path = rel.strip("/")
    if source["kind"] == "local":
        suffix = "/" if directory else ""
        return str((Path(source["runtime_root"]) / rel_path).resolve()) + suffix
    suffix = "/" if directory else ""
    return f"{source['ssh_target']}:{str(source['runtime_root']).rstrip('/')}/{rel_path}{suffix}"


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
    scan_blob_refs: list[dict[str, Any]],
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
        "scan_blob_refs": scan_blob_refs,
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
        file_manifest = _file_manifest(run_dir)
        critical = _critical_files(run_dir)
        classification = summarize_run_candidate_evidence(
            base=runs_root.parent,
            run_id=run_dir.name,
            runs_root=runs_root,
        )
        has_replay = _has_replay_evidence(critical)
        has_candidate_history_metadata = any(
            row.get("status") != NOT_SCANNED
            for row in classification.get("accounts") or []
            if isinstance(row, dict)
        )
        has_partial = any(
            part.startswith(".") and ("partial" in part or "tmp" in part)
            for path in files
            for part in path.relative_to(run_dir).parts
        )
        blob_refs, blob_status, blob_error = _run_scan_blob_refs(run_dir)
        out.append(
            {
                "run_id": run_dir.name,
                "path": str(run_dir),
                "path_display": _display_path(run_dir, base=base),
                "mtime_utc": _mtime_utc(run_dir),
                "file_count": len(files),
                "size_bytes": sum(_file_size(path) for path in files),
                "metadata_digest": _metadata_digest(run_dir, files),
                "file_manifest": file_manifest,
                "content_digest": _content_digest(file_manifest),
                "verified": bool(files) and not has_partial,
                "partial_artifact_detected": has_partial,
                "has_replay_evidence": has_replay,
                "has_candidate_history_metadata": has_candidate_history_metadata,
                "has_legacy_candidate_metadata": bool(
                    critical["legacy_candidate_files"]
                ),
                "candidate_evidence": classification,
                "critical_files": critical,
                "scan_blob_refs": blob_refs,
                "scan_blob_reference_status": blob_status,
                "scan_blob_reference_error": blob_error,
            }
        )
    return out


def _critical_files(run_dir: Path) -> dict[str, Any]:
    candidate_manifests = _relative_matches(
        run_dir,
        ("candidate_snapshot_manifest.v1.json",),
    )
    candidate_snapshots = _relative_matches(
        run_dir,
        (
            "opening_candidate_snapshot.json",
            "combo_yield_candidate_snapshot.json",
            "cc_lp_candidate_snapshot.json",
        ),
    )
    candidate_status = _relative_matches(
        run_dir,
        (
            "strategy_scan_status_index.v1.json",
            "strategy_scan_status_index.v2.json",
        ),
    )
    trace_files = _relative_matches(run_dir, ("candidate_filter_trace.jsonl",))
    legacy_candidate_files = _relative_matches(
        run_dir,
        (
            "*_candidates.csv",
            "*_candidates_labeled.csv",
            "*_candidates_reject_log.csv",
            "*_reject_log.csv",
            "*_pair_diagnostics.csv",
            "*_rank_shadow.csv",
            "*_put_universe.csv",
            "*_put_universe_labeled.csv",
            "*_put_universe_cash_filtered.csv",
            "*_put_universe_underwritten.csv",
        ),
    )
    state_files = _relative_matches(run_dir, ("last_run.json", "tick_metrics.json", "scheduler_decision.json"))
    return {
        "candidate_manifest_files": candidate_manifests,
        "candidate_snapshot_files": candidate_snapshots,
        "candidate_status_files": candidate_status,
        "trace_files": trace_files,
        "legacy_candidate_files": legacy_candidate_files,
        "state_files": state_files,
    }


def _has_replay_evidence(critical: dict[str, Any]) -> bool:
    return any(
        bool(critical.get(key))
        for key in (
            "candidate_manifest_files",
            "candidate_snapshot_files",
            "candidate_status_files",
            "trace_files",
        )
    )


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
    markets: set[str] = set()
    candidate_evidence = item.get("candidate_evidence")
    if isinstance(candidate_evidence, dict):
        for account in candidate_evidence.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            markets.update(
                str(value).strip().lower()
                for value in account.get("markets") or []
                if str(value).strip().lower() in {"us", "hk"}
            )
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
    expected_output_runs_plan_sha256: str | None = None,
) -> list[str]:
    args = [
        "./om",
        "service",
        "cleanup",
        "--repo-root",
        str(remote_repo_root),
        "--runtime-root",
        str(remote_runtime_root),
        "--keep-releases",
        "1000000",
        "--cleanup-output-runs",
        "--output-runs-keep-days",
        str(max(0, int(keep_days))),
        "--output-runs-keep-count",
        str(max(1, int(keep_count))),
    ]
    # Runtime logs do not participate in the verified run allowlist or its CAS
    # digest.  Keep them out of this destructive path until they have an
    # equivalent source-bound content manifest and apply-time digest.
    if confirm:
        expected = str(expected_output_runs_plan_sha256 or "").strip().lower()
        if not expected:
            raise ValueError("confirmed remote cleanup requires an expected plan digest")
        args.extend(["--expected-output-runs-plan-sha256", expected])
        args.extend(["--confirm", "--yes"])
    remote_command = "cd " + shlex.quote(str(remote_repo_root)) + " && " + " ".join(shlex.quote(arg) for arg in args)
    return ["ssh", ssh_target, remote_command]


def _remote_receipt_prune_command(
    *,
    ssh_target: str,
    remote_runtime_root: str | Path,
    keep_days: int,
    keep_count: int,
    confirm: bool,
    expected_plan_sha256: str = "",
) -> list[str]:
    args = [
        "python3",
        "-c",
        REMOTE_RECEIPT_PRUNE_SCRIPT,
        str(remote_runtime_root),
        str(max(0, int(keep_days))),
        str(max(1, int(keep_count))),
        "confirm" if confirm else "preview",
        str(expected_plan_sha256 or ""),
    ]
    return ["ssh", ssh_target, " ".join(shlex.quote(arg) for arg in args)]


def _receipt_rows_by_path(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("path")): row
        for row in rows
        if isinstance(row, dict) and row.get("path")
    }


def _receipt_identity(row: dict[str, Any] | None) -> tuple[Any, Any] | None:
    if not row:
        return None
    return row.get("size_bytes"), row.get("sha256")


def _archive_prune_remote_receipts(
    *,
    root: Path,
    remote: str,
    target: str,
    remote_runtime_root: str | Path,
    keep_days: int,
    keep_count: int,
    confirm: bool,
    latest: dict[str, Any],
    remote_inventory: dict[str, Any],
    remote_inventory_operation: dict[str, Any],
    remote_inventory_ok: bool,
    source_binding_ok: bool,
    run_cmd: Callable[..., Any],
) -> dict[str, Any]:
    latest_rows = _receipt_rows_by_path(latest.get("shadow_replay_receipts"))
    local_rows = _receipt_rows_by_path(_shadow_replay_receipt_inventory(root))
    remote_rows = _receipt_rows_by_path(remote_inventory.get("shadow_replay_receipts"))
    preview_operation = _run_command(
        _remote_receipt_prune_command(
            ssh_target=target,
            remote_runtime_root=remote_runtime_root,
            keep_days=keep_days,
            keep_count=keep_count,
            confirm=False,
        ),
        run_cmd=run_cmd,
        timeout=600,
        stdout_limit=2_000_000,
    )
    preview = _parse_cli_json(preview_operation.get("stdout"))
    candidates = preview.get("candidates") if isinstance(preview.get("candidates"), list) else []
    unverified: list[str] = []
    for row in candidates:
        path = str(row.get("path") or "") if isinstance(row, dict) else ""
        identity = _receipt_identity(row if isinstance(row, dict) else None)
        if not path or not identity or not (
            identity == _receipt_identity(latest_rows.get(path))
            == _receipt_identity(local_rows.get(path))
            == _receipt_identity(remote_rows.get(path))
        ):
            unverified.append(path or "<invalid>")
    plan_sha256 = str(preview.get("plan_sha256") or "").strip().lower()
    preview_valid = (
        preview.get("schema_version") == "shadow_replay_receipt_prune.v1"
        and preview.get("status") == "preview"
        and preview.get("ok") is True
        and not preview.get("blockers")
        and len(plan_sha256) == 64
        and all(character in "0123456789abcdef" for character in plan_sha256)
        and str(preview.get("runtime_root") or "").rstrip("/")
        == str(remote_runtime_root).rstrip("/")
        and preview.get("keep_days") == max(0, int(keep_days))
        and preview.get("keep_count") == max(1, int(keep_count))
    )
    guard = {
        "verified_inventory_path": str(root / "manifests" / "inventory.latest.json"),
        "source_binding_ok": source_binding_ok,
        "remote_inventory_ok": remote_inventory_ok,
        "planned_delete_paths": [str(row.get("path")) for row in candidates if isinstance(row, dict)],
        "unverified_delete_paths": unverified,
        "plan_sha256": plan_sha256 or None,
        "confirmable": bool(preview_operation.get("ok")) and preview_valid and source_binding_ok and not unverified,
    }
    operations = [remote_inventory_operation, preview_operation]
    if confirm and guard["confirmable"]:
        apply_operation = _run_command(
            _remote_receipt_prune_command(
                ssh_target=target,
                remote_runtime_root=remote_runtime_root,
                keep_days=keep_days,
                keep_count=keep_count,
                confirm=True,
                expected_plan_sha256=plan_sha256,
            ),
            run_cmd=run_cmd,
            timeout=600,
            stdout_limit=2_000_000,
        )
        apply_payload = _parse_cli_json(apply_operation.get("stdout"))
        planned_paths = guard["planned_delete_paths"]
        apply_operation["ok"] = (
            bool(apply_operation.get("ok"))
            and apply_payload.get("ok") is True
            and apply_payload.get("status") == "applied"
            and apply_payload.get("plan_sha256") == plan_sha256
            and apply_payload.get("deleted_paths") == planned_paths
        )
        operations.append(apply_operation)
    elif confirm:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "prune-remote",
            "scope": "shadow-replay-receipts",
            "ok": False,
            "changed": False,
            "dry_run": False,
            "status": "remote_prune_guard_failed",
            "remote": _safe_label(remote or DEFAULT_REMOTE),
            "archive_root": str(root),
            "deletion_guard": guard,
            "operations": operations,
        }
    ok = all(bool(item.get("ok")) for item in operations)
    apply_payload = _parse_cli_json(operations[-1].get("stdout")) if len(operations) == 3 else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "prune-remote",
        "scope": "shadow-replay-receipts",
        "ok": ok,
        "changed": bool(confirm and ok and apply_payload.get("changed")),
        "dry_run": not confirm,
        "remote": _safe_label(remote or DEFAULT_REMOTE),
        "archive_root": str(root),
        "remote_runtime_root": str(remote_runtime_root),
        "keep_days": max(0, int(keep_days)),
        "keep_count": max(1, int(keep_count)),
        "deletion_guard": guard,
        "operations": operations,
        "remote_cleanup_preview": preview,
    }


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


def _validate_cleanup_preview(
    payload: dict[str, Any],
    *,
    remote_runtime_root: str | Path,
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != "1.0":
        errors.append("schema_version_mismatch")
    if payload.get("tool_name") != "service.cleanup":
        errors.append("tool_name_mismatch")
    if payload.get("ok") is not True:
        errors.append("cleanup_preview_not_ok")
    data = payload.get("data")
    if not isinstance(data, dict):
        errors.append("data_missing")
        data = {}
    cleanup = data.get("output_runs_cleanup")
    if not isinstance(cleanup, dict):
        errors.append("output_runs_cleanup_missing")
        cleanup = {}
    rows = cleanup.get("delete_runs")
    if not isinstance(rows, list):
        errors.append("delete_runs_missing")
        rows = []
    runs_root = Path(str(remote_runtime_root)) / "output_runs"
    normalized: list[dict[str, Any]] = []
    run_ids: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"delete_run_{index}_not_object")
            continue
        raw_path = str(row.get("path") or "").rstrip("/")
        path = Path(raw_path)
        if not raw_path or path.parent != runs_root or path.name in {"", ".", ".."}:
            errors.append(f"delete_run_{index}_unsafe_path")
            continue
        if path.name in seen:
            errors.append(f"delete_run_{index}_duplicate")
            continue
        seen.add(path.name)
        run_ids.append(path.name)
        normalized.append(
            {
                "path": raw_path,
                "estimated_bytes": int(row.get("estimated_bytes") or 0),
                "mtime_utc": row.get("mtime_utc"),
            }
        )
    plan_sha256 = _cleanup_plan_sha256(normalized)
    advertised = str(data.get("output_runs_plan_sha256") or "").strip().lower()
    if advertised and advertised != plan_sha256:
        errors.append("output_runs_plan_sha256_mismatch")
    return {
        "ok": not errors,
        "errors": errors,
        "planned_delete_run_ids": run_ids,
        "plan_sha256": plan_sha256,
    }


def _validate_cleanup_confirm(
    payload: dict[str, Any],
    *,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != "1.0":
        errors.append("schema_version_mismatch")
    if payload.get("tool_name") != "service.cleanup":
        errors.append("tool_name_mismatch")
    if payload.get("ok") is not True:
        errors.append("cleanup_confirm_not_ok")
    data = payload.get("data")
    if not isinstance(data, dict):
        errors.append("data_missing")
        data = {}
    if str(data.get("expected_output_runs_plan_sha256") or "").strip().lower() != str(
        expected_plan_sha256
    ).strip().lower():
        errors.append("confirmed_plan_sha256_mismatch")
    if data.get("status") != "cleaned":
        errors.append("cleanup_status_not_cleaned")
    return {"ok": not errors, "errors": errors}


def _cleanup_plan_sha256(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        sorted(rows, key=lambda item: str(item.get("path") or "")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and not item.is_symlink()
    ):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            size_bytes = int(path.stat().st_size)
            relative = path.relative_to(root).as_posix()
        except OSError:
            continue
        out.append(
            {
                "path": relative,
                "size_bytes": size_bytes,
                "sha256": digest.hexdigest(),
            }
        )
    return out


def _content_digest(files: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    if source.get("source_host"):
        out["source_host"] = source.get("source_host")
    return out


__all__ = [
    "archive_build_datasets",
    "archive_inventory",
    "archive_prune_remote",
    "archive_pull",
    "archive_root_for",
    "archive_verify",
]
