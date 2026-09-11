from __future__ import annotations

from pathlib import Path

from .repository import SQLiteOptionPositionsRepository


def open_wheel_activation_repository(
    sqlite_path: str | Path,
) -> SQLiteOptionPositionsRepository:
    """Open a repository writer without running portfolio bootstrap work."""

    return SQLiteOptionPositionsRepository(Path(sqlite_path))
