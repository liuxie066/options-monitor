from __future__ import annotations

import ast
from collections import defaultdict
from datetime import datetime, timezone
import fnmatch
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import tokenize
import heapq
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.store_resolution import LEDGER_DB_RELATIVE_PATH


SCHEMA_VERSION = "storage_runtime_baseline.v1"
SOURCE_INVENTORY_RELATIVE_PATH = Path(
    "docs/architecture/data-storage-runtime-source-inventory.v1.json"
)
RUNTIME_SUBROOTS = (
    "output_runs",
    "output_accounts",
    "output_shared",
    "output",
    "logs",
)
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
LARGEST_FILE_LIMIT = 20
SQLITE_SNAPSHOT_ATTEMPTS = 3
GIB = 1024**3

# These are reporting heuristics only. They do not authorize movement or
# deletion. Later tiering work must replace them with a reviewed backend policy.
RESEARCH_HOT_MAX_AGE_DAYS = 30
COLD_CANDIDATE_MIN_AGE_DAYS = 180
COLD_CANDIDATE_MIN_BYTES = 64 * 1024 * 1024

_SQLITE_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "trade_events": ("event_json",),
    "position_lots": ("fields_json",),
    "assigned_stock_events": ("event_json",),
    "trade_lifecycle_cases": ("raw_json",),
    "trade_lifecycle_evidence": ("raw_json",),
    "trade_lifecycle_source_consumptions": ("raw_json",),
    "trade_lifecycle_allocations": ("raw_json",),
    "trade_lifecycle_timing_policies": ("raw_json",),
    "trade_lifecycle_migration_receipts": ("raw_json",),
    "strategy_group_identities": ("raw_json",),
    "combo_pair_inferences": ("evidence_json", "alternatives_json"),
}

_REFERENCE_PATH_KEYS = (
    "relpath",
    "path",
    "file",
    "file_path",
    "payload_relpath",
    "blob_relpath",
    "partition_relpath",
)
_REFERENCE_HASH_KEYS = ("sha256", "content_sha256", "payload_sha256", "blob_sha256")
_REFERENCE_SIZE_KEYS = (
    "size_bytes",
    "bytes",
    "content_bytes",
    "payload_size_bytes",
    "blob_size_bytes",
)


def collect_storage_runtime_baseline(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    ledger_sqlite: str | Path | None = None,
    history_reports: Sequence[str | Path] | None = None,
    output: str | Path | None = None,
    allow_external_ledger: bool = False,
    overwrite: bool = False,
    now_fn: Callable[[], datetime] | None = None,
    source_inventory_path: str | Path | None = None,
) -> dict[str, Any]:
    """Collect a payload-free, read-only storage and capacity baseline."""

    base = Path(repo_root).expanduser().resolve()
    root = _required_runtime_root(runtime_root)
    ledger_path, ledger_source = _resolve_ledger_path(
        root=root,
        repo_root=base,
        value=ledger_sqlite,
        allow_external=allow_external_ledger,
    )
    observed_at = _utc_now(now_fn)
    inventory_path = (
        _resolve_input_path(source_inventory_path, base=base)
        if source_inventory_path is not None
        else base / SOURCE_INVENTORY_RELATIVE_PATH
    )
    resolved_history_reports = [
        _resolve_input_path(item, base=base) for item in (history_reports or ())
    ]
    source_inventory = _collect_source_inventory(repo_root=base, manifest_path=inventory_path)
    runtime_storage, research_file_rows = _collect_runtime_storage(root=root, observed_at=observed_at)
    sqlite_payload = _collect_sqlite_metadata(ledger_path)
    research_storage = _collect_research_storage(
        root=root,
        observed_at=observed_at,
        file_rows=research_file_rows,
        history_reports=resolved_history_reports,
    )
    disk = shutil.disk_usage(root)
    thresholds = _capacity_thresholds(
        disk_total_bytes=int(disk.total),
        disk_free_bytes=int(disk.free),
        research_storage=research_storage,
    )
    source_statuses = {
        str(sqlite_payload.get("status")),
        str(research_storage.get("status")),
    }
    overall_status = "complete"
    if "data_unavailable" in source_statuses or "partial_data" in source_statuses:
        overall_status = "partial_data"
    elif "missing" in source_statuses:
        overall_status = "partial_data"

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": overall_status,
        "identity": {
            "observed_at_utc": observed_at.isoformat(),
            "runtime_root": str(root),
            "ledger_sqlite": _display_runtime_path(ledger_path, root=root),
            "ledger_source": ledger_source,
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "git_sha": _git_sha(base),
            "collection_options": {
                "allow_external_ledger": bool(allow_external_ledger),
                "history_report_count": len(resolved_history_reports),
                "sqlite_snapshot_attempts": SQLITE_SNAPSHOT_ATTEMPTS,
                "runtime_subroots": list(RUNTIME_SUBROOTS),
            },
        },
        "source_inventory": source_inventory,
        "sqlite": sqlite_payload,
        "runtime_storage": runtime_storage,
        "research_storage": research_storage,
        "thresholds": thresholds,
        "safety": {
            "query_only_sqlite": True,
            "source_sqlite_connections": 0,
            "no_follow_traversal": True,
            "payload_content_reads": 0,
            "content_verification": "not_performed",
            "mutation_operations": 0,
            "automatic_actions": [],
        },
    }
    if output is not None:
        output_path = _resolve_output_path(output, base=base)
        result["identity"]["output"] = str(output_path)
        _write_report(
            output_path,
            result,
            runtime_root=root,
            overwrite=overwrite,
        )
    return result


def _required_runtime_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    try:
        root = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"runtime root not found: {raw}") from exc
    if not root.is_dir():
        raise AgentToolError(code="INPUT_ERROR", message=f"runtime root is not a directory: {root}")
    return root


def _resolve_input_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _resolve_output_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    candidate = path if path.is_absolute() else base / path
    if candidate.is_symlink():
        raise AgentToolError(code="INPUT_ERROR", message=f"baseline output must not be a symlink: {candidate}")
    return candidate.resolve()


def _resolve_ledger_path(
    *,
    root: Path,
    repo_root: Path,
    value: str | Path | None,
    allow_external: bool,
) -> tuple[Path, str]:
    if value is None or not str(value).strip():
        candidate = root / LEDGER_DB_RELATIVE_PATH
        path = candidate.resolve()
        if _path_or_parent_is_symlink(candidate, stop=root):
            raise AgentToolError(
                code="INPUT_ERROR",
                message="default ledger path must not traverse a symlink",
            )
        return path, "runtime_default"
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    path = candidate.resolve()
    if _path_or_parent_is_symlink(candidate, stop=root if _is_relative_to(path, root) else None):
        raise AgentToolError(code="INPUT_ERROR", message=f"explicit ledger path must not traverse a symlink: {candidate}")
    if not _is_relative_to(path, root) and not allow_external:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="explicit ledger path must be inside runtime root unless --allow-external-ledger is set",
        )
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise AgentToolError(code="INPUT_ERROR", message=f"explicit ledger path must be a regular file: {path}")
    return path, "explicit_external" if not _is_relative_to(path, root) else "explicit_runtime"


def _utc_now(now_fn: Callable[[], datetime] | None) -> datetime:
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _git_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _collect_source_inventory(*, repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message="source inventory manifest is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message=f"source inventory manifest is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "data_storage_runtime_source_inventory.v1":
        raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message="source inventory schema is invalid")
    rules = payload.get("discovery_rules")
    roots = payload.get("scan_roots")
    declarations = payload.get("declared_locators")
    if not isinstance(rules, list) or not isinstance(roots, list) or not isinstance(declarations, list):
        raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message="source inventory lists are invalid")

    validated_rules: list[tuple[str, Mapping[str, Any], Sequence[Any]]] = []
    rule_ids: set[str] = set()
    for raw_rule in rules:
        if not isinstance(raw_rule, dict):
            raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message="source inventory rule is invalid")
        rule_id = str(raw_rule.get("id") or "").strip()
        classifiers = raw_rule.get("classifiers")
        if not rule_id or rule_id in rule_ids or not isinstance(classifiers, list) or not classifiers:
            raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message=f"source inventory rule is incomplete: {rule_id}")
        rule_ids.add(rule_id)
        validated_rules.append((rule_id, raw_rule, classifiers))

    call_symbols = {
        str(symbol)
        for _rule_id, rule, _classifiers in validated_rules
        if rule.get("kind") == "call"
        for symbol in (rule.get("symbols") or [])
    }
    literal_values = {
        str(value)
        for _rule_id, rule, _classifiers in validated_rules
        if rule.get("kind") == "literal"
        for value in (rule.get("values") or [])
    }
    parse_errors: list[dict[str, str]] = []
    matches: list[dict[str, Any]] = []
    unclassified: list[dict[str, str]] = []
    classifier_hits = {
        rule_id: [0 for _ in classifiers]
        for rule_id, _rule, classifiers in validated_rules
    }
    for root_name in roots:
        root_rel = str(root_name or "").strip()
        root = (repo_root / root_rel).resolve()
        if not _is_relative_to(root, repo_root) or not root.is_dir():
            raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message=f"source scan root is invalid: {root_rel}")
        for path in _iter_python_files(root):
            relpath = path.relative_to(repo_root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
                calls, literals = _token_discoveries(
                    text,
                    call_symbols=call_symbols,
                    literal_values=literal_values,
                )
            except (OSError, UnicodeError, IndentationError, tokenize.TokenError) as exc:
                text = ""
                calls = {}
                literals = {}
                parse_errors.append({"path": relpath, "error_type": type(exc).__name__})
            for rule_id, raw_rule, classifiers in validated_rules:
                discovered = _discover_rule_matches(
                    raw_rule,
                    text=text,
                    calls=calls,
                    literals=literals,
                )
                for locator, count in sorted(discovered.items()):
                    classifier_index = _classifier_index(relpath, classifiers)
                    if classifier_index is None:
                        unclassified.append({"rule_id": rule_id, "path": relpath, "locator": locator})
                        continue
                    classifier_hits[rule_id][classifier_index] += count
                    classifier = classifiers[classifier_index]
                    matches.append(
                        {
                            "rule_id": rule_id,
                            "path": relpath,
                            "locator": locator,
                            "match_count": count,
                            "owner": str(classifier.get("owner") or ""),
                            "operation": str(classifier.get("operation") or ""),
                            "history_dimension": str(classifier.get("history_dimension") or ""),
                            "path_class": str(classifier.get("path_class") or ""),
                            "later_phase": str(classifier.get("later_phase") or ""),
                        }
                    )

    stale_classifiers: list[dict[str, str]] = []
    for rule_id, _raw_rule, classifiers in validated_rules:
        for index, count in enumerate(classifier_hits[rule_id]):
            if count == 0:
                classifier = classifiers[index]
                stale_classifiers.append(
                    {
                        "rule_id": rule_id,
                        "path_glob": str(classifier.get("path_glob") or ""),
                    }
                )

    stale_locators: list[dict[str, str]] = []
    for item in declarations:
        if not isinstance(item, dict):
            stale_locators.append({"path": "", "locator": "invalid_declaration"})
            continue
        relpath = str(item.get("path") or "").strip()
        locator = str(item.get("locator") or "").strip()
        path = (repo_root / relpath).resolve()
        if (
            not relpath
            or not locator
            or not _is_relative_to(path, repo_root)
            or not path.is_file()
            or path.is_symlink()
        ):
            stale_locators.append({"path": relpath, "locator": locator})
            continue
        try:
            present = locator in path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            present = False
        if not present:
            stale_locators.append({"path": relpath, "locator": locator})

    if parse_errors or stale_classifiers or stale_locators or unclassified:
        raise AgentToolError(
            code="SOURCE_INVENTORY_ERROR",
            message="source inventory has stale or unclassified production matches",
            details={
                "parse_errors": parse_errors,
                "stale_classifiers": stale_classifiers,
                "stale_locators": stale_locators,
                "unclassified": unclassified,
            },
        )
    return {
        "schema_version": str(payload["schema_version"]),
        "status": "complete",
        "manifest_path": manifest_path.relative_to(repo_root).as_posix(),
        "scan_roots": [str(item) for item in roots],
        "rule_count": len(rules),
        "declared_locator_count": len(declarations),
        "classified_match_count": len(matches),
        "matches": sorted(matches, key=lambda item: (item["rule_id"], item["path"], item["locator"])),
        "stale_classifiers": [],
        "stale_locators": [],
        "unclassified_matches": [],
        "ignored_matches": list(payload.get("ignores") or []),
    }


def _iter_python_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name, reverse=True):
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in {"__pycache__", ".venv"}:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
                    yield Path(entry.path)


def _token_discoveries(
    text: str,
    *,
    call_symbols: set[str],
    literal_values: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    calls: dict[str, int] = defaultdict(int)
    literals: dict[str, int] = defaultdict(int)
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    significant = [
        token
        for token in tokens
        if token.type
        not in {
            tokenize.ENCODING,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENDMARKER,
        }
    ]
    for index, token in enumerate(significant):
        if token.type == tokenize.NAME and token.string in call_symbols:
            previous = significant[index - 1].string if index else ""
            following = significant[index + 1].string if index + 1 < len(significant) else ""
            if previous != "def" and following == "(":
                calls[token.string] += 1
        elif token.type == tokenize.STRING and literal_values:
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                continue
            if isinstance(value, str) and value in literal_values:
                literals[value] += 1
    return dict(calls), dict(literals)


def _discover_rule_matches(
    rule: Mapping[str, Any],
    *,
    text: str,
    calls: Mapping[str, int],
    literals: Mapping[str, int],
) -> dict[str, int]:
    kind = str(rule.get("kind") or "")
    out: dict[str, int] = defaultdict(int)
    if kind == "text":
        for pattern in rule.get("patterns") or []:
            value = str(pattern or "")
            if value:
                out[value] += text.count(value)
        return {key: count for key, count in out.items() if count}
    if kind == "call":
        wanted = {str(item) for item in rule.get("symbols") or []}
        return {name: int(calls[name]) for name in wanted if calls.get(name)}
    if kind == "literal":
        wanted = {str(item) for item in rule.get("values") or []}
        return {value: int(literals[value]) for value in wanted if literals.get(value)}
    raise AgentToolError(code="SOURCE_INVENTORY_ERROR", message=f"unsupported discovery rule kind: {kind}")


def _classifier_index(relpath: str, classifiers: Sequence[Any]) -> int | None:
    for index, raw in enumerate(classifiers):
        if isinstance(raw, dict) and fnmatch.fnmatch(relpath, str(raw.get("path_glob") or "")):
            return index
    return None


def _collect_sqlite_metadata(path: Path) -> dict[str, Any]:
    source_files = _sqlite_source_files(path)
    if not path.exists():
        return {
            "status": "missing",
            "source_files": source_files,
            "snapshot_attempts": 0,
            "tables": [],
            "unknown_tables": [],
            "query_mode": "stable_copy_mode_ro_query_only",
        }
    if path.is_symlink() or not path.is_file():
        return {
            "status": "data_unavailable",
            "reason": "ledger_not_regular_file",
            "source_files": source_files,
            "snapshot_attempts": 0,
            "tables": [],
            "unknown_tables": [],
            "query_mode": "stable_copy_mode_ro_query_only",
        }
    try:
        with tempfile.TemporaryDirectory(prefix="om-storage-baseline-sqlite-") as temp_name:
            copied_path, attempts, stable_state = _copy_stable_sqlite_set(path, Path(temp_name))
            query = _query_sqlite_copy(copied_path)
        return {
            "status": "complete",
            "source_files": _source_file_rows(stable_state),
            "snapshot_attempts": attempts,
            "query_mode": "stable_copy_mode_ro_query_only",
            **query,
        }
    except Exception as exc:
        return {
            "status": "data_unavailable",
            "reason": "stable_copy_or_query_failed",
            "error_type": type(exc).__name__,
            "source_files": _sqlite_source_files(path),
            "snapshot_attempts": SQLITE_SNAPSHOT_ATTEMPTS,
            "tables": [],
            "unknown_tables": [],
            "query_mode": "stable_copy_mode_ro_query_only",
        }


def _sqlite_paths(path: Path) -> tuple[Path, Path, Path]:
    return path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def _stat_tuple(path: Path) -> tuple[bool, int | None, int | None, int | None]:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False, None, None, None
    return True, int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ino)


def _sqlite_state(path: Path) -> dict[str, tuple[bool, int | None, int | None, int | None]]:
    return {item.name: _stat_tuple(item) for item in _sqlite_paths(path)}


def _source_file_rows(
    state: Mapping[str, tuple[bool, int | None, int | None, int | None]],
) -> list[dict[str, Any]]:
    rows = []
    for name, values in state.items():
        exists, size, mtime_ns, inode = values
        role = "db"
        if name.endswith("-wal"):
            role = "wal"
        elif name.endswith("-shm"):
            role = "shm"
        rows.append(
            {
                "role": role,
                "exists": exists,
                "size_bytes": size,
                "mtime_ns": mtime_ns,
                "inode": inode,
            }
        )
    return rows


def _sqlite_source_files(path: Path) -> list[dict[str, Any]]:
    return _source_file_rows(_sqlite_state(path))


def _copy_stable_sqlite_set(
    source: Path,
    destination_dir: Path,
) -> tuple[Path, int, dict[str, tuple[bool, int | None, int | None, int | None]]]:
    destination = destination_dir / source.name
    for attempt in range(1, SQLITE_SNAPSHOT_ATTEMPTS + 1):
        before = _sqlite_state(source)
        for item in destination_dir.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        for source_path in _sqlite_paths(source):
            if before[source_path.name][0]:
                shutil.copyfile(source_path, destination_dir / source_path.name)
        after = _sqlite_state(source)
        if before == after:
            return destination, attempt, after
    raise RuntimeError("sqlite source changed during all snapshot attempts")


def _query_sqlite_copy(path: Path) -> dict[str, Any]:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        table_names = [str(row["name"]) for row in table_rows]
        tables: list[dict[str, Any]] = []
        for table, json_columns in _SQLITE_JSON_COLUMNS.items():
            if table not in table_names:
                tables.append(
                    {
                        "table": table,
                        "status": "missing",
                        "row_count": None,
                        "json_bytes": None,
                    }
                )
                continue
            column_rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            present_columns = {str(row["name"]) for row in column_rows}
            selected_columns = [column for column in json_columns if column in present_columns]
            byte_expression = " + ".join(
                f'COALESCE(length("{column}"), 0)' for column in selected_columns
            )
            if selected_columns:
                row = conn.execute(
                    f'SELECT COUNT(*) AS row_count, COALESCE(SUM({byte_expression}), 0) AS json_bytes FROM "{table}"'
                ).fetchone()
                json_bytes: int | None = int(row["json_bytes"] or 0)
            else:
                row = conn.execute(f'SELECT COUNT(*) AS row_count FROM "{table}"').fetchone()
                json_bytes = None
            tables.append(
                {
                    "table": table,
                    "status": "complete" if selected_columns else "json_column_missing",
                    "row_count": int(row["row_count"] or 0),
                    "json_columns": selected_columns,
                    "json_bytes": json_bytes,
                }
            )
        allowed = set(_SQLITE_JSON_COLUMNS)
        unknown = [name for name in table_names if name not in allowed and not name.startswith("sqlite_")]
        return {
            "page": {
                "page_size_bytes": int(conn.execute("PRAGMA page_size").fetchone()[0]),
                "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
                "freelist_count": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
            },
            "tables": tables,
            "unknown_tables": unknown,
        }
    finally:
        conn.close()


def _collect_runtime_storage(*, root: Path, observed_at: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    aggregates = _RuntimeAggregates()
    research_files: list[dict[str, Any]] = []
    symlinks: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    for name in RUNTIME_SUBROOTS:
        path = root / name
        if path.is_symlink():
            symlinks.append({"path": name, "kind": "root"})
            roots.append({"root": name, "status": "symlink_not_followed", "file_count": 0, "size_bytes": 0})
            continue
        if not path.exists():
            roots.append({"root": name, "status": "missing", "file_count": 0, "size_bytes": 0})
            continue
        if not path.is_dir():
            roots.append({"root": name, "status": "not_directory", "file_count": 0, "size_bytes": 0})
            continue
        before_count = aggregates.file_count
        before_bytes = aggregates.size_bytes
        _scan_runtime_directory(
            path,
            root=root,
            observed_at=observed_at,
            aggregates=aggregates,
            research_files=research_files,
            symlinks=symlinks,
        )
        roots.append(
            {
                "root": name,
                "status": "complete",
                "file_count": aggregates.file_count - before_count,
                "size_bytes": aggregates.size_bytes - before_bytes,
            }
        )
    largest = [item for _size, _path, item in sorted(aggregates.largest, key=lambda row: (-row[0], row[1]))]
    payload = {
        "status": "complete",
        "file_count": aggregates.file_count,
        "size_bytes": aggregates.size_bytes,
        "roots": roots,
        "by_class": _aggregate_groups(aggregates.by_class, "storage_class"),
        "by_suffix": _aggregate_groups(aggregates.by_suffix, "suffix"),
        "by_month": _aggregate_groups(aggregates.by_month, "month"),
        "by_tier": _aggregate_groups(aggregates.by_tier, "tier"),
        "largest_files": [
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "storage_class": item["storage_class"],
                "tier": item["tier"],
            }
            for item in largest
        ],
        "symlinks_not_followed": sorted(symlinks, key=lambda item: item["path"]),
    }
    return payload, research_files


class _RuntimeAggregates:
    def __init__(self) -> None:
        self.file_count = 0
        self.size_bytes = 0
        self.by_class: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.by_suffix: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.by_month: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.by_tier: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.largest: list[tuple[int, str, dict[str, Any]]] = []

    def add(self, item: dict[str, Any]) -> None:
        size = int(item["size_bytes"])
        self.file_count += 1
        self.size_bytes += size
        for values, key in (
            (self.by_class, "storage_class"),
            (self.by_suffix, "suffix"),
            (self.by_month, "month"),
            (self.by_tier, "tier"),
        ):
            bucket = values[str(item[key])]
            bucket[0] += 1
            bucket[1] += size
        summary = {
            "path": item["path"],
            "size_bytes": size,
            "storage_class": item["storage_class"],
            "tier": item["tier"],
        }
        heapq.heappush(self.largest, (size, str(item["path"]), summary))
        if len(self.largest) > LARGEST_FILE_LIMIT:
            heapq.heappop(self.largest)


def _aggregate_groups(values: Mapping[str, Sequence[int]], key: str) -> list[dict[str, Any]]:
    return [
        {key: value, "file_count": int(counts[0]), "size_bytes": int(counts[1])}
        for value, counts in sorted(values.items())
    ]


def _scan_runtime_directory(
    directory: Path,
    *,
    root: Path,
    observed_at: datetime,
    aggregates: _RuntimeAggregates,
    research_files: list[dict[str, Any]],
    symlinks: list[dict[str, Any]],
) -> None:
    stack = [directory]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in sorted(entries, key=lambda item: item.name, reverse=True):
                path = Path(entry.path)
                relpath = path.relative_to(root).as_posix()
                if entry.is_symlink():
                    symlinks.append({"path": relpath, "kind": "entry"})
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                storage_class = _storage_class(relpath)
                age_days = max(0.0, (observed_at.timestamp() - stat.st_mtime) / 86400.0)
                tier = _storage_tier(storage_class=storage_class, age_days=age_days)
                item = {
                        "path": relpath,
                        "size_bytes": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns),
                        "age_days": round(age_days, 3),
                        "suffix": path.suffix.lower() or "[none]",
                        "month": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m"),
                        "storage_class": storage_class,
                        "tier": tier,
                    }
                aggregates.add(item)
                if storage_class in {"research_artifact", "immutable_shared_partition", "sealed_run_artifact"}:
                    research_files.append(item)


def _storage_class(relpath: str) -> str:
    parts = Path(relpath).parts
    if not parts:
        return "other"
    if parts[0] == "output_runs":
        return "sealed_run_artifact"
    if parts[0] in {"output_accounts", "output"}:
        return "compatibility_runtime_output"
    if parts[0] == "logs":
        return "runtime_log"
    if parts[:2] == ("output_shared", "state"):
        return "operational_state"
    if parts[:2] == ("output_shared", "required_data"):
        return "immutable_shared_partition"
    if parts[:2] == ("output_shared", "research"):
        return "research_artifact"
    return "shared_runtime_artifact" if parts[0] == "output_shared" else "other"


def _storage_tier(*, storage_class: str, age_days: float) -> str:
    if storage_class != "research_artifact":
        return "hot"
    return "hot" if age_days <= RESEARCH_HOT_MAX_AGE_DAYS else "warm"


def _collect_research_storage(
    *,
    root: Path,
    observed_at: datetime,
    file_rows: list[dict[str, Any]],
    history_reports: Sequence[str | Path],
) -> dict[str, Any]:
    manifests = [row for row in file_rows if _is_manifest_relpath(str(row["path"]))]
    manifest_results: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    generations: set[str] = set()
    parse_failures: list[dict[str, str]] = []
    for row in manifests:
        relpath = str(row["path"])
        path = root / relpath
        if int(row["size_bytes"]) > MANIFEST_MAX_BYTES:
            parse_failures.append({"path": relpath, "reason": "manifest_too_large"})
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parse_failures.append({"path": relpath, "reason": type(exc).__name__})
            continue
        if not isinstance(payload, dict):
            parse_failures.append({"path": relpath, "reason": "manifest_not_object"})
            continue
        generation = _first_text(payload, ("generation_id", "dataset_id", "run_id"))
        if generation:
            generations.add(generation)
        extracted = _extract_manifest_references(
            payload,
            manifest_path=path,
            root=root,
        )
        references.extend(extracted)
        manifest_results.append(
            {
                "path": relpath,
                "schema_version": str(payload.get("schema_version") or payload.get("schema") or "unknown"),
                "reference_count": len(extracted),
                "historical_verifier_receipt_present": _has_historical_verifier_receipt(payload),
            }
        )

    file_index = {str(row["path"]): row for row in file_rows}
    referenced_paths: set[str] = set()
    protected_failures: list[dict[str, Any]] = []
    hash_sizes: dict[str, int] = {}
    hash_physical: dict[str, dict[str, Any]] = {}
    logical_bytes = 0
    declared_reference_count = 0
    bytes_by_class: dict[str, int] = defaultdict(int)
    bytes_by_tier: dict[str, int] = defaultdict(int)
    bytes_by_market: dict[str, int] = defaultdict(int)
    for reference in references:
        size = reference.get("declared_size_bytes")
        digest = reference.get("declared_sha256")
        relpath = reference.get("resolved_relpath")
        storage_class = str(reference.get("artifact_class") or "unclassified_manifest_reference")
        market = str(reference.get("market") or "unknown").lower()
        if isinstance(size, int) and size >= 0:
            logical_bytes += size
            bytes_by_class[storage_class] += size
            bytes_by_market[market] += size
        if digest:
            declared_reference_count += 1
            if isinstance(size, int) and digest in hash_sizes and hash_sizes[digest] != size:
                protected_failures.append(
                    {"manifest": reference["manifest"], "reference": reference["display_path"], "reason": "declared_hash_size_conflict"}
                )
            elif isinstance(size, int):
                hash_sizes[digest] = size
        if relpath:
            referenced_paths.add(str(relpath))
            actual = file_index.get(str(relpath))
            if actual is None:
                protected_failures.append(
                    {"manifest": reference["manifest"], "reference": reference["display_path"], "reason": "missing"}
                )
                reference["presence_status"] = "missing"
                continue
            actual_size = int(actual["size_bytes"])
            reference["actual_size_bytes"] = actual_size
            if isinstance(size, int) and size != actual_size:
                protected_failures.append(
                    {"manifest": reference["manifest"], "reference": reference["display_path"], "reason": "declared_size_mismatch"}
                )
                reference["presence_status"] = "declared_size_mismatch"
            else:
                reference["presence_status"] = "present_size_match" if isinstance(size, int) else "present_size_undeclared"
            if digest and digest not in hash_physical:
                hash_physical[digest] = actual
        elif reference.get("path_value"):
            protected_failures.append(
                {"manifest": reference["manifest"], "reference": reference["display_path"], "reason": "unresolved_path"}
            )

    unique_declared_bytes = sum(hash_sizes.values())
    unknown_hash_size_count = len({str(ref["declared_sha256"]) for ref in references if ref.get("declared_sha256") and not isinstance(ref.get("declared_size_bytes"), int)})
    manifest_paths = {str(row["path"]) for row in manifests}
    research_files = [
        row
        for row in file_rows
        if str(row["storage_class"]) in {"research_artifact", "immutable_shared_partition", "sealed_run_artifact"}
    ]
    unmanifested = [row for row in research_files if str(row["path"]) not in referenced_paths | manifest_paths]
    physical_bytes = sum(int(row["size_bytes"]) for row in research_files)
    unknown_unique_bytes = sum(int(row["size_bytes"]) for row in unmanifested)
    cold_candidates: list[dict[str, Any]] = []
    for digest, row in hash_physical.items():
        if (
            float(row["age_days"]) >= COLD_CANDIDATE_MIN_AGE_DAYS
            and int(row["size_bytes"]) >= COLD_CANDIDATE_MIN_BYTES
        ):
            cold_candidates.append(
                {
                    "path": row["path"],
                    "declared_sha256": digest,
                    "size_bytes": row["size_bytes"],
                    "age_days": row["age_days"],
                    "action": "preview_only",
                }
            )
            bytes_by_tier["cold_candidate"] += int(row["size_bytes"])
        elif float(row["age_days"]) <= RESEARCH_HOT_MAX_AGE_DAYS:
            bytes_by_tier["hot"] += int(row["size_bytes"])
        else:
            bytes_by_tier["warm"] += int(row["size_bytes"])

    growth = _growth_and_forecast(
        root=root,
        observed_at=observed_at,
        current_unique_bytes=unique_declared_bytes,
        history_reports=history_reports,
    )
    if parse_failures or protected_failures:
        status = "data_unavailable"
    else:
        status = "complete"
    dedup_ratio = (
        round(logical_bytes / unique_declared_bytes, 6)
        if unique_declared_bytes > 0
        else None
    )
    return {
        "status": status,
        "content_verification": "not_performed",
        "manifest_count": len(manifests),
        "parsed_manifest_count": len(manifest_results),
        "manifest_parse_failures": parse_failures,
        "manifests": manifest_results,
        "root_count": sum(1 for rel in ("output_runs", "output_shared/research", "output_shared/required_data") if (root / rel).is_dir()),
        "generation_count": len(generations),
        "declared_reference_count": len(references),
        "declared_hash_reference_count": declared_reference_count,
        "logical_referenced_bytes": logical_bytes,
        "unique_declared_bytes": unique_declared_bytes,
        "declared_hash_unknown_size_count": unknown_hash_size_count,
        "dedup_ratio": dedup_ratio,
        "physical_bytes": physical_bytes,
        "unmanifested_file_count": len(unmanifested),
        "unknown_unique_bytes": unknown_unique_bytes,
        "protected_reference_failures": protected_failures,
        "same_size_content_status": "not_verified",
        "bytes_by_class": _byte_groups(bytes_by_class, "storage_class"),
        "bytes_by_market": _byte_groups(bytes_by_market, "market"),
        "bytes_by_tier": _byte_groups(bytes_by_tier, "tier"),
        "tier_policy": {
            "status": "reporting_heuristic_only",
            "hot_max_age_days": RESEARCH_HOT_MAX_AGE_DAYS,
            "cold_candidate_min_age_days": COLD_CANDIDATE_MIN_AGE_DAYS,
            "cold_candidate_min_bytes": COLD_CANDIDATE_MIN_BYTES,
            "backend": "not_implemented",
            "automatic_actions": [],
        },
        "cold_candidates": sorted(cold_candidates, key=lambda item: (-int(item["size_bytes"]), str(item["path"]))),
        "growth": growth,
    }


def _is_manifest_relpath(relpath: str) -> bool:
    path = Path(relpath)
    if path.suffix.lower() != ".json":
        return False
    name = path.name.lower()
    return "manifest" in name or name.startswith("inventory")


def _extract_manifest_references(payload: Mapping[str, Any], *, manifest_path: Path, root: Path) -> list[dict[str, Any]]:
    if payload.get("schema_version") == "research_archive.v2" and payload.get("action") == "verify":
        return _extract_research_archive_references(
            payload,
            manifest_path=manifest_path,
            root=root,
        )

    out: list[dict[str, Any]] = []

    manifest_reference_roots = _manifest_reference_roots(
        payload,
        manifest_path=manifest_path,
        root=root,
    )

    def visit(
        value: Any,
        context: Mapping[str, Any] | None = None,
        path_hint: str | None = None,
    ) -> None:
        if isinstance(value, dict):
            reference = _reference_from_mapping(
                value,
                manifest_path=manifest_path,
                root=root,
                context=context,
                reference_roots=manifest_reference_roots,
                path_hint=path_hint,
            )
            if reference is not None:
                out.append(reference)
            for child_key, child in value.items():
                child_path_hint = str(child_key) if _mapping_looks_like_file_index(value) else None
                visit(child, context=value, path_hint=child_path_hint)
        elif isinstance(value, list):
            for child in value:
                visit(child, context=context, path_hint=None)

    visit(dict(payload))
    out.extend(
        _extract_parallel_file_map_references(
            payload,
            manifest_path=manifest_path,
            root=root,
        )
    )
    unique: dict[tuple[str, str | None, int | None], dict[str, Any]] = {}
    for item in out:
        key = (
            str(item.get("path_value") or ""),
            str(item.get("declared_sha256")) if item.get("declared_sha256") else None,
            item.get("declared_size_bytes") if isinstance(item.get("declared_size_bytes"), int) else None,
        )
        unique.setdefault(key, item)
    return list(unique.values())


def _extract_research_archive_references(
    payload: Mapping[str, Any],
    *,
    manifest_path: Path,
    root: Path,
) -> list[dict[str, Any]]:
    declared_root = Path(str(payload.get("archive_root") or "")).expanduser()
    archive_root = declared_root.resolve() if declared_root.is_absolute() else manifest_path.parent.parent.resolve()
    if not _is_relative_to(archive_root, root):
        archive_root = manifest_path.parent.parent.resolve()
    references: list[dict[str, Any]] = []
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return references
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        run_id = str(run.get("run_id") or "").strip()
        file_manifest = run.get("file_manifest")
        if not run_id or not isinstance(file_manifest, list):
            continue
        reference_root = archive_root / "output_runs" / run_id
        for item in file_manifest:
            if not isinstance(item, Mapping):
                continue
            relpath = str(item.get("path") or "").strip()
            if not relpath:
                continue
            references.append(
                _explicit_manifest_reference(
                    manifest_path=manifest_path,
                    root=root,
                    reference_root=reference_root,
                    path_value=relpath,
                    digest=item.get("sha256"),
                    size=item.get("size_bytes"),
                    artifact_class="immutable_replay_authority",
                    market=_first_text(run, ("market",)),
                )
            )
    return references


def _extract_parallel_file_map_references(
    payload: Mapping[str, Any],
    *,
    manifest_path: Path,
    root: Path,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            files = value.get("files")
            hashes = value.get("file_sha256")
            sizes = value.get("file_size_bytes")
            if isinstance(files, Mapping) and isinstance(hashes, Mapping):
                for name, raw_path in files.items():
                    if not isinstance(raw_path, str) or not raw_path.strip():
                        continue
                    references.append(
                        _explicit_manifest_reference(
                            manifest_path=manifest_path,
                            root=root,
                            reference_root=manifest_path.parent,
                            path_value=raw_path,
                            digest=hashes.get(name),
                            size=sizes.get(name) if isinstance(sizes, Mapping) else None,
                            artifact_class="experiment_or_research_artifact",
                            market=_first_text(value, ("market",)),
                        )
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return references


def _explicit_manifest_reference(
    *,
    manifest_path: Path,
    root: Path,
    reference_root: Path,
    path_value: str,
    digest: Any,
    size: Any,
    artifact_class: str,
    market: str | None,
) -> dict[str, Any]:
    digest_value = str(digest or "").strip().lower() or None
    if digest_value is not None and (
        len(digest_value) != 64
        or any(char not in "0123456789abcdef" for char in digest_value)
    ):
        digest_value = None
    size_value = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    return {
        "manifest": manifest_path.relative_to(root).as_posix(),
        "display_path": path_value,
        "path_key": "adapter_path",
        "path_value": path_value,
        "resolved_relpath": _resolve_manifest_reference(
            path_value,
            manifest_path=manifest_path,
            root=root,
            reference_roots=(reference_root,),
        ),
        "declared_sha256": digest_value,
        "declared_size_bytes": size_value,
        "hash_key": "adapter_sha256",
        "size_key": "adapter_size_bytes" if size_value is not None else None,
        "artifact_class": artifact_class,
        "market": market,
    }


def _reference_from_mapping(
    value: Mapping[str, Any],
    *,
    manifest_path: Path,
    root: Path,
    context: Mapping[str, Any] | None,
    reference_roots: Sequence[Path],
    path_hint: str | None,
) -> dict[str, Any] | None:
    path_key, path_value = _first_key_value(value, _REFERENCE_PATH_KEYS)
    if path_value is None:
        for key, raw in value.items():
            if (
                str(key).endswith("_relpath")
                and not str(key).endswith("_root_relpath")
                and isinstance(raw, str)
                and raw.strip()
            ):
                path_key, path_value = str(key), raw
                break
    if path_value is None and path_hint:
        path_key, path_value = "mapping_key", path_hint
    hash_key, digest_value = _first_key_value(value, _REFERENCE_HASH_KEYS)
    if digest_value is None and path_key and path_key.endswith("_relpath"):
        hash_key = path_key.removesuffix("_relpath") + "_sha256"
        digest_value = value.get(hash_key)
    size_key, size_value = _first_key_value(value, _REFERENCE_SIZE_KEYS)
    if path_value is None and digest_value is None:
        return None
    if (
        path_value is not None
        and not str(path_key or "").endswith("relpath")
        and digest_value is None
        and size_value is None
    ):
        return None
    if path_value is not None and not isinstance(path_value, str):
        return None
    digest = str(digest_value or "").strip().lower() or None
    if digest is not None and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        digest = None
    size: int | None = None
    if isinstance(size_value, int) and not isinstance(size_value, bool) and size_value >= 0:
        size = size_value
    resolved_relpath = _resolve_manifest_reference(
        str(path_value or ""),
        manifest_path=manifest_path,
        root=root,
        reference_roots=reference_roots,
    )
    context_map = context or value
    artifact_class = _first_text(value, ("artifact_class", "storage_class", "class")) or _first_text(
        context_map, ("artifact_class", "storage_class", "class")
    )
    market = _first_text(value, ("market",)) or _first_text(context_map, ("market",))
    return {
        "manifest": manifest_path.relative_to(root).as_posix(),
        "display_path": str(path_value or "[hash-only]"),
        "path_key": path_key,
        "path_value": str(path_value or ""),
        "resolved_relpath": resolved_relpath,
        "declared_sha256": digest,
        "declared_size_bytes": size,
        "hash_key": hash_key,
        "size_key": size_key,
        "artifact_class": artifact_class or _artifact_class_for_reference(resolved_relpath),
        "market": market,
    }


def _mapping_looks_like_file_index(value: Mapping[str, Any]) -> bool:
    if not value:
        return False
    for child in value.values():
        if not isinstance(child, Mapping):
            continue
        if any(key in child for key in (*_REFERENCE_HASH_KEYS, *_REFERENCE_SIZE_KEYS)):
            return True
    return False


def _manifest_reference_roots(
    payload: Mapping[str, Any],
    *,
    manifest_path: Path,
    root: Path,
) -> list[Path]:
    roots: list[Path] = []
    for key, raw in payload.items():
        if not str(key).endswith("_root_relpath") or not isinstance(raw, str) or not raw.strip():
            continue
        candidate = (manifest_path.parent / raw).resolve()
        if _is_relative_to(candidate, root):
            roots.append(candidate)
    integrity = payload.get("integrity")
    if isinstance(integrity, dict) and isinstance(integrity.get("files"), dict):
        roots.append(manifest_path.parent.resolve())
    return list(dict.fromkeys(roots))


def _resolve_manifest_reference(
    value: str,
    *,
    manifest_path: Path,
    root: Path,
    reference_roots: Sequence[Path],
) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        candidate = path.resolve()
        return candidate.relative_to(root).as_posix() if _is_relative_to(candidate, root) else None
    candidates: list[Path] = []
    if path.parts and path.parts[0] in RUNTIME_SUBROOTS:
        candidates.append((root / path).resolve())
    candidates.extend((reference_root / path).resolve() for reference_root in reference_roots)
    candidates.extend(((manifest_path.parent / path).resolve(), (root / path).resolve()))
    valid = [candidate for candidate in candidates if _is_relative_to(candidate, root)]
    for candidate in valid:
        if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
            return candidate.relative_to(root).as_posix()
    return valid[0].relative_to(root).as_posix() if valid else None


def _artifact_class_for_reference(relpath: str | None) -> str:
    if not relpath:
        return "unclassified_manifest_reference"
    storage_class = _storage_class(relpath)
    if storage_class == "sealed_run_artifact":
        return "immutable_replay_authority"
    if storage_class == "immutable_shared_partition":
        return storage_class
    if storage_class == "research_artifact":
        return "experiment_or_research_artifact"
    return "unclassified_manifest_reference"


def _first_key_value(value: Mapping[str, Any], keys: Sequence[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in value and value.get(key) is not None:
            return key, value.get(key)
    return None, None


def _first_text(value: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _has_historical_verifier_receipt(payload: Mapping[str, Any]) -> bool:
    for key in ("verifier_receipt", "verification_receipt", "verified_at_utc", "verification"):
        if payload.get(key):
            return True
    return False


def _byte_groups(values: Mapping[str, int], key: str) -> list[dict[str, Any]]:
    return [{key: name, "bytes": int(size)} for name, size in sorted(values.items())]


def _growth_and_forecast(
    *,
    root: Path,
    observed_at: datetime,
    current_unique_bytes: int,
    history_reports: Sequence[str | Path],
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    previous_time: datetime | None = None
    for raw_path in history_reports:
        path = Path(raw_path).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            rejected.append({"report": path.name, "reason": type(exc).__name__})
            continue
        if isinstance(payload, dict) and payload.get("tool_name") == "research.storage-baseline":
            payload = payload.get("data")
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            rejected.append({"report": path.name, "reason": "schema_mismatch"})
            continue
        identity = payload.get("identity")
        research = payload.get("research_storage")
        if not isinstance(identity, dict) or not isinstance(research, dict):
            rejected.append({"report": path.name, "reason": "shape_invalid"})
            continue
        try:
            report_root = Path(str(identity.get("runtime_root") or "")).resolve()
            timestamp = datetime.fromisoformat(str(identity.get("observed_at_utc") or "").replace("Z", "+00:00"))
            timestamp = timestamp.astimezone(timezone.utc)
            unique_bytes = int(research["unique_declared_bytes"])
        except (KeyError, TypeError, ValueError, OSError):
            rejected.append({"report": path.name, "reason": "identity_or_measure_invalid"})
            continue
        if report_root != root:
            rejected.append({"report": path.name, "reason": "runtime_root_mismatch"})
            continue
        if timestamp >= observed_at or (previous_time is not None and timestamp <= previous_time):
            rejected.append({"report": path.name, "reason": "out_of_order"})
            continue
        observations.append(
            {
                "observed_at_utc": timestamp.isoformat(),
                "unique_declared_bytes": unique_bytes,
                "source": path.name,
            }
        )
        previous_time = timestamp
    observations.append(
        {
            "observed_at_utc": observed_at.isoformat(),
            "unique_declared_bytes": int(current_unique_bytes),
            "source": "current",
        }
    )
    intervals: list[dict[str, Any]] = []
    for previous, current in zip(observations, observations[1:]):
        previous_time_value = datetime.fromisoformat(str(previous["observed_at_utc"]))
        current_time_value = datetime.fromisoformat(str(current["observed_at_utc"]))
        days = (current_time_value - previous_time_value).total_seconds() / 86400.0
        if days <= 0:
            continue
        delta = int(current["unique_declared_bytes"]) - int(previous["unique_declared_bytes"])
        intervals.append(
            {
                "from_utc": previous["observed_at_utc"],
                "to_utc": current["observed_at_utc"],
                "days": round(days, 6),
                "new_unique_bytes": delta,
                "monthly_rate_bytes": int(round(delta * 30.0 / days)),
            }
        )
    if len(observations) < 2 or not intervals:
        return {
            "status": "insufficient_history",
            "observations": observations,
            "rejected_reports": rejected,
            "intervals": intervals,
            "monthly_unique_growth_bytes": None,
            "forecast_90d_additional_bytes": None,
            "rapid_growth_two_consecutive_months": False,
        }
    recent_rates = [int(item["monthly_rate_bytes"]) for item in intervals[-3:]]
    monthly_growth = int(round(statistics.median(recent_rates)))
    additional = max(0, monthly_growth * 3)
    rapid = _has_two_consecutive_growth_spikes(intervals)
    return {
        "status": "complete",
        "observations": observations,
        "rejected_reports": rejected,
        "intervals": intervals,
        "monthly_unique_growth_bytes": monthly_growth,
        "forecast_90d_additional_bytes": additional,
        "rapid_growth_two_consecutive_months": rapid,
    }


def _has_two_consecutive_growth_spikes(intervals: Sequence[Mapping[str, Any]]) -> bool:
    rates = [int(item.get("monthly_rate_bytes") or 0) for item in intervals]
    if len(rates) < 5:
        return False
    flags: list[bool] = []
    for index in range(3, len(rates)):
        median = statistics.median(rates[index - 3 : index])
        flags.append(median > 0 and rates[index] > 2 * median)
    return any(first and second for first, second in zip(flags, flags[1:]))


def _capacity_thresholds(
    *,
    disk_total_bytes: int,
    disk_free_bytes: int,
    research_storage: Mapping[str, Any],
) -> dict[str, Any]:
    growth = research_storage.get("growth") if isinstance(research_storage.get("growth"), dict) else {}
    forecast_additional = growth.get("forecast_90d_additional_bytes")
    forecast_free = (
        max(0, disk_free_bytes - int(forecast_additional))
        if isinstance(forecast_additional, int)
        else None
    )
    warning_floor = max(math.ceil(disk_total_bytes * 0.10), 20 * GIB)
    critical_floor = max(math.ceil(disk_total_bytes * 0.05), 10 * GIB)
    protected_failures = list(research_storage.get("protected_reference_failures") or [])
    critical_reasons: list[str] = []
    warning_reasons: list[str] = []
    if disk_free_bytes < critical_floor:
        critical_reasons.append("current_free_space_below_critical_floor")
    if protected_failures:
        critical_reasons.append("protected_object_missing_or_unresolved")
    if research_storage.get("manifest_parse_failures"):
        critical_reasons.append("generation_manifest_unresolved")
    if forecast_free is not None and forecast_free < warning_floor:
        warning_reasons.append("forecast_free_space_below_warning_floor")
    if growth.get("rapid_growth_two_consecutive_months") is True:
        warning_reasons.append("monthly_unique_growth_above_two_times_trailing_median")
    if critical_reasons:
        status = "critical"
    elif warning_reasons:
        status = "warning"
    elif forecast_free is None:
        status = "insufficient_history"
    else:
        status = "ok"
    return {
        "status": status,
        "filesystem_capacity_bytes": disk_total_bytes,
        "current_free_bytes": disk_free_bytes,
        "warning_floor_bytes": warning_floor,
        "critical_floor_bytes": critical_floor,
        "forecast_90d_free_bytes": forecast_free,
        "warning_reasons": warning_reasons,
        "critical_reasons": critical_reasons,
        "operator_decision_required": bool(warning_reasons or critical_reasons),
        "preview": {
            "cold_candidate_count": len(research_storage.get("cold_candidates") or []),
            "unmanifested_file_count": int(research_storage.get("unmanifested_file_count") or 0),
            "actions": [],
        },
        "automatic_actions": [],
    }


def _display_runtime_path(path: Path, *, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return f"external:{path.name}"


def _write_report(path: Path, payload: Mapping[str, Any], *, runtime_root: Path, overwrite: bool) -> None:
    if _path_or_parent_is_symlink(path, stop=path.parent):
        raise AgentToolError(code="INPUT_ERROR", message=f"baseline output must not be a symlink: {path}")
    if _is_relative_to(path, runtime_root):
        raise AgentToolError(code="INPUT_ERROR", message="baseline output must be outside the inventoried runtime root")
    if not path.parent.exists() or not path.parent.is_dir():
        raise AgentToolError(code="INPUT_ERROR", message=f"baseline output parent directory not found: {path.parent}")
    if path.exists() and not overwrite:
        raise AgentToolError(code="INPUT_ERROR", message=f"baseline output already exists: {path}")
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise AgentToolError(code="INPUT_ERROR", message=f"baseline output is not a regular file: {path}")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _path_or_parent_is_symlink(path: Path, *, stop: Path | None) -> bool:
    current = path.absolute()
    stop_path = stop.absolute() if stop is not None else None
    while True:
        if current.is_symlink():
            return True
        if stop_path is not None and current == stop_path:
            return False
        parent = current.parent
        if parent == current:
            return False
        current = parent


__all__ = ["SCHEMA_VERSION", "collect_storage_runtime_baseline"]
