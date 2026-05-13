from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.strategy_replay import analyze_strategy_replay, read_strategy_replay_file


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _resolve_path(value: Any, *, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _default_replay_paths(base: Path) -> list[Path]:
    report_dir = (base / "output" / "reports").resolve()
    candidates = [
        report_dir / "strategy_replay.csv",
        report_dir / "strategy_replay.json",
        report_dir / "strategy_replay.jsonl",
    ]
    return [path for path in candidates if path.exists()]


def _source_paths(payload: dict[str, Any], *, repo_base: Callable[[], Path]) -> list[Path]:
    base = repo_base()
    explicit: list[Any] = []
    if payload.get("replay_path"):
        explicit.append(payload.get("replay_path"))
    raw_paths = payload.get("replay_paths")
    if isinstance(raw_paths, list):
        explicit.extend(raw_paths)
    paths = [_resolve_path(value, base=base) for value in explicit if str(value or "").strip()]
    if paths:
        return paths
    return _default_replay_paths(base)


def strategy_replay_analyze_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_files: list[dict[str, Any]] = []
    inline_rows = payload.get("rows")
    if inline_rows is None:
        inline_rows = payload.get("records")
    if isinstance(inline_rows, list):
        rows.extend([dict(item) for item in inline_rows if isinstance(item, dict)])
        source_files.append({"path": "<inline_rows>", "row_count": len(rows)})

    has_explicit_paths = bool(payload.get("replay_path")) or isinstance(payload.get("replay_paths"), list)
    paths = _source_paths(payload, repo_base=repo_base) if (has_explicit_paths or not rows) else []
    for path in paths:
        if not path.exists():
            raise AgentToolError(
                code="DEPENDENCY_MISSING",
                message=f"strategy replay file not found: {path.name}",
                details={"replay_path": str(path)},
            )
        try:
            loaded = read_strategy_replay_file(path)
        except Exception as exc:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"failed to read strategy replay file: {path.name}",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        rows.extend(loaded)
        source_files.append({"path": mask_path(path), "row_count": len(loaded)})

    if not rows:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="strategy replay rows not found",
            hint="Pass rows/replay_path, or write output/reports/strategy_replay.csv.",
        )

    data = analyze_strategy_replay(
        rows,
        min_sample=_as_int(payload.get("min_sample"), default=5, low=1, high=1000),
        win_return_threshold=_as_float(payload.get("win_return_threshold"), default=0.0),
        bad_drawdown_threshold=_as_float(payload.get("bad_drawdown_threshold"), default=-0.15),
    )
    data["source_files"] = source_files
    return data, list(data.get("warnings") or []), {"source_files": source_files}
