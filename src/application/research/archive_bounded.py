"""Opt-in bounded archive execution; legacy pull/prune remain separate."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import stat
import sys
import time
from datetime import datetime, timezone

from src.application.research.archive_stream import (
    READ_FILES_SCRIPT,
    receive_files,
    TransferError,
    WriteBudget,
    publish_batch,
    safe_relative,
)


# Executed read-only in a short-lived interpreter, locally or over existing SSH.
# A complete metadata pass precedes hashing. Output is bounded by the caller.
SNAPSHOT_SCRIPT = r"""
import hashlib,json,os,socket,stat,sys,time
root,request_json=sys.argv[1:]
r=json.loads(request_json); deadline=time.monotonic()+r['seconds']
rows=[]; directories=[]; total=0; entries=0; seen=set()
def open_directory(relative):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in relative.split('/'):
            if part in ('','.','..'): raise ValueError('unsafe directory')
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise

def safe_lstat(relative):
    parts=relative.split('/')
    parent=open_directory('/'.join(parts[:-1])) if len(parts)>1 else os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: return os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
    finally: os.close(parent)

def open_regular(relative):
    parts=relative.split('/')
    if any(part in ('','.','..') for part in parts): raise ValueError('unsafe path')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        source=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    finally: os.close(fd)
    if not stat.S_ISREG(os.fstat(source).st_mode):
        os.close(source);raise ValueError('nonregular file')
    return os.fdopen(source,'rb')

# Only metadata is read for retention; selected content remains separately bounded.
run_metadata=[]; run_directory=open_directory('output_runs')
try:
    with os.scandir(run_directory) as iterator:
        metadata_count=0
        for item in iterator:
            metadata_count+=1
            if time.monotonic()>=deadline or metadata_count>r['max_entries']: raise ValueError('retention metadata budget')
            st=item.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode): run_metadata.append([item.name,st.st_mtime])
finally: os.close(run_directory)
pointer=None
try:
    with open_regular('output_shared/state/last_run_dir.txt') as handle:
        raw=handle.read(r['max_file_bytes']+1)
    if len(raw)>r['max_file_bytes']: raise ValueError('pointer too large')
    target=os.path.normpath(raw.decode().strip())
    if os.path.dirname(target)!=os.path.join(root,'output_runs'): raise ValueError('pointer outside runs')
    pointer=os.path.basename(target)
except FileNotFoundError: pass
for relative in r['roots']:
    path=os.path.join(root,relative)
    if not os.path.exists(path):
        if relative in ('output_shared/research','output_shared/required_data'):
            continue
        raise ValueError('selected run missing')
    pending=[relative]
    while pending:
        if time.monotonic()>=deadline: raise TimeoutError('snapshot deadline')
        rel=pending.pop()
        if rel in seen: continue
        seen.add(rel)
        full=os.path.join(root,rel); st=safe_lstat(rel)
        entries+=1
        if entries>r['max_entries']: raise ValueError('entry budget')
        if stat.S_ISDIR(st.st_mode):
            directories.append(rel)
            directory=open_directory(rel)
            try:
                with os.scandir(directory) as iterator:
                    for item in iterator:
                        pending.append(rel+'/'+item.name)
                        if entries+len(pending)>r['max_entries']: raise ValueError('entry budget')
            finally: os.close(directory)
        elif stat.S_ISREG(st.st_mode):
            if rel.endswith(('.db','.sqlite','.sqlite3','-wal','-shm')):
                raise ValueError('database excluded')
            total+=st.st_size
            if total>r['max_bytes'] or st.st_size>r['max_file_bytes']: raise ValueError('byte budget')
            rows.append({'path':rel,'size':st.st_size,'mtime_ns':st.st_mtime_ns,
                         'identity':[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]})
            if rel.endswith('/state/required_data_snapshot_manifest.json'):
                with open_regular(rel) as manifest_file:
                    raw=manifest_file.read(r['max_file_bytes']+1)
                if len(raw)>r['max_file_bytes']: raise ValueError('manifest length')
                manifest=json.loads(raw)
                for value in manifest['symbols'].values():
                    if 'scan_blob_ref' not in value: continue
                    ref=value['scan_blob_ref']; digest=ref['blob_sha256']
                    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest): raise ValueError('blob hash')
                    path='output_shared/blobs/sha256/'+digest[:2]+'/'+digest+'.json.gz'
                    if ref['blob_relpath']!=path or ref['uncompressed_size_bytes']>r['max_file_bytes']: raise ValueError('blob ref')
                    if path not in r['roots']: r['roots'].append(path)
        else: raise ValueError('nonregular input')
for row in rows:
    h=hashlib.sha256(); p=os.path.join(root,row['path'])
    with open_regular(row['path']) as f:
        before=os.fstat(f.fileno())
        remaining=row['size']
        while remaining:
            if time.monotonic()>=deadline: raise TimeoutError('hash deadline')
            chunk=f.read(min(65536,remaining))
            if not chunk: raise ValueError('short source')
            h.update(chunk);remaining-=len(chunk)
        extra=f.read(1);after=os.fstat(f.fileno())
    identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if extra or identity(before)!=identity(after) or before.st_size!=row['size'] or list(identity(before))!=row['identity']:
        raise ValueError('source changed')
    row['sha256']=h.hexdigest()
print(json.dumps({'host':socket.getfqdn(),'runtime_root':root,'run_metadata':sorted(run_metadata),'pointer':pointer,'files':sorted(rows,key=lambda x:x['path']),'directories':sorted(directories)},separators=(',',':')))
"""


def read_limits(path: Path) -> dict:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384:
        raise TransferError("limits file too large")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384:
            raise TransferError("limits file changed")
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise TransferError("limits file grew")
    value = json.loads(raw)
    names = (
        "seconds",
        "grace_seconds",
        "max_bytes",
        "max_entries",
        "max_file_bytes",
        "max_rss_bytes",
        "evidence_bytes",
        "reserve_bytes",
        "stderr_bytes",
    )
    if not isinstance(value, dict) or set(value) != set(names):
        raise TransferError("limits must contain exactly the documented fields")
    for name in names:
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise TransferError("limits must be positive finite numbers")
        if name not in ("seconds", "grace_seconds") and not isinstance(number, int):
            raise TransferError("byte and entry limits must be integers")
    if value["evidence_bytes"] < 4096:
        raise TransferError("evidence budget must reserve at least 4096 bytes")
    return value


def _source_command(source: dict, script: str, arguments: list[str], remaining: float) -> list[str]:
    if source["kind"] == "local":
        return [sys.executable, "-c", script, source["runtime_root"], *arguments]
    # timeout is an explicit deployment capability prerequisite, not installed here.
    argv = [
        "timeout",
        "--kill-after=1s",
        f"{max(0.001, remaining):.3f}s",
        "python3",
        "-c",
        script,
        source["runtime_root"],
        *arguments,
    ]
    return [
        "ssh",
        *(["-o", "Compression=yes"] if source.get("ssh_compression") else []),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        source["ssh_target"],
        shlex.join(argv),
    ]


def _snapshot(source: dict, roots: list[str], limits: dict, deadline: float, temporary: Path) -> dict:
    request = {k: limits[k] for k in ("max_bytes", "max_entries", "max_file_bytes")}
    request.update(roots=roots, seconds=max(0.001, deadline - time.monotonic()))
    command = _source_command(source, SNAPSHOT_SCRIPT, [json.dumps(request)], deadline - time.monotonic())
    # Metadata output must not share the unbounded legacy capture_output helper.
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    from src.application.research.archive_stream import stop_group
    import selectors

    selector = selectors.DefaultSelector()
    data = bytearray()
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TransferError("snapshot deadline")
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                if len(data) + len(chunk) > limits["_evidence_remaining"]:
                    raise TransferError("snapshot evidence budget")
                data.extend(chunk)
        if proc.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise TransferError("source snapshot failed")
        limits["_evidence_remaining"] -= len(data)
        result = json.loads(data)
        if result.get("runtime_root") != source["runtime_root"]:
            raise TransferError("source identity mismatch")
        return result
    finally:
        selector.close()
        stop_group(proc, limits["grace_seconds"])
        proc.stdout.close()


def _retention_check(snapshot: dict, run_ids: list[str], frozen_at: float) -> dict:
    from src.application.service_cleanup import retained_output_runs

    metadata = snapshot["run_metadata"]
    names = [name for name, _ in metadata]
    if len(names) != len(set(names)) or not set(run_ids).issubset(names):
        raise TransferError("selected run metadata missing or ambiguous")
    protected = retained_output_runs(
        [(name, datetime.fromtimestamp(mtime, tz=timezone.utc)) for name, mtime in metadata],
        keep_days=30,
        keep_count=200,
        now=datetime.fromtimestamp(frozen_at, tz=timezone.utc),
        pointer=snapshot["pointer"],
    )
    if any(run_id in protected for run_id in run_ids):
        raise TransferError("selected run is retention protected")
    return protected


def _compare_selected_snapshots(before: dict, after: dict, run_ids: list[str], frozen_at: float) -> dict:
    # Retention state may evolve outside this batch, but selected data may not.
    protected = _retention_check(after, run_ids, frozen_at)
    selected = lambda snap: sorted((name, mtime) for name, mtime in snap["run_metadata"] if name in run_ids)
    if selected(before) != selected(after):
        raise TransferError("selected run metadata changed")
    stable = lambda snap: {k: v for k, v in snap.items() if k not in {"run_metadata", "pointer"}}
    if stable(before) != stable(after):
        raise TransferError("source changed after copy")
    return protected


def _execute(request: dict) -> dict:
    from src.application.research import archive

    limits = request["limits"]
    deadline = request["deadline"]
    limits["_evidence_remaining"] = request["evidence_remaining"]
    source = request["source"]
    target = Path(request["target"])
    staging = Path(request["staging"])
    roots = [f"output_runs/{run_id}" for run_id in request["run_ids"]]
    if request.get("include_fixed_shared", True):
        roots.extend(archive.SYNC_RELATIVE_DIRS)
    before = _snapshot(source, roots, limits, deadline, staging)
    protected = _retention_check(before, request["run_ids"], request["frozen_at"])
    if request.get("expected_host") != before["host"]:
        raise TransferError("source host mismatch")
    old_archive = request.get("existing_archive_root")
    old_review = None
    if old_archive:
        old_root = Path(old_archive)
        inventory_file = old_root / "manifests" / "inventory.latest.json"
        if inventory_file.is_symlink() or inventory_file.stat().st_size > limits["max_file_bytes"]:
            raise TransferError("old inventory unsafe or exceeds budget")
        old_inventory = json.loads(inventory_file.read_bytes())
        old_ids = [archive._safe_run_id(row["run_id"]) for row in old_inventory.get("runs", [])]
        if not old_ids or len(old_ids) > 10:
            raise TransferError("old batch requires 1..10 explicit inventory rows")
        old_roots = [f"output_runs/{name}" for name in old_ids] + list(archive.SYNC_RELATIVE_DIRS)
        captured_old = _snapshot({"kind": "local", "runtime_root": str(old_root)}, old_roots, limits, deadline, staging)
        captured_source = _snapshot(source, old_roots, limits, deadline, staging)
        logical = lambda snap: [(row["path"], row["size"], row["sha256"]) for row in snap["files"]]
        if logical(captured_old) != logical(captured_source):
            raise TransferError("old archive source content mismatch")
        for name in old_ids:
            refs, status, _ = archive._run_scan_blob_refs(old_root / "output_runs" / name)
            if status == "invalid":
                raise TransferError("old canonical references invalid")
            for ref in refs:
                if ref["uncompressed_size_bytes"] > limits["max_file_bytes"]:
                    raise TransferError("old blob exceeds decode limit")
                archive.load_required_data_scan_blob(runtime_root=old_root, blob_ref=ref)
        old_review = {
            "run_ids": old_ids,
            "status": "content_verified_read_only",
            "source_snapshot_sha256": hashlib.sha256(json.dumps(captured_source, sort_keys=True).encode()).hexdigest(),
        }
    staging.mkdir(mode=0o700)
    budget = WriteBudget(limits["max_bytes"])
    operations = []

    # Canonical refs live in the required-data manifest. Read it under the same
    # frozen budget first, then extend the snapshot with its exact blob paths.
    def copy_rows(rows):
        command = _source_command(source, READ_FILES_SCRIPT, [], deadline - time.monotonic())
        operation = receive_files(
            command,
            root=staging,
            rows=rows,
            budget=budget,
            deadline=deadline,
            grace=limits["grace_seconds"],
            stderr_limit=min(limits["stderr_bytes"], limits["_evidence_remaining"]),
            reserve_bytes=limits["reserve_bytes"],
            evidence_limit=limits["_evidence_remaining"],
        )
        limits["_evidence_remaining"] -= operation["request_bytes"] + operation["stderr_bytes"]
        if limits["_evidence_remaining"] < 0:
            raise TransferError("cumulative evidence budget")
        operations.append(operation)

    # Snapshot discovers canonical refs before any payload transfer.
    refs = []
    space = os.statvfs(staging)
    allocated = sum(((row["size"] + space.f_frsize - 1) // space.f_frsize) * space.f_frsize for row in before["files"])
    required = allocated + limits["evidence_bytes"] + limits["reserve_bytes"]
    if space.f_bavail * space.f_frsize < required:
        raise TransferError("insufficient target batch space")
    for relative in before["directories"]:
        (staging / safe_relative(relative)).mkdir(parents=True, exist_ok=True)
    copy_rows(before["files"])
    for run_id in request["run_ids"]:
        found, status, _error = archive._run_scan_blob_refs(staging / "output_runs" / run_id)
        if status == "invalid":
            raise TransferError("invalid canonical refs")
        refs.extend(found)
    blobs = sorted({ref["blob_relpath"] for ref in refs})
    after = _snapshot(source, roots + blobs, limits, deadline, staging)
    protected_after = _compare_selected_snapshots(before, after, request["run_ids"], request["frozen_at"])
    for ref in refs:
        archive.load_required_data_scan_blob(runtime_root=staging, blob_ref=ref)
    inventory = archive._run_inventory(staging / "output_runs", base=staging, run_ids=request["run_ids"])
    if len(inventory) != len(request["run_ids"]) or not all(row["verified"] for row in inventory):
        raise TransferError("archive structure invalid")
    from src.application.candidate_evidence_history import load_run_candidate_evidence

    for run_id in request["run_ids"]:
        evidence = load_run_candidate_evidence(base=staging, run_id=run_id, runs_root=staging / "output_runs")
        if any(item.classification["status"].startswith("unsupported") for item in evidence):
            raise TransferError("candidate owner evidence unsupported")
    review = {
        "source_deleted": False,
        "apply_supported": False,
        "delete_token": None,
        "frozen_at": request["frozen_at"],
        "keep_days": 30,
        "keep_count": 200,
        "source_host": before["host"],
        "runtime_root": source["runtime_root"],
        "candidates": [
            {
                "run_id": run_id,
                "path": source["runtime_root"] + "/output_runs/" + run_id,
                "status": "archived_for_review_only",
            }
            for run_id in request["run_ids"]
        ],
        "protected": protected,
    }
    review_bytes = json.dumps(review, separators=(",", ":")).encode()
    if len(review_bytes) > limits["_evidence_remaining"]:
        raise TransferError("candidate evidence budget")
    limits["_evidence_remaining"] -= len(review_bytes)
    (staging / "retirement-review.json").write_bytes(review_bytes)
    receipt = {
        "state": "validated",
        "old_archive_review": old_review,
        "retention_checks": {
            "frozen_at": request["frozen_at"],
            "before": {
                "protected": protected,
                "pointer": before["pointer"],
                "metadata_sha256": hashlib.sha256(json.dumps(before["run_metadata"]).encode()).hexdigest(),
            },
            "after": {
                "protected": protected_after,
                "pointer": after["pointer"],
                "metadata_sha256": hashlib.sha256(json.dumps(after["run_metadata"]).encode()).hexdigest(),
            },
        },
        "source_identity": {k: v for k, v in before.items() if k != "files"},
        "source_files": before["files"],
        "operations": operations,
        "payload_written_bytes": budget.written,
        "source_deleted": False,
        "apply_supported": False,
        "delete_token": None,
        "run_ids": request["run_ids"],
    }
    encoded = json.dumps(receipt, separators=(",", ":")).encode()
    if len(encoded) > limits["_evidence_remaining"]:
        raise TransferError("cumulative receipt evidence budget")
    limits["_evidence_remaining"] -= len(encoded)
    (staging / "bounded-receipt.json").write_bytes(encoded)
    publish_batch(staging, target, deadline=deadline)
    if time.monotonic() >= deadline or (target / "bounded-receipt.json").read_bytes() != encoded:
        raise TransferError("published receipt readback unknown")
    return {
        "ok": True,
        "state": "published",
        "archive_root": str(target),
        "source_deleted": False,
        "apply_supported": False,
        "delete_token": None,
        "payload_written_bytes": budget.written,
        "operation_count": len(operations),
    }


def _worker(request: dict) -> None:
    try:
        result = _execute(request)
    except Exception as exc:
        result = {
            "ok": False,
            "state": "unknown" if Path(request["target"]).exists() else "failed",
            "reason": type(exc).__name__,
            "source_deleted": False,
            "staging": request["staging"],
            "archive_root": request["target"],
        }
    data = json.dumps(result, separators=(",", ":")).encode()
    if len(data) > 1024:
        data = b'{"ok":false,"state":"unknown","reason":"receipt_budget"}'
    # Exclusive outer receipt: never mutate an existing archive or receipt.
    with open(request["result"], "xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def bounded_pull(
    *,
    repo_root,
    archive_root,
    source_root=None,
    ssh_target=None,
    remote_runtime_root="/var/lib/options-monitor",
    run_ids=None,
    include_logs=True,
    write=False,
    limits_file=None,
    expected_source_host=None,
    existing_archive_root=None,
    include_fixed_shared=True,
    ssh_compression=False,
) -> dict:
    """Execute only an explicitly budgeted batch in a previously absent root."""
    import uuid
    from src.application.research import archive

    limits = read_limits(Path(limits_file))
    if not isinstance(ssh_compression, bool):
        raise TransferError("ssh_compression must be boolean")
    if not isinstance(include_fixed_shared, bool):
        raise TransferError("include_fixed_shared must be boolean")
    if not write or include_logs or not archive_root or not run_ids or len(run_ids) > 738:
        raise TransferError("bounded mode requires write, no logs, explicit root and 1..738 runs")
    ids = list(dict.fromkeys(archive._safe_run_id(item) for item in run_ids))
    target = Path(archive_root).expanduser().absolute()
    if not target.parent.is_dir() or target.parent.is_symlink() or target.exists() or target.is_symlink():
        raise TransferError("bounded target must be new with an existing private parent")
    source = archive._source_context(
        source_root=source_root, ssh_target=ssh_target, remote_runtime_root=remote_runtime_root
    )
    if ssh_compression and source["kind"] != "ssh":
        raise TransferError("SSH compression requires an SSH source")
    source["ssh_compression"] = ssh_compression
    if source["kind"] == "local":
        origin = Path(source["runtime_root"]).resolve()
        if target.is_relative_to(origin) or origin.is_relative_to(target):
            raise TransferError("source and target overlap")
    if expected_source_host is None:
        if source["kind"] != "local":
            raise TransferError("explicit expected source host required")
        import socket

        expected_source_host = socket.getfqdn()
    if existing_archive_root is not None:
        old = Path(existing_archive_root).absolute()
        if old.is_symlink() or not old.is_dir() or target.is_relative_to(old) or old.is_relative_to(target):
            raise TransferError("old archive must be a separate read-only root")
        existing_archive_root = str(old)
    token = uuid.uuid4().hex
    staging = target.parent / f".{target.name}-{token}.partial"
    result_path = target.parent / f".{target.name}-{token}.result.json"
    request = {
        "source": source,
        "include_fixed_shared": include_fixed_shared,
        "run_ids": ids,
        "target": str(target),
        "staging": str(staging),
        "result": str(result_path),
        "limits": limits,
        "expected_host": expected_source_host,
        "existing_archive_root": existing_archive_root,
        "frozen_at": time.time(),
        "deadline": time.monotonic() + limits["seconds"],
    }
    encoded = json.dumps(request).encode()
    if len(encoded) > limits["evidence_bytes"]:
        raise TransferError("request exceeds evidence budget")
    request["evidence_remaining"] = limits["evidence_bytes"] - len(encoded) - 2048
    if request["evidence_remaining"] <= 0:
        raise TransferError("no evidence capacity after request/result reserve")
    encoded = json.dumps(request).encode()
    command = [sys.executable, "-m", "src.application.research.archive_bounded"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=repo_root,
    )
    process.stdin.write(encoded)
    process.stdin.close()
    return _supervise(process, request, limits, target, staging, result_path)


def _supervise(process, request, limits, target, staging, result_path):
    try:
        return _supervise_observed(process, request, limits, target, staging, result_path)
    except (subprocess.SubprocessError, OSError, TransferError):
        return {
            "ok": False,
            "state": "unknown",
            "reason": "supervision_or_cleanup_unknown",
            "source_deleted": False,
            "archive_root": str(target),
            "staging": str(staging),
        }


def _supervise_observed(process, request, limits, target, staging, result_path):
    from src.application.research.archive_stream import process_table, stop_group

    tracked = {}
    peak_rss = 0
    reason = None
    try:
        while True:
            remaining = request["deadline"] - time.monotonic()
            if remaining <= 0:
                reason = "deadline"
                break
            try:
                rows = process_table(min(1.0, remaining))
            except subprocess.TimeoutExpired:
                reason = "deadline" if time.monotonic() >= request["deadline"] else "process_observation_unknown"
                break
            selected = {process.pid}
            changed = True
            while changed:
                previous = len(selected)
                selected.update(pid for pid, row in rows.items() if row["parent"] in selected)
                changed = len(selected) != previous
            for pid in selected:
                if pid in rows:
                    tracked[(pid, rows[pid]["started"])] = rows[pid]
            live = [row for (pid, started), row in tracked.items() if pid in rows and rows[pid]["started"] == started]
            rss = sum(rows[row["pid"]]["rss"] for row in live)
            peak_rss = max(peak_rss, rss)
            code = process.poll()
            if time.monotonic() >= request["deadline"] or rss > limits["max_rss_bytes"]:
                reason = "deadline" if time.monotonic() >= request["deadline"] else "rss_limit"
                break
            if code is not None:
                if any(row["pid"] != process.pid for row in live):
                    reason = "descendant_exit_unknown"
                break
            time.sleep(0.02)
        if reason:
            return {
                "ok": False,
                "state": "unknown",
                "reason": reason,
                "peak_process_tree_rss_bytes": peak_rss,
                "source_deleted": False,
                "archive_root": str(target),
                "staging": str(staging),
            }
        if not result_path.is_file() or result_path.stat().st_size > limits["evidence_bytes"]:
            raise TransferError("worker result missing or oversized; state unknown")
        result = json.loads(result_path.read_bytes())
        result.update(
            worker_exit=process.returncode, peak_process_tree_rss_bytes=peak_rss, result_path=str(result_path)
        )
        if process.returncode != 0 or time.monotonic() >= request["deadline"]:
            result.update(ok=False, state="unknown")
        return result
    finally:
        # Every transfer child has a private group, even if its leader exits.
        # Verify tracked start identities before touching a group to avoid PID reuse.
        try:
            rows = process_table()
        except (subprocess.SubprocessError, OSError, TransferError):
            # Still attempt the known worker group; no evidence for other groups.
            stop_group(process, limits["grace_seconds"])
            raise
        groups = set()
        for (pid, started), row in tracked.items():
            if pid in rows and rows[pid]["started"] == started:
                group = rows[pid]["group"]
                if group != process.pid:
                    groups.add(group)
        for group in groups:
            # A surviving tracked member confirms ownership even when the
            # group's leader has already exited. Never require leader liveness.
            class Group:
                pass

            child = Group()
            child.pid = group
            child.poll = lambda: None
            child.wait = lambda timeout=None: None
            stop_group(child, limits["grace_seconds"])
        stop_group(process, limits["grace_seconds"])


if __name__ == "__main__":
    _worker(json.load(sys.stdin))


def _restore_execute(*, archive_root: Path, target: Path, limits: dict, deadline: float) -> dict:
    """Restore a published batch into a new local tree; never contact its source."""
    import uuid
    from src.application.research import archive
    from src.application.candidate_evidence_history import load_run_candidate_evidence

    archive_root, target = Path(archive_root).absolute(), Path(target).absolute()
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise TransferError("restore requires a new target")
    if target.is_relative_to(archive_root) or archive_root.is_relative_to(target):
        raise TransferError("restore roots overlap")
    receipt_path = archive_root / "bounded-receipt.json"
    if receipt_path.is_symlink() or receipt_path.stat().st_size > limits["evidence_bytes"]:
        raise TransferError("restore receipt unsafe")
    receipt = json.loads(receipt_path.read_bytes())
    rows = receipt["source_files"]
    ids = receipt["run_ids"]
    directories = receipt["source_identity"]["directories"]
    if not ids or len(ids) > 738 or len(rows) + len(directories) > limits["max_entries"]:
        raise TransferError("restore count budget")
    if len({row["path"] for row in rows}) != len(rows):
        raise TransferError("duplicate restore paths")
    total = sum(row["size"] for row in rows)
    if total > limits["max_bytes"] or any(row["size"] > limits["max_file_bytes"] for row in rows):
        raise TransferError("restore byte budget")
    space = os.statvfs(target.parent)
    if space.f_bavail * space.f_frsize < total + limits["evidence_bytes"] + limits["reserve_bytes"]:
        raise TransferError("restore space budget")
    staging = target.parent / ("." + target.name + "-" + uuid.uuid4().hex + ".partial")
    staging.mkdir(mode=0o700)
    budget = WriteBudget(limits["max_bytes"])
    remaining_evidence = limits["evidence_bytes"] - receipt_path.stat().st_size - 1024
    if remaining_evidence <= 0:
        raise TransferError("restore evidence budget")
    for directory in directories:
        if time.monotonic() >= deadline:
            raise TransferError("restore deadline")
        (staging / safe_relative(directory)).mkdir(parents=True, exist_ok=True)
    local_rows = []
    for row in rows:
        rel = safe_relative(row["path"])
        record = (archive_root / rel).lstat()
        if not stat.S_ISREG(record.st_mode) or record.st_size != row["size"]:
            raise TransferError("restore file type or size changed")
        local_rows.append(
            {
                **row,
                "identity": [record.st_dev, record.st_ino, record.st_size, record.st_mtime_ns, record.st_ctime_ns],
            }
        )
    operation = receive_files(
        [sys.executable, "-c", READ_FILES_SCRIPT, str(archive_root)],
        root=staging,
        rows=local_rows,
        budget=budget,
        deadline=deadline,
        grace=limits["grace_seconds"],
        stderr_limit=min(limits["stderr_bytes"], remaining_evidence),
        reserve_bytes=limits["reserve_bytes"],
        evidence_limit=remaining_evidence,
    )
    remaining_evidence -= operation["request_bytes"] + operation["stderr_bytes"]
    for run_id in ids:
        if time.monotonic() >= deadline:
            raise TransferError("restore owner deadline")
        run_id = archive._safe_run_id(run_id)
        refs, status, _ = archive._run_scan_blob_refs(staging / "output_runs" / run_id)
        if status == "invalid":
            raise TransferError("restore invalid refs")
        for ref in refs:
            if ref["uncompressed_size_bytes"] > limits["max_file_bytes"]:
                raise TransferError("restore decode budget")
            archive.load_required_data_scan_blob(runtime_root=staging, blob_ref=ref)
        evidence = load_run_candidate_evidence(base=staging, run_id=run_id, runs_root=staging / "output_runs")
        if any(item.classification["status"].startswith("unsupported") for item in evidence):
            raise TransferError("restore owner unsupported")
    inventory = archive._run_inventory(staging / "output_runs", base=staging, run_ids=ids)
    if len(inventory) != len(ids) or not all(row["verified"] for row in inventory):
        raise TransferError("restore structure invalid")
    result = {
        "complete": True,
        "run_count": len(ids),
        "file_count": len(rows),
        "payload_written_bytes": budget.written,
        "source_deleted": False,
    }
    encoded = json.dumps(result, sort_keys=True).encode()
    if len(encoded) > remaining_evidence or time.monotonic() >= deadline:
        raise TransferError("restore receipt budget")
    (staging / "restore-receipt.json").write_bytes(encoded)
    publish_batch(staging, target, deadline=deadline)
    if (target / "restore-receipt.json").read_bytes() != encoded:
        raise TransferError("restore publication unknown")
    return result


def restore_batch(*, archive_root: Path, target: Path, limits_file: Path) -> dict:
    """Supervise the complete independent restore with the archive worker limits."""
    import uuid

    limits = read_limits(Path(limits_file))
    archive_root = Path(archive_root).expanduser().absolute()
    target = Path(target).expanduser().absolute()
    result_path = Path(target).parent / (".restore-" + uuid.uuid4().hex + ".result.json")
    code = """import json,sys,os
from pathlib import Path
from src.application.research.archive_bounded import _restore_execute
r=json.load(sys.stdin)
try:
 result=_restore_execute(archive_root=Path(r['archive_root']),target=Path(r['target']),limits=r['limits'],deadline=r['deadline'])
 result['ok']=True
except Exception as exc:
 result={'ok':False,'state':'unknown','reason':type(exc).__name__,'source_deleted':False}
with open(r['result'],'xb') as f:
 f.write(json.dumps(result).encode());f.flush();os.fsync(f.fileno())
"""
    request = dict(
        archive_root=str(archive_root),
        target=str(target),
        limits=limits,
        result=str(result_path),
        deadline=time.monotonic() + limits["seconds"],
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    process.stdin.write(json.dumps(request).encode())
    process.stdin.close()
    return _supervise(process, request, limits, Path(target), Path(target), result_path)
