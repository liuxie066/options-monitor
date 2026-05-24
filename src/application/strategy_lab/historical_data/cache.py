from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from src.application.agent_tool_contracts import AgentToolError
from src.application.strategy_lab.historical_data.contracts import (
    HistoricalDataSnapshot,
    historical_snapshot_summary,
)


class HistoricalDataCache:
    def __init__(self, *, base: Path, cache_dir: str | Path | None = None) -> None:
        self.base = Path(base).resolve()
        self.cache_dir = _resolve_output_path(
            cache_dir,
            base=self.base,
            default=self.base / "output_shared" / "strategy_lab" / "historical_data",
        )

    def write_snapshot(self, snapshot: HistoricalDataSnapshot) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{snapshot.snapshot_id}.json"
        path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def read_snapshot(self, path: str | Path) -> HistoricalDataSnapshot:
        resolved = _resolve_read_path(path, base=self.base)
        return HistoricalDataSnapshot.from_dict(json.loads(resolved.read_text(encoding="utf-8")))

    def relative(self, path: str | Path) -> str:
        return _relative(Path(path), base=self.base)


def load_historical_data_snapshots(
    paths: Iterable[str | Path] | None,
    *,
    base: Path,
) -> tuple[tuple[HistoricalDataSnapshot, ...], tuple[str, ...]]:
    snapshots: list[HistoricalDataSnapshot] = []
    warnings: list[str] = []
    cache = HistoricalDataCache(base=base)
    for value in paths or ():
        raw = str(value or "").strip()
        if not raw:
            continue
        snapshot = cache.read_snapshot(raw)
        snapshots.append(snapshot)
        warnings.extend(snapshot.warnings)
    return tuple(snapshots), tuple(warnings)


def historical_snapshots_summary(snapshots: Iterable[HistoricalDataSnapshot]) -> dict[str, Any]:
    items = [historical_snapshot_summary(snapshot) for snapshot in snapshots]
    return {
        "snapshot_count": len(items),
        "bar_count": sum(int(item.get("bar_count") or 0) for item in items),
        "snapshots": items,
        "warnings": [warning for item in items for warning in item.get("warnings", [])],
    }


def _resolve_output_path(value: str | Path | None, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    path = default.resolve() if not raw else Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="strategy lab historical data cache must stay under the repo root",
            details={"path": _relative(path, base=base)},
        ) from exc
    return path


def _resolve_read_path(value: str | Path, *, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def _relative(path: Path, *, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f".../{path.name}" if path.name else "..."
