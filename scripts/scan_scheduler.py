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
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')


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


def decide(schedule_cfg: dict, state: dict, now_utc: datetime, account: str | None = None, *, schedule_key: str = 'schedule') -> SchedulerDecision:
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

    should_run_scan = False
    reason = ''

    if (not in_window) and (not monitor_off_hours):
        should_run_scan = False
        reason = '窗口外：不扫描。'
    elif last_scan is None:
        should_run_scan = True
        reason = '首次运行，无历史扫描记录。'
    else:
        elapsed = now_utc - last_scan.astimezone(timezone.utc)
        if elapsed >= timedelta(minutes=interval_min):
            should_run_scan = True
            reason = f'距离上次扫描已超过 {interval_min} 分钟。'
        elif schedule_v2_enabled and force_final_scan:
            should_run_scan = True
            reason = '窗口收盘前最后一跳：强制扫描。'
        else:
            should_run_scan = False
            reason = f'距离上次扫描不足 {interval_min} 分钟。'

    is_notify_window_open = False
    if schedule_v2_enabled:
        off_window_notify = bool((schedule_v2.get('off_window') or {}).get('notify', False))
        if (not in_window) and (not off_window_notify):
            is_notify_window_open = False
        elif last_notify is None:
            is_notify_window_open = True
        else:
            is_notify_window_open = (now_utc - last_notify.astimezone(timezone.utc)) >= timedelta(minutes=notify_cooldown_min)
    elif (not in_window) and (not monitor_off_hours):
        is_notify_window_open = False
    elif last_notify is None:
        is_notify_window_open = True
    else:
        is_notify_window_open = (now_utc - last_notify.astimezone(timezone.utc)) >= timedelta(minutes=notify_cooldown_min)

    if not should_run_scan:
        if last_scan is None or interval_min >= 10**8:
            next_run = now_utc + timedelta(hours=24)
        else:
            next_run = last_scan.astimezone(timezone.utc) + timedelta(minutes=interval_min)
    else:
        next_run = now_utc

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


def main():
    parser = argparse.ArgumentParser(description='Scan scheduler / frequency controller for options-monitor')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='output/state', help='Directory for scheduler_state.json (default: output/state)')
    parser.add_argument('--state', default=None, help='[deprecated] explicit scheduler_state.json path. Prefer --state-dir.' )
    parser.add_argument('--schedule-key', default='schedule', help='Top-level key to read schedule config from (default: schedule). Example: schedule_hk' )
    parser.add_argument('--account', default=None, help='Account id for per-account notify cooldown state (optional).')
    parser.add_argument('--run-if-due', action='store_true', help='When due, run scripts/run_pipeline.py --config <config>')
    parser.add_argument('--mark-notified', action='store_true', help='Update last_notify_utc to now (call this only AFTER you actually sent a notification)')
    parser.add_argument('--mark-scanned', action='store_true', help='Update last_scan_utc to now (call this only AFTER you actually ran a scan)')
    parser.add_argument('--jsonl', action='store_true', help='Print a single-line JSON decision (for automation)')
    parser.add_argument('--force', action='store_true', help='Force running regardless of schedule')
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (base / config_path).resolve()
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (base / state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.state:
        state_path = Path(args.state)
        if not state_path.is_absolute():
            state_path = (base / state_path).resolve()
    else:
        state_path = (state_dir / 'scheduler_state.json').resolve()

    # config supports JSON (preferred) or YAML (legacy)
    if config_path.suffix.lower() == '.json':
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    schedule_key = str(args.schedule_key or 'schedule')
    schedule_cfg = cfg.get(schedule_key, {}) or {}
    schedule_enabled = bool(schedule_cfg.get('enabled', True))

    now_utc = datetime.now(timezone.utc)
    state = read_state(state_path)
    decision = decide(schedule_cfg, state, now_utc, account=(str(args.account) if args.account else None), schedule_key=schedule_key)

    if args.force:
        decision.should_run_scan = True
        decision.is_notify_window_open = True
        decision.reason = 'force 模式：忽略频率控制直接执行。'

    payload = asdict(decision)
    payload['should_notify'] = bool(payload.get('is_notify_window_open'))

    if args.jsonl:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not schedule_enabled and not args.force:
        print('[INFO] schedule disabled in config; no scheduling enforcement applied.')
        return

    if args.mark_notified:
        now_s = to_iso(datetime.now(timezone.utc))
        state['last_notify_utc'] = now_s
        # keep per-account map as optional debug info
        if args.account:
            m = state.get('last_notify_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(args.account)] = now_s
            state['last_notify_utc_by_account'] = m
        write_state(state_path, state)
        print(f'[DONE] marked notified -> {state_path}')
        return

    if args.mark_scanned:
        now_s = to_iso(datetime.now(timezone.utc))
        state['last_scan_utc'] = now_s
        if args.account:
            m = state.get('last_scan_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(args.account)] = now_s
            state['last_scan_utc_by_account'] = m
        write_state(state_path, state)
        print(f'[DONE] marked scanned -> {state_path}')
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

# NOTE: market-session helpers live in scripts/send_if_needed_multi.py (multi-account entrypoint).
