"""I/O utilities (Stage 1 infrastructure).

Goal: centralize repeated file read/write helpers so later refactors don't fork logic.

Note:
- Keep these helpers small and dependency-light.
- These are shared utilities; avoid importing run_pipeline from here.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError


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


def read_json(path: str | Path, default: Any = None, encoding: str = 'utf-8') -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding=encoding))
    except Exception:
        return default




def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bj_now() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def parse_last_json(text: str) -> dict[str, Any]:
    """Parse the last JSON object printed to text (tolerant of logs above it)."""
    lines = (text or '').splitlines()
    buf = []
    for ln in reversed(lines):
        if not ln.strip():
            continue
        buf.append(ln)
        if ln.strip().startswith('{'):
            break
    txt = '\n'.join(reversed(buf)).strip()
    if not txt:
        return {}
    try:
        return json.loads(txt)
    except Exception:
        return {}


def parse_last_json_obj(text: str) -> dict:
    """Parse the last JSON object fragment from free-form text (starts at first { ends at last })."""
    s = (text or '').strip()
    if not s:
        return {}
    i = s.find('{')
    j = s.rfind('}')
    if i < 0 or j < 0 or j <= i:
        return {}
    try:
        return json.loads(s[i:j+1])
    except Exception:
        return {}


def money_cny(v, *, decimals: int = 0, show_ccy: bool = True) -> str:
    """Format CNY amount.

    Defaults keep backward compatibility: no decimals + show "(CNY)".
    """
    try:
        if v is None:
            return '-'
        n = float(v)
        s = f"¥{n:,.{int(decimals)}f}"
        return (s + " (CNY)") if show_ccy else s
    except Exception:
        return '-'


def atomic_write_text(path: str | Path, content: str, *, encoding: str = 'utf-8') -> None:
    """Best-effort atomic write (write to tmp then replace)."""
    p = Path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + f'.tmp.{os.getpid()}')
    tmp.write_text(content, encoding=encoding)
    tmp.replace(p)


def atomic_write_json(path: str | Path, obj: Any, *, encoding: str = 'utf-8', indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent) + '\n', encoding=encoding)


def safe_read_csv(path: Path) -> pd.DataFrame:
    """Safe CSV reader.

    Treat header-only / empty / invalid CSV as empty DataFrame.
    """
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except EmptyDataError:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def copy_if_exists(src: Path, dst: Path) -> bool:
    """Copy file only when src exists and is non-empty.

    Return True if copied, False otherwise.
    """
    try:
        if src.exists() and src.stat().st_size > 0:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return True
    except Exception:
        return False
    return False


def is_fresh(path: Path, max_age_sec: int) -> bool:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        age = time.time() - path.stat().st_mtime
        return age <= float(max_age_sec)
    except Exception:
        return False


def load_cached_json(path: Path) -> dict | None:
    """Best-effort cached JSON loader.

    Returns None if file is missing/invalid/clearly incomplete.
    """
    try:
        if not path.exists() or path.stat().st_size <= 2:
            return None
        obj = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(obj, dict):
            return None
        # sanity keys
        if 'as_of_utc' not in obj and 'filters' not in obj:
            return None
        return obj
    except Exception:
        return None


def has_shared_required_data(symbol: str, shared_dir: Path) -> bool:
    """Return True when shared required_data artifacts exist and are readable.

    - raw json must exist and be non-empty
    - parsed csv must exist and be non-empty (header-only is accepted)
    """
    sym = str(symbol)
    raw_src = shared_dir / 'raw' / f"{sym}_required_data.json"
    parsed_src = shared_dir / 'parsed' / f"{sym}_required_data.csv"

    if not (raw_src.exists() and raw_src.stat().st_size > 0):
        return False
    if not (parsed_src.exists() and parsed_src.stat().st_size > 0):
        return False

    try:
        _ = safe_read_csv(parsed_src)
    except Exception:
        return False

    return True
