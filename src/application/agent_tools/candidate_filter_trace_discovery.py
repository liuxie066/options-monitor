from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from src.application.runtime_paths import resolve_runtime_root


CANDIDATE_FILTER_TRACE_NAME = "candidate_filter_trace.jsonl"
MAX_RECENT_TRACE_RUN_DIRS = 50


@dataclass(frozen=True)
class CandidateFilterTraceDiscovery:
    paths: tuple[Path, ...]
    roots: tuple[Path, ...]
    run_dirs: tuple[Path, ...]
    considered_paths: tuple[Path, ...]
    explicit_paths: bool


def discover_candidate_filter_trace_paths(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
) -> CandidateFilterTraceDiscovery:
    base = repo_base().expanduser().resolve()
    explicit = _explicit_trace_paths(payload, base=base)
    if explicit:
        existing = _existing_unique(explicit)
        return CandidateFilterTraceDiscovery(
            paths=tuple(existing),
            roots=(base,),
            run_dirs=(),
            considered_paths=tuple(explicit),
            explicit_paths=True,
        )

    roots = _trace_roots(payload, base=base)
    account = str(payload.get("account") or "").strip().lower()
    candidates: list[Path] = []
    run_dirs = _trace_run_dirs(payload, roots=roots, base=base)
    for run_dir in run_dirs:
        candidates.extend(_trace_candidates_for_run(run_dir, account=account))

    if payload.get("report_dir"):
        candidates.append(_resolve_path(payload.get("report_dir"), base=base) / CANDIDATE_FILTER_TRACE_NAME)

    for root in roots:
        candidates.append(root / "output_shared" / "reports" / CANDIDATE_FILTER_TRACE_NAME)
        candidates.append(root / "output_shared" / "agent_tools" / "reports" / CANDIDATE_FILTER_TRACE_NAME)

    return CandidateFilterTraceDiscovery(
        paths=tuple(_existing_unique(candidates)),
        roots=tuple(roots),
        run_dirs=tuple(run_dirs),
        considered_paths=tuple(_unique_paths(candidates)),
        explicit_paths=False,
    )


def find_candidate_filter_trace_paths(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
) -> list[Path]:
    return list(discover_candidate_filter_trace_paths(payload, repo_base=repo_base).paths)


def _explicit_trace_paths(payload: dict[str, Any], *, base: Path) -> list[Path]:
    raw_values: list[Any] = []
    if payload.get("trace_path"):
        raw_values.append(payload.get("trace_path"))
    raw_trace_paths = payload.get("trace_paths")
    if isinstance(raw_trace_paths, list):
        raw_values.extend(raw_trace_paths)
    return [_resolve_path(value, base=base) for value in raw_values if str(value or "").strip()]


def _trace_roots(payload: dict[str, Any], *, base: Path) -> list[Path]:
    roots: list[Path] = [base]
    raw_runtime_root = payload.get("runtime_root")
    if str(raw_runtime_root or "").strip():
        roots.append(_resolve_path(raw_runtime_root, base=base))

    raw_config_path = payload.get("config_path")
    if str(raw_config_path or "").strip():
        roots.append(_resolve_path(raw_config_path, base=base).parent)

    try:
        roots.append(
            resolve_runtime_root(
                repo_root=base,
                runtime_root=raw_runtime_root if str(raw_runtime_root or "").strip() else None,
            ).runtime_root
        )
    except Exception:
        pass

    for root in list(_unique_paths(roots)):
        roots.extend(_service_profile_roots(root, base=base))
    return _unique_paths(roots)


def _service_profile_roots(root: Path, *, base: Path) -> list[Path]:
    profile = _read_json_object(root / "service.profile.json")
    roots: list[Path] = []
    raw_runtime_root = profile.get("runtime_root")
    if str(raw_runtime_root or "").strip():
        roots.append(_resolve_path(raw_runtime_root, base=base))

    paths = profile.get("paths")
    if isinstance(paths, dict):
        raw_runs_root = paths.get("runs_root")
        if str(raw_runs_root or "").strip():
            runs_root = _resolve_path(raw_runs_root, base=base)
            roots.append(runs_root.parent if runs_root.name == "output_runs" else runs_root)
    return roots


def _trace_run_dirs(payload: dict[str, Any], *, roots: list[Path], base: Path) -> list[Path]:
    raw_run_dir = payload.get("run_dir")
    if str(raw_run_dir or "").strip():
        return _unique_paths([_resolve_path(raw_run_dir, base=base)])

    run_id = str(payload.get("run_id") or "").strip()
    if run_id:
        return _unique_paths([root / "output_runs" / run_id for root in roots])

    run_dirs: list[Path] = []
    for root in roots:
        run_dirs.extend(_pointer_run_dirs(root))
        run_dirs.extend(_recent_run_dirs(root))
    return _unique_paths(run_dirs)


def _pointer_run_dirs(root: Path) -> list[Path]:
    pointer = root / "output_shared" / "state" / "last_run_dir.txt"
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not raw:
        return []
    candidates = [_resolve_path(raw, base=root)]
    raw_path = Path(raw).expanduser()
    if not raw_path.is_absolute() and "output_runs" not in raw_path.parts:
        candidates.append(root / "output_runs" / raw)
    return [path for path in _unique_paths(candidates) if path.is_dir()]


def _recent_run_dirs(root: Path) -> list[Path]:
    runs_root = root / "output_runs"
    try:
        dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    except OSError:
        return []
    dirs.sort(key=_mtime_ns, reverse=True)
    return dirs[:MAX_RECENT_TRACE_RUN_DIRS]


def _trace_candidates_for_run(run_dir: Path, *, account: str) -> list[Path]:
    if account:
        return [run_dir / "accounts" / account / CANDIDATE_FILTER_TRACE_NAME]
    try:
        return sorted((run_dir / "accounts").glob(f"*/{CANDIDATE_FILTER_TRACE_NAME}"))
    except OSError:
        return []


def _existing_unique(paths: list[Path]) -> list[Path]:
    return [path for path in _unique_paths(paths) if path.exists() and path.is_file()]


def _unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _resolve_path(value: Any, *, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


__all__ = [
    "CANDIDATE_FILTER_TRACE_NAME",
    "CandidateFilterTraceDiscovery",
    "discover_candidate_filter_trace_paths",
    "find_candidate_filter_trace_paths",
]
