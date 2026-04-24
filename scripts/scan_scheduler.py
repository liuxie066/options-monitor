#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


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


def _market_session_dt(now_market: datetime, market_tz: ZoneInfo, session_time: time) -> datetime:
    return datetime.combine(now_market.date(), session_time, tzinfo=market_tz)


def _next_trading_day(day):
    next_day = day + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day


def _scheduled_notify_targets(
    *,
    now_market: datetime,
    market_tz: ZoneInfo,
    market_open: time,
    market_close: time,
    break_start: time | None,
    break_end: time | None,
    first_after_open_min: int,
    interval_min: int,
    final_before_close_min: int,
) -> list[datetime]:
    open_dt = _market_session_dt(now_market, market_tz, market_open)
    close_dt = _market_session_dt(now_market, market_tz, market_close)
    first_target = open_dt + timedelta(minutes=max(first_after_open_min, 0))
    final_target = close_dt - timedelta(minutes=max(final_before_close_min, 0))
    cadence = max(interval_min, 1)

    targets: list[datetime] = []
    cursor = first_target
    while cursor <= final_target:
        targets.append(cursor)
        cursor += timedelta(minutes=cadence)

    if final_target >= open_dt and all(t != final_target for t in targets):
        targets.append(final_target)

    targets = sorted(set(targets))
    return [
        target
        for target in targets
        if is_market_hours(target, market_open, market_close, break_start=break_start, break_end=break_end)
    ]


def _next_target_after(
    *,
    now_market: datetime,
    market_tz: ZoneInfo,
    market_open: time,
    market_close: time,
    break_start: time | None,
    break_end: time | None,
    first_after_open_min: int,
    interval_min: int,
    final_before_close_min: int,
) -> datetime:
    day_cursor = now_market
    for _ in range(8):
        targets = _scheduled_notify_targets(
            now_market=day_cursor,
            market_tz=market_tz,
            market_open=market_open,
            market_close=market_close,
            break_start=break_start,
            break_end=break_end,
            first_after_open_min=first_after_open_min,
            interval_min=interval_min,
            final_before_close_min=final_before_close_min,
        )
        for target in targets:
            if target > now_market:
                return target.astimezone(timezone.utc)

        next_day = _next_trading_day(day_cursor.date())
        day_cursor = datetime.combine(next_day, time(0, 0), tzinfo=market_tz)

    return now_market.astimezone(timezone.utc) + timedelta(days=1)


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

    first_after_open_min = int(schedule_cfg.get('first_notify_after_open_min', 30))
    interval_min = int(schedule_cfg.get('notify_interval_min', schedule_cfg.get('interval_min', 60)))
    final_before_close_min = int(schedule_cfg.get('final_notify_before_close_min', 10))
    notify_cooldown_min = interval_min

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

    targets = _scheduled_notify_targets(
        now_market=now_market,
        market_tz=market_tz,
        market_open=market_open,
        market_close=market_close,
        break_start=break_start,
        break_end=break_end,
        first_after_open_min=first_after_open_min,
        interval_min=interval_min,
        final_before_close_min=final_before_close_min,
    )
    due_targets = [target for target in targets if target <= now_market]
    due_target = due_targets[-1] if due_targets else None
    next_target = next(
        (target for target in targets if target > now_market),
        None,
    )

    if force:
        should_run_scan = True
        is_notify_window_open = True
        reason = 'force 模式：忽略交易时段目标点直接执行。'
        next_run = now_utc
    elif not in_hours:
        should_run_scan = False
        is_notify_window_open = False
        reason = '交易时段外：不扫描、不通知。'
        next_run = _next_target_after(
            now_market=now_market,
            market_tz=market_tz,
            market_open=market_open,
            market_close=market_close,
            break_start=break_start,
            break_end=break_end,
            first_after_open_min=first_after_open_min,
            interval_min=interval_min,
            final_before_close_min=final_before_close_min,
        )
    elif due_target is None:
        should_run_scan = False
        is_notify_window_open = False
        reason = f'交易时段内，等待开盘后 {first_after_open_min} 分钟的首次通知点。'
        next_run = (next_target.astimezone(timezone.utc) if next_target else now_utc)
    else:
        due_target_utc = due_target.astimezone(timezone.utc)
        already_processed = (
            last_scan is not None
            and last_scan.astimezone(timezone.utc) >= due_target_utc
        )
        should_run_scan = not already_processed
        is_notify_window_open = not already_processed
        if already_processed:
            reason = f'当前通知点 {due_target.strftime("%H:%M")} 已处理，等待下一个通知点。'
            if next_target is None:
                next_run = _next_target_after(
                    now_market=now_market,
                    market_tz=market_tz,
                    market_open=market_open,
                    market_close=market_close,
                    break_start=break_start,
                    break_end=break_end,
                    first_after_open_min=first_after_open_min,
                    interval_min=interval_min,
                    final_before_close_min=final_before_close_min,
                )
            else:
                next_run = next_target.astimezone(timezone.utc)
        else:
            reason = f'到达通知点 {due_target.strftime("%H:%M")}：执行扫描并允许通知。'
            next_run = now_utc

    # Also expose Beijing-time for readability/debuggability.
    open_dt_market = datetime.combine(now_market.date(), market_open, tzinfo=market_tz)
    close_dt_market = datetime.combine(now_market.date(), market_close, tzinfo=market_tz)

    return SchedulerDecision(
        now_utc=to_iso(now_utc),
        now_market=now_market.isoformat(),
        now_beijing=now_bj.isoformat(),
        in_market_hours=in_hours,
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

    if config_path.suffix.lower() != '.json':
        raise SystemExit('[CONFIG_ERROR] scheduler config must be a .json file')
    cfg = json.loads(config_path.read_text(encoding='utf-8'))

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
        cmd = [sys.executable, '-m', 'src.interfaces.cli.main', 'scan-pipeline', '--config', str(config_path)]
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


# NOTE: market-session selection lives in domain.domain.select_markets_to_run.
