from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None, encoding: str = "utf-8") -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding=encoding))
    except Exception:
        return default


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{uuid.uuid4().hex[:12]}")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(p)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    obj: Any,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_text(path, payload, encoding=encoding)
