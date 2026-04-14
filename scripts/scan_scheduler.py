#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.domain.engine import decide_scheduler_timing


@dataclass
class SchedulerDecision:
    now_utc: str
    now_market: str
    now_beijing: str
    in_market_hours: bool
    interval_min: int
    notify_cooldown_min: int
    should_run_scan: bool
    is_notify_window_open: bool
    reason: str
    next_run_utc: str
    next_run_market: str
    next_run_beijing: str
    market_open_beijing: str
    market_close_beijing: str
    schedule_key: str


STATE_DEFAULT = {
    'last_scan_utc': None,  # legacy (shared scan clock)
    'last_scan_utc_by_account': {},
    'last_notify_utc': None,  # legacy
    'last_notify_utc_by_account': {},
}


def read_state(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_text(encoding='utf-8'))
    return STATE_DEFAULT.copy()


def write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    from scripts.io_utils import atomic_write_text
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(':', 1)
    return time(hour=int(hour), minute=int(minute))


def is_market_hours(now_market: datetime, market_open: time, market_close: time, break_start: time | None = None, break_end: time | None = None) -> bool:
    """Return whether we should monitor during market hours.

    Supports an optional mid-day break window (e.g., HK 12:00-13:00).
    Break window is treated as [break_start, break_end) in local market time.
    """
    if now_market.weekday() >= 5:
        return False
    current = now_market.time()
    if not (market_open <= current <= market_close):
        return False

    if break_start is not None and break_end is not None:
        # half-open break interval
        if _time_in_range(current, break_start, break_end) and current != break_end:
            return False
    return True


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def maybe_parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _time_in_range(t: time, start: time, end: time) -> bool:
    # inclusive range; supports wrap-around
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def decide(
    schedule_cfg: dict,
    state: dict,
    now_utc: datetime,
    account: str | None = None,
    *,
    schedule_key: str = 'schedule',
    force: bool = False,
) -> SchedulerDecision:
    market_tz = ZoneInfo(schedule_cfg.get('market_timezone', 'America/New_York'))
    now_market = now_utc.astimezone(market_tz)

    bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))
    now_bj = now_utc.astimezone(bj_tz)

    market_open = parse_hhmm(schedule_cfg.get('market_open', '09:30'))
    market_close = parse_hhmm(schedule_cfg.get('market_close', '16:00'))

    break_start = None
    break_end = None
    if schedule_cfg.get('market_break_start') and schedule_cfg.get('market_break_end'):
        break_start = parse_hhmm(schedule_cfg.get('market_break_start'))
        break_end = parse_hhmm(schedule_cfg.get('market_break_end'))

    in_hours = is_market_hours(now_market, market_open, market_close, break_start=break_start, break_end=break_end)

    schedule_v2 = schedule_cfg.get('schedule_v2') or {}
    schedule_v2_enabled = bool(schedule_v2.get('enabled', False))

    # Base notification cooldown (minutes).
    notify_cooldown_min = int(schedule_cfg.get('notify_cooldown_min', 60))

    # Decide scan interval + window semantics.
    monitor_off_hours = bool(schedule_cfg.get('monitor_off_hours', False))
    in_window = in_hours
    force_final_scan = False

    if schedule_v2_enabled:
        # v2 single-window model: map market open/close into Beijing time.
        v2_market_tz = ZoneInfo(schedule_v2.get('market_timezone', schedule_cfg.get('market_timezone', 'America/New_York')))
        v2_bj_tz = ZoneInfo(schedule_v2.get('beijing_timezone', schedule_cfg.get('beijing_timezone', 'Asia/Shanghai')))
        now_v2_market = now_utc.astimezone(v2_market_tz)
        now_v2_bj = now_utc.astimezone(v2_bj_tz)

        window_cfg = schedule_v2.get('window') or {}
        window_mode = str(schedule_v2.get('window_mode', 'market_mapped'))

        open_from_market = str(window_cfg.get('open_from_market', 'market_open'))
        close_from_market = str(window_cfg.get('close_from_market', 'market_close'))

        if window_mode == 'market_mapped':
            open_src = market_open if open_from_market == 'market_open' else market_close
            close_src = market_close if close_from_market == 'market_close' else market_open

            open_dt_v2_market = datetime.combine(now_v2_market.date(), open_src, tzinfo=v2_market_tz)
            close_dt_v2_market = datetime.combine(now_v2_market.date(), close_src, tzinfo=v2_market_tz)
            open_v2_bj = open_dt_v2_market.astimezone(v2_bj_tz)
            close_v2_bj = close_dt_v2_market.astimezone(v2_bj_tz)
            in_window = _time_in_range(now_v2_bj.time(), open_v2_bj.time(), close_v2_bj.time())

            if in_window:
                close_anchor = close_v2_bj
                while close_anchor < now_v2_bj:
                    close_anchor += timedelta(days=1)
                mins_to_close = (close_anchor - now_v2_bj).total_seconds() / 60.0

                force_before_close_min = int((schedule_v2.get('scan') or {}).get('force_final_scan_before_close_min', 0))
                force_final_scan = mins_to_close <= max(force_before_close_min, 0)
        else:
            in_window = in_hours

        off_window = schedule_v2.get('off_window') or {}
        monitor_off_hours = bool(off_window.get('scan', False))
        notify_cooldown_min = int((schedule_v2.get('notify') or {}).get('cooldown_min', notify_cooldown_min))
        interval_min = int((schedule_v2.get('scan') or {}).get('cadence_min', 10))
    else:
        interval_min: int
        if not in_hours and not monitor_off_hours:
            # No monitoring outside market hours
            interval_min = 10**9
        else:
            # In-hours: support Beijing split (dense before 2am, sparse after 2am until close)
            dense = int(schedule_cfg.get('market_dense_interval_min', schedule_cfg.get('market_hours_interval_min', 30)))
            sparse = int(schedule_cfg.get('market_sparse_interval_min', dense))

            # (bj_tz/now_bj already computed above)
            sparse_after = parse_hhmm(schedule_cfg.get('sparse_after_beijing', '02:00'))

            # Compute market close time in Beijing (time-of-day)
            close_dt_market = datetime.combine(now_market.date(), market_close, tzinfo=market_tz)
            close_bj_time = close_dt_market.astimezone(bj_tz).time()

            # Sparse window: [sparse_after, close_bj_time] (handles DST because close_bj_time changes)
            use_sparse = _time_in_range(now_bj.time(), sparse_after, close_bj_time)
            interval_min = sparse if use_sparse else dense

    last_scan = None
    try:
        if account:
            m = state.get('last_scan_utc_by_account')
            if isinstance(m, dict):
                last_scan = maybe_parse_dt(m.get(str(account)))
    except Exception:
        last_scan = None
    if last_scan is None:
        last_scan = maybe_parse_dt(state.get('last_scan_utc'))

    # Notify cooldown should be account-specific in multi-account mode.
    # If account is provided, read last_notify from the per-account map first.
    last_notify = None
    try:
        if account:
            m = state.get('last_notify_utc_by_account')
            if isinstance(m, dict):
                last_notify = maybe_parse_dt(m.get(str(account)))
    except Exception:
        last_notify = None
    if last_notify is None:
        # fallback to legacy global notify timestamp
        last_notify = maybe_parse_dt(state.get('last_notify_utc'))

    timing_decision = decide_scheduler_timing(
        now_utc=now_utc,
        last_scan_utc=last_scan,
        last_notify_utc=last_notify,
        in_window=bool(in_window),
        monitor_off_hours=bool(monitor_off_hours),
        interval_min=int(interval_min),
        notify_cooldown_min=int(notify_cooldown_min),
        schedule_v2_enabled=bool(schedule_v2_enabled),
        force_final_scan=bool(force_final_scan),
        off_window_notify=bool((schedule_v2.get('off_window') or {}).get('notify', False)),
        force=bool(force),
    )
    should_run_scan = bool(timing_decision.get('should_run_scan'))
    is_notify_window_open = bool(timing_decision.get('is_notify_window_open'))
    reason = str(timing_decision.get('reason') or '')
    next_run_raw = timing_decision.get('next_run_utc')
    next_run = next_run_raw if isinstance(next_run_raw, datetime) else now_utc

    # Also expose Beijing-time for readability/debuggability.
    open_dt_market = datetime.combine(now_market.date(), market_open, tzinfo=market_tz)
    close_dt_market = datetime.combine(now_market.date(), market_close, tzinfo=market_tz)

    return SchedulerDecision(
        now_utc=to_iso(now_utc),
        now_market=now_market.isoformat(),
        now_beijing=now_bj.isoformat(),
        in_market_hours=in_window,
        interval_min=interval_min,
        notify_cooldown_min=notify_cooldown_min,
        should_run_scan=should_run_scan,
        is_notify_window_open=is_notify_window_open,
        reason=reason,
        next_run_utc=to_iso(next_run),
        next_run_market=next_run.astimezone(market_tz).isoformat(),
        next_run_beijing=next_run.astimezone(bj_tz).isoformat(),
        market_open_beijing=open_dt_market.astimezone(bj_tz).isoformat(),
        market_close_beijing=close_dt_market.astimezone(bj_tz).isoformat(),
        schedule_key=str(schedule_key),
    )


def run_scheduler(
    *,
    config: str | Path,
    state_dir: str | Path = 'output/state',
    state: str | Path | None = None,
    schedule_key: str = 'schedule',
    account: str | None = None,
    run_if_due: bool = False,
    mark_notified: bool = False,
    mark_scanned: bool = False,
    jsonl: bool = False,
    force: bool = False,
    base_dir: Path | None = None,
) -> dict:
    """执行调度判定并处理状态副作用。"""
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = (base / config_path).resolve()

    state_dir_path = Path(state_dir)
    if not state_dir_path.is_absolute():
        state_dir_path = (base / state_dir_path).resolve()
    state_dir_path.mkdir(parents=True, exist_ok=True)

    if state:
        state_path = Path(state)
        if not state_path.is_absolute():
            state_path = (base / state_path).resolve()
    else:
        state_path = (state_dir_path / 'scheduler_state.json').resolve()

    if config_path.suffix.lower() == '.json':
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding='utf-8'))

    schedule_key_val = str(schedule_key or 'schedule')
    schedule_cfg = cfg.get(schedule_key_val, {}) or {}
    schedule_enabled = bool(schedule_cfg.get('enabled', True))

    now_utc = datetime.now(timezone.utc)
    state_data = read_state(state_path)
    decision = decide(
        schedule_cfg,
        state_data,
        now_utc,
        account=(str(account) if account else None),
        schedule_key=schedule_key_val,
        force=bool(force),
    )

    payload = asdict(decision)
    payload['should_notify'] = bool(payload.get('is_notify_window_open'))

    if jsonl:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not schedule_enabled and not force:
        print('[INFO] schedule disabled in config; no scheduling enforcement applied.')
        return payload

    if mark_notified:
        now_s = to_iso(datetime.now(timezone.utc))
        state_data['last_notify_utc'] = now_s
        if account:
            m = state_data.get('last_notify_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(account)] = now_s
            state_data['last_notify_utc_by_account'] = m
        write_state(state_path, state_data)
        print(f'[DONE] marked notified -> {state_path}')
        return payload

    if mark_scanned:
        now_s = to_iso(datetime.now(timezone.utc))
        state_data['last_scan_utc'] = now_s
        if account:
            m = state_data.get('last_scan_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(account)] = now_s
            state_data['last_scan_utc_by_account'] = m
        write_state(state_path, state_data)
        print(f'[DONE] marked scanned -> {state_path}')
        return payload

    if run_if_due and decision.should_run_scan:
        cmd = [sys.executable, 'scripts/run_pipeline.py', '--config', str(config_path)]
        print(f"[RUN] {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(base))
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        state_data['last_scan_utc'] = to_iso(datetime.now(timezone.utc))
        write_state(state_path, state_data)
        print(f'[DONE] scheduler state -> {state_path}')
    elif run_if_due:
        print('[SKIP] 当前未到应扫描时间。')

    return payload


# NOTE: market-session helpers live in scripts/send_if_needed_multi.py (multi-account entrypoint).
