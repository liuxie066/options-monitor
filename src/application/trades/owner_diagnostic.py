"""Temporary, opt-in owner aggregates. No heap values, business I/O or background work."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import threading
import time
from typing import Callable


ROOT_NAMES = ("brief_cache", "inbox_cache", "lifecycle_cache")
KINDS = {id(t): t.__name__ for t in (dict, list, tuple, set, frozenset, str, bytes, int, float, bool, type(None))}
STOP_REASONS = (None, "nodes", "depth", "time", "metadata", "cancelled", "unsupported", "mutation")
MAX_PACKET = 16384
IO_SECONDS = 0.02


@dataclass(frozen=True)
class Limits:
    nodes: int = 50000
    depth: int = 64
    seconds: float = 0.10
    metadata_bytes: int = 8 * 1024 * 1024


def aggregate(roots: dict[str, dict], *, limits: Limits = Limits(),
              deadline: float | None = None, cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Stable shallow snapshots only; per-root reachability and deduplicated union.

    Cooperative budgets: not hard pause/RSS limits. Metadata excludes allocator
    arenas, original payloads retained by snapshots and serialization/socket work.
    """
    started = time.monotonic()
    deadline = min(deadline if deadline is not None else math.inf, started + limits.seconds)
    seen: dict[int, int] = {}
    union: dict[str, list[int]] = {}
    per_root = []
    stack = []
    reason = None
    metadata_high = 0
    steps = 0
    try:
        for index, name in enumerate(ROOT_NAMES):
            root = roots[name]
            if type(root) is not dict:
                raise ValueError("unstable_root")
            bit = 1 << index
            counts: dict[str, list[int]] = {}
            stack = [(iter((root,)), -1)]
            while stack:
                steps += 1
                # No list(items)/children: even one huge container is streamed.
                estimate = (sys.getsizeof(seen) + len(seen) * sys.getsizeof(id(seen))
                            + sys.getsizeof(stack)
                            + sum(sys.getsizeof(p) + sys.getsizeof(p[0]) for p in stack))
                metadata_high = max(metadata_high, estimate)
                if cancelled():
                    reason = "cancelled"
                elif time.monotonic() >= deadline:
                    reason = "time"
                elif estimate >= limits.metadata_bytes:
                    reason = "metadata"
                if reason:
                    break
                try:
                    value = next(stack[-1][0])
                    depth = stack[-1][1] + 1
                except StopIteration:
                    stack.pop()
                    continue
                oid = id(value)
                previous = seen.get(oid, 0)
                if previous & bit:
                    continue
                if depth > limits.depth:
                    reason = "depth"
                    break
                kind = type(value)
                kind_name = KINDS.get(id(kind))  # never hash/compare a user metaclass
                if kind_name is None:
                    reason = "unsupported"
                    break
                if not previous and len(seen) >= limits.nodes:
                    reason = "nodes"
                    break
                seen[oid] = previous | bit
                size = sys.getsizeof(value)  # exact builtin only
                row = counts.setdefault(kind_name, [0, 0])
                row[0] += 1
                row[1] += size
                if not previous:
                    row = union.setdefault(kind_name, [0, 0])
                    row[0] += 1
                    row[1] += size
                if kind is dict:
                    stack.extend([(iter(value.values()), depth), (iter(value.keys()), depth)])
                elif kind is list or kind is tuple or kind is set or kind is frozenset:
                    stack.append((iter(value), depth))
            per_root.append({"name": name, "entries": len(root), "snapshot_shallow_bytes": sys.getsizeof(root),
                             "complete": reason is None, "types": _rows(counts)})
            stack.clear()
            if reason:
                break
    except RuntimeError:
        reason = "mutation"
        per_root.append({"name": name, "entries": len(root), "snapshot_shallow_bytes": sys.getsizeof(root),
                         "complete": False, "types": _rows(counts)})
    finally:
        stack.clear()
        seen.clear()
    return {"coverage": "brief_cache_and_one_source", "metric": "snapshot_reachable_bytes",
            "complete": reason is None, "stop_reason": reason, "roots": per_root,
            "types": _rows(union), "steps": steps, "elapsed_s": time.monotonic() - started,
            "metadata_estimate_high_bytes": metadata_high}


def _rows(counts: dict[str, list[int]]) -> list[dict]:
    return [{"type": k, "count": v[0], "bytes": v[1]}
            for k, v in sorted(counts.items(), key=lambda item: item[1][1], reverse=True)]


def _start_ticks(pid: int) -> int:
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rfind(")") + 2:].split()[19])


def _peer(sock: socket.socket) -> tuple[int, int]:
    # Fail closed on platforms without Linux credentials; no permissive fallback.
    pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return pid, uid


def _read_message(sock: socket.socket, limit: int, seconds: float) -> bytes:
    """One EOF-delimited message, absolute timeout and bounded allocation."""
    end = time.monotonic() + seconds
    data = bytearray()
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("diagnostic_read_timeout")
        sock.settimeout(remaining)
        part = sock.recv(min(4096, limit + 1 - len(data)))
        if not part:
            return bytes(data)
        data.extend(part)
        if len(data) > limit:
            raise ValueError("diagnostic_message_too_large")


def _path_shape(path: Path) -> bool:
    return (path.is_absolute() and ".." not in path.parts and path.name == "owner.sock"
            and path.parent.name.startswith("om-owner-top-"))


def _service_path(path: Path) -> bool:
    return _path_shape(path) and path.parent.parent == Path(f"/run/om-owner-diagnostics-{os.getuid()}")


def _service_parent(path: Path) -> bool:
    """Admin bootstraps this one base; receiver creates only a fresh child."""
    base = path.parent.parent
    record = base.lstat()
    return (_service_path(path) and stat.S_ISDIR(record.st_mode)
            and stat.S_IMODE(record.st_mode) == 0o700 and record.st_uid == os.getuid()
            and all(not stat.S_ISLNK(parent.lstat().st_mode) for parent in base.parents))


def _endpoint(path: Path) -> bool:
    if not _path_shape(path):
        return False
    for part in (path, *path.parents):
        if stat.S_ISLNK(part.lstat().st_mode):
            return False
    if _service_path(path) and not _service_parent(path):
        return False
    parent, endpoint = path.parent.stat(), path.stat()
    return (parent.st_uid == endpoint.st_uid == os.getuid()
            and stat.S_IMODE(parent.st_mode) == 0o700 and stat.S_ISDIR(parent.st_mode)
            and stat.S_IMODE(endpoint.st_mode) == 0o600 and stat.S_ISSOCK(endpoint.st_mode))


class OwnerDiagnostic:
    """One explicit receiver attempt on a natural heartbeat, shared by sources."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.done = False

    @classmethod
    def from_environment(cls) -> OwnerDiagnostic | None:
        value = os.environ.get("OM_OWNER_DIAGNOSTIC_SOCKET")
        if not value or sys.platform != "linux":
            return None
        path = Path(value)
        return cls(path) if _service_path(path) else None

    def poll(self, snapshots: Callable[[], dict[str, dict]], *, cancelled: Callable[[], bool]) -> None:
        # Only a short claim lock, never a business lock, traversal lock or new thread.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self.done:
                return
            if cancelled():
                self.done = True
                return
            try:
                exists = self.path.lstat()
            except FileNotFoundError:
                # Wait for the explicit receiver on existing heartbeats, even
                # after startup warmup. Its accept window remains bounded.
                return
            self.done = True  # consume even permission/identity/transport failures
        except (OSError, ValueError):
            self.done = True
            return
        finally:
            self._lock.release()
        del exists
        try:
            if not _endpoint(self.path):
                return
            deadline = time.monotonic() + Limits().seconds
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(IO_SECONDS)
                client.connect(str(self.path))
                if _peer(client)[1] != os.getuid():
                    return
                raw = _read_message(client, 1024, IO_SECONDS)
                request = json.loads(raw)
                if (type(request) is not dict or set(request) != {"schema", "pid", "start_ticks", "request_id"}
                        or type(request["schema"]) is not int or request["schema"] != 1 or type(request["pid"]) is not int
                        or type(request["start_ticks"]) is not int or request["pid"] != os.getpid()
                        or request["start_ticks"] != _start_ticks(os.getpid())
                        or type(request["request_id"]) is not str
                        or re.fullmatch("[0-9a-f]{32}", request["request_id"]) is None):
                    return
                report = aggregate(snapshots(), deadline=deadline, cancelled=cancelled)
                if cancelled():
                    return
                wire = json.dumps({"schema": 1, "pid": os.getpid(), "start_ticks": request["start_ticks"],
                                   "request_id": request["request_id"], "report": report},
                                  allow_nan=False, separators=(",", ":")).encode()
                if len(wire) <= MAX_PACKET:
                    client.settimeout(min(IO_SECONDS, max(0.001, deadline-time.monotonic())))
                    client.sendall(wire)  # total socket timeout, no reconnect/retry
                    client.shutdown(socket.SHUT_WR)
        except Exception:
            # Never log exception text/payload, mutate service status, or fail business work.
            return


def receive(path: Path, *, pid: int, start_ticks: int, seconds: float = 120) -> dict:
    """Explicit operator-only foreground receiver; owns only its private socket dir."""
    if seconds <= 0 or seconds > 120 or not _path_shape(path):
        raise ValueError("invalid_receiver_scope")
    if not path.is_absolute() or _start_ticks(pid) != start_ticks:
        raise ValueError("identity_changed")
    if _service_path(path) and not _service_parent(path):
        raise ValueError("unsafe_service_base")
    end = time.monotonic() + seconds
    path.parent.mkdir(mode=0o700)  # existing directory refused, never adopted/deleted
    directory = path.parent.stat()
    endpoint = None
    try:
        if stat.S_IMODE(directory.st_mode) != 0o700 or directory.st_uid != os.getuid():
            raise ValueError("unsafe_directory")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            path.chmod(0o600)
            endpoint = path.lstat()
            server.listen(1)
            server.settimeout(max(0.001, end-time.monotonic()))
            with server.accept()[0] as client:
                if _peer(client) != (pid, os.getuid()) or _start_ticks(pid) != start_ticks:
                    raise ValueError("peer_identity_changed")
                client.settimeout(max(0.001, min(IO_SECONDS, end-time.monotonic())))
                request_id = os.urandom(16).hex()
                client.sendall(json.dumps({"schema": 1, "pid": pid, "start_ticks": start_ticks,
                                           "request_id": request_id}).encode())
                client.shutdown(socket.SHUT_WR)
                raw = _read_message(client, MAX_PACKET, min(1.0, end-time.monotonic()))
                response = json.loads(raw)
                if (not _safe_response(response) or response["pid"] != pid
                        or response["start_ticks"] != start_ticks or response["request_id"] != request_id):
                    raise ValueError("invalid_response")
                return response
    finally:
        # Refuse replacements; do not unlink somebody else's path or remove nonempty dirs.
        if endpoint is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (endpoint.st_dev, endpoint.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
        try:
            current = path.parent.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (directory.st_dev, directory.st_ino):
            try:
                path.parent.rmdir()
            except OSError:
                pass


def _safe_response(value: object) -> bool:
    """Strict output allowlist: receiver never prints arbitrary peer JSON."""
    def number(v):
        return ((type(v) is int and 0 <= v <= 2**63-1)
                or (type(v) is float and math.isfinite(v) and v >= 0))

    def rows(v):
        return (type(v) is list and len(v) <= len(KINDS)
                and all(type(r) is dict and set(r) == {"type", "count", "bytes"}
                        and type(r["type"]) is str and r["type"] in KINDS.values()
                        and type(r["count"]) is int and number(r["count"])
                        and type(r["bytes"]) is int and number(r["bytes"]) for r in v))
    if type(value) is not dict or set(value) != {"schema", "pid", "start_ticks", "request_id", "report"}:
        return False
    if (type(value["schema"]) is not int or value["schema"] != 1
            or type(value["pid"]) is not int or not number(value["pid"])
            or type(value["start_ticks"]) is not int or not number(value["start_ticks"])
            or type(value["request_id"]) is not str or re.fullmatch("[0-9a-f]{32}", value["request_id"]) is None):
        return False
    r = value["report"]
    return (type(r) is dict and set(r) == {"coverage", "metric", "complete", "stop_reason", "roots", "types", "steps", "elapsed_s", "metadata_estimate_high_bytes"}
            and r["coverage"] == "brief_cache_and_one_source" and r["metric"] == "snapshot_reachable_bytes"
            and type(r["complete"]) is bool and r["stop_reason"] in STOP_REASONS
            and rows(r["types"]) and all(number(r[k]) for k in ("steps", "elapsed_s", "metadata_estimate_high_bytes"))
            and type(r["roots"]) is list and len(r["roots"]) <= 3
            and all(type(root) is dict and set(root) == {"name", "entries", "snapshot_shallow_bytes", "complete", "types"}
                    and root["name"] in ROOT_NAMES and type(root["complete"]) is bool
                    and number(root["entries"]) and number(root["snapshot_shallow_bytes"]) and rows(root["types"])
                    for root in r["roots"]))


def receive_main() -> int:
    parser = argparse.ArgumentParser(description="One explicit payload-free owner diagnostic request (Linux only).")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--start-ticks", type=int, required=True)
    args = parser.parse_args()
    try:
        if not _service_path(args.socket) or not _service_parent(args.socket):
            raise ValueError("receiver_requires_run")
        print(json.dumps(receive(args.socket, pid=args.pid, start_ticks=args.start_ticks), allow_nan=False))
        return 0
    except (OSError, ValueError, AttributeError, struct.error):
        print("owner diagnostic unavailable; no business action taken", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(receive_main())
