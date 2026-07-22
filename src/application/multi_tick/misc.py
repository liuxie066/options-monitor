from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

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


HEADROOM_RE = re.compile(r"余量\s+(?P<val>[-+]?¥?\$?[0-9,]+(?:\.[0-9]+)?)")
CNY_RE = re.compile(r"¥\s*(?P<num>[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)")
COVER_RE = re.compile(r"cover\s+(?P<num>-?[0-9]+)")


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
