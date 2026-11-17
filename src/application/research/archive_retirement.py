"""Bind a bounded archive and independent restore to exact run-only retirement."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from src.application.research import archive_bounded as bounded
from src.application.research.archive_stream import TransferError, safe_relative, stop_group


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read(path, limits):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limits["evidence_bytes"]:
        raise TransferError("unsafe or oversized receipt")
    raw = path.read_bytes()
    if len(raw) > limits["evidence_bytes"]:
        raise TransferError("receipt grew")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _roots(receipt):
    from src.application.research.archive import _safe_run_id

    ids = receipt["run_ids"]
    if not ids or len(ids) > 738 or len(set(ids)) != len(ids):
        raise TransferError("invalid retirement run IDs")
    roots = ["output_runs/" + _safe_run_id(name) for name in ids]
    for row in receipt["source_files"]:
        rel = safe_relative(row["path"])
        if not any(rel.startswith(root + "/") for root in roots) and not rel.startswith("output_shared/blobs/sha256/"):
            raise TransferError("retirement requires run-only archive with canonical dependencies")
    return roots


def _files(snapshot):
    return [{k: row[k] for k in ("path", "size", "sha256")} for row in snapshot["files"]]


def _validate_copies(archive_root, restore_root, limits, deadline):
    archive_root, restore_root = Path(archive_root).absolute(), Path(restore_root).absolute()
    if (
        archive_root == restore_root
        or archive_root.is_relative_to(restore_root)
        or restore_root.is_relative_to(archive_root)
    ):
        raise TransferError("archive and independent restore overlap")
    if archive_root.is_symlink() or restore_root.is_symlink():
        raise TransferError("archive or restore alias")
    receipt, sha = _read(archive_root / "bounded-receipt.json", limits)
    restored, _ = _read(restore_root / "restore-receipt.json", limits)
    if receipt.get("state") != "validated" or receipt.get("source_deleted") is not False:
        raise TransferError("archive not validated")
    if (
        not restored.get("complete")
        or restored.get("archive_root") != str(archive_root)
        or restored.get("archive_receipt_sha256") != sha
        or restored.get("run_ids") != receipt["run_ids"]
    ):
        raise TransferError("independent restore receipt not bound to archive")
    roots = _roots(receipt)
    expected = _files({"files": receipt["source_files"]})
    for root in (archive_root, restore_root):
        snap = bounded._snapshot({"kind": "local", "runtime_root": str(root)}, roots.copy(), limits, deadline, root)
        if _files(snap) != expected or snap["directories"] != receipt["source_identity"]["directories"]:
            raise TransferError("archive or restored content changed")
    return receipt, sha


def _active_references(runtime, ids, deadline):
    """Known source UID only; permission unknown fails closed, never emit process data."""
    proc = Path("/proc")
    if not proc.is_dir():
        observer = Path("/usr/sbin/lsof")
        if not observer.is_file():
            raise TransferError("source reference observation unavailable")
        for name in ids:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransferError("source reference deadline")
            result = subprocess.run(
                [str(observer), "-n", "-P", "-t", "+D", str(runtime / "output_runs" / name)],
                capture_output=True,
                timeout=left,
            )
            if result.stdout or result.stderr or result.returncode not in (0, 1):
                raise TransferError("selected run referenced or reference unknown")
        return
    targets = [str(runtime / "output_runs" / name) for name in ids]
    count = fds = 0
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            count += 1
            if count > 256 or time.monotonic() >= deadline:
                raise TransferError("source reference budget")
            links = [path / "cwd", path / "exe"]
            links.extend((path / "fd").iterdir())
            for link in links:
                fds += 1
                if fds > 8192:
                    raise TransferError("source reference budget")
                try:
                    value = os.readlink(link)
                except FileNotFoundError:
                    continue
                if any(value == target or value.startswith(target + "/") for target in targets):
                    raise TransferError("selected run actively referenced")
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise TransferError("source reference unknown") from exc


def _source_retire(request):
    from src.application.service_cleanup import cleanup_output_runs_for_ids

    runtime = Path(request["runtime_root"]).absolute()
    receipt = request["archive_receipt"]
    limits = request["limits"]
    deadline = time.monotonic() + limits["seconds"]
    limits["_evidence_remaining"] = limits["evidence_bytes"]
    source = {"kind": "local", "runtime_root": str(runtime)}
    ids = receipt["run_ids"]

    def validate():
        if time.monotonic() >= deadline:
            raise TransferError("retirement deadline")
        snap = bounded._snapshot(source, _roots(receipt), limits, deadline, runtime)
        original = receipt["source_identity"]
        if (
            snap["host"] != original["host"]
            or snap["runtime_root"] != original["runtime_root"]
            or snap["files"] != receipt["source_files"]
            or snap["directories"] != original["directories"]
        ):
            raise TransferError("source changed since archive")
        bounded._retention_check(snap, ids, time.time())
        _active_references(runtime, ids, deadline)

    # Preview validates source too; apply repeats validation inside the transition guard.
    if not request["confirm"]:
        validate()
    return cleanup_output_runs_for_ids(
        runtime_root=runtime,
        run_ids=ids,
        confirm=request["confirm"],
        expected_plan_sha256=request.get("cleanup_sha256"),
        before_delete=validate,
        archive_binding_sha256=_digest(receipt),
        evidence_bytes=limits["evidence_bytes"],
        deadline=deadline,
    )


def _retire_execute(
    *,
    archive_root,
    restore_root,
    limits_file,
    source_root=None,
    ssh_target=None,
    remote_runtime_root="/var/lib/options-monitor",
    source_repo_root=None,
    expected_plan_sha256=None,
    confirm=False,
    deadline_override=None,
):
    """Default preview; confirm requires its digest and revalidates both local copies."""
    from src.application.research import archive

    limits = bounded.read_limits(Path(limits_file))
    deadline = deadline_override if deadline_override is not None else time.monotonic() + limits["seconds"]
    limits["_evidence_remaining"] = limits["evidence_bytes"]
    receipt, sha = _validate_copies(archive_root, restore_root, limits, deadline)
    source = archive._source_context(
        source_root=source_root, ssh_target=ssh_target, remote_runtime_root=remote_runtime_root
    )
    if source["runtime_root"] != receipt["source_identity"]["runtime_root"]:
        raise TransferError("wrong source runtime")
    request = {
        "archive_receipt": receipt,
        "runtime_root": source["runtime_root"],
        "confirm": False,
        "limits": {k: v for k, v in limits.items() if not k.startswith("_")},
    }

    def call(confirming=False, cleanup_sha=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError("retirement deadline")
        request["confirm"] = confirming
        request["cleanup_sha256"] = cleanup_sha
        request["limits"]["seconds"] = remaining
        payload = json.dumps(request).encode()
        if len(payload) > limits["_evidence_remaining"]:
            raise TransferError("retirement request budget")
        limits["_evidence_remaining"] -= len(payload)
        code = "from src.application.research.archive_retirement import _worker; _worker()"
        if source["kind"] == "local":
            command = [sys.executable, "-c", code]
        else:
            import shlex

            if not source_repo_root or not Path(source_repo_root).is_absolute():
                raise TransferError("explicit deployed source repo required")
            boot = "import sys;sys.path.insert(0," + repr(str(source_repo_root)) + ");" + code
            remote = [
                "timeout",
                "--kill-after=1s",
                str(max(1, int(remaining))) + "s",
                str(Path(source_repo_root) / ".venv/bin/python"),
                "-I",
                "-B",
                "-c",
                boot,
            ]
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                source["ssh_target"],
                shlex.join(remote),
            ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            stdout, stderr = process.communicate(payload, timeout=remaining)
            if len(stdout) + len(stderr) > limits["_evidence_remaining"]:
                raise TransferError("retirement response budget; outcome unknown")
            limits["_evidence_remaining"] -= len(stdout) + len(stderr)
            if process.returncode:
                raise TransferError("source retirement failed; inspect receipt before retry")
            return json.loads(stdout)
        except subprocess.TimeoutExpired as exc:
            raise TransferError("retirement timeout; source outcome unknown; do not retry") from exc
        finally:
            stop_group(process, limits["grace_seconds"])
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    preview = call()
    binding = {
        "archive_receipt_sha256": sha,
        "archive_root": str(Path(archive_root).absolute()),
        "restore_root": str(Path(restore_root).absolute()),
        "source": source,
        "cleanup_sha256": preview["plan_sha256"],
        "run_ids": receipt["run_ids"],
    }
    digest = _digest(binding)
    result = {
        "ok": True,
        "confirmed": confirm,
        "plan_sha256": digest,
        "binding": binding,
        "delete_runs": preview["delete_runs"],
        "source_deleted": False,
    }
    journal_key = hashlib.sha256((_digest(receipt) + preview["plan_sha256"]).encode()).hexdigest()
    result["source_receipt_path"] = str(
        Path(source["runtime_root"]) / "output_shared/state/run_retirements" / (journal_key + ".jsonl")
    )
    if confirm:
        if not expected_plan_sha256 or expected_plan_sha256 != digest:
            raise TransferError("retirement preview digest changed")
        try:
            outcome = call(True, preview["plan_sha256"])
            result.update(outcome=outcome, ok=outcome["ok"], source_deleted=outcome["source_deleted"])
        except TransferError as exc:
            result.update(ok=False, state="unknown", source_deleted=None, reason=str(exc))
    return result


def _worker():
    try:
        # Parent enforces the approved request budget; reject oversized input before parse.
        raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise TransferError("source request hard bound")
        request = json.loads(raw)
        if len(raw) > request["limits"]["evidence_bytes"]:
            raise TransferError("source request budget")
        result = _source_retire(request)
        encoded = json.dumps(result).encode()
        if len(encoded) > request["limits"]["evidence_bytes"]:
            raise TransferError("source result budget; outcome unknown")
        print(encoded.decode())
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "unknown", "reason": type(exc).__name__}))
        raise SystemExit(2)


def retire_batch(**kwargs):
    """Supervise verification and retirement; ambiguous remote outcomes are never false."""
    import uuid

    limits = bounded.read_limits(Path(kwargs["limits_file"]))
    deadline = time.monotonic() + limits["seconds"]
    target = Path(kwargs["archive_root"]).absolute()
    result_path = target.parent / (".retirement-" + uuid.uuid4().hex + ".result.json")
    request = {
        "kwargs": {k: str(v) if isinstance(v, Path) else v for k, v in kwargs.items()},
        "deadline": deadline,
        "result": str(result_path),
    }
    code = """import json,sys,os
from pathlib import Path
from src.application.research.archive_retirement import _retire_execute
r=json.load(sys.stdin)
try:
 out=_retire_execute(**r['kwargs'],deadline_override=r['deadline'])
except Exception as exc:
 out={'ok':False,'state':'unknown','source_deleted':None,'reason':str(exc),'error_type':type(exc).__name__}
wire=json.dumps(out).encode()
with open(r['result'],'xb') as f:
 f.write(wire);f.flush();os.fsync(f.fileno())
"""
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
    result = bounded._supervise(process, request, limits, target, target, result_path)
    if not result.get("ok"):
        result["source_deleted"] = None
    return result
