"""Bounded, descriptor-based project reading; file content never grants authority."""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from src.application.ledger.api import decode_evidence_cursor, encode_evidence_cursor
from src.application.research.redaction import redact_text
from src.application.secret_store import INBOUND_OPERATION_HMAC_KEY, SecretError, resolve_secret

MAX_FILE_BYTES = 1024 * 1024
MAX_QUERY_BYTES = 8 * MAX_FILE_BYTES
MAX_FILES = 200
MAX_METADATA = 40000
MAX_DIRECTORY = 10000
MAX_DEPTH = 32
MAX_OUTPUT_BYTES = 9000
MAX_PROJECT_OUTPUT_BYTES = 24000
REDACTION_VERSION = "project-lines-v1"
_EXCLUDED = {".git", "node_modules", "__pycache__", ".venv", ".ruff_cache", ".pytest_cache", "dist", "build", "reviews", "plans", "gateflow", "credentials", "secrets", "sessions", "memory", "cache"}
_TOP = {"docs", "src", "domain", "agent-runtime"}
_PROJECT_ROOT_ORDER = ("src", "domain", "agent-runtime", "docs", "configs")
_PROJECT_TRAVERSAL_VERSION = "project-navigation-v3"
_SUFFIXES = {".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".toml", ".rst", ".css", ".html", ".sh"}

_RETRIES: ContextVar[list[int] | None] = ContextVar("project_reader_retries", default=None)


@contextmanager
def reader_query_context():
    """One transient syscall retry shared by all reads in this logical query."""
    token = _RETRIES.set([0]) if _RETRIES.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _RETRIES.reset(token)


def _query(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with reader_query_context():
            return fn(*args, **kwargs)
    return wrapped


class ProjectReaderError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _check(deadline_monotonic: float | None, cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ProjectReaderError("cancelled")
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise ProjectReaderError("time_deadline")


def _parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or len(name) > 2048 or "\x00" in name or "\\" in name:
        raise ProjectReaderError("permission_denied")
    if not name:
        return ()
    parts = tuple(name.split("/"))
    if any(p in {"", ".", ".."} for p in parts):
        raise ProjectReaderError("permission_denied")
    return parts


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _io(fn: Callable[[], Any], deadline: float | None, cancelled: Callable[[], bool] | None) -> Any:
    for attempt in range(2):
        _check(deadline, cancelled)
        try:
            return fn()
        except OSError as exc:
            if attempt == 0 and exc.errno in {errno.EINTR, errno.EAGAIN}:
                retries = _RETRIES.get()
                if retries is not None and retries[0] == 0:
                    retries[0] += 1
                    continue
            code = "not_found" if exc.errno == errno.ENOENT else "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR} else "io_error"
            raise ProjectReaderError(code) from None
    raise ProjectReaderError("io_error")


def _directory(root: Path, name: str, deadline: float | None, cancelled: Callable[[], bool] | None, *, classify_types: bool = False) -> int:
    if not all(hasattr(os, attr) for attr in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")) or os.open not in os.supports_dir_fd:
        raise ProjectReaderError("capability_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = _io(lambda: os.dup(root) if isinstance(root, int) else os.open(root, flags), deadline, cancelled)
    try:
        parts = _parts(name)
        for index, part in enumerate(parts):
            if classify_types:
                info = _io(lambda: os.stat(part, dir_fd=descriptor, follow_symlinks=False), deadline, cancelled)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and _allowed("/".join(parts[:index + 1])):
                    raise ProjectReaderError("not_directory")
            child = _io(lambda: os.open(part, flags, dir_fd=descriptor), deadline, cancelled)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@_query
def read_bytes(root: Path, relative_name: str, *, deadline_monotonic: float | None = None,
               cancelled: Callable[[], bool] | None = None, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Read exact bounded bytes; the caller owns resource/account authorization."""
    parts = _parts(relative_name)
    if not parts:
        raise ProjectReaderError("unsupported")
    parent = _directory(root, "/".join(parts[:-1]), deadline_monotonic, cancelled)
    descriptor = None
    try:
        descriptor = _io(lambda: os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent), deadline_monotonic, cancelled)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectReaderError("unsupported")
        if before.st_nlink != 1:
            raise ProjectReaderError("permission_denied")
        limit = min(MAX_FILE_BYTES, max_bytes)
        if before.st_size > limit:
            raise ProjectReaderError("file_too_large")
        chunks, size = [], 0
        while size <= limit:
            chunk = _io(lambda: os.read(descriptor, min(65536, limit + 1 - size)), deadline_monotonic, cancelled)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or size != before.st_size or after.st_nlink != 1:
            raise ProjectReaderError("source_changed")
        if size > limit:
            raise ProjectReaderError("file_too_large")
        _check(deadline_monotonic, cancelled)
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


@_query
def read_tail_bytes(root: Path, relative_name: str, *, deadline_monotonic: float | None = None,
                    cancelled: Callable[[], bool] | None = None) -> tuple[bytes, bool, int]:
    """Read at most 1 MiB from a regular file's end through the same safe path walk."""
    parts = _parts(relative_name)
    if not parts:
        raise ProjectReaderError("unsupported")
    parent = _directory(root, "/".join(parts[:-1]), deadline_monotonic, cancelled)
    descriptor = None
    try:
        descriptor = _io(lambda: os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent), deadline_monotonic, cancelled)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectReaderError("unsupported")
        if before.st_nlink != 1:
            raise ProjectReaderError("permission_denied")
        truncated = before.st_size > MAX_FILE_BYTES
        os.lseek(descriptor, max(0, before.st_size - MAX_FILE_BYTES), os.SEEK_SET)
        chunks, size = [], 0
        while size < min(before.st_size, MAX_FILE_BYTES):
            chunk = _io(lambda: os.read(descriptor, min(65536, MAX_FILE_BYTES - size)), deadline_monotonic, cancelled)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or size != min(before.st_size, MAX_FILE_BYTES) or after.st_nlink != 1:
            raise ProjectReaderError("source_changed")
        _check(deadline_monotonic, cancelled)
        return b"".join(chunks), truncated, before.st_size
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


@_query
def list_names(root: Path, relative_name: str = "", *, deadline_monotonic: float | None = None,
               cancelled: Callable[[], bool] | None = None, max_entries: int = MAX_DIRECTORY) -> tuple[list[str], dict[str, Any]]:
    descriptor = _directory(root, relative_name, deadline_monotonic, cancelled)
    try:
        before = os.fstat(descriptor)
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                _check(deadline_monotonic, cancelled)
                names.append(entry.name)
                if len(names) > min(max_entries, MAX_DIRECTORY):
                    raise ProjectReaderError("directory_too_large")
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise ProjectReaderError("source_changed")
        names.sort()
        return names, {"dev": before.st_dev, "ino": before.st_ino, "mtime_ns": before.st_mtime_ns, "hash": digest(names)}
    except OSError:
        raise ProjectReaderError("io_error") from None
    finally:
        os.close(descriptor)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key() -> str:
    try:
        secret = resolve_secret(INBOUND_OPERATION_HMAC_KEY)
    except SecretError:
        raise ProjectReaderError("continuation_unavailable") from None
    if not secret:
        raise ProjectReaderError("continuation_unavailable")
    return hmac.new(secret.encode(), b"options-monitor/project-files/v1", hashlib.sha256).hexdigest()


def decode_cursor(cursor: str | None, binding: str) -> dict[str, Any] | None:
    if not cursor:
        return None
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise ProjectReaderError("cursor_invalidated")
    try:
        state = decode_evidence_cursor(cursor, _key())
    except ProjectReaderError:
        raise
    except ValueError:
        raise ProjectReaderError("cursor_invalidated") from None
    if state.get("binding") != binding:
        raise ProjectReaderError("cursor_invalidated")
    return state


def set_continuation(result: dict[str, Any], state: dict[str, Any] | None) -> None:
    result["next_cursor"] = None
    if state is None:
        return
    try:
        cursor = encode_evidence_cursor(state, _key())
        if len(cursor) > 8192:
            raise ProjectReaderError("scope_too_broad")
        result["next_cursor"] = cursor
    except ProjectReaderError as exc:
        if exc.code != "continuation_unavailable":
            raise
        result["continuation_status"] = exc.code
        coverage = result.get("coverage")
        if isinstance(coverage, dict):
            coverage.update(status="partial", complete=False, has_more=True)
        if "body_complete" in result:
            result["body_complete"] = False


def redacted_text(raw: bytes) -> str:
    if len(raw) > MAX_FILE_BYTES:
        raise ProjectReaderError("file_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProjectReaderError("unsupported") from None
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise ProjectReaderError("unsupported")
    return redact_text(text, preserve_newlines=True)


@_query
def page_text(raw: bytes, *, relative_name: str, scope: dict[str, Any], cursor: str | None = None,
              start_line: int = 1, max_lines: int = 80, resource: str = "project",
              project_token_limit: int = 2800,
              deadline_monotonic: float | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    _check(deadline_monotonic, cancelled)
    if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 300:
        raise ProjectReaderError("INPUT_ERROR")
    binding = digest({"resource": resource, "action": "read", "name": relative_name, "scope": scope})
    state = decode_cursor(cursor, binding)
    raw_hash = hashlib.sha256(raw).hexdigest()
    if state and state.get("hash") != raw_hash:
        raise ProjectReaderError("source_changed")
    text = redacted_text(raw)
    _check(deadline_monotonic, cancelled)
    lines = text.splitlines(keepends=True) or [""]
    offset = int(state["offset"]) if state else sum(len(line) for line in lines[:start_line - 1])
    if not 0 <= offset <= len(text):
        raise ProjectReaderError("cursor_invalidated")
    line = text.count("\n", 0, offset) + 1
    char = offset - (text.rfind("\n", 0, offset) + 1)
    end = offset
    for _ in range(max_lines):
        boundary = text.find("\n", end)
        end = len(text) if boundary < 0 else boundary + 1
        if end == len(text):
            break
    end = min(end, offset + (MAX_PROJECT_OUTPUT_BYTES if resource == "project" else 4000))
    lower, upper, best = offset, end, None
    while True:
        _check(deadline_monotonic, cancelled)
        result = {"text": text[offset:end], "relative_name": redact_text(relative_name),
                  "source": {"resource": resource, "revision": raw_hash, "content_hash": raw_hash, "redaction_version": REDACTION_VERSION},
                  "body_range": {"start_line": line, "start_char": char, "end_line": text.count("\n", 0, end) + 1, "end_char": end - (text.rfind("\n", 0, end) + 1)},
                  "body_complete": end == len(text),
                  "coverage": {"status": "complete", "complete_for": "point", "has_more": end < len(text)},
                  "freshness": {"status": "not_applicable"}, "action": "read", "resource": resource}
        result["source"].update(relative_name=result["relative_name"], body_range=result["body_range"])
        result["scope"] = {**{k: scope[k] for k in ("account", "market", "config_key", "run_id") if k in scope}, "resource": resource, "relative_name": result["relative_name"], "revision": raw_hash, "body_range": result["body_range"]}
        set_continuation(result, {"binding": binding, "hash": raw_hash, "offset": end} if end < len(text) else None)
        if resource != "project":
            if len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_OUTPUT_BYTES:
                return result
            if end <= offset + 1:
                raise ProjectReaderError("scope_too_broad")
            end = offset + max(1, (end - offset) // 2)
            continue
        if _project_output_fits(result, project_token_limit):
            lower, best = end, result
        else:
            upper = end - 1
        if upper <= lower:
            if best is not None:
                return best
            raise ProjectReaderError("scope_too_broad")
        end = (lower + upper + 1) // 2


def _project_output_fits(result: dict[str, Any], token_limit: int = 2800) -> bool:
    # Reserve room for the Bot contract and Host metadata before consuming a page.
    # Scope is copied both at the observation root and inside coverage.
    serialized = json.dumps([result, *(result.get(key) for key in
        ("source", "scope", "scope", "coverage", "freshness"))], ensure_ascii=False)
    non_ascii = sum(ord(char) > 127 for char in serialized)
    estimate = ((len(serialized) - non_ascii) / 4 + non_ascii) * 1.10
    return estimate <= token_limit and len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_PROJECT_OUTPUT_BYTES


def _search_entry(raw: bytes, name: str, query: str) -> dict[str, Any] | None:
    text = redacted_text(raw)
    found = text.find(query)
    if found < 0:
        return None
    start = text.rfind("\n", 0, found) + 1
    end = text.find("\n", found + len(query))
    end = len(text) if end < 0 else end
    for _ in range(8):
        start = text.rfind("\n", 0, max(0, start - 1)) + 1 if start else 0
        boundary = text.find("\n", min(len(text), end + 1))
        end = len(text) if boundary < 0 else boundary
    if end - start > 1800:
        start = max(start, found - 640)
        end = min(end, start + 1800)
    return {"relative_name": redact_text(name), "line": text.count("\n", 0, found) + 1,
            "text": text[start:end], "context_start_line": text.count("\n", 0, start) + 1,
            "context_end_line": text.count("\n", 0, max(start, end - 1)) + 1,
            "content_hash": hashlib.sha256(raw).hexdigest()}


def _allowed(name: str, *, directory: bool = False) -> bool:
    parts = _parts(name)
    if redact_text(name) != name:
        return False
    if any(p.lower() in _EXCLUDED or p.startswith(".") or any(x in p.lower() for x in ("credential", "secret")) for p in parts):
        return False
    if not parts:
        return directory
    if any(p in {"config.yaml", "config.yml", "config.json"} or (p.startswith("config.") and p.endswith(".json")) for p in parts):
        return False
    if parts[0] == "configs":
        return name in ({"configs", "configs/examples"} if directory else {"configs/examples/config.yaml.example"})
    if parts[0] not in _TOP:
        return not directory and len(parts) == 1 and parts[0].endswith(".md")
    return directory or Path(name).suffix.lower() in _SUFFIXES


@_query
def project_files(root: Path, *, action: str = "list", relative_name: str = "", query: str = "",
                  start_line: int = 1, max_lines: int = 80, cursor: str | None = None,
                  scope: dict[str, Any], deadline_monotonic: float | None = None,
                  cancelled: Callable[[], bool] | None = None,
                  project_token_limit: int = 2800) -> dict[str, Any]:
    """Project allowlist facade; returned cursors are navigation, never authority."""
    _parts(relative_name)
    if action not in {"list", "search", "read"} or not isinstance(query, str) or len(query) > 512:
        raise ProjectReaderError("INPUT_ERROR")
    if action == "read" and query:
        raise ProjectReaderError("INPUT_ERROR")
    if action != "read" and (start_line != 1 or max_lines != 80):
        raise ProjectReaderError("line_controls_require_read")
    root_fd = _directory(root, "", deadline_monotonic, cancelled)
    try:
        root_stat = os.fstat(root_fd)
        trusted = {**scope, "root": [root_stat.st_dev, root_stat.st_ino]}
        return _project_files_at(root_fd, action=action, relative_name=relative_name, query=query,
                                 start_line=start_line, max_lines=max_lines, cursor=cursor,
                                 trusted=trusted, deadline_monotonic=deadline_monotonic,
                                 cancelled=cancelled, project_token_limit=project_token_limit)
    finally:
        os.close(root_fd)


def _project_files_at(root: int, *, action: str, relative_name: str, query: str,
                      start_line: int, max_lines: int, cursor: str | None, trusted: dict[str, Any],
                      deadline_monotonic: float | None, cancelled: Callable[[], bool] | None,
                      project_token_limit: int) -> dict[str, Any]:
    if not (_allowed(relative_name) or _allowed(relative_name, directory=True)):
        raise ProjectReaderError("permission_denied")
    single_file = False
    if relative_name:
        parts = _parts(relative_name)
        parent = _directory(root, "/".join(parts[:-1]), deadline_monotonic, cancelled, classify_types=True)
        try:
            info = _io(lambda: os.stat(parts[-1], dir_fd=parent, follow_symlinks=False), deadline_monotonic, cancelled)
        finally:
            os.close(parent)
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or not _allowed(relative_name):
                raise ProjectReaderError("permission_denied")
            single_file = True
        elif not stat.S_ISDIR(info.st_mode):
            raise ProjectReaderError("permission_denied" if stat.S_ISLNK(info.st_mode) else "unsupported")
    if action == "read":
        if not single_file:
            raise ProjectReaderError("is_directory")
        raw = read_bytes(root, relative_name, deadline_monotonic=deadline_monotonic, cancelled=cancelled)
        return page_text(
            raw, relative_name=relative_name, scope=trusted, cursor=cursor,
            start_line=start_line, max_lines=max_lines,
            deadline_monotonic=deadline_monotonic, cancelled=cancelled,
            project_token_limit=project_token_limit,
        )
    if action == "list" and single_file:
        raise ProjectReaderError("not_directory")
    if action == "search" and not query:
        raise ProjectReaderError("INPUT_ERROR")
    binding = digest({"scope": trusted, "action": action, "resource": "project", "name": relative_name, "query": query,
                      "traversal_version": _PROJECT_TRAVERSAL_VERSION})
    state = decode_cursor(cursor, binding)
    if cursor and start_line != 1:
        raise ProjectReaderError("cursor_invalidated")
    if single_file and cursor:
        raise ProjectReaderError("cursor_invalidated")
    stack = [] if single_file else state["stack"] if state else [{"name": relative_name, "index": 0}]
    page_start = [{"name": frame["name"], "index": frame["index"]} for frame in stack]
    names_by_dir, identities_by_dir, metadata = {}, {}, 0
    for frame in stack:
        remaining_metadata = MAX_METADATA - metadata
        try:
            names, identity = list_names(root, frame["name"], deadline_monotonic=deadline_monotonic, cancelled=cancelled, max_entries=min(MAX_DIRECTORY, max(0, remaining_metadata - 1)))
        except ProjectReaderError as exc:
            if exc.code == "directory_too_large" and remaining_metadata < MAX_DIRECTORY:
                raise ProjectReaderError("scope_too_broad") from None
            raise
        metadata += len(names)
        if metadata > MAX_METADATA:
            raise ProjectReaderError("scope_too_broad")
        if "identity" in frame and frame["identity"] != identity:
            raise ProjectReaderError("source_changed")
        frame["identity"] = identity
        identities_by_dir[frame["name"]] = identity
        if not frame["name"]:
            names.sort(key=lambda name: (_PROJECT_ROOT_ORDER.index(name) if name in _PROJECT_ROOT_ORDER else len(_PROJECT_ROOT_ORDER), name))
        names_by_dir[frame["name"]] = names
    entries, scanned, byte_count, skipped = [], 0, 0, 0
    file_revision = None
    if single_file:
        raw = read_bytes(root, relative_name, deadline_monotonic=deadline_monotonic, cancelled=cancelled)
        entry = _search_entry(raw, relative_name, query)
        file_revision = hashlib.sha256(raw).hexdigest()
        scanned, byte_count = 1, len(raw)
        if entry:
            entries.append(entry)
    while stack and len(entries) < 40 and scanned < MAX_FILES:
        _check(deadline_monotonic, cancelled)
        frame = stack[-1]
        names = names_by_dir[frame["name"]]
        if frame["index"] >= len(names):
            stack.pop()
            continue
        name = "/".join(filter(None, (frame["name"], names[frame["index"]])))
        if not (_allowed(name) or _allowed(name, directory=True)):
            frame["index"] += 1
            continue
        parent = _directory(root, frame["name"], deadline_monotonic, cancelled)
        try:
            info = _io(lambda: os.stat(names[frame["index"]], dir_fd=parent, follow_symlinks=False), deadline_monotonic, cancelled)
        finally:
            os.close(parent)
        if stat.S_ISDIR(info.st_mode):
            if not _allowed(name, directory=True):
                frame["index"] += 1
                continue
            if action == "list":
                frame["index"] += 1
                entries.append({"relative_name": redact_text(name), "kind": "directory"})
                if len(json.dumps(entries, ensure_ascii=False).encode()) > 2000:
                    break
                continue
            if len(stack) >= MAX_DEPTH:
                raise ProjectReaderError("scope_too_broad")
            remaining_metadata = MAX_METADATA - metadata
            if remaining_metadata <= 1:
                if not scanned:
                    raise ProjectReaderError("scope_too_broad")
                break
            try:
                child_names, identity = list_names(root, name, deadline_monotonic=deadline_monotonic, cancelled=cancelled,
                                                  max_entries=min(MAX_DIRECTORY, remaining_metadata - 1))
            except ProjectReaderError as exc:
                if exc.code != "directory_too_large" or remaining_metadata > MAX_DIRECTORY:
                    raise
                if not scanned:
                    raise ProjectReaderError("scope_too_broad") from None
                metadata = MAX_METADATA
                break
            metadata += len(child_names)
            names_by_dir[name] = child_names
            identities_by_dir[name] = identity
            frame["index"] += 1
            stack.append({"name": name, "index": 0, "identity": identity})
            continue
        if not _allowed(name) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            skipped += 1
            frame["index"] += 1
            continue
        if action == "search" and byte_count + min(info.st_size, MAX_FILE_BYTES) > MAX_QUERY_BYTES:
            break
        frame["index"] += 1
        scanned += 1
        if action == "list":
            entry = {"relative_name": redact_text(name), "kind": "file", "size_bytes": info.st_size}
        else:
            try:
                raw = read_bytes(root, name, deadline_monotonic=deadline_monotonic, cancelled=cancelled, max_bytes=MAX_QUERY_BYTES - byte_count)
                byte_count += len(raw)
                entry = _search_entry(raw, name, query)
            except ProjectReaderError as exc:
                if exc.code not in {"unsupported", "file_too_large", "permission_denied", "not_found"}:
                    raise
                skipped += 1
                continue
            if entry is None:
                continue
        entries.append(entry)
        if len(json.dumps(entries, ensure_ascii=False).encode()) > 2000:
            break
    result = {"entries": entries, "scanned": scanned, "scanned_bytes": byte_count, "metadata_entries": metadata,
              "skipped": skipped, "remaining": None if stack else 0,
              "source": {"resource": "project", "revision": digest({"entries": entries, "file": file_revision, "directories": identities_by_dir, "start": page_start,
                                                                    "end": [{"name": frame["name"], "index": frame["index"]} for frame in stack]}), "content_hash": None},
              "coverage": {"status": "partial" if stack or skipped else "complete", "complete_for": "requested_page", "has_more": bool(stack), "complete": not stack and not skipped},
              "freshness": {"status": "not_applicable"}, "action": action, "resource": "project"}
    result["source"].update(relative_name=redact_text(relative_name))
    result["scope"] = {**{k: trusted[k] for k in ("account", "market", "config_key") if k in trusted}, "resource": "project", "relative_name": redact_text(relative_name)}
    set_continuation(result, {"binding": binding, "stack": stack} if stack else None)
    _check(deadline_monotonic, cancelled)
    if not _project_output_fits(result):
        raise ProjectReaderError("scope_too_broad")
    return result
