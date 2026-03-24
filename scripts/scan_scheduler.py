#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    in_market_hours: bool
    interval_min: int
    notify_cooldown_min: int
    should_run_scan: bool
    should_notify: bool
    reason: str
    next_run_utc: str
    next_run_market: str


STATE_DEFAULT = {
    'last_scan_utc': None,
    'last_notify_utc': None,
}


def read_state(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_text(encoding='utf-8'))
    return STATE_DEFAULT.copy()


def write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(':', 1)
    return time(hour=int(hour), minute=int(minute))


def is_market_hours(now_market: datetime, market_open: time, market_close: time) -> bool:
    if now_market.weekday() >= 5:
        return False
    current = now_market.time()
    return market_open <= current <= market_close


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


def decide(schedule_cfg: dict, state: dict, now_utc: datetime) -> SchedulerDecision:
    market_tz = ZoneInfo(schedule_cfg.get('market_timezone', 'America/New_York'))
    now_market = now_utc.astimezone(market_tz)
    market_open = parse_hhmm(schedule_cfg.get('market_open', '09:30'))
    market_close = parse_hhmm(schedule_cfg.get('market_close', '16:00'))
    in_hours = is_market_hours(now_market, market_open, market_close)

    # Base notification cooldown (minutes).
    # Content-aware overrides (e.g., HIGH-priority before 02:00 Beijing) are implemented in send_if_needed_multi
    # after the pipeline generates the notification text.
    notify_cooldown_min = int(schedule_cfg.get('notify_cooldown_min', 60))

    # Decide scan interval
    monitor_off_hours = bool(schedule_cfg.get('monitor_off_hours', False))

    interval_min: int
    if not in_hours and not monitor_off_hours:
        # No monitoring outside market hours
        interval_min = 10**9
    else:
        # In-hours: support Beijing split (dense before 2am, sparse after 2am until close)
        dense = int(schedule_cfg.get('market_dense_interval_min', schedule_cfg.get('market_hours_interval_min', 30)))
        sparse = int(schedule_cfg.get('market_sparse_interval_min', dense))

        bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))
        now_bj = now_utc.astimezone(bj_tz)
        sparse_after = parse_hhmm(schedule_cfg.get('sparse_after_beijing', '02:00'))

        # Compute market close time in Beijing (time-of-day)
        close_dt_market = datetime.combine(now_market.date(), market_close, tzinfo=market_tz)
        close_bj_time = close_dt_market.astimezone(bj_tz).time()

        # Sparse window: [sparse_after, close_bj_time] (handles DST because close_bj_time changes)
        use_sparse = _time_in_range(now_bj.time(), sparse_after, close_bj_time)
        interval_min = sparse if use_sparse else dense

        # Notification cooldown is handled separately (content-aware) in send_if_needed_multi.
        # Scheduler keeps a single base cooldown (notify_cooldown_min) here.

    last_scan = maybe_parse_dt(state.get('last_scan_utc'))
    last_notify = maybe_parse_dt(state.get('last_notify_utc'))

    should_run_scan = False
    reason = ''

    if (not in_hours) and (not monitor_off_hours):
        should_run_scan = False
        reason = '非交易时段：不监控。'
    elif last_scan is None:
        should_run_scan = True
        reason = '首次运行，无历史扫描记录。'
    else:
        elapsed = now_utc - last_scan.astimezone(timezone.utc)
        if elapsed >= timedelta(minutes=interval_min):
            should_run_scan = True
            reason = f'距离上次扫描已超过 {interval_min} 分钟。'
        else:
            should_run_scan = False
            reason = f'距离上次扫描不足 {interval_min} 分钟。'

    should_notify = False
    if (not in_hours) and (not monitor_off_hours):
        should_notify = False
    elif last_notify is None:
        should_notify = True
    else:
        should_notify = (now_utc - last_notify.astimezone(timezone.utc)) >= timedelta(minutes=notify_cooldown_min)

    if not should_run_scan:
        if last_scan is None or interval_min >= 10**8:
            next_run = now_utc + timedelta(hours=24)
        else:
            next_run = last_scan.astimezone(timezone.utc) + timedelta(minutes=interval_min)
    else:
        next_run = now_utc

    return SchedulerDecision(
        now_utc=to_iso(now_utc),
        now_market=now_market.isoformat(),
        in_market_hours=in_hours,
        interval_min=interval_min,
        notify_cooldown_min=notify_cooldown_min,
        should_run_scan=should_run_scan,
        should_notify=should_notify,
        reason=reason,
        next_run_utc=to_iso(next_run),
        next_run_market=next_run.astimezone(market_tz).isoformat(),
    )


def main():
    parser = argparse.ArgumentParser(description='Scan scheduler / frequency controller for options-monitor')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', default='output/state/scheduler_state.json')
    parser.add_argument('--run-if-due', action='store_true', help='When due, run scripts/run_pipeline.py --config <config>')
    parser.add_argument('--mark-notified', action='store_true', help='Update last_notify_utc to now (call this only AFTER you actually sent a notification)')
    parser.add_argument('--jsonl', action='store_true', help='Print a single-line JSON decision (for automation)')
    parser.add_argument('--force', action='store_true', help='Force running regardless of schedule')
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (base / config_path).resolve()
    state_path = Path(args.state)
    if not state_path.is_absolute():
        state_path = (base / state_path).resolve()

    # config supports JSON (preferred) or YAML (legacy)
    if config_path.suffix.lower() == '.json':
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    schedule_cfg = cfg.get('schedule', {}) or {}
    schedule_enabled = bool(schedule_cfg.get('enabled', True))

    now_utc = datetime.now(timezone.utc)
    state = read_state(state_path)
    decision = decide(schedule_cfg, state, now_utc)

    if args.force:
        decision.should_run_scan = True
        decision.reason = 'force 模式：忽略频率控制直接执行。'

    if args.jsonl:
        print(json.dumps(asdict(decision), ensure_ascii=False))
    else:
        print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))

    if not schedule_enabled and not args.force:
        print('[INFO] schedule disabled in config; no scheduling enforcement applied.')
        return

    if args.mark_notified:
        state['last_notify_utc'] = to_iso(datetime.now(timezone.utc))
        write_state(state_path, state)
        print(f'[DONE] marked notified -> {state_path}')
        return

    if args.run_if_due and decision.should_run_scan:
        cmd = [sys.executable, 'scripts/run_pipeline.py', '--config', str(config_path)]
        print(f"[RUN] {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(base))
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        state['last_scan_utc'] = to_iso(datetime.now(timezone.utc))
        # NOTE: do NOT update last_notify_utc here.
        # last_notify_utc should only be updated after a notification was actually sent.
        write_state(state_path, state)
        print(f'[DONE] scheduler state -> {state_path}')
    elif args.run_if_due:
        print('[SKIP] 当前未到应扫描时间。')


if __name__ == '__main__':
    main()
