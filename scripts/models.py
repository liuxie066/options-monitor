"""Typed models (Stage 1 infrastructure).

These are lightweight dataclasses/Ty pedDicts used to make IO and pipeline boundaries explicit.
They are not yet wired into the runtime flow; later stages will adopt them incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict, Literal


class SchedulerDecision(TypedDict, total=False):
    now_utc: str
    now_market: str
    now_beijing: str
    in_run_window: bool
    should_run_scan: bool
    is_notify_window_open: bool
    should_notify: bool
    reason: str
    next_run_utc: str
    next_run_market: str
    next_run_beijing: str
    run_window_start_beijing: str
    run_window_end_beijing: str
    schedule_key: str


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    market: str
    created_at_utc: datetime


@dataclass(frozen=True)
class AccountContext:
    account: str
    report_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class PipelineResult:
    account: str
    ran_scan: bool
    should_notify: bool
    meaningful: bool
    reason: str
    notification_text: str = ''
    status: Literal['ok', 'skipped', 'failed'] = 'ok'
