from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_runs_cli import resolve_runtime_runs_root


SCHEMA_VERSION = "runtime_logs.v1"
RUN_LOG_FILES = {
    "audit": "audit_events.jsonl",
    "tool": "tool_execution_audit.jsonl",
    "tick": "tick_metrics_history.json",
}


def collect_runtime_logs(
    *,
    repo_root: Path,
    runs_root: str | Path | None = None,
    logs_root: str | Path | None = None,
    profile_path: str | Path | None = None,
    run_id: str | None = None,
    run_dir: str | Path | None = None,
    kind: str = "all",
    lines: int = 50,
    log_file: str | Path | None = None,
) -> dict[str, Any]:
    base = repo_root.resolve()
    line_count = max(int(lines), 0)
    root = resolve_runtime_runs_root(base=base, runs_root=runs_root, profile_path=profile_path)
    service_logs_root = _resolve_logs_root(base=base, logs_root=logs_root, profile_path=profile_path)
    selected_run = _selected_run_dir(root=root, base=base, run_id=run_id, run_dir=run_dir)
    requested_run = bool(str(run_id or "").strip() or str(run_dir or "").strip())

    files: list[Path]
    if log_file:
        files = [_resolve_path(log_file, base=base)]
    elif kind == "service":
        files = _service_log_files(service_logs_root)
    else:
        if selected_run is None and not requested_run:
            selected_run = _latest_run_dir(root)
        files = _run_log_files(selected_run, kind=kind) if selected_run is not None else []

    entries = [_log_entry(path, base=base, lines=line_count) for path in files]
    ok = (not requested_run or (selected_run is not None and selected_run.exists())) and not (
        log_file and not entries[0].get("exists")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "runs_root": str(root),
        "runs_root_display": _display_path(root, base=base),
        "logs_root": str(service_logs_root),
        "logs_root_display": _display_path(service_logs_root, base=base),
        "selected_run": _run_payload(selected_run, base=base) if selected_run is not None else None,
        "summary": {
            "ok": ok,
            "kind": kind,
            "lines": line_count,
            "requested_run": requested_run,
            "requested_run_found": None if not requested_run else bool(selected_run is not None and selected_run.exists()),
            "file_count": len(entries),
            "existing_file_count": sum(1 for item in entries if item.get("exists")),
        },
        "files": entries,
    }


def format_runtime_logs(data: dict[str, Any]) -> str:
    summary = _dict(data.get("summary"))
    run = _dict(data.get("selected_run"))
    lines = [
        "options-monitor logs",
        f"kind: {_value(summary.get('kind'))} lines={_value(summary.get('lines'))}",
        f"runs_root: {_value(data.get('runs_root_display') or data.get('runs_root'))}",
    ]
    if run:
        lines.append(f"run: {_value(run.get('run_id'))} path={_value(run.get('path_display') or run.get('path'))}")
    if summary.get("requested_run") and summary.get("requested_run_found") is False:
        lines.append("requested run: not found")
    lines.append("")

    files = _list(data.get("files"))
    if not files:
        lines.append("no log files found")
        return "\n".join(lines).rstrip() + "\n"

    for item in files:
        entry = _dict(item)
        lines.append(
            f"== {_value(entry.get('path_display') or entry.get('path'))} "
            f"exists={_yes_no(entry.get('exists'))} lines={_value(entry.get('tail_line_count'))} =="
        )
        if entry.get("error"):
            lines.append(f"error: {entry.get('error')}")
        for line in _list(entry.get("tail")):
            lines.append(str(line))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _resolve_logs_root(*, base: Path, logs_root: str | Path | None, profile_path: str | Path | None) -> Path:
    if logs_root:
        return _resolve_path(logs_root, base=base)
    profile = _load_profile(profile_path, base=base) if profile_path else {}
    paths = _dict(profile.get("paths"))
    raw = paths.get("logs_root") or profile.get("logs_root")
    if raw:
        return _resolve_path(raw, base=base)
    runtime_root = profile.get("runtime_root")
    if runtime_root:
        return (_resolve_path(runtime_root, base=base) / "logs").resolve()
    return (base / "logs").resolve()


def _load_profile(profile_path: str | Path | None, *, base: Path) -> dict[str, Any]:
    if not profile_path:
        return {}
    path = _resolve_path(profile_path, base=base)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile must be a JSON object: {path}")
    return payload


def _selected_run_dir(*, root: Path, base: Path, run_id: str | None, run_dir: str | Path | None) -> Path | None:
    raw_run_dir = str(run_dir or "").strip()
    if raw_run_dir:
        return _resolve_path(raw_run_dir, base=base)
    raw_run_id = str(run_id or "").strip()
    if not raw_run_id:
        return None
    candidate = Path(raw_run_id)
    if candidate.is_absolute() or candidate.name != raw_run_id:
        return None
    return (root / raw_run_id).resolve()


def _latest_run_dir(root: Path) -> Path | None:
    if not root.exists() or not root.is_dir():
        return None
    dirs = [item for item in root.iterdir() if item.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda item: (item.stat().st_mtime, item.name))


def _run_log_files(run_dir: Path, *, kind: str) -> list[Path]:
    state = run_dir / "state"
    if kind in RUN_LOG_FILES:
        return [state / RUN_LOG_FILES[kind]]
    return [state / name for name in RUN_LOG_FILES.values()]


def _service_log_files(logs_root: Path) -> list[Path]:
    if not logs_root.exists() or not logs_root.is_dir():
        return []
    return sorted(
        [item for item in logs_root.iterdir() if item.is_file() and item.suffix == ".log"],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def _log_entry(path: Path, *, base: Path, lines: int) -> dict[str, Any]:
    exists = path.exists() and path.is_file()
    entry: dict[str, Any] = {
        "path": str(path),
        "path_display": _display_path(path, base=base),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else None,
        "tail_line_count": 0,
        "tail": [],
    }
    if not exists:
        return entry
    try:
        tail = _tail_lines(path, lines=lines)
    except OSError as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry
    entry["tail"] = tail
    entry["tail_line_count"] = len(tail)
    return entry


def _tail_lines(path: Path, *, lines: int) -> list[str]:
    if lines <= 0:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()[-lines:]


def _run_payload(run_dir: Path | None, *, base: Path) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    return {
        "run_id": run_dir.name,
        "path": str(run_dir),
        "path_display": _display_path(run_dir, base=base),
        "exists": run_dir.exists() and run_dir.is_dir(),
    }


def _resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def _display_path(path: Path, *, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"
