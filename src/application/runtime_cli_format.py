from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_runtime_cli_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def selected_run_dir(*, root: Path, base: Path, run_id: str | None, run_dir: str | Path | None) -> Path | None:
    raw_run_dir = str(run_dir or "").strip()
    if raw_run_dir:
        return resolve_runtime_cli_path(raw_run_dir, base=base)
    raw_run_id = str(run_id or "").strip()
    if not raw_run_id:
        return None
    candidate = Path(raw_run_id)
    if candidate.is_absolute() or candidate.name != raw_run_id:
        return None
    return (root / raw_run_id).resolve()


def display_path(path: Path, *, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def csv_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    if value is None:
        return "-"
    return str(value)


def display_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"
