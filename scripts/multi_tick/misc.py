from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from scripts.io_utils import read_json, atomic_write_json as write_json


DEBUG = False


def set_debug(flag: bool) -> None:
    global DEBUG
    DEBUG = bool(flag)


def log(msg: str) -> None:
    if DEBUG:
        print(msg)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(':', 1)
    return time(hour=int(hour), minute=int(minute))


def maybe_parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def atomic_symlink(path: Path, target: Path, *, tmp_dir: Path | None = None):
    tmp_root = tmp_dir or path.parent
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = tmp_root / f'{path.name}.tmp'
    if tmp.exists() or tmp.is_symlink():
        try:
            tmp.unlink(missing_ok=True)
        except IsADirectoryError:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    tmp.symlink_to(target, target_is_directory=True)
    if path.exists() and not path.is_symlink():
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    os.replace(tmp, path)


def update_legacy_output_link(path: Path, target: Path, *, tmp_dir: Path) -> bool:
    if path.exists() and not path.is_symlink():
        if os.access(path.parent, os.W_OK):
            raise RuntimeError(f"./output must be a symlink for multi-account mode: {path}")
        log(f'skip legacy output link update on read-only repo root: {path}')
        return False
    if not os.access(path.parent, os.W_OK):
        log(f'skip legacy output link update on read-only repo root: {path}')
        return False
    atomic_symlink(path, target, tmp_dir=tmp_dir)
    return True


def ensure_account_output_dir(d: Path):
    (d / 'raw').mkdir(parents=True, exist_ok=True)
    (d / 'parsed').mkdir(parents=True, exist_ok=True)
    (d / 'reports').mkdir(parents=True, exist_ok=True)
    (d / 'state').mkdir(parents=True, exist_ok=True)


@dataclass
class AccountResult:
    account: str
    ran_scan: bool
    should_notify: bool
    decision_reason: str
    notification_text: str


HEADROOM_RE = re.compile(r"加仓后余量\s+(?P<val>[-+]?¥?\$?[0-9,]+(?:\.[0-9]+)?)")
CNY_RE = re.compile(r"¥\s*(?P<num>[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)")
COVER_RE = re.compile(r"cover\s+(?P<num>-?[0-9]+)")


AUTO_CLOSE_APPLIED_RE = re.compile(r"applied_closed:\s*(?P<n>\d+)")
AUTO_CLOSE_CAND_RE = re.compile(r"candidates_should_close:\s*(?P<n>\d+)")
AUTO_CLOSE_ERR_RE = re.compile(r"^ERRORS:\s*(?P<n>\d+)\s*$", re.M)


def _safe_runlog_data(data: dict[str, Any] | None, max_items: int = 16) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= max_items:
            out['_truncated'] = True
            break
        kk = str(k)[:60]
        if isinstance(v, dict):
            out[kk] = {'_type': 'dict', 'size': len(v), 'keys': list(v.keys())[:8]}
        elif isinstance(v, (list, tuple, set)):
            out[kk] = {'_type': 'list', 'size': len(v)}
        elif isinstance(v, str):
            out[kk] = v[:160]
        elif v is None or isinstance(v, (bool, int, float)):
            out[kk] = v
        else:
            out[kk] = str(v)[:160]
    return out


def append_json_list(path: Path, item: dict, max_items: int = 500) -> None:
    try:
        arr = read_json(path, [])
        if not isinstance(arr, list):
            arr = []
        arr.append(item)
        if max_items > 0:
            arr = arr[-int(max_items):]
        write_json(path, arr)
    except Exception:
        pass
