"""I/O utilities (Stage 1 infrastructure).

Goal: centralize repeated file read/write helpers so later refactors don't fork logic.
This module is intentionally tiny and side-effect free.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_text(path: str | Path, *, encoding: str = 'utf-8') -> str:
    return Path(path).read_text(encoding=encoding)


def write_text(path: str | Path, content: str, *, encoding: str = 'utf-8') -> None:
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(content, encoding=encoding)


def read_json(path: str | Path, *, default: Any = None, encoding: str = 'utf-8') -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding=encoding))
    except Exception:
        return default


def atomic_write_text(path: str | Path, content: str, *, encoding: str = 'utf-8') -> None:
    """Best-effort atomic write (write to tmp then replace)."""
    p = Path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + f'.tmp.{os.getpid()}')
    tmp.write_text(content, encoding=encoding)
    tmp.replace(p)


def atomic_write_json(path: str | Path, obj: Any, *, encoding: str = 'utf-8', indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent) + '\n', encoding=encoding)
